#!/usr/bin/env python3
"""Publish the current Forgejo main as a stable GitHub release, in one step.

Other PCs only update from GitHub ``releases/latest`` (channel Stable); they
have no Git, no Node.js and no Forgejo access (Viktor 24.11.). This script is
the single, repeatable way to bring the merged Forgejo state there.

It publishes ONLY with Viktor's explicit approval for exactly this version
(AGENTS.md 0.3 and 13). Without ``publish`` it changes nothing:

  plan      read-only: which commit and version would be published, what is
            new since the last GitHub release, and what blocks a publication.
  publish   --version X.Y.Z --freigabe "<who, when, where Viktor approved>"
            creates the release commit (VERSION = X.Y.Z) on top of Forgejo
            main, the tag vX.Y.Z, pushes both to Forgejo (branch
            release/vX.Y.Z and the tag) and then to GitHub (master, fast
            forward only, and the tag). The tag starts the GitHub release
            workflow, which builds FlowShift-Setup.exe and the update manifest.
  verify    --version X.Y.Z, read-only: checks GitHub releases/latest with the
            same validation the installed updater uses (assets, manifest,
            checksums, optionally the Setup.exe hash).

Never forces a push, never moves a tag, never deletes anything remote.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "python"))

import update_model  # noqa: E402  (product validation, same as the updater)
from version import parse_semver  # noqa: E402

FORGEJO_REMOTE = "origin"
FORGEJO_BRANCH = "main"
GITHUB_URL = f"https://github.com/{update_model.REPOSITORY}.git"
GITHUB_BRANCH = "master"
LOCAL_GITHUB_REF = "refs/flowshift-release/github-master"
LOCAL_FORGEJO_REF = "refs/flowshift-release/forgejo-main"
_STABLE = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


class ReleaseError(RuntimeError):
    pass


def git(repo, *args, check=True):
    completed = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)
    if check and completed.returncode != 0:
        raise ReleaseError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def stable_target(version_text):
    """Stable version a main state X.Y.Z-dev.N is released as (X.Y.Z)."""
    parsed = parse_semver(version_text.strip())
    if parsed.build:
        raise ReleaseError(f"VERSION {version_text!r} carries build metadata")
    return f"{parsed.major}.{parsed.minor}.{parsed.patch}"


def stable_tags(ls_remote_output):
    tags = set()
    for line in ls_remote_output.splitlines():
        ref = line.split("\t")[-1].removesuffix("^{}")
        name = ref.removeprefix("refs/tags/")
        if _STABLE.match(name):
            tags.add(name)
    return sorted(tags, key=lambda tag: parse_semver(tag[1:]))


def next_dev_after(version_text):
    parsed = parse_semver(version_text)
    return f"{parsed.major}.{parsed.minor}.{parsed.patch + 1}-dev.1"


@dataclass
class Plan:
    main_commit: str
    main_version: str
    target: str
    last_github_tag: str | None
    github_master: str | None
    changes: list = field(default_factory=list)
    problems: list = field(default_factory=list)

    @property
    def tag(self):
        return f"v{self.target}"


def make_plan(repo=REPO_ROOT, forgejo_remote=FORGEJO_REMOTE, github_url=GITHUB_URL):
    """Read-only (fetch only): what would be published and what blocks it."""
    git(repo, "fetch", "--quiet", "--tags", forgejo_remote,
        f"+refs/heads/{FORGEJO_BRANCH}:{LOCAL_FORGEJO_REF}")
    main_commit = git(repo, "rev-parse", LOCAL_FORGEJO_REF)
    main_version = git(repo, "show", f"{main_commit}:VERSION")
    problems = []
    try:
        target = stable_target(main_version)
    except (ValueError, ReleaseError) as exc:
        raise ReleaseError(f"VERSION of {forgejo_remote}/{FORGEJO_BRANCH} is invalid: {exc}") from exc

    github_tags_raw = git(repo, "ls-remote", "--tags", github_url)
    github_tags = stable_tags(github_tags_raw)
    forgejo_tags = stable_tags(git(repo, "ls-remote", "--tags", forgejo_remote))
    last = github_tags[-1] if github_tags else None
    head = git(repo, "ls-remote", github_url, f"refs/heads/{GITHUB_BRANCH}")
    github_master = head.split()[0] if head else None

    tag = f"v{target}"
    if tag in github_tags or tag in forgejo_tags:
        problems.append(f"{tag} already exists (GitHub or Forgejo); raise VERSION on main "
                        f"first (to {next_dev_after(target)} or higher)")
    if last and parse_semver(target) <= parse_semver(last[1:]):
        problems.append(f"{tag} is not newer than the last GitHub release {last}")
    if github_master:
        git(repo, "fetch", "--quiet", github_url,
            f"+refs/heads/{GITHUB_BRANCH}:{LOCAL_GITHUB_REF}")
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", LOCAL_GITHUB_REF, main_commit],
            cwd=str(repo), capture_output=True)
        if ancestor.returncode != 0:
            problems.append(f"GitHub {GITHUB_BRANCH} has commits that are not in Forgejo "
                            f"{FORGEJO_BRANCH}; a fast forward is impossible (never force)")
    changes = []
    if last and git(repo, "rev-parse", "--verify", "--quiet", f"refs/tags/{last}",
                    check=False):
        log = git(repo, "log", "--no-merges", "--format=%s", f"{last}..{main_commit}")
        changes = [line for line in log.splitlines() if line]
    return Plan(main_commit, main_version, target, last, github_master, changes, problems)


def print_plan(plan, out=sys.stdout):
    print(f"Forgejo {FORGEJO_BRANCH}: {plan.main_commit[:12]}  VERSION {plan.main_version}", file=out)
    print(f"Would publish:  {plan.tag} (GitHub {GITHUB_BRANCH} + tag, then the release workflow)", file=out)
    print(f"Last GitHub release tag: {plan.last_github_tag or 'none'}", file=out)
    print(f"Commits since then (without merges): {len(plan.changes)}", file=out)
    for line in plan.changes[:200]:
        print(f"  - {line}", file=out)
    if plan.problems:
        print("BLOCKED:", file=out)
        for problem in plan.problems:
            print(f"  ! {problem}", file=out)
    else:
        print("Nothing blocks the publication. It still needs Viktor's approval for "
              f"{plan.tag} (publish --version {plan.target} --freigabe \"...\").", file=out)


def publish(plan, version, freigabe, repo=REPO_ROOT, forgejo_remote=FORGEJO_REMOTE,
            github_url=GITHUB_URL, out=sys.stdout):
    if not freigabe or len(freigabe.strip()) < 5:
        raise ReleaseError("--freigabe must say who approved, when and where (Viktor only)")
    if version != plan.target:
        raise ReleaseError(f"approved version {version} differs from the planned {plan.target}")
    if plan.problems:
        raise ReleaseError("publication blocked: " + "; ".join(plan.problems))
    worktree = Path(tempfile.mkdtemp(prefix="flowshift-release-"))
    git(repo, "worktree", "add", "--quiet", "--detach", str(worktree), plan.main_commit)
    try:
        (worktree / "VERSION").write_text(f"{version}\n", encoding="utf-8")
        git(worktree, "commit", "--quiet", "-am", f"Release {plan.tag}",
            "-m", f"Approved by Viktor: {freigabe.strip()}")
        commit = git(worktree, "rev-parse", "HEAD")
        git(worktree, "tag", "-a", plan.tag, "-m", f"FlowShift {version}", commit)
        # Forgejo first: it keeps the release commit and tag even if GitHub fails.
        git(worktree, "push", "--quiet", forgejo_remote,
            f"{commit}:refs/heads/release/{plan.tag}", f"refs/tags/{plan.tag}")
        # Fast forward only (no --force); the tag push starts the release workflow.
        git(worktree, "push", "--quiet", github_url, f"{commit}:refs/heads/{GITHUB_BRANCH}")
        git(worktree, "push", "--quiet", github_url, f"refs/tags/{plan.tag}")
    finally:
        git(repo, "worktree", "remove", "--force", str(worktree), check=False)
    print(f"Published {plan.tag} ({commit[:12]}) to Forgejo and GitHub.", file=out)
    print("Next: wait for the GitHub release workflow, then run "
          f"'verify --version {version}'. In Forgejo merge release/{plan.tag} into "
          f"{FORGEJO_BRANCH}; the next development version is {next_dev_after(version)}.", file=out)
    return commit


def _fetch(url, max_bytes, accept="application/octet-stream"):
    request = Request(url, headers={"Accept": accept, "User-Agent": "FlowShift-release-check"})
    with urlopen(request, timeout=60) as response:  # noqa: S310 (fixed GitHub URLs)
        body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ReleaseError(f"{url} is larger than expected")
    return body


def verify(version, fetch=_fetch, download_setup=False, out=sys.stdout):
    """Check GitHub releases/latest like an installed FlowShift on Stable does."""
    payload = json.loads(fetch(update_model.LATEST_RELEASE_API, 1024 * 1024,
                               "application/vnd.github+json"))
    if payload.get("tag_name") != f"v{version}":
        raise ReleaseError(f"releases/latest is {payload.get('tag_name')}, not v{version}")
    release = update_model.validate_release_payload(payload, "0.0.0")
    manifest = json.loads(fetch(release.manifest_url, 64 * 1024))
    descriptor = update_model.validate_manifest_payload(manifest, release)
    sums = fetch(release.checksums_url, 64 * 1024).decode("utf-8")
    listed = {name.lstrip("*"): digest.lower() for digest, name in
              (line.split(None, 1) for line in sums.splitlines() if line.strip())}
    if listed.get(update_model.INSTALLER_ASSET) != descriptor.installer_sha256:
        raise ReleaseError("SHA256SUMS.txt and update-manifest.json disagree on FlowShift-Setup.exe")
    if download_setup:
        setup = fetch(release.installer_url, update_model.MAX_INSTALLER_SIZE)
        if (len(setup) != descriptor.installer_size
                or hashlib.sha256(setup).hexdigest() != descriptor.installer_sha256):
            raise ReleaseError("FlowShift-Setup.exe does not match the manifest")
    print(f"OK: GitHub releases/latest is {descriptor.tag}; Setup.exe {descriptor.installer_size} "
          f"bytes, sha256 {descriptor.installer_sha256[:16]}..., manifest and checksums agree"
          + ("; Setup.exe hash checked" if download_setup else ""), file=out)
    return descriptor


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan", help="read-only overview")
    publish_parser = sub.add_parser("publish", help="publish (only with Viktor's approval)")
    publish_parser.add_argument("--version", required=True)
    publish_parser.add_argument("--freigabe", required=True,
                                help="who approved, when and where, e.g. 'Viktor 25.11. Telegram'")
    verify_parser = sub.add_parser("verify", help="read-only check of releases/latest")
    verify_parser.add_argument("--version", required=True)
    verify_parser.add_argument("--download-setup", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            verify(args.version, download_setup=args.download_setup)
            return 0
        plan = make_plan()
        print_plan(plan)
        if args.command == "publish":
            publish(plan, args.version, args.freigabe)
        return 1 if plan.problems else 0
    except ReleaseError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
