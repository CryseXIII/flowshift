"""Persistent state and explicit transition rules for FlowShift updates."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import threading
import uuid


SCHEMA_VERSION = 1

IDLE = "idle"
CHECKING = "checking"
UP_TO_DATE = "up_to_date"
UPDATE_AVAILABLE = "update_available"
DOWNLOADING = "downloading"
DOWNLOADED = "downloaded"
WAITING_FOR_IDLE = "waiting_for_idle"
INSTALL_HANDOFF = "install_handoff"
INSTALLING = "installing"
RESTARTING = "restarting"
ERROR = "error"

STATES = frozenset({
    IDLE, CHECKING, UP_TO_DATE, UPDATE_AVAILABLE, DOWNLOADING, DOWNLOADED,
    WAITING_FOR_IDLE, INSTALL_HANDOFF, INSTALLING, RESTARTING, ERROR,
})

TRANSITIONS = {
    IDLE: frozenset({CHECKING, UP_TO_DATE, ERROR}),
    CHECKING: frozenset({UP_TO_DATE, UPDATE_AVAILABLE, ERROR}),
    UP_TO_DATE: frozenset({CHECKING, ERROR}),
    UPDATE_AVAILABLE: frozenset({CHECKING, DOWNLOADING, ERROR}),
    DOWNLOADING: frozenset({DOWNLOADED, ERROR}),
    DOWNLOADED: frozenset({CHECKING, WAITING_FOR_IDLE, INSTALL_HANDOFF, DOWNLOADING, ERROR}),
    WAITING_FOR_IDLE: frozenset({INSTALL_HANDOFF, DOWNLOADED, ERROR}),
    INSTALL_HANDOFF: frozenset({INSTALLING, ERROR}),
    INSTALLING: frozenset({RESTARTING, ERROR}),
    RESTARTING: frozenset({IDLE, ERROR}),
    ERROR: frozenset({CHECKING, DOWNLOADING, UPDATE_AVAILABLE, UP_TO_DATE, ERROR}),
}

_INTERRUPTED_STATES = frozenset({CHECKING, DOWNLOADING, WAITING_FOR_IDLE,
                                 INSTALL_HANDOFF, INSTALLING, RESTARTING})


class InvalidStateTransition(ValueError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def default_update_state(current_version="unknown"):
    now = utc_now()
    return {
        "schema_version": SCHEMA_VERSION,
        "state": IDLE,
        "current_version": current_version,
        "latest_version": None,
        "last_check_at": None,
        "last_successful_check_at": None,
        "timestamps": {
            "created_at": now,
            "updated_at": now,
            "check_started_at": None,
            "check_completed_at": None,
            "download_started_at": None,
            "download_completed_at": None,
        },
        "release_url": None,
        "release_notes": "",
        "release": None,
        "downloaded_asset": None,
        "download_progress": {
            "bytes_downloaded": 0,
            "bytes_total": None,
            "percentage": 0.0,
            "bytes_per_second": 0.0,
            "eta_seconds": None,
        },
        "last_error": None,
        "last_update_result": None,
        "recovery_notices": [],
    }


def _notice(code, message, path=None):
    value = {"code": code, "message": message, "detected_at": utc_now()}
    if path is not None:
        value["path"] = str(path)
    return value


class UpdateStateStore:
    """Crash-resistant JSON state under an injected data directory or path."""

    def __init__(self, data_dir=None, path=None):
        if path is None:
            if data_dir is None:
                program_data = os.environ.get("PROGRAMDATA") or str(Path.home())
                data_dir = Path(program_data) / "FlowShift"
            path = Path(data_dir) / "update_state.json"
        self.path = Path(path)
        self.data_dir = self.path.parent

    def load(self, current_version="unknown"):
        baseline = default_update_state(current_version)
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return baseline

        if not self.path.exists():
            self.save(baseline)
            return baseline

        try:
            with self.path.open("r", encoding="utf-8") as stream:
                loaded = json.load(stream)
            if not isinstance(loaded, dict):
                raise ValueError("update state must be an object")
            if loaded.get("schema_version") != SCHEMA_VERSION:
                raise ValueError("unsupported update state schema")
            if loaded.get("state") not in STATES:
                raise ValueError("invalid persisted update state")
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            return self._recover_corrupt(baseline, exc)

        state = copy.deepcopy(baseline)
        state.update(loaded)
        state["current_version"] = current_version
        for key in ("timestamps", "download_progress"):
            value = baseline[key].copy()
            if isinstance(loaded.get(key), dict):
                value.update(loaded[key])
            state[key] = value
        if not isinstance(state.get("recovery_notices"), list):
            state["recovery_notices"] = []
        return state

    def _recover_corrupt(self, baseline, error):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = self.path.with_name(f"{self.path.stem}.backup-corrupt-{stamp}{self.path.suffix}")
        preserved = None
        try:
            os.replace(self.path, backup)
            preserved = backup
        except OSError:
            try:
                shutil.copy2(self.path, backup)
                preserved = backup
            except OSError:
                pass
        baseline["recovery_notices"].append(_notice(
            "corrupt_state_recovered",
            f"Corrupt update state was ignored: {type(error).__name__}",
            preserved or self.path,
        ))
        self.save(baseline)
        return baseline

    def save(self, state):
        temporary = None
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(state, stream, indent=2, sort_keys=True, ensure_ascii=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            return True
        except (OSError, TypeError, ValueError):
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            return False

    def recovery_notices(self):
        """Report restart artifacts without deleting or mutating any of them."""
        notices = []
        downloads = self.data_dir / "updates" / "downloads"
        try:
            for part in sorted(downloads.glob("*.part")):
                if part.is_file():
                    notices.append(_notice(
                        "stale_partial_download", "A partial update download remains", part))
        except OSError:
            pass

        indicators = (
            ("stale_update_plan", self.data_dir / "update_plan.json",
             "An update handoff plan remains"),
            ("stale_update_plan", self.data_dir / "updates" / "update_plan.json",
             "An update handoff plan remains"),
            ("rollback_directory_present", self.data_dir / "rollback",
             "An updater rollback directory is present"),
            ("rollback_directory_present", self.data_dir / "updates" / "rollback",
             "An updater rollback directory is present"),
            ("update_result_present", self.data_dir / "update_result.json",
             "An updater result file is present"),
            ("update_result_present", self.data_dir / "updates" / "last_update_result.json",
             "An updater result file is present"),
            ("update_result_present", self.data_dir / "install_result.json",
             "An installer result file is present"),
        )
        for code, path, message in indicators:
            try:
                if path.exists():
                    notices.append(_notice(code, message, path))
            except OSError:
                pass
        return notices


class UpdateStateMachine:
    """Thread-safe state owner with explicit, persisted transitions."""

    def __init__(self, current_version="unknown", store=None, initial_state=None):
        self._lock = threading.RLock()
        self._store = store
        if initial_state is not None:
            self._state = copy.deepcopy(initial_state)
        elif store is not None:
            self._state = store.load(current_version)
        else:
            self._state = default_update_state(current_version)

        self._state["current_version"] = current_version
        if store is not None:
            self._merge_notices(store.recovery_notices())
        if self._state.get("state") in _INTERRUPTED_STATES:
            previous = self._state["state"]
            self._state["state"] = ERROR
            self._state["last_error"] = {
                "code": "interrupted_operation",
                "message": f"The previous {previous} operation did not complete",
                "operation": previous,
                "at": utc_now(),
            }
            self._merge_notices([_notice(
                "interrupted_operation", f"Recovered interrupted update state: {previous}")])
        self._persist()

    def _merge_notices(self, notices):
        existing = self._state.setdefault("recovery_notices", [])
        known = {(item.get("code"), item.get("path")) for item in existing
                 if isinstance(item, dict)}
        for notice in notices:
            identity = notice.get("code"), notice.get("path")
            if identity not in known:
                existing.append(copy.deepcopy(notice))
                known.add(identity)

    def _persist(self):
        self._state.setdefault("timestamps", {})["updated_at"] = utc_now()
        if self._store is not None and not self._store.save(self._state):
            notices = self._state.setdefault("recovery_notices", [])
            if not any(item.get("code") == "state_persistence_failed"
                       for item in notices if isinstance(item, dict)):
                notices.append(_notice(
                    "state_persistence_failed",
                    "Update state could not be persisted atomically",
                    self._store.path,
                ))

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._state)

    def transition(self, target, **changes):
        with self._lock:
            current = self._state["state"]
            if target not in STATES:
                raise InvalidStateTransition(f"unknown update state: {target}")
            if target not in TRANSITIONS[current]:
                raise InvalidStateTransition(f"invalid update transition: {current} -> {target}")
            self._state.update(copy.deepcopy(changes))
            self._state["state"] = target
            self._persist()
            return copy.deepcopy(self._state)

    def patch(self, **changes):
        with self._lock:
            if "state" in changes and changes["state"] != self._state["state"]:
                raise InvalidStateTransition("state changes must use transition()")
            self._state.update(copy.deepcopy(changes))
            self._persist()
            return copy.deepcopy(self._state)
