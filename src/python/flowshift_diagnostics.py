"""Shared FlowShift diagnostics helpers.

Builds a serializable snapshot and a readable report for the local runtime,
with an offline fallback when the control socket is unavailable.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import platform as _platform
import shutil
import socket
import sys

import clipboard_model as cbm
import version
from runtime_model import recv_msg, send_msg


_BASE = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.environ.get("FLOWSHIFT_LOG_DIR") or _BASE
try:
    if os.environ.get("FLOWSHIFT_LOG_DIR"):
        os.makedirs(_DATA_DIR, exist_ok=True)
except OSError:
    _DATA_DIR = _BASE

CONFIG_FILE = os.environ.get("FLOWSHIFT_CONFIG") or os.path.join(_DATA_DIR, "config.json")
LOG_DIR = _DATA_DIR
LOG_FILE = os.path.join(LOG_DIR, "flowshift.log")
RUNTIME_OUT = os.path.join(LOG_DIR, "flowshift_runtime.out")
CONTROL_HOST = "127.0.0.1"
CONTROL_PORT = 45782
CLIPBOARD_ROOT = os.path.join(LOG_DIR, "clipboard")


def _utc_now():
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _safe_json_load(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _coerce_dict(value):
    return value if isinstance(value, dict) else {}


def _coerce_list(value):
    return value if isinstance(value, list) else []


def _looks_like_status(value):
    return isinstance(value, dict) and any(k in value for k in ("workers", "session", "active_peer_identity", "active_peer"))


def _normalize_runtime_state(runtime_state):
    if not isinstance(runtime_state, dict):
        return {}
    if isinstance(runtime_state.get("diagnostics"), dict):
        runtime_state = runtime_state["diagnostics"]
    elif isinstance(runtime_state.get("snapshot"), dict):
        runtime_state = runtime_state["snapshot"]
    if isinstance(runtime_state.get("status"), dict) and not _looks_like_status(runtime_state):
        runtime_state = dict(runtime_state)
        runtime_state["status"] = _coerce_dict(runtime_state.get("status"))
    return runtime_state


def _control_request(payload, timeout=1.5):
    with socket.create_connection((CONTROL_HOST, CONTROL_PORT), timeout=timeout) as sock:
        sock.settimeout(timeout)
        send_msg(sock, payload)
        return recv_msg(sock)


def _tail_text_file(path, max_lines=80, max_bytes=16384):
    try:
        if not os.path.exists(path):
            return ""
        with open(path, "rb") as f:
            if max_bytes and max_bytes > 0:
                try:
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    f.seek(max(0, size - max_bytes), os.SEEK_SET)
                except OSError:
                    f.seek(0)
            text = f.read().decode("utf-8", errors="replace")
        lines = text.splitlines()
        if len(lines) > max_lines:
            lines = lines[-max_lines:]
        return "\n".join(lines)
    except Exception:
        return ""


def _file_snapshot(path, max_lines=80):
    snap = {"path": path, "exists": os.path.exists(path)}
    if not snap["exists"]:
        return snap
    try:
        snap["size_bytes"] = os.path.getsize(path)
    except OSError:
        snap["size_bytes"] = None
    tail = _tail_text_file(path, max_lines=max_lines)
    if tail:
        snap["tail"] = tail
    return snap


def _first_existing_path(*paths):
    for path in paths:
        if path and os.path.exists(path):
            return path
    return None


def _free_disk_bytes(path):
    probe = path
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            probe = None
            break
        probe = parent
    if not probe:
        probe = _first_existing_path(LOG_DIR, _BASE, os.getcwd())
    if not probe:
        return None
    try:
        return int(shutil.disk_usage(probe).free)
    except Exception:
        return None


def _pillow_snapshot():
    try:
        import PIL  # noqa: F401

        import PIL as _PIL

        return True, getattr(_PIL, "__version__", None) or None
    except Exception:
        return False, None


def _clipboard_paths(root):
    profiles_dir = os.path.join(root, "profiles")
    temp_dir = os.path.join(root, "temp")
    incoming_dir = os.path.join(temp_dir, "incoming")
    object_dirs = []
    try:
        if os.path.isdir(profiles_dir):
            for entry in os.listdir(profiles_dir):
                obj_dir = os.path.join(profiles_dir, entry, "objects")
                if os.path.isdir(obj_dir):
                    object_dirs.append(obj_dir)
    except Exception:
        pass
    return profiles_dir, temp_dir, incoming_dir, object_dirs


def _runtime_status_fields(runtime_state):
    state = _normalize_runtime_state(runtime_state)
    status = _coerce_dict(state.get("status")) if "status" in state else state if _looks_like_status(state) else {}
    progress = _coerce_dict(state.get("clipboard_progress"))

    workers = status.get("workers") if isinstance(status.get("workers"), dict) else None
    critical_down = _coerce_list(status.get("critical_workers_down"))
    session = status.get("session") if isinstance(status.get("session"), dict) else None
    active_identity = status.get("active_peer_identity") or state.get("active_profile_identity") or None
    active_name = status.get("active_peer") or status.get("active_peer_label") or state.get("active_profile_name") or None
    runtime_healthy = status.get("runtime_healthy") if "runtime_healthy" in status else state.get("runtime_healthy")

    return {
        "state": state,
        "status": status,
        "progress": progress,
        "workers": workers,
        "critical_workers_down": critical_down,
        "session": session,
        "active_profile_identity": active_identity,
        "active_profile_name": active_name,
        "runtime_healthy": runtime_healthy,
        "control_socket_reachable": bool(state.get("control_socket_reachable", False)),
        "runtime_error": state.get("runtime_error"),
    }


def collect_environment_snapshot(config=None, runtime_state=None):
    cfg = _coerce_dict(config) if config is not None else _safe_json_load(CONFIG_FILE)
    cb_cfg = cbm.clipboard_settings(cfg)
    runtime = _runtime_status_fields(runtime_state)
    status = runtime["status"]
    progress = runtime["progress"]
    workers = runtime["workers"]
    vinfo = version.version_info()

    peers = _coerce_list(cfg.get("peers"))
    peer_port = cfg.get("port", 45781)
    try:
        peer_port = int(peer_port)
    except Exception:
        peer_port = 45781

    clipboard_root = CLIPBOARD_ROOT
    profiles_dir, temp_dir, incoming_dir, object_dirs = _clipboard_paths(clipboard_root)
    clipboard_root_exists = os.path.isdir(clipboard_root)
    clipboard_profiles_dir_exists = os.path.isdir(profiles_dir)
    clipboard_objects_dir_exists = bool(object_dirs)
    clipboard_temp_dir_exists = os.path.isdir(temp_dir)
    clipboard_incoming_dir_exists = os.path.isdir(incoming_dir)
    free_disk_bytes = _free_disk_bytes(clipboard_root)

    pillow_available, pillow_version = _pillow_snapshot()

    snapshot = {
        "generated_at": _utc_now(),
        "source": runtime.get("source") or ("runtime" if runtime["control_socket_reachable"] else "offline"),
        "app_version": version.APP_VERSION,
        "protocol_version": vinfo.get("protocol_version"),
        "git_commit": vinfo.get("git_commit"),
        "git_branch": vinfo.get("git_branch"),
        "python_version": sys.version.split()[0],
        "platform": _platform.platform(),
        "is_windows": sys.platform == "win32",
        "process_id": os.getpid(),
        "working_dir": os.getcwd(),
        "config_path": CONFIG_FILE,
        "config_exists": os.path.exists(CONFIG_FILE),
        "log_dir": LOG_DIR,
        "log_file_exists": os.path.exists(LOG_FILE),
        "runtime_out_exists": os.path.exists(RUNTIME_OUT),
        "control_socket_host": CONTROL_HOST,
        "control_socket_port": CONTROL_PORT,
        "control_socket_reachable": runtime["control_socket_reachable"],
        "peer_port": peer_port,
        "peers_count": len(peers) if peers else len(_coerce_list(status.get("peers"))),
        "active_profile_identity": runtime["active_profile_identity"],
        "active_profile_name": runtime["active_profile_name"],
        "clipboard_enabled": bool(cb_cfg.get("enabled")),
        "clipboard_root": clipboard_root,
        "clipboard_root_exists": clipboard_root_exists,
        "clipboard_profiles_dir_exists": clipboard_profiles_dir_exists,
        "clipboard_objects_dir_exists": clipboard_objects_dir_exists,
        "clipboard_temp_dir_exists": clipboard_temp_dir_exists,
        "clipboard_incoming_dir_exists": clipboard_incoming_dir_exists,
        "free_disk_bytes_clipboard_root": free_disk_bytes,
        "pillow_available": bool(pillow_available),
        "pillow_version": pillow_version,
        "max_auto_transfer_mb": cb_cfg.get("max_auto_transfer_mb"),
        "max_item_gb": cb_cfg.get("max_item_gb"),
        "disk_assembler_threshold_mb": cb_cfg.get("clipboard_disk_assembler_threshold_mb"),
        "ram_zip_limit_mb": cb_cfg.get("clipboard_ram_zip_limit_mb"),
        "temp_cleanup_max_age_hours": cb_cfg.get("clipboard_temp_cleanup_max_age_hours"),
        "clipboard_progress_count": len(progress),
        "transfer_job_count": len(progress),
        "worker_health": workers,
        "runtime_healthy": runtime["runtime_healthy"],
        "critical_workers_down": runtime["critical_workers_down"],
        "session": runtime["session"],
        "runtime_error": runtime["runtime_error"],
    }
    return snapshot


def diagnostics_ok(snapshot):
    problems = []
    if not snapshot.get("config_exists"):
        problems.append("config fehlt")

    clipboard_enabled = bool(snapshot.get("clipboard_enabled"))
    if clipboard_enabled:
        if not snapshot.get("clipboard_root_exists"):
            problems.append("Clipboard-Root fehlt")
        if not snapshot.get("clipboard_profiles_dir_exists"):
            problems.append("Clipboard-Profiles-Dir fehlt")
        if not snapshot.get("clipboard_temp_dir_exists"):
            problems.append("Clipboard-Temp-Dir fehlt")
        if not snapshot.get("clipboard_objects_dir_exists"):
            problems.append("Clipboard-Objects-Dir fehlt")
        if snapshot.get("free_disk_bytes_clipboard_root") is None:
            problems.append("freier Speicher für Clipboard-Root nicht bestimmbar")

    if snapshot.get("control_socket_reachable"):
        if snapshot.get("runtime_healthy") is False:
            problems.append("Runtime gesundheitsstatus ist fehlerhaft")
        down = snapshot.get("critical_workers_down") or []
        if down:
            problems.append("kritische Worker down: " + ", ".join(map(str, down)))

    return len(problems) == 0, problems


def _diagnostics_hints(snapshot):
    hints = []
    if not snapshot.get("control_socket_reachable"):
        hints.append("Runtime control socket not reachable; showing offline snapshot.")
    if snapshot.get("clipboard_enabled") and snapshot.get("free_disk_bytes_clipboard_root") is not None:
        hints.append("Clipboard storage has measurable free disk space.")
    return hints


def _fmt_bytes(value):
    if value is None:
        return "unknown"
    try:
        value = float(value)
    except Exception:
        return "unknown"
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(value)} {units[idx]}"
    return f"{value:.1f} {units[idx]}"


def _fmt_bool(value):
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "unknown"


def _line(label, value, indent="  "):
    return f"{indent}{label}: {value}"


def format_diagnostics_report(snapshot):
    ok, problems = diagnostics_ok(snapshot)
    hints = _diagnostics_hints(snapshot)
    lines = ["FlowShift Diagnostics"]
    lines.append(_line("generated_at", snapshot.get("generated_at", "-")))
    lines.append("")

    lines.append("Version")
    lines.append(_line("app_version", snapshot.get("app_version", "-")))
    lines.append(_line("protocol_version", snapshot.get("protocol_version", "-")))
    lines.append(_line("git_commit", snapshot.get("git_commit", "-")))
    lines.append(_line("git_branch", snapshot.get("git_branch", "-")))
    lines.append(_line("python_version", snapshot.get("python_version", "-")))
    lines.append(_line("platform", snapshot.get("platform", "-")))
    lines.append(_line("is_windows", _fmt_bool(snapshot.get("is_windows"))))
    lines.append("")

    lines.append("Runtime")
    lines.append(_line("control_socket", f"{snapshot.get('control_socket_host', CONTROL_HOST)}:{snapshot.get('control_socket_port', CONTROL_PORT)}"))
    lines.append(_line("reachable", _fmt_bool(snapshot.get("control_socket_reachable"))))
    if snapshot.get("runtime_error"):
        lines.append(_line("runtime_error", snapshot.get("runtime_error")))
    lines.append(_line("active_profile_identity", snapshot.get("active_profile_identity") or "-"))
    lines.append(_line("active_profile_name", snapshot.get("active_profile_name") or "-"))
    lines.append(_line("runtime_healthy", _fmt_bool(snapshot.get("runtime_healthy"))))
    if snapshot.get("critical_workers_down") is not None:
        down = snapshot.get("critical_workers_down") or []
        lines.append(_line("critical_workers_down", ", ".join(map(str, down)) if down else "-"))
    if snapshot.get("session") is not None:
        sess = snapshot.get("session") or {}
        sid = sess.get("session_id", "-")
        uname = sess.get("username") or "-"
        lines.append(_line("session", f"id={sid} interactive={_fmt_bool(sess.get('interactive'))} service={_fmt_bool(sess.get('is_service_session'))} user={uname}"))
    if snapshot.get("clipboard_progress_count") is not None:
        lines.append(_line("clipboard_progress_count", snapshot.get("clipboard_progress_count")))
    lines.append("")

    lines.append("Clipboard")
    lines.append(_line("enabled", _fmt_bool(snapshot.get("clipboard_enabled"))))
    lines.append(_line("root", snapshot.get("clipboard_root", "-")))
    lines.append(_line("root_exists", _fmt_bool(snapshot.get("clipboard_root_exists"))))
    lines.append(_line("profiles_dir_exists", _fmt_bool(snapshot.get("clipboard_profiles_dir_exists"))))
    lines.append(_line("objects_dir_exists", _fmt_bool(snapshot.get("clipboard_objects_dir_exists"))))
    lines.append(_line("temp_dir_exists", _fmt_bool(snapshot.get("clipboard_temp_dir_exists"))))
    lines.append(_line("incoming_dir_exists", _fmt_bool(snapshot.get("clipboard_incoming_dir_exists"))))
    lines.append(_line("peers_count", snapshot.get("peers_count", "-")))
    lines.append(_line("peer_port", snapshot.get("peer_port", "-")))
    lines.append(_line("max_auto_transfer_mb", snapshot.get("max_auto_transfer_mb", "-")))
    lines.append(_line("max_item_gb", snapshot.get("max_item_gb", "-")))
    lines.append(_line("disk_assembler_threshold_mb", snapshot.get("disk_assembler_threshold_mb", "-")))
    lines.append(_line("ram_zip_limit_mb", snapshot.get("ram_zip_limit_mb", "-")))
    lines.append(_line("temp_cleanup_max_age_hours", snapshot.get("temp_cleanup_max_age_hours", "-")))
    lines.append("")

    lines.append("Storage")
    lines.append(_line("free_disk_bytes_clipboard_root", _fmt_bytes(snapshot.get("free_disk_bytes_clipboard_root"))))
    lines.append("")

    lines.append("Dependencies")
    pillow = f"available={_fmt_bool(snapshot.get('pillow_available'))}"
    if snapshot.get("pillow_version"):
        pillow += f" version={snapshot.get('pillow_version')}"
    lines.append(_line("Pillow", pillow))
    lines.append("")

    lines.append("Worker")
    if snapshot.get("worker_health") is not None:
        workers = snapshot.get("worker_health") or {}
        alive = 0
        failed = 0
        if isinstance(workers, dict):
            for data in workers.values():
                if isinstance(data, dict):
                    if data.get("alive"):
                        alive += 1
                    if data.get("failed"):
                        failed += 1
        lines.append(_line("worker_count", len(workers) if isinstance(workers, dict) else "-"))
        lines.append(_line("alive", alive))
        lines.append(_line("failed", failed))
    else:
        lines.append(_line("worker_health", "unavailable"))
    lines.append("")

    lines.append("Problems")
    if problems:
        for problem in problems:
            lines.append(f"  - {problem}")
    else:
        lines.append("  - none")
    lines.append("")

    lines.append("Hints")
    if hints:
        for hint in hints:
            lines.append(f"  - {hint}")
    else:
        lines.append("  - none")
    lines.append("")
    lines.append(f"Status: {'OK' if ok else 'NOT OK'}")
    return "\n".join(lines)


def collect_diagnostics(timeout=1.5):
    runtime_state = None
    runtime_error = None
    runtime_reachable = False

    try:
        resp = _control_request({"type": "diagnostics"}, timeout=timeout)
        if resp.get("type") == "diagnostics":
            runtime_state = resp.get("diagnostics") or resp.get("snapshot") or {}
            runtime_state = _normalize_runtime_state(runtime_state)
            runtime_state["control_socket_reachable"] = True
            runtime_reachable = True
        elif resp.get("type") == "status":
            runtime_state = {"status": resp.get("status") or {}, "control_socket_reachable": True}
            runtime_reachable = True
        elif resp.get("type") == "error":
            runtime_error = resp.get("error") or "diagnostics request failed"
    except Exception as exc:
        runtime_error = str(exc)

    if not runtime_reachable:
        try:
            resp = _control_request({"type": "status"}, timeout=timeout)
            if resp.get("type") == "status":
                runtime_state = {"status": resp.get("status") or {}, "control_socket_reachable": True}
                runtime_reachable = True
                try:
                    prog = _control_request({"type": "clip_progress"}, timeout=timeout)
                    if prog.get("type") == "ok":
                        runtime_state["clipboard_progress"] = prog.get("progress") or {}
                except Exception:
                    pass
        except Exception as exc:
            runtime_error = runtime_error or str(exc)

    if runtime_state is None:
        runtime_state = {"control_socket_reachable": False, "runtime_error": runtime_error}
    elif runtime_error and "runtime_error" not in runtime_state:
        runtime_state["runtime_error"] = runtime_error

    snapshot = collect_environment_snapshot(runtime_state=runtime_state)
    ok, problems = diagnostics_ok(snapshot)
    return {
        "ok": ok,
        "problems": problems,
        "snapshot": snapshot,
        "diagnostics": snapshot,
        "report": format_diagnostics_report(snapshot),
        "runtime_reachable": bool(snapshot.get("control_socket_reachable")),
        "source": snapshot.get("source", "offline"),
    }


def report_text(report):
    """Backward-compatible alias for the readable report text."""
    if isinstance(report, dict) and "report" in report:
        return str(report["report"])
    if isinstance(report, dict):
        return format_diagnostics_report(report)
    return str(report)
