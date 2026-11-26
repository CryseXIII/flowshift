"""scripts/bump_dev_version.py: the next free development version after a release.

After a publication of vX.Y.Z, main must not go on with X.Y.Z-dev.N (the
release plan blocks on it): the next build run raises VERSION to
X.Y.(Z+1)-dev.1 with the script, and this test turns red while the repository
VERSION still belongs to a published release tag.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from version import parse_semver  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "bump_dev_version", ROOT / "scripts" / "bump_dev_version.py")
bump = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bump)


def released(*versions):
    return [parse_semver(value) for value in versions]


class NextFreeDevVersionTests(unittest.TestCase):
    def test_counter_goes_on_while_the_release_is_not_out(self):
        self.assertEqual(bump.next_free_dev_version("0.7.3-dev.1", released("0.7.2")),
                         "0.7.3-dev.2")
        self.assertEqual(bump.next_free_dev_version("0.7.2-dev.4", released("0.7.1")),
                         "0.7.2-dev.5")
        self.assertEqual(bump.next_free_dev_version("0.7.2-dev.4", []), "0.7.2-dev.5")

    def test_published_release_moves_on_to_the_next_patch(self):
        self.assertEqual(bump.next_free_dev_version("0.7.2-dev.4", released("0.7.1", "0.7.2")),
                         "0.7.3-dev.1")
        # A stable VERSION (release commit) and a main that fell behind a newer tag.
        self.assertEqual(bump.next_free_dev_version("0.7.2", released("0.7.2")), "0.7.3-dev.1")
        self.assertEqual(bump.next_free_dev_version("0.7.1-dev.9", released("0.7.2")),
                         "0.7.3-dev.1")

    def test_unreleased_problem_names_the_command_and_the_version(self):
        problem = bump.unreleased_problem("0.7.2-dev.4", released("0.7.2"))
        self.assertIn("bump_dev_version.py", problem)
        self.assertIn("0.7.3-dev.1", problem)
        self.assertIsNone(bump.unreleased_problem("0.7.3-dev.1", released("0.7.2")))
        self.assertIsNone(bump.unreleased_problem("0.7.2", released("0.7.2")))
        self.assertIsNone(bump.unreleased_problem("0.7.2-dev.4", []))

    def test_explicit_target_keeps_the_old_behaviour(self):
        self.assertEqual(bump.next_dev_version("0.7.2-dev.4", "0.7.2"), "0.7.2-dev.5")
        with self.assertRaises(ValueError):
            bump.next_dev_version("0.7.2-dev.4", "0.7.3")


@unittest.skipUnless(shutil.which("git"), "git is needed for the release tags")
class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        environment = {
            "HOME": str(self.root), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Attrappe", "GIT_AUTHOR_EMAIL": "attrappe@example.invalid",
            "GIT_COMMITTER_NAME": "Attrappe", "GIT_COMMITTER_EMAIL": "attrappe@example.invalid",
        }
        self.env = mock.patch.dict(os.environ, environment)
        self.env.start()
        self.version_file = self.root / "VERSION"
        self.version_file.write_text("0.7.2\n", encoding="utf-8")
        for args in (["init", "--quiet", "-b", "main"], ["add", "VERSION"],
                     ["commit", "--quiet", "-m", "Release v0.7.2"],
                     ["tag", "-a", "v0.7.2", "-m", "FlowShift 0.7.2"],
                     ["tag", "v0.7.3-dev.1"]):
            subprocess.run(["git", *args], cwd=str(self.root), check=True, capture_output=True)
        self.version_file.write_text("0.7.2-dev.4\n", encoding="utf-8")

    def tearDown(self):
        self.env.stop()
        self.temporary.cleanup()

    def main(self, *args):
        tags = bump.released_versions(self.root)
        with mock.patch.object(bump, "released_versions", lambda: tags):
            return bump.main(["--version-file", str(self.version_file), *args])

    def test_released_versions_reads_only_stable_tags(self):
        self.assertEqual(bump.released_versions(self.root), released("0.7.2"))
        self.assertEqual(bump.released_versions(self.root / "missing"), [])

    def test_check_fails_until_the_bump_and_the_bump_skips_the_released_number(self):
        with mock.patch("sys.stderr"), mock.patch("sys.stdout"):
            self.assertEqual(self.main("--check"), 1)
            self.assertEqual(self.main(), 0)
            self.assertEqual(self.version_file.read_text(encoding="utf-8"), "0.7.3-dev.1\n")
            self.assertEqual(self.main("--check"), 0)
            self.assertEqual(self.main(), 0)
            self.assertEqual(self.version_file.read_text(encoding="utf-8"), "0.7.3-dev.2\n")
            # The release commit itself (stable VERSION = its tag) passes the check.
            self.version_file.write_text("0.7.2\n", encoding="utf-8")
            self.assertEqual(self.main("--check", "--release-tag", "v0.7.2"), 0)


class RepositoryVersionTests(unittest.TestCase):
    def test_repository_version_is_not_a_published_release(self):
        """Red after a publication until the next build run raises VERSION (AGENTS.md 7)."""
        current = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        tags = bump.released_versions(ROOT)
        if not tags:
            self.skipTest("no release tags in this checkout")
        self.assertIsNone(bump.unreleased_problem(current, tags))


if __name__ == "__main__":
    unittest.main()
