"""Shared helpers for freeze/verify scripts.

Provides anchor-reproducibility hashing: the ability to compute a runtime hash
from the pinned files AS THEY EXIST at a recorded ``anchor_commit`` (via
``git show``), not just from the working tree. A freeze is only a real anchor if
``Hash(files @ anchor_commit) == runtime_sha256``.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha256_of_files_at_commit(rel_paths: list[str] | tuple[str, ...], commit: str) -> str:
    """Hash the pinned files as they exist at ``commit`` (LF-normalized)."""
    h = hashlib.sha256()
    for rel in sorted(rel_paths):
        try:
            blob = subprocess.check_output(
                ["git", "show", f"{commit}:{rel}"], cwd=ROOT, stderr=subprocess.DEVNULL
            )
        except subprocess.CalledProcessError as exc:
            raise FileNotFoundError(f"missing at {commit}: {rel}") from exc
        h.update(blob.replace(b"\r\n", b"\n"))
    return h.hexdigest()


def verify_anchor(runtime_files: list[str] | tuple[str, ...], anchor_commit: str, runtime_sha256: str) -> bool:
    """Prove the recorded anchor can reproduce the frozen runtime hash.

    Returns True (and prints ok) only when ``Hash(files @ anchor_commit) ==
    runtime_sha256``. A freeze is not a real anchor if checking out the recorded
    commit does not reproduce the claimed bytes.
    """
    try:
        actual = sha256_of_files_at_commit(list(runtime_files), anchor_commit)
    except FileNotFoundError as exc:
        print(f"FAIL anchor_sha256: {exc}")
        return False
    if actual != runtime_sha256:
        print(f"FAIL anchor_sha256: frozen={runtime_sha256}")
        print(f"                    anchor={actual}")
        return False
    print(f"ok   anchor_sha256: {actual}")
    return True