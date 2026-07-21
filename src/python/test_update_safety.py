"""Pure update-install idle/safety foundation tests."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from update_safety import REASON_PRIORITY, is_safe_to_install_update


def check(condition, label):
    if condition:
        print(f"[PASS] {label}")
        return
    print(f"[FAIL] {label}")
    raise AssertionError(label)


idle = {
    "shutting_down": False,
    "update_operation_active": False,
    "forwarding_active": False,
    "edge_session_active": False,
    "clipboard_transfer_active": False,
    "input_pipeline_busy": False,
    "overlay_command_active": False,
}
check(is_safe_to_install_update(idle) == {
    "safe": True, "reason": "safe", "blockers": [],
}, "fully idle snapshot is safe")

all_busy = dict(idle, shutting_down=True, update_operation_active=True,
                forwarding_active=True, edge_session_active=True,
                clipboard_transfer_active=True, input_pipeline_busy=True,
                overlay_command_active=True)
result = is_safe_to_install_update(all_busy)
check(result["safe"] is False, "active runtime is unsafe")
check(result["reason"] == "runtime_shutting_down", "shutdown has stable first priority")
check(result["blockers"] == list(REASON_PRIORITY), "all blockers use stable priority")

forwarding = is_safe_to_install_update(dict(idle, forwarding_active=True))
check(forwarding["reason"] == "forwarding_active", "forwarding blocks install")

edge = is_safe_to_install_update(dict(
    idle, edge_session_active=False,
    edge_switching={"active_session": {"session_id": "edge-1", "active": True}},
))
check(edge["reason"] == "edge_session_active", "active edge session blocks install")

clipboard = is_safe_to_install_update(dict(
    idle, clipboard_transfer_active=False,
    clipboard_activity={"blocking": True, "active_assemblers": 1},
))
check(clipboard["reason"] == "clipboard_transfer_active",
      "clipboard activity snapshot blocks install")

pipeline = is_safe_to_install_update(dict(
    idle, input_pipeline_busy=False,
    pipeline={"event_queue_size": 1, "inject_queue_size": 0},
))
check(pipeline["reason"] == "input_pipeline_busy", "queued input blocks install")

overlay = is_safe_to_install_update(dict(
    idle, overlay_command_active=False, overlay={"command_active": True},
))
check(overlay["reason"] == "overlay_command_active", "overlay command blocks install")

check(is_safe_to_install_update(None)["reason"] == "runtime_state_unknown",
      "missing runtime state fails closed")

print("\nAll update safety tests passed.")
