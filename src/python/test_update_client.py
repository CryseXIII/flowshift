"""Custom-script tests for Phase 1.5 stable GitHub release discovery.

Run: ``python src/python/test_update_client.py``
"""
from dataclasses import FrozenInstanceError
import copy
import json
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import update_client as client
import update_model as model


_failures = []
_checks = 0
HASH = "ab" * 32
_ABSENT = object()


def check(condition, label):
    global _checks
    _checks += 1
    if condition:
        print(f"[PASS] {label}")
    else:
        print(f"[FAIL] {label}")
        _failures.append(label)


class FakeTransport:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def request(self, request):
        self.requests.append(request)
        if not self.outcomes:
            raise AssertionError("unexpected transport request")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def response(status, value=b"", headers=None):
    if isinstance(value, (dict, list)):
        value = json.dumps(value).encode("utf-8")
    elif isinstance(value, str):
        value = value.encode("utf-8")
    return client.HttpResponse(status, headers or {}, value)


def asset_url(tag, name):
    return f"https://github.com/CryseXIII/flowshift/releases/download/{tag}/{name}"


def release_payload(version="0.5.0", size=123456, digest=_ABSENT):
    tag = f"v{version}"
    installer = {
        "name": model.INSTALLER_ASSET,
        "size": size,
        "browser_download_url": asset_url(tag, model.INSTALLER_ASSET),
    }
    if digest is not _ABSENT:
        installer["digest"] = digest
    return {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "html_url": f"https://github.com/CryseXIII/flowshift/releases/tag/{tag}",
        "body": "Release notes",
        "assets": [
            installer,
            {
                "name": model.MANIFEST_ASSET,
                "size": 512,
                "browser_download_url": asset_url(tag, model.MANIFEST_ASSET),
            },
            {
                "name": model.CHECKSUMS_ASSET,
                "size": 128,
                "browser_download_url": asset_url(tag, model.CHECKSUMS_ASSET),
            },
        ],
    }


def manifest_payload(version="0.5.0", size=123456, digest=HASH):
    return {
        "schema_version": 1,
        "version": version,
        "tag": f"v{version}",
        "channel": "stable",
        "installer": {
            "name": model.INSTALLER_ASSET,
            "size": size,
            "sha256": digest,
        },
        "minimum_updater_version": "0.4.0",
    }


def discover(release=None, manifest=None, current="0.4.0", api_status=200, logger=None):
    release = release_payload() if release is None else release
    manifest = manifest_payload() if manifest is None else manifest
    transport = FakeTransport([response(api_status, release), response(200, manifest)])
    result = client.discover_stable_release(current, transport=transport, logger=logger)
    return result, transport


# Successful comparisons all remain validated successes.
newer, newer_transport = discover()
check(newer.status == model.STATUS_UPDATE_AVAILABLE, "newer stable release is update_available")
check(newer.release is not None and newer.release.relation == "newer", "newer relation is retained")
same, _ = discover(release_payload("0.4.0"), manifest_payload("0.4.0"))
check(same.status == model.STATUS_UP_TO_DATE and same.release.relation == "same",
      "same release is successful up_to_date")
older, _ = discover(release_payload("0.3.9"), manifest_payload("0.3.9"))
check(older.status == model.STATUS_UP_TO_DATE and older.release.relation == "older",
      "older remote release is successful up_to_date")
descriptor = newer.release
check((descriptor.current_version, descriptor.version, descriptor.tag) == ("0.4.0", "0.5.0", "v0.5.0"),
      "descriptor carries current version, remote version, and tag")
check(descriptor.release_notes == "Release notes" and descriptor.release_url.endswith("/tag/v0.5.0"),
      "descriptor carries release URL and notes")
check(descriptor.installer_size == 123456 and descriptor.installer_sha256 == HASH,
      "descriptor carries installer size and normalized hash")
check(descriptor.manifest_url.endswith(model.MANIFEST_ASSET)
      and descriptor.checksums_url.endswith(model.CHECKSUMS_ASSET),
      "descriptor carries manifest and checksums URLs")
check(descriptor.minimum_updater_version == "0.4.0", "descriptor carries minimum updater version")
try:
    descriptor.version = "9.9.9"
except (FrozenInstanceError, AttributeError):
    immutable = True
else:
    immutable = False
check(immutable, "release descriptor is immutable-ish")

# The fixed endpoint, identity, limits, and no-installer behavior are observable by tests.
api_request = newer_transport.requests[0]
check(api_request.url == "https://api.github.com/repos/CryseXIII/flowshift/releases/latest",
      "client uses only the fixed releases/latest endpoint")
check(api_request.headers.get("User-Agent") == "FlowShift/0.4.0", "User-Agent is exact")
check(api_request.timeout == client.API_TIMEOUT_SECONDS and api_request.max_bytes == client.API_RESPONSE_MAX_BYTES,
      "API request has timeout and response limit")
check(len(newer_transport.requests) == 2
      and newer_transport.requests[1].url == release_payload()["assets"][1]["browser_download_url"],
      "only manifest browser_download_url from release is fetched")
check(newer_transport.requests[1].timeout == client.MANIFEST_TIMEOUT_SECONDS
      and newer_transport.requests[1].max_bytes == client.MANIFEST_RESPONSE_MAX_BYTES,
      "manifest request has timeout and response limit")

# Invalid local/release versions and non-stable releases never become updates.
invalid_current_transport = FakeTransport([])
invalid_current = client.discover_stable_release("not-semver", invalid_current_transport)
check(invalid_current.status == model.STATUS_ERROR
      and invalid_current.error_code == model.ERROR_INVALID_VERSION
      and not invalid_current_transport.requests, "invalid current version is controlled before transport")
invalid_tag_payload = release_payload()
invalid_tag_payload["tag_name"] = "0.5.0"
invalid_tag, invalid_tag_transport = discover(invalid_tag_payload)
check(invalid_tag.status == model.STATUS_NO_STABLE_RELEASE
      and invalid_tag.error_code == model.ERROR_INVALID_RELEASE
      and len(invalid_tag_transport.requests) == 1, "invalid release tag is ignored as no stable release")
prerelease_payload = release_payload("0.5.0-beta.1")
prerelease_payload["prerelease"] = True
prerelease, prerelease_transport = discover(prerelease_payload, manifest_payload("0.5.0-beta.1"))
check(prerelease.status == model.STATUS_NO_STABLE_RELEASE
      and len(prerelease_transport.requests) == 1, "prerelease is ignored without fetching manifest")
prerelease_tag_payload = release_payload("0.5.0-beta.1")
prerelease_tag, prerelease_tag_transport = discover(
    prerelease_tag_payload, manifest_payload("0.5.0-beta.1"))
check(prerelease_tag.status == model.STATUS_NO_STABLE_RELEASE
      and len(prerelease_tag_transport.requests) == 1,
      "prerelease SemVer tag is ignored even when GitHub flag is false")
draft_payload = release_payload()
draft_payload["draft"] = True
draft, draft_transport = discover(draft_payload)
check(draft.status == model.STATUS_NO_STABLE_RELEASE
      and len(draft_transport.requests) == 1, "draft is ignored without fetching manifest")

# Controlled API and transport failures.
not_found = client.discover_stable_release("0.4.0", FakeTransport([response(404)]))
check(not_found.status == model.STATUS_NO_STABLE_RELEASE and not_found.error_code == model.ERROR_NO_RELEASE,
      "404 is controlled no_release")
rate_limited = client.discover_stable_release("0.4.0", FakeTransport([response(403)]))
check(rate_limited.error_code == model.ERROR_RATE_LIMIT, "403 is controlled rate_limit")
server_error = client.discover_stable_release("0.4.0", FakeTransport([response(500)]))
check(server_error.error_code == model.ERROR_SERVER, "5xx is controlled server_error")
timed_out = client.discover_stable_release("0.4.0", FakeTransport([TimeoutError()]))
check(timed_out.error_code == model.ERROR_TIMEOUT, "timeout is controlled")
dns_error = client.discover_stable_release("0.4.0", FakeTransport([socket.gaierror()]))
check(dns_error.error_code == model.ERROR_DNS, "DNS failure is controlled")
offline = client.discover_stable_release("0.4.0", FakeTransport([ConnectionError()]))
check(offline.error_code == model.ERROR_OFFLINE, "offline connection failure is controlled")
transport_error = client.discover_stable_release("0.4.0", FakeTransport([OSError()]))
check(transport_error.error_code == model.ERROR_TRANSPORT, "other transport failure is controlled")
malformed_release = client.discover_stable_release("0.4.0", FakeTransport([response(200, b"{broken")]))
check(malformed_release.error_code == model.ERROR_MALFORMED_JSON, "malformed release JSON is controlled")
oversized_api = client.discover_stable_release(
    "0.4.0", FakeTransport([response(200, b"x" * (client.API_RESPONSE_MAX_BYTES + 1))]))
check(oversized_api.error_code == model.ERROR_RESPONSE_TOO_LARGE, "API response byte limit is enforced")

# Every exact required asset must appear once.
for missing_name in (model.INSTALLER_ASSET, model.MANIFEST_ASSET, model.CHECKSUMS_ASSET):
    payload = release_payload()
    payload["assets"] = [asset for asset in payload["assets"] if asset["name"] != missing_name]
    result, _ = discover(payload)
    check(result.error_code == model.ERROR_MISSING_ASSET, f"missing exact asset rejected: {missing_name}")
case_mismatch = release_payload()
case_mismatch["assets"][0]["name"] = model.INSTALLER_ASSET.lower()
case_result, _ = discover(case_mismatch)
check(case_result.error_code == model.ERROR_MISSING_ASSET, "asset names are exact and case-sensitive")
for duplicate_name in (model.INSTALLER_ASSET, model.MANIFEST_ASSET, model.CHECKSUMS_ASSET):
    payload = release_payload()
    payload["assets"].append(copy.deepcopy(next(a for a in payload["assets"] if a["name"] == duplicate_name)))
    result, _ = discover(payload)
    check(result.error_code == model.ERROR_DUPLICATE_ASSET, f"duplicate asset rejected: {duplicate_name}")

# Manifest schema and release binding.
malformed_manifest_transport = FakeTransport([response(200, release_payload()), response(200, b"not-json")])
malformed_manifest = client.discover_stable_release("0.4.0", malformed_manifest_transport)
check(malformed_manifest.error_code == model.ERROR_MALFORMED_JSON, "malformed manifest JSON is controlled")

manifest_mutations = (
    ("schema", lambda value: value.update(schema_version=2)),
    ("non-integer schema", lambda value: value.update(schema_version=1.0)),
    ("version", lambda value: value.update(version="0.6.0")),
    ("tag", lambda value: value.update(tag="v0.6.0")),
    ("channel", lambda value: value.update(channel="beta")),
    ("installer name", lambda value: value["installer"].update(name="Other.exe")),
    ("installer size", lambda value: value["installer"].update(size=123457)),
    ("invalid hash", lambda value: value["installer"].update(sha256="ABC123")),
    ("minimum updater", lambda value: value.update(minimum_updater_version="v0.4")),
)
for label, mutate in manifest_mutations:
    manifest = manifest_payload()
    mutate(manifest)
    result, _ = discover(manifest=manifest)
    check(result.error_code == model.ERROR_INVALID_MANIFEST, f"manifest {label} mismatch rejected")

# GitHub's optional installer digest is bound to the manifest when present.
digest_match, _ = discover(release_payload(digest=f"sha256:{HASH}"), manifest_payload(digest=HASH.upper()))
check(digest_match.status == model.STATUS_UPDATE_AVAILABLE
      and digest_match.release.installer_sha256 == HASH, "GitHub digest match accepts case-normalized hex")
digest_absent, _ = discover(release_payload(), manifest_payload())
check(digest_absent.status == model.STATUS_UPDATE_AVAILABLE, "absent GitHub digest is tolerated")
digest_mismatch, _ = discover(release_payload(digest=f"sha256:{'cd' * 32}"), manifest_payload())
check(digest_mismatch.error_code == model.ERROR_DIGEST_MISMATCH, "GitHub digest mismatch is rejected")
invalid_asset_digest, _ = discover(release_payload(digest=f"md5:{HASH}"), manifest_payload())
check(invalid_asset_digest.error_code == model.ERROR_INVALID_RELEASE, "malformed GitHub digest is rejected")

# Size is positive, release-bound, and capped at exactly 2 GiB.
maximum, _ = discover(
    release_payload(size=model.MAX_INSTALLER_SIZE),
    manifest_payload(size=model.MAX_INSTALLER_SIZE),
)
check(maximum.status == model.STATUS_UPDATE_AVAILABLE, "2 GiB installer size boundary is accepted")
too_large, _ = discover(
    release_payload(size=model.MAX_INSTALLER_SIZE + 1),
    manifest_payload(size=model.MAX_INSTALLER_SIZE + 1),
)
check(too_large.error_code == model.ERROR_INVALID_RELEASE, "installer above 2 GiB is rejected")
zero_size, _ = discover(release_payload(size=0), manifest_payload(size=0))
check(zero_size.error_code == model.ERROR_INVALID_RELEASE, "non-positive installer size is rejected")

# URL policy blocks non-HTTPS, local/UNC, credentials, ports, and arbitrary hosts.
allowed_urls = (
    "https://github.com/CryseXIII/flowshift/releases/download/v1.0.0/file",
    "https://api.github.com/repos/CryseXIII/flowshift/releases/latest",
    "https://objects.githubusercontent.com/object",
    "https://release-assets.githubusercontent.com/object?token=x",
)
check(all(model.validate_github_https_url(value) == value for value in allowed_urls),
      "explicit official GitHub host policy accepts required hosts")
rejected_urls = (
    "http://github.com/file",
    "file:///C:/installer.exe",
    "\\\\server\\share\\installer.exe",
    "https://example.com/file",
    "https://github.com.evil.example/file",
    "https://user@github.com/file",
    "https://github.com:444/file",
)
for value in rejected_urls:
    try:
        model.validate_github_https_url(value)
    except model.UpdateValidationError as exc:
        rejected = exc.code == model.ERROR_INVALID_URL
    else:
        rejected = False
    check(rejected, f"unsafe URL rejected: {value}")
unsafe_asset = release_payload()
unsafe_asset["assets"][1]["browser_download_url"] = "https://evil.example/manifest.json"
unsafe_result, unsafe_transport = discover(unsafe_asset)
check(unsafe_result.error_code == model.ERROR_INVALID_URL and len(unsafe_transport.requests) == 1,
      "unsafe manifest URL is rejected before fetch")

# Manifest redirects are manual, bounded, and host-validated at every hop.
redirect_transport = FakeTransport([
    response(200, release_payload()),
    response(302, headers={"Location": "https://release-assets.githubusercontent.com/object"}),
    response(200, manifest_payload()),
])
redirect_result = client.discover_stable_release("0.4.0", redirect_transport)
check(redirect_result.status == model.STATUS_UPDATE_AVAILABLE
      and redirect_transport.requests[-1].url == "https://release-assets.githubusercontent.com/object",
      "official GitHub asset redirect is followed")
for location in ("http://github.com/file", "file:///tmp/file", "//evil.example/file"):
    transport = FakeTransport([
        response(200, release_payload()),
        response(302, headers={"Location": location}),
    ])
    result = client.discover_stable_release("0.4.0", transport)
    check(result.error_code == model.ERROR_INVALID_URL and len(transport.requests) == 2,
          f"unsafe manifest redirect rejected: {location}")
bounded_transport = FakeTransport(
    [response(200, release_payload())]
    + [response(302, headers={"Location": f"https://objects.githubusercontent.com/hop-{index}"})
       for index in range(client.MAX_MANIFEST_REDIRECTS + 1)]
)
bounded = client.discover_stable_release("0.4.0", bounded_transport)
check(bounded.error_code == model.ERROR_TOO_MANY_REDIRECTS
      and len(bounded_transport.requests) == client.MAX_MANIFEST_REDIRECTS + 2,
      "manifest redirect count is bounded")
oversized_manifest_transport = FakeTransport([
    response(200, release_payload()),
    response(200, b"x" * (client.MANIFEST_RESPONSE_MAX_BYTES + 1)),
])
oversized_manifest = client.discover_stable_release("0.4.0", oversized_manifest_transport)
check(oversized_manifest.error_code == model.ERROR_RESPONSE_TOO_LARGE,
      "manifest response byte limit is enforced")

events = []
logged, _ = discover(logger=lambda code, message: events.append((code, message)))
check(logged.status == model.STATUS_UPDATE_AVAILABLE and events[-1][0] == model.STATUS_UPDATE_AVAILABLE,
      "optional logger receives controlled outcomes")


if _failures:
    print(f"\n{len(_failures)} of {_checks} checks failed")
    for failure in _failures:
        print(f" - {failure}")
    raise SystemExit(1)

print(f"\nAll {_checks} update-client checks passed.")
