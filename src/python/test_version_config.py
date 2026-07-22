"""Custom-script tests for the Phase 1.5 version/config foundation.

Run: ``python src/python/test_version_config.py``
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import importlib.util

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config_schema as schema
import version

_bump_spec = importlib.util.spec_from_file_location(
    "bump_dev_version", Path(__file__).resolve().parents[2] / "scripts" / "bump_dev_version.py")
bump_dev_version = importlib.util.module_from_spec(_bump_spec)
_bump_spec.loader.exec_module(bump_dev_version)


_failures = []
_checks = 0


def check(condition, label):
    global _checks
    _checks += 1
    if condition:
        print(f"[PASS] {label}")
    else:
        print(f"[FAIL] {label}")
        _failures.append(label)


def rejects(callback, label):
    try:
        callback()
    except ValueError:
        check(True, label)
    except Exception as exc:
        check(False, f"{label} ({type(exc).__name__})")
    else:
        check(False, label)


# Strict SemVer parsing and precedence.
check(str(version.parse_semver("0.4.0")) == "0.4.0", "numeric SemVer parses")
check(version.parse_semver("0.3.0") < version.parse_semver("0.4.0"), "0.3.0 precedes 0.4.0")
check(version.parse_semver("0.4.0") == version.parse_semver("0.4.0"), "same versions compare equal")
check(version.parse_semver("0.4.1") > version.parse_semver("0.4.0"), "0.4.1 follows 0.4.0")
check(version.parse_semver("1.0.0") > version.parse_semver("0.99.99"), "1.0.0 follows 0.99.99")
check(version.parse_semver("1.9.0") < version.parse_semver("2.0.0"), "major precedence")
check(version.parse_semver("1.2.9") < version.parse_semver("1.3.0"), "minor precedence")
check(version.parse_semver("1.2.3") < version.parse_semver("1.2.4"), "patch precedence")
check(version.parse_semver("0.5.0-beta.1") < version.parse_semver("0.5.0"), "prerelease precedes stable")
precedence = [
    "1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta",
    "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0",
]
check([str(item) for item in sorted(map(version.parse_semver, reversed(precedence)))] == precedence,
      "SemVer prerelease precedence sequence")
check(version.parse_semver("1.0.0+build.1") == version.parse_semver("1.0.0+build.2"),
      "build metadata does not affect precedence")
check(str(version.parse_release_tag("v0.5.0-beta.1")) == "0.5.0-beta.1", "release tag accepts leading v")
check(str(version.parse_semver("0.5.0-dev.1")) == "0.5.0-dev.1", "development SemVer parses")
check(bump_dev_version.next_dev_version("0.4.0", "0.5.0") == "0.5.0-dev.1",
      "first development version follows prior stable")
check(bump_dev_version.next_dev_version("0.5.0-dev.1", "0.5.0") == "0.5.0-dev.2",
      "development counter increments exactly once")
rejects(lambda: bump_dev_version.validate_version("0.5.0-dev.1", "v0.5.0"),
        "development VERSION cannot satisfy stable release tag")
check(str(bump_dev_version.validate_version("0.5.0", "v0.5.0")) == "0.5.0",
      "stable release tag exactly matches VERSION")
for invalid in ("v1.2.3", "1.2", "1.2.3.4", "01.2.3", "1.02.3", "1.2.03",
                "1.2.3-01", "1.2.3-", "1.2.3+", " 1.2.3", "1.2.3 "):
    rejects(lambda value=invalid: version.parse_semver(value), f"invalid SemVer rejected: {invalid!r}")
for invalid_tag in ("1.2.3", "vv1.2.3", "release-1.2.3", "v1.2"):
    rejects(lambda value=invalid_tag: version.parse_release_tag(value),
            f"invalid release tag rejected: {invalid_tag!r}")
check(version.stable_versions(["0.5.0-beta.1", "0.5.0", "bad"]) == ["0.5.0"],
      "stable version filtering excludes prerelease and invalid values")
check(version.stable_release_tags(["v0.5.0-beta.1", "v0.5.0", "bad"]) == ["v0.5.0"],
      "stable release tag filtering excludes prerelease")
check(version.load_product_version() == "0.5.0-dev.1", "product version loads from repository root")
check(version.load_product_version(version_path=Path("does-not-exist")) == "unknown",
      "missing VERSION reports unknown through helper override")


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)

    (root / "VERSION").write_text("2.3.4\n", encoding="utf-8")
    check(version.load_product_version(root=root) == "2.3.4",
          "installed-layout root VERSION loads")
    (root / "VERSION").write_text("not-a-version\n", encoding="utf-8")
    check(version.load_product_version(root=root) == "unknown",
          "invalid installed VERSION reports unknown")

    old_path = root / "config.json"
    old = {
        "device_id": "abcd1234",
        "peers": [{"name": "Peer", "host": "192.0.2.1"}],
        "display_layout": {"enabled": False, "future_layout": 7},
        "future_root": {"preserve": True},
    }
    old_path.write_text(json.dumps(old), encoding="utf-8")
    migrated = schema.load_config(old_path)
    check(migrated["config_schema_version"] == 1, "old config migrates to schema 1")
    check(migrated["updates"] == schema.DEFAULT_UPDATES, "missing updates receive defaults")
    check(migrated["device_id"] == old["device_id"], "migration preserves device_id")
    check(migrated["peers"] == old["peers"], "migration preserves peers")
    check(migrated["display_layout"] == old["display_layout"], "migration preserves display layout")
    check(migrated["future_root"] == old["future_root"], "migration preserves unknown root keys")
    migration_backup = root / "config.backup-schema-0-to-1.json"
    check(migration_backup.exists() and json.loads(migration_backup.read_text(encoding="utf-8")) == old,
          "schema migration creates the required pre-migration backup")
    before = old_path.read_bytes()
    loaded_again = schema.load_config(old_path)
    check(loaded_again == migrated and old_path.read_bytes() == before, "migration is idempotent")

    values_path = root / "values.json"
    values = {
        "config_schema_version": 1,
        "updates": {
            "enabled": "false", "check_on_start": 0, "channel": "stable",
            "policy": "download", "future_update_key": "kept",
        },
        "unknown": 42,
    }
    values_path.write_text(json.dumps(values), encoding="utf-8")
    normalized = schema.load_config(values_path)
    check(normalized["updates"]["enabled"] is False, "updates enabled boolean normalizes")
    check(normalized["updates"]["check_on_start"] is False, "updates check_on_start boolean normalizes")
    check(normalized["updates"]["channel"] == "stable", "existing stable channel is retained")
    check(normalized["updates"]["policy"] == "download", "existing allowed policy is retained")
    check(normalized["updates"]["future_update_key"] == "kept", "unknown update keys are retained")
    check(normalized["unknown"] == 42, "unknown current-schema keys are retained")

    invalid = schema.migrate_config({
        "config_schema_version": 1,
        "updates": {"enabled": "maybe", "check_on_start": [], "channel": "prerelease", "policy": "force"},
    })
    check(invalid["updates"] == schema.DEFAULT_UPDATES, "invalid update values fall back to strict defaults")

    corrupt_path = root / "corrupt.json"
    corrupt_bytes = b'{"broken":'
    corrupt_path.write_bytes(corrupt_bytes)
    recovered = schema.load_config(corrupt_path, {"device_id": "fresh123"})
    corrupt_backups = list(root.glob("corrupt.backup-corrupt-*.json"))
    check(recovered["config_schema_version"] == 1 and recovered["device_id"] == "fresh123",
          "corrupt config recovers without crashing")
    check(len(corrupt_backups) == 1 and corrupt_backups[0].read_bytes() == corrupt_bytes,
          "corrupt config is preserved in a clearly named backup")
    check(json.loads(corrupt_path.read_text(encoding="utf-8")) == recovered,
          "recovered config is valid JSON on disk")

    atomic_path = root / "atomic.json"
    replacements = []
    real_replace = schema.os.replace

    def recording_replace(source, destination):
        replacements.append((Path(source), Path(destination)))
        return real_replace(source, destination)

    schema.os.replace = recording_replace
    try:
        saved = schema.save_config(atomic_path, {"device_id": "atomic12", "future": "kept"})
    finally:
        schema.os.replace = real_replace
    check(any(destination == atomic_path and source.suffix == ".tmp" for source, destination in replacements),
          "config save uses temp file plus os.replace")
    check(json.loads(atomic_path.read_text(encoding="utf-8")) == saved, "atomic save persists normalized config")
    check(saved["future"] == "kept" and saved["updates"] == schema.DEFAULT_UPDATES,
          "atomic save preserves unknown keys and normalizes schema/updates")
    check(not list(root.glob(".atomic.json.*.tmp")), "atomic save leaves no temporary file")


if _failures:
    print(f"\n{len(_failures)} of {_checks} checks failed")
    for failure in _failures:
        print(f" - {failure}")
    raise SystemExit(1)

print(f"\nAll {_checks} version/config checks passed.")
