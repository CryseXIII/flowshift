"""Handler table and central check for Command Wheel actions.

Every action ``kind`` of the registry in ``overlay_actions`` maps to exactly one
handler. A handler carries a mandatory risk class; a handler without one is
refused when it is registered. ``dispatch`` is the single check every action
passes before it runs, in this order:

1. look up handler and class by the action's ``kind``;
2. check the channel the tray set (never a value from a request body);
3. write one log line with only action id, channel and class (never contents);
4. run the handler.

This module has no Windows dependencies so the check is tested on every
platform; ``tray.py`` supplies the productive handler functions.
"""
from __future__ import annotations

# L0 copies or opens local views, L1 sends key sequences to the focused
# window. L2 (send to an own, confirmed target) and L3 (mail, new or foreign
# target) are reserved for later handlers.
RISK_CLASSES = ("L0", "L1", "L2", "L3")

CHANNEL_WHEEL_SWIPE = "wheel_swipe"
CHANNEL_WEB_API = "web_api"
CHANNELS = frozenset({CHANNEL_WHEEL_SWIPE, CHANNEL_WEB_API})

# Risk class of each productive handler kind.
HANDLER_CLASSES = {
    "keys": "L1",
    "runtime": "L0",
}


class HandlerError(ValueError):
    """A handler registration was refused."""


class HandlerRegistry:
    def __init__(self, log=None):
        self._handlers = {}
        self._log = log

    def register(self, kind, handler, risk_class=None):
        if not isinstance(kind, str) or not kind:
            raise HandlerError("handler kind must be a non-empty string")
        if kind in self._handlers:
            raise HandlerError(f"handler kind {kind} is already registered")
        if not callable(handler):
            raise HandlerError(f"handler for {kind} is not callable")
        if risk_class not in RISK_CLASSES:
            raise HandlerError(f"handler {kind} needs a risk class ({', '.join(RISK_CLASSES)})")
        self._handlers[kind] = (handler, risk_class)

    def kinds(self):
        return tuple(self._handlers)

    def risk_class(self, kind):
        entry = self._handlers.get(kind)
        return entry[1] if entry else None

    def dispatch(self, action, context, channel):
        """Run ``action`` through the check; returns ``{"ok", "reason"}``."""
        entry = self._handlers.get(action.get("kind"))
        if entry is None:
            return {"ok": False, "reason": "unknown_kind"}
        handler, risk_class = entry
        if channel not in CHANNELS:
            return {"ok": False, "reason": "invalid_channel"}
        if self._log is not None:
            self._log(f"action check id={action['id']} channel={channel} class={risk_class}")
        return handler(action, context)


def build_registry(handlers, log=None):
    """Registry of the productive handlers; each kind takes its class from
    ``HANDLER_CLASSES``, so a kind without a class is refused."""
    registry = HandlerRegistry(log=log)
    for kind, handler in handlers.items():
        registry.register(kind, handler, HANDLER_CLASSES.get(kind))
    return registry
