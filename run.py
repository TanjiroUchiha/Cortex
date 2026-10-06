#!/usr/bin/env python3
"""Cortex launcher — one command for the whole stack.

Starts the M2 merger service, then the M1 front-door API, waits until the API is
answering, and opens the UI in your browser. Nothing else needs to be running —
Ollama is only required for the LLM/embedding paths (``--llm-*`` / semantic
retrieval); without it M1 falls back to keyword retrieval and the deterministic
merge, so the demo still answers.

    python run.py                        # M2 + M1, UI at http://127.0.0.1:8000/
    python run.py --no-m2                # M1 only (deterministic local merge)
    python run.py --llm-route --llm-merge
    python run.py --frontend-port 5500   # UI on a SEPARATE origin (:5500) — enables CORS

Ctrl+C stops every process this launcher started.

The M2 service is loaded from ``M2/.env`` (read here, so ``python-dotenv`` is not
required) and runs under ``M2/.venv`` when one exists; the API runs under the same
interpreter as this launcher (or ``M1/.venv`` when present).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
IS_WINDOWS = os.name == "nt"
OLLAMA_TAGS = "http://127.0.0.1:11434/api/tags"


def venv_python(pkg_dir: Path) -> str | None:
    """Return the interpreter inside a package's local ``.venv``, if one exists."""
    for rel in ("Scripts/python.exe", "bin/python"):
        candidate = pkg_dir / ".venv" / rel
        if candidate.exists():
            return str(candidate)
    return None


def load_env_file(path: Path) -> dict[str, str]:
    """Minimal ``KEY=VALUE`` ``.env`` reader — avoids a python-dotenv dependency."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def health_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def wait_for_health(url: str, timeout: float, label: str) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if health_ok(url):
            print(f"  {label} ready.")
            return True
        time.sleep(0.4)
    return False


def cortex_already_up(url: str) -> bool:
    """Is a Cortex M1 already listening here? (any 200 isn't enough — must be ours)."""
    try:
        with urllib.request.urlopen(url + "/health", timeout=2) as response:
            body = json.loads(response.read())
        return body.get("status") == "ok" and "mode" in body
    except (urllib.error.URLError, OSError, ValueError):
        return False


def start(cmd: list[str], env: dict[str, str]) -> subprocess.Popen:
    # A new process group keeps Ctrl+C in this console from half-killing the children;
    # we stop them ourselves in stop_all() so shutdown is orderly.
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0
    return subprocess.Popen(cmd, cwd=str(ROOT), env=env, creationflags=flags)


def stop_all(children: list[tuple[subprocess.Popen, str]]) -> None:
    for proc, name in reversed(children):
        if proc.poll() is not None:
            continue
        print(f"  stopping {name}…")
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()


def ollama_status() -> str:
    try:
        with urllib.request.urlopen(OLLAMA_TAGS, timeout=2) as response:
            models = [m.get("name", "") for m in (json.loads(response.read()).get("models") or [])]
        return "up · " + (", ".join(models) if models else "no models pulled")
    except (urllib.error.URLError, OSError, ValueError):
        return "not reachable — keyword retrieval + deterministic merge will be used"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)  # show progress as it happens, even when piped
    parser = argparse.ArgumentParser(description="Launch the Cortex stack (M2 + M1 + UI).",
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--port", type=int, default=8000, help="M1 API port")
    parser.add_argument("--m2-port", type=int, default=9001, help="M2 merger port")
    parser.add_argument("--no-m2", action="store_true", help="skip M2 (M1 uses its local merge)")
    parser.add_argument("--frontend-port", type=int, default=None,
                        help="serve Frontend/ statically on this port as a separate origin "
                             "(enables CORS on the API and opens that origin)")
    parser.add_argument("--retrieval", choices=("semantic", "keyword"), default="semantic",
                        help="M1 retrieval backend; semantic falls back to keyword without the model")
    parser.add_argument("--llm-route", action="store_true", help="use the model as the M1 router")
    parser.add_argument("--llm-merge", action="store_true", help="use the model as the M2 merger")
    parser.add_argument("--llm-verify", action="store_true", help="use the model as the V1 verifier")
    parser.add_argument("--llm-answer", action="store_true", help="let the model write skill prose")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    parser.add_argument("--ready-timeout", type=float, default=120,
                        help="seconds to wait for a service before giving up")
    args = parser.parse_args()

    children: list[tuple[subprocess.Popen, str]] = []
    print("Cortex launcher")
    print(f"  Ollama: {ollama_status()}")

    env = os.environ.copy()
    # Cortex/.env provides shared config (GOOGLE_CLIENT_ID, CORTEX_JWT_SECRET,
    # CORTEX_ADMIN_*) for the M1 process; real env vars still win if both are set.
    for key, value in load_env_file(ROOT / ".env").items():
        env.setdefault(key, value)
    api_url = f"http://127.0.0.1:{args.port}"
    ui_url = api_url
    if args.frontend_port:
        ui_url = f"http://127.0.0.1:{args.frontend_port}"
        # A separately hosted UI needs its origin allowlisted, or authorize() rejects it.
        configured = [o for o in env.get("CORTEX_ALLOWED_ORIGINS", "").split(",") if o.strip()]
        env["CORTEX_ALLOWED_ORIGINS"] = ",".join(dict.fromkeys(configured + [ui_url]))

    m2_url = None
    try:
        if cortex_already_up(api_url):
            print(f"  Cortex is already serving at {api_url} — an earlier launcher is still alive.")
            print(f"  Landing:   {api_url}/")
            print(f"  Assistant: {api_url}/app")
            print("  (stop that process first if you meant to restart with different options)")
            if not args.no_browser:
                webbrowser.open(f"{api_url}/")
            return 0
        if not args.no_m2:
            m2_env = {**os.environ, **load_env_file(ROOT / "M2" / ".env")}
            m2_python = venv_python(ROOT / "M2") or sys.executable
            m2_cmd = [m2_python, "-m", "uvicorn", "M2.api:app", "--host", "127.0.0.1",
                      "--port", str(args.m2_port), "--log-level", "warning"]
            children.append((start(m2_cmd, m2_env), "M2"))
            print(f"  starting M2 on http://127.0.0.1:{args.m2_port} …")
            if wait_for_health(f"http://127.0.0.1:{args.m2_port}/health", min(args.ready_timeout, 40), "M2"):
                m2_url = f"http://127.0.0.1:{args.m2_port}/merge"
            else:
                print("  M2 did not come up — M1 will use its local merge.")

        m1_python = venv_python(ROOT / "M1") or sys.executable
        m1_cmd = [m1_python, str(ROOT / "M1" / "api.py"), "--mode", "demo",
                  "--retrieval", args.retrieval, "--port", str(args.port)]
        for flag in ("llm_route", "llm_merge", "llm_verify", "llm_answer"):
            if getattr(args, flag):
                m1_cmd.append("--" + flag.replace("_", "-"))
        if m2_url:
            m1_cmd += ["--m2-url", m2_url]
        children.append((start(m1_cmd, env), "M1"))
        print(f"  starting M1 on {api_url} …")
        api_up = wait_for_health(f"{api_url}/health", args.ready_timeout, "M1")
        if not api_up:
            print("  M1 did not report healthy in time — check the logs above.")

        if args.frontend_port:
            fe_cmd = [sys.executable, "-m", "http.server", str(args.frontend_port),
                      "--bind", "127.0.0.1", "--directory", str(ROOT / "Frontend")]
            children.append((start(fe_cmd, os.environ.copy()), "frontend"))
            print(f"  serving Frontend/ on {ui_url} (separate origin) …")

        assistant_url = (f"{ui_url}/index.html?api={api_url}" if args.frontend_port else f"{api_url}/app")
        print(f"\n  Landing:   {api_url}/")
        print(f"  Assistant: {assistant_url}")
        print(f"  API docs:  {api_url}/docs")
        print("  Press Ctrl+C to stop.\n")

        if api_up and not args.no_browser:
            webbrowser.open(assistant_url if args.frontend_port else f"{api_url}/")

        while True:
            time.sleep(0.5)
            dead = next((name for proc, name in children if proc.poll() is not None), None)
            if dead:
                print(f"\n  {dead} exited — shutting down the rest.")
                break
    except KeyboardInterrupt:
        print("\n  Ctrl+C received — shutting down.")
    finally:
        stop_all(children)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

