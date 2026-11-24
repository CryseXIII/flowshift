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
            (Path(destination) / name).write_bytes(f"attrappe {name}".encode())
        return True, "Saved"

    def download_pip(self, version, destination):
        self.calls.append(("pip", version))
        (Path(destination) / f"pip-{version}-py3-none-any.whl").write_bytes(b"attrappe pip")
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

    def test_downloads_folder_default(self):
        with mock.patch.dict(os.environ, {"USERPROFILE": "/home/attrappe"}):
            if os.name != "nt":
                self.assertEqual(up.default_output_dir(), Path("/home/attrappe/Downloads"))


if __name__ == "__main__":
    unittest.main()
