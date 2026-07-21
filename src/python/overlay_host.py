"""Isolated FlowShift overlay process using authenticated Windows named pipes."""
from __future__ import annotations

import argparse
import base64
import binascii
import ctypes
import ipaddress
import json
import math
import os
import queue
import sys
import threading
import time
from multiprocessing.connection import Listener
from urllib.parse import urlparse

import overlay_geometry as geometry
import overlay_protocol as protocol


OVERLAY_WIDTH_CSS = 480
OVERLAY_HEIGHT_CSS = 300
UI_QUEUE_SIZE = 64
INVALID_LOG_INTERVAL = 5.0


def _positive_timeout(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return parsed


def _pipe_address(value):
    value = str(value or "")
    prefix = "\\\\.\\pipe\\"
    name = value[len(prefix):] if value.lower().startswith(prefix.lower()) else ""
    if (not name or len(value) > 240 or any(char in name for char in "\\/:\x00")
            or name in (".", "..")):
        raise argparse.ArgumentTypeError(
            r"must be a Windows named pipe such as \\.\pipe\FlowShiftOverlay-<id>"
        )
    return value


def _auth_key(value):
    try:
        raw = base64.b64decode(str(value or ""), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be valid base64") from exc
    if len(raw) != 32:
        raise argparse.ArgumentTypeError("must decode to exactly 32 bytes")
    return raw


def _overlay_url(value):
    value = str(value or "").strip()
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a valid HTTP(S) URL") from exc
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise argparse.ArgumentTypeError("must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or port is not None and not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("must not contain credentials or an invalid port")
    hostname = parsed.hostname.lower()
    try:
        loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        loopback = hostname == "localhost"
    if not loopback:
        raise argparse.ArgumentTypeError("must use a loopback host")
    return value


def _positive_pid(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be a positive process ID") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive process ID")
    return parsed


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="FlowShift isolated overlay host")
    parser.add_argument("--pipe", required=True, type=_pipe_address)
    parser.add_argument("--auth-key", required=True, type=_auth_key)
    parser.add_argument("--url", required=True, type=_overlay_url)
    parser.add_argument("--parent-pid", required=True, type=_positive_pid)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--startup-timeout", type=_positive_timeout, default=10.0)
    return parser.parse_args(argv)


class _Bridge:
    """The complete JavaScript-to-Python surface exposed to the overlay."""

    def __init__(self, host):
        self._host = host

    def hide_overlay(self):
        return self._host.bridge_hide()

    def overlay_event(self, event, data=None):
        return self._host.bridge_event(event, data)


class OverlayHost:
    def __init__(self, args, webview=None):
        self.args = args
        self.webview = webview
        self.listener = None
        self.connection = None
        self.window = None
        self.stop_event = threading.Event()
        self.ui_ready = threading.Event()
        self.ui_queue = queue.Queue(maxsize=UI_QUEUE_SIZE)
        self.send_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.visible = False
        self.mode = None
        self._invalid_lock = threading.Lock()
        self._invalid_last = 0.0
        self._invalid_suppressed = 0
        self._ipc_thread = None
        self._ui_thread = None
        self._parent_thread = None

    def _log_invalid(self, text):
        now = time.monotonic()
        with self._invalid_lock:
            if now - self._invalid_last < INVALID_LOG_INTERVAL:
                self._invalid_suppressed += 1
                return
            suffix = (f" ({self._invalid_suppressed} similar messages suppressed)"
                      if self._invalid_suppressed else "")
            self._invalid_last = now
            self._invalid_suppressed = 0
        print(f"overlay host invalid IPC: {text}{suffix}", file=sys.stderr, flush=True)

    def _send(self, message_type, payload=None, request_id=None):
        connection = self.connection
        if connection is None:
            return False
        with self.send_lock:
            protocol.send_message(connection, message_type, payload, request_id)
        return True

    def _send_error(self, exc, request_id=None):
        if isinstance(exc, protocol.OverlayProtocolError):
            code = exc.code
            text = exc.message
            request_id = exc.request_id or request_id
        else:
            code = "host_error"
            text = str(exc).strip() or type(exc).__name__
        try:
            self._send("error", {"code": code, "message": text}, request_id)
        except protocol.OverlayProtocolError:
            self.stop_event.set()

    def _placement(self, payload):
        area = geometry.get_monitor_work_area(payload["x"], payload["y"])
        return geometry.clamp_overlay_to_work_area(
            payload["x"], payload["y"], OVERLAY_WIDTH_CSS,
            OVERLAY_HEIGHT_CSS, area,
        )

    @staticmethod
    def _visible_payload(mode, placement):
        return {
            "mode": mode,
            "x": placement.x,
            "y": placement.y,
            "width": placement.width,
            "height": placement.height,
            "dpi": placement.dpi,
        }

    def _show(self, payload):
        if payload["target"]["kind"] != "local":
            raise protocol.OverlayProtocolError(
                "unsupported_target", "remote overlays are not supported locally"
            )
        placement = self._placement(payload)
        if not self.args.headless:
            update_payload = {
                "mode": payload["mode"],
                "target": payload["target"],
                "x": placement.x,
                "y": placement.y,
                "dpi": placement.dpi,
                "scale": placement.scale,
                "data": payload["data"],
            }
            update = json.dumps(update_payload, ensure_ascii=True, allow_nan=False,
                                 separators=(",", ":"))
            self._position_window(placement)
            self.window.evaluate_js(
                "if (window.flowshiftOverlay && "
                "typeof window.flowshiftOverlay.update === 'function') {"
                f"window.flowshiftOverlay.update({update});"
                "}"
            )
            self.window.show()
        with self.state_lock:
            self.visible = True
            self.mode = payload["mode"]
        return self._visible_payload(payload["mode"], placement)

    def _position_window(self, placement):
        """Apply a physical-pixel rectangle after Per-Monitor-V2 setup."""
        try:
            from ctypes import wintypes

            handle = int(self.window.native.Handle.ToInt64())
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.SetWindowPos.argtypes = [
                wintypes.HWND, wintypes.HWND,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                wintypes.UINT,
            ]
            user32.SetWindowPos.restype = wintypes.BOOL
            if not user32.SetWindowPos(
                    wintypes.HWND(handle), wintypes.HWND(-1), placement.x, placement.y,
                    placement.width, placement.height, 0x0200):
                raise ctypes.WinError(ctypes.get_last_error())
        except (AttributeError, TypeError, ValueError):
            self.window.resize(placement.width, placement.height)
            self.window.move(placement.x, placement.y)

    def _verify_react_ready(self, *_args):
        deadline = time.monotonic() + self.args.startup_timeout
        while not self.stop_event.is_set() and time.monotonic() < deadline:
            try:
                ready = self.window.evaluate_js(
                    "Boolean(window.flowshiftOverlay && "
                    "typeof window.flowshiftOverlay.update === 'function')"
                )
                if ready:
                    self.ui_ready.set()
                    return
            except Exception:
                pass
            time.sleep(0.05)

    def _on_loaded(self, *_args):
        threading.Thread(
            target=self._verify_react_ready,
            name="overlay-react-ready",
            daemon=True,
        ).start()

    def _watch_parent(self):
        synchronize = 0x00100000
        wait_object_0 = 0
        handle = ctypes.windll.kernel32.OpenProcess(
            synchronize, False, self.args.parent_pid,
        )
        if not handle:
            return
        try:
            result = ctypes.windll.kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
        if result != wait_object_0 or self.stop_event.is_set():
            return
        self.stop_event.set()
        if self.connection is not None:
            try:
                self.connection.close()
            except OSError:
                pass
        if not self.args.headless and self.window is not None:
            try:
                self._run_ui("destroy", timeout=1.0)
            except Exception:
                pass

    def _hide(self):
        if not self.args.headless and self.window is not None:
            self.window.hide()
        with self.state_lock:
            self.visible = False
            self.mode = None

    def _ui_worker(self):
        while True:
            command, payload, done = self.ui_queue.get()
            try:
                if command == "show":
                    done["result"] = self._show(payload)
                elif command == "hide":
                    self._hide()
                elif command == "destroy":
                    self._hide()
                    if self.window is not None:
                        self.window.destroy()
                elif command == "stop":
                    pass
                else:
                    raise ValueError(f"unknown UI command: {command}")
            except Exception as exc:
                done["error"] = exc
            finally:
                done["event"].set()
                self.ui_queue.task_done()
            if command in ("destroy", "stop"):
                return

    def _run_ui(self, command, payload=None, timeout=None):
        if self.args.headless:
            if command == "show":
                return self._show(payload)
            if command in ("hide", "destroy"):
                self._hide()
                return None
        done = {"event": threading.Event(), "result": None, "error": None}
        try:
            self.ui_queue.put((command, payload, done), timeout=0.25)
        except queue.Full as exc:
            raise protocol.OverlayProtocolError("ui_busy", "overlay UI queue is full") from exc
        if not done["event"].wait(timeout or self.args.startup_timeout):
            raise protocol.OverlayProtocolError("ui_timeout", "overlay UI command timed out")
        if done["error"] is not None:
            raise done["error"]
        return done["result"]

    def _handle_message(self, message):
        message_type = message["type"]
        request_id = message["request_id"]
        if message_type == "hello":
            if message["payload"]["role"] != "runtime":
                raise protocol.OverlayProtocolError(
                    "invalid_handshake", "hello must identify the runtime", request_id
                )
            if not self.ui_ready.wait(self.args.startup_timeout):
                raise protocol.OverlayProtocolError(
                    "startup_timeout", "overlay UI did not become ready", request_id
                )
            self._send("ready", {"pid": os.getpid()}, request_id)
        elif message_type == "ping":
            self._send("pong", {}, request_id)
        elif message_type == "show_overlay":
            result = self._run_ui("show", message["payload"])
            self._send("overlay_visible", result, request_id)
        elif message_type == "hide_overlay":
            self._run_ui("hide")
            self._send("overlay_hidden", {}, request_id)
        elif message_type == "shutdown":
            self._send("shutdown", {}, request_id)
            if not self.args.headless:
                try:
                    self._run_ui("destroy", timeout=2.0)
                except Exception:
                    pass
            self.stop_event.set()
        else:
            raise protocol.OverlayProtocolError(
                "unexpected_message", f"host cannot accept {message_type}", request_id
            )

    def _ipc_loop(self):
        try:
            self.connection = self.listener.accept()
            self._send("hello", {"role": "host", "pid": os.getpid()})
            while not self.stop_event.is_set():
                try:
                    message = protocol.receive_message(self.connection)
                except protocol.OverlayProtocolError as exc:
                    if exc.code == "transport_error":
                        break
                    self._log_invalid(f"{exc.code}: {exc.message}")
                    self._send_error(exc)
                    continue
                try:
                    self._handle_message(message)
                except Exception as exc:
                    self._log_invalid(str(exc))
                    self._send_error(exc, message.get("request_id"))
        except (OSError, EOFError, protocol.OverlayProtocolError) as exc:
            if not self.stop_event.is_set():
                self._log_invalid(str(exc))
        finally:
            if (not self.args.headless and self.window is not None
                    and self._ui_thread is not None and self._ui_thread.is_alive()):
                try:
                    self._run_ui("destroy", timeout=1.0)
                except Exception:
                    pass
            self.stop_event.set()

    def bridge_hide(self):
        if self.stop_event.is_set():
            return False
        if self.args.headless:
            self._hide()
        else:
            done = {"event": threading.Event(), "result": None, "error": None}
            try:
                self.ui_queue.put_nowait(("hide", None, done))
            except queue.Full:
                return False
            if not done["event"].wait(1.0) or done["error"] is not None:
                return False
        try:
            self._send("overlay_hidden", {})
        except protocol.OverlayProtocolError:
            return False
        return True

    def bridge_event(self, event, data=None):
        if not isinstance(event, str) or not event.strip() or not isinstance(data or {}, dict):
            return False
        try:
            self._send("overlay_event", {"event": event.strip(), "data": data or {}})
        except protocol.OverlayProtocolError:
            return False
        return True

    def run(self):
        self.listener = Listener(
            self.args.pipe, family="AF_PIPE", authkey=self.args.auth_key,
        )
        try:
            self._parent_thread = threading.Thread(
                target=self._watch_parent, name="overlay-parent-watch", daemon=True,
            )
            self._parent_thread.start()
            if self.args.headless:
                self.ui_ready.set()
                self._ipc_loop()
                return 0

            self.window = self.webview.create_window(
                "FlowShift Overlay",
                self.args.url,
                width=OVERLAY_WIDTH_CSS,
                height=OVERLAY_HEIGHT_CSS,
                resizable=False,
                frameless=True,
                on_top=True,
                background_color="#111827",
                js_api=_Bridge(self),
                hidden=True,
            )
            self.window.events.loaded += self._on_loaded
            self._ui_thread = threading.Thread(
                target=self._ui_worker, name="overlay-host-ui", daemon=True,
            )
            self._ipc_thread = threading.Thread(
                target=self._ipc_loop, name="overlay-host-ipc", daemon=True,
            )
            self._ui_thread.start()
            self._ipc_thread.start()
            self.webview.start(gui="edgechromium", debug=False)
            self.stop_event.set()
            if self._ui_thread.is_alive():
                done = {"event": threading.Event(), "result": None, "error": None}
                try:
                    self.ui_queue.put_nowait(("stop", None, done))
                except queue.Full:
                    pass
            self._ipc_thread.join(timeout=2.0)
            self._ui_thread.join(timeout=2.0)
            return 0
        finally:
            self.stop_event.set()
            if self.connection is not None:
                try:
                    self.connection.close()
                except OSError:
                    pass
            if self.listener is not None:
                try:
                    self.listener.close()
                except OSError:
                    pass


def main(argv=None):
    args = parse_args(argv)
    if sys.platform != "win32":
        print("overlay host requires Windows named pipes", file=sys.stderr)
        return 2

    webview = None
    if not args.headless:
        geometry.set_per_monitor_v2_awareness()
        try:
            import webview as imported_webview
        except ImportError as exc:
            print(
                f"overlay host requires pywebview in normal mode: {exc}",
                file=sys.stderr,
                flush=True,
            )
            return 3
        webview = imported_webview

    try:
        return OverlayHost(args, webview=webview).run()
    except (OSError, ValueError) as exc:
        print(f"overlay host failed: {exc}", file=sys.stderr, flush=True)
        return 4


if __name__ == "__main__":
    sys.exit(main())
