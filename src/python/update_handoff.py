"""Validated handoff from the running application to the external updater."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid

from update_download import DownloadedAsset
from update_model import MAX_INSTALLER_SIZE, ReleaseDescriptor, validate_github_https_url
from version import CREATE_NO_WINDOW, parse_semver


SCHEMA_VERSION = 1
TASK_NAME = "FlowShift"
CONTROL_PORT = 45782
PEER_PORT = 45781
API_PORT = 5000
RUNNER_NAME = "update_flowshift.ps1"
ACK_TIMEOUT_SECONDS = 60.0
ACK_POLL_SECONDS = 0.05


class UpdateHandoffError(RuntimeError):
    """A safe, caller-facing handoff failure with a stable error code."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class HandoffResult:
    status: str
    request_id: str
    plan_path: str
    ack_path: str


def _same_path(left, right):
    try:
        return os.path.normcase(str(Path(left).resolve(strict=False))) == os.path.normcase(
            str(Path(right).resolve(strict=False)))
    except (OSError, TypeError, ValueError):
        return False


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, indent=2, sort_keys=True, ensure_ascii=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except (OSError, TypeError, ValueError) as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise UpdateHandoffError("plan_write_failed", "Could not write the update plan") from exc


def _copy_runner(source, destination):
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise UpdateHandoffError("runner_copy_failed", "Could not stage the external updater") from exc


def _hash_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _validate_release_and_asset(release, asset, install_dir, data_dir):
    if type(release) is not ReleaseDescriptor or type(asset) is not DownloadedAsset:
        raise UpdateHandoffError(
            "unverified_update_data", "Validated release and downloaded asset objects are required")

    try:
        current = parse_semver(release.current_version)
        target = parse_semver(release.version)
    except ValueError as exc:
        raise UpdateHandoffError("invalid_version", "Update versions are invalid") from exc
    if (str(current) != release.current_version or str(target) != release.version
            or not current.is_stable or not target.is_stable
            or target <= current or release.relation != "newer"
            or release.tag != f"v{release.version}"):
        raise UpdateHandoffError("invalid_release", "Release version relationship is invalid")
    for value in (release.release_url, release.installer_url, release.manifest_url,
                  release.checksums_url):
        try:
            validate_github_https_url(value)
        except ValueError as exc:
            raise UpdateHandoffError("invalid_release", "Release contains an unapproved URL") from exc

    digest = release.installer_sha256
    if (not isinstance(digest, str) or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)):
        raise UpdateHandoffError("invalid_release", "Release installer hash is invalid")
    if (type(release.installer_size) is not int or release.installer_size <= 0
            or release.installer_size > MAX_INSTALLER_SIZE):
        raise UpdateHandoffError("invalid_release", "Release installer size is invalid")

    expected_name = f"FlowShift-Setup-{release.version}.exe"
    expected_path = data_dir / "updates" / "downloads" / expected_name
    if (asset.basename != expected_name or asset.version != release.version
            or not _same_path(asset.path, expected_path)
            or asset.size != release.installer_size
            or asset.sha256 != digest):
        raise UpdateHandoffError("invalid_asset", "Downloaded asset does not match managed release data")
    if not expected_path.is_file():
        raise UpdateHandoffError("asset_missing", "Verified installer is missing")
    try:
        actual_size = expected_path.stat().st_size
        actual_hash = _hash_file(expected_path)
    except OSError as exc:
        raise UpdateHandoffError("asset_unreadable", "Verified installer could not be read") from exc
    if actual_size != release.installer_size:
        raise UpdateHandoffError("size_mismatch", "Installer size changed after download")
    if actual_hash != digest:
        raise UpdateHandoffError("hash_mismatch", "Installer hash changed after download")
    return expected_path


def _read_ack(path, request_id):
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
        return (isinstance(value, dict) and value.get("schema_version") == SCHEMA_VERSION
                and value.get("request_id") == request_id
                and value.get("status") == "accepted"
                and type(value.get("runner_pid")) is int and value["runner_pid"] > 0)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return False


def handoff_update(release, asset, runtime_root, runtime_pid=None, *, launcher=None,
                   ack_timeout=ACK_TIMEOUT_SECONDS, poll_interval=ACK_POLL_SECONDS,
                   _program_files=None, _program_data=None, _clock=time.monotonic,
                   _sleep=time.sleep, _request_id=None, _created_at=None):
    """Stage and launch the updater, returning only after its validated ack.

    The underscored overrides exist for isolated tests. Production layout values
    always come from the Windows environment and no URL or executable is accepted.
    """
    program_files_value = _program_files or os.environ.get("ProgramFiles")
    program_data_value = _program_data or os.environ.get("PROGRAMDATA")
    if not program_files_value or not program_data_value:
        raise UpdateHandoffError("unsupported_layout", "Official Windows paths are unavailable")
    program_files = Path(program_files_value)
    program_data = Path(program_data_value)
    install_dir = program_files / "FlowShift"
    data_dir = program_data / "FlowShift"
    if not _same_path(runtime_root, install_dir):
        raise UpdateHandoffError("development_mode", "Updates require the official installed layout")
    if not install_dir.is_dir():
        raise UpdateHandoffError("install_missing", "Official FlowShift installation is missing")

    installer_path = _validate_release_and_asset(release, asset, install_dir, data_dir)
    runner_source = install_dir / RUNNER_NAME
    if not runner_source.is_file():
        raise UpdateHandoffError("runner_missing", "Installed external updater is missing")

    if runtime_pid is None:
        runtime_pid = os.getpid()
    if type(runtime_pid) is not int or runtime_pid <= 0:
        raise UpdateHandoffError("invalid_runtime", "Runtime PID is invalid")
    request_id = _request_id or uuid.uuid4().hex
    if (not isinstance(request_id, str) or len(request_id) != 32
            or any(character not in "0123456789abcdef" for character in request_id)):
        raise UpdateHandoffError("invalid_request", "Update request ID is invalid")

    updates_dir = data_dir / "updates"
    plan_path = updates_dir / "update_plan.json"
    runner_path = updates_dir / "runner" / RUNNER_NAME
    ack_path = updates_dir / "acks" / f"update_ack-{request_id}.json"
    plan = {
        "schema_version": SCHEMA_VERSION,
        "from_version": release.current_version,
        "to_version": release.version,
        "installer_path": str(installer_path.resolve()),
        "installer_size": release.installer_size,
        "installer_sha256": release.installer_sha256,
        "install_dir": str(install_dir.resolve()),
        "data_dir": str(data_dir.resolve()),
        "created_at": _created_at or _utc_now(),
        "request_id": request_id,
        "runtime_pid": runtime_pid,
        "task_name": TASK_NAME,
        "control_port": CONTROL_PORT,
        "peer_port": PEER_PORT,
        "api_port": API_PORT,
    }

    _atomic_json(plan_path, plan)
    _copy_runner(runner_source, runner_path)
    try:
        ack_path.unlink(missing_ok=True)
    except OSError as exc:
        raise UpdateHandoffError("ack_prepare_failed", "Could not prepare updater acknowledgement") from exc

    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    powershell = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    arguments = [
        str(powershell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
        str(runner_path), "-PlanPath", str(plan_path),
    ]
    launch = launcher or subprocess.Popen
    try:
        launch(arguments, shell=False, creationflags=CREATE_NO_WINDOW)
    except (OSError, ValueError, TypeError) as exc:
        raise UpdateHandoffError("runner_launch_failed", "Could not launch the external updater") from exc

    deadline = _clock() + max(float(ack_timeout), 0.0)
    while True:
        if _read_ack(ack_path, request_id):
            return HandoffResult("started", request_id, str(plan_path), str(ack_path))
        if _clock() >= deadline:
            raise UpdateHandoffError("ack_timeout", "External updater did not acknowledge the request")
        _sleep(max(float(poll_interval), 0.001))
