"""Transport-neutral, same-volume V2 publication. No network activation.

StagedFile.fingerprint is LOCAL receiver evidence, not a wire field: the tuple
returned by staged_fingerprint(fstat, handle) after hashing/fsync and verified rename.
Reopen must rehash retained files before issuing fresh evidence. Callers must not
construct this evidence from untrusted metadata or stat an unverified file.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import threading
from collections import OrderedDict
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
from pathlib import Path

import clipboard_manifest_v2 as manifest_v2
import clipboard_paths
import clipboard_resume_v2 as resume_v2


_locks_guard = threading.Lock()
_locks = {}


class ObjectStoreV2Error(RuntimeError):
    def __init__(self, code, message, *, retryable=False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class PublishedManifest:
    transfer_id: str
    provisional_manifest_digest: str
    manifest_digest: str
    manifest: dict
    object_hashes: tuple[str, ...]


class _WindowsStat:
    """Path stat enriched with change time obtained from its verified handle."""
    def __init__(self, info, change_time_ns):
        self.info = info
        self.change_time_ns = change_time_ns

    def __getattr__(self, name):
        return getattr(self.info, name)


def staged_fingerprint(info, handle=None):
    """Return (device, inode, size, mtime_ns, change_time_ns).

    Windows requires an open Python file object or CRT descriptor, never a raw
    Win32 HANDLE. Its ChangeTime is in nanoseconds since the Windows epoch;
    POSIX uses st_ctime_ns. Raw Windows stat creation time is never trusted.
    """
    identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    if os.name != "nt":
        return identity + (info.st_ctime_ns,)
    if handle is None:
        if isinstance(info, _WindowsStat):
            return identity + (info.change_time_ns,)
        raise ObjectStoreV2Error("fingerprint_unavailable", "Windows fingerprint requires an open handle")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class FILE_BASIC_INFO(ctypes.Structure):
        _fields_ = [(name, ctypes.c_longlong) for name in (
            "CreationTime", "LastAccessTime", "LastWriteTime", "ChangeTime")]
        _fields_.append(("FileAttributes", wintypes.DWORD))

    try:
        fd = handle if isinstance(handle, int) else handle.fileno()
        opened = os.fstat(fd)
        if identity != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns):
            raise ObjectStoreV2Error("stage_changed", "fingerprint handle does not match stat")
        query = ctypes.WinDLL("kernel32", use_last_error=True).GetFileInformationByHandleEx
        query.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        query.restype = wintypes.BOOL
        basic = FILE_BASIC_INFO()
        if not query(msvcrt.get_osfhandle(fd), 0, ctypes.byref(basic), ctypes.sizeof(basic)):
            raise ctypes.WinError(ctypes.get_last_error())
        if basic.ChangeTime <= 0:
            raise OSError("invalid file change time")
        return identity + (basic.ChangeTime * 100,)
    except (OSError, ValueError, AttributeError) as exc:
        raise ObjectStoreV2Error("fingerprint_unavailable", "file change time is unavailable") from exc


def _fsync_directory(path):
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _safe_directory(path, *, create=False):
    path = Path(os.path.abspath(path))
    current = Path(path.anchor)
    for component in path.parts[1:]:
        parent = current
        current /= component
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            if not create:
                raise
            try:
                os.mkdir(current)
                _fsync_directory(parent)
            except FileExistsError:
                pass
            info = os.lstat(current)
        if not stat.S_ISDIR(info.st_mode) or clipboard_paths._is_reparse_point(current, info):
            raise ObjectStoreV2Error("unsafe_store", "store traverses an unsafe directory")


def _regular(path):
    _safe_directory(os.path.dirname(path))
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or clipboard_paths._is_reparse_point(path, info):
        raise ObjectStoreV2Error("unsafe_store", "store entry is not a regular file")
    if os.name == "nt":
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        try:
            opened = os.fstat(fd)
            if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
                    opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns):
                raise ObjectStoreV2Error("unsafe_store", "store entry changed while opening")
            info = _WindowsStat(opened, staged_fingerprint(opened, fd)[4])
        finally:
            os.close(fd)
    return info


@contextmanager
def _open_regular(path, *, writable=False):
    before = _regular(path)
    fd = os.open(path, (os.O_RDWR if writable else os.O_RDONLY) | getattr(os, "O_BINARY", 0)
                 | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "r+b" if writable else "rb", buffering=0) as handle:
        if staged_fingerprint(before) != staged_fingerprint(os.fstat(handle.fileno()), handle):
            raise ObjectStoreV2Error("unsafe_store", "store entry changed while opening")
        yield handle


@contextmanager
def open_verified_handoff(path):
    """Yield an unbuffered r+b file for verified rename/link handoff.

    Windows denies other WRITE opens, shares READ|DELETE, and fails immediately
    with retryable stage_busy if a writer already holds the inode. Close the
    staging writer before entering; use this handle for fstat, fingerprint and
    fsync, and never write through it. Rename remains possible while it is open.
    POSIX has no mandatory exclusion here: callers must rehash after a metadata
    change before trusting a refreshed fingerprint. The private-root boundary
    still excludes hostile same-user directory replacement/mmap writers.
    """
    before = _regular(path)
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                           ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        create.restype = wintypes.HANDLE
        native = create(os.fspath(path), 0x80000000 | 0x40000000,
                        0x1 | 0x4, None, 3, 0x00200000, None)
        if native == ctypes.c_void_p(-1).value:
            code = ctypes.get_last_error()
            if code in (32, 33):
                raise ObjectStoreV2Error("stage_busy", "verified handoff has an active writer",
                                        retryable=True)
            raise ctypes.WinError(code)
        try:
            fd = msvcrt.open_osfhandle(native, os.O_RDWR | os.O_BINARY)
        except BaseException:
            close = kernel.CloseHandle
            close.argtypes = (wintypes.HANDLE,)
            close.restype = wintypes.BOOL
            close(native)
            raise
    else:
        fd = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "r+b", buffering=0) as handle:
        opened = os.fstat(handle.fileno())
        if (not stat.S_ISREG(opened.st_mode)
                or clipboard_paths._is_reparse_point(path, opened)
                or staged_fingerprint(before) != staged_fingerprint(opened, handle)):
            raise ObjectStoreV2Error("stage_changed", "verified handoff identity changed")
        yield handle


class ClipboardObjectStoreV2:
    """Cooperating writers and GC serialize on one bounded per-root lock.

    The root must be private to the application user. Ancestors/reparse points
    are checked, but these path APIs cannot defend against an adversarial local
    process concurrently renaming directories owned by that same user.
    """

    def __init__(self, clipboard_root, *, incoming_root=None, lock_timeout=5.0):
        self.root = os.path.abspath(os.fspath(clipboard_root))
        self.incoming_root = os.path.abspath(os.fspath(
            incoming_root if incoming_root is not None else os.path.join(self.root, "incoming")))
        self.objects_root = os.path.join(self.root, "objects", "sha256")
        self.manifests_root = os.path.join(self.root, "manifests", "sha256")
        self.pending_root = os.path.join(self.root, "objects", "pending-v2")
        self.lock_path = os.path.join(self.root, "objects", "publication-v2.lock")
        self.lock_timeout = lock_timeout
        self._verified = OrderedDict()
        with _locks_guard:
            self._thread_lock, self._nesting = _locks.setdefault(
                os.path.normcase(self.root), (threading.RLock(), threading.local()))
        for path in (self.objects_root, self.manifests_root, self.pending_root):
            _safe_directory(path, create=True)

    @contextmanager
    def locked(self):
        if not self._thread_lock.acquire(timeout=self.lock_timeout):
            raise ObjectStoreV2Error("store_busy", "object store lock timed out", retryable=True)
        try:
            if getattr(self._nesting, "active", False):
                yield
                return
            _safe_directory(os.path.dirname(self.lock_path))
            if os.path.lexists(self.lock_path):
                _regular(self.lock_path)
            try:
                with resume_v2._process_lock(self.lock_path, timeout=self.lock_timeout):
                    self._nesting.active = True
                    try:
                        yield
                    finally:
                        self._nesting.active = False
            except resume_v2.ResumeJournalError as exc:
                raise ObjectStoreV2Error(exc.code, "object store lock failed", retryable=True) from exc
        finally:
            self._thread_lock.release()

    @staticmethod
    def _hex(value, length=64):
        if (not isinstance(value, str) or len(value) != length
                or any(character not in "0123456789abcdef" for character in value)):
            raise ObjectStoreV2Error("invalid_object", "invalid publication identifier")
        return value

    def object_path(self, digest):
        digest = self._hex(digest)
        return os.path.join(self.objects_root, digest[:2], digest)

    def manifest_path(self, digest):
        digest = self._hex(digest)
        return os.path.join(self.manifests_root, digest[:2], digest + ".json")

    def _pending(self, publication):
        return os.path.join(self.pending_root, self._hex(publication.transfer_id, 32))

    def receipt(self, publication):
        manifest = manifest_v2.validate_manifest(publication.manifest)
        manifest_v2.content_identity(manifest)
        hashes = tuple(sorted({entry["sha256"] for entry in manifest["entries"]
                               if entry["type"] == "file"}))
        if (manifest["manifest_digest"] != publication.manifest_digest
                or publication.object_hashes != hashes):
            raise ObjectStoreV2Error("invalid_object", "publication does not match its manifest")
        return {
            "transfer_id": self._hex(publication.transfer_id, 32),
            "provisional_manifest_digest": self._hex(publication.provisional_manifest_digest),
            "manifest_digest": publication.manifest_digest,
            "item_id": manifest["item_id"], "item_revision": manifest["item_revision"],
        }

    def _matches(self, path, size, digest):
        try:
            info = _regular(path)
            fingerprint = staged_fingerprint(info)
            if info.st_size != size:
                return False
            if self._verified.get(path) == (fingerprint, digest):
                return True
            with _open_regular(path) as handle:
                hasher = hashlib.sha256()
                remaining = size
                while remaining:
                    chunk = handle.read(min(1024 * 1024, remaining))
                    if not chunk:
                        return False
                    hasher.update(chunk)
                    remaining -= len(chunk)
                if (handle.read(1) or hasher.hexdigest() != digest
                        or staged_fingerprint(os.fstat(handle.fileno()), handle) != fingerprint
                        or staged_fingerprint(_regular(path)) != fingerprint):
                    return False
            self._remember(path, fingerprint, digest)
            return True
        except (OSError, ObjectStoreV2Error):
            return False

    def _remember(self, path, fingerprint, digest):
        self._verified[path] = (fingerprint, digest)
        self._verified.move_to_end(path)
        while len(self._verified) > 2 * manifest_v2.MAX_FILES:
            self._verified.popitem(last=False)

    def _install_bytes(self, target, payload, *, repair=False):
        _safe_directory(os.path.dirname(target), create=True)
        if os.path.lexists(target):
            with _open_regular(target) as handle:
                matches = handle.read(len(payload) + 1) == payload
            if matches:
                with _open_regular(target, writable=True) as handle:
                    os.fsync(handle.fileno())
                _fsync_directory(os.path.dirname(target))
                return
            if not repair:
                raise ObjectStoreV2Error("publication_conflict", "stored publication conflicts")
            os.unlink(target)
        descriptor, temporary = tempfile.mkstemp(prefix=".publish-", dir=os.path.dirname(target))
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temporary, target)  # Same-volume, atomic no-replace publication.
            with _open_regular(target, writable=True) as handle:
                os.fsync(handle.fileno())
            _fsync_directory(os.path.dirname(target))
        finally:
            os.unlink(temporary)

    def _pin(self, source, target):
        # Caller fsyncs its already protected writable handle. Opening another
        # writable handle here would conflict with Windows deny-WRITE sharing.
        if os.path.lexists(target):
            _regular(target)
            if os.path.samestat(_regular(source), _regular(target)):
                return
            os.unlink(target)
        os.link(source, target)
        with _open_regular(target) as handle:
            if not os.path.samestat(os.fstat(handle.fileno()), _regular(source)):
                raise ObjectStoreV2Error("stage_changed", "publication pin identity changed")
        _fsync_directory(os.path.dirname(target))

    def publish_staged_transfer(self, staged_result):
        """Publish verified files and bind a transfer; retain all receiver stages."""
        manifest = manifest_v2.validate_manifest(staged_result.finalized_manifest)
        expected = {entry["index"]: entry for entry in manifest["entries"]
                    if entry["type"] == "file"}
        publication = PublishedManifest(
            staged_result.transfer_id, staged_result.provisional_manifest_digest,
            manifest["manifest_digest"], manifest,
            tuple(sorted({entry["sha256"] for entry in expected.values()})))
        binding = manifest_v2.canonical_json_bytes(self.receipt(publication))
        files = tuple(staged_result.files)
        if (len(files) != len(expected)
                or any(type(file.entry_index) is not int for file in files)
                or {file.entry_index for file in files} != set(expected)):
            raise ObjectStoreV2Error("invalid_stage", "stage file set does not match manifest")
        with self.locked():
            stage_dir = os.path.join(self.incoming_root, publication.transfer_id)
            _safe_directory(stage_dir)
            pending = self._pending(publication)
            evidence = {}
            # Prevalidate the complete set before any content is published.
            for file in files:
                source = os.path.abspath(os.fspath(file.path))
                entry = expected[file.entry_index]
                fingerprint = getattr(file, "fingerprint", None)
                if (source != os.path.join(stage_dir, f"{file.entry_index}.verified")
                        or type(file.size) is not int or file.size != entry["size"]
                        or file.sha256 != entry["sha256"]
                        or not isinstance(fingerprint, tuple) or len(fingerprint) != 5
                        or any(type(value) is not int or value < 0 for value in fingerprint)
                        or fingerprint[1] == 0):
                    raise ObjectStoreV2Error("invalid_stage", "stage evidence is invalid")
                info = _regular(source)
                current = staged_fingerprint(info)
                # Our own hardlink operations can change ctime. Only a previously
                # verified, unchanged inode may use this in-process retry evidence.
                trusted = self._verified.get(source) == (current, file.sha256)
                if current != fingerprint and not (trusted and current[:4] == fingerprint[:4]):
                    raise ObjectStoreV2Error("stage_changed", "verified stage fingerprint changed")
                if info.st_size != file.size:
                    raise ObjectStoreV2Error("stage_changed", "verified stage size changed")
                evidence[source] = current
            _safe_directory(pending, create=True)
            self._install_bytes(os.path.join(pending, "receipt.json"), binding)
            for file in files:
                source = os.path.abspath(file.path)
                target = self.object_path(file.sha256)
                _safe_directory(os.path.dirname(target), create=True)
                with ExitStack() as handoff:
                    handle = handoff.enter_context(open_verified_handoff(source))
                    before = staged_fingerprint(os.fstat(handle.fileno()), handle)
                    if before != evidence[source]:
                        raise ObjectStoreV2Error("stage_changed", "stage changed before publication")
                    target_handle = handle
                    if os.path.lexists(target):
                        if not os.path.samestat(_regular(target), os.fstat(handle.fileno())):
                            target_handle = handoff.enter_context(open_verified_handoff(target))
                        if not self._matches(target, file.size, file.sha256):
                            # Never modify an inode another profile/stage may hold.
                            os.unlink(target)
                            target_handle = handle
                    if not os.path.lexists(target):
                        os.link(source, target)
                        if (staged_fingerprint(_regular(target))[:4] != before[:4]
                                or staged_fingerprint(os.fstat(handle.fileno()), handle)[:4] != before[:4]):
                            os.unlink(target)
                            raise ObjectStoreV2Error("stage_changed", "stage changed during publication")
                    os.fsync(handle.fileno())
                    _fsync_directory(os.path.dirname(target))
                    # Dedup can refer to a different inode from .verified. A retained
                    # per-transfer hardlink protects that shared inode until receipt.
                    self._pin(target, os.path.join(pending, file.sha256))
                    os.fsync(target_handle.fileno())
                    os.fsync(handle.fileno())
                    if (staged_fingerprint(_regular(source))[:4] != before[:4]
                            or staged_fingerprint(os.fstat(handle.fileno()), handle)[:4] != before[:4]
                            or not os.path.samestat(_regular(target), os.fstat(target_handle.fileno()))):
                        raise ObjectStoreV2Error("stage_changed", "stage changed during publication")
                    if os.name != "nt":
                        # Link ctime cannot be distinguished from a hidden write
                        # on POSIX. Force hash verification rather than bless it.
                        for path in {source, target}:
                            self._verified.pop(path, None)
                            if not self._matches(path, file.size, file.sha256):
                                raise ObjectStoreV2Error("stage_changed", "handoff content changed")
                    self._remember(target, staged_fingerprint(_regular(target)), file.sha256)
                    self._remember(source, staged_fingerprint(_regular(source)), file.sha256)
            target = self.manifest_path(publication.manifest_digest)
            self._install_bytes(target, manifest_v2.canonical_manifest_bytes(manifest), repair=True)
            with open_verified_handoff(target) as handle:
                self._pin(target, os.path.join(pending, "manifest.json"))
                os.fsync(handle.fileno())
            return publication

    def validate_publication(self, publication, *, require_pending=False):
        if not isinstance(publication, PublishedManifest):
            return False
        try:
            with self.locked():
                receipt = self.receipt(publication)
                if require_pending:
                    binding = manifest_v2.canonical_json_bytes(receipt)
                    with _open_regular(os.path.join(self._pending(publication), "receipt.json")) as handle:
                        if handle.read(len(binding) + 1) != binding:
                            return False
                payload = manifest_v2.canonical_manifest_bytes(publication.manifest)
                with _open_regular(self.manifest_path(publication.manifest_digest)) as handle:
                    if handle.read(len(payload) + 1) != payload:
                        return False
                return all(self._matches(self.object_path(entry["sha256"]),
                                         entry["size"], entry["sha256"])
                           for entry in publication.manifest["entries"] if entry["type"] == "file")
        except ObjectStoreV2Error as exc:
            if exc.retryable:
                raise
            return False
        except (OSError, ValueError):
            return False

    def item_is_publishable(self, item):
        payload = item.get("payload") if isinstance(item, dict) else None
        if not isinstance(payload, dict) or payload.get("encoding") != "object_manifest_v2":
            return False
        try:
            manifest = manifest_v2.validate_manifest(item.get("batch_manifest"))
            if (payload.get("sha256") != manifest["manifest_digest"]
                    or payload.get("size") != manifest["total_size"]
                    or item.get("sha256") != manifest_v2.content_identity(manifest)):
                return False
            return self.validate_publication(PublishedManifest(
                "0" * 32, manifest["manifest_digest"], manifest["manifest_digest"], manifest,
                tuple(sorted({entry["sha256"] for entry in manifest["entries"]
                              if entry["type"] == "file"}))))
        except ObjectStoreV2Error as exc:
            if exc.retryable:
                raise
            return False
        except ValueError:
            return False

    def discard_pending(self, transfer_id, provisional_manifest_digest):
        """Discard only this transfer's pins, not stages, journals or shared objects.

        Returns False if already absent, True if removed. Binding/safety failures
        raise without deleting any entry. Interrupted deletion keeps the receipt
        until all pins are gone, so the same request is safe to retry.
        """
        transfer_id = self._hex(transfer_id, 32)
        digest = self._hex(provisional_manifest_digest)
        with self.locked():
            pending = os.path.join(self.pending_root, transfer_id)
            if not os.path.lexists(pending):
                return False
            _safe_directory(pending)
            with os.scandir(pending) as entries:
                children = list(entries)
            if not children:
                os.rmdir(pending)
                _fsync_directory(self.pending_root)
                return True
            receipt_path = os.path.join(pending, "receipt.json")
            with _open_regular(receipt_path) as handle:
                raw = handle.read(4097)
            try:
                receipt = json.loads(raw)
                if (len(raw) > 4096 or not isinstance(receipt, dict)
                        or set(receipt) != {"transfer_id", "provisional_manifest_digest",
                                           "manifest_digest", "item_id", "item_revision"}
                        or receipt["transfer_id"] != transfer_id
                        or receipt["provisional_manifest_digest"] != digest
                        or not isinstance(receipt["item_id"], str)
                        or not receipt["item_id"] or len(receipt["item_id"]) > 128
                        or type(receipt["item_revision"]) is not int
                        or not 0 <= receipt["item_revision"] <= manifest_v2.UINT64_MAX
                        or raw != manifest_v2.canonical_json_bytes(receipt)):
                    raise ValueError("invalid receipt")
                self._hex(receipt["manifest_digest"])
            except (ValueError, TypeError) as exc:
                raise ObjectStoreV2Error("publication_conflict", "pending transfer binding is invalid") from exc
            for child in children:
                if child.name not in ("receipt.json", "manifest.json"):
                    self._hex(child.name)
                _regular(child.path)
            # Receipt-last is the retry boundary. Names are exclusively generated
            # by publication; no receipt field is ever used as an unlink path.
            for child in children:
                if child.name != "receipt.json":
                    os.unlink(child.path)
            _fsync_directory(pending)
            os.unlink(receipt_path)
            os.rmdir(pending)
            _fsync_directory(self.pending_root)
            self._verified.clear()  # Unlink changes inode ChangeTime/ctime.
            return True

    def _release_committed_pins(self, publication):
        """Store-only: call under root lock AFTER verifying a durable index receipt."""
        pending = self._pending(publication)
        if not os.path.lexists(pending):
            return
        _safe_directory(pending)
        allowed = set(publication.object_hashes) | {"receipt.json", "manifest.json"}
        children = list(os.scandir(pending))
        for child in children:
            if child.name not in allowed:
                raise ObjectStoreV2Error("unsafe_store", "unexpected publication pin")
            _regular(child.path)
        sources = {}
        for entry in publication.manifest["entries"]:
            if entry["type"] == "file":
                sources.setdefault(entry["sha256"], []).append(os.path.join(
                    self.incoming_root, publication.transfer_id, f'{entry["index"]}.verified'))
        for child in children:
            digest = child.name
            target = self.object_path(digest) if digest in publication.object_hashes else None
            previous = self._verified.get(target) if target else None
            if previous is None or os.name != "nt":
                os.unlink(child.path)
                if target:
                    self._verified.pop(target, None)
                continue
            with open_verified_handoff(target) as handle:
                unchanged = staged_fingerprint(os.fstat(handle.fileno()), handle) == previous[0]
                os.unlink(child.path)
                if unchanged:
                    current = staged_fingerprint(os.fstat(handle.fileno()), handle)
                    if current[:4] == previous[0][:4]:
                        self._remember(target, current, digest)
                        for path in sources.get(digest, ()):
                            if self._verified.get(path) == previous:
                                self._remember(path, current, digest)
        os.rmdir(pending)
        _fsync_directory(self.pending_root)

    def collect_garbage(self):
        """Collect only unreferenced single-link objects/manifests. Fail closed.

        Pending pins and receiver .verified links survive failed index commits.
        Interrupted transfers are intentionally retained until receipt or explicit
        cancellation integration; this collector never guesses journal liveness.
        """
        with self.locked():
            referenced_objects, referenced_manifests = set(), set()
            profiles = os.path.join(self.root, "profiles")
            if os.path.lexists(profiles):
                _safe_directory(profiles)
                with os.scandir(profiles) as entries:
                    profile_entries = list(entries)
                for profile in profile_entries:
                    _safe_directory(profile.path)
                    index = os.path.join(profile.path, "index.json")
                    if not os.path.lexists(index):
                        continue
                    with _open_regular(index) as handle:
                        data = json.load(handle)
                    if (not isinstance(data, dict) or data.get("schema_version") not in (0, 1, 2, 3)
                            or not isinstance(data.get("items"), list)):
                        raise ObjectStoreV2Error("unsafe_index", "cannot collect with an unknown index")
                    for item in data["items"]:
                        if not isinstance(item, dict):
                            raise ObjectStoreV2Error("unsafe_index", "invalid index item")
                        if (item.get("payload") or {}).get("encoding") != "object_manifest_v2":
                            continue
                        manifest = manifest_v2.validate_manifest(item.get("batch_manifest"))
                        referenced_manifests.add(manifest["manifest_digest"])
                        referenced_objects.update(entry["sha256"] for entry in manifest["entries"]
                                                  if entry["type"] == "file")
            candidates = []
            for root, references, suffix in ((self.objects_root, referenced_objects, ""),
                                              (self.manifests_root, referenced_manifests, ".json")):
                _safe_directory(root)
                with os.scandir(root) as entries:
                    shards = list(entries)
                for shard in shards:
                    self._hex(shard.name, 2)
                    _safe_directory(shard.path)
                    with os.scandir(shard.path) as entries:
                        children = list(entries)
                    for child in children:
                        if child.name.startswith(".publish-"):
                            _regular(child.path)
                            candidates.append(child.path)
                            continue
                        digest = child.name[:-5] if suffix and child.name.endswith(suffix) else child.name
                        self._hex(digest)
                        if child.name != digest + suffix or digest[:2] != shard.name:
                            raise ObjectStoreV2Error("unsafe_store", "invalid object shard")
                        info = _regular(child.path)
                        if digest not in references and info.st_nlink == 1:
                            candidates.append(child.path)
            for path in candidates:
                os.unlink(path)
                self._verified.pop(path, None)
                _fsync_directory(os.path.dirname(path))
            return len(candidates)
