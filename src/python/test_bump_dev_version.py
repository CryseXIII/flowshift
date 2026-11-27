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
        with mock.patch.object(bump, "released_versions", lambda **_: tags):
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


@unittest.skipUnless(shutil.which("git"), "git is needed for the release tags")
class TagOnlyOnTheRemoteTests(unittest.TestCase):
    """v0.7.4 (27.11.): the tag sits on the release commit, which reaches main only
    with the backport; a build checkout that fetched just main lacks it locally and
    counted 0.7.4-dev.1 -> 0.7.4-dev.2 instead of 0.7.5-dev.1."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        environment = {
            "HOME": str(base), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Attrappe", "GIT_AUTHOR_EMAIL": "attrappe@example.invalid",
            "GIT_COMMITTER_NAME": "Attrappe", "GIT_COMMITTER_EMAIL": "attrappe@example.invalid",
        }
        self.env = mock.patch.dict(os.environ, environment)
        self.env.start()
        self.forgejo = base / "forgejo.git"
        seed = base / "seed"
        self.build = base / "build"
        git = self.git
        git(base, "init", "--quiet", "--bare", "-b", "main", str(self.forgejo))
        git(base, "init", "--quiet", "-b", "main", str(seed))
        (seed / "VERSION").write_text("0.7.3\n", encoding="utf-8")
        git(seed, "add", "VERSION")
        git(seed, "commit", "--quiet", "-m", "Release v0.7.3")
        git(seed, "tag", "-a", "v0.7.3", "-m", "FlowShift 0.7.3")
        (seed / "VERSION").write_text("0.7.4-dev.1\n", encoding="utf-8")
        git(seed, "commit", "--quiet", "-am", "Next fix")
        git(seed, "push", "--quiet", str(self.forgejo), "main", "refs/tags/v0.7.3")
        # Release commit on its own branch, tagged, backport not merged yet.
        git(seed, "checkout", "--quiet", "-b", "release/v0.7.4")
        (seed / "VERSION").write_text("0.7.4\n", encoding="utf-8")
        git(seed, "commit", "--quiet", "-am", "Release v0.7.4")
        git(seed, "tag", "-a", "v0.7.4", "-m", "FlowShift 0.7.4")
        git(seed, "push", "--quiet", str(self.forgejo), "release/v0.7.4", "refs/tags/v0.7.4")
        # The build checkout fetches only main (tags follow only into main's history).
        git(base, "init", "--quiet", "-b", "main", str(self.build))
        git(self.build, "remote", "add", "origin", str(self.forgejo))
        git(self.build, "fetch", "--quiet", "origin", "main:refs/remotes/origin/main")
        git(self.build, "checkout", "--quiet", "-B", "main", "origin/main")
        self.version_file = self.build / "VERSION"

    def tearDown(self):
        self.env.stop()
        self.temporary.cleanup()

    @staticmethod
    def git(cwd, *args):
        return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True,
                              text=True).stdout

    def main(self, *args):
        original = bump.released_versions

        def released_versions(remotes=()):
            self.assertIn("origin", remotes)
            return original(self.build, ("origin",))
        with mock.patch.object(bump, "released_versions", released_versions), \
                mock.patch("sys.stderr"), mock.patch("sys.stdout"):
            return bump.main(["--version-file", str(self.version_file), *args])

    def test_local_tags_miss_the_release_the_remote_has_it(self):
        self.assertEqual(self.git(self.build, "tag", "--list").split(), ["v0.7.3"])
        self.assertEqual(bump.released_versions(self.build), released("0.7.3"))
        self.assertEqual(bump.released_versions(self.build, ("origin",)),
                         released("0.7.3", "0.7.4"))

    def test_bump_after_the_publication_goes_to_the_next_patch(self):
        self.version_file.write_text("0.7.4-dev.2\n", encoding="utf-8")
        self.assertEqual(self.main("--check"), 1)
        self.assertEqual(self.main(), 0)
        self.assertEqual(self.version_file.read_text(encoding="utf-8"), "0.7.5-dev.1\n")
        self.assertEqual(self.main("--check"), 0)

    def test_unreachable_remote_falls_back_to_the_local_tags(self):
        missing = str(Path(self.temporary.name) / "nowhere.git")
        self.assertEqual(bump.released_versions(self.build, (missing,)), released("0.7.3"))

    def test_default_remotes_are_forgejo_and_github(self):
        self.assertEqual(bump.RELEASE_REMOTES[0], "origin")
        self.assertTrue(bump.RELEASE_REMOTES[1].startswith("https://github.com/"))

    def test_remote_tag_names_folds_peeled_lines(self):
        lines = ["abc\trefs/tags/v0.7.4", "def\trefs/tags/v0.7.4^{}",
                 "123\trefs/tags/v0.7.5-dev.1", "456\trefs/heads/main"]
        self.assertEqual(bump.remote_tag_names(lines), ["v0.7.4", "v0.7.4", "v0.7.5-dev.1"])


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
