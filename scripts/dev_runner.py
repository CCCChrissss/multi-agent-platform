"""Windows foreground supervisor. Only its Windows Job owns/stops children.

Use scripts/dev.ps1; this module's --child is an internal launch barrier:
the child reads a command only AFTER it has been assigned to its parent's Job.
This prevents grandchildren escaping between Popen and Job assignment.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid

from scripts.windows_job import WindowsJob, process_birth

ROOT = Path(__file__).resolve().parents[1]
GROUPS = ("ollama", "services", "workers", "ui")


def read_state(root: Path, group: str) -> dict | None:
    path = root / ".run" / f"{group}.json"
    if not path.exists():
        return None
    state = json.loads(path.read_text(encoding="utf-8"))
    if state["root"] != str(root.resolve()) or state["group"] != group:
        raise RuntimeError(f"Invalid owner record: {path}")
    uuid.UUID(hex=state["token"])
    return state


def owner_alive(state: dict) -> bool:
    return process_birth(state["pid"]) == state["birth"]


def stop_path(root: Path, state: dict) -> Path:
    return root / ".run" / f"{state['group']}.{state['token']}.stop"


def request_stop(root: Path, group: str, *, preview: bool = False) -> bool:
    state = read_state(root, group)
    if state is None:
        print(f"{group}: no managed process")
        return False
    if not owner_alive(state):
        print(f"{group}: stale record; no process will be stopped")
        return False
    print(f"{group}: {'would request stop' if preview else 'requesting stop'} (owner PID {state['pid']})")
    if not preview:
        stop_path(root, state).touch()
    return True


def supervise(command: list[str], root: Path, group: str, workflow: str = "") -> int:
    root = root.resolve()
    directory = root / ".run"
    directory.mkdir(exist_ok=True)
    # The launcher already parsed .env with python-dotenv and exported the
    # resulting values. Honcho's own POSIX-style parser treats backslashes in
    # Windows paths as escapes, so point Honcho at an intentionally empty file
    # instead of parsing the user's .env a second time.
    (directory / "managed.empty.env").touch()
    record = directory / f"{group}.json"
    old = read_state(root, group)
    if old:
        if owner_alive(old):
            raise RuntimeError(f"{group} already running; stop it before restarting")
        record.unlink()
        stop_path(root, old).unlink(missing_ok=True)
    state = dict(root=str(root), group=group, pid=os.getpid(), birth=process_birth(os.getpid()),
                 token=uuid.uuid4().hex, workflow=workflow)
    # Exclusive creation refuses concurrent supervisors for the same group.
    with record.open("x", encoding="utf-8") as stream:
        json.dump(state, stream)
    job = None
    child = None
    try:
        job = WindowsJob()
        child = subprocess.Popen(
            [sys.executable, "-B", "-m", "scripts.dev_runner", "--child"],
            cwd=ROOT, stdin=subprocess.PIPE, text=True, encoding="utf-8",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        job.assign(child.pid)
        child.stdin.write(json.dumps({"command": command, "cwd": str(root)}) + "\n")
        child.stdin.close()
        print(f"[dev] {group}: managed in {root}; Ctrl+C or dev.ps1 stop to stop", flush=True)
        while child.poll() is None and not stop_path(root, state).exists():
            time.sleep(0.2)
        return child.returncode if child.returncode is not None else 0
    except KeyboardInterrupt:
        return 0
    finally:
        if child and child.poll() is None:
            try:
                child.send_signal(signal.CTRL_BREAK_EVENT)
                child.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired, KeyboardInterrupt):
                pass
        if job:
            job.close()  # all assigned descendants, even if their parent exited
        if child and child.poll() is None:
            child.kill()  # covers failure BEFORE job.assign; child cannot spawn yet
            child.wait(timeout=5)
        record.unlink(missing_ok=True)
        stop_path(root, state).unlink(missing_ok=True)


def require_free(ports: list[int]) -> None:
    for port in ports:
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                raise RuntimeError(f"Port {port} unavailable. Stop its known owner or use another machine; no process was killed.") from None


def command_for(group: str) -> list[str]:
    python = str(ROOT / ".venv" / "Scripts" / "python.exe")
    if group == "ollama":
        ollama = shutil.which("ollama")
        if not ollama:
            raise RuntimeError("ollama is not on PATH; install it and reopen the terminal")
        return [ollama, "serve"]
    if group == "ui":
        return [python, "-m", "uvicorn", "demo.api:app", "--host", "127.0.0.1", "--port", "8010"]
    procfile = "Procfile.workers" if group == "workers" else "Procfile"
    command = [python, "-m", "honcho", "start", "-f", procfile, "-e", ".run/managed.empty.env"]
    if group == "services":
        command += ["litellm", "stt", "notified", "agents"]  # shared Ollama is never implicitly owned
    return command


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=(*GROUPS, "stop", "status", "trigger"))
    parser.add_argument("--workflow", default="stt_check_notify", choices=("stt_check_notify", "stt_exclusion_notify"))
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        request = json.loads(sys.stdin.readline())
        try:
            return subprocess.call(request["command"], cwd=request["cwd"])
        except KeyboardInterrupt:
            return 130
    if os.name != "nt":
        parser.error("this launcher currently supports Windows only")
    if args.action in ("stop", "status"):
        stopping = []
        for group in reversed(GROUPS):
            if args.action == "stop":
                if request_stop(ROOT, group, preview=args.preview) and not args.preview:
                    stopping.append(read_state(ROOT, group))
            else:
                state = read_state(ROOT, group)
                print(f"{group}: {'running' if state and owner_alive(state) else 'stopped'}" +
                      (f" workflow={state['workflow']}" if state else ""))
        deadline = time.monotonic() + 12
        while any(state and owner_alive(state) for state in stopping):
            if time.monotonic() >= deadline:
                raise RuntimeError("Stop requested but owner still running; inspect its terminal before retrying")
            time.sleep(0.2)
        return 0
    if not args.action:
        parser.error("action is required")
    # dotenv is an existing project dependency, used only when starting services.
    from dotenv import dotenv_values
    env_path = ROOT / ".env"
    if not env_path.exists():
        raise RuntimeError("Missing .env; copy .env.example and configure your own database first")
    values = dotenv_values(env_path, encoding="utf-8")
    workflow = f"workflows/definitions/{args.workflow}.yaml"
    declared = values.get("WORKFLOW_DEF_PATH")
    if declared and (ROOT / declared).resolve() != (ROOT / workflow).resolve():
        raise RuntimeError(".env WORKFLOW_DEF_PATH conflicts with -Workflow; comment it out or make it match")
    for key, value in values.items():
        if value is not None:
            os.environ[key] = value
    os.environ.update(PYTHONUTF8="1", UV_NO_SYNC="true", WORKFLOW_DEF_PATH=workflow)
    for group in ("services", "workers", "ui"):
        state = read_state(ROOT, group)
        if args.action != "ollama" and state and owner_alive(state) and state["workflow"] != workflow:
            raise RuntimeError("Another managed group uses a different workflow. Stop all groups before switching.")
    if args.action == "trigger":
        payload = "tsmc" if args.workflow == "stt_check_notify" else "exclusion"
        return subprocess.call([sys.executable, "-B", "-m", "orchestrator.trigger", "--workflow-def", workflow,
                                "--payload-file", f"samples/payloads/{payload}.json"], cwd=ROOT)
    require_free({"ollama": [11434], "services": [4000, 8001, 8002, 8003], "ui": [8010], "workers": []}[args.action])
    os.chdir(ROOT)
    return supervise(command_for(args.action), ROOT, args.action, workflow)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"[dev] {exc}", file=sys.stderr)
        raise SystemExit(1)
