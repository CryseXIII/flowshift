"""Validate or increment FlowShift's central development VERSION file.

Without --target the next free development version is derived from VERSION and
the stable release tags vX.Y.Z of the repository: once vX.Y.Z is published, a
main still at X.Y.Z-dev.N goes on with X.Y.(Z+1)-dev.1, otherwise dev.N + 1.
Build runs call it exactly once per task (AGENTS.md 7); --check fails while
VERSION is a development version of an already published release.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "python"))

from version import parse_release_tag, parse_semver, stable_release_tags  # noqa: E402


def next_dev_version(current, target):
    current_version = parse_semver(current)
    target_version = parse_semver(target)
    if not target_version.is_stable:
        raise ValueError("target must be a stable SemVer")
    target_core = (target_version.major, target_version.minor, target_version.patch)
    current_core = (current_version.major, current_version.minor, current_version.patch)
    if current_version.is_stable and current_version < target_version:
        return f"{target}-dev.1"
    if current_core != target_core or len(current_version.prerelease) != 2 \
            or current_version.prerelease[0] != "dev" \
            or not current_version.prerelease[1].isdigit():
        raise ValueError(f"{current!r} is not a {target}-dev.N version")
    return f"{target}-dev.{int(current_version.prerelease[1]) + 1}"


def _core(parsed):
    return parse_semver(f"{parsed.major}.{parsed.minor}.{parsed.patch}")


def released_versions(repo=ROOT):
    """Stable versions with a vX.Y.Z tag in ``repo`` (empty without git or tags)."""
    try:
        completed = subprocess.run(["git", "tag", "--list", "v*"], cwd=str(repo),
                                   capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0:
        return []
    return [parse_release_tag(tag) for tag in stable_release_tags(completed.stdout.split())]


def next_free_dev_version(current, released):
    """Next development version after ``current``, above every published release."""
    current_version = parse_semver(current)
    core = _core(current_version)
    newest = max(released, default=None)
    if current_version.is_stable or (newest is not None and newest >= core):
        base = max(core, newest) if newest is not None else core
        return f"{base.major}.{base.minor}.{base.patch + 1}-dev.1"
    return next_dev_version(current, str(core))


def unreleased_problem(current, released):
    """Why a development VERSION has to be raised first (its release is out), or None."""
    current_version = parse_semver(current)
    newest = max(released, default=None)
    if current_version.is_stable or newest is None or newest < _core(current_version):
        return None
    return (f"VERSION {current} belongs to the published v{newest} or an older release; "
            f"raise it with 'python3 scripts/bump_dev_version.py' (gives "
            f"{next_free_dev_version(current, released)})")


def validate_version(value, release_tag=None):
    parsed = parse_semver(value)
    if parsed.prerelease:
        if len(parsed.prerelease) != 2 or parsed.prerelease[0] not in {
                "dev", "alpha", "beta", "rc"} or not parsed.prerelease[1].isdigit():
            raise ValueError("unsupported FlowShift prerelease version")
    if release_tag is not None:
        tagged = parse_release_tag(release_tag)
        if not parsed.is_stable or tagged != parsed:
            raise ValueError("stable release tag must exactly match stable VERSION")
    return parsed


def write_atomic(path, value):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--version-file", type=Path, default=ROOT / "VERSION")
    parser.add_argument("--target", help="stable target X.Y.Z; default: derived from VERSION "
                                         "and the release tags")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--release-tag")
    args = parser.parse_args(argv)
    current = args.version_file.read_text(encoding="utf-8").strip()
    if args.check:
        validate_version(current, args.release_tag)
        problem = None if args.release_tag else unreleased_problem(current, released_versions())
        if problem:
            print(problem, file=sys.stderr)
            return 1
        print(current)
        return 0
    if args.target:
        updated = next_dev_version(current, args.target)
    else:
        updated = next_free_dev_version(current, released_versions())
    write_atomic(args.version_file, updated)
    print(updated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
