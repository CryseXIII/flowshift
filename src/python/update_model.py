"""Pure validation models for FlowShift releases (stable on GitHub, development on Forgejo)."""
from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import urlparse

from version import SemVer, parse_release_tag, parse_semver


REPOSITORY = "CryseXIII/flowshift"
LATEST_RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
INSTALLER_ASSET = "FlowShift-Setup.exe"
MANIFEST_ASSET = "update-manifest.json"
CHECKSUMS_ASSET = "SHA256SUMS.txt"

CHANNEL_STABLE = "stable"
CHANNEL_DEVELOPMENT = "development"
CHANNELS = frozenset({CHANNEL_STABLE, CHANNEL_DEVELOPMENT})
# The development channel follows the merged state of branch main in the own
# Forgejo. There is no package build: the source archive of the newest main
# commit is installed with the scripts it contains (install_flowshift.ps1).
# The origin is fixed: only the own network (LAN, WireGuard or Tailscale) reaches it.
FORGEJO_HOST = "forgejo.thegoodhunter.vps"
FORGEJO_ORIGIN = f"http://{FORGEJO_HOST}"
FORGEJO_REPOSITORY = "private-platform/flowshift"
FORGEJO_BRANCH = "main"
FORGEJO_API = f"{FORGEJO_ORIGIN}/api/v1/repos/{FORGEJO_REPOSITORY}"
FORGEJO_BRANCH_API = f"{FORGEJO_API}/branches/{FORGEJO_BRANCH}"
SOURCE_GITHUB = "github"
SOURCE_FORGEJO = "forgejo"
CHANNEL_SOURCES = {
    CHANNEL_STABLE: {"source": SOURCE_GITHUB, "label": f"GitHub {REPOSITORY}",
                     "url": f"https://github.com/{REPOSITORY}/releases"},
    CHANNEL_DEVELOPMENT: {"source": SOURCE_FORGEJO,
                          "label": f"Forgejo {FORGEJO_REPOSITORY} ({FORGEJO_BRANCH})",
                          "url": f"{FORGEJO_ORIGIN}/{FORGEJO_REPOSITORY}/src/branch/{FORGEJO_BRANCH}"},
}
MAX_INSTALLER_SIZE = 2 * 1024 * 1024 * 1024
# Source archives of main are a few MB; the limits stop a broken or hostile
# archive before it fills the disk (zip bomb).
MAX_SOURCE_ARCHIVE_SIZE = 64 * 1024 * 1024
MAX_SOURCE_UNPACKED_SIZE = 1024 * 1024 * 1024
MAX_SOURCE_ENTRIES = 20000
# The first updater that can install a source archive (update_flowshift.ps1
# with package_kind "source"). Older installations update to this once by hand.
SOURCE_UPDATER_MIN_VERSION = "0.7.0-dev.29"
SOURCE_REQUIRED_FILES = ("VERSION", "install_flowshift.ps1", "update_flowshift.ps1",
                         "src/python/tray.py", "webgui/package.json")
_COMMIT_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")

STATUS_UPDATE_AVAILABLE = "update_available"
STATUS_UP_TO_DATE = "up_to_date"
STATUS_NO_STABLE_RELEASE = "no_stable_release"
STATUS_ERROR = "error"

ERROR_RATE_LIMIT = "rate_limit"
ERROR_NO_RELEASE = "no_release"
ERROR_SERVER = "server_error"
ERROR_HTTP = "http_error"
ERROR_TIMEOUT = "timeout"
ERROR_DNS = "dns_error"
ERROR_OFFLINE = "offline"
ERROR_TRANSPORT = "transport_error"
ERROR_RESPONSE_TOO_LARGE = "response_too_large"
ERROR_MALFORMED_JSON = "malformed_json"
ERROR_INVALID_VERSION = "invalid_version"
ERROR_INVALID_RELEASE = "invalid_release"
ERROR_MISSING_ASSET = "missing_asset"
ERROR_DUPLICATE_ASSET = "duplicate_asset"
ERROR_INVALID_URL = "invalid_url"
ERROR_INVALID_MANIFEST = "invalid_manifest"
ERROR_DIGEST_MISMATCH = "digest_mismatch"
ERROR_TOO_MANY_REDIRECTS = "too_many_redirects"
ERROR_SOURCE_UNREACHABLE = "source_unreachable"
ERROR_CREDENTIALS_MISSING = "credentials_missing"
ERROR_CREDENTIALS_REJECTED = "credentials_rejected"
ERROR_INVALID_CHANNEL = "invalid_channel"
ERROR_INVALID_SOURCE = "invalid_source_archive"

_SHA256_PATTERN = re.compile(r"^[0-9A-Fa-f]{64}$")
_OFFICIAL_GITHUB_HOSTS = frozenset({
    "github.com",
    "api.github.com",
    "objects.githubusercontent.com",
})


class UpdateValidationError(ValueError):
    """A validation failure carrying a stable manager-facing error code."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class NoStableRelease(UpdateValidationError):
    """The latest endpoint returned a release that is not stable."""


@dataclass(frozen=True, slots=True)
class ValidatedRelease:
    """Validated release metadata retained while its manifest is fetched."""

    current_version: SemVer
    version: SemVer
    version_text: str
    tag: str
    relation: str
    release_url: str
    release_notes: str
    installer_url: str
    installer_size: int
    installer_digest: str | None
    manifest_url: str
    checksums_url: str
    channel: str = CHANNEL_STABLE


@dataclass(frozen=True, slots=True)
class ReleaseDescriptor:
    """Plain validated release data suitable for a future update manager."""

    current_version: str
    version: str
    tag: str
    relation: str
    release_url: str
    release_notes: str
    installer_url: str
    installer_size: int
    installer_sha256: str
    manifest_url: str
    checksums_url: str
    minimum_updater_version: str
    channel: str = CHANNEL_STABLE


@dataclass(frozen=True, slots=True)
class UpdateResult:
    """Controlled outcome of stable release discovery."""

    status: str
    release: ReleaseDescriptor | None = None
    error_code: str | None = None
    message: str = ""


def is_official_github_host(host):
    if not isinstance(host, str):
        return False
    host = host.rstrip(".").lower()
    return host in _OFFICIAL_GITHUB_HOSTS or host.endswith(".githubusercontent.com")


def validate_github_https_url(value):
    """Return an HTTPS GitHub URL or reject credentials, ports, and other hosts."""
    if not isinstance(value, str) or not value:
        raise UpdateValidationError(ERROR_INVALID_URL, "GitHub URL is missing")
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError as exc:
        raise UpdateValidationError(ERROR_INVALID_URL, "GitHub URL is malformed") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or not is_official_github_host(parsed.hostname)
    ):
        raise UpdateValidationError(ERROR_INVALID_URL, "URL is not an approved GitHub HTTPS URL")
    return value


def is_forgejo_host(host):
    return isinstance(host, str) and host.rstrip(".").lower() == FORGEJO_HOST


def validate_forgejo_url(value):
    """Return a URL on the fixed Forgejo origin; credentials in URLs are rejected."""
    if not isinstance(value, str) or not value:
        raise UpdateValidationError(ERROR_INVALID_URL, "Forgejo URL is missing")
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError as exc:
        raise UpdateValidationError(ERROR_INVALID_URL, "Forgejo URL is malformed") from exc
    scheme = parsed.scheme.lower()
    if (
        scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 80 if scheme == "http" else 443)
        or not is_forgejo_host(parsed.hostname)
    ):
        raise UpdateValidationError(ERROR_INVALID_URL, "URL is not on the approved Forgejo origin")
    return value


def validate_channel(channel):
    if channel not in CHANNELS:
        raise UpdateValidationError(ERROR_INVALID_CHANNEL, "update channel is not supported")
    return channel


def validate_source_url(value, channel=CHANNEL_STABLE):
    """Validate a release URL against the single source allowed for ``channel``."""
    if validate_channel(channel) == CHANNEL_DEVELOPMENT:
        return validate_forgejo_url(value)
    return validate_github_https_url(value)


def _required_string(mapping, key, code, context):
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise UpdateValidationError(code, f"{context} {key} must be a non-empty string")
    return value


def _parse_current_version(value):
    if isinstance(value, SemVer):
        return value
    try:
        return parse_semver(value)
    except ValueError as exc:
        raise UpdateValidationError(ERROR_INVALID_VERSION, "current version is not valid SemVer") from exc


def _asset_by_name(assets, name):
    matches = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == name]
    if not matches:
        raise UpdateValidationError(ERROR_MISSING_ASSET, f"required release asset is missing: {name}")
    if len(matches) != 1:
        raise UpdateValidationError(ERROR_DUPLICATE_ASSET, f"release asset is duplicated: {name}")
    return matches[0]


def _asset_url(asset, name, channel=CHANNEL_STABLE):
    try:
        return validate_source_url(asset.get("browser_download_url"), channel)
    except UpdateValidationError as exc:
        raise UpdateValidationError(exc.code, f"invalid URL for release asset {name}") from exc


def validate_release_payload(payload, current_version, channel=CHANNEL_STABLE):
    """Validate one release object and select its exact assets.

    The stable channel accepts only non-prerelease GitHub releases. The
    development channel also accepts Forgejo prereleases (``v0.7.0-dev.27``).
    """
    channel = validate_channel(channel)
    current = _parse_current_version(current_version)
    if not isinstance(payload, dict):
        raise UpdateValidationError(ERROR_INVALID_RELEASE, "release response must be a JSON object")
    if not isinstance(payload.get("draft"), bool) or not isinstance(payload.get("prerelease"), bool):
        raise UpdateValidationError(ERROR_INVALID_RELEASE, "release stability flags are invalid")
    if payload["draft"]:
        raise NoStableRelease(ERROR_INVALID_RELEASE, "latest release is a draft")
    if payload["prerelease"] and channel == CHANNEL_STABLE:
        raise NoStableRelease(ERROR_INVALID_RELEASE, "latest release is a prerelease")

    tag = payload.get("tag_name")
    try:
        remote = parse_release_tag(tag)
    except ValueError as exc:
        raise NoStableRelease(ERROR_INVALID_RELEASE, "latest release tag is not strict v<SemVer>") from exc
    if channel == CHANNEL_DEVELOPMENT and (remote.build or str(remote) != tag[1:]):
        raise NoStableRelease(ERROR_INVALID_RELEASE, "release tag is not canonical v<SemVer>")
    if not remote.is_stable and channel == CHANNEL_STABLE:
        raise NoStableRelease(ERROR_INVALID_RELEASE, "latest release tag is not stable")

    version_text = tag[1:]
    release_url = validate_source_url(
        _required_string(payload, "html_url", ERROR_INVALID_RELEASE, "release"), channel
    )
    notes = payload.get("body")
    if notes is None:
        notes = ""
    if not isinstance(notes, str):
        raise UpdateValidationError(ERROR_INVALID_RELEASE, "release body must be text or null")
    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise UpdateValidationError(ERROR_INVALID_RELEASE, "release assets must be a list")

    installer = _asset_by_name(assets, INSTALLER_ASSET)
    manifest = _asset_by_name(assets, MANIFEST_ASSET)
    checksums = _asset_by_name(assets, CHECKSUMS_ASSET)
    installer_size = installer.get("size")
    if (
        isinstance(installer_size, bool)
        or not isinstance(installer_size, int)
        or installer_size <= 0
        or installer_size > MAX_INSTALLER_SIZE
    ):
        raise UpdateValidationError(ERROR_INVALID_RELEASE, "installer asset size is invalid")

    digest = installer.get("digest")
    if digest is None:
        installer_digest = None
    elif isinstance(digest, str) and digest.startswith("sha256:") and _SHA256_PATTERN.fullmatch(digest[7:]):
        installer_digest = digest[7:].lower()
    else:
        raise UpdateValidationError(ERROR_INVALID_RELEASE, "installer asset digest is invalid")

    if remote > current:
        relation = "newer"
    elif remote < current:
        relation = "older"
    else:
        relation = "same"
    return ValidatedRelease(
        current_version=current,
        version=remote,
        version_text=version_text,
        tag=tag,
        relation=relation,
        release_url=release_url,
        release_notes=notes,
        installer_url=_asset_url(installer, INSTALLER_ASSET, channel),
        installer_size=installer_size,
        installer_digest=installer_digest,
        manifest_url=_asset_url(manifest, MANIFEST_ASSET, channel),
        checksums_url=_asset_url(checksums, CHECKSUMS_ASSET, channel),
        channel=channel,
    )


def validate_commit_id(value):
    """Return a full lowercase Git commit ID (SHA-1 or SHA-256 repositories)."""
    if not isinstance(value, str) or not _COMMIT_PATTERN.fullmatch(value):
        raise UpdateValidationError(ERROR_INVALID_RELEASE, "Forgejo commit ID is invalid")
    return value


def forgejo_version_url(commit):
    return f"{FORGEJO_API}/raw/VERSION?ref={validate_commit_id(commit)}"


def forgejo_archive_url(commit):
    return f"{FORGEJO_API}/archive/{validate_commit_id(commit)}.zip"


def forgejo_commit_url(commit):
    return f"{FORGEJO_ORIGIN}/{FORGEJO_REPOSITORY}/commit/{validate_commit_id(commit)}"


def parse_branch_payload(payload):
    """Return ``(commit_id, first message line)`` of the Forgejo branch response."""
    if not isinstance(payload, dict) or payload.get("name") != FORGEJO_BRANCH:
        raise UpdateValidationError(ERROR_INVALID_RELEASE, "Forgejo branch response is invalid")
    commit = payload.get("commit")
    if not isinstance(commit, dict):
        raise UpdateValidationError(ERROR_INVALID_RELEASE, "Forgejo branch has no commit")
    commit_id = validate_commit_id(commit.get("id"))
    message = commit.get("message")
    summary = message.strip().splitlines()[0][:200] if isinstance(message, str) and message.strip() else ""
    return commit_id, summary


def parse_source_version(text):
    """Validate the VERSION file of main: canonical SemVer without build metadata."""
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UpdateValidationError(ERROR_INVALID_VERSION, "VERSION is not UTF-8") from exc
    if not isinstance(text, str):
        raise UpdateValidationError(ERROR_INVALID_VERSION, "VERSION is missing")
    value = text.lstrip("﻿").strip()
    try:
        parsed = parse_semver(value)
    except ValueError as exc:
        raise UpdateValidationError(ERROR_INVALID_VERSION, "VERSION of main is not SemVer") from exc
    if parsed.build or str(parsed) != value:
        raise UpdateValidationError(ERROR_INVALID_VERSION, "VERSION of main is not canonical")
    return parsed


def _unsafe_member(name):
    if not name or "\\" in name or name.startswith("/") or ":" in name or "\x00" in name:
        return True
    return any(part in ("", ".", "..") for part in name.rstrip("/").split("/"))


def validate_source_archive(archive, commit, version):
    """Check a Forgejo source archive of ``commit`` before anything uses it.

    ``archive`` is an open :class:`zipfile.ZipFile`. Git stores the commit ID
    as the ZIP comment, which binds the bytes to the commit that was checked.
    All members must sit below one top folder without absolute paths, ``..``
    or drive letters; the size limits stop zip bombs; VERSION must match.
    Returns the name of the top folder.
    """
    commit = validate_commit_id(commit)
    expected = str(version)
    comment = archive.comment.decode("ascii", "replace").strip()
    if comment != commit:
        raise UpdateValidationError(ERROR_INVALID_SOURCE, "source archive belongs to another commit")
    members = archive.infolist()
    if not members or len(members) > MAX_SOURCE_ENTRIES:
        raise UpdateValidationError(ERROR_INVALID_SOURCE, "source archive entry count is invalid")
    top = None
    unpacked = 0
    names = set()
    for member in members:
        name = member.filename
        if _unsafe_member(name):
            raise UpdateValidationError(ERROR_INVALID_SOURCE, "source archive contains an unsafe path")
        first = name.split("/", 1)[0]
        if top is None:
            top = first
        elif first != top:
            raise UpdateValidationError(ERROR_INVALID_SOURCE, "source archive has more than one top folder")
        unpacked += member.file_size
        if unpacked > MAX_SOURCE_UNPACKED_SIZE:
            raise UpdateValidationError(ERROR_INVALID_SOURCE, "source archive unpacks too large")
        names.add(name)
    for required in SOURCE_REQUIRED_FILES:
        if f"{top}/{required}" not in names:
            raise UpdateValidationError(ERROR_INVALID_SOURCE, f"source archive lacks {required}")
    with archive.open(f"{top}/VERSION") as stream:
        packaged = parse_source_version(stream.read(256))
    if str(packaged) != expected:
        raise UpdateValidationError(ERROR_INVALID_SOURCE, "source archive VERSION differs from main")
    return top


def development_descriptor(current_version, version, commit, summary, size, sha256):
    """ReleaseDescriptor of a verified source archive of Forgejo main."""
    current = _parse_current_version(current_version)
    remote = parse_source_version(str(version))
    if (isinstance(size, bool) or not isinstance(size, int) or size <= 0
            or size > MAX_SOURCE_ARCHIVE_SIZE):
        raise UpdateValidationError(ERROR_INVALID_SOURCE, "source archive size is invalid")
    if not isinstance(sha256, str) or not _SHA256_PATTERN.fullmatch(sha256):
        raise UpdateValidationError(ERROR_INVALID_SOURCE, "source archive SHA-256 is invalid")
    relation = "newer" if remote > current else "older" if remote < current else "same"
    notes = f"main {commit[:10]}" + (f": {summary}" if summary else "")
    return ReleaseDescriptor(
        current_version=str(current),
        version=str(remote),
        tag=f"v{remote}",
        relation=relation,
        release_url=forgejo_commit_url(commit),
        release_notes=notes,
        installer_url=forgejo_archive_url(commit),
        installer_size=size,
        installer_sha256=sha256.lower(),
        manifest_url=forgejo_version_url(commit),
        checksums_url=FORGEJO_BRANCH_API,
        minimum_updater_version=SOURCE_UPDATER_MIN_VERSION,
        channel=CHANNEL_DEVELOPMENT,
    )


def validate_manifest_payload(payload, release):
    """Validate schema-v1 manifest data against its originating release object."""
    if not isinstance(payload, dict):
        raise UpdateValidationError(ERROR_INVALID_MANIFEST, "manifest must be a JSON object")
    schema_version = payload.get("schema_version")
    if type(schema_version) is not int or schema_version != 1:
        raise UpdateValidationError(ERROR_INVALID_MANIFEST, "manifest schema_version must be 1")
    if payload.get("version") != release.version_text:
        raise UpdateValidationError(ERROR_INVALID_MANIFEST, "manifest version does not match release")
    if payload.get("tag") != release.tag:
        raise UpdateValidationError(ERROR_INVALID_MANIFEST, "manifest tag does not match release")
    allowed_channels = ({CHANNEL_STABLE} if release.channel == CHANNEL_STABLE
                        else {CHANNEL_STABLE, CHANNEL_DEVELOPMENT})
    if payload.get("channel") not in allowed_channels:
        raise UpdateValidationError(ERROR_INVALID_MANIFEST,
                                    f"manifest channel is not allowed for {release.channel}")

    installer = payload.get("installer")
    if not isinstance(installer, dict):
        raise UpdateValidationError(ERROR_INVALID_MANIFEST, "manifest installer must be an object")
    if installer.get("name") != INSTALLER_ASSET:
        raise UpdateValidationError(ERROR_INVALID_MANIFEST, "manifest installer name is invalid")
    size = installer.get("size")
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or size <= 0
        or size > MAX_INSTALLER_SIZE
        or size != release.installer_size
    ):
        raise UpdateValidationError(ERROR_INVALID_MANIFEST, "manifest installer size is invalid")
    digest = installer.get("sha256")
    if not isinstance(digest, str) or not _SHA256_PATTERN.fullmatch(digest):
        raise UpdateValidationError(ERROR_INVALID_MANIFEST, "manifest installer sha256 is invalid")
    digest = digest.lower()
    if release.installer_digest is not None and digest != release.installer_digest:
        raise UpdateValidationError(ERROR_DIGEST_MISMATCH, "manifest hash differs from release asset digest")

    minimum = payload.get("minimum_updater_version")
    try:
        parsed_minimum = parse_semver(minimum)
    except ValueError as exc:
        raise UpdateValidationError(ERROR_INVALID_MANIFEST, "minimum_updater_version is invalid") from exc

    return ReleaseDescriptor(
        current_version=str(release.current_version),
        version=release.version_text,
        tag=release.tag,
        relation=release.relation,
        release_url=release.release_url,
        release_notes=release.release_notes,
        installer_url=release.installer_url,
        installer_size=size,
        installer_sha256=digest,
        manifest_url=release.manifest_url,
        checksums_url=release.checksums_url,
        minimum_updater_version=str(parsed_minimum),
        channel=release.channel,
    )
