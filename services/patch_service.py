"""
Patch Service — LLM-powered auto-fix loop for production failures.

When a failure is labeled, this service:
  1. Reads the relevant source file
  2. Pulls the full trace for that case
  3. Calls Claude Sonnet with failure context + code
  4. Extracts a concrete patch (unified diff or full replacement)
  5. For low/medium severity: applies the patch and runs the eval to verify
  6. For high/critical severity: writes the proposal to db/patches/ for human review

Usage (automatic — called by control_layer.label_failure):
    from services.patch_service import patch_service
    proposal = patch_service.propose(failure, case, trace_events)
    if proposal and failure.severity in ("low", "medium"):
        patch_service.apply_and_verify(proposal)

Manual trigger:
    from services.patch_service import patch_service
    from db.repository import list_unlabeled_failures
    for row in list_unlabeled_failures():
        patch_service.propose_from_row(row)
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from db.models import FailureLabel, Case

_PROJECT_ROOT = Path(__file__).parent.parent

# Maps failure source/suggested_patch → file path(s) to include in the prompt
_SOURCE_TO_FILES: dict[str, list[str]] = {
    "create_square_invoice_draft":  ["agents/invoice_graph.py"],
    "publish_invoice_node":         ["agents/invoice_graph.py"],
    "publish_invoice":              ["agents/invoice_graph.py"],
    "_run":                         ["bot.py"],
    "customer_service":             ["services/customer_service.py"],
    "product_service":              ["services/product_service.py"],
    "square_service":               ["services/square_service.py"],
    "supervisor":                   ["agents/supervisor_graph.py"],
    "guardrail":                    ["services/guardrail_service.py"],
    "pre_input":                    ["services/guardrail_service.py"],
    "pre_tool":                     ["services/guardrail_service.py"],
    "post_tool":                    ["services/guardrail_service.py"],
    "pre_output":                   ["services/guardrail_service.py"],
    "invoice_agent":                ["agents/invoice_graph.py"],
    "llm":                          ["agents/invoice_graph.py"],
}

_PATCH_TYPE_TO_FILES: dict[str, list[str]] = {
    "prompt":    ["agents/invoice_graph.py", "agents/supervisor_graph.py"],
    "guardrail": ["services/guardrail_service.py"],
    "tool":      ["services/square_service.py", "services/customer_service.py"],
    "routing":   ["agents/supervisor_graph.py"],
    "schema":    ["db/models.py", "agents/invoice_graph.py"],
    "workflow":  ["agents/invoice_graph.py"],
}

_AUTO_APPLY_SEVERITIES = {"low", "medium"}


@dataclass
class PatchProposal:
    patch_id: str
    failure_id: str
    case_id: str
    failure_type: str
    severity: str
    suggested_patch_type: str       # prompt | tool | guardrail | schema | routing | workflow
    target_files: list[str]         # relative paths
    explanation: str                # LLM explanation of root cause
    patch_content: str              # the actual code patch (diff or replacement block)
    raw_llm_output: str
    applied: bool = False
    eval_passed: Optional[bool] = None
    tags: list = field(default_factory=list)


class PatchService:
    """LLM-powered failure-to-patch loop."""

    def _patches_dir(self) -> Path:
        d = _PROJECT_ROOT / "db" / "patches"
        d.mkdir(exist_ok=True)
        return d

    def _read_file_for_context(self, rel_path: str) -> str:
        """Read a source file, return its content with line numbers (capped at 300 lines)."""
        path = _PROJECT_ROOT / rel_path
        if not path.exists():
            return f"[file not found: {rel_path}]"
        lines = path.read_text().splitlines()
        # Include full file if ≤ 300 lines, otherwise first 300
        shown = lines[:300]
        numbered = "\n".join(f"{i+1:4d}  {l}" for i, l in enumerate(shown))
        if len(lines) > 300:
            numbered += f"\n... ({len(lines) - 300} more lines truncated)"
        return f"### {rel_path}\n```python\n{numbered}\n```"

    def _gather_files(self, failure: FailureLabel) -> list[str]:
        """Select which source files to include in the prompt."""
        files = set()
        for f in _SOURCE_TO_FILES.get(failure.source, []):
            files.add(f)
        for f in _PATCH_TYPE_TO_FILES.get(failure.suggested_patch, []):
            files.add(f)
        if not files:
            # Fallback: include the file most likely related to the responsible layer
            layer_files = {
                "supervisor":     "agents/supervisor_graph.py",
                "invoice_agent":  "agents/invoice_graph.py",
                "guardrail":      "services/guardrail_service.py",
                "square":         "services/square_service.py",
                "human":          "bot.py",
            }
            fb = layer_files.get(failure.responsible_layer, "agents/invoice_graph.py")
            files.add(fb)
        return sorted(files)

    def _fetch_trace(self, case_id: str) -> list[dict]:
        """Fetch trace events for this case from Supabase (best-effort)."""
        try:
            from db.repository import _get_client
            client = _get_client()
            result = (
                client.table("trace_events")
                .select("event_type,layer,data,error,ts")
                .eq("case_id", case_id)
                .order("ts")
                .limit(30)
                .execute()
            )
            return result.data or []
        except Exception:
            return []

    def propose(
        self,
        failure: FailureLabel,
        case: Case,
        trace_events: Optional[list[dict]] = None,
    ) -> Optional[PatchProposal]:
        """Call Claude Sonnet to propose a fix for this failure.

        Returns a PatchProposal (written to disk) or None if the LLM call fails.
        """
        try:
            from langchain_anthropic import ChatAnthropic
            from langchain_core.messages import HumanMessage, SystemMessage
        except ImportError:
            logging.warning("[patch] langchain_anthropic not available — skipping patch proposal")
            return None

        try:
            # Gather source files
            target_files = self._gather_files(failure)
            file_contexts = "\n\n".join(self._read_file_for_context(f) for f in target_files)

            # Fetch trace if not provided
            if trace_events is None:
                trace_events = self._fetch_trace(case.case_id)

            trace_summary = json.dumps(trace_events, indent=2, default=str)[:3000]

            system = (
                "You are a senior Python engineer fixing a bug in a production LangGraph "
                "invoice agent for a winery. You will be given:\n"
                "  1. A labeled failure with severity, type, and description\n"
                "  2. The full trace of what happened in that case\n"
                "  3. The relevant source files\n\n"
                "Your task:\n"
                "  A. Identify the exact root cause (one sentence)\n"
                "  B. Propose the minimal code patch that fixes it\n"
                "  C. Format the patch as a unified diff OR a clearly marked replacement block\n\n"
                "Rules:\n"
                "  - Fix the root cause, not the symptom\n"
                "  - Keep the patch minimal — change only what is broken\n"
                "  - Do NOT refactor unrelated code\n"
                "  - Do NOT change the data model unless the patch type is 'schema'\n"
                "  - The patch must not introduce new dependencies\n\n"
                "Output format (strictly follow this):\n"
                "ROOT_CAUSE: <one sentence>\n\n"
                "PATCH:\n"
                "```diff\n"
                "<unified diff here, or if not applicable use a REPLACE block>\n"
                "```\n\n"
                "Or if a full section replacement is cleaner:\n"
                "PATCH:\n"
                "```python\n"
                "# FILE: <relative/path.py>\n"
                "# REPLACE lines <start>-<end>:\n"
                "<replacement code>\n"
                "```"
            )

            user = (
                f"FAILURE:\n"
                f"  type:              {failure.failure_type}\n"
                f"  severity:          {failure.severity}\n"
                f"  source:            {failure.source}\n"
                f"  responsible_layer: {failure.responsible_layer}\n"
                f"  suggested_patch:   {failure.suggested_patch}\n"
                f"  description:       {failure.description}\n\n"
                f"CASE:\n"
                f"  input:   {case.raw_input[:300]}\n"
                f"  intent:  {case.intent}\n"
                f"  agent:   {case.agent}\n"
                f"  outcome: {case.outcome}\n\n"
                f"TRACE (last 30 events):\n{trace_summary}\n\n"
                f"SOURCE FILES:\n{file_contexts}"
            )

            llm = ChatAnthropic(model="claude-sonnet-4-6", temperature=0, max_tokens=4096)
            result = llm.invoke([
                SystemMessage(content=system),
                HumanMessage(content=user),
            ])
            raw = result.content.strip()

            # Parse output
            explanation = ""
            patch_content = ""

            rc_match = re.search(r"ROOT_CAUSE:\s*(.+?)(?:\n\n|$)", raw, re.DOTALL)
            if rc_match:
                explanation = rc_match.group(1).strip()

            patch_match = re.search(r"PATCH:\s*```(?:diff|python)\n(.*?)```", raw, re.DOTALL)
            if patch_match:
                patch_content = patch_match.group(1).strip()

            proposal = PatchProposal(
                patch_id=uuid.uuid4().hex[:12],
                failure_id=failure.failure_id,
                case_id=case.case_id,
                failure_type=failure.failure_type,
                severity=failure.severity,
                suggested_patch_type=failure.suggested_patch,
                target_files=target_files,
                explanation=explanation or raw[:200],
                patch_content=patch_content or raw,
                raw_llm_output=raw,
            )

            # Write proposal to disk
            self._save_proposal(proposal)
            logging.info(
                "[patch] proposal created: %s | type=%s severity=%s",
                proposal.patch_id, failure.failure_type, failure.severity,
            )
            return proposal

        except Exception as e:
            logging.warning("[patch] propose failed: %s", e)
            return None

    def _save_proposal(self, proposal: PatchProposal) -> None:
        path = self._patches_dir() / f"{proposal.patch_id}.json"
        with open(path, "w") as f:
            json.dump({
                "patch_id":            proposal.patch_id,
                "failure_id":          proposal.failure_id,
                "case_id":             proposal.case_id,
                "failure_type":        proposal.failure_type,
                "severity":            proposal.severity,
                "suggested_patch_type": proposal.suggested_patch_type,
                "target_files":        proposal.target_files,
                "explanation":         proposal.explanation,
                "patch_content":       proposal.patch_content,
                "applied":             proposal.applied,
                "eval_passed":         proposal.eval_passed,
            }, f, indent=2)

    def apply_and_verify(self, proposal: PatchProposal) -> bool:
        """Apply the patch and run the eval suite to verify it doesn't regress.

        Returns True if patch applied and evals pass.
        Only auto-applies for low/medium severity — higher severities need human review.
        """
        if proposal.severity not in _AUTO_APPLY_SEVERITIES:
            logging.info(
                "[patch] severity=%s — NOT auto-applying patch %s. "
                "Review at db/patches/%s.json",
                proposal.severity, proposal.patch_id, proposal.patch_id,
            )
            return False

        if not proposal.patch_content:
            logging.warning("[patch] no patch content in proposal %s", proposal.patch_id)
            return False

        # Auto-apply: write patched content to target file(s)
        applied = self._apply_patch_to_files(proposal)
        if not applied:
            return False

        # Verify: run the eval suite
        passed = self._run_evals()
        proposal.applied = applied
        proposal.eval_passed = passed
        self._save_proposal(proposal)

        if passed:
            logging.info("[patch] APPLIED + VERIFIED: %s", proposal.patch_id)
            # Mark failure as patched
            try:
                from db.repository import _get_client
                client = _get_client()
                client.table("failure_labels").update(
                    {"patch_applied": True}
                ).eq("failure_id", proposal.failure_id).execute()
            except Exception:
                pass
        else:
            logging.warning(
                "[patch] patch %s applied but evals FAILED — patch needs manual review",
                proposal.patch_id,
            )

        return passed

    def _apply_patch_to_files(self, proposal: PatchProposal) -> bool:
        """Apply patch_content to target files.

        Handles two formats:
          1. Unified diff (--- a/file ... +++ b/file ...)
          2. REPLACE block (# FILE: path.py / # REPLACE lines X-Y: / new code)
        """
        content = proposal.patch_content

        # Format 1: unified diff
        if content.startswith("---") or "@@" in content[:200]:
            return self._apply_unified_diff(content, proposal.target_files)

        # Format 2: REPLACE block
        file_match = re.search(r"#\s*FILE:\s*(\S+)", content)
        lines_match = re.search(r"#\s*REPLACE lines\s+(\d+)-(\d+)", content)

        if file_match and lines_match:
            rel_path = file_match.group(1)
            start = int(lines_match.group(1)) - 1   # 0-indexed
            end   = int(lines_match.group(2))        # exclusive

            # Strip the header comments from replacement code
            code = re.sub(r"#\s*(FILE|REPLACE).*\n", "", content).strip()

            path = _PROJECT_ROOT / rel_path
            if not path.exists():
                logging.warning("[patch] target file not found: %s", rel_path)
                return False

            lines = path.read_text().splitlines(keepends=True)
            new_lines = lines[:start] + [code + "\n"] + lines[end:]
            path.write_text("".join(new_lines))
            logging.info("[patch] replaced lines %d-%d in %s", start + 1, end, rel_path)
            return True

        # Could not parse patch format — write it to a review file
        review_path = self._patches_dir() / f"{proposal.patch_id}_REVIEW_NEEDED.py"
        review_path.write_text(
            f"# Patch {proposal.patch_id} — could not auto-apply\n"
            f"# Failure: {proposal.failure_type} | severity: {proposal.severity}\n"
            f"# Root cause: {proposal.explanation}\n\n"
            f"{content}"
        )
        logging.warning(
            "[patch] could not auto-apply patch %s — review at %s",
            proposal.patch_id, review_path,
        )
        return False

    def _apply_unified_diff(self, diff_text: str, hint_files: list[str]) -> bool:
        """Apply a unified diff using Python's built-in patch logic."""
        try:
            import subprocess
            patch_path = self._patches_dir() / f"_tmp_{uuid.uuid4().hex[:8]}.patch"
            patch_path.write_text(diff_text)
            result = subprocess.run(
                ["patch", "-p1", "--dry-run", "-i", str(patch_path)],
                cwd=str(_PROJECT_ROOT),
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                logging.warning("[patch] dry-run failed: %s", result.stderr[:300])
                patch_path.unlink(missing_ok=True)
                return False
            # Dry run passed — apply for real
            subprocess.run(
                ["patch", "-p1", "-i", str(patch_path)],
                cwd=str(_PROJECT_ROOT),
                check=True,
                capture_output=True,
            )
            patch_path.unlink(missing_ok=True)
            return True
        except Exception as e:
            logging.warning("[patch] unified diff apply failed: %s", e)
            return False

    def _run_evals(self) -> bool:
        """Run the eval suite and return True if all cases pass."""
        try:
            import subprocess
            result = subprocess.run(
                ["python3", "-m", "db.eval_runner"],
                cwd=str(_PROJECT_ROOT),
                capture_output=True,
                text=True,
                timeout=60,
            )
            passed = result.returncode == 0
            if not passed:
                logging.warning("[patch] eval run output:\n%s", result.stdout[-500:])
            return passed
        except Exception as e:
            logging.warning("[patch] eval run failed: %s", e)
            return False

    def propose_from_failure_id(self, failure_id: str) -> Optional[PatchProposal]:
        """Convenience: look up failure + case from DB and propose a patch."""
        try:
            from db.repository import _get_client
            client = _get_client()

            fl_row = (
                client.table("failure_labels")
                .select("*").eq("failure_id", failure_id).limit(1).execute()
            ).data
            if not fl_row:
                logging.warning("[patch] failure not found: %s", failure_id)
                return None
            fl = fl_row[0]

            case_row = (
                client.table("agent_cases")
                .select("*").eq("case_id", fl["case_id"]).limit(1).execute()
            ).data
            if not case_row:
                logging.warning("[patch] case not found: %s", fl["case_id"])
                return None
            cr = case_row[0]

            from db.models import Case
            case = Case(
                case_id=cr["case_id"], sender_id=cr["sender_id"],
                user_id=cr["user_id"], thread_id=cr.get("thread_id", ""),
                raw_input=cr.get("raw_input", ""), intent=cr.get("intent", ""),
                agent=cr.get("agent", ""), risk_level=cr.get("risk_level", "low"),
                outcome=cr.get("outcome", ""), error_summary=cr.get("error_summary", ""),
            )
            from db.models import FailureLabel
            failure = FailureLabel(
                failure_id=fl["failure_id"], case_id=fl["case_id"],
                failure_type=fl["failure_type"], severity=fl["severity"],
                source=fl.get("source", ""), responsible_layer=fl.get("responsible_layer", ""),
                description=fl.get("description", ""), suggested_patch=fl.get("suggested_patch", ""),
                confidence=fl.get("confidence", 1.0),
            )
            return self.propose(failure, case)
        except Exception as e:
            logging.warning("[patch] propose_from_failure_id failed: %s", e)
            return None


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

patch_service = PatchService()
