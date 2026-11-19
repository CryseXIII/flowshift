#!/usr/bin/env python3
"""Build and publish the FlowShift development package for one merged main state.

Runs on Linux (Forgejo Actions runner): Inno Setup 6 runs under Wine, so no
Windows machine is needed to *build*. Testing the Setup stays a Windows step.

Steps (each one fails loudly, nothing half-published stays visible):
1. stage the curated payload with the same file lists as ``build_release.ps1``
   (parsed from it, so there is one list) plus the prebuilt ``webgui/dist`` and
   the ``UPDATE_CHANNEL`` marker;
2. compile ``FlowShift-Setup.exe`` with ISCC;
3. write ``update-manifest.json`` (channel ``development``) and ``SHA256SUMS.txt``;
4. with ``--publish``: create a *draft* prerelease ``v<VERSION>`` in Forgejo,
   upload the three assets, download them again, compare size and SHA-256,
   and only then publish the release. FlowShift ignores drafts.

The Forgejo token comes from the environment (``FORGEJO_TOKEN``, a CI secret);
it is never written to disk or printed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import uuid
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER_NAME = "FlowShift-Setup.exe"
MANIFEST_NAME = "update-manifest.json"
CHECKSUMS_NAME = "SHA256SUMS.txt"
ASSET_NAMES = (INSTALLER_NAME, MANIFEST_NAME, CHECKSUMS_NAME)
CHANNEL = "development"
# Older updaters cannot hand off prerelease versions (update_flowshift.ps1 and
# update_handoff.py accept them since 0.7.0-dev.27).
MINIMUM_UPDATER_VERSION = "0.7.0-dev.27"
DEFAULT_API = "http://forgejo-backend.thegoodhunter.vps:3001/api/v1"
DEFAULT_REPOSITORY = "private-platform/flowshift"
_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-dev\.(0|[1-9][0-9]*))?$")
_FORBIDDEN = ("src/python/service.py", "src/python/test_update_client.py", "webgui/src",
              "node_modules")


class BuildError(RuntimeError):
    pass


def read_version(root=REPO_ROOT):
    version = (Path(root) / "VERSION").read_text(encoding="utf-8").strip()
    if not _VERSION.fullmatch(version):
        raise BuildError(f"VERSION must be X.Y.Z or X.Y.Z-dev.N: {version!r}")
    return version


def payload_lists(build_script):
    """Read ``$rootFiles``, ``$pythonFiles`` and ``$backendFiles`` from build_release.ps1."""
    text = Path(build_script).read_text(encoding="utf-8")
    lists = {}
    for name in ("rootFiles", "pythonFiles", "backendFiles"):
        match = re.search(r"\$" + name + r"\s*=\s*@\((.*?)\)", text, re.S)
        if not match:
            raise BuildError(f"${name} not found in {build_script}")
        lists[name] = re.findall(r"'([^']+)'", match.group(1))
        if not lists[name]:
            raise BuildError(f"${name} is empty in {build_script}")
    return lists


def stage_payload(root, payload, version):
    root = Path(root)
    payload = Path(payload)
    if payload.exists():
        shutil.rmtree(payload)
    lists = payload_lists(root / "packaging" / "build_release.ps1")
    files = list(lists["rootFiles"])
    files += [f"src/python/{name}" for name in lists["pythonFiles"]]
    files += [f"src/python/input_backends/{name}" for name in lists["backendFiles"]]
    for relative in files:
        source = root / relative
        if not source.is_file():
            raise BuildError(f"required release input is missing: {relative}")
        target = payload / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    shutil.copytree(root / "docs", payload / "docs")

    dist = root / "webgui" / "dist"
    for name in ("index.html", "overlay.html", "VERSION"):
        if not (dist / name).is_file():
            raise BuildError(f"webgui/dist/{name} is missing; run npm ci && npm run build in webgui")
    dist_version = (dist / "VERSION").read_text(encoding="utf-8").strip()
    if dist_version != version:
        raise BuildError(f"webgui/dist was built for {dist_version!r}, not {version!r}")
    shutil.copytree(dist, payload / "webgui" / "dist")
    (payload / "UPDATE_CHANNEL").write_text(f"{CHANNEL}\n", encoding="utf-8")

    for relative in _FORBIDDEN:
        if (payload / relative).exists():
            raise BuildError(f"forbidden development payload entry was staged: {relative}")
    return payload


def windows_path(path, wine=True):
    """Wine maps the Linux root to drive Z:."""
    text = str(Path(path).resolve())
    return "Z:" + text.replace("/", "\\") if wine and not text[1:3] == ":\\" else text


def compile_installer(iscc, version, payload, release_dir, root=REPO_ROOT, wine=True,
                      runner=subprocess.run):
    release_dir = Path(release_dir)
    release_dir.mkdir(parents=True, exist_ok=True)
    command = shlex.split(iscc) + [
        f"/DMyAppVersion={version}",
        f"/DSourceRoot={windows_path(payload, wine)}",
        f"/DOutputDir={windows_path(release_dir, wine)}",
        windows_path(Path(root) / "packaging" / "FlowShift.iss", wine),
    ]
    result = runner(command)
    if result.returncode != 0:
        raise BuildError(f"ISCC failed with exit code {result.returncode}")
    installer = release_dir / INSTALLER_NAME
    if not installer.is_file() or installer.stat().st_size <= 0:
        raise BuildError("ISCC produced no FlowShift-Setup.exe")
    return installer


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_metadata(release_dir, version):
    release_dir = Path(release_dir)
    installer = release_dir / INSTALLER_NAME
    size = installer.stat().st_size
    digest = sha256_file(installer)
    manifest = {
        "schema_version": 1,
        "version": version,
        "tag": f"v{version}",
        "channel": CHANNEL,
        "installer": {"name": INSTALLER_NAME, "size": size, "sha256": digest},
        "minimum_updater_version": MINIMUM_UPDATER_VERSION,
    }
    manifest_path = release_dir / MANIFEST_NAME
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (release_dir / CHECKSUMS_NAME).write_text(
        f"{digest}  {INSTALLER_NAME}\n{sha256_file(manifest_path)}  {MANIFEST_NAME}\n",
        encoding="utf-8")
    return manifest


def verify_release_dir(release_dir, version):
    """Re-read everything from disk: manifest, sums and installer must agree."""
    release_dir = Path(release_dir)
    manifest = json.loads((release_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    installer = release_dir / INSTALLER_NAME
    expected = {
        "schema_version": 1, "version": version, "tag": f"v{version}", "channel": CHANNEL,
        "installer": {"name": INSTALLER_NAME, "size": installer.stat().st_size,
                      "sha256": sha256_file(installer)},
        "minimum_updater_version": MINIMUM_UPDATER_VERSION,
    }
    if manifest != expected:
        raise BuildError("update-manifest.json does not match the installer")
    sums = (release_dir / CHECKSUMS_NAME).read_text(encoding="utf-8").splitlines()
    wanted = [f"{expected['installer']['sha256']}  {INSTALLER_NAME}",
              f"{sha256_file(release_dir / MANIFEST_NAME)}  {MANIFEST_NAME}"]
    if sums != wanted:
        raise BuildError("SHA256SUMS.txt does not match the assets")
    return manifest


class Forgejo:
    """Minimal Forgejo release API client; ``opener`` is injectable for tests."""

    def __init__(self, api, repository, token, opener=urlopen):
        self.api = api.rstrip("/")
        self.repository = repository
        self._token = token
        self._open = opener

    def _call(self, method, path, body=None, content_type="application/json", raw=False):
        url = path if path.startswith("http") else f"{self.api}/repos/{self.repository}{path}"
        data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode("utf-8")
        request = Request(url, data=data, method=method, headers={
            "Authorization": f"token {self._token}",
            "Accept": "application/json",
            **({"Content-Type": content_type} if data is not None else {}),
        })
        try:
            with self._open(request, timeout=300) as response:
                payload = response.read()
        except HTTPError as exc:
            if exc.code == 404 and method == "GET":
                return None
            raise BuildError(f"Forgejo {method} {path} failed: HTTP {exc.code}") from None
        if raw:
            return payload
        return json.loads(payload.decode("utf-8")) if payload else {}

    def release_by_tag(self, tag):
        return self._call("GET", f"/releases/tags/{quote(tag)}")

    def create_draft(self, tag, target, title, body):
        return self._call("POST", "/releases", {
            "tag_name": tag, "target_commitish": target, "name": title, "body": body,
            "draft": True, "prerelease": True,
        })

    def delete_asset(self, release_id, asset_id):
        self._call("DELETE", f"/releases/{release_id}/assets/{asset_id}")

    def upload_asset(self, release_id, path):
        boundary = uuid.uuid4().hex
        name = Path(path).name
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"attachment\"; "
                f"filename=\"{name}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
                ).encode("utf-8") + Path(path).read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        return self._call("POST", f"/releases/{release_id}/assets?name={quote(name)}", body,
                          f"multipart/form-data; boundary={boundary}")

    def download(self, url):
        return self._call("GET", url, raw=True)

    def publish(self, release_id):
        return self._call("PATCH", f"/releases/{release_id}", {"draft": False})


def publish(forgejo, release_dir, version, target, log=print):
    """Upload as draft, verify by downloading again, then publish. Idempotent."""
    release_dir = Path(release_dir)
    tag = f"v{version}"
    local = {name: (release_dir / name) for name in ASSET_NAMES}
    hashes = {name: sha256_file(path) for name, path in local.items()}
    release = forgejo.release_by_tag(tag)
    if release is not None and not release.get("draft"):
        if _verified(forgejo, release, local, hashes):
            log(f"{tag} is already published and verified")
            return release
        raise BuildError(f"{tag} is published with different assets; refusing to overwrite")
    if release is None:
        release = forgejo.create_draft(
            tag, target, f"FlowShift {version} (development)",
            f"Development package of main at {target}. Installed automatically by "
            "FlowShift on the development update channel.")
    for asset in release.get("assets") or []:
        forgejo.delete_asset(release["id"], asset["id"])
    for name in ASSET_NAMES:
        forgejo.upload_asset(release["id"], local[name])
    fresh = forgejo.release_by_tag(tag) or release
    if not _verified(forgejo, fresh, local, hashes):
        raise BuildError(f"uploaded assets of {tag} do not match; release stays a draft")
    published = forgejo.publish(release["id"])
    log(f"{tag} published and verified")
    return published


def _verified(forgejo, release, local, hashes):
    assets = {item.get("name"): item for item in release.get("assets") or []}
    if set(assets) != set(ASSET_NAMES):
        return False
    for name, item in assets.items():
        if item.get("size") != local[name].stat().st_size:
            return False
        data = forgejo.download(item["browser_download_url"])
        if data is None or hashlib.sha256(data).hexdigest() != hashes[name]:
            return False
    return True


def git_head(root=REPO_ROOT):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                          capture_output=True, text=True).stdout.strip()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--build-root", default=str(REPO_ROOT / "build" / "dev-package"))
    parser.add_argument("--iscc", default=os.environ.get("ISCC", "iscc"),
                        help="ISCC command, e.g. 'wine /opt/inno/ISCC.exe'")
    parser.add_argument("--no-wine-paths", action="store_true",
                        help="pass native paths (when ISCC runs on Windows)")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--api", default=os.environ.get("FORGEJO_API", DEFAULT_API))
    parser.add_argument("--repository", default=os.environ.get("FORGEJO_REPOSITORY",
                                                               DEFAULT_REPOSITORY))
    args = parser.parse_args(argv)

    version = read_version()
    if "-dev." not in version:
        # Stable versions are published on GitHub after Viktor's approval only.
        print(f"VERSION {version} is stable: no development package is built")
        return 0
    build_root = Path(args.build_root)
    payload = stage_payload(REPO_ROOT, build_root / "payload", version)
    release_dir = build_root / "release"
    if release_dir.exists():
        shutil.rmtree(release_dir)
    compile_installer(args.iscc, version, payload, release_dir, wine=not args.no_wine_paths)
    write_metadata(release_dir, version)
    manifest = verify_release_dir(release_dir, version)
    print(f"development package {version} built: {manifest['installer']['size']} bytes, "
          f"sha256 {manifest['installer']['sha256']}")
    if args.publish:
        token = os.environ.get("FORGEJO_TOKEN")
        if not token:
            raise BuildError("FORGEJO_TOKEN is not set")
        publish(Forgejo(args.api, args.repository, token), release_dir, version, git_head())
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BuildError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
