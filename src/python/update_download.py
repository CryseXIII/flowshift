"""Verified streaming downloader for validated FlowShift release descriptors."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import HTTPRedirectHandler, Request, build_opener

from update_model import (
    ERROR_INVALID_URL,
    ERROR_TOO_MANY_REDIRECTS,
    MAX_INSTALLER_SIZE,
    ReleaseDescriptor,
    UpdateValidationError,
    validate_github_https_url,
)
from version import parse_semver


DOWNLOAD_TIMEOUT_SECONDS = 30
MAX_DOWNLOAD_REDIRECTS = 5
CHUNK_SIZE = 1024 * 1024
PROGRESS_INTERVAL_SECONDS = 0.25
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

ERROR_HTTP = "download_http_error"
ERROR_TIMEOUT = "download_timeout"
ERROR_TRANSPORT = "download_transport_error"
ERROR_SIZE_MISMATCH = "size_mismatch"
ERROR_HASH_MISMATCH = "hash_mismatch"
ERROR_INTERRUPTED = "download_interrupted"
ERROR_INVALID_DESCRIPTOR = "invalid_release_descriptor"
ERROR_FILESYSTEM = "download_filesystem_error"


class DownloadError(OSError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class DownloadRequest:
    url: str
    headers: dict
    timeout: int


@dataclass(slots=True)
class DownloadResponse:
    status: int
    headers: dict
    stream: object

    def read(self, size):
        return self.stream.read(size)

    def close(self):
        close = getattr(self.stream, "close", None)
        if close is not None:
            close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


@dataclass(frozen=True, slots=True)
class DownloadedAsset:
    path: str
    basename: str
    version: str
    size: int
    sha256: str
    reused: bool = False


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


class UrllibDownloadTransport:
    """HTTPS transport that exposes a response stream and never auto-redirects."""

    def __init__(self):
        self._opener = build_opener(_NoRedirect())

    def request(self, request):
        raw = Request(request.url, headers=request.headers, method="GET")
        try:
            response = self._opener.open(raw, timeout=request.timeout)
        except HTTPError as exc:
            response = exc
        return DownloadResponse(response.getcode(), dict(response.headers.items()), response)


def _header(headers, name):
    for key, value in headers.items():
        if str(key).lower() == name.lower():
            return value
    return None


def _emit(logger, code, message):
    if logger is not None:
        try:
            logger(code, message)
        except Exception:
            pass


def _close(response):
    try:
        response.close()
    except Exception:
        pass


def _request(transport, request):
    try:
        response = transport.request(request) if hasattr(transport, "request") else transport(request)
        if not hasattr(response, "status") or not hasattr(response, "headers") or not hasattr(response, "read"):
            raise TypeError("download transport returned an invalid response")
        return response
    except (TimeoutError, socket.timeout) as exc:
        raise DownloadError(ERROR_TIMEOUT, "Installer download timed out") from exc
    except URLError as exc:
        raise DownloadError(ERROR_TRANSPORT, "Installer download transport failed") from exc
    except (OSError, TypeError) as exc:
        if isinstance(exc, DownloadError):
            raise
        raise DownloadError(ERROR_TRANSPORT, "Installer download transport failed") from exc


def _validated_paths(data_dir, version):
    try:
        parsed = parse_semver(version)
    except ValueError as exc:
        raise DownloadError(ERROR_INVALID_DESCRIPTOR, "Release version is invalid") from exc
    if str(parsed) != version:
        raise DownloadError(ERROR_INVALID_DESCRIPTOR, "Release version is not canonical")
    basename = f"FlowShift-Setup-{version}.exe"
    if Path(basename).name != basename:
        raise DownloadError(ERROR_INVALID_DESCRIPTOR, "Generated installer name is unsafe")
    downloads = Path(data_dir) / "updates" / "downloads"
    final = downloads / basename
    part = downloads / f"{basename}.part"
    try:
        root = downloads.resolve()
        if os.path.commonpath((str(root), str(final.resolve()))) != str(root):
            raise DownloadError(ERROR_INVALID_DESCRIPTOR, "Installer path escapes download directory")
    except ValueError as exc:
        raise DownloadError(ERROR_INVALID_DESCRIPTOR, "Installer path is unsafe") from exc
    return downloads, final, part


def _validate_descriptor(descriptor):
    if not isinstance(descriptor, ReleaseDescriptor):
        raise DownloadError(ERROR_INVALID_DESCRIPTOR, "A validated release descriptor is required")
    if (isinstance(descriptor.installer_size, bool)
            or not isinstance(descriptor.installer_size, int)
            or descriptor.installer_size <= 0
            or descriptor.installer_size > MAX_INSTALLER_SIZE):
        raise DownloadError(ERROR_INVALID_DESCRIPTOR, "Installer size is invalid")
    digest = descriptor.installer_sha256
    if (not isinstance(digest, str) or len(digest) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in digest)):
        raise DownloadError(ERROR_INVALID_DESCRIPTOR, "Installer SHA-256 is invalid")
    try:
        validate_github_https_url(descriptor.installer_url)
    except UpdateValidationError as exc:
        raise DownloadError(ERROR_INVALID_URL, str(exc)) from exc


def _file_matches(path, expected_size, expected_hash):
    try:
        if path.stat().st_size != expected_size:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest() == expected_hash.lower()
    except OSError:
        return False


def _progress(downloaded, total, started):
    elapsed = max(time.monotonic() - started, 0.000001)
    rate = downloaded / elapsed
    remaining = max(total - downloaded, 0)
    return {
        "bytes_downloaded": downloaded,
        "bytes_total": total,
        "percentage": round(downloaded * 100.0 / total, 2),
        "bytes_per_second": round(rate, 2),
        "eta_seconds": round(remaining / rate, 2) if rate > 0 else None,
    }


def _open_download(transport, initial_url, headers):
    url = validate_github_https_url(initial_url)
    redirects = 0
    while True:
        response = _request(transport, DownloadRequest(url, headers, DOWNLOAD_TIMEOUT_SECONDS))
        if response.status == 200:
            return response
        if response.status in _REDIRECT_STATUSES:
            if redirects >= MAX_DOWNLOAD_REDIRECTS:
                _close(response)
                raise DownloadError(ERROR_TOO_MANY_REDIRECTS, "Installer redirect limit exceeded")
            location = _header(response.headers, "Location")
            _close(response)
            if not isinstance(location, str) or not location:
                raise DownloadError(ERROR_HTTP, "Installer redirect has no Location")
            try:
                url = validate_github_https_url(urljoin(url, location))
            except UpdateValidationError as exc:
                raise DownloadError(exc.code, str(exc)) from exc
            redirects += 1
            continue
        status = response.status
        _close(response)
        raise DownloadError(ERROR_HTTP, f"Unexpected installer HTTP status {status}")


def download_installer(descriptor, data_dir, current_version, transport=None,
                       progress_callback=None, should_abort=None, logger=None):
    """Stream and verify only ``descriptor.installer_url`` into managed storage."""
    _validate_descriptor(descriptor)
    downloads, final, part = _validated_paths(data_dir, descriptor.version)
    expected_size = descriptor.installer_size
    expected_hash = descriptor.installer_sha256.lower()
    should_abort = should_abort or (lambda: False)
    transport = transport or UrllibDownloadTransport()

    try:
        downloads.mkdir(parents=True, exist_ok=True)
        if final.exists():
            if _file_matches(final, expected_size, expected_hash):
                if progress_callback is not None:
                    progress_callback({
                        "bytes_downloaded": expected_size,
                        "bytes_total": expected_size,
                        "percentage": 100.0,
                        "bytes_per_second": 0.0,
                        "eta_seconds": 0.0,
                    })
                _emit(logger, "download_reused", f"Verified installer reused: {final.name}")
                return DownloadedAsset(str(final), final.name, descriptor.version,
                                       expected_size, expected_hash, True)
            final.unlink()
        part.unlink(missing_ok=True)
    except OSError as exc:
        raise DownloadError(ERROR_FILESYSTEM, "Could not prepare installer download path") from exc

    headers = {
        "Accept": "application/octet-stream",
        "User-Agent": f"FlowShift/{current_version}",
    }
    response = None
    started = time.monotonic()
    downloaded = 0
    last_progress = started
    digest = hashlib.sha256()
    try:
        if should_abort():
            raise DownloadError(ERROR_INTERRUPTED, "Installer download was interrupted")
        response = _open_download(transport, descriptor.installer_url, headers)
        content_length = _header(response.headers, "Content-Length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except (TypeError, ValueError) as exc:
                raise DownloadError(ERROR_SIZE_MISMATCH, "Installer Content-Length is invalid") from exc
            if declared > MAX_INSTALLER_SIZE or declared != expected_size:
                raise DownloadError(ERROR_SIZE_MISMATCH, "Installer Content-Length does not match release")

        if progress_callback is not None:
            progress_callback(_progress(0, expected_size, started))
        with part.open("xb") as output:
            while True:
                if should_abort():
                    raise DownloadError(ERROR_INTERRUPTED, "Installer download was interrupted")
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                if not isinstance(chunk, (bytes, bytearray)):
                    raise DownloadError(ERROR_TRANSPORT, "Installer stream returned invalid data")
                downloaded += len(chunk)
                if downloaded > expected_size or downloaded > MAX_INSTALLER_SIZE:
                    raise DownloadError(ERROR_SIZE_MISMATCH, "Installer exceeds expected size")
                output.write(chunk)
                digest.update(chunk)
                now = time.monotonic()
                if progress_callback is not None and now - last_progress >= PROGRESS_INTERVAL_SECONDS:
                    progress_callback(_progress(downloaded, expected_size, started))
                    last_progress = now
            output.flush()
            os.fsync(output.fileno())

        if downloaded != expected_size:
            raise DownloadError(
                ERROR_SIZE_MISMATCH,
                f"Installer size mismatch: expected {expected_size}, actual {downloaded}",
            )
        actual_hash = digest.hexdigest()
        if actual_hash != expected_hash:
            raise DownloadError(
                ERROR_HASH_MISMATCH,
                f"Installer SHA-256 mismatch: expected {expected_hash}, actual {actual_hash}",
            )
        if should_abort():
            raise DownloadError(ERROR_INTERRUPTED, "Installer download was interrupted")
        os.replace(part, final)
        if progress_callback is not None:
            progress_callback(_progress(downloaded, expected_size, started))
        _emit(logger, "download_verified", f"Installer verified: {final.name}")
        return DownloadedAsset(str(final), final.name, descriptor.version,
                               downloaded, actual_hash, False)
    except DownloadError:
        raise
    except (TimeoutError, socket.timeout) as exc:
        raise DownloadError(ERROR_TIMEOUT, "Installer download timed out") from exc
    except (URLError, OSError) as exc:
        raise DownloadError(ERROR_TRANSPORT, "Installer download failed") from exc
    finally:
        if response is not None:
            _close(response)
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
