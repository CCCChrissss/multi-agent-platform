"""Cross-platform temporary paths for uploaded audio.

Windows does not allow a second process or file handle to reopen a
``NamedTemporaryFile`` while its original handle is still open. Breeze ASR
reopens the uploaded path, so the service must close the temporary handle
before inference and delete the path afterwards.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def temporary_audio_path(suffix: str = "") -> Iterator[Path]:
    """Yield a closed temporary path and remove it when the caller finishes."""

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temporary_file:
        path = Path(temporary_file.name)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)
