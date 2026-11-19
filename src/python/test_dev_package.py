"""Offline tests for the Linux development package builder (packaging/build_dev_package.py)."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import update_model

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "build_dev_package", REPO / "packaging" / "build_dev_package.py")
builder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(builder)

VERSION = "0.7.0-dev.29"


def fake_repo(root):
    root = Path(root)
    (root / "packaging").mkdir(parents=True)
    shutil.copy2(REPO / "packaging" / "build_release.ps1", root / "packaging")
    lists = builder.payload_lists(root / "packaging" / "build_release.ps1")
    files = list(lists["rootFiles"]) + [f"src/python/{n}" for n in lists["pythonFiles"]]
    files += [f"src/python/input_backends/{n}" for n in lists["backendFiles"]]
    for relative in files:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {relative}\n", encoding="utf-8")
    (root / "VERSION").write_text(VERSION + "\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "setup.md").write_text("setup\n", encoding="utf-8")
    dist = root / "webgui" / "dist"
    dist.mkdir(parents=True)
    for name in ("index.html", "overlay.html"):
        (dist / name).write_text("<html></html>\n", encoding="utf-8")
    (dist / "VERSION").write_text(VERSION + "\n", encoding="utf-8")
    return root


def fake_iscc(command):
    output = next(item for item in command if item.startswith("/DOutputDir="))
    path = output.split("=", 1)[1]
    if path.startswith("Z:"):
        path = path[2:].replace("\\", "/")
    Path(path, builder.INSTALLER_NAME).write_bytes(b"MZ fake setup " + VERSION.encode())
    return types.SimpleNamespace(returncode=0)


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = fake_repo(Path(self.temporary.name) / "repo")
        self.build = Path(self.temporary.name) / "build"

    def tearDown(self):
        self.temporary.cleanup()

    def test_payload_lists_come_from_build_release_and_include_new_modules(self):
        lists = builder.payload_lists(REPO / "packaging" / "build_release.ps1")
        for name in ("update_credentials.py", "update_client.py", "tray.py", "web_api.py"):
            self.assertIn(name, lists["pythonFiles"])
        self.assertIn("install_flowshift.ps1", lists["rootFiles"])
        self.assertIn("update_flowshift.ps1", lists["rootFiles"])

    def test_stage_build_and_verify_a_development_package(self):
        payload = builder.stage_payload(self.root, self.build / "payload", VERSION)
        self.assertEqual((payload / "UPDATE_CHANNEL").read_text(encoding="utf-8"), "development\n")
        self.assertTrue((payload / "webgui" / "dist" / "overlay.html").is_file())
        self.assertTrue((payload / "src" / "python" / "update_credentials.py").is_file())
        self.assertFalse((payload / "webgui" / "src").exists())

        release = self.build / "release"
        commands = []

        def runner(command):
            commands.append(command)
            return fake_iscc(command)

        builder.compile_installer("wine ISCC.exe", VERSION, payload, release, root=self.root,
                                  runner=runner)
        self.assertEqual(commands[0][:2], ["wine", "ISCC.exe"])
        self.assertTrue(any(item.startswith("/DSourceRoot=Z:\\") for item in commands[0]))
        manifest = builder.write_metadata(release, VERSION)
        self.assertEqual(builder.verify_release_dir(release, VERSION), manifest)
        self.assertEqual(manifest["channel"], "development")

        # The manifest passes the same validation FlowShift runs on the PC.
        base = f"{update_model.FORGEJO_ORIGIN}/private-platform/flowshift/releases"
        payload_release = {
            "tag_name": f"v{VERSION}", "draft": False, "prerelease": True,
            "html_url": f"{base}/tag/v{VERSION}", "body": "",
            "assets": [{"name": name, "size": (release / name).stat().st_size,
                        "browser_download_url": f"{base}/download/v{VERSION}/{name}"}
                       for name in builder.ASSET_NAMES],
        }
        validated = update_model.select_development_release([payload_release], "0.7.0-dev.22")
        descriptor = update_model.validate_manifest_payload(manifest, validated)
        self.assertEqual(descriptor.installer_sha256,
                         hashlib.sha256((release / builder.INSTALLER_NAME).read_bytes()).hexdigest())

        (release / builder.INSTALLER_NAME).write_bytes(b"tampered")
        with self.assertRaises(builder.BuildError):
            builder.verify_release_dir(release, VERSION)

    def test_stale_webgui_and_bad_version_are_refused(self):
        (self.root / "webgui" / "dist" / "VERSION").write_text("0.7.0-dev.26\n", encoding="utf-8")
        with self.assertRaises(builder.BuildError):
            builder.stage_payload(self.root, self.build / "payload", VERSION)
        (self.root / "VERSION").write_text("0.7.0-beta.1\n", encoding="utf-8")
        with self.assertRaises(builder.BuildError):
            builder.read_version(self.root)


class FakeForgejo:
    def __init__(self, existing=None, corrupt_upload=False):
        self.releases = {}
        self.blobs = {}
        self.calls = []
        self.corrupt_upload = corrupt_upload
        if existing:
            self.releases[existing["tag_name"]] = existing

    def release_by_tag(self, tag):
        self.calls.append(("get", tag))
        value = self.releases.get(tag)
        return json.loads(json.dumps(value)) if value else None

    def create_draft(self, tag, target, title, body):
        self.calls.append(("create", tag, target))
        self.releases[tag] = {"id": 7, "tag_name": tag, "draft": True, "assets": []}
        return dict(self.releases[tag])

    def _by_id(self, release_id):
        return next(item for item in self.releases.values() if item["id"] == release_id)

    def delete_asset(self, release_id, asset_id):
        self.calls.append(("delete", asset_id))
        release = self._by_id(release_id)
        release["assets"] = [a for a in release["assets"] if a["id"] != asset_id]

    def upload_asset(self, release_id, path):
        data = Path(path).read_bytes()
        if self.corrupt_upload and path.name == builder.INSTALLER_NAME:
            data = data[:-1] + b"X"
        url = f"http://forgejo.thegoodhunter.vps/dl/{path.name}"
        self.blobs[url] = data
        release = self._by_id(release_id)
        release["assets"].append({"id": len(release["assets"]) + 100, "name": path.name,
                                  "size": len(data), "browser_download_url": url})
        self.calls.append(("upload", path.name))

    def download(self, url):
        return self.blobs.get(url)

    def publish(self, release_id):
        self.calls.append(("publish", release_id))
        release = self._by_id(release_id)
        release["draft"] = False
        return release


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.release = Path(self.temporary.name)
        (self.release / builder.INSTALLER_NAME).write_bytes(b"MZ setup bytes")
        builder.write_metadata(self.release, VERSION)

    def tearDown(self):
        self.temporary.cleanup()

    def test_upload_as_draft_verify_then_publish_and_rerun_is_idempotent(self):
        forgejo = FakeForgejo()
        result = builder.publish(forgejo, self.release, VERSION, "abc123", log=lambda m: None)
        self.assertFalse(result["draft"])
        kinds = [call[0] for call in forgejo.calls]
        self.assertLess(kinds.index("create"), kinds.index("upload"))
        self.assertEqual(kinds[-1], "publish")
        self.assertEqual(kinds.count("upload"), 3)

        before = len(forgejo.calls)
        builder.publish(forgejo, self.release, VERSION, "abc123", log=lambda m: None)
        self.assertNotIn("upload", [call[0] for call in forgejo.calls[before:]])

    def test_corrupted_upload_never_gets_published(self):
        forgejo = FakeForgejo(corrupt_upload=True)
        with self.assertRaises(builder.BuildError):
            builder.publish(forgejo, self.release, VERSION, "abc123", log=lambda m: None)
        self.assertNotIn("publish", [call[0] for call in forgejo.calls])
        self.assertTrue(forgejo.releases[f"v{VERSION}"]["draft"])

    def test_leftover_draft_is_cleaned_and_completed(self):
        leftover = {"id": 7, "tag_name": f"v{VERSION}", "draft": True,
                    "assets": [{"id": 1, "name": builder.INSTALLER_NAME, "size": 3,
                                "browser_download_url": "http://forgejo.thegoodhunter.vps/old"}]}
        forgejo = FakeForgejo(existing=leftover)
        builder.publish(forgejo, self.release, VERSION, "abc123", log=lambda m: None)
        self.assertIn(("delete", 1), forgejo.calls)
        self.assertNotIn("create", [call[0] for call in forgejo.calls])
        self.assertFalse(forgejo.releases[f"v{VERSION}"]["draft"])


if __name__ == "__main__":
    unittest.main()
