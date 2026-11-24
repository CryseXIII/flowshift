"""USB package against the real PyPI (Viktor 24.11. 22:38).

test_usb_package.py replaces PyPI with stand-ins; those were green while the
real build could never work: ``pip download --only-binary=:all:`` found no
wheel for proxy-tools (pywebview needs it, PyPI has it only as source) and
the stand-ins simply wrote wheels for every requirement. This module asks
the real PyPI for the complete requirements.txt:

- every line has a Windows amd64 wheel or a source package for the bundled
  Python (``wheel_report``);
- ``build_package`` with the real pip: source-only packages are built into a
  wheel, every supported Python gets its wheel set (only python.org is a
  stand-in, its 45 MB do not tell anything about the requirements);
- the unpacked package installs with ``pip install --no-index --find-links
  --require-hashes`` into an empty target for Windows amd64 and every Python
  of the package, with every network path pointing nowhere.

It needs internet (pypi.org, files.pythonhosted.org). Without a pip of its
own the test machine's Python uses the pinned pip wheel from PyPI (SHA-256
from PyPI checked) on ``PYTHONPATH`` with ``python -m pip``. Without any
network path to PyPI (the sandbox of the automatic test run has none) the module reports itself skipped with the
reason; as soon as PyPI answers, every error fails. ``FLOWSHIFT_SKIP_PYPI_TESTS=1``
skips it explicitly.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import usb_package as up
from test_usb_package import INSTALL_SCRIPT, REPO, VERSION, extract, make_installed_root

SKIP = os.environ.get("FLOWSHIFT_SKIP_PYPI_TESTS") == "1"
PYPI_HOSTS = ("pypi.org", "files.pythonhosted.org")


def pypi_unreachable():
    """Why this machine cannot reach PyPI at all, or ``""``.

    Only a missing network path counts (no DNS, no route, refused): the
    sandbox of the automatic test run has no internet and reports this module
    as skipped. Once the hosts answer, every PyPI error is a real failure.
    """
    for host in PYPI_HOSTS:
        try:
            socket.create_connection((host, 443), timeout=10).close()
        except OSError as exc:
            return f"no network path to {host}:443 ({exc}); the real PyPI test needs internet"
    return ""


UNREACHABLE = "" if SKIP else pypi_unreachable()
CONSTANTS = up.installer_constants(INSTALL_SCRIPT)
MINORS = list(range(CONSTANTS["min_minor"], CONSTANTS["max_minor"] + 1))
BUNDLED = int(CONSTANTS["python_version"].split(".")[1])
REQUIREMENTS_TEXT = (REPO / "requirements.txt").read_text(encoding="utf-8")


def pip_command(folder):
    """(command, extra environment) to start pip.

    ``python -m pip`` if this Python has pip; else the pinned pip wheel from
    PyPI on ``PYTHONPATH`` (a wheel is a zip, Python imports pip from it) and
    still ``python -m pip``. The environment has to reach every pip process:
    the test's own subprocess calls and usb_package._run_pip.
    """
    command = [sys.executable, "-m", "pip"]
    if importlib.util.find_spec("pip") is not None:
        return command, {}
    files = up.default_fetch_release("pip", CONSTANTS["pip_version"])
    wheel = next(item for item in files if item["filename"].endswith("-py3-none-any.whl"))
    with urllib.request.urlopen(wheel["url"], timeout=up.PYPI_TIMEOUT) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != wheel["sha256"]:
        raise AssertionError(f"{wheel['filename']} from PyPI has the wrong SHA-256")
    path = Path(folder) / wheel["filename"]
    path.write_bytes(data)
    return command, {"PYTHONPATH": str(path)}


class PipCommandTests(unittest.TestCase):
    """Offline: without its own pip the wheel goes on PYTHONPATH, not into the command."""

    def test_without_pip_the_wheel_is_on_pythonpath_and_pip_starts_as_module(self):
        data = b"attrappe pip wheel"
        release = [{"filename": "pip-1.0-py3-none-any.whl", "url": "https://example.invalid/pip.whl",
                    "sha256": hashlib.sha256(data).hexdigest()}]
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = data
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(importlib.util, "find_spec", return_value=None), \
                mock.patch.object(up, "default_fetch_release", return_value=release), \
                mock.patch.object(urllib.request, "urlopen", return_value=response):
            command, env = pip_command(folder)
            wheel = Path(folder) / "pip-1.0-py3-none-any.whl"
            self.assertEqual(command, [sys.executable, "-m", "pip"])
            self.assertEqual(env, {"PYTHONPATH": str(wheel)})
            self.assertEqual(wheel.read_bytes(), data)

    def test_wrong_sha256_of_the_pip_wheel_is_refused(self):
        release = [{"filename": "pip-1.0-py3-none-any.whl", "url": "https://example.invalid/pip.whl",
                    "sha256": "0" * 64}]
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"attrappe"
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(importlib.util, "find_spec", return_value=None), \
                mock.patch.object(up, "default_fetch_release", return_value=release), \
                mock.patch.object(urllib.request, "urlopen", return_value=response):
            with self.assertRaises(AssertionError):
                pip_command(folder)

    def test_with_own_pip_nothing_is_downloaded(self):
        with mock.patch.object(importlib.util, "find_spec", return_value=object()), \
                mock.patch.object(up, "default_fetch_release", side_effect=AssertionError("no download")):
            self.assertEqual(pip_command("unused"), ([sys.executable, "-m", "pip"], {}))


@unittest.skipIf(SKIP, "FLOWSHIFT_SKIP_PYPI_TESTS=1")
@unittest.skipIf(UNREACHABLE, UNREACHABLE)
class RealPyPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temp.name)
        cls.pip, extra = pip_command(cls.base)
        # For every pip process: this test's subprocess calls and _run_pip.
        cls.environ = mock.patch.dict(os.environ, extra)
        cls.environ.start()

    @classmethod
    def tearDownClass(cls):
        cls.environ.stop()
        cls.temp.cleanup()

    def test_every_requirement_has_a_windows_wheel_or_source_for_the_bundled_python(self):
        report = up.wheel_report(REQUIREMENTS_TEXT, MINORS)
        self.assertEqual(len(report), len(up.parse_requirements(REQUIREMENTS_TEXT)))
        for item in report:
            with self.subTest(package=item["name"]):
                self.assertIn(item["minors"][BUNDLED], ("wheel", "source"), item)
        # The case of 24.11.: proxy-tools is on PyPI only as source, locked by hash.
        proxy = next(item for item in report if item["name"] == "proxy-tools")
        self.assertEqual(set(proxy["minors"].values()), {"source"})
        self.assertIn(proxy["sdist_sha256"],
                      next(e for e in up.parse_requirements(REQUIREMENTS_TEXT) if e["name"] == "proxy-tools")["hashes"])

    def test_real_build_then_offline_install_for_every_python(self):
        python_bytes = b"attrappe python installer"
        root = make_installed_root(self.base / "Program Files" / "FlowShift", python_bytes=python_bytes)
        out = self.base / "Downloads"
        steps = []

        def fetch_python(url, destination):
            Path(destination).write_bytes(python_bytes)

        with mock.patch.object(up, "PIP_COMMAND", self.pip):
            result = up.build_package(root, out, self.base / "work", fetch_python=fetch_python,
                                      progress=steps.append)
        self.assertIn(f"3.{BUNDLED}", result["python_versions"])
        self.assertTrue(any(step.startswith("Building the Python package proxy-tools") for step in steps), steps)

        unpacked = self.base / "stick"
        extract(result["path"], unpacked)
        package = unpacked / f"FlowShift-{VERSION}-usb"
        self.assertEqual(up.check_package(package), [])
        manifest = json.loads((package / "offline" / up.MANIFEST_NAME).read_text())
        built = {item["name"]: item for item in manifest["built_from_source"]}
        self.assertIn("proxy-tools", built)
        self.assertTrue(built["proxy-tools"]["file"].endswith("-none-any.whl"))
        names = {item["name"] for item in up.parse_requirements(REQUIREMENTS_TEXT)}

        # The other PC: no index, no proxy, nothing but the package folder.
        no_network = dict(os.environ, PIP_NO_INDEX="1", PIP_INDEX_URL="http://127.0.0.1:9/simple",
                          HTTP_PROXY="http://127.0.0.1:9", HTTPS_PROXY="http://127.0.0.1:9",
                          http_proxy="http://127.0.0.1:9", https_proxy="http://127.0.0.1:9",
                          PIP_CONFIG_FILE=os.devnull)
        requirements = package / "offline" / up.OFFLINE_REQUIREMENTS
        for version in result["python_versions"]:
            minor = version.split(".")[1]
            wheel_dir = package / "offline" / up.WHEELS_DIR / f"cp3{minor}"
            target = self.base / "installed" / f"cp3{minor}"
            platform = ["--only-binary=:all:", "--platform", "win_amd64", "--implementation", "cp",
                        "--python-version", version, "--target", str(target)]
            with self.subTest(python=version):
                run = subprocess.run(
                    [*self.pip, "install", "--disable-pip-version-check", "--no-cache-dir", "--no-index",
                     "--find-links", str(wheel_dir), "--require-hashes", "-r", str(requirements), *platform],
                    env=no_network, capture_output=True, text=True, timeout=600)
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                installed = {path.name.split("-")[0].lower().replace("_", "-")
                             for path in target.glob("*.dist-info")}
                self.assertEqual(installed, names)
                self.assertTrue((target / "proxy_tools" / "__init__.py").is_file())
                # Why requirements-offline.txt exists: requirements.txt has only
                # the sdist hash of proxy-tools, so the built wheel is refused.
                plain = subprocess.run(
                    [*self.pip, "install", "--disable-pip-version-check", "--no-cache-dir", "--no-index",
                     "--find-links", str(wheel_dir), "--require-hashes", "-r", str(package / "requirements.txt"),
                     *platform[:-1], str(self.base / "plain" / f"cp3{minor}")],
                    env=no_network, capture_output=True, text=True, timeout=600)
                self.assertNotEqual(plain.returncode, 0)
                self.assertIn("proxy", (plain.stdout + plain.stderr).lower())
        with zipfile.ZipFile(result["path"]) as archive:
            self.assertIn(f"FlowShift-{VERSION}-usb/offline/{up.OFFLINE_REQUIREMENTS}", archive.namelist())


if __name__ == "__main__":
    unittest.main()
