"""scripts/publish_github_release.py against local stand-in repositories.

Two bare repositories play Forgejo and GitHub; nothing leaves this machine.
The tests prove the one-step publication: plan is read-only, publish needs an
approval text and the planned version, pushes a release commit (VERSION =
X.Y.Z) and the tag to both sides without force and leaves Forgejo main alone;
verify accepts only a release the installed updater would accept.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_SPEC = importlib.util.spec_from_file_location(
    "publish_github_release",
    Path(__file__).resolve().parents[2] / "scripts" / "publish_github_release.py")
release_script = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = release_script  # dataclasses look the module up there
_SPEC.loader.exec_module(release_script)
ReleaseError = release_script.ReleaseError


def run(cwd, *args):
    completed = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if completed.returncode != 0:
        raise AssertionError(f"git {args}: {completed.stderr}")
    return completed.stdout.strip()


class VersionHelperTests(unittest.TestCase):
    def test_stable_target_and_next_dev(self):
        self.assertEqual(release_script.stable_target("0.7.0-dev.51\n"), "0.7.0")
        self.assertEqual(release_script.stable_target("0.7.0"), "0.7.0")
        self.assertEqual(release_script.next_dev_after("0.7.0"), "0.7.1-dev.1")
        with self.assertRaises(ReleaseError):
            release_script.stable_target("0.7.0+build.1")

    def test_stable_tags_ignore_prereleases_and_peeled_refs(self):
        listing = "\n".join([
            "a\trefs/tags/v0.5.4", "b\trefs/tags/v0.6.3", "c\trefs/tags/v0.6.3^{}",
            "d\trefs/tags/v0.7.0-dev.5", "e\trefs/tags/v0.10.0", "f\trefs/tags/latest",
        ])
        self.assertEqual(release_script.stable_tags(listing), ["v0.5.4", "v0.6.3", "v0.10.0"])


@unittest.skipUnless(shutil.which("git"), "git is needed on the publishing machine only")
class PublishTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        environment = {
            "HOME": str(root), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Attrappe", "GIT_AUTHOR_EMAIL": "attrappe@example.invalid",
            "GIT_COMMITTER_NAME": "Attrappe", "GIT_COMMITTER_EMAIL": "attrappe@example.invalid",
            "GIT_TERMINAL_PROMPT": "0",
        }
        self.env = mock.patch.dict(os.environ, environment)
        self.env.start()
        self.forgejo = root / "forgejo.git"
        self.github = root / "github.git"
        for bare in (self.forgejo, self.github):
            run(root, "init", "--quiet", "--bare", str(bare))
        seed = root / "seed"
        run(root, "init", "--quiet", "-b", "main", str(seed))
        (seed / "VERSION").write_text("0.6.3\n", encoding="utf-8")
        run(seed, "add", "VERSION")
        run(seed, "commit", "--quiet", "-m", "Release v0.6.3")
        run(seed, "tag", "-a", "v0.6.3", "-m", "FlowShift 0.6.3")
        run(seed, "push", "--quiet", str(self.github), "main:refs/heads/master", "refs/tags/v0.6.3")
        (seed / "VERSION").write_text("0.7.0-dev.51\n", encoding="utf-8")
        (seed / "feature.txt").write_text("wheel\n", encoding="utf-8")
        run(seed, "add", "VERSION", "feature.txt")
        run(seed, "commit", "--quiet", "-m", "Command wheel and stable updates")
        run(seed, "push", "--quiet", str(self.forgejo), "main:refs/heads/main", "refs/tags/v0.6.3")
        self.seed = seed
        self.publisher = root / "publisher"
        run(root, "clone", "--quiet", str(self.forgejo), str(self.publisher))

    def tearDown(self):
        self.env.stop()
        self.temporary.cleanup()

    def plan(self):
        return release_script.make_plan(self.publisher, "origin", str(self.github))

    def test_plan_is_read_only_and_lists_the_changes(self):
        github_before = run(self.github, "rev-parse", "refs/heads/master")
        plan = self.plan()
        self.assertEqual((plan.main_version, plan.target, plan.last_github_tag),
                         ("0.7.0-dev.51", "0.7.0", "v0.6.3"))
        self.assertEqual(plan.changes, ["Command wheel and stable updates"])
        self.assertEqual(plan.problems, [])
        self.assertEqual(run(self.github, "rev-parse", "refs/heads/master"), github_before)
        self.assertEqual(run(self.github, "tag", "--list"), "v0.6.3")
        out = io.StringIO()
        release_script.print_plan(plan, out)
        self.assertIn("Would publish:  v0.7.0", out.getvalue())
        self.assertIn("approval", out.getvalue())

    def test_publish_needs_approval_and_the_planned_version(self):
        plan = self.plan()
        with self.assertRaises(ReleaseError):
            release_script.publish(plan, "0.7.0", "", self.publisher, "origin", str(self.github))
        with self.assertRaises(ReleaseError):
            release_script.publish(plan, "0.8.0", "Viktor 25.11. Telegram", self.publisher,
                                   "origin", str(self.github))
        self.assertEqual(run(self.github, "tag", "--list"), "v0.6.3")

    def test_publish_creates_the_release_on_both_sides_without_touching_main(self):
        forgejo_main = run(self.forgejo, "rev-parse", "refs/heads/main")
        commit = release_script.publish(self.plan(), "0.7.0", "Viktor 25.11. Telegram",
                                        self.publisher, "origin", str(self.github),
                                        out=io.StringIO())
        self.assertEqual(run(self.github, "rev-parse", "refs/heads/master"), commit)
        self.assertEqual(run(self.github, "show", "refs/heads/master:VERSION"), "0.7.0")
        self.assertEqual(run(self.github, "rev-parse", "v0.7.0^{commit}"), commit)
        self.assertEqual(run(self.forgejo, "rev-parse", "v0.7.0^{commit}"), commit)
        self.assertEqual(run(self.forgejo, "rev-parse", "refs/heads/release/v0.7.0"), commit)
        self.assertEqual(run(self.forgejo, "rev-parse", "refs/heads/main"), forgejo_main)
        self.assertIn("Approved by Viktor: Viktor 25.11. Telegram",
                      run(self.github, "log", "-1", "--format=%B", commit))
        self.assertEqual(run(self.publisher, "worktree", "list").count("\n"), 0)
        # The same version cannot be published twice.
        self.assertTrue(any("already exists" in problem for problem in self.plan().problems))

    def test_diverged_github_master_blocks_instead_of_forcing(self):
        other = Path(self.temporary.name) / "other"
        run(self.publisher.parent, "clone", "--quiet", "-b", "master", str(self.github), str(other))
        (other / "hotfix.txt").write_text("only on GitHub\n", encoding="utf-8")
        run(other, "add", "hotfix.txt")
        run(other, "commit", "--quiet", "-m", "Hotfix only on GitHub")
        run(other, "push", "--quiet", "origin", "master")
        plan = self.plan()
        self.assertTrue(any("fast forward" in problem for problem in plan.problems))
        with self.assertRaises(ReleaseError):
            release_script.publish(plan, "0.7.0", "Viktor 25.11. Telegram", self.publisher,
                                   "origin", str(self.github))


class VerifyTests(unittest.TestCase):
    def published(self, version="0.7.0", sums_digest=None):
        setup = b"setup attrappe"
        digest = hashlib.sha256(setup).hexdigest()
        base = f"https://github.com/CryseXIII/flowshift/releases/download/v{version}"
        payload = {
            "tag_name": f"v{version}", "draft": False, "prerelease": False,
            "html_url": f"https://github.com/CryseXIII/flowshift/releases/tag/v{version}",
            "body": "", "assets": [
                {"name": "FlowShift-Setup.exe", "size": len(setup),
                 "browser_download_url": f"{base}/FlowShift-Setup.exe"},
                {"name": "update-manifest.json", "size": 1,
                 "browser_download_url": f"{base}/update-manifest.json"},
                {"name": "SHA256SUMS.txt", "size": 1,
                 "browser_download_url": f"{base}/SHA256SUMS.txt"},
            ]}
        manifest = {"schema_version": 1, "version": version, "tag": f"v{version}",
                    "channel": "stable", "minimum_updater_version": "0.6.0",
                    "installer": {"name": "FlowShift-Setup.exe", "size": len(setup),
                                  "sha256": digest}}
        files = {
            release_script.update_model.LATEST_RELEASE_API: json.dumps(payload).encode(),
            f"{base}/update-manifest.json": json.dumps(manifest).encode(),
            f"{base}/SHA256SUMS.txt": f"{sums_digest or digest}  FlowShift-Setup.exe\n".encode(),
            f"{base}/FlowShift-Setup.exe": setup,
        }
        return lambda url, max_bytes, accept="": files[url]

    def test_accepts_a_complete_release(self):
        descriptor = release_script.verify("0.7.0", fetch=self.published(),
                                           download_setup=True, out=io.StringIO())
        self.assertEqual(descriptor.tag, "v0.7.0")

    def test_rejects_wrong_latest_and_disagreeing_checksums(self):
        with self.assertRaises(ReleaseError):
            release_script.verify("0.7.1", fetch=self.published(), out=io.StringIO())
        with self.assertRaises(ReleaseError):
            release_script.verify("0.7.0", fetch=self.published(sums_digest="0" * 64),
                                  out=io.StringIO())


if __name__ == "__main__":
    unittest.main()
