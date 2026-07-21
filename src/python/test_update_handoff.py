"""Offline tests for the external updater handoff boundary."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import update_handoff as handoff
from update_download import DownloadedAsset
from update_model import ReleaseDescriptor


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.program_files = self.root / "Program Files"
        self.program_data = self.root / "ProgramData"
        self.install_dir = self.program_files / "FlowShift"
        self.data_dir = self.program_data / "FlowShift"
        self.install_dir.mkdir(parents=True)
        (self.install_dir / handoff.RUNNER_NAME).write_text("# runner\n", encoding="utf-8")
        self.content = b"verified setup bytes"
        self.digest = hashlib.sha256(self.content).hexdigest()
        self.version = "0.5.0"
        self.installer = (self.data_dir / "updates" / "downloads" /
                          f"FlowShift-Setup-{self.version}.exe")
        self.installer.parent.mkdir(parents=True)
        self.installer.write_bytes(self.content)
        self.release = ReleaseDescriptor(
            current_version="0.4.0",
            version=self.version,
            tag=f"v{self.version}",
            relation="newer",
            release_url=f"https://github.com/CryseXIII/flowshift/releases/tag/v{self.version}",
            release_notes="notes",
            installer_url=("https://github.com/CryseXIII/flowshift/releases/download/"
                           f"v{self.version}/FlowShift-Setup.exe"),
            installer_size=len(self.content),
            installer_sha256=self.digest,
            manifest_url="https://github.com/CryseXIII/flowshift/update-manifest.json",
            checksums_url="https://github.com/CryseXIII/flowshift/SHA256SUMS.txt",
            minimum_updater_version="0.4.0",
        )
        self.asset = DownloadedAsset(
            str(self.installer), self.installer.name, self.version,
            len(self.content), self.digest,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def call(self, launcher, **changes):
        values = {
            "release": self.release,
            "asset": self.asset,
            "runtime_root": self.install_dir,
            "runtime_pid": 1234,
            "launcher": launcher,
            "ack_timeout": 0,
            "_program_files": self.program_files,
            "_program_data": self.program_data,
            "_request_id": "a" * 32,
            "_created_at": "2026-07-21T12:00:00Z",
        }
        values.update(changes)
        return handoff.handoff_update(**values)

    def accepting_launcher(self, calls=None):
        def launch(arguments, **kwargs):
            if calls is not None:
                calls.append((arguments, kwargs))
            plan_path = Path(arguments[arguments.index("-PlanPath") + 1])
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            ack = self.data_dir / "updates" / "acks" / f"update_ack-{plan['request_id']}.json"
            ack.parent.mkdir(parents=True, exist_ok=True)
            ack.write_text(json.dumps({
                "schema_version": 1,
                "request_id": plan["request_id"],
                "status": "accepted",
                "runner_pid": 9876,
            }), encoding="utf-8")
            return object()
        return launch

    def test_rejects_development_layout_and_path_traversal(self):
        with self.assertRaises(handoff.UpdateHandoffError) as raised:
            self.call(self.accepting_launcher(), runtime_root=self.root / "checkout")
        self.assertEqual(raised.exception.code, "development_mode")

        escaped = replace(self.asset, path=str(self.installer.parent / ".." / "escape.exe"))
        with self.assertRaises(handoff.UpdateHandoffError) as raised:
            self.call(self.accepting_launcher(), asset=escaped)
        self.assertEqual(raised.exception.code, "invalid_asset")

    def test_rejects_non_model_data(self):
        with self.assertRaises(handoff.UpdateHandoffError) as raised:
            self.call(self.accepting_launcher(), release={"installer_url": "https://evil.example"})
        self.assertEqual(raised.exception.code, "unverified_update_data")

    def test_rehashes_and_resizes_before_writing_plan(self):
        self.installer.write_bytes(self.content + b"changed")
        with self.assertRaises(handoff.UpdateHandoffError) as raised:
            self.call(self.accepting_launcher())
        self.assertEqual(raised.exception.code, "size_mismatch")
        self.assertFalse((self.data_dir / "updates" / "update_plan.json").exists())

        self.installer.write_bytes(b"X" * len(self.content))
        with self.assertRaises(handoff.UpdateHandoffError) as raised:
            self.call(self.accepting_launcher())
        self.assertEqual(raised.exception.code, "hash_mismatch")

    def test_plan_is_atomic_and_contains_only_fixed_execution_values(self):
        replacements = []
        real_replace = handoff.os.replace

        def recording_replace(source, destination):
            replacements.append((Path(source), Path(destination)))
            return real_replace(source, destination)

        handoff.os.replace = recording_replace
        try:
            result = self.call(self.accepting_launcher())
        finally:
            handoff.os.replace = real_replace
        plan_path = Path(result.plan_path)
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        self.assertEqual(plan["schema_version"], 1)
        self.assertEqual(plan["from_version"], "0.4.0")
        self.assertEqual(plan["to_version"], "0.5.0")
        self.assertEqual(plan["task_name"], "FlowShift")
        self.assertEqual((plan["control_port"], plan["peer_port"], plan["api_port"]),
                         (45782, 45781, 5000))
        self.assertEqual(set(plan), {
            "schema_version", "from_version", "to_version", "installer_path",
            "installer_size", "installer_sha256", "install_dir", "data_dir",
            "created_at", "request_id", "runtime_pid", "task_name",
            "control_port", "peer_port", "api_port",
        })
        self.assertTrue(any(source.suffix == ".tmp" and destination == plan_path
                            for source, destination in replacements))

    def test_copies_runner_before_launch(self):
        observed = []

        def launch(arguments, **kwargs):
            copied = self.data_dir / "updates" / "runner" / handoff.RUNNER_NAME
            observed.append(copied.read_text(encoding="utf-8"))
            return self.accepting_launcher()(arguments, **kwargs)

        self.call(launch)
        self.assertEqual(observed, ["# runner\n"])

    def test_ack_timeout_is_not_reported_as_started(self):
        with self.assertRaises(handoff.UpdateHandoffError) as raised:
            self.call(lambda arguments, **kwargs: object())
        self.assertEqual(raised.exception.code, "ack_timeout")

    def test_request_specific_ack_reports_started(self):
        result = self.call(self.accepting_launcher())
        self.assertEqual(result.status, "started")
        self.assertEqual(result.request_id, "a" * 32)
        self.assertIn(result.request_id, result.ack_path)

    def test_launch_uses_argument_list_without_shell(self):
        calls = []
        self.call(self.accepting_launcher(calls))
        arguments, kwargs = calls[0]
        self.assertIsInstance(arguments, list)
        self.assertFalse(kwargs["shell"])
        self.assertEqual(arguments[4:7], ["-File", str(
            self.data_dir / "updates" / "runner" / handoff.RUNNER_NAME), "-PlanPath"])
        self.assertNotIn("cmd", Path(arguments[0]).name.lower())


if __name__ == "__main__":
    unittest.main()
