"""Updates from a connected FlowShift (Viktor 25.11.).

When two FlowShift PCs are connected, both see the other's version in the
hello. The PC with the higher version offers its installed program as a
package; the PC with the lower version decides with its own setting (Settings
> Software Update > "Updates from connected devices", the same three levels as
the normal update):

- ``notify`` (default): it only shows the offer; nothing is transferred before
  the user clicks Download or Install.
- ``download``: it fetches and checks the package, the user clicks Install.
- ``install``: it fetches, checks and installs the package and restarts
  FlowShift as soon as no transfer, clipboard action or forwarding runs.

Safety rules (the receiver checks everything, the sender is never trusted):

- Only paired devices: the offer must come over the link of a device that is
  in this PC's own device list (``peers`` with its device ID), and the package
  is fetched by dialing the address from that list, never the address the
  offer came from. Offers of unknown devices are ignored and logged.
- Only newer versions: the package version must be higher than the installed
  one (no downgrade, no same version, no build metadata). A development
  version (``X.Y.Z-dev.N``) is only accepted on the development channel or
  when the user ticked "also development versions" (``allow_development``).
- Complete and unchanged: the transfer resumes where it broke off, the whole
  file must have the size and SHA-256 named in the offer, and every file in
  the ZIP must be listed in ``peer_package.json`` with its size and SHA-256
  (nothing missing, nothing extra, no unsafe paths, the package's VERSION and
  WebGUI stamp equal the offered version, all files the installer needs).
- Same way as every other update: the checked package goes to the external
  updater (``update_handoff.handoff_peer_package``) with the shared install
  lock, rollback copy, health check and rollback. No new port: the package
  travels over a second connection to the existing FlowShift port, like the
  clipboard data channel; no rights beyond the existing update.

The checksums show the package arrived complete and unchanged on the way.
They do not prove who built it: a device in the same network that pretends to
be a paired PC (same address and device ID) could offer its own package until
secured pairing exists (Kanban card 722). That is why the default is
``notify``.

Course of an update as seen from the sending PC (Viktor 25.11. 17:34):

- The versions are compared as soon as both PCs see each other (hello), not
  only at the screen edge. A PC that just updated itself offers its new
  version to every older peer once the link is back.
- The receiver reports every step back over the link (``peer_update_status``:
  transfer percent, waiting and why, installing, restarting, failed with the
  reason). ``PeerUpdateMonitor`` turns that, together with the version in the
  next hello, into one state per peer for the Dashboard and short tray
  messages when the update starts and when the peer is back.
- The receiver never restarts while it is being controlled (forwarding or an
  edge session from the other PC): the install waits until the user is back on
  his own PC. A switch towards a peer that is installing or restarting is
  refused with "<PC> is being updated" instead of failing silently; switching
  works again as soon as the peer is connected again.
- After the restart the peer dials in again by itself (normal reconnect). A
  failed install is rolled back by the updater; the peer reports the reason
  after the restart. After ``MAX_AUTOMATIC_ATTEMPTS`` failed installs of the
  same version automatic attempts pause (a click on Install still works).
- The update messages (offer, status, package channel) never change
  incompatibly, so PCs with different link protocols still exchange the hello
  and the update; only forwarding waits until both speak the same protocol
  (``link_mode``).
"""
from __future__ import annotations

import base64
import binascii
import datetime as _dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import threading
import time
import uuid
import zipfile

import socket

import config_schema
from runtime_model import FramedReader, send_msg
from version import parse_semver

PACKAGE_FORMAT = 1
MANIFEST_NAME = "peer_package.json"
OUTBOX_DIR = "peer_outbox"
STATE_NAME = "peer_update.json"

POLICY_NOTIFY = "notify"
POLICY_DOWNLOAD = "download"
POLICY_INSTALL = "install"

CHANNEL_DEVELOPMENT = "development"

# Wire messages. The offer travels over the normal peer link (older FlowShift
# versions ignore unknown types); the package over its own short connection.
T_OFFER = "peer_update_offer"
T_CHANNEL_HELLO = "peer_update_channel_hello"
T_CHANNEL_ACK = "peer_update_channel_ack"
T_CHUNK = "peer_update_chunk"
T_DONE = "peer_update_done"
# Receiver -> sender: what happens with the offered update (display only).
T_STATUS = "peer_update_status"

# Version of the update messages above, sent in the hello. They only ever get
# new optional fields, so every FlowShift with peer updates understands every
# other one, whatever its link protocol is.
UPDATE_PROTOCOL = 1
LINK_FULL = "full"
LINK_UPDATE_ONLY = "update_only"

CHUNK_SIZE = 512 * 1024
MAX_PACKAGE_SIZE = 256 * 1024 * 1024
MAX_UNPACKED_SIZE = 768 * 1024 * 1024
MAX_FILES = 20000
MAX_VERSION_LENGTH = 64
MAX_NAME_LENGTH = 128
CHANNEL_TIMEOUT_SECONDS = 30.0
DOWNLOAD_ATTEMPTS = 3
RETRY_PAUSE_SECONDS = 3.0
# Before an automatic install the user sees for this long what will happen
# and can cancel (tray message and Software Update).
ANNOUNCE_SECONDS = 60.0
SAFETY_POLL_SECONDS = 5.0
HISTORY_LIMIT = 20
# Failures after which a new offer of the same package starts the automatic
# download again (the other PC was away); a damaged or refused package is
# never fetched again by itself.
TRANSIENT_ERRORS = frozenset({"unreachable", "interrupted", "no_answer", "transfer_failed"})
# Failed installs of the same version after which automatic installs pause
# (no update loop); the user can still click Install, a newer version starts
# counting again.
MAX_AUTOMATIC_ATTEMPTS = 2
# How long the sender waits for a restarting peer before it reports that the
# peer did not come back.
RESTART_TIMEOUT_SECONDS = 300.0
# Rough time the link is gone while the peer installs and restarts (shown on
# the sending PC).
RESTART_ESTIMATE_SECONDS = 90
# A record of the last handoff older than this is not reported again.
REPORT_MAX_AGE_SECONDS = 24 * 3600
MAX_MESSAGE_LENGTH = 300

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DEVICE_ID = re.compile(r"^[0-9a-z][0-9a-z_-]{0,63}$")

IDLE = "idle"
AVAILABLE = "available"
DOWNLOADING = "downloading"
DOWNLOADED = "downloaded"
WAITING = "waiting"
INSTALLING = "installing"
FAILED = "failed"


class PeerPackageError(Exception):
    """A plain reason why a package was not built, fetched or accepted."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def package_file_name(version):
    return f"FlowShift-Peer-{version}.zip"


def top_folder(version):
    return f"FlowShift-{version}-peer"


def _now_text():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical(value):
    """Parsed version when ``value`` is a plain, canonical SemVer string."""
    if not isinstance(value, str) or not value or len(value) > MAX_VERSION_LENGTH:
        return None
    try:
        parsed = parse_semver(value)
    except (TypeError, ValueError):
        return None
    if str(parsed) != value or parsed.build:
        return None
    return parsed


def normalize_device_id(value):
    text = str(value or "").strip().lower()
    return text if _DEVICE_ID.match(text) else ""


# ── decisions ───────────────────────────────────────────────────────────────

def should_offer(local_version, remote_version):
    """True when this PC has the higher version and should offer its package."""
    local, remote = _canonical(local_version), _canonical(remote_version)
    return local is not None and remote is not None and local > remote


def accept_decision(installed_version, offered_version, channel, allow_development):
    """``(True, None)`` when the offered version may be installed here, else
    ``(False, reason)`` with a stable reason code."""
    installed = _canonical(installed_version)
    offered = _canonical(offered_version)
    if installed is None or offered is None:
        return False, "invalid_version"
    if offered <= installed:
        return False, "not_newer"
    if not offered.is_stable and channel != CHANNEL_DEVELOPMENT and not allow_development:
        return False, "development_version_not_allowed"
    return True, None


def link_mode(local_protocol, remote_protocol):
    """``LINK_FULL`` when both PCs speak the same link protocol, else
    ``LINK_UPDATE_ONLY``: the link stays up for the hello, the update offer,
    the status and the package (those messages never change incompatibly),
    but input is not forwarded until both run the same protocol."""
    try:
        local, remote = int(local_protocol), int(remote_protocol)
    except (TypeError, ValueError):
        return LINK_UPDATE_ONLY
    return LINK_FULL if local == remote and local > 0 else LINK_UPDATE_ONLY


def trusted_peer(peers, identity, own_device_id=""):
    """Configured peer (dict) for a link identity ``device:<id>``, else None.

    Only devices in this PC's own device list with a device ID count as
    paired; an identity by name or address never does.
    """
    if not isinstance(identity, str) or not identity.startswith("device:"):
        return None
    device_id = normalize_device_id(identity[len("device:"):])
    if not device_id or device_id == normalize_device_id(own_device_id):
        return None
    for peer in peers or []:
        if isinstance(peer, dict) and normalize_device_id(peer.get("device_id")) == device_id:
            return peer
    return None


def peer_endpoint(peer):
    """``(host, port)`` from this PC's own device list entry."""
    host = str(peer.get("host") or "").strip()
    try:
        port = int(peer.get("port", 45781))
    except (TypeError, ValueError):
        return None
    if not host or not 0 < port < 65536:
        return None
    return host, port


# ── building the package (sender) ───────────────────────────────────────────

def build_package(runtime_root, output_dir, *, now=None):
    """Build ``FlowShift-Peer-<version>.zip`` of the installed program.

    Same program files and prebuilt WebGUI as the USB package
    (``usb_package.collect_program``), without the offline Python files: the
    receiver already has Python, the installer gets the pinned packages the
    usual way. ``peer_package.json`` lists every file with size and SHA-256.
    """
    import usb_package

    try:
        version, entries = usb_package.collect_program(runtime_root)
    except usb_package.PackageError as exc:
        raise PeerPackageError(exc.code, exc.message) from exc
    files = []
    for source, name in entries:
        files.append({"path": name, "size": Path(source).stat().st_size,
                      "sha256": _sha256_file(source)})
    manifest = {
        "format": PACKAGE_FORMAT,
        "version": version,
        "created_at": (now or _now_text()),
        "files": files,
    }
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    final = output_dir / package_file_name(version)
    partial = output_dir / (package_file_name(version) + f".{uuid.uuid4().hex}.part")
    top = top_folder(version)
    try:
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for source, name in entries:
                archive.write(source, f"{top}/{name}")
            archive.writestr(f"{top}/{MANIFEST_NAME}", json.dumps(manifest, indent=2) + "\n")
        os.replace(partial, final)
    finally:
        try:
            partial.unlink()
        except OSError:
            pass
    return {"path": str(final), "version": version, "size": final.stat().st_size,
            "sha256": _sha256_file(final)}


class PackageCache:
    """The sender's package of the installed version, built once on demand."""

    def __init__(self, runtime_root, outbox_dir, builder=build_package, log=None):
        self._runtime_root = Path(runtime_root)
        self._outbox = Path(outbox_dir)
        self._builder = builder
        self._log = log
        self._lock = threading.Lock()
        self._info = None
        self._failure = None

    def get(self):
        """Package info dict, or None when it cannot be built here."""
        with self._lock:
            if self._info is not None and Path(self._info["path"]).is_file():
                return dict(self._info)
            if self._failure is not None:
                return None
            try:
                info = self._builder(self._runtime_root, self._outbox)
            except PeerPackageError as exc:
                self._failure = exc
                if self._log:
                    self._log("INFO", f"peer update: no package to offer ({exc.code}: {exc.message})")
                return None
            except Exception as exc:  # disk full, permissions: offer nothing
                self._failure = PeerPackageError("build_failed", str(exc))
                if self._log:
                    self._log("WARN", f"peer update: package build failed: {exc!r}")
                return None
            self._info = info
            # Only the package of the installed version is kept.
            for old in self._outbox.glob("FlowShift-Peer-*.zip*"):
                if old.name != Path(info["path"]).name:
                    try:
                        old.unlink()
                    except OSError:
                        pass
            if self._log:
                self._log("INFO", f"peer update: package {info['version']} ready "
                                  f"size={info['size']} sha256={info['sha256'][:12]}")
            return dict(info)

    def offer_message(self, device_name=""):
        info = self.get()
        if info is None:
            return None
        return {"type": T_OFFER, "version": info["version"], "size": info["size"],
                "sha256": info["sha256"], "device_name": str(device_name)[:MAX_NAME_LENGTH]}


def validate_offer(msg):
    """Checked offer fields or ``None`` for a malformed offer."""
    if not isinstance(msg, dict) or msg.get("type") != T_OFFER:
        return None
    version = msg.get("version")
    size = msg.get("size")
    sha256 = msg.get("sha256")
    if _canonical(version) is None:
        return None
    if type(size) is not int or not 0 < size <= MAX_PACKAGE_SIZE:
        return None
    if not isinstance(sha256, str) or not _SHA256.match(sha256):
        return None
    return {"version": version, "size": size, "sha256": sha256}


# ── serving the package (sender, one short connection per request) ─────────

def serve_channel(hello, package, send, requester_allowed, own_device_id=""):
    """Answer a package request on its own connection.

    ``hello`` is the first message, ``package`` the PackageCache info (or
    None), ``send(msg)`` writes one framed message and ``requester_allowed``
    tells whether the asking device is in this PC's device list. Returns a
    short result code for the log.
    """
    def refuse(reason):
        send({"type": T_CHANNEL_ACK, "ok": False, "reason": reason})
        return reason

    if not requester_allowed:
        return refuse("not_paired")
    if package is None:
        return refuse("no_package")
    if hello.get("version") != package["version"] or hello.get("sha256") != package["sha256"]:
        return refuse("package_changed")
    offset = hello.get("offset", 0)
    if type(offset) is not int or not 0 <= offset <= package["size"]:
        return refuse("invalid_offset")
    send({"type": T_CHANNEL_ACK, "ok": True, "version": package["version"],
          "size": package["size"], "sha256": package["sha256"], "offset": offset,
          "device_id": normalize_device_id(own_device_id)})
    with open(package["path"], "rb") as handle:
        handle.seek(offset)
        position = offset
        while True:
            block = handle.read(CHUNK_SIZE)
            if not block:
                break
            send({"type": T_CHUNK, "offset": position,
                  "data": base64.b64encode(block).decode("ascii")})
            position += len(block)
    send({"type": T_DONE, "size": position})
    return "sent" if offset == 0 else "resumed"


def serve_socket(conn, hello, package_cache, peers, own_device_id, log=None):
    """Serve one package request that arrived as the first message on ``conn``.

    The asking device must be in this PC's own device list as well; the
    package is built (once) only for such a request.
    """
    requester = normalize_device_id((hello or {}).get("device_id"))
    allowed = bool(requester) and trusted_peer(peers, f"device:{requester}", own_device_id) is not None
    package = package_cache.get() if allowed and package_cache is not None else None
    try:
        conn.settimeout(CHANNEL_TIMEOUT_SECONDS)
        result = serve_channel(hello, package, lambda msg: send_msg(conn, msg), allowed, own_device_id)
    except (OSError, ValueError) as exc:
        result = f"aborted ({type(exc).__name__})"
    if log is not None:
        log("INFO", f"peer update package request from device {requester or '-'}: {result}")
    return result


class SocketChannel:
    """Receiver side of one package connection (framed JSON like every link)."""

    def __init__(self, sock):
        self._sock = sock
        self._reader = FramedReader(sock)

    def read_message(self, timeout):
        return self._reader.read_message(timeout)

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass


def open_socket_channel(endpoint, hello, connect_timeout=10.0):
    """Dial the FlowShift port of a paired device and send the package request."""
    sock = socket.create_connection(endpoint, timeout=connect_timeout)
    try:
        send_msg(sock, hello)
    except BaseException:
        sock.close()
        raise
    return SocketChannel(sock)


# ── fetching the package (receiver) ─────────────────────────────────────────

class TransferCancelled(Exception):
    pass


def download_package(open_channel, offer, own_device_id, downloads_dir, *, progress=None,
                     cancel=None, attempts=DOWNLOAD_ATTEMPTS, pause=RETRY_PAUSE_SECONDS,
                     sleep=time.sleep, timeout=CHANNEL_TIMEOUT_SECONDS):
    """Fetch the offered package into ``downloads_dir``; returns its path.

    ``open_channel(hello)`` returns an object with ``read_message(timeout)``
    and ``close()`` after the hello was sent. A broken transfer continues
    from the bytes already saved in ``<name>.part``; after the last byte the
    whole file must have the offered size and SHA-256, otherwise it is
    deleted. ``cancel`` is a threading.Event; a cancelled transfer keeps no
    file.
    """
    downloads_dir = Path(downloads_dir)
    downloads_dir.mkdir(parents=True, exist_ok=True)
    final = downloads_dir / package_file_name(offer["version"])
    partial = downloads_dir / (final.name + ".part")
    size, sha256 = offer["size"], offer["sha256"]
    last_error = None
    for attempt in range(1, max(1, int(attempts)) + 1):
        if cancel is not None and cancel.is_set():
            break
        try:
            offset = partial.stat().st_size if partial.is_file() else 0
        except OSError:
            offset = 0
        if offset > size:
            partial.unlink()
            offset = 0
        try:
            _fetch_once(open_channel, offer, own_device_id, partial, offset, progress, cancel, timeout)
        except TransferCancelled:
            break
        except PeerPackageError as exc:
            last_error = exc
            if exc.code in {"not_paired", "no_package", "package_changed"}:
                break
            if attempt < attempts and not (cancel is not None and cancel.is_set()):
                sleep(pause)
            continue
        actual_size = partial.stat().st_size
        if actual_size != size or _sha256_file(partial) != sha256:
            partial.unlink()
            last_error = PeerPackageError(
                "checksum_mismatch",
                "The package did not arrive unchanged (size or SHA-256 differs from the offer); "
                "it was deleted.")
            continue
        os.replace(partial, final)
        return final
    if cancel is not None and cancel.is_set():
        try:
            partial.unlink()
        except OSError:
            pass
        raise PeerPackageError("cancelled", "The transfer was cancelled; nothing was kept.")
    raise last_error or PeerPackageError("transfer_failed", "The package could not be fetched.")


def _fetch_once(open_channel, offer, own_device_id, partial, offset, progress, cancel, timeout):
    try:
        _fetch_channel(open_channel, offer, own_device_id, partial, offset, progress, cancel, timeout)
    except (PeerPackageError, TransferCancelled):
        raise
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        # Connection closed or reset, timeout, a broken frame: the bytes saved
        # so far stay in the .part file for the next try.
        raise PeerPackageError("interrupted", f"The transfer broke off: {exc}") from exc


def _fetch_channel(open_channel, offer, own_device_id, partial, offset, progress, cancel, timeout):
    hello = {"type": T_CHANNEL_HELLO, "device_id": own_device_id, "version": offer["version"],
             "sha256": offer["sha256"], "offset": offset}
    try:
        channel = open_channel(hello)
    except PeerPackageError:
        raise
    except Exception as exc:
        raise PeerPackageError("unreachable", f"The other PC could not be reached: {exc}") from exc
    try:
        ack = channel.read_message(timeout)
        if not isinstance(ack, dict) or ack.get("type") != T_CHANNEL_ACK:
            raise PeerPackageError("no_answer", "The other PC did not answer the package request.")
        if ack.get("ok") is not True:
            reason = str(ack.get("reason") or "refused")[:40]
            raise PeerPackageError(reason, f"The other PC refused the package ({reason}).")
        expected = normalize_device_id((offer.get("peer") or {}).get("device_id"))
        if expected and normalize_device_id(ack.get("device_id")) != expected:
            raise PeerPackageError("not_paired", "The address in the device list answers with another device ID.")
        if (ack.get("version") != offer["version"] or ack.get("sha256") != offer["sha256"]
                or ack.get("size") != offer["size"] or ack.get("offset") != offset):
            raise PeerPackageError("package_changed", "The other PC now offers another package.")
        position = offset
        with open(partial, "ab") as handle:
            while True:
                if cancel is not None and cancel.is_set():
                    raise TransferCancelled()
                msg = channel.read_message(timeout)
                if msg is None:
                    raise PeerPackageError("interrupted", "The transfer stopped (no data in time).")
                if msg.get("type") == T_DONE:
                    break
                if msg.get("type") != T_CHUNK or msg.get("offset") != position:
                    raise PeerPackageError("protocol_error", "The other PC sent unexpected data.")
                try:
                    block = base64.b64decode(msg.get("data") or "", validate=True)
                except (binascii.Error, ValueError, TypeError) as exc:
                    raise PeerPackageError("protocol_error", "The other PC sent unreadable data.") from exc
                if not block or position + len(block) > offer["size"]:
                    raise PeerPackageError("protocol_error", "The other PC sent more data than offered.")
                handle.write(block)
                position += len(block)
                if progress is not None:
                    progress(position, offer["size"])
            handle.flush()
            os.fsync(handle.fileno())
        if position != offer["size"]:
            raise PeerPackageError("interrupted", "The transfer ended before the last byte.")
    finally:
        try:
            channel.close()
        except Exception:
            pass


# ── checking the package (receiver, before anything is installed) ──────────

def _safe_member(name, top):
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        return None
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or ":" in name:
        return None
    if not path.parts or path.parts[0] != top:
        return None
    return "/".join(path.parts[1:])


def _required_files(archive, top):
    import usb_package

    try:
        script = archive.read(f"{top}/install_flowshift.ps1").decode("utf-8-sig")
        constants = usb_package.installer_constants(script)
    except (KeyError, UnicodeError, usb_package.PackageError) as exc:
        raise PeerPackageError("incomplete", "The package has no readable install_flowshift.ps1.") from exc
    return [item.replace("\\", "/") for item in constants["required_program_files"]]


def verify_package(path, offer, installed_version, channel, allow_development):
    """Check a fetched package completely; returns the manifest or raises.

    Nothing is unpacked here: every entry is read from the ZIP and compared
    with ``peer_package.json``.
    """
    ok, reason = accept_decision(installed_version, offer["version"], channel, allow_development)
    if not ok:
        raise PeerPackageError(reason, f"Version {offer['version']} is not accepted here ({reason}).")
    path = Path(path)
    try:
        actual_size = path.stat().st_size
    except OSError as exc:
        raise PeerPackageError("missing", "The fetched package is gone.") from exc
    if actual_size != offer["size"] or _sha256_file(path) != offer["sha256"]:
        raise PeerPackageError("checksum_mismatch", "The package differs from the offer (size or SHA-256).")
    version = offer["version"]
    top = top_folder(version)
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise PeerPackageError("not_a_package", "The fetched file is not a FlowShift package.") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_FILES:
            raise PeerPackageError("too_many_files", "The package has too many files.")
        members = {}
        total = 0
        for info in infos:
            relative = _safe_member(info.filename, top)
            if relative is None:
                raise PeerPackageError("unsafe_path", f"The package contains an unsafe path: {info.filename[:120]!r}")
            if info.is_dir():
                continue
            # Unix mode bits in the upper half: links are never accepted.
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise PeerPackageError("unsafe_path", f"The package contains a link: {relative}")
            if relative in members:
                raise PeerPackageError("duplicate_file", f"The package lists {relative} twice.")
            total += info.file_size
            if total > MAX_UNPACKED_SIZE:
                raise PeerPackageError("too_large", "The unpacked package would be too large.")
            members[relative] = info
        if MANIFEST_NAME not in members:
            raise PeerPackageError("manifest_missing", f"The package has no {MANIFEST_NAME}.")
        try:
            manifest = json.loads(archive.read(members.pop(MANIFEST_NAME)).decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise PeerPackageError("manifest_invalid", f"{MANIFEST_NAME} is unreadable.") from exc
        if (not isinstance(manifest, dict) or manifest.get("format") != PACKAGE_FORMAT
                or not isinstance(manifest.get("files"), list)):
            raise PeerPackageError("manifest_invalid", f"{MANIFEST_NAME} has an unknown format.")
        if manifest.get("version") != version:
            raise PeerPackageError("version_mismatch",
                                   f"The package is {manifest.get('version')!r}, not the offered {version}.")
        listed = {}
        for entry in manifest["files"]:
            if not isinstance(entry, dict):
                raise PeerPackageError("manifest_invalid", "A file entry is invalid.")
            name, size, digest = entry.get("path"), entry.get("size"), entry.get("sha256")
            if (_safe_member(f"{top}/{name}", top) != name or name in listed or name == MANIFEST_NAME
                    or type(size) is not int or size < 0
                    or not isinstance(digest, str) or not _SHA256.match(digest)):
                raise PeerPackageError("manifest_invalid", f"The file entry {str(name)[:120]!r} is invalid.")
            listed[name] = (size, digest)
        missing = sorted(set(listed) - set(members))
        extra = sorted(set(members) - set(listed))
        if missing:
            raise PeerPackageError("incomplete", f"The package lacks {', '.join(missing[:5])}.")
        if extra:
            raise PeerPackageError("unlisted_file", f"The package has files that are not listed: {', '.join(extra[:5])}.")
        if any(name == "offline" or name.startswith("offline/") for name in members):
            raise PeerPackageError("unlisted_file", "A package from another PC never carries offline files.")
        for name, info in members.items():
            size, digest = listed[name]
            hasher = hashlib.sha256()
            count = 0
            with archive.open(info) as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    hasher.update(block)
                    count += len(block)
                    if count > size:
                        break
            if count != size or hasher.hexdigest() != digest:
                raise PeerPackageError("file_damaged", f"{name} differs from {MANIFEST_NAME} (size or SHA-256).")
        for name in ("VERSION", "webgui/dist/VERSION"):
            if name not in members:
                raise PeerPackageError("incomplete", f"The package lacks {name}.")
            stamp = archive.read(members[name]).decode("utf-8-sig", "replace").strip()
            if stamp != version:
                raise PeerPackageError("version_mismatch", f"{name} says {stamp[:40]!r}, not {version}.")
        for name in ["install_flowshift.ps1", "webgui/dist/index.html", "webgui/dist/overlay.html"] + \
                _required_files(archive, top):
            if name not in members:
                raise PeerPackageError("incomplete", f"The package lacks {name}.")
    return manifest


# ── the receiver's flow ─────────────────────────────────────────────────────

class PeerUpdateController:
    """Offer -> download -> check -> wait until idle -> hand off to the updater.

    All side effects come in as callbacks, so the tests run two of these with
    real packages and an in-memory connection.
    """

    def __init__(self, *, data_dir, installed_version, settings_getter, own_device_id_getter,
                 open_channel, handoff, safety_query=None, busy_check=None, reserve=None, release=None,
                 shutdown_callback=None, report=None,
                 notify=None, log=None, status_callback=None, development_mode=False,
                 announce_seconds=ANNOUNCE_SECONDS, safety_poll=SAFETY_POLL_SECONDS,
                 retry_pause=RETRY_PAUSE_SECONDS, clock=time.time):
        self._data_dir = Path(data_dir)
        self._installed = installed_version
        self._settings_getter = settings_getter
        self._own_device_id = own_device_id_getter
        self._open_channel = open_channel
        self._handoff = handoff
        self._safety_query = safety_query
        self._busy_check = busy_check
        self._reserve = reserve
        self._release = release
        self._install_after_download = False
        self._install_automatic = False
        self._shutdown_callback = shutdown_callback
        # report(identity, msg) sends a peer_update_status to the PC that
        # offered the update (shown on its Dashboard).
        self._report_fn = report
        self._report_lock = threading.Lock()
        self._last_report = {}
        self._linked_reported = set()
        self._restarting = False
        self._notify = notify
        self._log_fn = log
        self._status_callback = status_callback
        self._development_mode = bool(development_mode)
        self._announce_seconds = float(announce_seconds)
        self._safety_poll = float(safety_poll)
        self._retry_pause = float(retry_pause)
        self._clock = clock
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._stop = threading.Event()
        self._worker = None
        self._offer = None
        self._state = IDLE
        self._progress = None
        self._error = None
        self._package = None
        self._install_at = None
        self._wait_reason = None
        self._notified = set()
        # Newer version offered by a device that is not paired here: shown in
        # the WebGUI with what to do, never fetched.
        self._unpaired = None
        self._history = []
        self._last = self._load_last()

    # persistent record of the last handoff, read again after the restart
    @property
    def _state_path(self):
        return self._data_dir / "updates" / STATE_NAME

    def _load_last(self):
        try:
            value = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return None
        if not isinstance(value, dict):
            return None
        keys = ("from_version", "to_version", "peer_name", "handed_off_at")
        if not all(isinstance(value.get(key), str) for key in keys):
            return None
        record = {key: value[key][:MAX_NAME_LENGTH] for key in keys}
        attempts = value.get("attempts")
        record["attempts"] = attempts if type(attempts) is int and 0 < attempts < 1000 else 1
        identity = value.get("peer_identity")
        if isinstance(identity, str) and identity.startswith("device:"):
            record["peer_identity"] = identity[:MAX_NAME_LENGTH]
        return record

    def _save_last(self, record):
        path = self._state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
        self._last = record

    def _log(self, level, text):
        stamp = _now_text()
        with self._lock:
            self._history.append({"at": stamp, "level": level, "text": text})
            del self._history[:-HISTORY_LIMIT]
        if self._log_fn is not None:
            try:
                self._log_fn(level, f"peer update {text}")
            except Exception:
                pass

    def _publish(self):
        if self._status_callback is not None:
            try:
                self._status_callback()
            except Exception:
                pass
        self._send_report()

    def _settings(self):
        try:
            updates = config_schema.normalize_updates(self._settings_getter())
        except Exception:
            updates = config_schema.normalize_updates(None)
        return updates["channel"], updates["from_peers"], updates["enabled"]

    # ── inputs ──
    def on_offer(self, identity, peer, msg):
        """An offer arrived over the link of ``identity``; ``peer`` is the entry
        of this PC's device list for it (None = not paired)."""
        offer = validate_offer(msg)
        name = str((peer or {}).get("name") or (msg or {}).get("device_name") or identity)[:MAX_NAME_LENGTH]
        if offer is None:
            self._log("WARN", f"offer from {name} ignored: malformed")
            return "malformed"
        if peer is None:
            with self._lock:
                known = self._unpaired
                self._unpaired = {"peer_name": name, "version": offer["version"], "received_at": _now_text()}
            if known is None or known["version"] != offer["version"] or known["peer_name"] != name:
                self._log("WARN", f"offer of {offer['version']} from {name} ignored: not in this PC's "
                                  "device list with its device ID")
            self._publish()
            return "not_paired"
        channel, settings, enabled = self._settings()
        ok, reason = accept_decision(self._installed, offer["version"], channel,
                                     settings["allow_development"])
        if not ok:
            if reason != "not_newer":
                self._log("INFO", f"offer of {offer['version']} from {name} not accepted ({reason})")
            return reason
        offer.update(identity=identity, peer_name=name, peer=dict(peer), received_at=_now_text())
        with self._lock:
            current = self._offer
            if self._state in {DOWNLOADING, WAITING, INSTALLING}:
                return "busy"
            if current is not None and current["version"] == offer["version"] \
                    and current["sha256"] == offer["sha256"]:
                current.update(identity=identity, peer=dict(peer), peer_name=name)
                retry = (self._state == FAILED and (self._error or {}).get("code") in TRANSIENT_ERRORS)
                if not retry:
                    return "known"
                self._state = AVAILABLE
                self._error = None
                offer = current
            if current is not None and parse_semver(current["version"]) > parse_semver(offer["version"]):
                return "lower_than_known"
            self._offer = offer
            self._state = AVAILABLE
            self._error = None
            self._progress = None
            self._package = None
        self._log("INFO", f"{name} offers FlowShift {offer['version']} "
                          f"({offer['size']} bytes); setting: {settings['policy']}")
        if offer["version"] not in self._notified:
            self._notified.add(offer["version"])
            self._tell(f"{name} has FlowShift {offer['version']}",
                       {POLICY_NOTIFY: "Open Software Update to download and install it.",
                        POLICY_DOWNLOAD: "FlowShift downloads it; click Install in Software Update.",
                        POLICY_INSTALL: "FlowShift downloads and installs it and then restarts."}
                       [settings["policy"]])
        self._publish()
        if enabled and settings["policy"] in {POLICY_DOWNLOAD, POLICY_INSTALL}:
            if self._held(offer["version"]):
                failure = self._last_failure(offer["version"]) or {}
                self._log("WARN", f"automatic install of {offer['version']} paused after "
                                  f"{self._failed_attempts(offer['version'])} failed attempts "
                                  f"({failure.get('message', '-')}); click Install to try again")
                if ("paused", offer["version"]) not in self._notified:
                    self._notified.add(("paused", offer["version"]))
                    self._tell(f"FlowShift {offer['version']} paused",
                               f"Installing it failed {self._failed_attempts(offer['version'])} times; "
                               f"FlowShift {self._installed} keeps running. Software Update has the reason.")
            else:
                self._start(install=settings["policy"] == POLICY_INSTALL, automatic=True)
        return "accepted"

    def _failed_attempts(self, version):
        """Failed installs of exactly ``version`` on this PC (0 once it runs)."""
        if version == self._installed:
            return 0
        last = self._last or {}
        if last.get("to_version") == version:
            return int(last.get("attempts") or 1)
        result = self._last_result() or {}
        if result.get("to_version") == version and result.get("result") != "success":
            return 1
        return 0

    def _held(self, version):
        """True after MAX_AUTOMATIC_ATTEMPTS failed installs of exactly this
        version: no automatic update loop (Install by hand still works)."""
        return self._failed_attempts(version) >= MAX_AUTOMATIC_ATTEMPTS

    def _last_failure(self, version):
        """Why the last install of ``version`` failed (read after the restart)."""
        if not self._failed_attempts(version):
            return None
        result = self._last_result() or {}
        if result.get("to_version") != version or result.get("result") == "success":
            return {"code": "not_installed",
                    "message": f"FlowShift {version} did not start; {self._installed} runs again."}
        message = str(result.get("error") or "").strip() or "The updater reported no reason."
        return {"code": str(result.get("result") or "failed")[:40], "message": message[:MAX_MESSAGE_LENGTH]}

    def _tell(self, title, text):
        if self._notify is not None:
            try:
                self._notify(title, text)
            except Exception:
                pass

    def settings_changed(self):
        """Called after the user saved the setting: a waiting offer follows it."""
        channel, settings, enabled = self._settings()
        with self._lock:
            offer = self._offer
            state = self._state
        if offer is None:
            return None
        ok, reason = accept_decision(self._installed, offer["version"], channel, settings["allow_development"])
        if not ok:
            with self._lock:
                if self._state in {AVAILABLE, DOWNLOADED, FAILED}:
                    self._offer = None
                    self._state = IDLE
                    self._package = None
                    self._error = None
            self._log("INFO", f"offer of {offer['version']} dropped after the settings change ({reason})")
            self._publish()
            return reason
        if not enabled or self._held(offer["version"]):
            return None
        if state == AVAILABLE and settings["policy"] in {POLICY_DOWNLOAD, POLICY_INSTALL}:
            return self._start(install=settings["policy"] == POLICY_INSTALL, automatic=True)
        if state == DOWNLOADED and settings["policy"] == POLICY_INSTALL:
            return self._start(install=True, automatic=True)
        return None

    # ── user actions ──
    def download(self):
        return self._start(install=False, automatic=False)

    def install(self):
        return self._start(install=True, automatic=False)

    def cancel(self):
        with self._lock:
            if self._state not in {DOWNLOADING, WAITING}:
                return False
            self._cancel.set()
        self._log("INFO", "cancelled by the user")
        return True

    def _start(self, install, automatic):
        with self._lock:
            if self._development_mode:
                return "development_checkout"
            if self._offer is None:
                return "no_offer"
            if self._state in {DOWNLOADING, WAITING, INSTALLING}:
                if install and self._state == DOWNLOADING:
                    self._install_after_download = True
                    self._install_automatic = automatic
                return "already_running"
            self._cancel.clear()
            self._install_after_download = bool(install)
            self._install_automatic = bool(automatic)
            have_package = self._state == DOWNLOADED and self._package is not None
            self._state = WAITING if (have_package and install) else DOWNLOADING
            if self._state == DOWNLOADING:
                self._progress = {"bytes": 0, "total": self._offer["size"]}
            self._error = None
            self._worker = threading.Thread(target=self._run, name="FlowShiftPeerUpdate", daemon=True)
            self._worker.start()
        self._publish()
        return "started"

    def _fail(self, code, message):
        with self._lock:
            self._state = FAILED if code != "cancelled" else AVAILABLE
            self._error = {"code": code, "message": message}
            self._install_at = None
            self._wait_reason = None
        self._log("WARN" if code != "cancelled" else "INFO", f"stopped: {code}: {message}")
        self._publish()

    def _run(self):
        offer = dict(self._offer)
        name = offer["peer_name"]
        try:
            if self._package is None or self._state == DOWNLOADING:
                path = self._fetch(offer)
                if path is None:
                    return
            if not self._install_after_download:
                self._log("INFO", f"FlowShift {offer['version']} from {name} is downloaded and checked; "
                                  "click Install to install it")
                return
            self._install(offer)
        except Exception as exc:  # never let the worker die silently
            self._fail("internal_error", f"{type(exc).__name__}: {exc}")

    def _fetch(self, offer):
        name = offer["peer_name"]
        endpoint = peer_endpoint(offer["peer"])
        if endpoint is None:
            self._fail("no_address", f"{name} has no address in the device list.")
            return None
        self._log("INFO", f"fetching FlowShift {offer['version']} from {name} ({endpoint[0]}:{endpoint[1]})")
        downloads = self._data_dir / "updates" / "downloads"

        def progress(done, total):
            with self._lock:
                self._progress = {"bytes": done, "total": total}
            self._send_report()

        def opener(hello):
            return self._open_channel(offer["peer"], hello)

        try:
            path = download_package(opener, offer, self._own_device_id(), downloads, progress=progress,
                                    cancel=self._cancel, pause=self._retry_pause, sleep=self._cancel.wait)
            channel, settings, _ = self._settings()
            verify_package(path, offer, self._installed, channel, settings["allow_development"])
        except PeerPackageError as exc:
            if exc.code not in {"cancelled"}:
                for leftover in (downloads / package_file_name(offer["version"]),):
                    try:
                        leftover.unlink()
                    except OSError:
                        pass
            self._fail(exc.code, exc.message)
            return None
        for old in downloads.glob("FlowShift-Peer-*.zip"):
            if old.name != Path(path).name:
                try:
                    old.unlink()
                except OSError:
                    pass
        with self._lock:
            self._package = {"path": str(path), "size": offer["size"], "sha256": offer["sha256"]}
            self._state = DOWNLOADED
        self._log("INFO", f"FlowShift {offer['version']} from {name} fetched and checked "
                          f"({offer['size']} bytes, every file matches {MANIFEST_NAME})")
        self._publish()
        return path

    def _install(self, offer):
        name = offer["peer_name"]
        automatic = self._install_automatic
        announce = self._announce_seconds if automatic else 0.0
        with self._lock:
            self._state = WAITING
            self._install_at = self._clock() + announce
            self._wait_reason = None
        if announce > 0:
            self._tell(f"FlowShift {offer['version']} from {name}",
                       f"FlowShift installs it and restarts in about {int(announce)} seconds, "
                       "not during a transfer or while forwarding. Cancel in Software Update.")
        self._log("INFO", f"install of {offer['version']} from {name} announced; FlowShift restarts "
                          "when no transfer, clipboard action or forwarding runs")
        self._publish()
        while True:
            if self._cancel.is_set():
                with self._lock:
                    self._state = DOWNLOADED
                    self._install_at = None
                    self._wait_reason = None
                self._log("INFO", f"install of {offer['version']} cancelled; the package stays ready")
                self._publish()
                return
            remaining = self._install_at - self._clock()
            if remaining <= 0:
                reason = self._blocker()
                if reason is None:
                    reason = self._reserve_now()
                if reason is None:
                    break
                if reason != self._wait_reason:
                    self._log("INFO", f"install waits: {reason}")
                with self._lock:
                    self._wait_reason = reason
                self._publish()
                self._cancel.wait(self._safety_poll)
            else:
                self._cancel.wait(min(remaining, self._safety_poll))
        with self._lock:
            self._state = INSTALLING
            package = dict(self._package)
        self._publish()
        try:
            self._handoff(self._installed, offer["version"], package["path"], package["size"], package["sha256"])
        except Exception as exc:
            self._release_now()
            code = getattr(exc, "code", "handoff_failed")
            self._fail(code, str(exc) or "The updater did not start.")
            return
        attempts = self._failed_attempts(offer["version"]) + 1
        self._save_last({"from_version": self._installed, "to_version": offer["version"],
                         "peer_name": name, "handed_off_at": _now_text(), "attempts": attempts,
                         "peer_identity": str(offer.get("identity") or "")[:MAX_NAME_LENGTH]})
        self._log("INFO", f"updater started: {self._installed} -> {offer['version']} from {name} "
                          f"(attempt {attempts}); FlowShift restarts now")
        # Last word to the sending PC before the link goes away.
        with self._lock:
            self._restarting = True
        self._send_report()
        if self._shutdown_callback is not None:
            self._shutdown_callback()

    def _reserve_now(self):
        """Stop new transfers and input from starting; None when still safe."""
        if self._reserve is None:
            return None
        try:
            result = self._reserve()
        except Exception:
            result = None
        if isinstance(result, dict) and result.get("safe") is True:
            return None
        self._release_now()
        return str((result or {}).get("reason") or "runtime_state_unknown")

    def _release_now(self):
        if self._release is not None:
            try:
                self._release()
            except Exception:
                pass

    def _blocker(self):
        if self._busy_check is not None:
            try:
                busy = self._busy_check()
            except Exception:
                busy = "update_state_unknown"
            if busy:
                return str(busy)
        if self._safety_query is None:
            return None
        try:
            result = self._safety_query()
        except Exception:
            return "runtime_state_unknown"
        if isinstance(result, dict) and result.get("safe") is True:
            return None
        return str((result or {}).get("reason") or "runtime_state_unknown")

    # ── reports to the PC that offered the update ──
    def status_message(self):
        """``peer_update_status`` for the sending PC: what happens with its offer."""
        channel, settings, enabled = self._settings()
        with self._lock:
            offer = self._offer
            state = "restarting" if self._restarting else self._state
            progress = dict(self._progress) if self._progress else None
            wait_reason = self._wait_reason
            error = dict(self._error) if self._error else None
            install_at = self._install_at
        version = offer["version"] if offer else None
        msg = {"type": T_STATUS, "update_protocol": UPDATE_PROTOCOL, "state": state,
               "installed_version": self._installed, "version": version,
               "policy": settings["policy"], "automatic_enabled": bool(enabled)}
        if state == DOWNLOADING and progress and progress.get("total"):
            msg["percent"] = max(0, min(100, int(progress["bytes"] * 100 / progress["total"])))
        if state == WAITING:
            msg["wait_reason"] = wait_reason
            if install_at:
                msg["install_in_seconds"] = max(0, int(install_at - self._clock()))
        if state == "restarting":
            msg["restart_seconds"] = RESTART_ESTIMATE_SECONDS
        if error:
            msg["error"] = {"code": str(error.get("code"))[:40],
                            "message": str(error.get("message") or "")[:MAX_MESSAGE_LENGTH]}
        if version is not None:
            attempts = self._failed_attempts(version)
            if attempts:
                msg["failed_attempts"] = attempts
                msg["last_failure"] = self._last_failure(version)
            msg["paused"] = self._held(version)
        return msg

    def _send_report(self, identity=None, force=False):
        """Send the status to ``identity`` (default: the PC of the current
        offer) when it changed since the last report to that PC."""
        if self._report_fn is None:
            return False
        with self._lock:
            offer = self._offer
        target = identity or (offer or {}).get("identity")
        if not target:
            return False
        msg = self.status_message()
        key = json.dumps({k: v for k, v in msg.items() if k != "install_in_seconds"}, sort_keys=True)
        with self._report_lock:
            if not force and self._last_report.get(target) == key:
                return False
            self._last_report[target] = key
        try:
            self._report_fn(target, msg)
        except Exception as exc:
            with self._report_lock:
                self._last_report.pop(target, None)
            if self._log_fn is not None:
                try:
                    self._log_fn("DEBUG", f"peer update status to {target} not sent: {exc!r}")
                except Exception:
                    pass
            return False
        return True

    def on_peer_linked(self, identity, peer):
        """A paired PC is connected (again): tell it how its last update ended.

        After the restart this is how the sending PC learns "updated" or
        "failed: reason, runs X". Only the PC the update came from gets it,
        and only for a handoff of the last day.
        """
        if peer is None or self._report_fn is None:
            return None
        last = self._last
        if not last or last.get("peer_identity") != identity:
            return None
        with self._report_lock:
            if identity in self._linked_reported:
                return None
            self._linked_reported.add(identity)
        try:
            handed = _dt.datetime.strptime(last["handed_off_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=_dt.timezone.utc).timestamp()
        except (KeyError, ValueError):
            return None
        if self._clock() - handed > REPORT_MAX_AGE_SECONDS:
            return None
        version = last["to_version"]
        if version == self._installed:
            msg = {"type": T_STATUS, "update_protocol": UPDATE_PROTOCOL, "state": "updated",
                   "installed_version": self._installed, "version": version}
        else:
            failure = self._last_failure(version) or {
                "code": "not_installed", "message": f"FlowShift {version} is not running."}
            msg = {"type": T_STATUS, "update_protocol": UPDATE_PROTOCOL, "state": FAILED,
                   "installed_version": self._installed, "version": version, "error": failure,
                   "failed_attempts": self._failed_attempts(version), "paused": self._held(version)}
        try:
            self._report_fn(identity, msg)
        except Exception:
            return None
        self._log("INFO", f"told {last['peer_name']}: update to {version} {msg['state']}")
        return msg["state"]

    def wait(self, timeout=None):
        worker = self._worker
        if worker is not None:
            worker.join(timeout)

    def shutdown(self):
        self._cancel.set()
        self.wait(5.0)

    # ── WebGUI ──
    def snapshot(self):
        channel, settings, enabled = self._settings()
        with self._lock:
            offer = self._offer
            last = dict(self._last) if self._last else None
            result = self._last_result()
            if last is not None:
                last["result"] = result.get("result") if result and result.get("to_version") == last["to_version"] else None
                last["installed"] = last["to_version"] == self._installed
            return {
                "state": self._state,
                "installed_version": self._installed,
                "channel": channel,
                "policy": settings["policy"],
                "allow_development": settings["allow_development"],
                "automatic_enabled": enabled,
                "development_mode": self._development_mode,
                "offer": None if offer is None else {
                    "version": offer["version"], "size": offer["size"], "peer_name": offer["peer_name"],
                    "development": not parse_semver(offer["version"]).is_stable,
                    "received_at": offer["received_at"],
                },
                "progress": dict(self._progress) if self._progress else None,
                "install_at": (_dt.datetime.fromtimestamp(self._install_at, _dt.timezone.utc)
                               .strftime("%Y-%m-%dT%H:%M:%SZ") if self._install_at else None),
                "wait_reason": self._wait_reason,
                "error": dict(self._error) if self._error else None,
                "unpaired_offer": dict(self._unpaired) if self._unpaired else None,
                "failed_attempts": self._failed_attempts(offer["version"]) if offer else 0,
                "paused": bool(offer) and self._held(offer["version"]),
                "max_automatic_attempts": MAX_AUTOMATIC_ATTEMPTS,
                "last": last,
                "log": list(self._history),
            }

    def _last_result(self):
        try:
            value = json.loads((self._data_dir / "updates" / "last_update_result.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return None
        return value if isinstance(value, dict) else None


# ── the sending PC's view of its peers (Dashboard) ──────────────────────────

# Sender-side phases per peer, stable codes for the WebGUI.
PEER_OLDER = "older"
PEER_OFFERED = "offered"
PEER_TRANSFERRING = "transferring"
PEER_INTERRUPTED = "interrupted"
PEER_READY = "ready"
PEER_WAITING = "waiting"
PEER_INSTALLING = "installing"
PEER_RESTARTING = "restarting"
PEER_UPDATED = "updated"
PEER_FAILED = "failed"
PEER_UPDATE_ONLY = "update_only"

# While a peer is in one of these, a switch to it is refused with a message.
SWITCH_BLOCKING = frozenset({PEER_INSTALLING, PEER_RESTARTING})

# Receiver states (and the two report-only states) -> sender phase.
_RECEIVER_PHASES = {
    AVAILABLE: PEER_OFFERED,
    DOWNLOADING: PEER_TRANSFERRING,
    DOWNLOADED: PEER_READY,
    WAITING: PEER_WAITING,
    INSTALLING: PEER_INSTALLING,
    "restarting": PEER_RESTARTING,
    FAILED: PEER_FAILED,
    "updated": PEER_UPDATED,
}
# Waiting because the user works on the other PC right now.
_CONTROLLED_REASONS = frozenset({"forwarding_active", "edge_session_active", "input_pipeline_busy"})


def _short(value, limit=MAX_MESSAGE_LENGTH):
    return str(value or "").strip()[:limit]


def validate_status(msg):
    """Checked ``peer_update_status`` fields or None (display only, but never
    trusted beyond plain text and numbers)."""
    if not isinstance(msg, dict) or msg.get("type") != T_STATUS:
        return None
    state = msg.get("state")
    if state not in _RECEIVER_PHASES and state != IDLE:
        return None
    installed = msg.get("installed_version")
    if _canonical(installed) is None:
        return None
    version = msg.get("version")
    if version is not None and _canonical(version) is None:
        return None
    result = {"state": state, "installed_version": installed, "version": version,
              "policy": msg.get("policy") if msg.get("policy") in {POLICY_NOTIFY, POLICY_DOWNLOAD, POLICY_INSTALL} else None,
              "paused": msg.get("paused") is True}
    for key, top in (("percent", 100), ("install_in_seconds", 24 * 3600), ("restart_seconds", 3600),
                     ("failed_attempts", 1000)):
        value = msg.get(key)
        if type(value) is int and 0 <= value <= top:
            result[key] = value
    if isinstance(msg.get("wait_reason"), str):
        result["wait_reason"] = _short(msg["wait_reason"], 60)
    for key in ("error", "last_failure"):
        value = msg.get(key)
        if isinstance(value, dict):
            result[key] = {"code": _short(value.get("code"), 40), "message": _short(value.get("message"))}
    return result


class PeerUpdateMonitor:
    """What the PC with the newer version shows per peer while it updates it.

    Fed by the hello of every link (versions), the end of a link and the
    receiver's ``peer_update_status`` messages; ``notify(title, text)`` shows
    the short tray message when the update starts and when the peer is back.
    """

    def __init__(self, local_version, *, notify=None, log=None, on_change=None, clock=time.time,
                 restart_timeout=RESTART_TIMEOUT_SECONDS):
        self._local = local_version
        self._notify = notify
        self._log_fn = log
        self._on_change = on_change
        self._clock = clock
        self._restart_timeout = float(restart_timeout)
        self._lock = threading.Lock()
        self._peers = {}

    def _log(self, level, text):
        if self._log_fn is not None:
            try:
                self._log_fn(level, f"peer update monitor: {text}")
            except Exception:
                pass

    def _tell(self, title, text):
        if self._notify is not None:
            try:
                self._notify(title, text)
            except Exception:
                pass

    def _changed(self):
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:
                pass

    def _entry(self, identity, name):
        entry = self._peers.get(identity)
        if entry is None:
            entry = {"identity": identity, "name": name, "phase": None, "remote_version": None,
                     "target_version": self._local, "connected": False, "since": self._clock(),
                     "started_notified": False}
            self._peers[identity] = entry
        if name:
            entry["name"] = _short(name, MAX_NAME_LENGTH)
        return entry

    def _set(self, entry, phase, **fields):
        if entry["phase"] != phase:
            entry["since"] = self._clock()
        entry["phase"] = phase
        for key in ("percent", "wait_reason", "install_in_seconds", "restart_seconds", "error",
                    "paused", "failed_attempts", "policy"):
            entry.pop(key, None)
        entry.update({key: value for key, value in fields.items() if value is not None})

    # ── inputs ──
    def on_link(self, identity, name, remote_version, mode=LINK_FULL):
        """A hello arrived: compare the versions right away (not at the edge)."""
        back = None
        with self._lock:
            entry = self._peers.get(identity)
            previous = entry["phase"] if entry else None
            was_connected = bool(entry and entry["connected"])
            entry = self._entry(identity, name)
            entry["connected"] = True
            entry["remote_version"] = remote_version if _canonical(remote_version) else None
            entry["link_mode"] = mode
            target = entry.get("target_version") or self._local
            # A peer without status reports (0.7.1-dev.2) that went away older
            # and came back with this version was updated as well.
            silent_update = (previous in {PEER_OLDER, PEER_OFFERED, PEER_INTERRUPTED, PEER_FAILED}
                             and not was_connected and remote_version == target)
            if silent_update:
                self._set(entry, PEER_UPDATED)
                back = entry
            elif previous in {PEER_INSTALLING, PEER_RESTARTING, PEER_WAITING, PEER_READY}:
                if remote_version == target:
                    self._set(entry, PEER_UPDATED)
                    back = entry
                else:
                    # Came back with the old version: the receiver's report
                    # (sent right after the hello) names the reason.
                    self._set(entry, PEER_FAILED, error={
                        "code": "not_updated",
                        "message": f"came back with {remote_version or 'an unknown version'}"})
            elif should_offer(self._local, remote_version):
                if previous != PEER_FAILED:
                    self._set(entry, PEER_OLDER)
                entry["target_version"] = self._local
            elif previous == PEER_UPDATED and remote_version == target:
                pass
            elif mode == LINK_UPDATE_ONLY:
                self._set(entry, PEER_UPDATE_ONLY)
            else:
                # Same version (or this PC is the older one): nothing to show.
                del self._peers[identity]
                entry = None
            phase = entry["phase"] if entry is not None else None
        if back is not None:
            self._log("INFO", f"{back['name']} is back with FlowShift {back['remote_version']}")
            self._tell(f"{back['name']} is updated",
                       f"{back['name']} is connected again with FlowShift {back['remote_version']} "
                       "(same version as this PC). Switching works again.")
        self._changed()
        return phase

    def on_unlink(self, identity):
        with self._lock:
            entry = self._peers.get(identity)
            if entry is None:
                return None
            entry["connected"] = False
            if entry["phase"] in {PEER_INSTALLING, PEER_WAITING, PEER_RESTARTING}:
                # The receiver restarts now (or crashed while installing).
                if entry["phase"] != PEER_RESTARTING:
                    self._set(entry, PEER_RESTARTING, restart_seconds=RESTART_ESTIMATE_SECONDS)
            elif entry["phase"] == PEER_TRANSFERRING:
                self._set(entry, PEER_INTERRUPTED)
            elif entry["phase"] in {PEER_UPDATED, PEER_UPDATE_ONLY}:
                del self._peers[identity]
            phase = entry["phase"]
        self._changed()
        return phase

    def on_status(self, identity, name, msg):
        """The receiver reported a step; returns the new phase or None."""
        status = validate_status(msg)
        if status is None:
            self._log("WARN", f"malformed status from {name or identity} ignored")
            return None
        started = back = failed = None
        with self._lock:
            tracked = identity in self._peers
            entry = self._entry(identity, name)
            entry["remote_version"] = status["installed_version"]
            if status["version"]:
                entry["target_version"] = status["version"]
            state = status["state"]
            phase = _RECEIVER_PHASES.get(state)
            if state in {IDLE, AVAILABLE} and status.get("last_failure") and status["paused"]:
                phase = PEER_FAILED
                status["error"] = status["last_failure"]
            if phase is None:
                return entry["phase"]
            if phase == PEER_UPDATED and status["installed_version"] != entry["target_version"]:
                phase = PEER_FAILED
            previous = entry["phase"]
            # "came back with the old version" from the hello is replaced by
            # the receiver's reason and only then told.
            if previous == PEER_FAILED and (entry.get("error") or {}).get("code") == "not_updated":
                previous = PEER_RESTARTING
            self._set(entry, phase, percent=status.get("percent"), wait_reason=status.get("wait_reason"),
                      install_in_seconds=status.get("install_in_seconds"),
                      restart_seconds=status.get("restart_seconds"), error=status.get("error"),
                      paused=status["paused"] or None, failed_attempts=status.get("failed_attempts"),
                      policy=status.get("policy"))
            if phase in {PEER_TRANSFERRING, PEER_WAITING, PEER_INSTALLING} and not entry["started_notified"]:
                entry["started_notified"] = True
                started = dict(entry)
            if phase == PEER_UPDATED and previous != PEER_UPDATED and tracked:
                back = dict(entry)
            if phase == PEER_FAILED and previous != PEER_FAILED:
                entry["started_notified"] = False
                failed = dict(entry)
        if started is not None:
            self._log("INFO", f"update of {started['name']} to {started['target_version']} started")
            self._tell(f"Updating {started['name']}",
                       f"{started['name']} gets FlowShift {started['target_version']} "
                       f"(runs {started['remote_version']}). It restarts when you are not working on it; "
                       "the Dashboard shows each step.")
        if back is not None:
            self._log("INFO", f"{back['name']} reports FlowShift {back['remote_version']} running")
            self._tell(f"{back['name']} is updated",
                       f"{back['name']} runs FlowShift {back['remote_version']} now. Switching works again.")
        if failed is not None:
            reason = (failed.get("error") or {}).get("message") or "no reason given"
            self._log("WARN", f"update of {failed['name']} failed: {reason}")
            self._tell(f"Update of {failed['name']} failed",
                       f"{reason}. {failed['name']} keeps running FlowShift {failed['remote_version']}."
                       + (" Automatic attempts are paused." if failed.get("paused") else ""))
        self._changed()
        return phase

    # ── outputs ──
    def view(self, identity):
        """Dashboard state of one peer (None = nothing to show)."""
        with self._lock:
            entry = self._peers.get(identity)
            if entry is None:
                return None
            entry = dict(entry)
            if entry["phase"] == PEER_RESTARTING and not entry["connected"] \
                    and self._clock() - entry["since"] > self._restart_timeout:
                live = self._peers[identity]
                self._set(live, PEER_FAILED, error={
                    "code": "no_return",
                    "message": f"did not come back within {int(self._restart_timeout // 60)} minutes"})
                entry = dict(live)
        if entry["phase"] is None:
            return None
        return {
            "phase": entry["phase"],
            "text": describe(entry),
            "remote_version": entry.get("remote_version"),
            "target_version": entry.get("target_version"),
            "percent": entry.get("percent"),
            "wait_reason": entry.get("wait_reason"),
            "error": entry.get("error"),
            "paused": bool(entry.get("paused")),
            "connected": entry["connected"],
            "since": _dt.datetime.fromtimestamp(entry["since"], _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    def snapshot(self):
        with self._lock:
            identities = list(self._peers)
        return {identity: view for identity in identities if (view := self.view(identity)) is not None}

    def switch_block(self, identity):
        """Message instead of a silent failure when the target is updating."""
        view = self.view(identity)
        if view is None:
            return None
        with self._lock:
            entry = self._peers.get(identity) or {}
            name = entry.get("name") or identity
            mode = entry.get("link_mode")
        if view["phase"] in SWITCH_BLOCKING:
            return (f"{name} is being updated to FlowShift {view['target_version']}; "
                    "switching works again as soon as it is back.")
        if view["phase"] == PEER_UPDATE_ONLY or mode == LINK_UPDATE_ONLY:
            return (f"{name} runs FlowShift {view['remote_version']} with another link protocol; "
                    "it must be updated first.")
        return None


def describe(entry):
    """One plain line for the Dashboard."""
    phase = entry.get("phase")
    remote = entry.get("remote_version") or "?"
    target = entry.get("target_version") or "?"
    name = entry.get("name") or "the other PC"
    error = (entry.get("error") or {}).get("message") or "no reason given"
    if phase == PEER_OLDER:
        return f"Older version {remote}"
    if phase == PEER_OFFERED:
        if entry.get("policy") == POLICY_NOTIFY:
            return f"Older version {remote}; update {target} offered, waits for a click on {name}"
        return f"Older version {remote}; update {target} offered"
    if phase == PEER_TRANSFERRING:
        percent = entry.get("percent")
        return f"Update is being transferred ({percent if percent is not None else 0} %)"
    if phase == PEER_INTERRUPTED:
        return "Transfer interrupted; continues when connected again"
    if phase == PEER_READY:
        return f"Update {target} transferred; waits for Install on {name}"
    if phase == PEER_WAITING:
        if entry.get("wait_reason") in _CONTROLLED_REASONS:
            return "Update waits until you are back on this PC"
        seconds = entry.get("install_in_seconds")
        if entry.get("wait_reason"):
            return f"Update waits until {name} is idle ({entry['wait_reason']})"
        if seconds:
            return f"Installs in about {seconds} s"
        return "Installs shortly"
    if phase == PEER_INSTALLING:
        return "Is being installed"
    if phase == PEER_RESTARTING:
        return f"Restarts (connection gone for about {entry.get('restart_seconds') or RESTART_ESTIMATE_SECONDS} s)"
    if phase == PEER_UPDATED:
        return f"Connected again, version {remote}"
    if phase == PEER_FAILED:
        text = f"Update failed: {error}; runs on with {remote}"
        if entry.get("paused"):
            text += f" (automatic attempts paused after {entry.get('failed_attempts') or MAX_AUTOMATIC_ATTEMPTS})"
        return text
    if phase == PEER_UPDATE_ONLY:
        return f"Version {remote} speaks another link protocol: only the update works"
    return str(phase)
