"""Remote preview, stages 3 and 4: picture transfer between paired PCs.

Viktor 27.11.: "den Videostream hinbekommen". Stage 1 (``remote_preview``)
has the settings, the request and the lifecycle, ``preview_capture`` takes and
encodes one picture. This module carries the pictures over the existing peer
connection (the same framed JSON link as input and clipboard, no new port):

- ``preview_frame``: one JPEG picture, cut into parts of at most
  ``CHUNK_BYTES`` so input and other messages of the link can go between the
  parts (each part is sent on its own under the link's send lock).
- ``preview_ack``: the receiver has the complete picture ``seq``.
- ``preview_status``: the sender cannot take a picture right now (screen
  locked, capture failed) or can again (empty reason).

Flow control (``FlowControl``): at most ``WINDOW`` pictures may be on the way
without an ack. While the window is full the sender takes no new picture, so
a slow line skips pictures instead of filling up the connection; input to the
target travels the other way and never waits behind a picture. A picture is
only sent when it differs from the last one sent (``preview_capture`` compares
the pixels before encoding).

Pure module (standard library only), unit-testable on any OS.
"""
from __future__ import annotations

import base64
import binascii
import socket
import threading
import time
import zlib

import remote_preview as rprev

PREVIEW_FRAME = "preview_frame"
PREVIEW_ACK = "preview_ack"
PREVIEW_STATUS = "preview_status"
MESSAGE_TYPES = (PREVIEW_FRAME, PREVIEW_ACK, PREVIEW_STATUS)

CHUNK_BYTES = 48 * 1024
MAX_FRAME_BYTES = 4 * 1024 * 1024
MAX_PARTS = -(-MAX_FRAME_BYTES // CHUNK_BYTES)
MAX_SEQ = 2 ** 53
MAX_SIDE_PX = 8192
WINDOW = 2
STALL_SECONDS = 10.0
CAPTURE_RETRY_SECONDS = 1.0

# preview_status reasons (besides "" = pictures come again).
REASON_SCREEN_LOCKED = "screen_locked"
REASON_CAPTURE_FAILED = "capture_failed"
REASON_STALLED = "stalled"
REASON_SEND_FAILED = "send_failed"

STATUS_TEXTS = {
    REASON_SCREEN_LOCKED: "the screen of the other PC is locked (sign-in or lock screen), "
                          "Windows allows no picture of it",
    REASON_CAPTURE_FAILED: "the other PC cannot take a picture of its screen right now",
}


def status_text(reason):
    return STATUS_TEXTS.get(reason, rprev.reason_text(reason))


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _int_in(value, low, high):
    return _is_int(value) and low <= value <= high


# ── Messages ────────────────────────────────────────────────────────
def frame_messages(session_id, seq, jpeg, width, height, chunk_bytes=CHUNK_BYTES):
    """All ``preview_frame`` parts of one picture, in sending order."""
    if not jpeg or len(jpeg) > MAX_FRAME_BYTES:
        raise ValueError(f"picture size {len(jpeg or b'')} outside 1..{MAX_FRAME_BYTES} bytes")
    crc = zlib.crc32(jpeg) & 0xFFFFFFFF
    parts = -(-len(jpeg) // chunk_bytes)
    return [{
        "type": PREVIEW_FRAME, "session_id": session_id, "seq": seq,
        "part": index, "parts": parts, "size": len(jpeg), "crc": crc,
        "width": width, "height": height,
        "data": base64.b64encode(jpeg[index * chunk_bytes:(index + 1) * chunk_bytes]).decode("ascii"),
    } for index in range(parts)]


def build_ack(session_id, seq):
    return {"type": PREVIEW_ACK, "session_id": session_id, "seq": seq}


def build_status(session_id, reason=""):
    return {"type": PREVIEW_STATUS, "session_id": session_id, "reason": reason}


def build_accept(session_id, settings):
    settings = rprev.normalize_settings(settings)
    return {"type": rprev.PREVIEW_ACCEPT, "session_id": session_id,
            "fps": settings["fps"], "max_width": settings["max_width"]}


def parse_stream_message(msg):
    """Validate one ``preview_frame`` / ``preview_ack`` / ``preview_status``.

    Returns ``(True, fields)`` or ``(False, reason)``; never raises. A frame's
    ``data`` comes back decoded as ``bytes``.
    """
    if not isinstance(msg, dict) or msg.get("type") not in MESSAGE_TYPES:
        return False, rprev.REASON_INVALID_MESSAGE
    kind = msg["type"]
    session_id = msg.get("session_id")
    if not rprev.valid_session_id(session_id):
        return False, rprev.REASON_INVALID_MESSAGE
    fields = {"type": kind, "session_id": session_id}
    if kind in (PREVIEW_FRAME, PREVIEW_ACK):
        if not _int_in(msg.get("seq"), 1, MAX_SEQ):
            return False, rprev.REASON_INVALID_MESSAGE
        fields["seq"] = msg["seq"]
    if kind == PREVIEW_STATUS:
        reason = msg.get("reason", "")
        if reason != "" and not (isinstance(reason, str) and rprev._REASON_RE.match(reason)):
            return False, rprev.REASON_INVALID_MESSAGE
        fields["reason"] = reason
    if kind == PREVIEW_FRAME:
        size = msg.get("size")
        parts = msg.get("parts")
        if not _int_in(size, 1, MAX_FRAME_BYTES) or not _int_in(parts, 1, MAX_PARTS):
            return False, rprev.REASON_INVALID_MESSAGE
        if not _int_in(msg.get("part"), 0, parts - 1) or not _int_in(msg.get("crc"), 0, 0xFFFFFFFF):
            return False, rprev.REASON_INVALID_MESSAGE
        if not _int_in(msg.get("width"), 1, MAX_SIDE_PX) or not _int_in(msg.get("height"), 1, MAX_SIDE_PX):
            return False, rprev.REASON_INVALID_MESSAGE
        data = msg.get("data")
        # base64 of one part: at most 4/3 of CHUNK_BYTES (checked before decoding).
        if not isinstance(data, str) or not data or len(data) > (CHUNK_BYTES * 4) // 3 + 4:
            return False, rprev.REASON_INVALID_MESSAGE
        try:
            raw = base64.b64decode(data.encode("ascii"), validate=True)
        except (binascii.Error, ValueError, UnicodeEncodeError):
            return False, rprev.REASON_INVALID_MESSAGE
        if not raw or len(raw) > CHUNK_BYTES:
            return False, rprev.REASON_INVALID_MESSAGE
        fields.update(part=msg["part"], parts=parts, size=size, crc=msg["crc"],
                      width=msg["width"], height=msg["height"], data=raw)
    return True, fields


# ── Receiver: put the parts of a picture together ──────────────────
class FrameAssembler:
    """Collect the parts of the newest picture; older or broken pictures are dropped.

    The link is one TCP connection, so the parts of a picture arrive in order.
    A part out of order, a part of an older picture, a size or checksum that
    does not match each throw the unfinished picture away (``dropped``).
    """

    def __init__(self):
        self.seq = None
        self.last_done = 0
        self.dropped = 0
        self._parts = []
        self._meta = None

    def reset(self):
        if self._parts:
            self.dropped += 1
        self.seq, self._parts, self._meta = None, [], None

    def add(self, fields):
        """Add one parsed part; returns the complete picture or ``None``."""
        seq = fields["seq"]
        if seq <= self.last_done:
            self.dropped += 1
            return None
        meta = (fields["parts"], fields["size"], fields["crc"], fields["width"], fields["height"])
        if seq != self.seq:
            self.reset()
            if fields["part"] != 0:
                self.dropped += 1
                return None
            self.seq, self._meta = seq, meta
        elif meta != self._meta or fields["part"] != len(self._parts):
            self.reset()
            self.dropped += 1
            return None
        self._parts.append(fields["data"])
        if sum(len(p) for p in self._parts) > fields["size"]:
            self.reset()
            return None
        if len(self._parts) < fields["parts"]:
            return None
        jpeg = b"".join(self._parts)
        self.seq, self._parts, self._meta = None, [], None
        if len(jpeg) != fields["size"] or (zlib.crc32(jpeg) & 0xFFFFFFFF) != fields["crc"]:
            self.dropped += 1
            return None
        self.last_done = seq
        return {"seq": seq, "jpeg": jpeg, "width": fields["width"], "height": fields["height"]}


# ── Sender: flow control ────────────────────────────────────────────
class FlowControl:
    """At most ``window`` pictures without ack; a picture without ack for
    ``stall_seconds`` means the other side no longer reads (``stalled``)."""

    def __init__(self, window=WINDOW, stall_seconds=STALL_SECONDS, clock=time.monotonic):
        self.window = max(1, int(window))
        self.stall_seconds = float(stall_seconds)
        self.clock = clock
        self._lock = threading.Lock()
        self._in_flight = {}   # seq -> sent at

    def can_send(self):
        with self._lock:
            return len(self._in_flight) < self.window

    def sent(self, seq):
        with self._lock:
            self._in_flight[seq] = self.clock()

    def acked(self, seq):
        """Ack ``seq`` and everything before it; ``False`` for an unknown seq."""
        with self._lock:
            if seq not in self._in_flight:
                return False
            for key in [k for k in self._in_flight if k <= seq]:
                del self._in_flight[key]
            return True

    def in_flight(self):
        with self._lock:
            return len(self._in_flight)

    def stalled(self):
        with self._lock:
            if not self._in_flight:
                return False
            return self.clock() - min(self._in_flight.values()) > self.stall_seconds


# ── Sender: the streaming loop on the target ───────────────────────
class PreviewStreamer:
    """Take, compare and send pictures for one preview session.

    ``capture(previous_key)`` returns ``None`` when the screen did not change
    since ``previous_key``, else ``{"key", "jpeg", "width", "height"}``, or
    raises an exception with a ``code`` (``preview_capture.CaptureError``).
    ``send(msg)`` sends one message on the link (raises when the link is gone).
    ``on_end(reason)`` is called once when the streamer ends by itself.
    ``step()`` is one tick of the loop, so tests can drive it without a thread.
    """

    def __init__(self, session_id, fps, capture, send, log=None, on_end=None,
                 window=WINDOW, stall_seconds=STALL_SECONDS, clock=time.monotonic):
        self.session_id = session_id
        self.fps = max(rprev.FPS_MIN, min(int(fps), rprev.FPS_MAX))
        self.capture = capture
        self.send = send
        self.log = log or (lambda level, text: None)
        self.on_end = on_end
        self.clock = clock
        self.flow = FlowControl(window, stall_seconds, clock)
        self.seq = 0
        self.last_key = None
        self.problem = ""          # reason sent with preview_status, "" = fine
        self.retry_at = 0.0
        self.stats = {"sent": 0, "bytes": 0, "skipped_same": 0, "skipped_slow": 0,
                      "capture_errors": 0}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self.ended = ""

    # -- control ---------------------------------------------------------
    def start(self, thread_setup=None):
        def run():
            if thread_setup:
                try:
                    thread_setup()
                except Exception:
                    pass
            self._run()
        self._thread = threading.Thread(target=run, name="preview-streamer", daemon=True)
        self._thread.start()

    def stop(self, timeout=2.0):
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout)

    @property
    def stopped(self):
        return self._stop.is_set()

    def on_ack(self, seq):
        if self.flow.acked(seq):
            self._wake.set()
            return True
        return False

    def snapshot(self):
        return dict(self.stats, seq=self.seq, in_flight=self.flow.in_flight(),
                    problem=self.problem, fps=self.fps)

    # -- loop ------------------------------------------------------------
    def _end(self, reason):
        if self.ended:
            return
        self.ended = reason
        self._stop.set()
        if self.on_end:
            try:
                self.on_end(reason)
            except Exception:
                pass

    def _status(self, reason):
        if reason == self.problem:
            return
        self.problem = reason
        self.send(build_status(self.session_id, reason))
        if reason:
            self.log("INFO", f"remote_preview sending paused session={self.session_id}: "
                             f"{status_text(reason)}")
        else:
            self.log("INFO", f"remote_preview sending again session={self.session_id}")

    def step(self):
        """One tick. Returns what happened: sent, same, slow, error, wait, stalled, stopped."""
        if self._stop.is_set():
            return "stopped"
        now = self.clock()
        if self.flow.stalled():
            self.log("WARN", f"remote_preview stopped session={self.session_id}: no answer for "
                             f"{self.flow.stall_seconds:g} s (the other PC no longer reads pictures)")
            self._end(REASON_STALLED)
            return "stalled"
        if now < self.retry_at:
            return "wait"
        if not self.flow.can_send():
            # Slow line: no new picture until an older one arrived.
            self.stats["skipped_slow"] += 1
            return "slow"
        try:
            frame = self.capture(self.last_key)
        except Exception as exc:
            self.stats["capture_errors"] += 1
            code = getattr(exc, "code", "")
            reason = REASON_SCREEN_LOCKED if code == REASON_SCREEN_LOCKED else REASON_CAPTURE_FAILED
            if reason != self.problem:
                self.log("WARN", f"remote_preview capture failed session={self.session_id}: {exc}")
            self.retry_at = now + CAPTURE_RETRY_SECONDS
            try:
                self._status(reason)
            except Exception as send_exc:
                self._end(REASON_SEND_FAILED)
                self.log("INFO", f"remote_preview link gone session={self.session_id}: {send_exc!r}")
                return "stopped"
            return "error"
        try:
            if self.problem:
                self._status("")
            if frame is None:
                self.stats["skipped_same"] += 1
                return "same"
            self.seq += 1
            messages = frame_messages(self.session_id, self.seq, frame["jpeg"],
                                      frame["width"], frame["height"])
            self.flow.sent(self.seq)
            for msg in messages:
                if self._stop.is_set():
                    return "stopped"   # abort between parts; the receiver drops the rest
                self.send(msg)
        except Exception as exc:
            self._end(REASON_SEND_FAILED)
            self.log("INFO", f"remote_preview link gone session={self.session_id}: {exc!r}")
            return "stopped"
        self.last_key = frame["key"]
        self.stats["sent"] += 1
        self.stats["bytes"] += len(frame["jpeg"])
        return "sent"

    def _run(self):
        interval = 1.0 / self.fps
        self.log("INFO", f"remote_preview sending started session={self.session_id} fps={self.fps}")
        next_at = self.clock()
        while not self._stop.is_set():
            result = self.step()
            if result in ("stopped", "stalled"):
                break
            next_at = max(next_at + interval, self.clock())
            self._wake.clear()
            if result == "slow":
                # Wait for an ack (or the stall timeout), not a fixed tick.
                self._wake.wait(min(1.0, self.flow.stall_seconds))
            else:
                self._wake.wait(max(0.0, next_at - self.clock()))
        s = self.stats
        self.log("INFO", f"remote_preview sending ended session={self.session_id} "
                         f"reason={self.ended or 'stopped'} sent={s['sent']} kb={s['bytes'] // 1024} "
                         f"unchanged={s['skipped_same']} skipped_slow={s['skipped_slow']} "
                         f"capture_errors={s['capture_errors']}")


# ── Receiver statistics ─────────────────────────────────────────────
class ReceiveStats:
    """Pictures per second and kilobytes per second over the last few seconds."""

    def __init__(self, span=3.0, clock=time.monotonic):
        self.span = span
        self.clock = clock
        self.frames = 0
        self.bytes = 0
        self.last_at = None
        self._recent = []

    def add(self, size):
        now = self.clock()
        self.frames += 1
        self.bytes += size
        self.last_at = now
        self._recent.append((now, size))
        self._recent = [(t, s) for t, s in self._recent if now - t <= self.span]

    def snapshot(self):
        now = self.clock()
        recent = [(t, s) for t, s in self._recent if now - t <= self.span]
        fps = len(recent) / self.span
        kbps = sum(s for _t, s in recent) / 1024 / self.span
        return {"frames": self.frames, "kb": self.bytes // 1024, "fps": round(fps, 1),
                "kb_per_s": round(kbps, 1),
                "last_frame_s": None if self.last_at is None else round(now - self.last_at, 1)}


# ── Self-test: one picture through the transfer code ───────────────
def loopback_transfer(jpeg, width, height, send_msg, recv_msg, clock=time.perf_counter,
                      timeout=5.0):
    """Send ``jpeg`` over a local socket pair with the link framing and put it together again.

    ``send_msg`` / ``recv_msg`` are the link's framing (``runtime_model``).
    Returns ``(ok, detail, ms)``; never raises.
    """
    started = clock()
    left = right = None
    try:
        left, right = socket.socketpair()
        right.settimeout(timeout)
        messages = frame_messages("selftest", 1, jpeg, width, height)
        errors = []

        def writer():
            try:
                for msg in messages:
                    send_msg(left, msg)
                send_msg(left, build_ack("selftest", 1))
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=writer, name="preview-selftest-send", daemon=True)
        thread.start()
        assembler = FrameAssembler()
        done = None
        while done is None:
            ok, fields = parse_stream_message(recv_msg(right))
            if not ok:
                return False, f"a part was not readable ({fields})", 0.0
            if fields["type"] == PREVIEW_ACK:
                break
            done = assembler.add(fields)
        thread.join(timeout)
        if errors:
            return False, f"sending failed: {errors[0]!r}", 0.0
        if done is None or done["jpeg"] != jpeg:
            return False, "the picture arrived incomplete or changed", 0.0
        ms = round((clock() - started) * 1000, 1)
        return True, (f"{len(jpeg) // 1024} KB in {len(messages)} part(s) sent and put together "
                      f"again in {ms:.0f} ms, checksum matches"), ms
    except Exception as exc:
        return False, f"{exc.__class__.__name__}: {str(exc)[:160]}", 0.0
    finally:
        for sock in (left, right):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
