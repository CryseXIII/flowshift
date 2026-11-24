"""USB package of the installed FlowShift (Viktor 24.11.).

Settings > Software Update on a PC with the development channel builds one ZIP
of the version that is installed right now, for example
``FlowShift-0.7.0-dev.52-usb.zip``. On another PC (no Git, no Node.js, no
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
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
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

_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$")


class PackageError(Exception):
    """A plain reason why the package could not be built or is not usable."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


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
    return {
        "python_version": python_version,
        "python_file": f"python-{python_version}-amd64.exe",
        "python_url": f"https://www.python.org/ftp/python/{python_version}/python-{python_version}-amd64.exe",
        "python_sha256": one(r"^\$PythonInstallerSha256\s*=\s*'([0-9a-f]{64})'", "the Python SHA-256"),
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
    0.7.0-dev.52, and the WebGUI must be the one built for this version.
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

def default_fetch_python(url, destination, timeout=PYTHON_DOWNLOAD_TIMEOUT):
    partial = Path(str(destination) + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "FlowShift-usb-package"})
    with urllib.request.urlopen(request, timeout=timeout) as response, open(partial, "wb") as handle:
        shutil.copyfileobj(response, handle, 1024 * 1024)
    os.replace(partial, destination)


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


def default_download_wheels(requirements, python_minor, destination):
    """Hash-locked Windows wheels for CPython 3.<minor>; (ok, last output line)."""
    code, output = _run_pip([
        "download", "--disable-pip-version-check", "--no-input",
        "--only-binary=:all:", "--platform", "win_amd64", "--implementation", "cp",
        "--python-version", f"3.{python_minor}", "--require-hashes",
        "-r", str(requirements), "-d", str(destination)])
    return code == 0, (output.splitlines() or [""])[-1]


def default_download_pip(pip_version, destination):
    code, output = _run_pip([
        "download", "--disable-pip-version-check", "--no-input", "--no-deps",
        "--only-binary=:all:", f"pip=={pip_version}", "-d", str(destination)])
    return code == 0, (output.splitlines() or [""])[-1]


# ── build ───────────────────────────────────────────────────────────────────

def _file_list(folder, base):
    return [{"file": path.relative_to(base).as_posix(), "sha256": sha256_file(path)}
            for path in sorted(Path(folder).iterdir()) if path.is_file()]


def build_package(runtime_root, output_dir, work_dir, *, fetch_python=default_fetch_python,
                  download_wheels=default_download_wheels, download_pip=default_download_pip,
                  progress=None, now=None):
    """Build ``FlowShift-<version>-usb.zip`` in ``output_dir``; returns a summary."""
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
        step(f"Downloading Python {constants['python_version']} from python.org")
        python_path = offline / constants["python_file"]
        try:
            fetch_python(constants["python_url"], python_path)
        except Exception as exc:
            raise PackageError("python_download_failed",
                               f"Python {constants['python_version']} could not be downloaded from python.org: {exc}") from exc
        python_sha = sha256_file(python_path)
        if python_sha != constants["python_sha256"]:
            raise PackageError("python_checksum", "The downloaded Python installer has the wrong SHA-256; nothing was packed.")

        step(f"Downloading pip {constants['pip_version']}")
        ok, detail = download_pip(constants["pip_version"], common)
        if not ok or not any(common.iterdir()):
            raise PackageError("pip_download_failed", f"pip {constants['pip_version']} could not be downloaded: {detail}")

        requirements = runtime_root / "requirements.txt"
        wheel_sets, skipped = {}, {}
        for minor in range(constants["min_minor"], constants["max_minor"] + 1):
            step(f"Downloading the Python packages for Python 3.{minor}")
            target = offline / WHEELS_DIR / f"cp3{minor}"
            target.mkdir(parents=True)
            ok, detail = download_wheels(requirements, minor, target)
            if ok and any(target.iterdir()):
                wheel_sets[f"3.{minor}"] = _file_list(target, offline)
            else:
                shutil.rmtree(target, ignore_errors=True)
                skipped[f"3.{minor}"] = detail or "no wheels"
        bundled_minor = constants["python_version"].rsplit(".", 1)[0]
        if bundled_minor not in wheel_sets:
            raise PackageError(
                "wheels_download_failed",
                f"The Python packages for Python {bundled_minor} could not be downloaded: {skipped.get(bundled_minor)}")

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

    def __init__(self, runtime_root, work_dir, output_dir=None, builder=build_package, log=None):
        self._runtime_root = Path(runtime_root)
        self._work_dir = Path(work_dir)
        self._output_dir = output_dir
        self._builder = builder
        self._log = log
        self._lock = threading.Lock()
        self._thread = None
        self._status = {"state": "idle", "step": None, "message": None, "error_code": None,
                        "path": None, "file_name": None, "size": None, "version": None,
                        "python_versions": [], "started_at": None, "finished_at": None}

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

    def start(self):
        """``(True, status)`` when a build started, ``(False, status)`` if one runs."""
        with self._lock:
            if self._status["state"] == "running":
                return False, dict(self._status)
            self._status.update(state="running", step="Starting", message=None, error_code=None,
                                path=None, file_name=None, size=None, version=None,
                                python_versions=[], started_at=_now_text(), finished_at=None)
            self._thread = threading.Thread(target=self._run, name="usb-package", daemon=True)
            self._thread.start()
            return True, dict(self._status)

    def wait(self, timeout=None):
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _run(self):
        started = time.monotonic()
        self._emit("INFO", "usb package build started")
        try:
            output = Path(self._output_dir) if self._output_dir is not None else default_output_dir()
            result = self._builder(self._runtime_root, output, self._work_dir,
                                   progress=lambda text: self._set(step=text))
            self._set(state="done", step=None, finished_at=_now_text(), path=result["path"],
                      file_name=result["file_name"], size=result["size"], version=result["version"],
                      python_versions=result.get("python_versions", []),
                      message=f"Saved {result['file_name']} to {Path(result['path']).parent}")
            self._emit("INFO", f"usb package ready file={result['file_name']} size={result['size']} "
                               f"python={','.join(result.get('python_versions', []))} "
                               f"seconds={time.monotonic() - started:.0f}")
        except PackageError as exc:
            self._set(state="failed", step=None, finished_at=_now_text(), message=exc.message,
                      error_code=exc.code)
            self._emit("WARN", f"usb package failed code={exc.code}: {exc.message}")
        except Exception as exc:
            self._set(state="failed", step=None, finished_at=_now_text(),
                      message=f"The USB package could not be built: {exc}", error_code="unexpected")
            self._emit("ERROR", f"usb package failed unexpectedly: {exc!r}")


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
