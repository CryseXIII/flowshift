"""Versioned FlowShift configuration loading and atomic persistence."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile


CONFIG_SCHEMA_VERSION = 1
UPDATE_CHANNELS = frozenset({"stable"})
UPDATE_POLICIES = frozenset({"notify", "download", "install"})
DEFAULT_UPDATES = {
    "enabled": True,
    "check_on_start": True,
    "channel": "stable",
    "policy": "notify",
}


def normalize_boolean(value, default):
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return bool(default)


def normalize_updates(value):
    updates = copy.deepcopy(value) if isinstance(value, dict) else {}
    updates["enabled"] = normalize_boolean(updates.get("enabled"), True)
    updates["check_on_start"] = normalize_boolean(updates.get("check_on_start"), True)

    channel = updates.get("channel", DEFAULT_UPDATES["channel"])
    channel = channel.strip().lower() if isinstance(channel, str) else ""
    updates["channel"] = channel if channel in UPDATE_CHANNELS else DEFAULT_UPDATES["channel"]

    policy = updates.get("policy", DEFAULT_UPDATES["policy"])
    policy = policy.strip().lower() if isinstance(policy, str) else ""
    updates["policy"] = policy if policy in UPDATE_POLICIES else DEFAULT_UPDATES["policy"]
    return updates


def _schema_version(config):
    value = config.get("config_schema_version", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _migrate_0_to_1(config):
    config["updates"] = normalize_updates(config.get("updates"))
    config["config_schema_version"] = 1
    return config


_MIGRATIONS = {0: _migrate_0_to_1}


def migrate_config(config):
    """Apply schema migrations one version at a time without dropping keys."""
    migrated = copy.deepcopy(config) if isinstance(config, dict) else {}
    version = _schema_version(migrated)
    if version > CONFIG_SCHEMA_VERSION:
        migrated["updates"] = normalize_updates(migrated.get("updates"))
        return migrated
    while version < CONFIG_SCHEMA_VERSION:
        migration = _MIGRATIONS.get(version)
        if migration is None:
            raise ValueError(f"no config migration from schema {version}")
        migrated = migration(migrated)
        version = _schema_version(migrated)
    migrated["updates"] = normalize_updates(migrated.get("updates"))
    return migrated


def schema_backup_path(path, from_version=0, to_version=1):
    path = Path(path)
    return path.with_name(f"{path.stem}.backup-schema-{from_version}-to-{to_version}{path.suffix}")


def _atomic_write_bytes(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _atomic_write_json(path, config):
    payload = (json.dumps(config, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    _atomic_write_bytes(path, payload)


def _fresh_config(default_config=None):
    config = copy.deepcopy(default_config) if isinstance(default_config, dict) else {}
    config["config_schema_version"] = CONFIG_SCHEMA_VERSION
    config["updates"] = normalize_updates(config.get("updates"))
    return config


def _corrupt_backup_path(path):
    path = Path(path)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return path.with_name(f"{path.stem}.backup-corrupt-{stamp}{path.suffix}")


def _preserve_corrupt_file(path):
    path = Path(path)
    backup = _corrupt_backup_path(path)
    try:
        os.replace(path, backup)
        return backup
    except OSError:
        return None


def _decode_config(raw):
    config = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(config, dict):
        raise ValueError("config root must be an object")
    return config


def _backup_before_migration(path, raw, from_version):
    backup = schema_backup_path(path, from_version, from_version + 1)
    if not backup.exists():
        _atomic_write_bytes(backup, raw)


def load_config(path, default_config=None):
    """Load, recover, migrate, normalize, and persist a FlowShift config."""
    path = Path(path)
    if not path.exists():
        config = _fresh_config(default_config)
        _atomic_write_json(path, config)
        return config

    try:
        raw = path.read_bytes()
        original = _decode_config(raw)
    except (json.JSONDecodeError, UnicodeError, ValueError):
        config = _fresh_config(default_config)
        if _preserve_corrupt_file(path) is not None:
            _atomic_write_json(path, config)
        return config
    except OSError:
        return _fresh_config(default_config)

    from_version = _schema_version(original)
    if from_version < CONFIG_SCHEMA_VERSION:
        _backup_before_migration(path, raw, from_version)
    config = migrate_config(original)
    if config != original:
        _atomic_write_json(path, config)
    return config


def save_config(path, config):
    """Normalize and atomically replace a FlowShift config file."""
    path = Path(path)
    if path.exists():
        try:
            raw = path.read_bytes()
            on_disk = _decode_config(raw)
            from_version = _schema_version(on_disk)
            if from_version < CONFIG_SCHEMA_VERSION:
                _backup_before_migration(path, raw, from_version)
        except (json.JSONDecodeError, UnicodeError, ValueError):
            if _preserve_corrupt_file(path) is None:
                return migrate_config(config)
        except OSError:
            pass
    normalized = migrate_config(config)
    _atomic_write_json(path, normalized)
    return normalized
