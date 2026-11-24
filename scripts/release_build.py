#!/usr/bin/env python3
"""Build the GitHub release assets of FlowShift on the Linux server, without GitHub Actions.

GitHub Actions is not available for this repository (account locked for billing, Viktor
24.11.: nothing on GitHub may cost money). This module does what the former tag workflow
did, on the publishing server (LXC 218, user without root), and is driven by
``publish_github_release.py assets``:

* toolchain (cached, every download pinned by SHA-256):
  - Wine 10.0 from the signed Debian trixie archive (amd64 packages plus the i386 PE DLLs
    of ``libwine:i386`` for the 32-bit Inno Setup compiler), unpacked into a user folder;
  - Inno Setup 6.7.4 (official jrsoftware release), installed into a private Wine prefix
    with Wine's headless ("null") graphics driver;
  - Node.js 24.18.1 (nodejs.org) and npm 12.0.2, as pinned in webgui/package.json.
* build from a clean worktree of the release tag: Python unit tests, WebGUI ``npm ci``,
  ``npm audit``, ``npm test`` and ``npm run build``, the curated payload with the file lists
  of ``packaging/build_release.ps1`` and the content rules of
  ``packaging/test_release_packaging.ps1``, ``ISCC.exe`` on ``packaging/FlowShift.iss``,
  then update-manifest.json and SHA256SUMS.txt exactly as ``build_release.ps1`` writes them.

What only Windows can show (Setup.exe really installs, PowerShell scripts, imports of the
Windows-only modules) stays a manual check for Viktor.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile
from urllib.request import Request, urlopen

INSTALLER_NAME = "FlowShift-Setup.exe"
MANIFEST_NAME = "update-manifest.json"
CHECKSUMS_NAME = "SHA256SUMS.txt"
ASSET_NAMES = (INSTALLER_NAME, MANIFEST_NAME, CHECKSUMS_NAME)
MINIMUM_UPDATER_VERSION = "0.4.0"  # default of build_release.ps1
MAX_INSTALLER_BYTES = 2 * 1024 * 1024 * 1024
# The full discovery has known Windows-only failures on Linux and already runs for every
# merge in the Forgejo test runner; the build itself gates on the modules that decide
# whether an installed FlowShift can take this release.
RELEASE_TEST_MODULES = (
    "test_update_client", "test_update_safety", "test_update_runtime", "test_update_handoff",
    "test_update_manager", "test_update_channel", "test_update_stable_path",
    "test_version_config", "test_webgui_health", "test_usb_package", "test_install_lock",
)
_STABLE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")

DEBIAN_POOL = "https://deb.debian.org/debian/"
# (pool path, SHA-256 from the signed trixie index, 24.11.2026). A Debian point release
# that replaces one of them makes the download fail with a clear message; then take the new
# file name and hash from the signed index (apt-cache show <paket>) and update the pin.
WINE_PACKAGES = (
    ("pool/main/w/wine/libwine_10.0~repack-6_amd64.deb",
     "702c99d2fbaef2a4966667e0cdabd477ed1754c1b3b5d4476606eaf5fd6e1444"),
    ("pool/main/w/wine/libwine_10.0~repack-6_i386.deb",
     "fef4802a6553a17eaa40003333a86fc697440d1d21c5824f2dbd1c444f01527b"),
    ("pool/main/w/wine/wine64_10.0~repack-6_amd64.deb",
     "4cdf8319d873c13210815c1aac18859a56ea21b0b2d35a3388dab881464ed488"),
    ("pool/main/libu/libunwind/libunwind8_1.8.1-0.1_amd64.deb",
     "db21a86dd05c93413f0ef36282a9c64d32410d240f332273a53be1408bec1f62"),
    ("pool/main/libz/libz-mingw-w64/libz-mingw-w64_1.3.1+dfsg-2_all.deb",
     "c4e6d6bfabe8b6d12c797736a3d762d83513b49f488d1aaf61d4526387a2dd3e"),
)
INNO_URL = "https://github.com/jrsoftware/issrc/releases/download/is-6_7_4/innosetup-6.7.4.exe"
INNO_SHA256 = "a549929319f9120716677c685535ca4671e4b937b5fdc87e2585f32d8eb30e8c"
NODE_VERSION = "24.18.1"
NODE_URL = f"https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-linux-x64.tar.xz"
NODE_SHA256 = "d6c664df3f3f61458e8c277585571328522d705166723a7c7823a9253a4d15a0"
NPM_VERSION = "12.0.2"
TOOLCHAIN_ID = "wine-10.0~repack-6+inno-6.7.4+node-24.18.1+npm-12.0.2+1"

# Debian's wineserver only searches fixed absolute folders for its NLS tables. The unused
# entry /usr/local/share/wine/nls is rewritten (same length, NUL padded) to a link in the
# home directory that points at the unpacked tables; no root and no system folder needed.
_WINESERVER_NLS = b"/usr/local/share/wine/nls\0"


class BuildError(RuntimeError):
    pass


def default_tools_root():
    return Path(os.environ.get("FLOWSHIFT_RELEASE_TOOLS",
                               Path.home() / ".cache" / "flowshift-release-tools"))


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url, sha256, target, log):
    """Download once into ``target``; the file is used only if its SHA-256 matches the pin."""
    target = Path(target)
    if target.is_file() and sha256_file(target) == sha256:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    log(f"download {url}")
    partial = target.with_name(target.name + ".part")
    digest = hashlib.sha256()
    with urlopen(Request(url, headers={"User-Agent": "FlowShift-release-build"}),  # noqa: S310
                 timeout=300) as response, open(partial, "wb") as handle:
        for block in iter(lambda: response.read(1024 * 1024), b""):
            digest.update(block)
            handle.write(block)
    if digest.hexdigest() != sha256:
        partial.unlink(missing_ok=True)
        raise BuildError(f"{url}: SHA-256 {digest.hexdigest()} differs from the pin {sha256}")
    os.replace(partial, target)
    return target


def run(args, cwd=None, env=None, log=None, timeout=3600, label=None):
    """Run a build step; its full output goes to the log file, a failure names the step."""
    completed = subprocess.run([str(a) for a in args], cwd=str(cwd) if cwd else None, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                               errors="replace", timeout=timeout)
    if log:
        log(f"$ {label or ' '.join(str(a) for a in args)}  (exit {completed.returncode})",
            completed.stdout)
    if completed.returncode != 0:
        tail = "\n".join(completed.stdout.splitlines()[-25:])
        raise BuildError(f"{label or args[0]} failed with exit {completed.returncode}:\n{tail}")
    return completed.stdout


# -- toolchain -----------------------------------------------------------------------------------
@dataclass
class Toolchain:
    root: Path

    @property
    def wine_root(self):
        return self.root / "wine-root"

    @property
    def prefix(self):
        return self.root / "wine-prefix"

    @property
    def iscc(self):
        return "C:\\InnoSetup6\\ISCC.exe"

    @property
    def node_bin(self):
        return self.root / f"node-v{NODE_VERSION}-linux-x64" / "bin"

    @property
    def npm_prefix(self):
        return self.root / f"npm-{NPM_VERSION}"

    @property
    def nls_link(self):
        return Path(os.environ.get("FLOWSHIFT_WINE_NLS_LINK", Path.home() / "fsw-nls"))

    def wine_env(self):
        lib = self.wine_root / "usr/lib/x86_64-linux-gnu"
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("WINE", "DISPLAY", "WAYLAND"))}
        env.update({
            "WINEPREFIX": str(self.prefix), "WINEARCH": "win64", "WINEDEBUG": "-all",
            "WINESERVER": str(self.wine_root / "usr/lib/wine/wineserver"),
            "WINEDLLPATH": str(lib / "wine"), "WINEDLLOVERRIDES": "mscoree,mshtml=",
            "LD_LIBRARY_PATH": str(lib),
        })
        return env

    def wine(self, *args, log=None, timeout=900, label=None):
        return run([self.wine_root / "usr/lib/wine/wine64", *args], env=self.wine_env(),
                   log=log, timeout=timeout, label=label)

    def stop_wine(self):
        subprocess.run([str(self.wine_root / "usr/lib/wine/wineserver64"), "-k"],
                       env=self.wine_env(), capture_output=True, timeout=60)

    def node_env(self):
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join([str(self.npm_prefix / "bin"), str(self.node_bin),
                                       env.get("PATH", "")])
        env["npm_config_update_notifier"] = "false"
        env["npm_config_fund"] = "false"
        return env


def _unpack_deb(deb, target):
    run(["dpkg-deb", "-x", deb, target], label=f"dpkg-deb -x {Path(deb).name}")


def _install_wine(tools, downloads, log):
    root = tools.wine_root
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    i386_tree = tools.root / "libwine-i386"
    shutil.rmtree(i386_tree, ignore_errors=True)
    for pool_path, sha256 in WINE_PACKAGES:
        deb = download(DEBIAN_POOL + pool_path, sha256, downloads / PurePosixPath(pool_path).name, log)
        _unpack_deb(deb, i386_tree if deb.name.endswith("_i386.deb") else root)
    # New WoW64: the 64-bit Wine runs 32-bit programs with the i386 PE DLLs alone.
    source = i386_tree / "usr/lib/i386-linux-gnu/wine/i386-windows"
    target = root / "usr/lib/x86_64-linux-gnu/wine/i386-windows"
    target.mkdir(parents=True, exist_ok=True)
    for dll in source.iterdir():
        shutil.copy2(dll, target / dll.name)
    shutil.rmtree(i386_tree)
    # Debian's wineserver wrapper names an absolute path; point it at the unpacked binary.
    wrapper = root / "usr/lib/wine/wineserver"
    wrapper.write_text('#!/bin/sh -e\nexec "$(dirname "$0")/wineserver64" -p0 "$@"\n',
                       encoding="utf-8")
    wrapper.chmod(0o755)
    link = tools.nls_link
    new = os.fsencode(str(link)) + b"\0"
    if not link.is_absolute() or len(new) > len(_WINESERVER_NLS):
        raise BuildError(f"NLS link {link} must be absolute and at most "
                         f"{len(_WINESERVER_NLS) - 1} bytes (set FLOWSHIFT_WINE_NLS_LINK)")
    server = root / "usr/lib/wine/wineserver64"
    data = server.read_bytes()
    if data.count(_WINESERVER_NLS) != 1:
        raise BuildError("wineserver64 does not contain the expected NLS search path once")
    server.write_bytes(data.replace(_WINESERVER_NLS, new.ljust(len(_WINESERVER_NLS), b"\0")))
    nls = root / "usr/share/wine/nls"
    if link.is_symlink() or link.exists():
        if not link.is_symlink():
            raise BuildError(f"{link} exists and is not a link; remove it or set FLOWSHIFT_WINE_NLS_LINK")
        link.unlink()
    link.symlink_to(nls)


def _install_inno(tools, downloads, log):
    setup = download(INNO_URL, INNO_SHA256, downloads / PurePosixPath(INNO_URL).name, log)
    if tools.prefix.exists():
        shutil.rmtree(tools.prefix)
    # zlib1.dll (Debian libz-mingw-w64) is a native DLL that user32 imports; it has to be in
    # place before the first Wine start.
    windows = tools.prefix / "drive_c/windows"
    for folder, mingw in (("system32", "x86_64-w64-mingw32"), ("syswow64", "i686-w64-mingw32")):
        (windows / folder).mkdir(parents=True)
        shutil.copy2(tools.wine_root / "usr" / mingw / "lib/zlib1.dll", windows / folder / "zlib1.dll")
    tools.wine("wineboot", "-i", log=log, label="wineboot -i")
    # Headless: Wine's null graphics driver lets the Inno wizard create its (hidden) windows.
    tools.wine("reg", "add", "HKCU\\Software\\Wine\\Drivers", "/v", "Graphics", "/d", "null",
               "/f", log=log, label="reg add Graphics=null")
    tools.wine(setup, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER",
               "/NOICONS", "/DIR=C:\\InnoSetup6", log=log, label="Inno Setup 6.7.4 install")
    if not (tools.prefix / "drive_c/InnoSetup6/ISCC.exe").is_file():
        raise BuildError("ISCC.exe is missing after the Inno Setup installation")


def _install_node(tools, downloads, log):
    archive = download(NODE_URL, NODE_SHA256, downloads / PurePosixPath(NODE_URL).name, log)
    shutil.rmtree(tools.node_bin.parent, ignore_errors=True)
    with tarfile.open(archive) as tar:
        tar.extractall(tools.root, filter="data")
    shutil.rmtree(tools.npm_prefix, ignore_errors=True)
    run([tools.node_bin / "npm", "install", "--global", "--prefix", tools.npm_prefix,
         f"npm@{NPM_VERSION}"], env=tools.node_env(), log=log, label=f"npm install npm@{NPM_VERSION}")


def _print_line(line, detail=None):
    print(line, flush=True)


def ensure_toolchain(root=None, log=_print_line):
    """Install (once) or reuse the pinned toolchain; returns a ready :class:`Toolchain`."""
    tools = Toolchain(Path(root) if root else default_tools_root())
    tools.root.mkdir(parents=True, exist_ok=True)
    marker = tools.root / "toolchain.id"
    downloads = tools.root / "downloads"
    if not (marker.is_file() and marker.read_text(encoding="utf-8").strip() == TOOLCHAIN_ID
            and tools.nls_link.is_symlink()):
        marker.unlink(missing_ok=True)
        log(f"installing release toolchain {TOOLCHAIN_ID} in {tools.root}")
        try:
            _install_wine(tools, downloads, log)
            _install_inno(tools, downloads, log)
            _install_node(tools, downloads, log)
        finally:
            tools.stop_wine()
        marker.write_text(TOOLCHAIN_ID + "\n", encoding="utf-8")
    versions = run([tools.node_bin / "node", "--version"], env=tools.node_env()).strip()
    npm = run(["npm", "--version"], env=tools.node_env()).strip()
    if versions != f"v{NODE_VERSION}" or npm != NPM_VERSION:
        raise BuildError(f"toolchain reports node {versions} / npm {npm}, expected "
                         f"v{NODE_VERSION} / {NPM_VERSION}")
    return tools


# -- payload -------------------------------------------------------------------------------------
def _ps_array(text, name):
    match = re.search(r"\$" + re.escape(name) + r"\s*=\s*@\((.*?)\)", text, re.S)
    if not match:
        raise BuildError(f"build_release.ps1: array ${name} not found")
    items = re.findall(r"'([^']+)'", match.group(1))
    if not items:
        raise BuildError(f"build_release.ps1: array ${name} is empty")
    return items


def payload_rules(repo):
    """File lists of build_release.ps1 and the content rules of test_release_packaging.ps1."""
    builder = (Path(repo) / "packaging/build_release.ps1").read_text(encoding="utf-8")
    files = list(_ps_array(builder, "rootFiles"))
    files += [f"src/python/{name}" for name in _ps_array(builder, "pythonFiles")]
    files += [f"src/python/input_backends/{name}" for name in _ps_array(builder, "backendFiles")]
    contract = (Path(repo) / "packaging/test_release_packaging.ps1").read_text(encoding="utf-8")
    blocks = re.findall(r"foreach \(\$relative in @\((.*?)\)\)", contract, re.S)
    if len(blocks) != 2:
        raise BuildError("test_release_packaging.ps1: required/forbidden lists not found")
    required, forbidden = ([item.replace("\\", "/") for item in re.findall(r"'([^']+)'", block)]
                           for block in blocks)
    forbidden += ["src/python/service.py", "node_modules"]
    return files, required, sorted(set(forbidden))


def stage_payload(repo, payload, version):
    repo, payload = Path(repo), Path(payload)
    if payload.exists():
        shutil.rmtree(payload)
    payload.mkdir(parents=True)
    files, required, forbidden = payload_rules(repo)
    for relative in files:
        source = repo / relative
        if not source.is_file():
            raise BuildError(f"Required release input is missing: {relative}")
        (payload / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, payload / relative)
    shutil.copytree(repo / "docs", payload / "docs")
    dist = repo / "webgui/dist"
    for name in ("index.html", "overlay.html", "VERSION"):
        if not (dist / name).is_file():
            raise BuildError(f"webgui/dist/{name} is missing; the WebGUI build did not run")
    dist_version = (dist / "VERSION").read_text(encoding="utf-8").strip()
    if dist_version != version:
        raise BuildError(f"webgui/dist was built for '{dist_version}', not '{version}'")
    shutil.copytree(dist, payload / "webgui/dist")
    for relative in required:
        if not (payload / relative).is_file():
            raise BuildError(f"Missing payload file: {relative}")
    for relative in forbidden:
        if (payload / relative).exists():
            raise BuildError(f"Development file leaked into payload: {relative}")
    inno = (repo / "packaging/FlowShift.iss").read_text(encoding="utf-8")
    for needle, message in (("OutputBaseFilename=FlowShift-Setup", "Inno output name is not fixed"),
                            ("HasCommandLineParameter('/FLOWUPDATE')", "Inno setup does not recognize FLOWUPDATE"),
                            ("install_flowshift.ps1", "Inno setup does not run the core installer")):
        if needle not in inno:
            raise BuildError(message)
    return payload


# -- release files -------------------------------------------------------------------------------
def manifest_bytes(version, installer_size, installer_sha256,
                   minimum_updater_version=MINIMUM_UPDATER_VERSION):
    manifest = {
        "schema_version": 1, "version": version, "tag": f"v{version}", "channel": "stable",
        "installer": {"name": INSTALLER_NAME, "size": installer_size, "sha256": installer_sha256},
        "minimum_updater_version": minimum_updater_version,
    }
    return (json.dumps(manifest, indent=4) + "\n").encode("utf-8")


def checksums_bytes(installer_sha256, manifest_sha256):
    return (f"{installer_sha256}  {INSTALLER_NAME}\n"
            f"{manifest_sha256}  {MANIFEST_NAME}\n").encode("utf-8")


def write_release_files(release_dir, version):
    """update-manifest.json and SHA256SUMS.txt next to an existing FlowShift-Setup.exe."""
    release_dir = Path(release_dir)
    installer = release_dir / INSTALLER_NAME
    size = installer.stat().st_size
    if size <= 0 or size > MAX_INSTALLER_BYTES:
        raise BuildError(f"Installer size is outside the update contract: {size}")
    with open(installer, "rb") as handle:
        if handle.read(2) != b"MZ":
            raise BuildError("FlowShift-Setup.exe is not a Windows executable")
    installer_sha = sha256_file(installer)
    manifest = manifest_bytes(version, size, installer_sha)
    (release_dir / MANIFEST_NAME).write_bytes(manifest)
    (release_dir / CHECKSUMS_NAME).write_bytes(
        checksums_bytes(installer_sha, hashlib.sha256(manifest).hexdigest()))
    present = sorted(p.name for p in release_dir.iterdir())
    if present != sorted(ASSET_NAMES):
        raise BuildError(f"Unexpected release assets: {', '.join(present)}")
    return {name: release_dir / name for name in ASSET_NAMES}


def _windows_path(path):
    return "Z:" + str(Path(path).resolve()).replace("/", "\\")


def build_assets(source, version, work, tools, log=_print_line, run_tests=True):
    """Build the three release assets from the checked-out release ``source``.

    ``source`` is a clean checkout of the release tag; ``work`` receives payload and
    release folder. Returns {asset name: path}.
    """
    source, work = Path(source), Path(work)
    if not _STABLE.match(version):
        raise BuildError(f"VERSION must be canonical stable SemVer for a release build: '{version}'")
    found = (source / "VERSION").read_text(encoding="utf-8").strip()
    if found != version:
        raise BuildError(f"Release tag v{version} does not match VERSION '{found}'")
    if run_tests:
        run(["python3", "-m", "unittest", *RELEASE_TEST_MODULES], cwd=source / "src/python",
            log=log, timeout=1800, label="Python unit tests of updater, installer and packaging")
    webgui = source / "webgui"
    env = tools.node_env()
    for args, label in ((["npm", "ci", "--include=dev", "--no-fund"], "npm ci"),
                        (["npm", "audit", "--include=dev", "--audit-level=high"], "npm audit"),
                        (["npm", "test"], "npm test (vitest)"),
                        (["npm", "run", "build"], "npm run build (vite)")):
        run(args, cwd=webgui, env=env, log=log, timeout=1800, label=label)
    payload = stage_payload(source, work / "payload", version)
    release = work / "release"
    if release.exists():
        shutil.rmtree(release)
    release.mkdir(parents=True)
    try:
        tools.wine(tools.iscc, f"/DMyAppVersion={version}", f"/DSourceRoot={_windows_path(payload)}",
                   f"/DOutputDir={_windows_path(release)}",
                   _windows_path(source / "packaging/FlowShift.iss"),
                   log=log, timeout=1800, label="ISCC.exe FlowShift.iss (Inno Setup 6.7.4 under Wine)")
    finally:
        tools.stop_wine()
    return write_release_files(release, version)


class BuildLog:
    """Full step output goes into a file; only one line per step is shown."""

    def __init__(self, path, out):
        self.path = Path(path)
        self.out = out
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("", encoding="utf-8")

    def __call__(self, line, detail=None):
        print(line, file=self.out, flush=True)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            if detail:
                handle.write(detail if detail.endswith("\n") else detail + "\n")


def temporary_work_dir():
    return Path(tempfile.mkdtemp(prefix="flowshift-release-build-"))
