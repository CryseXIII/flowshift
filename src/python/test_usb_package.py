"""USB package (Viktor 24.11.): build, unpack, install without Git/Node, start.

The end-to-end test builds the package from an installed-layout copy of this
repository (real scripts, real Python sources, a stamped WebGUI), unpacks the
ZIP into an empty folder and then works only with a PATH that holds nothing
but a Python interpreter (no git, no node, no npm): it checks the package the
way install_flowshift.ps1 does, copies the program files like the installer,
starts the Web-API of the installed copy and asks it for its version, the
WebGUI and the overlay page. Downloads (python.org, PyPI) are replaced by
small stand-ins whose SHA-256 is written into the copied installer script.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import usb_package as up

REPO = Path(__file__).resolve().parents[2]
VERSION = (REPO / "VERSION").read_text(encoding="utf-8").strip()
FAKE_PYTHON = b"attrappe python installer"
INSTALL_SCRIPT = (REPO / "install_flowshift.ps1").read_text(encoding="utf-8-sig")


def make_installed_root(target, webgui_version=VERSION, python_bytes=FAKE_PYTHON):
    """Program folder as install_flowshift.ps1 leaves it in Program Files\\FlowShift."""
    target = Path(target)
    target.mkdir(parents=True)
    for name in up.PROGRAM_FILES:
        if (REPO / name).is_file():
            shutil.copy2(REPO / name, target / name)
    shutil.copytree(REPO / "src" / "python", target / "src" / "python",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "config.json", "*.log"))
    shutil.copytree(REPO / "docs", target / "docs")
    # Local runtime junk that must never reach the package.
    (target / "src" / "python" / "config.json").write_text("{}", encoding="utf-8")
    (target / "src" / "python" / "__pycache__").mkdir()
    (target / "src" / "python" / "__pycache__" / "tray.cpython-313.pyc").write_bytes(b"x")
    # The checked-in script pins the real python.org installer; the stand-in
    # gets its own SHA-256 so the build runs the real check.
    script = (target / "install_flowshift.ps1").read_text(encoding="utf-8-sig")
    real = up.installer_constants(script)["python_sha256"]
    script = script.replace(real, hashlib.sha256(python_bytes).hexdigest())
    (target / "install_flowshift.ps1").write_text(script, encoding="utf-8")
    webgui = target / "webgui"
    (webgui / "assets").mkdir(parents=True)
    (webgui / "index.html").write_text(
        '<!doctype html><html><body><div id="root"></div><script src="/assets/app.js"></script></body></html>',
        encoding="utf-8")
    (webgui / "overlay.html").write_text(
        '<!doctype html><html><body><div id="overlay-root"></div></body></html>', encoding="utf-8")
    (webgui / "assets" / "app.js").write_text("console.log('flowshift')", encoding="utf-8")
    (webgui / "VERSION").write_text(webgui_version + "\n", encoding="utf-8")
    (webgui / "config.json").write_text('{"port": 5173}', encoding="utf-8")
    return target


def write_wheel(path, member):
    """Smallest wheel stand-in: a ZIP with one module (pip refuses anything else)."""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(member, "# attrappe\n")


class FakeDownloads:
    """python.org and PyPI stand-ins; wheels exist only for some Python versions."""

    def __init__(self, python_bytes=FAKE_PYTHON, wheel_minors=(13, 15)):
        self.python_bytes = python_bytes
        self.wheel_minors = set(wheel_minors)
        self.calls = []

    def fetch_python(self, url, destination):
        self.calls.append(("python", url))
        Path(destination).write_bytes(self.python_bytes)

    def download_wheels(self, requirements, minor, destination):
        self.calls.append(("wheels", minor))
        assert Path(requirements).name == "requirements.txt"
        if minor not in self.wheel_minors:
            return False, f"ERROR: No matching distribution for cp3{minor}"
        for name in (f"pillow-13.0.0-cp3{minor}-cp3{minor}-win_amd64.whl",
                     "pywebview-6.2.1-py3-none-any.whl"):
            write_wheel(Path(destination) / name, "attrappe/__init__.py")
        return True, "Saved"

    def download_pip(self, version, destination):
        self.calls.append(("pip", version))
        write_wheel(Path(destination) / f"pip-{version}-py3-none-any.whl", "pip/__init__.py")
        return True, "Saved"

    def kwargs(self):
        return {"fetch_python": self.fetch_python, "download_wheels": self.download_wheels,
                "download_pip": self.download_pip}


def extract(zip_path, target):
    with zipfile.ZipFile(zip_path) as archive:
        for name in archive.namelist():
            assert not name.startswith("/") and ".." not in name.split("/"), name
        archive.extractall(target)


# Copies the unpacked package like install_flowshift.ps1 (program files to the
# program folder, webgui/dist to webgui) and starts the Web-API of the copy.
# Runs as its own process with a PATH that holds only python3.
INSTALL_AND_START = textwrap.dedent("""
    import json, shutil, sys, threading
    from http.client import HTTPConnection
    from http.server import ThreadingHTTPServer
    from pathlib import Path

    package, install = Path(sys.argv[1]), Path(sys.argv[2])
    install.mkdir(parents=True)
    shutil.copytree(package / "src", install / "src")
    shutil.copytree(package / "docs", install / "docs")
    for name in ("VERSION", "requirements.txt", "README.md", "LICENSE", "install_flowshift.bat",
                 "install_flowshift.ps1", "update_flowshift.ps1", "uninstall_flowshift.ps1",
                 "uninstall_flowshift.bat"):
        shutil.copy2(package / name, install / name)
    shutil.copytree(package / "webgui" / "dist", install / "webgui")
    sys.path.insert(0, str(install / "src" / "python"))
    import version, web_api, usb_package
    web_api.init(build_status=lambda: {"app_version": version.APP_VERSION})
    server = ThreadingHTTPServer(("127.0.0.1", 0), web_api.make_api_handler())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    def get(path):
        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", path, headers={"Host": f"127.0.0.1:{port}"})
        response = connection.getresponse()
        body = response.read().decode("utf-8", "replace")
        connection.close()
        return response.status, body
    status, body = get("/api/status")
    index_status, index = get("/index.html")
    overlay_status, overlay = get("/overlay.html")
    server.shutdown()
    config = install / "config.json"
    config.write_text(json.dumps({"device_name": "attrappe", "updates": {"channel": "development",
                                  "policy": "install", "enabled": True, "check_on_start": True}}))
    usb_package.force_stable_channel(config)
    print(json.dumps({
        "api_status": status, "app_version": json.loads(body).get("app_version"),
        "index": index_status == 200 and 'id="root"' in index,
        "overlay": overlay_status == 200 and "overlay-root" in overlay,
        "webgui_root": str(web_api.resolve_webgui_root()),
        "config": json.loads(config.read_text()),
    }))
""")


class InstallerConstantsTests(unittest.TestCase):
    def test_reads_the_pins_of_the_real_installer(self):
        constants = up.installer_constants(INSTALL_SCRIPT)
        self.assertRegex(constants["python_version"], r"^3\.\d+\.\d+$")
        self.assertEqual(constants["python_file"], f"python-{constants['python_version']}-amd64.exe")
        self.assertTrue(constants["python_url"].startswith("https://www.python.org/ftp/python/"))
        self.assertRegex(constants["python_sha256"], r"^[0-9a-f]{64}$")
        # Size of the official python-3.15.1-amd64.exe (checked 24.11. against python.org).
        self.assertEqual(constants["python_size"], 45555248)
        self.assertLessEqual(constants["min_minor"], constants["max_minor"])
        self.assertEqual(constants["python_version"].split(".")[1], str(constants["max_minor"]))
        self.assertIn("pip==" + constants["pip_version"], INSTALL_SCRIPT)
        self.assertIn("src/python/tray.py", constants["required_program_files"])
        self.assertIn("uninstall_flowshift.bat", constants["required_program_files"])

    def test_installer_keeps_itself_in_the_program_folder_and_uses_the_offline_folder(self):
        # The package is built from the program folder: the installer must copy
        # its own scripts there, and read offline/ of a USB package.
        staged = INSTALL_SCRIPT.split("function New-StagedProgramFiles", 1)[1].split("\n}\n", 1)[0]
        for name in ("install_flowshift.ps1", "install_flowshift.bat", "update_flowshift.ps1",
                     "uninstall_flowshift.ps1"):
            self.assertIn(f"'{name}'", staged)
        self.assertIn("$script:UsbPackage = Read-UsbPackage", INSTALL_SCRIPT)
        self.assertIn("'--no-index', '--find-links', $wheelDir", INSTALL_SCRIPT)
        self.assertIn("usb_package.py'), '--stable-channel'", INSTALL_SCRIPT)
        # Neither Git nor Node.js is called by the installer itself.
        self.assertNotRegex(INSTALL_SCRIPT, r"(?m)^\s*&?\s*git(\.exe)?\s")
        self.assertNotIn("& node", INSTALL_SCRIPT)


class BuildInstallStartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = make_installed_root(self.base / "Program Files" / "FlowShift")
        self.out = self.base / "Downloads"
        self.work = self.base / "ProgramData" / "updates" / "usb-build"

    def tearDown(self):
        self.temp.cleanup()

    def build(self, downloads=None, **extra):
        downloads = downloads or FakeDownloads()
        steps = []
        result = up.build_package(self.root, self.out, self.work, progress=steps.append,
                                  **downloads.kwargs(), **extra)
        return result, steps, downloads

    def test_build_unpack_install_without_git_or_node_and_start(self):
        result, steps, downloads = self.build()
        zip_path = Path(result["path"])
        self.assertEqual(zip_path.name, f"FlowShift-{VERSION}-usb.zip")
        self.assertEqual(zip_path.parent, self.out)
        self.assertEqual(result["version"], VERSION)
        self.assertEqual(result["python_versions"], ["3.13", "3.15"])
        self.assertFalse(self.work.exists(), "work folder is removed")
        self.assertEqual(list(self.out.iterdir()), [zip_path], "no .part file left")
        self.assertIn("Writing the ZIP", steps)
        self.assertIn(("pip", up.installer_constants(INSTALL_SCRIPT)["pip_version"]), downloads.calls)

        with zipfile.ZipFile(zip_path) as archive:
            names = set(archive.namelist())
        top = f"FlowShift-{VERSION}-usb/"
        self.assertTrue(all(name.startswith(top) for name in names))
        for name in ("install_flowshift.bat", "install_flowshift.ps1", "update_flowshift.ps1",
                     "uninstall_flowshift.ps1", "VERSION", "requirements.txt",
                     "webgui/dist/index.html", "webgui/dist/overlay.html", "webgui/dist/VERSION",
                     "webgui/dist/assets/app.js", "src/python/tray.py", "src/python/usb_package.py",
                     "offline/usb_package.json", "offline/wheels/cp315/pywebview-6.2.1-py3-none-any.whl",
                     "offline/wheels/common/pip-26.2-py3-none-any.whl"):
            self.assertIn(top + name, names)
        self.assertNotIn(top + "webgui/dist/config.json", names)
        self.assertNotIn(top + "src/python/config.json", names)
        self.assertFalse(any("__pycache__" in name or name.endswith(".pyc") for name in names))
        self.assertFalse(any("/.git/" in name or "node_modules" in name for name in names))

        # Unpack on the "other PC": an empty folder, nothing else.
        other_pc = self.base / "other-pc"
        extract(zip_path, other_pc / "stick")
        package = other_pc / "stick" / f"FlowShift-{VERSION}-usb"
        manifest = json.loads((package / "offline" / "usb_package.json").read_text())
        self.assertEqual(manifest["version"], VERSION)
        self.assertEqual(sorted(manifest["wheel_sets"]), ["3.13", "3.15"])
        self.assertIn("3.11", manifest["skipped_wheel_sets"])

        # A PATH with only python3: no git, no node, no npm.
        bindir = other_pc / "bin"
        bindir.mkdir()
        (bindir / "python3").symlink_to(sys.executable)
        for tool in ("git", "node", "npm", "npx"):
            self.assertIsNone(shutil.which(tool, path=str(bindir)))
        env = {"PATH": str(bindir), "HOME": str(other_pc), "LANG": "C.UTF-8"}

        check = subprocess.run(["python3", "-I", str(package / "src" / "python" / "usb_package.py"),
                                "--check-package", str(package)],
                               env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        self.assertIn("package ok", check.stdout)

        helper = self.base / "helper" / "install_and_start.py"
        helper.parent.mkdir()
        helper.write_text(INSTALL_AND_START, encoding="utf-8")
        install = other_pc / "Program Files" / "FlowShift"
        run = subprocess.run(["python3", "-I", str(helper), str(package), str(install)],
                             env=env, capture_output=True, text=True, timeout=120, cwd=str(other_pc))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        started = json.loads(run.stdout.strip().splitlines()[-1])
        self.assertEqual(started["api_status"], 200)
        self.assertEqual(started["app_version"], VERSION)
        self.assertTrue(started["index"], "WebGUI index.html served from the installed copy")
        self.assertTrue(started["overlay"], "overlay.html served from the installed copy")
        self.assertEqual(Path(started["webgui_root"]), install / "webgui")
        self.assertEqual(started["config"]["updates"]["channel"], "stable")
        self.assertEqual(started["config"]["updates"]["policy"], "install")
        self.assertEqual(started["config"]["device_name"], "attrappe")

    def test_damaged_offline_file_is_reported(self):
        result, _, _ = self.build()
        target = self.base / "unpacked"
        extract(result["path"], target)
        package = target / f"FlowShift-{VERSION}-usb"
        self.assertEqual(up.check_package(package), [])
        wheel = package / "offline" / "wheels" / "cp313" / "pywebview-6.2.1-py3-none-any.whl"
        wheel.write_bytes(b"changed")
        problems = up.check_package(package)
        self.assertEqual(len(problems), 1)
        self.assertIn("damaged", problems[0])
        (package / "webgui" / "dist" / "VERSION").write_text("0.0.1\n")
        self.assertIn("webgui/dist belongs to another version", up.check_package(package))

    def test_webgui_of_another_version_is_refused(self):
        shutil.rmtree(self.root)
        self.root = make_installed_root(self.root, webgui_version="0.0.1")
        with self.assertRaises(up.PackageError) as caught:
            self.build()
        self.assertEqual(caught.exception.code, "webgui_mismatch")
        self.assertFalse(self.out.exists() and any(self.out.iterdir()))

    def test_older_installation_without_installer_scripts_is_refused(self):
        (self.root / "install_flowshift.bat").unlink()
        with self.assertRaises(up.PackageError) as caught:
            self.build()
        self.assertEqual(caught.exception.code, "program_incomplete")
        self.assertIn("install_flowshift.bat", caught.exception.message)

    def test_wrong_python_checksum_leaves_nothing_behind(self):
        with self.assertRaises(up.PackageError) as caught:
            self.build(FakeDownloads(python_bytes=b"something else"))
        self.assertEqual(caught.exception.code, "python_checksum")
        self.assertFalse(self.work.exists())
        self.assertFalse(self.out.exists() and any(self.out.iterdir()))

    def test_missing_wheels_for_the_bundled_python_fail(self):
        with self.assertRaises(up.PackageError) as caught:
            self.build(FakeDownloads(wheel_minors=(13,)))
        self.assertEqual(caught.exception.code, "wheels_download_failed")


REAL_INSTALLER = b"MZ" + b"attrappe official installer " * 200


class LocalServer:
    """python.org stand-in on 127.0.0.1; ``routes[path] = handler(request)``."""

    def __init__(self, routes):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                routes[self.path](self)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def send(handler, body, status=200, content_type="application/octet-stream", length=None):
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body) if length is None else length))
    handler.end_headers()
    handler.wfile.write(body)
    handler.wfile.flush()
    handler.close_connection = True


def constants_for(data=REAL_INSTALLER, url="https://www.python.org/ftp/python/3.15.1/python-3.15.1-amd64.exe"):
    return {"python_version": "3.15.1", "python_file": "python-3.15.1-amd64.exe", "python_url": url,
            "python_sha256": hashlib.sha256(data).hexdigest(), "python_size": len(data)}


class FetchPythonTests(unittest.TestCase):
    """default_fetch_python against a local server (urllib, real sockets)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Like %ProgramData%\FlowShift\updates\usb-build\offline, with a deep path.
        self.folder = Path(self.temp.name, *(["usb-build-attrappe"] * 12), "offline")
        self.folder.mkdir(parents=True)
        self.target = self.folder / "python-3.15.1-amd64.exe"
        self.server = LocalServer({
            "/ok": lambda h: send(h, REAL_INSTALLER),
            "/cut": lambda h: send(h, REAL_INSTALLER[:1000], length=len(REAL_INSTALLER)),
            "/filter": lambda h: send(h, b"<html>Blocked by web filter</html>", content_type="text/html"),
            "/missing": lambda h: send(h, b"not found", status=404, content_type="text/plain"),
            "/moved": lambda h: (h.send_response(302), h.send_header("Location", "/ok"),
                                 h.send_header("Content-Length", "0"), h.end_headers()),
        })
        self.addCleanup(self.server.close)

    def test_whole_file_with_status_type_and_streamed_hash(self):
        info = up.default_fetch_python(self.server.base + "/moved", self.target, timeout=5)
        self.assertEqual(self.target.read_bytes(), REAL_INSTALLER)
        self.assertFalse(Path(str(self.target) + ".part").exists())
        self.assertEqual(info["status"], 200)
        self.assertEqual(info["size"], len(REAL_INSTALLER))
        self.assertEqual(info["expected_size"], len(REAL_INSTALLER))
        self.assertEqual(info["stream_sha256"], hashlib.sha256(REAL_INSTALLER).hexdigest())
        self.assertTrue(info["starts_with_mz"])
        self.assertTrue(info["final_url"].endswith("/ok"))

    def test_broken_connection_is_not_a_normal_end(self):
        # urllib returns the short body without an error; the length check catches it.
        with self.assertRaises(up.DownloadError) as caught:
            up.default_fetch_python(self.server.base + "/cut", self.target, timeout=5)
        self.assertEqual(caught.exception.cause, "incomplete")
        self.assertEqual(caught.exception.info["size"], 1000)
        self.assertFalse(self.target.exists())

    def test_http_error_keeps_status_and_type(self):
        with self.assertRaises(up.DownloadError) as caught:
            up.default_fetch_python(self.server.base + "/missing", self.target, timeout=5)
        self.assertEqual(caught.exception.cause, "http_error")
        self.assertEqual(caught.exception.info["status"], 404)

    def test_filter_page_twice_names_the_cause_sizes_hashes_and_the_way_out(self):
        constants = constants_for(url=self.server.base + "/filter")
        steps = []
        with self.assertRaises(up.PackageError) as caught:
            up.download_python(constants, self.target,
                               lambda url, dest: up.default_fetch_python(url, dest, timeout=5),
                               steps.append, downloads_folder=Path("C:/Users/attrappe/Downloads"))
        error = caught.exception
        self.assertEqual(error.code, "python_checksum")
        self.assertIn("web page came back instead of the installer", error.message)
        self.assertIn(f"Expected {len(REAL_INSTALLER):,} bytes".replace(",", "."), error.message)
        self.assertIn("received 34 bytes", error.message)
        self.assertIn(up.short_sha(hashlib.sha256(b"<html>Blocked by web filter</html>").hexdigest()),
                      error.message)
        self.assertIn("HTTP 200, type text/html", error.message)
        self.assertIn('"Use downloaded Python installer"', error.message)
        self.assertEqual(len(error.details), 2)
        self.assertIn("cause=not_installer", error.details[0])
        self.assertEqual(error.python_fallback["file"], "python-3.15.1-amd64.exe")
        self.assertEqual(steps, ["Downloading Python 3.15.1 again (try 2 of 2)"])
        self.assertFalse(self.target.exists())


class DownloadPythonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.target = Path(self.temp.name) / "python-3.15.1-amd64.exe"
        self.constants = constants_for()

    def test_second_try_succeeds(self):
        answers = [b"MZ broken", REAL_INSTALLER]

        def fetch(url, destination):
            Path(destination).write_bytes(answers.pop(0))

        sha = up.download_python(self.constants, self.target, fetch, lambda text: None)
        self.assertEqual(sha, self.constants["python_sha256"])
        self.assertEqual(answers, [])

    def test_file_changed_after_download_points_to_the_virus_scanner(self):
        def fetch(url, destination):
            Path(destination).write_bytes(b"MZ quarantined")
            return {"status": 200, "content_type": "application/octet-stream",
                    "stream_sha256": self.constants["python_sha256"], "starts_with_mz": True}

        with self.assertRaises(up.PackageError) as caught:
            up.download_python(self.constants, self.target, fetch, lambda text: None)
        self.assertIn("changed on disk afterwards", caught.exception.message)
        self.assertIn("cause=changed_on_disk", caught.exception.details[1])

    def test_no_connection_is_a_download_failure(self):
        def fetch(url, destination):
            raise up.DownloadError("network", {"proxy": "proxy.attrappe:8080"}, "connection refused")

        with self.assertRaises(up.PackageError) as caught:
            up.download_python(self.constants, self.target, fetch, lambda text: None)
        self.assertEqual(caught.exception.code, "python_download_failed")
        self.assertIn("no connection to python.org (connection refused)", caught.exception.message)
        self.assertIn("received nothing", caught.exception.message)
        self.assertIn("proxy=proxy.attrappe:8080", caught.exception.details[0])


class PythonFromDownloadsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.downloads = Path(self.temp.name) / "Downloads"
        self.downloads.mkdir()
        self.target = Path(self.temp.name) / "offline.exe"
        self.constants = constants_for()

    def test_browser_copy_with_number_is_found_and_checked(self):
        (self.downloads / "python-3.15.1-amd64.exe").write_bytes(b"MZ older broken copy")
        good = self.downloads / "python-3.15.1-amd64 (1).exe"
        good.write_bytes(REAL_INSTALLER)
        sha, source = up.python_from_folder(self.constants, self.downloads, self.target)
        self.assertEqual(sha, self.constants["python_sha256"])
        self.assertEqual(source, good)
        self.assertEqual(self.target.read_bytes(), REAL_INSTALLER)

    def test_wrong_file_is_refused_with_sizes_and_hashes(self):
        (self.downloads / "python-3.15.1-amd64.exe").write_bytes(b"MZ other file")
        with self.assertRaises(up.PackageError) as caught:
            up.python_from_folder(self.constants, self.downloads, self.target)
        self.assertEqual(caught.exception.code, "python_manual_checksum")
        self.assertIn("is not the official file", caught.exception.message)
        self.assertIn("the file has 13 bytes", caught.exception.message)
        self.assertFalse(self.target.exists())

    def test_missing_file_says_where_to_save_it(self):
        with self.assertRaises(up.PackageError) as caught:
            up.python_from_folder(self.constants, self.downloads, self.target)
        self.assertEqual(caught.exception.code, "python_manual_missing")
        self.assertIn(str(self.downloads), caught.exception.message)
        self.assertIn(self.constants["python_url"], caught.exception.message)


class BuildFallbackTests(unittest.TestCase):
    """Build with the browser-saved Python and the retries of pip and wheels."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root = make_installed_root(base / "Program Files" / "FlowShift")
        self.work = base / "ProgramData" / "FlowShift" / "updates" / "usb-build"
        self.out = base / "Downloads"

    def build(self, downloads, **options):
        return up.build_package(self.root, self.out, self.work, now=None, **downloads.kwargs(), **options)

    def test_python_from_downloads_folder_without_python_org(self):
        self.out.mkdir()
        (self.out / "python-3.15.1-amd64.exe").write_bytes(FAKE_PYTHON)
        downloads = FakeDownloads()
        result = self.build(downloads, python_source=up.PYTHON_SOURCE_DOWNLOADS_FOLDER)
        self.assertNotIn("python", [call[0] for call in downloads.calls])
        unpacked = Path(self.temp.name) / "unpacked"
        extract(result["path"], unpacked)
        self.assertEqual(up.check_package(unpacked / up.top_folder(VERSION)), [])

    def test_python_from_downloads_folder_still_checks_the_hash(self):
        self.out.mkdir()
        (self.out / "python-3.15.1-amd64.exe").write_bytes(b"MZ not the pinned file")
        with self.assertRaises(up.PackageError) as caught:
            self.build(FakeDownloads(), python_source=up.PYTHON_SOURCE_DOWNLOADS_FOLDER)
        self.assertEqual(caught.exception.code, "python_manual_checksum")
        self.assertFalse(self.work.exists())

    def test_pip_and_wheels_get_a_second_try_and_html_is_no_wheel(self):
        downloads = FakeDownloads()
        real_pip, real_wheels = downloads.download_pip, downloads.download_wheels
        state = {"pip": 0, "wheels15": 0}

        def flaky_pip(version, destination):
            state["pip"] += 1
            if state["pip"] == 1:
                (Path(destination) / f"pip-{version}-py3-none-any.whl").write_bytes(b"<html>proxy</html>")
                return True, "Saved"
            return real_pip(version, destination)

        def flaky_wheels(requirements, minor, destination):
            if minor == 15:
                state["wheels15"] += 1
                if state["wheels15"] == 1:
                    return False, "ERROR: Read timed out"
            return real_wheels(requirements, minor, destination)

        downloads.download_pip, downloads.download_wheels = flaky_pip, flaky_wheels
        result = self.build(downloads)
        self.assertEqual(state, {"pip": 2, "wheels15": 2})
        self.assertIn("3.15", result["python_versions"])
        # Versions without wheels on PyPI are tried once only.
        self.assertEqual([call for call in downloads.calls if call == ("wheels", 11)], [("wheels", 11)])

    def test_pip_failing_twice_names_the_reason(self):
        downloads = FakeDownloads()
        downloads.download_pip = lambda version, destination: (False, "ERROR: ProxyError 407")
        with self.assertRaises(up.PackageError) as caught:
            self.build(downloads)
        self.assertEqual(caught.exception.code, "pip_download_failed")
        self.assertIn("ProxyError 407", caught.exception.message)
        self.assertIn("pypi.org", caught.exception.message)
        self.assertEqual(len(caught.exception.details), 2)


class StableChannelTests(unittest.TestCase):
    def test_switches_development_to_stable_and_keeps_the_rest(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(json.dumps({"device_name": "pc2", "port": 45781,
                                        "updates": {"channel": "development", "policy": "download",
                                                    "enabled": False, "check_on_start": True}}))
            self.assertTrue(up.force_stable_channel(path))
            saved = json.loads(path.read_text())
            self.assertEqual(saved["updates"]["channel"], "stable")
            self.assertEqual(saved["updates"]["policy"], "download")
            self.assertFalse(saved["updates"]["enabled"])
            self.assertEqual(saved["device_name"], "pc2")
            self.assertFalse(up.force_stable_channel(path))


class JobTests(unittest.TestCase):
    def test_one_build_at_a_time_and_result(self):
        gate = threading.Event()

        def builder(root, output, work, progress):
            progress("Downloading pip 26.2")
            gate.wait(5)
            return {"path": str(Path(output) / "FlowShift-1.0.0-usb.zip"),
                    "file_name": "FlowShift-1.0.0-usb.zip", "size": 1234, "version": "1.0.0",
                    "python_versions": ["3.15"]}

        lines = []
        with tempfile.TemporaryDirectory() as temp:
            job = up.UsbPackageJob(temp, Path(temp) / "work", output_dir=Path(temp) / "out",
                                   builder=builder, log=lambda level, text: lines.append(text))
            started, status = job.start()
            self.assertTrue(started)
            self.assertEqual(status["state"], "running")
            again, _ = job.start()
            self.assertFalse(again)
            gate.set()
            job.wait(5)
            final = job.status()
        self.assertEqual(final["state"], "done")
        self.assertEqual(final["file_name"], "FlowShift-1.0.0-usb.zip")
        self.assertIn("Saved FlowShift-1.0.0-usb.zip", final["message"])
        self.assertTrue(any("usb package ready" in line for line in lines))

    def test_failure_is_reported_in_plain_words(self):
        def builder(root, output, work, progress):
            raise up.PackageError("webgui_mismatch", "The installed WebGUI is 0.0.1")

        with tempfile.TemporaryDirectory() as temp:
            job = up.UsbPackageJob(temp, Path(temp) / "work", output_dir=temp, builder=builder)
            job.start()
            job.wait(5)
            status = job.status()
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["error_code"], "webgui_mismatch")
        self.assertEqual(status["message"], "The installed WebGUI is 0.0.1")

    def test_python_failure_offers_the_downloads_file_and_names_the_log(self):
        calls = []

        def builder(root, output, work, progress, **options):
            calls.append(options)
            if not options:
                raise up.PackageError("python_checksum", "The installer was not right twice.",
                                      details=["python download try 1: cause=wrong_bytes"],
                                      python_fallback={"file": "python-3.15.1-amd64.exe"})
            return {"path": str(Path(output) / "FlowShift-1.0.0-usb.zip"),
                    "file_name": "FlowShift-1.0.0-usb.zip", "size": 1, "version": "1.0.0"}

        lines = []
        log_path = "C:\\ProgramData\\FlowShift\\flowshift.log"
        with tempfile.TemporaryDirectory() as temp:
            job = up.UsbPackageJob(temp, Path(temp) / "work", output_dir=temp, builder=builder,
                                   log=lambda level, text: lines.append(text), log_path=log_path)
            job.start()
            job.wait(5)
            failed = job.status()
            with self.assertRaises(ValueError):
                job.start(python_source="no-check")
            job.start(python_source=up.PYTHON_SOURCE_DOWNLOADS_FOLDER)
            job.wait(5)
            done = job.status()
        self.assertEqual(failed["message"], f"The installer was not right twice. Details: {log_path}")
        self.assertEqual(failed["python_fallback"], {"file": "python-3.15.1-amd64.exe"})
        self.assertIn("usb package python download try 1: cause=wrong_bytes", lines)
        self.assertEqual(calls, [{}, {"python_source": "downloads"}])
        self.assertEqual(done["state"], "done")
        self.assertIsNone(done["python_fallback"])
        self.assertEqual(done["python_source"], "downloads")

    def test_downloads_folder_default(self):
        with mock.patch.dict(os.environ, {"USERPROFILE": "/home/attrappe"}):
            if os.name != "nt":
                self.assertEqual(up.default_output_dir(), Path("/home/attrappe/Downloads"))


if __name__ == "__main__":
    unittest.main()
