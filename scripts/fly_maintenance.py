"""Run reviewed maintenance code inside the app; service keys stay on Fly.

Requires FLY_API_TOKEN with access to winefornia-agent. Only the named operations
below are accepted. Copies this checkout's scripts into a temporary directory on
one running web Machine, then removes them. Does not modify the deployed image.
"""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import shlex
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def remote_source(operation: str, full: bool = False) -> str:
    script = "scripts/sync.py" if operation == "sync" else "scripts/database_security.py"
    paths = [script]
    if operation != "sync":
        paths.append("db/migrations/20260914_backend_table_security.sql")
    payload = {p: base64.b64encode((ROOT / p).read_bytes()).decode() for p in paths}
    args = ["--full"] if full and operation == "sync" else []
    if operation == "secure-database":
        args = ["--apply"]
    return f"""import base64, os, pathlib, runpy, sys, tempfile
os.chdir('/app')
sys.path.insert(0, '/app')
with tempfile.TemporaryDirectory(prefix='winefornia-maintenance-') as d:
    for name, content in {payload!r}.items():
        p = pathlib.Path(d) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(base64.b64decode(content))
    sys.argv = [{script!r}] + {args!r}
    runpy.run_path(str(pathlib.Path(d) / {script!r}), run_name='__main__')
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["sync", "audit-security", "secure-database"])
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    proc = subprocess.run(["flyctl", "machines", "list", "--app", "winefornia-agent", "--json"],
                          check=True, capture_output=True, text=True, timeout=60)
    machines = json.loads(proc.stdout)
    eligible = [m for m in machines if m.get("state") == "started"
                and m.get("config", {}).get("metadata", {}).get("fly_process_group") == "web"]
    if not eligible:
        raise SystemExit("No running winefornia-agent web Machine; refusing an ambiguous target")
    machine = sorted(eligible, key=lambda m: m["id"])[0]["id"]
    print(f"Running {args.operation} on web Machine {machine}; service keys remain on Fly", flush=True)
    command = shlex.join(["python", "-c", remote_source(args.operation, args.full)])
    subprocess.run(["flyctl", "ssh", "console", "--app", "winefornia-agent",
                    "--machine", machine, "--command", command], check=True, timeout=1500)


if __name__ == "__main__":
    main()
