"""Dependency-free smoke test for Windows-safe uploaded-audio handling.

Run from the repository root:
    python -m services.stt.temp_audio_smoke_test
"""

from __future__ import annotations

from services.stt.temp_audio import temporary_audio_path


def main() -> None:
    marker = b"RIFF\x00\x00\x00\x00WAVE"

    with temporary_audio_path(".wav") as path:
        path.write_bytes(marker)
        with path.open("rb") as reopened:
            assert reopened.read() == marker
        assert path.exists()

    assert not path.exists()
    print("[OK] temporary audio can be reopened and is removed afterwards")


if __name__ == "__main__":
    main()
