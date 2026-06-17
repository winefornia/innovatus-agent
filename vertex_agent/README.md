# Tasting-room coordinator — Vertex ADK migration

Goal-driven replacement for the LangGraph pipeline (`agents/case_desk_graph.py` +
the 26-state machine). The agent reads a case, sees the goal sub-conditions, and
proposes the single next action that closes the biggest gap — every
facility/client/payment action routed through the **existing Google Chat approval
card** (kept on purpose). Powered by **Claude**, not Gemini.

This package is **parallel** to the live system — nothing in production imports
it yet, so the current FastAPI/LangGraph path keeps running until cutover.

## Files
- `goal_model.py` — the goal sub-conditions + `derive_goal_state()` (the anti-state-machine; derived from existing reservation fields, no data changes).
- `tools.py` — ADK tools wrapping existing repository/service code: `get_case`, `list_open_cases`, `propose_action` (the HITL gate — posts the approval card, never sends email).
- `agent.py` — the ADK `LlmAgent` (`root_agent`) with Claude + the coordination instructions.

## Run locally
```bash
pip install -r requirements.txt        # includes google-adk + litellm
export ANTHROPIC_API_KEY=...           # Claude-direct (simplest)
# plus the usual SUPABASE_URL / SUPABASE_SERVICE_KEY so the tools can read cases
adk web                                # visual chat at http://localhost:8000
#   or: adk run vertex_agent
```
Ask it: *"coordinate reservation <id>"* — it should load the case, name the gap,
and propose one action (which posts an approval card to the Chat space).

## Model: two ways to power it with Claude
- **Claude-direct (default):** `TR_AGENT_MODEL=anthropic/claude-sonnet-4-6`, set `ANTHROPIC_API_KEY`. Reuses the key you already have. Fastest to test.
- **Claude-on-Vertex (data stays in GCP):** enable Claude in Vertex Model Garden, set `TR_AGENT_MODEL` to the Vertex partner-model string and configure ADK for Vertex (`GOOGLE_CLOUD_PROJECT=winefornia-tastingroom-499611`, `GOOGLE_CLOUD_LOCATION=us-east5`, `GOOGLE_GENAI_USE_VERTEXAI=TRUE`). Choose this if data residency matters.

## Status / what's next
- [x] Vertex AI / Agent Platform API enabled on `winefornia-tastingroom-499611`.
- [x] Goal-oriented ADK agent (this package), Claude-powered, HITL preserved.
- [x] 3-party coordination + two case types + party priority; case type detected from the form.
- [x] Email intake rebuilt without LangGraph (`intake.py`); watcher routes here.
- [x] **Legacy LangGraph removed** — `case_desk_graph`, `case_judge`, `case_memory`,
      `safety_guards`, the 23-state machine, and graph-only scripts are deleted.
      The agent is now the SOLE tasting-room engine.
- [x] `google-adk` + `litellm` added to `requirements.txt`.
- [ ] **Live end-to-end run** against a real inbound email (writes to Supabase) before deploy.
- [ ] First non-dry-run approval card test.
- [ ] (Optional) deploy to Vertex Agent Engine for a managed runtime vs. the current Fly runtime.
