"""Read-only local prerequisites check; never installs, starts, or calls a service."""
from __future__ import annotations

from importlib import metadata
import os
from pathlib import Path
import re
import shutil
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    failures = 0

    def check(ok: bool, message: str) -> None:
        nonlocal failures
        print(f"[{'OK' if ok else 'MISSING'}] {message}")
        failures += not ok

    check(sys.version_info[:2] == (3, 11), f"Python baseline 3.11 (running {sys.version.split()[0]})")
    check((ROOT / '.venv').is_dir(), "repository .venv; create with uv sync --locked")
    for tool in ("git", "uv", "ollama"):
        check(shutil.which(tool) is not None, f"{tool} on PATH")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = project["project"]["dependencies"] + project["dependency-groups"]["dev"]
    missing = []
    for requirement in requirements:
        name = re.split(r"[\[<>=!~; ]", requirement, maxsplit=1)[0]
        try:
            metadata.version(name)
        except metadata.PackageNotFoundError:
            missing.append(name)
    check(not missing, "installed distributions" + (": " + ", ".join(missing) + " missing" if missing else " present (versions not validated)"))
    env_path = ROOT / ".env"
    check(env_path.is_file(), ".env exists; copy .env.example if missing")
    if env_path.is_file():
        try:
            from dotenv import dotenv_values
            values = {**os.environ, **{k: v for k, v in dotenv_values(env_path, encoding="utf-8").items() if v is not None}}
        except ImportError:
            check(False, "python-dotenv required to inspect configuration; run uv sync --locked")
        except (OSError, UnicodeError):
            check(False, "cannot read .env as UTF-8")
        else:
            database = values.get("PERSISTENCE_DATABASE_URL", "")
            check(database.startswith(("postgresql://", "postgres://")) and "YOUR_PASSWORD" not in database,
                  "database URL configured (value hidden; connection NOT tested)")
            for key in ("HF_HUB_CACHE", "UV_CACHE_DIR", "OLLAMA_MODELS"):
                if values.get(key):
                    check(Path(os.path.expandvars(values[key])).expanduser().is_absolute(), f"{key} override is absolute")
            workflow = values.get("WORKFLOW_DEF_PATH")
            check(not workflow or (ROOT / workflow).is_file(), "WORKFLOW_DEF_PATH unset or file exists")
            print("[INFO] Local workflows and distillation default to local-qwen3; cloud API keys are optional")
    print("[INFO] No service/model/DB connection tested. Follow docs/windows-setup.md for readiness and end-to-end checks.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
