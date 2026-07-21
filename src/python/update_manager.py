"""Serialized Phase 1.5 update discovery and download orchestration."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
import queue
import threading
import time

import update_client
from update_download import DownloadError, download_installer
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
    UPDATE_AVAILABLE,
    UP_TO_DATE,
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
ERROR_INCOMPATIBLE_UPDATER = "incompatible_updater"
ERROR_OPERATION_INTERRUPTED = "operation_interrupted"


@dataclass(frozen=True, slots=True)
class ManagerResult:
    status: str
    operation: str
    message: str = ""
    error_code: str | None = None

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


class UpdateManager:
    """One long-lived worker with a one-command queue and honest admission results."""

    def __init__(self, data_dir=None, state_path=None, current_version=version.APP_VERSION,
                 updater_version=None, discovery=None, download_transport=None,
                 logger=None, runtime_root=None, official_install_dir=None,
                 program_files=None):
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
        self._shutdown = threading.Event()
        self._commands = queue.Queue(maxsize=1)
        self._admission_lock = threading.Lock()
        self._active_operation = None
        self._development_mode = is_development_mode(
            runtime_root or Path(__file__).resolve().parents[2],
            official_install_dir=official_install_dir,
            program_files=program_files,
        )
        self._worker = threading.Thread(
            target=self._worker_main,
            name="FlowShiftUpdateWorker",
            daemon=False,
        )
        self._worker.start()
        _emit(self._logger, "update_manager_started", "Update manager worker started")
        for notice in self._machine.snapshot().get("recovery_notices", []):
            if isinstance(notice, dict):
                _emit(self._logger, "update_recovery_notice", notice.get("message", "Recovery notice"))

    @property
    def worker_thread(self):
        return self._worker

    def snapshot(self):
        value = self._machine.snapshot()
        value["development_mode"] = self._development_mode
        value["can_install"] = not self._development_mode
        value["blocked_reason"] = "development_checkout" if self._development_mode else None
        with self._admission_lock:
            value["active_operation"] = self._active_operation
            value["shutting_down"] = self._shutdown.is_set()
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
            except Exception as exc:
                self._set_error("manager_internal_error",
                                f"Update {command} failed internally: {type(exc).__name__}",
                                command)
                _emit(self._logger, "manager_internal_error", str(exc))
            finally:
                with self._admission_lock:
                    self._active_operation = None
                self._commands.task_done()
            if self._shutdown.is_set():
                break

    def _run_check(self):
        now = utc_now()
        self._machine.transition(
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
            self._machine.transition(
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
            self._machine.transition(
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
        self._machine.transition(
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
        self._machine.transition(
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
            self._machine.transition(ERROR, **changes)
        except InvalidStateTransition:
            self._machine.patch(**changes)

    def wait_for_quiescence(self, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._admission_lock:
                if self._active_operation is None:
                    return True
            time.sleep(0.005)
        return False

    def shutdown(self, timeout=5.0):
        self._shutdown.set()
        _emit(self._logger, "update_manager_shutdown", "Update manager shutdown requested")
        if threading.current_thread() is not self._worker:
            self._worker.join(max(0.0, timeout))
        return not self._worker.is_alive()
