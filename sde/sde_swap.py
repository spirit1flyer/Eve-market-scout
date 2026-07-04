"""
Atomic build-then-swap for SDE database downloads.

Shared by both SDE downloaders (sde_manager's type DB and sde_industry's
industry DB): the replacement database is built at <db>.new and only
replaces the live file once the build fully succeeds. A failed download or
build leaves the previous working database untouched. (Review finding 5-5:
the old flow deleted the live DB before downloading anything, so any
failure — Fuzzwork down, timeout, parse error — left NO SDE at all.)
"""

from contextlib import contextmanager
from pathlib import Path


@contextmanager
def build_then_swap(db_path: Path):
    """Yield a temp path to build the new database at; swap it in on success.

    On success the temp file atomically replaces db_path (Path.replace).
    On any exception the partial temp file is removed, db_path is never
    touched, and the exception propagates to the caller.

    Callers must fully close their connection to the temp file before the
    with-block ends (Windows cannot replace/unlink an open file), and must
    not `return` out of the block early — a normal exit triggers the swap.
    """
    tmp_path = db_path.with_name(db_path.name + ".new")
    # Leftover partial from a previous crashed/killed attempt.
    if tmp_path.exists():
        tmp_path.unlink()
    try:
        yield tmp_path
        if not tmp_path.exists():
            raise RuntimeError(f"build produced no database at {tmp_path}")
        tmp_path.replace(db_path)
    except BaseException:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise
