"""Dependency-free repository compatibility checks.

The script is intentionally standard-library only so it can run before
``uv sync`` and in the Windows CI job. It checks syntax and repository
contracts; it does not replace dependency-backed smoke or integration tests.

Run from any directory:
    python scripts/static_compat_check.py
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from urllib.parse import unquote


REPO_ROOT = Path(__file__).resolve().parents[1]
BINARY_SUFFIXES = {
    ".aac",
    ".aif",
    ".aiff",
    ".flac",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".m4a",
    ".mp3",
    ".mp4",
    ".ogg",
    ".pdf",
    ".png",
    ".webp",
    ".wav",
}
AUDIO_SUFFIXES = {".aac", ".aif", ".aiff", ".flac", ".m4a", ".mp3", ".ogg", ".wav"}
MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]\n]*\]\(([^)\n]+)\)")
FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
MODEL_ALIAS_RE = re.compile(r"^\s*-\s+model_name:\s*([^\s#]+)", re.MULTILINE)
WORKFLOW_MODEL_RE = re.compile(r"^\s+model:\s*([^\s#]+)", re.MULTILINE)
URI_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def repository_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=REPO_ROOT,
        check=True,
        stdout=subprocess.PIPE,
    )
    return [REPO_ROOT / raw.decode("utf-8") for raw in result.stdout.split(b"\0") if raw]


def display(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def read_utf8(path: Path, errors: list[str]) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        errors.append(f"{display(path)} is not UTF-8: {exc}")
    except OSError as exc:
        errors.append(f"cannot read {display(path)}: {exc}")
    return None


def local_markdown_target(raw_target: str) -> str | None:
    target = raw_target.strip()
    if target.startswith("<"):
        closing = target.find(">")
        if closing == -1:
            return target
        target = target[1:closing]
    else:
        target = target.split(maxsplit=1)[0]

    if not target or target.startswith("#") or URI_SCHEME_RE.match(target):
        return None
    return unquote(target.split("#", 1)[0].split("?", 1)[0])


def check_markdown_links(path: Path, text: str, errors: list[str]) -> int:
    checked = 0
    without_code_blocks = FENCED_CODE_RE.sub(lambda match: "\n" * match.group(0).count("\n"), text)
    for match in MARKDOWN_LINK_RE.finditer(without_code_blocks):
        target = local_markdown_target(match.group(1))
        if target is None:
            continue
        checked += 1
        candidate = (REPO_ROOT / target.lstrip("/")) if target.startswith("/") else (path.parent / target)
        if not candidate.exists():
            line = without_code_blocks.count("\n", 0, match.start()) + 1
            errors.append(f"{display(path)}:{line} has missing local link: {target}")
    return checked


def check_audio_header(path: Path, errors: list[str]) -> None:
    header = path.read_bytes()[:16]
    suffix = path.suffix.lower()
    valid = True
    if suffix in {".aif", ".aiff"}:
        valid = len(header) >= 12 and header[:4] == b"FORM" and header[8:12] in {b"AIFF", b"AIFC"}
    elif suffix == ".wav":
        valid = len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WAVE"
    elif suffix == ".flac":
        valid = header.startswith(b"fLaC")
    elif suffix == ".ogg":
        valid = header.startswith(b"OggS")
    elif suffix == ".mp3":
        valid = header.startswith(b"ID3") or (len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0)
    elif suffix in {".m4a", ".aac"}:
        valid = b"ftyp" in header or (len(header) >= 2 and header[0] == 0xFF and header[1] & 0xF0 == 0xF0)
    if not valid:
        errors.append(f"{display(path)} does not have a valid {suffix} header")


def main() -> int:
    errors: list[str] = []
    files = repository_files()
    texts: dict[Path, str] = {}

    for path in files:
        if not path.exists():
            errors.append(f"tracked file is missing: {display(path)}")
            continue
        if path.suffix.lower() not in BINARY_SUFFIXES:
            text = read_utf8(path, errors)
            if text is not None:
                texts[path] = text

    python_count = 0
    for path, source in texts.items():
        if path.suffix.lower() != ".py":
            continue
        python_count += 1
        try:
            ast.parse(source, filename=display(path))
        except SyntaxError as exc:
            errors.append(f"{display(path)}:{exc.lineno} Python syntax error: {exc.msg}")

    toml_count = 0
    for name in ("pyproject.toml", "uv.lock"):
        path = REPO_ROOT / name
        source = texts.get(path)
        if source is None:
            continue
        toml_count += 1
        try:
            tomllib.loads(source)
        except tomllib.TOMLDecodeError as exc:
            errors.append(f"{name} TOML parse error: {exc}")

    json_count = 0
    for path, source in texts.items():
        if path.suffix.lower() != ".json":
            continue
        json_count += 1
        try:
            json.loads(source)
        except json.JSONDecodeError as exc:
            errors.append(f"{display(path)}:{exc.lineno} JSON parse error: {exc.msg}")

    markdown_link_count = 0
    for path, source in texts.items():
        if path.suffix.lower() == ".md":
            markdown_link_count += check_markdown_links(path, source, errors)

    gateway_config = texts.get(REPO_ROOT / "gateway" / "config.yaml", "")
    aliases = {value.strip("\"'") for value in MODEL_ALIAS_RE.findall(gateway_config)}
    if not aliases:
        errors.append("gateway/config.yaml declares no model aliases")

    workflow_model_count = 0
    for path, source in texts.items():
        if path.parent != REPO_ROOT / "workflows" / "definitions" or path.suffix.lower() != ".yaml":
            continue
        for value in WORKFLOW_MODEL_RE.findall(source):
            workflow_model_count += 1
            alias = value.strip("\"'")
            if alias not in aliases:
                errors.append(f"{display(path)} references unknown model alias: {alias}")

    audio_count = 0
    for path in files:
        if path.suffix.lower() in AUDIO_SUFFIXES and path.exists():
            audio_count += 1
            check_audio_header(path, errors)

    required = [REPO_ROOT / "AGENTS.md", REPO_ROOT / ".python-version"]
    for path in required:
        if not path.exists():
            errors.append(f"required compatibility file is missing: {display(path)}")

    if errors:
        print("[FAIL] static compatibility checks found problems:")
        for error in errors:
            print(f"  - {error}")
        return 1

    print(f"[OK] {len(texts)} repository text files decode as UTF-8")
    print(f"[OK] {python_count} Python files parse with ast")
    print(f"[OK] {toml_count} TOML and {json_count} JSON files parse")
    print(f"[OK] {markdown_link_count} local Markdown links resolve")
    print(f"[OK] {workflow_model_count} workflow model references resolve to {len(aliases)} gateway aliases")
    print(f"[OK] {audio_count} repository audio headers are valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
