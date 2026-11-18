"""Check that the installed WebGUI matches the running FlowShift program.

The WebGUI is a mandatory part of FlowShift: the dashboard needs ``index.html``
and the command wheel / clipboard overlay need ``overlay.html``. The installer
stamps the deployed assets with a ``VERSION`` file equal to the program
``VERSION``; Vite builds write the same file into ``webgui/dist``. A missing
file or a different version means the WebGUI was not updated together with the
program (for example an old ``Program Files\\FlowShift\\webgui`` left behind by
an earlier installer).
"""
from __future__ import annotations

from pathlib import Path

REQUIRED_FILES = ("index.html", "overlay.html")
VERSION_FILE = "VERSION"
MAX_VERSION_BYTES = 128

OK = "ok"
MISSING = "missing"
INCOMPLETE = "incomplete"
OUTDATED = "outdated"
UNVERSIONED = "unversioned"

REPAIR_ACTION = ("Run install_flowshift.bat (or FlowShift-Setup.exe) again; "
                 "it updates the program and the WebGUI together and keeps "
                 "your settings.")


def _read_version(root):
    try:
        path = Path(root) / VERSION_FILE
        if not path.is_file() or path.stat().st_size > MAX_VERSION_BYTES:
            return None
        value = path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return value or None


def check_webgui(root, app_version):
    """Return a JSON-safe health report for the WebGUI directory ``root``.

    ``overlay_ready`` is the hard gate for starting the overlay host: without
    ``overlay.html`` the host can only time out. ``ok`` additionally requires
    the WebGUI version to equal the program version.
    """
    app_version = str(app_version or "").strip() or None
    report = {
        "ok": False,
        "state": MISSING,
        "overlay_ready": False,
        "app_version": app_version,
        "webgui_version": None,
        "missing_files": list(REQUIRED_FILES),
        "message": "",
        "action": REPAIR_ACTION,
    }
    if root is None:
        report["message"] = "The FlowShift WebGUI is not installed."
        return report
    root = Path(root)
    try:
        missing = [name for name in REQUIRED_FILES if not (root / name).is_file()]
    except OSError:
        missing = list(REQUIRED_FILES)
    webgui_version = _read_version(root)
    report["missing_files"] = missing
    report["webgui_version"] = webgui_version
    report["overlay_ready"] = "overlay.html" not in missing
    if missing:
        report["state"] = INCOMPLETE if len(missing) < len(REQUIRED_FILES) else MISSING
        report["message"] = (
            "The installed FlowShift WebGUI is incomplete or older than the "
            f"program (missing: {', '.join(missing)}). Command wheel and "
            "clipboard overlay cannot open."
        )
        return report
    if webgui_version is None:
        report["state"] = UNVERSIONED
        report["message"] = (
            "The installed FlowShift WebGUI has no version stamp and is older "
            f"than the program ({app_version or 'unknown'})."
        )
        return report
    if app_version is not None and webgui_version != app_version:
        report["state"] = OUTDATED
        report["message"] = (
            f"The installed FlowShift WebGUI ({webgui_version}) does not match "
            f"the program ({app_version})."
        )
        return report
    report["ok"] = True
    report["state"] = OK
    report["action"] = None
    return report
