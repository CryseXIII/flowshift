"""Serialized Phase 1.5 update discovery, download, and install orchestration."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import queue
import threading
import time

import update_client
from update_download import DownloadError, DownloadedAsset, download_installer
from update_model import (
    ReleaseDescriptor,
    STATUS_ERROR,
    STATUS_NO_STABLE_RELEASE,
    STATUS_UPDATE_AVAILABLE,
    STATUS_UP_TO_DATE,
    UpdateResult,
)
from update_state import (
    CHECKING,
    DOWNLOADED,
    DOWNLOADING,
    ERROR,
    INSTALL_HANDOFF,
    INSTALLING,
    UPDATE_AVAILABLE,
    UP_TO_DATE,
    WAITING_FOR_IDLE,
    InvalidStateTransition,
    UpdateStateMachine,
    UpdateStateStore,
    utc_now,
)
import version


RESULT_QUEUED = "queued"
RESULT_ALREADY_RUNNING = "already_running"
RESULT_NOT_AVAILABLE = "not_available"
RESULT_SHUTTING_DOWN = "shutting_down"
RESULT_BLOCKED = "blocked"
ERROR_INCOMPATIBLE_UPDATER = "incompatible_updater"
ERROR_OPERATION_INTERRUPTED = "operation_interrupted"
ERROR_DEVELOPMENT_MODE = "development_mode"
ERROR_INSTALL_BLOCKED = "install_blocked"
EXTERNAL_RESULT_STATUSES = frozenset({
    "success", "failed", "rollback_success", "rollback_failed",
})


@dataclass(frozen=True, slots=True)
class ManagerResult:
    status: str
    operation: str
    message: str = ""
    error_code: str | None = None
    blocker: dict | None = None

    def to_dict(self):
        return asdict(self)


def _same_path(left, right):
    try:
        left = os.path.normcase(os.path.abspath(os.fspath(left)))
        right = os.path.normcase(os.path.abspath(os.fspath(right)))
        return left == right
    except (OSError, TypeError, ValueError):
        return False


def is_development_mode(runtime_root, official_install_dir=None, program_files=None):
    """Pure installed-layout check; anything except an exact official root is dev."""
    candidates = []
    if official_install_dir is not None:
        candidates.append(Path(official_install_dir))
    if program_files is None:
        program_files = os.environ.get("ProgramFiles") or os.environ.get("PROGRAMFILES")
    if program_files:
        candidates.append(Path(program_files) / "FlowShift")
    return not any(_same_path(runtime_root, candidate) for candidate in candidates)


def _emit(logger, code, message):
    if logger is not None:
        try:
            logger(code, message)
        except Exception:
            pass


def _safe_result(value, default_reason="runtime_state_unknown"):
    if isinstance(value, dict) and type(value.get("safe")) is bool:
        blockers = value.get("blockers", [])
        if not isinstance(blockers, list):
            blockers = []
        reason = value.get("reason")
        return {
            "safe": value["safe"],
            "reason": reason if isinstance(reason, str) and reason else (
                "safe" if value["safe"] else default_reason),
            "blockers": [str(item) for item in blockers if item],
        }
    if value is True:
        return {"safe": True, "reason": "safe", "blockers": []}
    return {"safe": False, "reason": default_reason, "blockers": [default_reason]}


class UpdateManager:
    """Two bounded workers: serialized commands and automatic policy scheduling."""

    def __init__(self, data_dir=None, state_path=None, current_version=version.APP_VERSION,
                 updater_version=None, discovery=None, download_transport=None,
                 logger=None, runtime_root=None, official_install_dir=None,
                 program_files=None, safety_query=None, reserve=None, release=None,
                 handoff=None, shutdown_callback=None, status_callback=None,
                 install_retry_interval=2.0):
        if data_dir is None and state_path is None:
            program_data = os.environ.get("PROGRAMDATA") or str(Path.home())
            data_dir = Path(program_data) / "FlowShift"
        self.current_version = current_version
        self.updater_version = updater_version or current_version
        self._store = UpdateStateStore(data_dir=data_dir, path=state_path)
        self.data_dir = self._store.data_dir
        self._machine = UpdateStateMachine(current_version, self._store)
        self._discovery = discovery or update_client.discover_stable_release
        self._download_transport = download_transport
        self._logger = logger
        self._safety_query = safety_query or (
            lambda: {"safe": True, "reason": "safe", "blockers": []})
        self._reserve = reserve or (lambda automatic: True)
        self._release = release or (lambda: None)
        self._handoff = handoff
        self._shutdown_callback = shutdown_callback or (lambda: None)
        self._status_callback = status_callback
        self._install_retry_interval = max(0.05, float(install_retry_interval))
        self._shutdown = threading.Event()
        self._commands = queue.Queue(maxsize=1)
        self._admission_lock = threading.Lock()
        self._active_operation = None
        self._reservation_held = False
        self._install_pending = False
        self._policy_thread = None
        self._policy_wakeup = threading.Event()
        self._policy_settings_getter = None
        self._development_mode = is_development_mode(
            runtime_root or Path(__file__).resolve().parents[2],
            official_install_dir=official_install_dir,
            program_files=program_files,
        )
        self._ingest_external_result()
        self._worker = threading.Thread(
            target=self._worker_main,
            name="FlowShiftUpdateWorker",
            daemon=True,
        )
        self._worker.start()
        _emit(self._logger, "update_manager_started", "Update manager worker started")
        for notice in self._machine.snapshot().get("recovery_notices", []):
            if isinstance(notice, dict):
                _emit(self._logger, "update_recovery_notice", notice.get("message", "Recovery notice"))
        self._publish_status()

    @property
    def worker_thread(self):
        return self._worker

    @property
    def policy_thread(self):
        return self._policy_thread

    def snapshot(self):
        value = self._machine.snapshot()
        value["development_mode"] = self._development_mode
        descriptor = None
        try:
            if isinstance(value.get("release"), dict):
                descriptor = ReleaseDescriptor(**value["release"])
        except (TypeError, ValueError):
            pass
        managed_asset_ready = (
            value.get("state") == DOWNLOADED
            and self._downloaded_asset(descriptor) is not None
        )
        value["can_install"] = bool(not self._development_mode and managed_asset_ready)
        if self._development_mode:
            value["blocked_reason"] = "development_checkout"
        elif not managed_asset_ready:
            value["blocked_reason"] = "update_not_downloaded"
        else:
            value["blocked_reason"] = None
        with self._admission_lock:
            value["active_operation"] = self._active_operation
            value["reservation_held"] = self._reservation_held
            value["install_pending"] = self._install_pending
            value["shutting_down"] = self._shutdown.is_set()
            value["automatic_policy_running"] = bool(
                self._policy_thread is not None and self._policy_thread.is_alive())
        return value

    def _publish_status(self):
        if self._status_callback is not None:
            try:
                self._status_callback(self.snapshot())
            except Exception:
                pass

    def _transition(self, target, **changes):
        value = self._machine.transition(target, **changes)
        self._publish_status()
        return value

    def _patch(self, **changes):
        value = self._machine.patch(**changes)
        self._publish_status()
        return value

    def _admit(self, operation):
        with self._admission_lock:
            if self._shutdown.is_set():
                return ManagerResult(RESULT_SHUTTING_DOWN, operation,
                                     "Update manager is shutting down")
            if self._active_operation is not None:
                return ManagerResult(RESULT_ALREADY_RUNNING, operation,
                                     f"{self._active_operation} is already running")
            self._active_operation = operation
            try:
                self._commands.put_nowait(operation)
            except queue.Full:
                self._active_operation = None
                return ManagerResult(RESULT_ALREADY_RUNNING, operation,
                                     "Update command queue is busy")
        _emit(self._logger, f"{operation}_queued", f"Update {operation} queued")
        self._policy_wakeup.set()
        self._publish_status()
        return ManagerResult(RESULT_QUEUED, operation, f"Update {operation} queued")

    def check_for_updates(self):
        return self._admit("check")

    def _descriptor(self):
        release = self._machine.snapshot().get("release")
        if not isinstance(release, dict):
            return None
        try:
            return ReleaseDescriptor(**release)
        except (TypeError, ValueError):
            return None

    def _downloaded_asset(self, descriptor):
        record = self._machine.snapshot().get("downloaded_asset")
        if not isinstance(record, dict) or descriptor is None:
            return None
        try:
            asset = DownloadedAsset(**record)
        except (TypeError, ValueError):
            return None
        expected_name = f"FlowShift-Setup-{descriptor.version}.exe"
        expected_path = self.data_dir / "updates" / "downloads" / expected_name
        if (asset.basename != expected_name or asset.version != descriptor.version
                or asset.size != descriptor.installer_size
                or asset.sha256 != descriptor.installer_sha256
                or not _same_path(asset.path, expected_path)):
            return None
        try:
            if not expected_path.is_file() or expected_path.stat().st_size != asset.size:
                return None
        except OSError:
            return None
        return asset

    def _updater_compatible(self, descriptor):
        try:
            return version.parse_semver(self.updater_version) >= version.parse_semver(
                descriptor.minimum_updater_version)
        except ValueError:
            return False

    def download_update(self):
        with self._admission_lock:
            if self._shutdown.is_set():
                return ManagerResult(RESULT_SHUTTING_DOWN, "download",
                                     "Update manager is shutting down")
            if self._active_operation is not None:
                return ManagerResult(RESULT_ALREADY_RUNNING, "download",
                                     f"{self._active_operation} is already running")

        descriptor = self._descriptor()
        state = self._machine.snapshot()["state"]
        if descriptor is None or descriptor.relation != "newer" or state not in {
                UPDATE_AVAILABLE, ERROR, DOWNLOADED}:
            return ManagerResult(RESULT_NOT_AVAILABLE, "download",
                                 "No validated update is available")
        if not self._updater_compatible(descriptor):
            message = (f"Updater {self.updater_version} is older than required "
                       f"{descriptor.minimum_updater_version}")
            self._set_error(ERROR_INCOMPATIBLE_UPDATER, message, "download")
            _emit(self._logger, ERROR_INCOMPATIBLE_UPDATER, message)
            return ManagerResult(RESULT_NOT_AVAILABLE, "download", message,
                                 ERROR_INCOMPATIBLE_UPDATER)
        return self._admit("download")

    def _acquire_reservation(self, automatic):
        try:
            reserved = self._reserve(bool(automatic))
        except Exception as exc:
            return _safe_result(False, f"reservation_error:{type(exc).__name__}")
        if reserved is False:
            return _safe_result(False, "reservation_unavailable")
        with self._admission_lock:
            self._reservation_held = True
        try:
            return _safe_result(self._safety_query())
        except Exception:
            return _safe_result(False)

    def _release_reservation(self):
        with self._admission_lock:
            held = self._reservation_held
            self._reservation_held = False
        if held:
            try:
                self._release()
            except Exception as exc:
                _emit(self._logger, "update_reservation_release_failed", str(exc))
        self._publish_status()

    def install_update(self, automatic=False):
        operation = "install"
        with self._admission_lock:
            if self._shutdown.is_set():
                return ManagerResult(RESULT_SHUTTING_DOWN, operation,
                                     "Update manager is shutting down")
            if self._active_operation is not None or self._install_pending:
                running = self._active_operation or "install"
                return ManagerResult(RESULT_ALREADY_RUNNING, operation,
                                     f"{running} is already running")
            if self._development_mode:
                blocker = {"safe": False, "reason": "development_checkout",
                           "blockers": ["development_checkout"]}
                return ManagerResult(RESULT_BLOCKED, operation,
                                     "Install is disabled in development mode",
                                     ERROR_DEVELOPMENT_MODE, blocker)
            self._active_operation = "install_prepare"

        descriptor = self._descriptor()
        asset = self._downloaded_asset(descriptor)
        state = self._machine.snapshot()["state"]
        if state != DOWNLOADED or descriptor is None or asset is None:
            with self._admission_lock:
                self._active_operation = None
            return ManagerResult(RESULT_NOT_AVAILABLE, operation,
                                 "No currently verified managed asset is ready to install")

        safety = self._acquire_reservation(automatic)
        if not safety["safe"] and not automatic:
            self._release_reservation()
            with self._admission_lock:
                self._active_operation = None
            _emit(self._logger, "install_blocked", f"Update install blocked: {safety['reason']}")
            self._publish_status()
            return ManagerResult(RESULT_BLOCKED, operation,
                                 f"Update install blocked: {safety['reason']}",
                                 ERROR_INSTALL_BLOCKED, safety)

        try:
            if safety["safe"]:
                self._transition(INSTALL_HANDOFF, last_error=None)
            else:
                self._transition(WAITING_FOR_IDLE, last_error=None)
                _emit(self._logger, "install_waiting_for_idle",
                      f"Automatic update waiting for idle: {safety['reason']}")
        except InvalidStateTransition:
            self._release_reservation()
            with self._admission_lock:
                self._active_operation = None
            return ManagerResult(RESULT_NOT_AVAILABLE, operation,
                                 "Update state changed before install admission")

        with self._admission_lock:
            self._active_operation = operation
            self._install_pending = True
            try:
                self._commands.put_nowait(operation)
            except queue.Full:
                self._active_operation = None
                self._install_pending = False
                queue_busy = True
            else:
                queue_busy = False
        if queue_busy:
            self._set_error("install_queue_busy", "Update command queue is busy", operation)
            self._release_reservation()
            return ManagerResult(RESULT_ALREADY_RUNNING, operation, "Update command queue is busy")
        _emit(self._logger, "install_queued", "Update install queued")
        self._policy_wakeup.set()
        self._publish_status()
        message = ("Automatic update is waiting for runtime idle"
                   if not safety["safe"] else "Update install handoff queued")
        return ManagerResult(RESULT_QUEUED, operation, message,
                             blocker=safety if not safety["safe"] else None)

    def _worker_main(self):
        while True:
            try:
                command = self._commands.get(timeout=0.1)
            except queue.Empty:
                if self._shutdown.is_set():
                    break
                continue
            try:
                if command == "check":
                    self._run_check()
                elif command == "download":
                    self._run_download()
                elif command == "install":
                    self._run_install()
            except Exception as exc:
                self._set_error("manager_internal_error",
                                f"Update {command} failed internally: {type(exc).__name__}",
                                command)
                _emit(self._logger, "manager_internal_error", str(exc))
                if command == "install":
                    self._release_reservation()
                    with self._admission_lock:
                        self._install_pending = False
            finally:
                with self._admission_lock:
                    self._active_operation = None
                self._commands.task_done()
                self._policy_wakeup.set()
                self._publish_status()
            if self._shutdown.is_set():
                break

    def _run_check(self):
        now = utc_now()
        self._transition(
            CHECKING,
            last_error=None,
            timestamps={**self._machine.snapshot()["timestamps"], "check_started_at": now},
        )
        _emit(self._logger, "check_started", "Stable update check started")
        if self._shutdown.is_set():
            self._set_error(ERROR_OPERATION_INTERRUPTED, "Update check interrupted by shutdown", "check")
            return
        try:
            result = self._discovery(current_version=self.current_version, logger=self._logger)
        except Exception as exc:
            self._set_error("discovery_error",
                            f"Update discovery failed: {type(exc).__name__}", "check")
            _emit(self._logger, "check_failed", "Update discovery raised an exception")
            return
        if self._shutdown.is_set():
            self._set_error(ERROR_OPERATION_INTERRUPTED, "Update check interrupted by shutdown", "check")
            return
        if not isinstance(result, UpdateResult):
            self._set_error("invalid_discovery_result", "Discovery returned an invalid result", "check")
            return

        completed = utc_now()
        timestamps = {**self._machine.snapshot()["timestamps"], "check_completed_at": completed}
        result_record = {
            "status": result.status,
            "error_code": result.error_code,
            "message": result.message,
            "at": completed,
        }
        if (result.status == STATUS_UPDATE_AVAILABLE and result.release is not None
                and result.release.relation == "newer"):
            descriptor = asdict(result.release)
            self._transition(
                UPDATE_AVAILABLE,
                latest_version=result.release.version,
                last_check_at=completed,
                last_successful_check_at=completed,
                release_url=result.release.release_url,
                release_notes=result.release.release_notes,
                release=descriptor,
                downloaded_asset=None,
                last_error=None,
                last_update_result=result_record,
                timestamps=timestamps,
            )
            _emit(self._logger, "check_update_available",
                  f"Update {result.release.version} is available")
            return
        if (result.status in {STATUS_UP_TO_DATE, STATUS_NO_STABLE_RELEASE}
                or (result.release is not None and result.release.relation in {"same", "older"})):
            descriptor = asdict(result.release) if result.release is not None else None
            self._transition(
                UP_TO_DATE,
                latest_version=result.release.version if result.release is not None else None,
                last_check_at=completed,
                last_successful_check_at=completed,
                release_url=result.release.release_url if result.release is not None else None,
                release_notes=result.release.release_notes if result.release is not None else "",
                release=descriptor,
                downloaded_asset=None,
                last_error=None,
                last_update_result=result_record,
                timestamps=timestamps,
            )
            _emit(self._logger, "check_up_to_date", "FlowShift is up to date")
            return
        code = result.error_code or "discovery_error"
        self._set_error(code, result.message or "Update discovery failed", "check",
                        result_record=result_record, timestamps=timestamps)
        _emit(self._logger, "check_failed", result.message or code)

    def _run_download(self):
        descriptor = self._descriptor()
        if descriptor is None:
            self._set_error("release_descriptor_missing",
                            "Validated release descriptor is unavailable", "download")
            return
        if not self._updater_compatible(descriptor):
            self._set_error(ERROR_INCOMPATIBLE_UPDATER,
                            "Current updater does not meet the release minimum", "download")
            return
        timestamps = {**self._machine.snapshot()["timestamps"],
                      "download_started_at": utc_now(), "download_completed_at": None}
        self._transition(
            DOWNLOADING,
            downloaded_asset=None,
            download_progress={
                "bytes_downloaded": 0,
                "bytes_total": descriptor.installer_size,
                "percentage": 0.0,
                "bytes_per_second": 0.0,
                "eta_seconds": None,
            },
            last_error=None,
            timestamps=timestamps,
        )
        _emit(self._logger, "download_started", f"Downloading update {descriptor.version}")

        def progress(value):
            self._machine.patch(download_progress=value)

        try:
            asset = download_installer(
                descriptor,
                self.data_dir,
                self.current_version,
                transport=self._download_transport,
                progress_callback=progress,
                should_abort=self._shutdown.is_set,
                logger=self._logger,
            )
        except DownloadError as exc:
            self._set_error(exc.code, str(exc), "download")
            _emit(self._logger, "download_failed", f"{exc.code}: {exc}")
            return
        if self._shutdown.is_set():
            self._set_error(ERROR_OPERATION_INTERRUPTED,
                            "Installer download interrupted by shutdown", "download")
            return
        completed = utc_now()
        timestamps = {**self._machine.snapshot()["timestamps"],
                      "download_completed_at": completed}
        asset_record = asdict(asset)
        self._transition(
            DOWNLOADED,
            downloaded_asset=asset_record,
            last_error=None,
            last_update_result={
                "status": DOWNLOADED,
                "version": descriptor.version,
                "reused": asset.reused,
                "at": completed,
            },
            timestamps=timestamps,
        )
        _emit(self._logger, "download_completed",
              f"Verified update {descriptor.version} downloaded")

    def _automatic_install_enabled(self):
        getter = self._policy_settings_getter
        if getter is None:
            return True
        try:
            settings = getter()
        except Exception:
            return False
        return (isinstance(settings, dict)
                and settings.get("enabled") is True
                and settings.get("policy") == "install")

    def _run_install(self):
        descriptor = self._descriptor()
        asset = self._downloaded_asset(descriptor)
        if descriptor is None or asset is None or self._handoff is None:
            self._set_error("install_data_invalid",
                            "Verified update handoff data is unavailable", "install")
            self._release_reservation()
            with self._admission_lock:
                self._install_pending = False
            return

        while self._machine.snapshot()["state"] == WAITING_FOR_IDLE:
            if self._shutdown.is_set():
                self._set_error(ERROR_OPERATION_INTERRUPTED,
                                "Update install interrupted by shutdown", "install")
                self._release_reservation()
                with self._admission_lock:
                    self._install_pending = False
                return
            if not self._automatic_install_enabled():
                self._transition(DOWNLOADED, last_error=None)
                self._release_reservation()
                with self._admission_lock:
                    self._install_pending = False
                _emit(self._logger, "install_wait_cancelled", "Automatic updates were disabled")
                return
            try:
                safety = _safe_result(self._safety_query())
            except Exception:
                safety = _safe_result(False)
            if safety["safe"]:
                self._transition(INSTALL_HANDOFF, last_error=None)
                break
            _emit(self._logger, "install_waiting_for_idle",
                  f"Automatic update still waiting for idle: {safety['reason']}")
            if self._shutdown.wait(self._install_retry_interval):
                continue

        if self._shutdown.is_set():
            self._set_error(ERROR_OPERATION_INTERRUPTED,
                            "Update install interrupted by shutdown", "install")
            self._release_reservation()
            with self._admission_lock:
                self._install_pending = False
            return
        _emit(self._logger, "install_handoff_started",
              f"Handing update {descriptor.version} to external updater")
        try:
            self._handoff(descriptor, asset)
        except Exception as exc:
            code = getattr(exc, "code", "handoff_failed")
            self._set_error(code, str(exc) or "External updater handoff failed", "install")
            self._release_reservation()
            with self._admission_lock:
                self._install_pending = False
            _emit(self._logger, "install_handoff_failed", f"{code}: {exc}")
            return

        self._transition(INSTALLING, last_error=None)
        _emit(self._logger, "external_updater_started",
              f"External updater acknowledged update {descriptor.version}")
        try:
            self._shutdown_callback()
        except Exception as exc:
            _emit(self._logger, "runtime_shutdown_request_failed", str(exc))

    def _set_error(self, code, message, operation, result_record=None, timestamps=None):
        occurred_at = utc_now()
        error = {"code": code, "message": message, "operation": operation, "at": occurred_at}
        changes = {
            "last_error": error,
            "last_update_result": result_record or {
                "status": ERROR,
                "error_code": code,
                "message": message,
                "operation": operation,
                "at": occurred_at,
            },
        }
        if operation == "check":
            changes["last_check_at"] = occurred_at
        if timestamps is not None:
            changes["timestamps"] = timestamps
        try:
            self._transition(ERROR, **changes)
        except InvalidStateTransition:
            self._patch(**changes)

    def _ingest_external_result(self):
        path = self.data_dir / "updates" / "last_update_result.json"
        try:
            if not path.is_file():
                return
            with path.open("r", encoding="utf-8") as stream:
                value = json.load(stream)
            if not isinstance(value, dict) or type(value.get("schema_version")) is not int \
                    or value.get("schema_version") != 1:
                raise ValueError("invalid result schema")
            for key in ("from_version", "to_version", "started_at", "finished_at"):
                if not isinstance(value.get(key), str) or not value[key]:
                    raise ValueError(f"invalid result field: {key}")
            if value.get("result") not in EXTERNAL_RESULT_STATUSES:
                raise ValueError("invalid update result")
            if value.get("error") is not None and not isinstance(value.get("error"), str):
                raise ValueError("invalid result error")
            if set(value) - {"schema_version", "from_version", "to_version", "started_at",
                             "finished_at", "result", "error"}:
                raise ValueError("unexpected result field")
            if value["result"] == "success":
                if value["to_version"] == self.current_version:
                    try:
                        self._machine.transition(
                            UP_TO_DATE,
                            latest_version=self.current_version,
                            downloaded_asset=None,
                            download_progress={
                                "bytes_downloaded": 0,
                                "bytes_total": None,
                                "percentage": 0.0,
                                "bytes_per_second": 0.0,
                                "eta_seconds": None,
                            },
                            release=None,
                            last_error=None,
                            last_update_result=value,
                        )
                    except InvalidStateTransition:
                        self._machine.patch(last_update_result=value)
                else:
                    self._machine.transition(
                        ERROR,
                        last_error={
                            "code": "external_result_version_mismatch",
                            "message": "Successful update result does not match the running version",
                            "operation": "startup_recovery",
                            "at": utc_now(),
                        },
                        last_update_result=value,
                    )
            else:
                self._machine.patch(last_update_result=value)
            _emit(self._logger, "external_update_result_loaded",
                  f"Loaded external update result: {value['result']}")
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            snapshot = self._machine.snapshot()
            notices = list(snapshot.get("recovery_notices", []))
            notices.append({
                "code": "invalid_external_update_result",
                "message": f"External update result was ignored: {type(exc).__name__}",
                "path": str(path),
                "detected_at": utc_now(),
            })
            self._machine.patch(recovery_notices=notices)
            _emit(self._logger, "external_update_result_invalid", str(exc))

    def start_automatic_updates(self, settings_getter, readiness_predicate,
                                startup_delay=5.0, poll_interval=5.0,
                                startup_wait=None):
        """Start the single nonblocking automatic-policy scheduler."""
        if not callable(settings_getter) or not callable(readiness_predicate):
            raise TypeError("automatic update settings and readiness callbacks are required")
        with self._admission_lock:
            if self._shutdown.is_set():
                return False
            if self._policy_thread is not None:
                return False
            self._policy_settings_getter = settings_getter
            delay = min(10.0, max(2.0, float(startup_delay)))
            interval = min(3600.0, max(0.05, float(poll_interval)))
            self._policy_thread = threading.Thread(
                target=self._policy_main,
                args=(readiness_predicate, delay, interval, startup_wait),
                name="FlowShiftUpdatePolicy",
                daemon=True,
            )
            self._policy_thread.start()
        _emit(self._logger, "automatic_updates_scheduled",
              f"Automatic update policy scheduled after {delay:.1f}s")
        self._publish_status()
        return True

    def _policy_settings(self):
        try:
            value = self._policy_settings_getter()
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}

    def _policy_main(self, readiness_predicate, startup_delay, poll_interval, startup_wait):
        while not self._shutdown.is_set():
            try:
                if readiness_predicate():
                    break
            except Exception:
                pass
            self._policy_wakeup.wait(poll_interval)
            self._policy_wakeup.clear()
        if startup_wait is None:
            if self._shutdown.wait(startup_delay):
                return
        else:
            try:
                if startup_wait(self._shutdown, startup_delay):
                    return
            except Exception as exc:
                _emit(self._logger, "automatic_startup_wait_failed", str(exc))
                return

        settings = self._policy_settings()
        if settings.get("enabled") is True and settings.get("check_on_start") is True:
            self.check_for_updates()

        while not self._shutdown.is_set():
            settings = self._policy_settings()
            if settings.get("enabled") is True:
                policy = settings.get("policy", "notify")
                snapshot = self.snapshot()
                if snapshot.get("active_operation") is None:
                    if snapshot.get("state") == UPDATE_AVAILABLE and policy in {"download", "install"}:
                        self.download_update()
                    elif (snapshot.get("state") == DOWNLOADED and policy == "install"
                          and snapshot.get("can_install") is True):
                        self.install_update(automatic=True)
            self._policy_wakeup.wait(poll_interval)
            self._policy_wakeup.clear()

    def wait_for_quiescence(self, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._admission_lock:
                if self._active_operation is None:
                    return True
            time.sleep(0.005)
        return False

    def shutdown(self, timeout=5.0):
        timeout = max(0.0, float(timeout))
        deadline = time.monotonic() + timeout
        self._shutdown.set()
        self._policy_wakeup.set()
        _emit(self._logger, "update_manager_shutdown", "Update manager shutdown requested")
        policy = self._policy_thread
        if policy is not None and threading.current_thread() is not policy:
            policy.join(max(0.0, deadline - time.monotonic()))
        if threading.current_thread() is not self._worker:
            self._worker.join(max(0.0, deadline - time.monotonic()))
        self._publish_status()
        return (not self._worker.is_alive()
                and (policy is None or not policy.is_alive()))
