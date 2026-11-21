"""Install lock shared with install_flowshift.ps1, update_flowshift.ps1 and the uninstaller.

The PowerShell scripts hold ``ProgramData\\FlowShift\\updates\\install.lock``
open for their whole run and share it for reading only. Windows then refuses
to open it for writing anywhere else, which is what :func:`is_locked` checks;
reading the owner record stays possible. Windows closes the handle when the
holding process ends, so a crashed run never leaves a lock behind.

``manual_install.json`` is written by a manual install_flowshift.ps1 run right
before it starts FlowShift. The runtime that starts afterwards consumes it and
waits for its next regular update check instead of updating at once.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path


LOCK_NAME = "install.lock"
MANUAL_INSTALL_NAME = "manual_install.json"
MAX_RECORD_BYTES = 4096


def lock_path(data_dir):
    return Path(data_dir) / "updates" / LOCK_NAME


def manual_install_path(data_dir):
    return Path(data_dir) / "updates" / MANUAL_INSTALL_NAME


def is_locked(data_dir, opener=open):
    """True while another process holds the install lock.

    A missing file or one nobody holds means "free". Only a sharing violation
    (PermissionError on Windows) counts as held; other errors do not block
    updates forever.
    """
    path = lock_path(data_dir)
    try:
        if not path.is_file():
            return False
        with opener(path, "r+b"):
            return False
    except PermissionError:
        return True
    except OSError:
        return False


def _read_record(path):
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_RECORD_BYTES + 1)
    except OSError:
        return None
    if not raw or len(raw) > MAX_RECORD_BYTES:
        return None
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def read_owner(data_dir):
    """Owner record of the lock (owner, version, pid, since) or ``None``."""
    return _read_record(lock_path(data_dir))


def describe_owner(owner):
    if not isinstance(owner, dict):
        return "unknown owner"
    kind = {"install": "manual installation", "update": "automatic update",
            "uninstall": "uninstallation"}.get(owner.get("owner"), "installation")
    version = owner.get("version")
    since = owner.get("since")
    text = kind
    if isinstance(version, str) and version:
        text += f" of {version}"
    if isinstance(since, str) and since:
        text += f" since {since}"
    return text


def _parse_time(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None


def consume_manual_install(data_dir, current_version, now=None):
    """Read and delete ``manual_install.json``.

    Returns the record when it names ``current_version`` (this runtime was
    just installed by hand), otherwise ``None``. The file is removed in both
    cases so it only ever affects the first start after the installation.
    """
    path = manual_install_path(data_dir)
    if not path.is_file():
        return None
    record = _read_record(path)
    try:
        os.remove(path)
    except OSError:
        pass
    if not isinstance(record, dict) or record.get("version") != current_version:
        return None
    finished = _parse_time(record.get("finished_at"))
    if finished is None:
        return None
    now = now or datetime.now(timezone.utc)
    # A marker from a day ago belongs to an installation whose runtime never
    # started; it no longer says anything about this start.
    if (now - finished).total_seconds() > 24 * 3600:
        return None
    return record
