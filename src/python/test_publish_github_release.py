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
_BUMP_SPEC = importlib.util.spec_from_file_location(
    "bump_dev_version", Path(__file__).resolve().parents[2] / "scripts" / "bump_dev_version.py")
bump_dev_version = importlib.util.module_from_spec(_BUMP_SPEC)
_BUMP_SPEC.loader.exec_module(bump_dev_version)


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

    def test_next_build_run_raises_version_after_a_publication(self):
        if not run(self.forgejo, "tag", "--list", "v0.7.0"):  # AssetsTests published already
            release_script.publish(self.plan(), "0.7.0", "Viktor 25.11. Telegram",
                                   self.publisher, "origin", str(self.github), out=io.StringIO())
        blocked = self.plan().problems
        self.assertTrue(any("already exists" in p and "0.7.1-dev.1" in p
                            and "bump_dev_version.py" in p for p in blocked), blocked)
        # The next build run: main still carries 0.7.0-dev.51, the tag v0.7.0 is out.
        run(self.seed, "fetch", "--quiet", str(self.forgejo), "refs/tags/*:refs/tags/*",
            "refs/heads/release/v0.7.0:refs/heads/release-v0.7.0")
        released = bump_dev_version.released_versions(self.seed)
        current = (self.seed / "VERSION").read_text(encoding="utf-8").strip()
        self.assertIn("0.7.1-dev.1", bump_dev_version.unreleased_problem(current, released))
        with mock.patch.object(bump_dev_version, "released_versions", lambda: released), \
                mock.patch("sys.stdout"):
            self.assertEqual(
                bump_dev_version.main(["--version-file", str(self.seed / "VERSION")]), 0)
        self.assertEqual((self.seed / "VERSION").read_text(encoding="utf-8"), "0.7.1-dev.1\n")
        run(self.seed, "commit", "--quiet", "-am", "Next fix")
        run(self.seed, "push", "--quiet", str(self.forgejo), "main:refs/heads/main")
        plan = self.plan()
        self.assertEqual((plan.main_version, plan.tag), ("0.7.1-dev.1", "v0.7.1"))
        # Only the backport of v0.7.0 is left, it changes no file and keeps VERSION.
        self.assertEqual(len(plan.problems), 1, plan.problems)
        self.assertIn("fast forward", plan.problems[0])
        run(self.seed, "merge", "--quiet", "-s", "ours", "--no-edit", "release-v0.7.0")
        self.assertEqual(run(self.seed, "diff", "--name-only", "HEAD^1", "HEAD"), "")
        run(self.seed, "push", "--quiet", str(self.forgejo), "main:refs/heads/main")
        self.assertEqual(self.plan().problems, [])
        self.assertEqual(bump_dev_version.unreleased_problem("0.7.1-dev.1", released), None)

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


def built_files(directory, version="0.7.0", setup=b"MZ setup attrappe"):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "FlowShift-Setup.exe").write_bytes(setup)
    return {name: path for name, path in
            release_script.release_build.write_release_files(directory, version).items()}


class FakeGitHub:
    """In-memory stand-in for the GitHub release API used by probe and assets."""

    def __init__(self):
        self.store = {}
        self.next_id = 100
        self.published_tags = []
        self.deleted = []
        self.corrupt = None

    def _id(self):
        self.next_id += 1
        return self.next_id

    def releases(self):
        return [json.loads(json.dumps(r)) for r in self.store.values()]

    def release(self, release_id):
        return json.loads(json.dumps(self.store[release_id]))

    def create_draft(self, tag, name, body):
        release_id = self._id()
        self.store[release_id] = {
            "id": release_id, "tag_name": tag, "name": name, "body": body, "draft": True,
            "prerelease": False, "assets": [], "upload_url": "https://uploads.github.com/x{?name}",
            "html_url": f"https://github.com/CryseXIII/flowshift/releases/tag/{tag}"}
        return self.release(release_id)

    def upload(self, release, name, data):
        if self.corrupt == name:
            data = data + b"!"
        asset = {"id": self._id(), "name": name, "size": len(data), "state": "uploaded",
                 "url": f"api-asset/{name}", "data": data.hex(),
                 "browser_download_url": f"https://github.com/CryseXIII/flowshift/releases/download/"
                                         f"{self.store[release['id']]['tag_name']}/{name}"}
        self.store[release["id"]]["assets"].append(asset)
        return asset

    def delete_asset(self, asset_id):
        for release in self.store.values():
            release["assets"] = [a for a in release["assets"] if a["id"] != asset_id]

    def download_asset(self, asset, max_bytes):
        return bytes.fromhex(asset["data"])

    def publish(self, release_id):
        self.store[release_id]["draft"] = False
        self.published_tags.append(self.store[release_id]["tag_name"])
        return self.release(release_id)

    def delete_release(self, release_id):
        self.deleted.append(release_id)
        del self.store[release_id]

    def status(self, url):
        return 404

    def latest_fetch(self, url, max_bytes, accept=""):
        latest = [r for r in self.store.values() if not r["draft"]][-1]
        if url == release_script.update_model.LATEST_RELEASE_API:
            return json.dumps(latest).encode()
        for asset in latest["assets"]:
            if asset["browser_download_url"] == url:
                return bytes.fromhex(asset["data"])
        raise KeyError(url)


class GitHubClientTests(unittest.TestCase):
    def test_token_is_only_asked_inside_flowshift_github(self):
        with mock.patch.dict(os.environ, {"FLOWSHIFT_GITHUB_RELEASE": ""}):
            with self.assertRaises(ReleaseError):
                release_script.github_token()

    def test_asset_download_follows_the_redirect_without_the_token(self):
        calls = []

        def transport(method, url, headers, data=None):
            calls.append((url, dict(headers)))
            if url.startswith("https://api.github.com/"):
                return 302, {"Location": "https://objects.githubusercontent.com/f"}, b""
            return 200, {}, b"inhalt"

        api = release_script.GitHub("attrappe", transport=transport)
        body = api.download_asset({"name": "a", "url": "https://api.github.com/repos/x/assets/1"}, 100)
        self.assertEqual(body, b"inhalt")
        self.assertIn("Authorization", calls[0][1])
        self.assertNotIn("Authorization", calls[1][1])

    def test_api_errors_name_status_and_message_but_not_the_token(self):
        def transport(method, url, headers, data=None):
            return 403, {}, json.dumps({"message": "Resource not accessible"}).encode()

        api = release_script.GitHub("attrappe", transport=transport)
        with self.assertRaises(ReleaseError) as caught:
            api.releases()
        self.assertIn("HTTP 403 Resource not accessible", str(caught.exception))
        self.assertNotIn("attrappe", str(caught.exception))

    def test_probe_creates_and_deletes_a_draft(self):
        api = FakeGitHub()
        self.assertTrue(release_script.probe(api, out=io.StringIO(), now=lambda: 1))
        self.assertEqual(api.store, {})
        self.assertEqual(len(api.deleted), 1)
        self.assertEqual(api.published_tags, [])


class LocalCheckTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.files = built_files(Path(self.temporary.name) / "release")

    def tearDown(self):
        self.temporary.cleanup()

    def test_built_files_pass_the_updater_validation(self):
        payload, fetch = release_script.local_release_payload("0.7.0", self.files)
        descriptor = release_script.check_release(payload, fetch, "0.7.0", download_setup=True)
        self.assertEqual(descriptor.installer_size, len(b"MZ setup attrappe"))

    def test_a_tampered_manifest_or_setup_is_rejected(self):
        manifest = self.files["update-manifest.json"]
        manifest.write_bytes(manifest.read_bytes().replace(b'"stable"', b'"beta"'))
        payload, fetch = release_script.local_release_payload("0.7.0", self.files)
        with self.assertRaises(ReleaseError):
            release_script.check_release(payload, fetch, "0.7.0")
        files = built_files(Path(self.temporary.name) / "second")
        files["FlowShift-Setup.exe"].write_bytes(b"MZ other setup")
        payload, fetch = release_script.local_release_payload("0.7.0", files)
        with self.assertRaises(ReleaseError):
            release_script.check_release(payload, fetch, "0.7.0", download_setup=True)

    def test_draft_check_rejects_an_asset_that_differs_after_upload(self):
        api = FakeGitHub()
        api.corrupt = "FlowShift-Setup.exe"
        draft = api.create_draft("v0.7.0", "FlowShift 0.7.0", "")
        for name, path in self.files.items():
            api.upload(draft, name, path.read_bytes())
        with self.assertRaises(ReleaseError):
            release_script.check_draft(api, draft["id"], self.files, "0.7.0")


class AssetsTests(PublishTests):
    """assets on top of a real publish into the stand-in repositories."""

    def setUp(self):
        super().setUp()
        release_script.publish(self.plan(), "0.7.0", "Viktor 24.11. Telegram", self.publisher,
                               "origin", str(self.github), out=io.StringIO())
        self.api = FakeGitHub()
        self.builds = []
        self.red_options = []

    def fake_build(self, source, version, work, tools, log=None, red_webgui_tests_log_only=False):
        self.builds.append((Path(source) / "VERSION").read_text(encoding="utf-8").strip())
        self.red_options.append(red_webgui_tests_log_only)
        return built_files(Path(work) / "release", version)

    def run_assets(self, version="0.7.0", freigabe="Viktor 24.11. 21:54 Telegram", **options):
        return release_script.assets(
            version, freigabe, self.api, self.publisher, "origin", str(self.github),
            out=io.StringIO(), toolchain=lambda log=None: None, build=self.fake_build,
            verify_fetch=self.api.latest_fetch, sleep=lambda seconds: None,
            work=Path(self.temporary.name) / "work", **options)

    def test_builds_from_the_tag_checks_uploads_and_publishes(self):
        descriptor = self.run_assets()
        self.assertEqual(self.builds, ["0.7.0"])
        self.assertEqual(self.red_options, [False])  # never relaxed by default
        self.assertEqual(descriptor.tag, "v0.7.0")
        self.assertEqual(self.api.published_tags, ["v0.7.0"])
        (release,) = self.api.store.values()
        self.assertEqual(sorted(a["name"] for a in release["assets"]),
                         ["FlowShift-Setup.exe", "SHA256SUMS.txt", "update-manifest.json"])
        self.assertIn("Command wheel and stable updates", release["body"])
        self.assertEqual(run(self.publisher, "worktree", "list").count("\n"), 0)
        # A published release is never replaced.
        with self.assertRaises(ReleaseError):
            self.run_assets()

    def test_nothing_is_published_when_the_upload_differs(self):
        self.api.corrupt = "update-manifest.json"
        with self.assertRaises(ReleaseError):
            self.run_assets()
        self.assertEqual(self.api.published_tags, [])
        (release,) = self.api.store.values()
        self.assertTrue(release["draft"])
        # The next run reuses the draft and replaces its assets.
        self.api.corrupt = None
        self.run_assets()
        (release,) = self.api.store.values()
        self.assertEqual(len(release["assets"]), 3)
        self.assertEqual(self.api.published_tags, ["v0.7.0"])

    def test_next_plan_names_the_unmerged_release_branch(self):
        (self.seed / "VERSION").write_text("0.7.1-dev.1\n", encoding="utf-8")
        run(self.seed, "commit", "--quiet", "-am", "Next development version")
        run(self.seed, "push", "--quiet", str(self.forgejo), "main:refs/heads/main")
        problems = self.plan().problems
        self.assertTrue(any("git merge -s ours origin/release/v0.7.0" in p for p in problems),
                        problems)

    def test_red_webgui_option_reaches_the_build_only_when_given(self):
        self.run_assets(red_webgui_tests_log_only=True)
        self.assertEqual(self.red_options, [True])
        release_script.build_only(
            "0.7.0", self.publisher, "origin", out=io.StringIO(),
            toolchain=lambda log=None: None, build=self.fake_build,
            work=Path(self.temporary.name) / "work2", red_webgui_tests_log_only=True)
        self.assertEqual(self.red_options, [True, True])

    def test_build_only_checks_and_uploads_nothing(self):
        files = release_script.build_only(
            "0.7.0", self.publisher, "origin", out=io.StringIO(),
            toolchain=lambda log=None: None, build=self.fake_build,
            work=Path(self.temporary.name) / "work")
        self.assertEqual(sorted(files), ["FlowShift-Setup.exe", "SHA256SUMS.txt",
                                         "update-manifest.json"])
        self.assertEqual(self.builds, ["0.7.0"])
        self.assertEqual(self.api.store, {})
        with self.assertRaises(ReleaseError):
            release_script.build_only("0.9.0", self.publisher, "origin", out=io.StringIO(),
                                      toolchain=lambda log=None: None, build=self.fake_build)

    def test_a_failing_build_step_publishes_nothing(self):
        def failing_build(source, version, work, tools, log=None, red_webgui_tests_log_only=False):
            raise release_script.release_build.BuildError("npm test (vitest) failed with exit 1")
        with self.assertRaises(release_script.release_build.BuildError):
            release_script.assets(
                "0.7.0", "Viktor 24.11. 21:54 Telegram", self.api, self.publisher, "origin",
                str(self.github), out=io.StringIO(), toolchain=lambda log=None: None,
                build=failing_build, work=Path(self.temporary.name) / "work")
        self.assertEqual(self.api.store, {})
        self.assertEqual(run(self.publisher, "worktree", "list").count("\n"), 0)

    def test_needs_approval_and_the_same_tag_on_both_sides(self):
        with self.assertRaises(ReleaseError):
            self.run_assets(freigabe="")
        with self.assertRaises(ReleaseError):
            self.run_assets(version="0.7.1")
        self.assertEqual(self.builds, [])
        self.assertEqual(self.api.store, {})

    def test_build_from_main_builds_the_next_version_before_any_approval(self):
        (self.seed / "VERSION").write_text("0.7.1-dev.2\n", encoding="utf-8")
        run(self.seed, "commit", "--quiet", "-am", "Fix WebGUI tests and Shift-click range")
        run(self.seed, "push", "--quiet", str(self.forgejo), "main:refs/heads/main")
        forgejo_main = run(self.forgejo, "rev-parse", "refs/heads/main")
        tags_before = (run(self.forgejo, "tag", "--list"), run(self.github, "tag", "--list"))
        files = release_script.build_from_main(
            "0.7.1", self.publisher, "origin", out=io.StringIO(),
            toolchain=lambda log=None: None, build=self.fake_build,
            work=Path(self.temporary.name) / "work-main")
        self.assertEqual(sorted(files), ["FlowShift-Setup.exe", "SHA256SUMS.txt",
                                         "update-manifest.json"])
        # Built as 0.7.1 from main, strict, and nothing changed anywhere.
        self.assertEqual(self.builds, ["0.7.1"])
        self.assertEqual(self.red_options, [False])
        self.assertEqual(self.api.store, {})
        self.assertEqual((run(self.forgejo, "tag", "--list"), run(self.github, "tag", "--list")),
                         tags_before)
        self.assertEqual(run(self.forgejo, "rev-parse", "refs/heads/main"), forgejo_main)
        self.assertEqual(run(self.publisher, "worktree", "list").count("\n"), 0)
        # Another version than the one main is heading for is refused.
        for wrong in ("0.7.2", "0.7.1-dev.2"):
            with self.assertRaises(ReleaseError):
                release_script.build_from_main(wrong, self.publisher, "origin", out=io.StringIO(),
                                               toolchain=lambda log=None: None,
                                               build=self.fake_build)
        self.assertEqual(self.builds, ["0.7.1"])

    # The inherited publish tests are covered by PublishTests already.
    test_plan_is_read_only_and_lists_the_changes = None
    test_publish_needs_approval_and_the_planned_version = None
    test_publish_creates_the_release_on_both_sides_without_touching_main = None
    test_diverged_github_master_blocks_instead_of_forcing = None


class CommandLineTests(unittest.TestCase):
    def test_red_webgui_option_is_explicit_for_assets_and_build(self):
        with mock.patch.object(release_script, "build_only") as build_only:
            self.assertEqual(release_script.main(["build", "--version", "0.7.0"]), 0)
            self.assertEqual(release_script.main(
                ["build", "--version", "0.7.0", "--rote-webgui-tests-nur-protokollieren"]), 0)
        self.assertEqual([c.kwargs["red_webgui_tests_log_only"] for c in build_only.call_args_list],
                         [False, True])
        with mock.patch.object(release_script, "build_from_main") as from_main, \
                mock.patch.object(release_script, "build_only") as build_only:
            self.assertEqual(release_script.main(["build", "--version", "0.7.1", "--vom-main"]), 0)
        from_main.assert_called_once_with("0.7.1")
        build_only.assert_not_called()
        # The main build is always strict: no option for red WebGUI tests.
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", io.StringIO()):
            release_script.main(["build", "--version", "0.7.1", "--vom-main",
                                 "--rote-webgui-tests-nur-protokollieren"])
        with mock.patch.object(release_script, "assets") as assets, \
                mock.patch.object(release_script, "github_token", return_value="attrappe"):
            release_script.main(["assets", "--version", "0.7.0", "--freigabe", "Viktor 25.11. 16:50",
                                 "--rote-webgui-tests-nur-protokollieren"])
            release_script.main(["assets", "--version", "0.7.0", "--freigabe", "Viktor 25.11. 16:50"])
        self.assertEqual([c.kwargs["red_webgui_tests_log_only"] for c in assets.call_args_list],
                         [True, False])
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", io.StringIO()):
            release_script.main(["publish", "--version", "0.7.0", "--freigabe", "Viktor",
                                 "--rote-webgui-tests-nur-protokollieren"])


VITEST_RED = ("\x1b[31m FAIL \x1b[39m src/overlay/ClipboardOverlay.test.jsx > shift click\n"
              " Test Files  3 failed | 20 passed (23)\n"
              "      Tests  \x1b[1m\x1b[31m7 failed\x1b[39m\x1b[22m | \x1b[32m125 passed\x1b[39m (132)\n"
              "   Duration  9.10s\n")


class WebguiTestOptionTests(unittest.TestCase):
    """build_assets: a red vitest run stops the build unless the explicit option is set."""

    build = release_script.release_build

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.source = Path(self.temporary.name) / "source"
        (self.source / "webgui").mkdir(parents=True)
        (self.source / "VERSION").write_text("0.7.0\n", encoding="utf-8")
        self.steps = []
        self.lines = []

    def fake_execute(self, vitest_exit, vitest_output):
        def execute(args, cwd=None, env=None, log=None, timeout=3600, label=None):
            self.steps.append(" ".join(str(a) for a in args))
            code, output = (vitest_exit, vitest_output) if list(args) == ["npm", "test"] else (0, "ok")
            if log:
                log(f"$ {label}  (exit {code})", output)
            return subprocess.CompletedProcess(args, code, output)
        return execute

    def build_with(self, vitest_exit, vitest_output, **options):
        tools = mock.Mock(iscc="C:\\InnoSetup6\\ISCC.exe")
        tools.node_env.return_value = {}
        with mock.patch.object(self.build, "_execute", self.fake_execute(vitest_exit, vitest_output)), \
                mock.patch.object(self.build, "stage_payload", return_value=Path(self.temporary.name)), \
                mock.patch.object(self.build, "write_release_files", return_value={"built": True}):
            result = self.build.build_assets(
                self.source, "0.7.0", Path(self.temporary.name) / "work", tools,
                log=lambda line, detail=None: self.lines.append(line), run_tests=False, **options)
        self.assertTrue(tools.wine.called)
        return result

    def test_vitest_counts_reads_the_summary_line(self):
        self.assertEqual(self.build.vitest_counts(VITEST_RED), (7, 132))
        self.assertEqual(self.build.vitest_counts("      Tests  132 passed (132)\n"), (0, 132))
        self.assertIsNone(self.build.vitest_counts("SyntaxError: Unexpected token"))

    def test_without_the_option_a_red_vitest_run_stops_the_build(self):
        with self.assertRaises(self.build.BuildError) as raised:
            self.build_with(1, VITEST_RED)
        self.assertIn("npm test (vitest) failed with exit 1", str(raised.exception))
        self.assertNotIn("npm run build", self.steps)

    def test_with_the_option_the_build_goes_on_and_logs_the_red_tests(self):
        self.assertEqual(self.build_with(1, VITEST_RED, red_webgui_tests_log_only=True),
                         {"built": True})
        self.assertEqual(self.steps[-1], "npm run build")
        warning = [line for line in self.lines if line.startswith("WARNUNG")]
        self.assertEqual(len(warning), 1, self.lines)
        self.assertIn("7 von 132", warning[0])

    def test_the_option_does_not_hide_a_vitest_crash_or_other_red_steps(self):
        with self.assertRaises(self.build.BuildError):
            self.build_with(1, "Error: Cannot find module 'vitest'\n", red_webgui_tests_log_only=True)
        with self.assertRaises(self.build.BuildError):
            self.build_with(1, " Test Files  1 failed (1)\n      Tests  0 passed (0)\n",
                            red_webgui_tests_log_only=True)
        self.assertNotIn("npm run build", self.steps)

    def test_green_vitest_logs_no_warning(self):
        self.build_with(0, "      Tests  132 passed (132)\n", red_webgui_tests_log_only=True)
        self.assertFalse([line for line in self.lines if line.startswith("WARNUNG")])


class ReleaseBuildTests(unittest.TestCase):
    build = release_script.release_build

    def test_manifest_and_checksums_follow_build_release_ps1(self):
        with tempfile.TemporaryDirectory() as directory:
            files = built_files(directory, "0.7.0", b"MZ abc")
            manifest = json.loads(files["update-manifest.json"].read_bytes())
            self.assertEqual(manifest, {
                "schema_version": 1, "version": "0.7.0", "tag": "v0.7.0", "channel": "stable",
                "installer": {"name": "FlowShift-Setup.exe", "size": 6,
                              "sha256": hashlib.sha256(b"MZ abc").hexdigest()},
                "minimum_updater_version": "0.4.0"})
            sums = files["SHA256SUMS.txt"].read_text(encoding="utf-8").splitlines()
            self.assertEqual(sums, [
                f"{hashlib.sha256(b'MZ abc').hexdigest()}  FlowShift-Setup.exe",
                f"{hashlib.sha256(files['update-manifest.json'].read_bytes()).hexdigest()}  "
                "update-manifest.json"])

    def test_a_non_windows_installer_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(self.build.BuildError):
                built_files(directory, "0.7.0", b"ELF")

    def test_payload_lists_come_from_the_powershell_packaging(self):
        files, required, forbidden = self.build.payload_rules(Path(__file__).resolve().parents[2])
        self.assertIn("install_flowshift.ps1", files)
        self.assertIn("src/python/update_model.py", files)
        self.assertIn("src/python/input_backends/windows_win32.py", files)
        self.assertIn("webgui/dist/VERSION", required)
        self.assertIn("webgui/src", forbidden)
        self.assertNotIn("src/python/service.py", files)

    def test_stage_payload_copies_the_lists_and_checks_the_webgui_version(self):
        repo = Path(__file__).resolve().parents[2]
        files, _, _ = self.build.payload_rules(repo)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            for relative in files + ["packaging/build_release.ps1",
                                     "packaging/test_release_packaging.ps1",
                                     "packaging/FlowShift.iss"]:
                (source / relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(repo / relative, source / relative)
            shutil.copytree(repo / "docs", source / "docs")
            dist = source / "webgui/dist"
            dist.mkdir(parents=True)
            for name in ("index.html", "overlay.html"):
                (dist / name).write_text("<html></html>", encoding="utf-8")
            (dist / "VERSION").write_text("0.6.9\n", encoding="utf-8")
            with self.assertRaises(self.build.BuildError):
                self.build.stage_payload(source, Path(directory) / "payload", "0.7.0")
            (dist / "VERSION").write_text("0.7.0\n", encoding="utf-8")
            payload = self.build.stage_payload(source, Path(directory) / "payload", "0.7.0")
            self.assertTrue((payload / "src/python/update_model.py").is_file())
            self.assertTrue((payload / "webgui/dist/VERSION").is_file())
            self.assertFalse((payload / "src/python/service.py").exists())


if __name__ == "__main__":
    unittest.main()
