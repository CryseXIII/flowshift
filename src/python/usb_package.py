"""USB package of the installed FlowShift (Viktor 24.11.).

Settings > Software Update on a PC with the development channel builds one ZIP
of the version that is installed right now, for example
``FlowShift-0.7.0-dev.53-usb.zip``. On another PC (no Git, no Node.js, no
Forgejo) the user unpacks it and double-clicks ``install_flowshift.bat``.

What the ZIP contains (one top folder ``FlowShift-<version>-usb``):

- the program files of the installed version, including the installer,
  updater and uninstaller scripts;
- the prebuilt WebGUI of the installed version as ``webgui/dist`` (stamped
  with the same VERSION, so the installer needs no Node.js);
- ``offline/``: the official python.org installer the installer would
  otherwise download (same version and SHA-256 as in install_flowshift.ps1),
  the pinned pip wheel and the hash-locked dependency wheels for every
  supported Python minor version that PyPI has wheels for, plus
  ``usb_package.json`` with the SHA-256 of every file there.

With that the installer needs no internet at all on a PC with Windows 10/11
(WebView2 is part of Windows 11 and of current Windows 10; it is not
bundled, the installer says so if it is missing).

The same module switches the update channel of an installation from a USB
package to stable (``--stable-channel``) and checks an unpacked package
(``--check-package``), both used by install_flowshift.ps1 and the tests.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

MANIFEST_FORMAT = 1
OFFLINE_DIR = "offline"
MANIFEST_NAME = "usb_package.json"
WHEELS_DIR = "wheels"
COMMON_WHEELS = "common"

# Copied as they are from the installed program folder (missing optional
# files are skipped); ``install_webgui.ps1`` only matters with Node.js.
PROGRAM_FILES = (
    "VERSION", "requirements.txt", "README.md", "LICENSE",
    "install_flowshift.bat", "install_flowshift.ps1", "install_webgui.ps1",
    "update_flowshift.ps1", "uninstall_flowshift.ps1", "uninstall_flowshift.bat",
)
REQUIRED_FILES = (
    "VERSION", "requirements.txt", "install_flowshift.bat", "install_flowshift.ps1",
    "update_flowshift.ps1", "uninstall_flowshift.ps1", "uninstall_flowshift.bat",
)
PROGRAM_DIRS = ("src", "docs")
# Local runtime files that never belong into a package.
SKIP_NAMES = frozenset({"__pycache__", "config.json", "flowshift.log", "flowshift_runtime.out"})
SKIP_SUFFIXES = (".pyc", ".pyo")
# The installed WebGUI folder holds the port setting of this PC; not copied.
WEBGUI_SKIP = frozenset({"config.json"})

PYTHON_DOWNLOAD_TIMEOUT = 300
PIP_DOWNLOAD_TIMEOUT = 900
# pip's own per-connection network timeout in seconds (--timeout).
PIP_NETWORK_TIMEOUT = 60
# One automatic second try per download (Viktor 24.11.: wrong SHA-256 once).
DOWNLOAD_ATTEMPTS = 2
# Sources of the Python installer: python.org, or the file the user saved
# with the browser in the Downloads folder (checked with the same SHA-256).
PYTHON_SOURCE_DOWNLOAD = "download"
PYTHON_SOURCE_DOWNLOADS_FOLDER = "downloads"
PYTHON_SOURCES = (PYTHON_SOURCE_DOWNLOAD, PYTHON_SOURCE_DOWNLOADS_FOLDER)

_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$")


class PackageError(Exception):
    """A plain reason why the package could not be built or is not usable."""

    def __init__(self, code, message, *, details=(), python_fallback=None):
        super().__init__(message)
        self.code = code
        self.message = message
        # Extra lines for the log (sizes, hashes, HTTP answers of every try).
        self.details = list(details)
        # Set when the user can save the Python installer with the browser
        # and let the build use that file: {"file", "url", "folder"}.
        self.python_fallback = python_fallback


def package_name(version):
    return f"FlowShift-{version}-usb.zip"


def top_folder(version):
    return f"FlowShift-{version}-usb"


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_version(root):
    path = Path(root) / "VERSION"
    try:
        value = path.read_text(encoding="utf-8-sig").strip()
    except OSError as exc:
        raise PackageError("version_missing", f"VERSION is missing in {root}") from exc
    if not _SEMVER.match(value):
        raise PackageError("version_invalid", f"VERSION in {root} is not a valid version: {value!r}")
    return value


def installer_constants(script_text):
    """Python and pip pins of install_flowshift.ps1, the single source for both."""
    def one(pattern, what):
        match = re.search(pattern, script_text, re.MULTILINE)
        if not match:
            raise PackageError("installer_unreadable", f"install_flowshift.ps1 does not name {what}")
        return match.group(1)

    python_version = one(r"^\$PythonVersion\s*=\s*'(\d+\.\d+\.\d+)'", "the Python version")
    size = re.search(r"^\$PythonInstallerSize\s*=\s*(\d+)", script_text, re.MULTILINE)
    return {
        "python_version": python_version,
        "python_file": f"python-{python_version}-amd64.exe",
        "python_url": f"https://www.python.org/ftp/python/{python_version}/python-{python_version}-amd64.exe",
        "python_sha256": one(r"^\$PythonInstallerSha256\s*=\s*'([0-9a-f]{64})'", "the Python SHA-256"),
        # Only for the error message; the SHA-256 alone decides.
        "python_size": int(size.group(1)) if size else None,
        "min_minor": int(one(r"^\$MinPythonMinor\s*=\s*(\d+)", "the oldest Python")),
        "max_minor": int(one(r"^\$MaxPythonMinor\s*=\s*(\d+)", "the newest Python")),
        "pip_version": one(r"'pip==(\d+(?:\.\d+)*)'", "the pip version"),
        "required_program_files": [
            item.replace("\\", "/") for item in re.findall(r"'([^']+)'", _required_block(script_text))
        ],
    }


def _required_block(script_text):
    match = re.search(r"^\$RequiredProgramFiles\s*=\s*@\((.*?)^\)", script_text, re.MULTILINE | re.DOTALL)
    if not match:
        raise PackageError("installer_unreadable", "install_flowshift.ps1 does not name the required files")
    return match.group(1)


def _skipped(path):
    return path.name in SKIP_NAMES or path.name.endswith(SKIP_SUFFIXES)


def _walk(root, skip=_skipped):
    """Files under ``root`` (sorted, stable ZIP order) without local junk."""
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not skip(Path(current) / d))
        for name in sorted(files):
            path = Path(current) / name
            if not skip(path):
                yield path


def collect_program(runtime_root):
    """``[(source path, path in the package)]`` of the installed program.

    Fails with a plain reason when the installation cannot be packed: the
    installer scripts are only part of the program folder since
    0.7.0-dev.53, and the WebGUI must be the one built for this version.
    """
    root = Path(runtime_root)
    version = read_version(root)
    missing = [name for name in REQUIRED_FILES if not (root / name).is_file()]
    if missing:
        raise PackageError(
            "program_incomplete",
            f"The installed FlowShift in {root} lacks {', '.join(missing)}. "
            "Install the current version once more (Software Update or install_flowshift.bat), then try again.")
    entries = []
    for name in PROGRAM_FILES:
        if (root / name).is_file():
            entries.append((root / name, name))
    for folder in PROGRAM_DIRS:
        if (root / folder).is_dir():
            for path in _walk(root / folder):
                entries.append((path, path.relative_to(root).as_posix()))
    webgui = root / "webgui"
    for name in ("index.html", "overlay.html"):
        if not (webgui / name).is_file():
            raise PackageError(
                "webgui_missing",
                f"The installed WebGUI in {webgui} lacks {name}. Install the current version once more, then try again.")
    try:
        stamp = (webgui / "VERSION").read_text(encoding="utf-8-sig").strip()
    except OSError:
        stamp = ""
    if stamp != version:
        raise PackageError(
            "webgui_mismatch",
            f"The installed WebGUI is {stamp or 'unversioned'}, the program is {version}. "
            "Install the current version once more, then try again.")
    for path in _walk(webgui, skip=lambda p: _skipped(p) or (p.parent == webgui and p.name in WEBGUI_SKIP)):
        relative = path.relative_to(webgui).as_posix()
        entries.append((path, f"webgui/dist/{relative}"))
    return version, entries


# ── downloads (Windows runtime defaults; the tests inject their own) ────────

class DownloadError(Exception):
    """A download that did not deliver the whole file; ``info`` as below."""

    def __init__(self, cause, info, detail=""):
        super().__init__(detail or cause)
        self.cause = cause
        self.info = info
        self.detail = detail


def _proxy_text(url):
    """``host:port`` of the proxy urllib uses for ``url`` (no user/password)."""
    try:
        scheme = urllib.parse.urlsplit(url).scheme
        proxy = urllib.request.getproxies().get(scheme)
        if not proxy:
            return None
        parts = urllib.parse.urlsplit(proxy if "://" in proxy else f"http://{proxy}")
        return f"{parts.hostname}:{parts.port}" if parts.port else parts.hostname
    except Exception:
        return None


def _replace(source, target, attempts=10, pause=0.5):
    """``os.replace`` that waits a moment for a virus scanner holding the file."""
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(pause)


def default_fetch_python(url, destination, timeout=PYTHON_DOWNLOAD_TIMEOUT):
    """Download ``url`` to ``destination``; returns what came back.

    ``{"status", "content_type", "final_url", "expected_size", "size",
    "stream_sha256", "starts_with_mz", "proxy"}``. The SHA-256 is counted
    while the bytes arrive, so the caller can tell a wrong download from a
    file that was changed on disk afterwards (virus scanner). A download
    that ends before Content-Length raises ``DownloadError("incomplete")``:
    urllib itself reports such a broken connection as a normal end.
    """
    destination = Path(destination)
    partial = Path(str(destination) + ".part")
    info = {"status": None, "content_type": None, "final_url": None, "expected_size": None,
            "size": 0, "stream_sha256": None, "starts_with_mz": None, "proxy": _proxy_text(url)}
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": "FlowShift-usb-package",
                                                   "Accept-Encoding": "identity"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            info["status"] = getattr(response, "status", None)
            info["content_type"] = response.headers.get("Content-Type")
            info["final_url"] = response.geturl()
            length = response.headers.get("Content-Length")
            info["expected_size"] = int(length) if length and length.strip().isdigit() else None
            with open(partial, "wb") as handle:
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    if info["size"] == 0:
                        info["starts_with_mz"] = block[:2] == b"MZ"
                    handle.write(block)
                    digest.update(block)
                    info["size"] += len(block)
                handle.flush()
                os.fsync(handle.fileno())
    except urllib.error.HTTPError as exc:
        info["status"] = exc.code
        info["content_type"] = exc.headers.get("Content-Type") if exc.headers else None
        raise DownloadError("http_error", info, f"HTTP {exc.code} {exc.reason}") from exc
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        reason = getattr(exc, "reason", None) or exc
        cause = "timeout" if isinstance(reason, TimeoutError) or "timed out" in str(reason) else "network"
        raise DownloadError(cause, info, str(reason)) from exc
    finally:
        info["stream_sha256"] = digest.hexdigest()
    if info["expected_size"] is not None and info["size"] != info["expected_size"]:
        raise DownloadError("incomplete", info,
                            f"{info['size']} of {info['expected_size']} bytes arrived")
    _replace(partial, destination)
    return info


def _pip_python():
    """Console python next to pythonw.exe (the tray runs under pythonw)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").is_file():
        return str(exe.parent / "python.exe")
    return str(exe)


def _run_pip(args, timeout=PIP_DOWNLOAD_TIMEOUT):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run([_pip_python(), "-m", "pip", *args], capture_output=True, text=True,
                            timeout=timeout, creationflags=flags)
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode, output.strip()


def _pip_tail(output, lines=3):
    """Last lines of a pip run: the reason why it failed is at the end."""
    return " | ".join(line.strip() for line in output.splitlines()[-lines:] if line.strip())


def default_download_wheels(requirements, python_minor, destination):
    """Hash-locked Windows wheels for CPython 3.<minor>; (ok, last output lines)."""
    code, output = _run_pip([
        "download", "--disable-pip-version-check", "--no-input", "--timeout", str(PIP_NETWORK_TIMEOUT),
        "--only-binary=:all:", "--platform", "win_amd64", "--implementation", "cp",
        "--python-version", f"3.{python_minor}", "--require-hashes",
        "-r", str(requirements), "-d", str(destination)])
    return code == 0, _pip_tail(output)


def default_download_pip(pip_version, destination):
    code, output = _run_pip([
        "download", "--disable-pip-version-check", "--no-input", "--no-deps", "--timeout", str(PIP_NETWORK_TIMEOUT),
        "--only-binary=:all:", f"pip=={pip_version}", "-d", str(destination)])
    return code == 0, _pip_tail(output)


# ── Python installer: download with diagnosis, or the user's own file ───────

def short_sha(value):
    return f"{value[:8]}...{value[-6:]}" if isinstance(value, str) and len(value) == 64 else "-"


def _bytes_text(value):
    return f"{value:,} bytes".replace(",", ".") if isinstance(value, int) else "unknown size"


def _diagnose(constants, info, file_sha, error=None):
    """``(cause, plain sentence, what to do)`` for one failed Python download."""
    content_type = (info.get("content_type") or "").lower()
    browser = "Save the file with your browser and use it as described below."
    if error is not None:
        if error.cause == "http_error":
            return "http_error", f"python.org answered {error.detail}", "Try again in a few minutes."
        if error.cause in ("network", "timeout"):
            return (error.cause, f"no connection to python.org ({error.detail})",
                    "Check the internet connection of this PC, then try again.")
        if error.cause == "incomplete":
            return ("incomplete", f"the download stopped early ({error.detail})",
                    "The connection broke off; try again, ideally on a stable network.")
        return ("unreadable", f"the downloaded file could not be read ({error.detail})",
                "A virus scanner may hold the file; try again in a moment.")
    if content_type.startswith("text/") or info.get("starts_with_mz") is False:
        return ("not_installer",
                "a web page came back instead of the installer (usually a proxy, firewall or web filter)",
                "Open the python.org link in your browser: a block page there means the network blocks "
                "the download; otherwise " + browser[0].lower() + browser[1:])
    if info.get("stream_sha256") == constants["python_sha256"]:
        return ("changed_on_disk",
                "the file arrived correctly but was changed on disk afterwards (usually a virus scanner)",
                "Let your virus scanner allow the FlowShift update folder, or " + browser[0].lower() + browser[1:])
    return "wrong_bytes", "the received bytes differ from the official file", browser


def _attempt_line(number, constants, info, file_size, file_sha, cause):
    proxy = f" proxy={info['proxy']}" if info.get("proxy") else ""
    final = info.get("final_url")
    moved = f" redirected_to={final}" if final and final != constants["python_url"] else ""
    length = info.get("expected_size")
    return (f"python download try {number}: cause={cause} http={info.get('status') or '-'} "
            f"type={info.get('content_type') or '-'} expected_size={constants.get('python_size') or '-'} "
            f"content_length={length if length is not None else '-'} "
            f"received={file_size if file_size is not None else '-'} sha256={file_sha or '-'} "
            f"stream_sha256={info.get('stream_sha256') or '-'}{proxy}{moved}")


def _python_fallback(constants, folder):
    return {"file": constants["python_file"], "url": constants["python_url"],
            "folder": str(folder) if folder is not None else None}


def _remove(*paths):
    for path in paths:
        try:
            Path(path).unlink()
        except OSError:
            pass


def download_python(constants, destination, fetch_python, step, downloads_folder=None,
                    attempts=DOWNLOAD_ATTEMPTS):
    """Fetch the python.org installer; one automatic second try.

    Returns its SHA-256 (always the pinned one). After the last failed try
    raises ``PackageError`` with the cause, expected and received size and
    SHA-256, HTTP status and content type, and the way out: the browser
    download of the same file, checked with the same SHA-256.
    """
    destination = Path(destination)
    expected = constants["python_sha256"]
    details, last = [], None
    for number in range(1, attempts + 1):
        if number > 1:
            step(f"Downloading Python {constants['python_version']} again (try {number} of {attempts})")
        _remove(destination, str(destination) + ".part")
        info, error, file_size, file_sha = {}, None, None, None
        try:
            info = fetch_python(constants["python_url"], destination) or {}
        except DownloadError as exc:
            error, info = exc, exc.info
            file_size = info.get("size") or None
            file_sha = info.get("stream_sha256") if file_size else None
        except Exception as exc:  # noqa: BLE001 - any failure becomes a plain reason
            error = DownloadError("network", info, str(exc))
        if error is None:
            try:
                file_size = destination.stat().st_size
                file_sha = sha256_file(destination)
            except OSError as exc:
                error = DownloadError("unreadable", info, str(exc))
        if error is None and file_sha == expected:
            return file_sha
        cause, sentence, hint = _diagnose(constants, info, file_sha, error)
        details.append(_attempt_line(number, constants, info, file_size, file_sha, cause))
        last = (cause, sentence, hint, info, file_size, file_sha)
    _remove(destination, str(destination) + ".part")
    cause, sentence, hint, info, file_size, file_sha = last
    code = "python_download_failed" if cause in ("http_error", "network", "timeout") else "python_checksum"
    received = (f"received {_bytes_text(file_size)}, SHA-256 {short_sha(file_sha)}"
                if file_size else "received nothing")
    answer = f"HTTP {info.get('status') or '-'}, type {info.get('content_type') or '-'}"
    tries = "twice" if attempts == 2 else f"{attempts} times"
    message = (
        f"The Python {constants['python_version']} installer from python.org was not right {tries}; "
        f"nothing was packed. Cause: {sentence}. Expected {_bytes_text(constants.get('python_size'))}, "
        f"SHA-256 {short_sha(expected)}; {received} ({answer}). {hint} "
        f"Way out: download {constants['python_file']} with your browser into the Downloads folder and "
        "click \"Use downloaded Python installer\"; it is checked with the same SHA-256.")
    raise PackageError(code, message, details=details,
                       python_fallback=_python_fallback(constants, downloads_folder))


def python_from_folder(constants, folder, destination):
    """Copy the user's own ``python-<version>-amd64.exe`` from ``folder``.

    Browsers name a second download ``python-3.15.1-amd64 (1).exe``; every
    such file is tried, newest first, and only one with the pinned SHA-256 is
    used. The copy is checked again (virus scanner between read and copy).
    Returns ``(sha256, source path)``.
    """
    folder = Path(folder)
    stem = constants["python_file"][:-len(".exe")].lower()
    expected = constants["python_sha256"]
    fallback = _python_fallback(constants, folder)
    try:
        candidates = [path for path in folder.iterdir()
                      if path.is_file() and path.name.lower().startswith(stem)
                      and path.name.lower().endswith(".exe")]
    except OSError:
        candidates = []
    if not candidates:
        raise PackageError(
            "python_manual_missing",
            f"There is no {constants['python_file']} in {folder}. Download it with your browser from "
            f"{constants['python_url']}, save it in that folder and click \"Use downloaded Python installer\" "
            "again.", python_fallback=fallback)
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    details, newest = [], None
    for path in candidates:
        try:
            size, digest = path.stat().st_size, sha256_file(path)
        except OSError as exc:
            details.append(f"python file {path}: unreadable ({exc})")
            continue
        details.append(f"python file {path}: size={size} sha256={digest}")
        newest = newest or (path, size, digest)
        if digest != expected:
            continue
        shutil.copyfile(path, destination)
        if sha256_file(destination) != expected:
            raise PackageError(
                "python_checksum",
                f"{path.name} is right, but its copy in the FlowShift update folder was changed at once "
                "(usually a virus scanner). Let the scanner allow the FlowShift update folder, then try again.",
                details=details, python_fallback=fallback)
        return expected, path
    if newest is None:
        raise PackageError(
            "python_manual_missing",
            f"{candidates[0].name} in {folder} cannot be read (still downloading, or held by a virus "
            "scanner). Wait a moment and click \"Use downloaded Python installer\" again.",
            details=details, python_fallback=fallback)
    path, size, digest = newest
    raise PackageError(
        "python_manual_checksum",
        f"{path.name} in {folder} is not the official file: expected "
        f"{_bytes_text(constants.get('python_size'))}, SHA-256 {short_sha(expected)}; the file has "
        f"{_bytes_text(size)}, SHA-256 {short_sha(digest)}. Delete it, download it again from "
        f"{constants['python_url']} and click \"Use downloaded Python installer\" again.",
        details=details, python_fallback=fallback)


def _is_wheel(path, must_contain=None):
    """A real wheel (ZIP) and not a proxy page saved under a wheel name."""
    try:
        with zipfile.ZipFile(path) as archive:
            return must_contain is None or must_contain in archive.namelist()
    except (OSError, zipfile.BadZipFile):
        return False


def _download_with_retry(download, step, label, target, check, attempts=DOWNLOAD_ATTEMPTS):
    """``download(target)`` with one more try after a failure; ``(ok, log lines)``."""
    target = Path(target)
    details = []
    for number in range(1, attempts + 1):
        if number > 1:
            step(f"{label} again (try {number} of {attempts})")
            for path in list(target.iterdir()):
                _remove(path)
        ok, detail = download(target)
        files = [path for path in sorted(target.iterdir()) if path.is_file()]
        bad = [path.name for path in files if not check(path)]
        if ok and files and not bad:
            return True, details
        if bad:
            reason = f"not a valid package file: {', '.join(bad)}"
        else:
            reason = detail or "no files"
        details.append(f"{label} try {number}: {reason}")
    return False, details


# ── build ───────────────────────────────────────────────────────────────────

def _file_list(folder, base):
    return [{"file": path.relative_to(base).as_posix(), "sha256": sha256_file(path)}
            for path in sorted(Path(folder).iterdir()) if path.is_file()]


def build_package(runtime_root, output_dir, work_dir, *, fetch_python=default_fetch_python,
                  download_wheels=default_download_wheels, download_pip=default_download_pip,
                  progress=None, now=None, python_source=PYTHON_SOURCE_DOWNLOAD):
    """Build ``FlowShift-<version>-usb.zip`` in ``output_dir``; returns a summary.

    ``python_source="downloads"`` takes the Python installer the user saved
    with the browser in ``output_dir`` (the Downloads folder) instead of
    downloading it; the SHA-256 check is the same.
    """
    def step(text):
        if progress is not None:
            progress(text)

    runtime_root = Path(runtime_root)
    work_dir = Path(work_dir)
    output_dir = Path(output_dir)
    step("Collecting the installed program and WebGUI")
    version, entries = collect_program(runtime_root)
    constants = installer_constants((runtime_root / "install_flowshift.ps1").read_text(encoding="utf-8-sig"))
    if work_dir.exists():
        shutil.rmtree(work_dir)
    offline = work_dir / OFFLINE_DIR
    common = offline / WHEELS_DIR / COMMON_WHEELS
    common.mkdir(parents=True)
    try:
        python_path = offline / constants["python_file"]
        if python_source == PYTHON_SOURCE_DOWNLOADS_FOLDER:
            step(f"Checking {constants['python_file']} in {output_dir}")
            python_sha, _ = python_from_folder(constants, output_dir, python_path)
        else:
            step(f"Downloading Python {constants['python_version']} from python.org")
            python_sha = download_python(constants, python_path, fetch_python, step,
                                         downloads_folder=output_dir)

        pip_label = f"Downloading pip {constants['pip_version']}"
        step(pip_label)
        ok, details = _download_with_retry(
            lambda folder: download_pip(constants["pip_version"], folder), step, pip_label, common,
            lambda path: _is_wheel(path, "pip/__init__.py"))
        if not ok:
            raise PackageError(
                "pip_download_failed",
                f"pip {constants['pip_version']} could not be downloaded from PyPI twice: "
                f"{details[-1].split(': ', 1)[-1]}. Check that this PC reaches pypi.org and "
                "files.pythonhosted.org (proxy or web filter?), then try again.", details=details)

        requirements = runtime_root / "requirements.txt"
        bundled_minor = constants["python_version"].rsplit(".", 1)[0]
        wheel_sets, skipped = {}, {}
        for minor in range(constants["min_minor"], constants["max_minor"] + 1):
            label = f"Downloading the Python packages for Python 3.{minor}"
            step(label)
            target = offline / WHEELS_DIR / f"cp3{minor}"
            target.mkdir(parents=True)
            # PyPI has no wheels for some versions at all; only the bundled
            # Python is worth a second try. pip checks every hash itself.
            ok, details = _download_with_retry(
                lambda folder, minor=minor: download_wheels(requirements, minor, folder), step, label,
                target, _is_wheel, attempts=DOWNLOAD_ATTEMPTS if f"3.{minor}" == bundled_minor else 1)
            if ok:
                wheel_sets[f"3.{minor}"] = _file_list(target, offline)
                continue
            shutil.rmtree(target, ignore_errors=True)
            skipped[f"3.{minor}"] = details[-1].split(": ", 1)[-1]
            if f"3.{minor}" == bundled_minor:
                raise PackageError(
                    "wheels_download_failed",
                    f"The Python packages for Python {bundled_minor} could not be downloaded twice: "
                    f"{skipped[bundled_minor]}. Check that this PC reaches pypi.org and "
                    "files.pythonhosted.org (proxy or web filter?), then try again.", details=details)

        created = (now or _dt.datetime.now(_dt.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
        manifest = {
            "format": MANIFEST_FORMAT,
            "version": version,
            "created_at": created,
            "webgui_version": version,
            "python": {"version": constants["python_version"], "file": constants["python_file"],
                       "sha256": python_sha},
            "pip": {"version": constants["pip_version"], "files": _file_list(common, offline)},
            "wheel_sets": wheel_sets,
            "skipped_wheel_sets": skipped,
        }
        (offline / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

        step("Writing the ZIP")
        output_dir.mkdir(parents=True, exist_ok=True)
        final = output_dir / package_name(version)
        partial = output_dir / (package_name(version) + ".part")
        top = top_folder(version)
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for source, name in entries:
                archive.write(source, f"{top}/{name}")
            for path in _walk(offline, skip=lambda p: False):
                archive.write(path, f"{top}/{OFFLINE_DIR}/{path.relative_to(offline).as_posix()}")
        os.replace(partial, final)
        return {
            "path": str(final),
            "file_name": final.name,
            "size": final.stat().st_size,
            "version": version,
            "python_versions": sorted(wheel_sets, key=lambda v: int(v.split(".")[1])),
            "skipped_python_versions": sorted(skipped, key=lambda v: int(v.split(".")[1])),
        }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        try:
            (output_dir / (package_name(version) + ".part")).unlink()
        except OSError:
            pass


# ── check an unpacked package (installer and tests) ─────────────────────────

def check_package(package_root):
    """Plain problems of an unpacked package; empty list = installable offline."""
    root = Path(package_root)
    problems = []
    try:
        version = read_version(root)
    except PackageError as exc:
        return [exc.message]
    try:
        constants = installer_constants((root / "install_flowshift.ps1").read_text(encoding="utf-8-sig"))
    except (OSError, PackageError) as exc:
        return [f"install_flowshift.ps1 is missing or unreadable: {exc}"]
    for relative in constants["required_program_files"]:
        if not (root / relative).is_file():
            problems.append(f"missing program file {relative}")
    dist = root / "webgui" / "dist"
    for name in ("index.html", "overlay.html", "VERSION"):
        if not (dist / name).is_file():
            problems.append(f"missing webgui/dist/{name}")
    if (dist / "VERSION").is_file() and (dist / "VERSION").read_text(encoding="utf-8-sig").strip() != version:
        problems.append("webgui/dist belongs to another version")
    offline = root / OFFLINE_DIR
    try:
        manifest = json.loads((offline / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return problems + ["offline/usb_package.json is missing or unreadable"]
    if manifest.get("version") != version:
        problems.append(f"offline files belong to {manifest.get('version')}, not {version}")
    python = manifest.get("python") or {}
    if python.get("version") != constants["python_version"] or python.get("sha256") != constants["python_sha256"]:
        problems.append("the bundled Python does not match install_flowshift.ps1")
    files = [{"file": python.get("file"), "sha256": python.get("sha256")}]
    files += list((manifest.get("pip") or {}).get("files") or [])
    for entries in (manifest.get("wheel_sets") or {}).values():
        files += list(entries)
    for entry in files:
        name = entry.get("file") if isinstance(entry, dict) else None
        if not isinstance(name, str) or ".." in name.split("/") or name.startswith("/"):
            problems.append(f"invalid file entry {name!r}")
            continue
        path = offline / name
        if not path.is_file():
            problems.append(f"missing offline/{name}")
        elif sha256_file(path) != entry.get("sha256"):
            problems.append(f"offline/{name} is damaged (SHA-256 differs)")
    bundled_minor = constants["python_version"].rsplit(".", 1)[0]
    if bundled_minor not in (manifest.get("wheel_sets") or {}):
        problems.append(f"no Python packages for the bundled Python {bundled_minor}")
    return problems


# ── stable channel after an install from a USB package ──────────────────────

def force_stable_channel(config_path):
    """Set ``updates.channel`` to stable; True when it was changed."""
    import config_schema

    config = config_schema.load_config(config_path)
    updates = config.get("updates") if isinstance(config.get("updates"), dict) else {}
    if updates.get("channel") == "stable":
        return False
    updates = dict(updates)
    updates["channel"] = "stable"
    config["updates"] = updates
    config_schema.save_config(config_path, config)
    return True


# ── background job behind the WebGUI button ─────────────────────────────────

def default_output_dir():
    """The user's Downloads folder (known folder, also when moved)."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                            ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

            # FOLDERID_Downloads {374DE290-123F-4565-9164-39C4925E467B}
            folder = GUID(0x374DE290, 0x123F, 0x4565,
                          (ctypes.c_ubyte * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B))
            pointer = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(folder), 0, None,
                                                          ctypes.byref(pointer)) == 0:
                value = pointer.value
                ctypes.windll.ole32.CoTaskMemFree(pointer)
                if value:
                    return Path(value)
        except Exception:
            pass
    return Path(os.environ.get("USERPROFILE") or Path.home()) / "Downloads"


class UsbPackageJob:
    """One package build at a time, in the background; status for the WebGUI."""

    def __init__(self, runtime_root, work_dir, output_dir=None, builder=build_package, log=None,
                 log_path=None):
        self._runtime_root = Path(runtime_root)
        self._work_dir = Path(work_dir)
        self._output_dir = output_dir
        self._builder = builder
        self._log = log
        # Named in every failure message, so the user knows where the details are.
        self._log_path = str(log_path) if log_path else None
        self._lock = threading.Lock()
        self._thread = None
        self._status = {"state": "idle", "step": None, "message": None, "error_code": None,
                        "path": None, "file_name": None, "size": None, "version": None,
                        "python_versions": [], "started_at": None, "finished_at": None,
                        "python_source": None, "python_fallback": None, "log_path": self._log_path}

    def status(self):
        with self._lock:
            return dict(self._status)

    def _set(self, **values):
        with self._lock:
            self._status.update(values)

    def _emit(self, level, text):
        if self._log is not None:
            try:
                self._log(level, text)
            except Exception:
                pass

    def start(self, python_source=PYTHON_SOURCE_DOWNLOAD):
        """``(True, status)`` when a build started, ``(False, status)`` if one runs.

        ``python_source="downloads"``: use the Python installer the user saved
        in the Downloads folder (after a failed download); same SHA-256 check.
        """
        if python_source not in PYTHON_SOURCES:
            raise ValueError(f"unknown python source {python_source!r}")
        with self._lock:
            if self._status["state"] == "running":
                return False, dict(self._status)
            self._status.update(state="running", step="Starting", message=None, error_code=None,
                                path=None, file_name=None, size=None, version=None,
                                python_versions=[], started_at=_now_text(), finished_at=None,
                                python_source=python_source, python_fallback=None)
            self._thread = threading.Thread(target=self._run, args=(python_source,),
                                            name="usb-package", daemon=True)
            self._thread.start()
            return True, dict(self._status)

    def wait(self, timeout=None):
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _failure_text(self, message):
        return f"{message} Details: {self._log_path}" if self._log_path else message

    def _run(self, python_source=PYTHON_SOURCE_DOWNLOAD):
        started = time.monotonic()
        self._emit("INFO", f"usb package build started python_source={python_source}")
        try:
            output = Path(self._output_dir) if self._output_dir is not None else default_output_dir()
            options = {} if python_source == PYTHON_SOURCE_DOWNLOAD else {"python_source": python_source}
            result = self._builder(self._runtime_root, output, self._work_dir,
                                   progress=lambda text: self._set(step=text), **options)
            self._set(state="done", step=None, finished_at=_now_text(), path=result["path"],
                      file_name=result["file_name"], size=result["size"], version=result["version"],
                      python_versions=result.get("python_versions", []),
                      message=f"Saved {result['file_name']} to {Path(result['path']).parent}")
            self._emit("INFO", f"usb package ready file={result['file_name']} size={result['size']} "
                               f"python={','.join(result.get('python_versions', []))} "
                               f"seconds={time.monotonic() - started:.0f}")
        except PackageError as exc:
            for line in exc.details:
                self._emit("WARN", f"usb package {line}")
            self._emit("WARN", f"usb package failed code={exc.code}: {exc.message}")
            self._set(state="failed", step=None, finished_at=_now_text(),
                      message=self._failure_text(exc.message), error_code=exc.code,
                      python_fallback=exc.python_fallback)
        except Exception as exc:
            self._emit("ERROR", f"usb package failed unexpectedly: {exc!r}")
            self._set(state="failed", step=None, finished_at=_now_text(),
                      message=self._failure_text(f"The USB package could not be built: {exc}"),
                      error_code="unexpected")


def _now_text():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv=None):
    parser = argparse.ArgumentParser(description="FlowShift USB package helper")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--stable-channel", metavar="CONFIG", help="set updates.channel to stable")
    group.add_argument("--check-package", metavar="FOLDER", help="check an unpacked USB package")
    args = parser.parse_args(argv)
    if args.stable_channel:
        changed = force_stable_channel(args.stable_channel)
        print("channel=stable changed" if changed else "channel=stable unchanged")
        return 0
    problems = check_package(args.check_package)
    for problem in problems:
        print(f"problem: {problem}")
    if not problems:
        print("package ok")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(main())
