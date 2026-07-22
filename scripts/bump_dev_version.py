"""Validate or increment FlowShift's central development VERSION file."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "python"))

from version import parse_release_tag, parse_semver  # noqa: E402


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
    parser.add_argument("--target", default="0.5.0")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--release-tag")
    args = parser.parse_args(argv)
    current = args.version_file.read_text(encoding="utf-8").strip()
    if args.check:
        validate_version(current, args.release_tag)
        print(current)
        return 0
    updated = next_dev_version(current, args.target)
    write_atomic(args.version_file, updated)
    print(updated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
