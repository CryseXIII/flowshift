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
import sys
import time
import uuid

import install_lock
from update_download import DownloadedAsset, asset_basename
from update_model import (
    CHANNEL_DEVELOPMENT,
    CHANNELS,
    MAX_INSTALLER_SIZE,
    ReleaseDescriptor,
    validate_source_url,
)
from version import CREATE_NO_WINDOW, parse_semver


SCHEMA_VERSION = 1
TASK_NAME = "FlowShift"
CONTROL_PORT = 45782
PEER_PORT = 45781
API_PORT = 5000
RUNNER_NAME = "update_flowshift.ps1"
ACK_TIMEOUT_SECONDS = 60.0
ACK_POLL_SECONDS = 0.05
PACKAGE_INSTALLER = "installer"
PACKAGE_SOURCE = "source"
# Package of a connected FlowShift with a newer version (peer_update.py).
PACKAGE_PEER = "peer"
ACK_ACCEPTED = "accepted"
ACK_BUSY = "busy"
ACK_STATUSES = frozenset({ACK_ACCEPTED, ACK_BUSY})
ERROR_INSTALL_BUSY = "install_busy"
# install_flowshift.ps1 builds the WebGUI of a source state with Node.js
# (webgui/dist is not in the repository); same minimum as install_webgui.ps1.
MIN_NODE_MAJOR = 24
NODE_PROBE_TIMEOUT_SECONDS = 15


def is_elevated():
    """True when this process may replace the program folder under Program Files.

    FlowShift normally runs through its scheduled task with the highest
    privileges, so the external updater inherits administrator rights and no
    UAC prompt appears. Started directly (desktop shortcut of an older install,
    command line), it runs without them and an automatic update would wait for
    a UAC click nobody gives.
    """
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


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
    if release.channel not in CHANNELS:
        raise UpdateHandoffError("invalid_release", "Release channel is invalid")
    # Development packages carry prerelease versions; the stable channel only
    # installs stable versions. The installed version may be a prerelease on
    # both channels: a PC that ran a development state (0.7.0-dev.50) and is
    # switched back to stable must reach 0.7.0 through GitHub as well.
    prerelease_target_allowed = release.channel == CHANNEL_DEVELOPMENT
    if (str(current) != release.current_version or str(target) != release.version
            or current.build or target.build
            or (not prerelease_target_allowed and not target.is_stable)
            or target <= current or release.relation != "newer"
            or release.tag != f"v{release.version}"):
        raise UpdateHandoffError("invalid_release", "Release version relationship is invalid")
    for value in (release.release_url, release.installer_url, release.manifest_url,
                  release.checksums_url):
        try:
            validate_source_url(value, release.channel)
        except ValueError as exc:
            raise UpdateHandoffError("invalid_release", "Release contains an unapproved URL") from exc

    digest = release.installer_sha256
    if (not isinstance(digest, str) or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)):
        raise UpdateHandoffError("invalid_release", "Release installer hash is invalid")
    if (type(release.installer_size) is not int or release.installer_size <= 0
            or release.installer_size > MAX_INSTALLER_SIZE):
        raise UpdateHandoffError("invalid_release", "Release installer size is invalid")

    expected_name = asset_basename(release.version, release.channel)
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


def node_candidates(program_files=None, which=shutil.which):
    """node.exe locations in the order install_webgui.ps1 checks them."""
    candidates = []
    for base in (program_files or os.environ.get("ProgramFiles"),
                 os.environ.get("ProgramFiles(x86)")):
        if base:
            candidates.append(Path(base) / "nodejs" / "node.exe")
    found = which("node")
    if found:
        candidates.append(Path(found))
    return candidates


def find_node(candidates=None, run=subprocess.run):
    """Return ``(path, version)`` of a usable Node.js >= 24 or ``None``."""
    for candidate in (node_candidates() if candidates is None else candidates):
        candidate = Path(candidate)
        if not candidate.is_file() or not (candidate.parent / "npm.cmd").is_file():
            continue
        try:
            completed = run([str(candidate), "--version"], capture_output=True, text=True,
                            timeout=NODE_PROBE_TIMEOUT_SECONDS, shell=False,
                            creationflags=CREATE_NO_WINDOW)
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
        text = (completed.stdout or "").strip()
        major = text.lstrip("v").split(".", 1)[0]
        if completed.returncode == 0 and major.isdigit() and int(major) >= MIN_NODE_MAJOR:
            return str(candidate), text
    return None


def _read_ack(path, request_id):
    """Return the validated ack status ("accepted" or "busy") or ``None``."""
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return None
    if (isinstance(value, dict) and value.get("schema_version") == SCHEMA_VERSION
            and value.get("request_id") == request_id
            and value.get("status") in ACK_STATUSES
            and type(value.get("runner_pid")) is int and value["runner_pid"] > 0):
        return value["status"]
    return None


def _install_busy_error(owner=None):
    holder = install_lock.describe_owner(owner)
    return UpdateHandoffError(
        ERROR_INSTALL_BUSY,
        f"Another FlowShift installation is running ({holder}). Nothing was changed; "
        "FlowShift tries again at its next regular update check.")


def handoff_update(release, asset, runtime_root, runtime_pid=None, *, launcher=None,
                   ack_timeout=ACK_TIMEOUT_SECONDS, poll_interval=ACK_POLL_SECONDS,
                   node_finder=None, elevation_check=None, lock_check=None,
                   _program_files=None, _program_data=None, _clock=time.monotonic,
                   _sleep=time.sleep, _request_id=None, _created_at=None):
    """Stage and launch the updater, returning only after its validated ack.

    A development update is a Forgejo source archive: the runner unpacks it and
    runs its install_flowshift.ps1, which builds the WebGUI with Node.js. Without
    Node.js 24+ the handoff stops here, before FlowShift is stopped.

    The underscored overrides exist for isolated tests. Production layout values
    always come from the Windows environment and no URL or executable is accepted.
    """
    install_dir, data_dir = _official_layout(
        runtime_root, _program_files or os.environ.get("ProgramFiles"),
        _program_data or os.environ.get("PROGRAMDATA"))

    installer_path = _validate_release_and_asset(release, asset, install_dir, data_dir)
    package_kind = PACKAGE_SOURCE if release.channel == CHANNEL_DEVELOPMENT else PACKAGE_INSTALLER
    if package_kind == PACKAGE_SOURCE and (node_finder or find_node)() is None:
        raise UpdateHandoffError(
            "node_missing",
            f"Node.js {MIN_NODE_MAJOR} or newer is not installed; the development state "
            "builds its WebGUI with it. Install Node.js LTS once, then check again.")
    return _stage_and_launch(
        release.current_version, release.version, installer_path, release.installer_size,
        release.installer_sha256, package_kind, install_dir, data_dir, runtime_pid,
        launcher=launcher, ack_timeout=ack_timeout, poll_interval=poll_interval,
        elevation_check=elevation_check, lock_check=lock_check, _clock=_clock, _sleep=_sleep,
        _request_id=_request_id, _created_at=_created_at)


def _official_layout(runtime_root, program_files_value, program_data_value):
    if not program_files_value or not program_data_value:
        raise UpdateHandoffError("unsupported_layout", "Official Windows paths are unavailable")
    install_dir = Path(program_files_value) / "FlowShift"
    data_dir = Path(program_data_value) / "FlowShift"
    if not _same_path(runtime_root, install_dir):
        raise UpdateHandoffError("development_mode", "Updates require the official installed layout")
    if not install_dir.is_dir():
        raise UpdateHandoffError("install_missing", "Official FlowShift installation is missing")
    return install_dir, data_dir


def handoff_peer_package(from_version, to_version, package_path, size, sha256, runtime_root,
                         runtime_pid=None, *, launcher=None, ack_timeout=ACK_TIMEOUT_SECONDS,
                         poll_interval=ACK_POLL_SECONDS, elevation_check=None, lock_check=None,
                         _program_files=None, _program_data=None, _clock=time.monotonic,
                         _sleep=time.sleep, _request_id=None, _created_at=None):
    """Hand a checked package from a connected FlowShift to the external updater.

    ``peer_update.verify_package`` has checked the package file by file before;
    here only the fixed place, size, SHA-256 and the version relation are
    checked again, then the same runner as for every other update installs it
    (install lock, rollback copy, health check, rollback on failure). The
    package carries its prebuilt WebGUI, so no Node.js is needed.
    """
    install_dir, data_dir = _official_layout(
        runtime_root, _program_files or os.environ.get("ProgramFiles"),
        _program_data or os.environ.get("PROGRAMDATA"))
    try:
        current = parse_semver(from_version)
        target = parse_semver(to_version)
    except (TypeError, ValueError) as exc:
        raise UpdateHandoffError("invalid_version", "Update versions are invalid") from exc
    if (str(current) != from_version or str(target) != to_version or current.build or target.build
            or target <= current):
        raise UpdateHandoffError("invalid_release", "Release version relationship is invalid")
    if (not isinstance(sha256, str) or len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)):
        raise UpdateHandoffError("invalid_release", "Package hash is invalid")
    if type(size) is not int or size <= 0 or size > MAX_INSTALLER_SIZE:
        raise UpdateHandoffError("invalid_release", "Package size is invalid")
    expected_path = data_dir / "updates" / "downloads" / f"FlowShift-Peer-{to_version}.zip"
    if not _same_path(package_path, expected_path):
        raise UpdateHandoffError("invalid_asset", "Package is outside managed update storage")
    if not expected_path.is_file():
        raise UpdateHandoffError("asset_missing", "Verified package is missing")
    try:
        actual_size = expected_path.stat().st_size
        actual_hash = _hash_file(expected_path)
    except OSError as exc:
        raise UpdateHandoffError("asset_unreadable", "Verified package could not be read") from exc
    if actual_size != size:
        raise UpdateHandoffError("size_mismatch", "Package size changed after the check")
    if actual_hash != sha256:
        raise UpdateHandoffError("hash_mismatch", "Package hash changed after the check")
    return _stage_and_launch(
        from_version, to_version, expected_path, size, sha256, PACKAGE_PEER, install_dir, data_dir,
        runtime_pid, launcher=launcher, ack_timeout=ack_timeout, poll_interval=poll_interval,
        elevation_check=elevation_check, lock_check=lock_check, _clock=_clock, _sleep=_sleep,
        _request_id=_request_id, _created_at=_created_at)


def _stage_and_launch(from_version, to_version, installer_path, installer_size, installer_sha256,
                      package_kind, install_dir, data_dir, runtime_pid, *, launcher, ack_timeout,
                      poll_interval, elevation_check, lock_check, _clock, _sleep, _request_id,
                      _created_at):
    if not (elevation_check or is_elevated)():
        raise UpdateHandoffError(
            "not_elevated",
            "FlowShift is running without administrator rights and cannot replace its "
            "program folder. Sign out and in again (or restart the PC) so FlowShift "
            "starts through its autostart; it then installs the update by itself.")
    runner_source = install_dir / RUNNER_NAME
    if not runner_source.is_file():
        raise UpdateHandoffError("runner_missing", "Installed external updater is missing")
    # Checked before anything is written; the runner checks again and holds
    # the lock for its whole run (install_flowshift.ps1 holds it as well).
    if (lock_check or install_lock.is_locked)(data_dir):
        raise _install_busy_error(install_lock.read_owner(data_dir))

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
        "from_version": from_version,
        "to_version": to_version,
        "installer_path": str(Path(installer_path).resolve()),
        "installer_size": installer_size,
        "installer_sha256": installer_sha256,
        "package_kind": package_kind,
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
        # Never inherit this runtime's working directory (src/python inside the
        # program folder): it alone keeps the folder busy and the updater could
        # not move it aside ("access denied").
        launch(arguments, shell=False, creationflags=CREATE_NO_WINDOW, cwd=str(updates_dir))
    except (OSError, ValueError, TypeError) as exc:
        raise UpdateHandoffError("runner_launch_failed", "Could not launch the external updater") from exc

    deadline = _clock() + max(float(ack_timeout), 0.0)
    while True:
        status = _read_ack(ack_path, request_id)
        if status == ACK_BUSY:
            # The runner found the install lock taken (an installation started
            # in between) and ended without touching anything.
            raise _install_busy_error(install_lock.read_owner(data_dir))
        if status == ACK_ACCEPTED:
            return HandoffResult("started", request_id, str(plan_path), str(ack_path))
        if _clock() >= deadline:
            raise UpdateHandoffError("ack_timeout", "External updater did not acknowledge the request")
        _sleep(max(float(poll_interval), 0.001))
