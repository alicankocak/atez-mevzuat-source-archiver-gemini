from __future__ import annotations

import fcntl
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


DEFAULT_DATE_LEASE_DIR = (
    Path(tempfile.gettempdir()) / "atez-mevzuat-source-archiver-gemini-locks"
)


@contextmanager
def date_archive_lease(
    report_date: str,
    *,
    lock_dir: Path | str | None = None,
) -> Iterator[bool]:
    """Try to hold the process-wide mutation lease for one archive date."""
    lease_dir = Path(lock_dir) if lock_dir is not None else DEFAULT_DATE_LEASE_DIR
    lease_dir.mkdir(parents=True, exist_ok=True)
    lock_file = (lease_dir / f"{report_date}.lock").open("a+")
    acquired = False
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            pass
        yield acquired
    finally:
        if acquired:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()
