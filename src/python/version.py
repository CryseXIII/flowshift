"""FlowShift version + git revision info.

Used for the Live-Test version-equality gate and the status snapshot. All git
calls run **without a visible console window** on Windows (CREATE_NO_WINDOW) and
degrade to ``"unknown"`` when git is unavailable.
"""
from __future__ import annotations

from functools import total_ordering
import os
from pathlib import Path
import re
import subprocess
import sys

import platform_capabilities as _caps

_BASE = os.path.dirname(os.path.abspath(__file__))

_SEMVER_PATTERN = re.compile(
    r"^(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)"
    r"(?:-((?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


@total_ordering
class SemVer:
    """Strict Semantic Versioning 2.0 value with precedence comparisons."""

    __slots__ = ("major", "minor", "patch", "prerelease", "build")

    def __init__(self, major, minor, patch, prerelease=(), build=()):
        self.major = int(major)
        self.minor = int(minor)
        self.patch = int(patch)
        self.prerelease = tuple(prerelease)
        self.build = tuple(build)

    @classmethod
    def parse(cls, value):
        if not isinstance(value, str):
            raise ValueError("semantic version must be a string")
        match = _SEMVER_PATTERN.fullmatch(value)
        if not match:
            raise ValueError(f"invalid semantic version: {value!r}")
        prerelease = tuple(match.group(4).split(".")) if match.group(4) else ()
        build = tuple(match.group(5).split(".")) if match.group(5) else ()
        return cls(match.group(1), match.group(2), match.group(3), prerelease, build)

    @property
    def is_stable(self):
        return not self.prerelease

    def _precedence(self):
        return self.major, self.minor, self.patch

    def __eq__(self, other):
        if not isinstance(other, SemVer):
            return NotImplemented
        return self._precedence() == other._precedence() and self.prerelease == other.prerelease

    def __lt__(self, other):
        if not isinstance(other, SemVer):
            return NotImplemented
        if self._precedence() != other._precedence():
            return self._precedence() < other._precedence()
        if not self.prerelease:
            return bool(other.prerelease)
        if not other.prerelease:
            return True
        for left, right in zip(self.prerelease, other.prerelease):
            if left == right:
                continue
            left_numeric = left.isdigit()
            right_numeric = right.isdigit()
            if left_numeric and right_numeric:
                return int(left) < int(right)
            if left_numeric != right_numeric:
                return left_numeric
            return left < right
        return len(self.prerelease) < len(other.prerelease)

    def __hash__(self):
        return hash((self._precedence(), self.prerelease))

    def __str__(self):
        value = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            value += "-" + ".".join(self.prerelease)
        if self.build:
            value += "+" + ".".join(self.build)
        return value

    def __repr__(self):
        return f"SemVer({str(self)!r})"


def parse_semver(value):
    return SemVer.parse(value)


def parse_release_tag(tag):
    """Parse the required release-tag shape ``v<SemVer>``."""
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise ValueError("release tag must start with v")
    return parse_semver(tag[1:])


def is_stable_version(value):
    try:
        parsed = value if isinstance(value, SemVer) else parse_semver(value)
    except ValueError:
        return False
    return parsed.is_stable


def stable_versions(values):
    """Return valid stable version strings/objects, preserving input order."""
    return [value for value in values if is_stable_version(value)]


def stable_release_tags(tags):
    stable = []
    for tag in tags:
        try:
            if parse_release_tag(tag).is_stable:
                stable.append(tag)
        except ValueError:
            pass
    return stable


def load_product_version(root=None, version_path=None):
    """Read VERSION from a repository/installation root, or report unknown."""
    if version_path is not None:
        path = Path(version_path)
    else:
        product_root = Path(root) if root is not None else Path(__file__).resolve().parents[2]
        path = product_root / "VERSION"
    try:
        value = path.read_text(encoding="utf-8").strip()
        parse_semver(value)
        return value
    except (OSError, UnicodeError, ValueError):
        return "unknown"


APP_VERSION = load_product_version()

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
