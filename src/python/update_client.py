"""Bounded, dependency-injectable GitHub client for stable update discovery."""
from __future__ import annotations

from dataclasses import dataclass
import json
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import HTTPRedirectHandler, Request, build_opener

import version
from update_model import (
    ERROR_DNS,
    ERROR_HTTP,
    ERROR_INVALID_VERSION,
    ERROR_MALFORMED_JSON,
    ERROR_NO_RELEASE,
    ERROR_OFFLINE,
    ERROR_RATE_LIMIT,
    ERROR_RESPONSE_TOO_LARGE,
    ERROR_SERVER,
    ERROR_TIMEOUT,
    ERROR_TOO_MANY_REDIRECTS,
    ERROR_TRANSPORT,
    LATEST_RELEASE_API,
    NoStableRelease,
    STATUS_ERROR,
    STATUS_NO_STABLE_RELEASE,
    STATUS_UPDATE_AVAILABLE,
    STATUS_UP_TO_DATE,
    UpdateResult,
    UpdateValidationError,
    validate_github_https_url,
    validate_manifest_payload,
    validate_release_payload,
)


API_TIMEOUT_SECONDS = 10
MANIFEST_TIMEOUT_SECONDS = 10
API_RESPONSE_MAX_BYTES = 1024 * 1024
MANIFEST_RESPONSE_MAX_BYTES = 256 * 1024
MAX_MANIFEST_REDIRECTS = 5
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


@dataclass(frozen=True, slots=True)
class HttpRequest:
    url: str
    headers: dict
    timeout: int
    max_bytes: int


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: dict
    body: bytes


class ResponseTooLarge(OSError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


class UrllibTransport:
    """One-request transport; redirect decisions remain in the update client."""

    def __init__(self):
        self._opener = build_opener(_NoRedirect())

    def request(self, request):
        raw_request = Request(request.url, headers=request.headers, method="GET")
        try:
            response = self._opener.open(raw_request, timeout=request.timeout)
        except HTTPError as exc:
            response = exc
        with response:
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > request.max_bytes:
                        raise ResponseTooLarge("response exceeds byte limit")
                except ValueError:
                    pass
            body = response.read(request.max_bytes + 1)
            if len(body) > request.max_bytes:
                raise ResponseTooLarge("response exceeds byte limit")
            return HttpResponse(
                status=response.getcode(),
                headers=dict(response.headers.items()),
                body=body,
            )


def _emit(logger, code, message):
    if logger is None:
        return
    try:
        logger(code, message)
    except Exception:
        pass


def _error(code, message, logger, status=STATUS_ERROR):
    _emit(logger, code, message)
    return UpdateResult(status=status, error_code=code, message=message)


def _header(headers, name):
    for key, value in headers.items():
        if str(key).lower() == name.lower():
            return value
    return None


def _request(transport, request):
    try:
        response = transport.request(request) if hasattr(transport, "request") else transport(request)
        if not isinstance(response, HttpResponse):
            raise TypeError("transport must return HttpResponse")
        if len(response.body) > request.max_bytes:
            raise ResponseTooLarge("response exceeds byte limit")
        return response
    except ResponseTooLarge as exc:
        raise UpdateValidationError(ERROR_RESPONSE_TOO_LARGE, str(exc)) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise UpdateValidationError(ERROR_TIMEOUT, "GitHub request timed out") from exc
    except socket.gaierror as exc:
        raise UpdateValidationError(ERROR_DNS, "GitHub host could not be resolved") from exc
    except URLError as exc:
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise UpdateValidationError(ERROR_TIMEOUT, "GitHub request timed out") from exc
        if isinstance(reason, socket.gaierror):
            raise UpdateValidationError(ERROR_DNS, "GitHub host could not be resolved") from exc
        if isinstance(reason, ConnectionError):
            raise UpdateValidationError(ERROR_OFFLINE, "GitHub is unreachable") from exc
        raise UpdateValidationError(ERROR_TRANSPORT, "GitHub transport failed") from exc
    except ConnectionError as exc:
        raise UpdateValidationError(ERROR_OFFLINE, "GitHub is unreachable") from exc
    except OSError as exc:
        raise UpdateValidationError(ERROR_TRANSPORT, "GitHub transport failed") from exc
    except Exception as exc:
        raise UpdateValidationError(ERROR_TRANSPORT, "GitHub transport failed") from exc


def _decode_json(body, context):
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateValidationError(ERROR_MALFORMED_JSON, f"{context} is not valid UTF-8 JSON") from exc


def _check_api_status(response):
    if response.status == 200:
        return
    if response.status == 403:
        raise UpdateValidationError(ERROR_RATE_LIMIT, "GitHub API rate limit or access denial")
    if response.status == 404:
        raise NoStableRelease(ERROR_NO_RELEASE, "GitHub has no latest release")
    if 500 <= response.status <= 599:
        raise UpdateValidationError(ERROR_SERVER, "GitHub API server error")
    raise UpdateValidationError(ERROR_HTTP, f"unexpected GitHub API status {response.status}")


def _fetch_manifest(transport, initial_url, headers):
    url = validate_github_https_url(initial_url)
    redirects = 0
    while True:
        response = _request(transport, HttpRequest(
            url=url,
            headers=headers,
            timeout=MANIFEST_TIMEOUT_SECONDS,
            max_bytes=MANIFEST_RESPONSE_MAX_BYTES,
        ))
        if response.status == 200:
            return response.body
        if response.status in _REDIRECT_STATUSES:
            if redirects >= MAX_MANIFEST_REDIRECTS:
                raise UpdateValidationError(ERROR_TOO_MANY_REDIRECTS, "manifest redirect limit exceeded")
            location = _header(response.headers, "Location")
            if not isinstance(location, str) or not location:
                raise UpdateValidationError(ERROR_HTTP, "manifest redirect has no Location")
            url = validate_github_https_url(urljoin(url, location))
            redirects += 1
            continue
        if response.status == 403:
            raise UpdateValidationError(ERROR_RATE_LIMIT, "manifest download access denied")
        if 500 <= response.status <= 599:
            raise UpdateValidationError(ERROR_SERVER, "GitHub manifest server error")
        raise UpdateValidationError(ERROR_HTTP, f"unexpected manifest status {response.status}")


def discover_stable_release(current_version=version.APP_VERSION, transport=None, logger=None):
    """Discover and validate the fixed repository's latest stable release."""
    try:
        # Validate before making any request and use the same value in User-Agent.
        current = version.parse_semver(current_version)
    except ValueError:
        return _error(ERROR_INVALID_VERSION, "current version is not valid SemVer", logger)

    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": f"FlowShift/{current_version}",
    }
    transport = transport or UrllibTransport()
    try:
        response = _request(transport, HttpRequest(
            url=LATEST_RELEASE_API,
            headers=headers,
            timeout=API_TIMEOUT_SECONDS,
            max_bytes=API_RESPONSE_MAX_BYTES,
        ))
        _check_api_status(response)
        release = validate_release_payload(_decode_json(response.body, "release response"), current)
        manifest = _decode_json(_fetch_manifest(transport, release.manifest_url, headers), "manifest")
        descriptor = validate_manifest_payload(manifest, release)
    except NoStableRelease as exc:
        return _error(exc.code, str(exc), logger, STATUS_NO_STABLE_RELEASE)
    except UpdateValidationError as exc:
        return _error(exc.code, str(exc), logger)

    status = STATUS_UPDATE_AVAILABLE if descriptor.relation == "newer" else STATUS_UP_TO_DATE
    _emit(logger, status, f"stable release {descriptor.tag} validated")
    return UpdateResult(status=status, release=descriptor)
