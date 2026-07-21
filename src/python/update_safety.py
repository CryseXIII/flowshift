"""Pure update-install safety checks over an immutable runtime snapshot."""
from __future__ import annotations


REASON_PRIORITY = (
    "runtime_shutting_down",
    "update_operation_active",
    "forwarding_active",
    "edge_session_active",
    "clipboard_transfer_active",
    "input_pipeline_busy",
    "overlay_command_active",
)


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _edge_active(snapshot):
    if snapshot.get("edge_session_active"):
        return True
    session = snapshot.get("edge_session")
    if isinstance(session, dict):
        return session.get("active", True) is not False
    edge = _mapping(snapshot.get("edge_switching"))
    session = edge.get("active_session")
    if isinstance(session, dict):
        return session.get("active", True) is not False
    return bool(session)


def _clipboard_active(snapshot):
    if snapshot.get("clipboard_transfer_active"):
        return True
    activity = snapshot.get("clipboard_activity", snapshot.get("clipboard"))
    if not isinstance(activity, dict):
        return False
    if activity.get("blocking"):
        return True
    if activity.get("blocking_job_statuses"):
        return True
    for key in ("blocking_jobs", "active_assemblers", "active_local_operations",
                "queued", "active", "retry_pending"):
        value = activity.get(key, 0)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return True
    transfer_queue = _mapping(activity.get("transfer_queue"))
    if transfer_queue.get("blocking") or transfer_queue.get("blocking_job_statuses"):
        return True
    for key in ("queued", "active", "retry_pending"):
        value = transfer_queue.get(key, 0)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return True
    return False


def _pipeline_busy(snapshot):
    if snapshot.get("input_pipeline_busy"):
        return True
    pipeline = _mapping(snapshot.get("input_pipeline", snapshot.get("pipeline")))
    if pipeline.get("busy"):
        return True
    if pipeline.get("pressed_state_active"):
        return True
    pressed = _mapping(pipeline.get("pressed_state"))
    if pressed.get("active"):
        return True
    for tracker in ("sent", "injected"):
        if _mapping(pressed.get(tracker)).get("active"):
            return True
    for key in ("event_queue_size", "inject_queue_size", "queued", "active"):
        value = pipeline.get(key, 0)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return True
    return False


def is_safe_to_install_update(snapshot):
    """Return ``{safe, reason, blockers}`` without mutating runtime activity.

    Known blockers are always returned in ``REASON_PRIORITY`` order. Callers may
    add future conservative blockers through ``additional_blockers``; unknown
    fields otherwise remain forward-compatible.
    """
    if not isinstance(snapshot, dict):
        return {
            "safe": False,
            "reason": "runtime_state_unknown",
            "blockers": ["runtime_state_unknown"],
        }

    update = _mapping(snapshot.get("update"))
    forwarding = _mapping(snapshot.get("forwarding_state"))
    overlay = _mapping(snapshot.get("overlay"))
    active = {
        "runtime_shutting_down": bool(
            snapshot.get("runtime_shutting_down") or snapshot.get("shutting_down")),
        "update_operation_active": bool(
            snapshot.get("update_operation_active")
            or update.get("operation_active")
            or update.get("active_operation")),
        "forwarding_active": bool(
            snapshot.get("forwarding_active") or forwarding.get("active")),
        "edge_session_active": _edge_active(snapshot),
        "clipboard_transfer_active": _clipboard_active(snapshot),
        "input_pipeline_busy": _pipeline_busy(snapshot),
        "overlay_command_active": bool(
            snapshot.get("overlay_command_active")
            or overlay.get("command_active")
            or overlay.get("commands_active")),
    }
    blockers = [reason for reason in REASON_PRIORITY if active[reason]]
    additional = snapshot.get("additional_blockers", ())
    if isinstance(additional, str):
        additional = (additional,)
    if isinstance(additional, (set, frozenset)):
        additional = sorted(additional, key=str)
    if isinstance(additional, (list, tuple)):
        blockers.extend(str(reason) for reason in additional
                        if reason and str(reason) not in blockers)
    return {
        "safe": not blockers,
        "reason": blockers[0] if blockers else "safe",
        "blockers": blockers,
    }
