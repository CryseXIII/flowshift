"""FlowShift diagnostics helpers tests."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import flowshift_diagnostics as diag

_failures = []


def check(cond, label):
    if cond:
        print(f"[PASS] {label}")
    else:
        print(f"[FAIL] {label}")
        _failures.append(label)


tmp = tempfile.mkdtemp(prefix="fs_diag_")
try:
    cfg_path = os.path.join(tmp, "config.json")
    log_dir = os.path.join(tmp, "logs")
    clipboard_root = os.path.join(log_dir, "clipboard")
    os.makedirs(log_dir, exist_ok=True)
    os.makedirs(os.path.join(clipboard_root, "profiles", "p1", "objects"), exist_ok=True)
    os.makedirs(os.path.join(clipboard_root, "temp", "incoming"), exist_ok=True)
    with open(os.path.join(clipboard_root, "temp", "keep.txt"), "w", encoding="utf-8") as f:
        f.write("keep")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "device_name": "DiagHost",
                "device_id": "1234abcd",
                "port": 45781,
                "peers": [{"name": "Alpha"}],
                "hotkeys": [{"action": "forward_peer:device:abcd"}],
                "clipboard": {
                    "enabled": True,
                    "max_auto_transfer_mb": 12,
                    "max_item_gb": 3,
                    "clipboard_disk_assembler_threshold_mb": 7,
                    "clipboard_ram_zip_limit_mb": 8,
                    "clipboard_temp_cleanup_max_age_hours": 9,
                },
            },
            f,
        )

    old_cfg_file = diag.CONFIG_FILE
    old_log_dir = diag.LOG_DIR
    old_log_file = diag.LOG_FILE
    old_runtime_out = diag.RUNTIME_OUT
    old_clip_root = diag.CLIPBOARD_ROOT
    try:
        diag.CONFIG_FILE = cfg_path
        diag.LOG_DIR = log_dir
        diag.LOG_FILE = os.path.join(log_dir, "flowshift.log")
        diag.RUNTIME_OUT = os.path.join(log_dir, "flowshift_runtime.out")
        diag.CLIPBOARD_ROOT = clipboard_root

        with open(cfg_path, encoding="utf-8") as cfg_file:
            cfg_data = json.load(cfg_file)

        snapshot = diag.collect_environment_snapshot(
            config=cfg_data,
            runtime_state={
                "status": {
                    "workers": {"forward_loop": {"alive": True, "failed": False}},
                    "runtime_healthy": True,
                    "critical_workers_down": [],
                    "session": {"session_id": 1, "interactive": True, "is_service_session": False, "username": "Viktor"},
                    "active_peer_identity": "device:abcd",
                    "active_peer": "Alpha",
                    "peers": [{"name": "Alpha"}],
                },
                "clipboard_progress": {"item1": {"status": "running"}},
                "control_socket_reachable": True,
                "source": "runtime",
            },
        )

        required_fields = [
            "app_version", "protocol_version", "git_commit", "git_branch", "python_version",
            "platform", "is_windows", "process_id", "working_dir", "config_path",
            "config_exists", "log_dir", "log_file_exists", "runtime_out_exists",
            "control_socket_host", "control_socket_port", "control_socket_reachable",
            "peer_port", "peers_count", "active_profile_identity", "active_profile_name",
            "clipboard_enabled", "clipboard_root", "clipboard_root_exists",
            "clipboard_profiles_dir_exists", "clipboard_objects_dir_exists",
            "clipboard_temp_dir_exists", "clipboard_incoming_dir_exists",
            "free_disk_bytes_clipboard_root", "pillow_available", "pillow_version",
            "max_auto_transfer_mb", "max_item_gb", "disk_assembler_threshold_mb",
            "ram_zip_limit_mb", "temp_cleanup_max_age_hours", "worker_health",
            "runtime_healthy", "critical_workers_down", "session", "clipboard_progress_count",
        ]
        for field in required_fields:
            check(field in snapshot, f"snapshot includes {field}")

        check(isinstance(snapshot["pillow_available"], bool), "pillow_available is bool")
        check(snapshot["control_socket_reachable"] is True, "runtime reachable captured")
        check(snapshot["clipboard_progress_count"] == 1, "clipboard progress count captured")
        check(snapshot["active_profile_name"] == "Alpha", "active profile name captured")
        check(snapshot["clipboard_root"] == clipboard_root, "clipboard root captured")
        check(snapshot["clipboard_objects_dir_exists"] is True, "clipboard objects dir exists")

        old_platform = diag.sys.platform
        try:
            diag.sys.platform = "linux"
            offwin = diag.collect_environment_snapshot(config=cfg_data, runtime_state=None)
            check(offwin["is_windows"] is False, "off-Windows snapshot is supported")
        finally:
            diag.sys.platform = old_platform

        report = diag.format_diagnostics_report(snapshot)
        check("Version" in report and "Clipboard" in report and "Problems" in report, "report has readable sections")
        check("FlowShift HTML Test" not in report and "secret clipboard text" not in report, "report excludes clipboard contents")

        ok, problems = diag.diagnostics_ok(snapshot)
        check(ok is True, "valid snapshot is OK")
        check(problems == [], "valid snapshot has no problems")

        missing_root = dict(snapshot)
        missing_root["clipboard_root_exists"] = False
        missing_root["clipboard_profiles_dir_exists"] = False
        missing_root["clipboard_objects_dir_exists"] = False
        missing_root["clipboard_temp_dir_exists"] = False
        missing_root["free_disk_bytes_clipboard_root"] = None
        ok2, problems2 = diag.diagnostics_ok(missing_root)
        check(ok2 is False, "missing clipboard root is a problem when enabled")
        check(any("Clipboard-Root" in p or "Clipboard" in p for p in problems2), "missing clipboard root reported")

        offline_payload = None
        old_request = diag._control_request
        try:
            def offline_request(_payload, timeout=1.5):
                raise OSError("control socket unavailable")

            diag._control_request = offline_request
            offline_payload = diag.collect_diagnostics(timeout=0.1)
        finally:
            diag._control_request = old_request
        check(offline_payload["runtime_reachable"] is False, "offline diagnostics marks runtime unreachable")
        check("Runtime control socket not reachable" in offline_payload["report"], "offline report includes hint")

    finally:
        diag.CONFIG_FILE = old_cfg_file
        diag.LOG_DIR = old_log_dir
        diag.LOG_FILE = old_log_file
        diag.RUNTIME_OUT = old_runtime_out
        diag.CLIPBOARD_ROOT = old_clip_root

    cli_env = dict(os.environ)
    cli_env["FLOWSHIFT_CONFIG"] = cfg_path
    cli_env["FLOWSHIFT_LOG_DIR"] = log_dir
    cli_run = subprocess.run(
        [sys.executable, os.path.join(os.path.dirname(__file__), "flowshift_diagnose.py"), "--json", "--timeout", "0.1"],
        capture_output=True,
        text=True,
        env=cli_env,
    )
    check(cli_run.returncode in (0, 1), "flowshift_diagnose --json exits cleanly")
    cli_payload = json.loads(cli_run.stdout)
    check("snapshot" in cli_payload and "report" in cli_payload, "flowshift_diagnose --json emits payload")

finally:
    try:
        shutil.rmtree(tmp, ignore_errors=True)
    except Exception:
        pass


print()
if _failures:
    print(f"FAILED: {len(_failures)} check(s): {', '.join(_failures)}")
    sys.exit(1)
print("All diagnostics tests passed.")
