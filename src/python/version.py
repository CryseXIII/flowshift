"""FlowShift version + git revision info.

Used for the Live-Test version-equality gate and the status snapshot. All git
calls run **without a visible console window** on Windows (CREATE_NO_WINDOW) and
degrade to ``"unknown"`` when git is unavailable.
"""
from __future__ import annotations

import os
import subprocess
import sys

import platform_capabilities as _caps

APP_VERSION = "0.3.0"

_BASE = os.path.dirname(os.path.abspath(__file__))

# CREATE_NO_WINDOW on Windows, 0 (no-op) elsewhere. Reused across the codebase
# so subprocesses never flash a CMD window.
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _git(args, timeout=3):
    try:
        r = subprocess.run(
            ["git"] + args, cwd=_BASE, capture_output=True, text=True,
            timeout=timeout, creationflags=CREATE_NO_WINDOW,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        return None
    return None


def git_commit():
    return _git(["rev-parse", "HEAD"]) or "unknown"


def git_short_commit():
    c = git_commit()
    return c[:12] if c and c != "unknown" else "unknown"


def git_branch():
    return _git(["rev-parse", "--abbrev-ref", "HEAD"]) or "unknown"


def git_dirty():
    """True if there are uncommitted changes, False if clean, None if unknown."""
    s = _git(["status", "--porcelain"])
    if s is None:
        return None
    return len(s.strip()) > 0


def git_pushed(branch=None):
    """True if local HEAD == origin/<branch> (i.e. already pushed), else False.

    None if it cannot be determined (no git / no remote).
    """
    branch = branch or git_branch()
    local = git_commit()
    if local == "unknown" or not branch or branch == "unknown":
        return None
    remote = _git(["ls-remote", "origin", branch], timeout=6)
    if remote is None:
        return None
    remote_sha = remote.split()[0] if remote.split() else ""
    if not remote_sha:
        return None
    return local == remote_sha


def version_info():
    """Compact version record for the status snapshot / hello exchange."""
    return {
        "app_version": APP_VERSION,
        "git_commit": git_commit(),
        "git_branch": git_branch(),
        "protocol_version": _caps.PROTOCOL_VERSION,
    }
