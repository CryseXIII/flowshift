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
            forward only, and the tag), then does the backport (ruecktrag).
            Next step: assets.
  ruecktrag --version X.Y.Z brings the release commit into Forgejo main's
            history (GitHub master must stay a fast forward of main): branch
            task/release-vX.Y.Z-nach-main from origin/main plus 'git merge -s
            ours --no-edit origin/release/vX.Y.Z' (main's content unchanged,
            diff empty, VERSION stays the development version), pushed to
            Forgejo only (never force, no tags), and its PR opened through the
            Forgejo API with git's credential for origin (else the compare link
            is printed). Viktor merges that PR as a merge commit. publish and
            assets run it by themselves; repeated runs reuse branch and PR.
            Started by Claude at the console only; bot runs never merge.
  assets    --version X.Y.Z --freigabe "..." builds the release on this server
            (no GitHub Actions: the account is locked for billing and nothing
            may cost money): FlowShift-Setup.exe with Inno Setup under Wine, the
            WebGUI, update-manifest.json and SHA256SUMS.txt from a clean
            checkout of the tag (scripts/release_build.py). It checks them with
            the same validation as verify, uploads them into a draft release,
            downloads them back and compares the hashes, and only then
            publishes the release as latest, runs verify on it and makes sure
            the backport branch and PR exist. A red
            'npm test' stops the build; only the explicit option
            --rote-webgui-tests-nur-protokollieren (never a default) logs the
            number of red vitest tests and builds on; every other check stays
            strict.
  verify    --version X.Y.Z, read-only: checks GitHub releases/latest with the
            same validation the installed updater uses (assets, manifest,
            checksums, optionally the Setup.exe hash). plan and verify also say
            in one sentence whether the backport into main is still missing.
  build     --version X.Y.Z, the build and checks of assets from the Forgejo
            tag without any upload and without the token (same option).
            With --vom-main it builds Forgejo main (at X.Y.Z-dev.N) as X.Y.Z
            before any approval: no tag, VERSION only in the temporary
            worktree, every check strict (the proof asked before approval).
  probe     proves that the token may create releases and upload assets: a
            draft with a throw-away name and one small asset, deleted again
            (drafts create no tag and are never visible to users).

Steps that talk to the GitHub API (assets, probe) need the token and run only
under /srv/opencode/autopilot/bin/flowshift-github release ...; the token is
asked from git's credential helper of that run and stays in memory.

Never forces a push, never moves a tag, never deletes a tag or a published
release.
"""
from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import os
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "python"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import release_build  # noqa: E402  (server build without GitHub Actions)
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
    backport: str | None = None

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
            hint = ""
            if last and git(repo, "rev-parse", "--verify", "--quiet", f"refs/tags/{last}^{{commit}}",
                            check=False) == git(repo, "rev-parse", LOCAL_GITHUB_REF):
                hint = (f"; GitHub {GITHUB_BRANCH} is the release commit of {last}: bring it into "
                        f"{FORGEJO_BRANCH} keeping main's content with 'ruecktrag --version "
                        f"{last[1:]}' (git merge -s ours {forgejo_remote}/release/{last} on "
                        f"{backport_branch(last)}) and merge its PR")
            problems.append(f"GitHub {GITHUB_BRANCH} has commits that are not in Forgejo "
                            f"{FORGEJO_BRANCH}; a fast forward is impossible (never force){hint}")
    changes = []
    if last and git(repo, "rev-parse", "--verify", "--quiet", f"refs/tags/{last}",
                    check=False):
        log = git(repo, "log", "--no-merges", "--format=%s", f"{last}..{main_commit}")
        changes = [line for line in log.splitlines() if line]
    backport = backport_sentence(repo, forgejo_remote, last, main_commit) if last else None
    return Plan(main_commit, main_version, target, last, github_master, changes, problems,
                backport)


def print_plan(plan, out=sys.stdout):
    print(f"Forgejo {FORGEJO_BRANCH}: {plan.main_commit[:12]}  VERSION {plan.main_version}", file=out)
    print(f"Would publish:  {plan.tag} (GitHub {GITHUB_BRANCH} + tag, then assets builds "
          "the release here)", file=out)
    print(f"Last GitHub release tag: {plan.last_github_tag or 'none'}", file=out)
    if plan.backport:
        print(plan.backport, file=out)
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
            github_url=GITHUB_URL, out=sys.stdout, pulls=None, pulls_unavailable=None):
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
        # Fast forward only (no --force). No GitHub Actions run; assets builds the release.
        git(worktree, "push", "--quiet", github_url, f"{commit}:refs/heads/{GITHUB_BRANCH}")
        git(worktree, "push", "--quiet", github_url, f"refs/tags/{plan.tag}")
    finally:
        git(repo, "worktree", "remove", "--force", str(worktree), check=False)
    print(f"Published {plan.tag} ({commit[:12]}) to Forgejo and GitHub.", file=out)
    _backport_after(version, repo, forgejo_remote, pulls, pulls_unavailable, out)
    print(f"Next: 'assets --version {version} --freigabe \"...\"' builds, checks, uploads and "
          f"publishes the release, then verifies it; the next development version is "
          f"{next_dev_after(version)}.", file=out)
    return commit


def _backport_after(version, repo, forgejo_remote, pulls, pulls_unavailable, out):
    """Backport as part of publish and assets; the published release itself stays valid."""
    try:
        ruecktrag(version, repo, forgejo_remote, pulls, pulls_unavailable, out)
    except ReleaseError as exc:
        raise ReleaseError(f"v{version} is published, but the backport into {FORGEJO_BRANCH} "
                           f"failed: {exc}; run 'ruecktrag --version {version}' again") from exc


def _fetch(url, max_bytes, accept="application/octet-stream"):
    request = Request(url, headers={"Accept": accept, "User-Agent": "FlowShift-release-check"})
    with urlopen(request, timeout=60) as response:  # noqa: S310 (fixed GitHub URLs)
        body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ReleaseError(f"{url} is larger than expected")
    return body


def check_release(payload, fetch, version, download_setup=False):
    """The updater's validation of one release object and its assets (verify and assets)."""
    if payload.get("tag_name") != f"v{version}":
        raise ReleaseError(f"release is {payload.get('tag_name')}, not v{version}")
    try:
        release = update_model.validate_release_payload(payload, "0.0.0")
        manifest_raw = fetch(release.manifest_url, 64 * 1024)
        descriptor = update_model.validate_manifest_payload(json.loads(manifest_raw), release)
    except (update_model.UpdateValidationError, ValueError) as exc:
        raise ReleaseError(f"the updater would reject the release: {exc}") from exc
    sums = fetch(release.checksums_url, 64 * 1024).decode("utf-8")
    listed = {name.strip().lstrip("*"): digest.lower() for digest, name in
              (line.split(None, 1) for line in sums.splitlines() if line.strip())}
    if listed.get(update_model.INSTALLER_ASSET) != descriptor.installer_sha256:
        raise ReleaseError("SHA256SUMS.txt and update-manifest.json disagree on FlowShift-Setup.exe")
    manifest_sum = listed.get(update_model.MANIFEST_ASSET)
    if manifest_sum is not None and manifest_sum != hashlib.sha256(manifest_raw).hexdigest():
        raise ReleaseError("SHA256SUMS.txt does not match update-manifest.json")
    if download_setup:
        setup = fetch(release.installer_url, update_model.MAX_INSTALLER_SIZE)
        if (len(setup) != descriptor.installer_size
                or hashlib.sha256(setup).hexdigest() != descriptor.installer_sha256):
            raise ReleaseError("FlowShift-Setup.exe does not match the manifest")
    return descriptor


def verify(version, fetch=_fetch, download_setup=False, out=sys.stdout, repo=None,
           forgejo_remote=FORGEJO_REMOTE):
    """Check GitHub releases/latest like an installed FlowShift on Stable does.

    With ``repo`` it also says in one sentence whether the backport into Forgejo main is
    still missing (information only, the release check decides the result).
    """
    payload = json.loads(fetch(update_model.LATEST_RELEASE_API, 1024 * 1024,
                               "application/vnd.github+json"))
    if payload.get("tag_name") != f"v{version}":
        raise ReleaseError(f"releases/latest is {payload.get('tag_name')}, not v{version}")
    descriptor = check_release(payload, fetch, version, download_setup)
    print(f"OK: GitHub releases/latest is {descriptor.tag}; Setup.exe {descriptor.installer_size} "
          f"bytes, sha256 {descriptor.installer_sha256[:16]}..., manifest and checksums agree"
          + ("; Setup.exe hash checked" if download_setup else ""), file=out)
    if repo is not None:
        tag = f"v{version}"
        try:
            git(repo, "fetch", "--quiet", forgejo_remote,
                f"+refs/heads/{FORGEJO_BRANCH}:{LOCAL_FORGEJO_REF}",
                f"refs/tags/{tag}:refs/tags/{tag}")
            print(backport_sentence(repo, forgejo_remote, tag,
                                    git(repo, "rev-parse", LOCAL_FORGEJO_REF)), file=out)
        except ReleaseError as exc:
            print(f"Backport of {tag}: could not be checked ({exc}).", file=out)
    return descriptor


# -- GitHub API (assets, probe) -------------------------------------------------------------------
GITHUB_API = "https://api.github.com"
GITHUB_MARKER = "FLOWSHIFT_GITHUB_RELEASE"  # set by flowshift-github release for exactly this run


def github_token(repo=REPO_ROOT):
    """Ask git's credential helper of the flowshift-github run; the value stays in memory.

    Outside that run a global helper could answer github.com with another token, so the
    marker of flowshift-github is required.
    """
    if os.environ.get(GITHUB_MARKER) != "1":
        raise ReleaseError("the GitHub API needs the token: start this step through "
                           "/srv/opencode/autopilot/bin/flowshift-github release ...")
    query = f"protocol=https\nhost=github.com\npath={update_model.REPOSITORY}.git\n\n"
    completed = subprocess.run(["git", "credential", "fill"], input=query, capture_output=True,
                               text=True, cwd=str(repo), timeout=60)
    secret = None
    if completed.returncode == 0:
        for line in completed.stdout.splitlines():
            if line.startswith("password="):
                secret = line[len("password="):]
    if not secret:
        raise ReleaseError("git credential gave no GitHub token (see flowshift-github pruefen)")
    return secret


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def urllib_transport(method, url, headers, data=None, max_bytes=update_model.MAX_INSTALLER_SIZE):
    """One HTTP exchange without following redirects -> (status, headers, body)."""
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with build_opener(_NoRedirect).open(request, timeout=300) as response:  # noqa: S310
            return response.status, dict(response.headers), response.read(max_bytes + 1)
    except HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read(64 * 1024)


class GitHub:
    """Minimal GitHub REST client for releases of CryseXIII/flowshift."""

    def __init__(self, token, transport=urllib_transport, repository=update_model.REPOSITORY):
        self._token = token
        self.transport = transport
        self.base = f"{GITHUB_API}/repos/{repository}"

    def call(self, method, url, body=None, data=None, content_type=None,
             accept="application/vnd.github+json", ok=(200, 201, 204)):
        if not url.startswith("https://"):
            url = self.base + url
        headers = {"Authorization": f"Bearer {self._token}", "Accept": accept,
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "FlowShift-release"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            content_type = "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        status, response_headers, raw = self.transport(method, url, headers, data)
        if status not in ok:
            try:
                message = json.loads(raw or b"{}").get("message", "")
            except ValueError:
                message = ""
            raise ReleaseError(f"GitHub {method} {url.split('?')[0]}: HTTP {status} {message}".rstrip())
        if accept == "application/octet-stream" or not raw:
            return status, response_headers, raw
        return status, response_headers, json.loads(raw)

    def releases(self):
        return self.call("GET", "/releases?per_page=100")[2]

    def release(self, release_id):
        return self.call("GET", f"/releases/{int(release_id)}")[2]

    def create_draft(self, tag, name, body):
        return self.call("POST", "/releases", body={
            "tag_name": tag, "name": name, "body": body, "draft": True, "prerelease": False,
            "generate_release_notes": False})[2]

    def upload(self, release, name, data):
        upload_url = release["upload_url"].split("{")[0]
        if not upload_url.startswith("https://uploads.github.com/"):
            raise ReleaseError(f"unexpected upload URL {upload_url}")
        return self.call("POST", f"{upload_url}?name={quote(name)}", data=data,
                         content_type="application/octet-stream")[2]

    def delete_asset(self, asset_id):
        self.call("DELETE", f"/releases/assets/{int(asset_id)}")

    def download_asset(self, asset, max_bytes):
        """Download through the API (works for drafts); the redirect goes without token."""
        status, headers, raw = self.call("GET", asset["url"], accept="application/octet-stream",
                                         ok=(200, 302))
        if status == 302:
            location = headers.get("Location") or headers.get("location")
            if not location or not location.startswith("https://"):
                raise ReleaseError(f"asset {asset.get('name')}: redirect without HTTPS location")
            status, _, raw = self.transport("GET", location, {"User-Agent": "FlowShift-release"},
                                            None)
            if status != 200:
                raise ReleaseError(f"asset {asset.get('name')}: download HTTP {status}")
        if len(raw) > max_bytes:
            raise ReleaseError(f"asset {asset.get('name')} is larger than expected")
        return raw

    def publish(self, release_id):
        return self.call("PATCH", f"/releases/{int(release_id)}",
                         body={"draft": False, "make_latest": "true"})[2]

    def delete_release(self, release_id):
        self.call("DELETE", f"/releases/{int(release_id)}")

    def status(self, url):
        return self.call("GET", url, ok=(200, 404))[0]


def probe(api, out=sys.stdout, now=time.time):
    """Draft release with one small asset, deleted again; proves the API works for us."""
    name = f"flowshift-probe-{int(now())}"
    draft = api.create_draft(name, f"Probe {name} (wird sofort geloescht)",
                             "Automatische Pruefung der Release-Rechte, kein Release.")
    print(f"draft created: id {draft['id']} (draft={draft.get('draft')}, tag {name} not created)",
          file=out)
    try:
        asset = api.upload(draft, "probe.txt", b"flowshift probe\n")
        back = api.download_asset(asset, 1024)
        if back != b"flowshift probe\n":
            raise ReleaseError("uploaded probe asset reads back differently")
        print(f"asset uploaded and read back: {asset['name']} ({asset['size']} bytes)", file=out)
    finally:
        api.delete_release(draft["id"])
    gone = api.status(f"/releases/{int(draft['id'])}") == 404
    no_tag = api.status(f"/git/ref/tags/{quote(name)}") == 404
    print(f"draft deleted: {'yes' if gone else 'NO'}; probe tag absent: {'yes' if no_tag else 'NO'}",
          file=out)
    if not (gone and no_tag):
        raise ReleaseError("probe draft or tag still exists")
    print("OK: GitHub accepts releases and asset uploads with this token", file=out)
    return True


def remote_tag_commit(repo, remote, tag):
    listing = git(repo, "ls-remote", remote, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}")
    refs = dict(reversed(line.split("\t")) for line in listing.splitlines() if "\t" in line)
    return refs.get(f"refs/tags/{tag}^{{}}") or refs.get(f"refs/tags/{tag}")


# -- Backport (Ruecktrag) of the release commit into Forgejo main ----------------------------------
# GitHub master is the release commit; the next publish needs it as an ancestor of Forgejo main
# (fast forward only). A 'git merge -s ours' brings it into main's history and keeps main's
# content (empty diff, VERSION stays the development version). The script pushes a branch and
# opens its PR; Viktor merges it in Forgejo. Bot runs keep their 'git merge' ban.

def backport_branch(tag):
    return f"task/release-{tag}-nach-main"


def is_ancestor(repo, ancestor, descendant):
    return subprocess.run(["git", "merge-base", "--is-ancestor", ancestor, descendant],
                          cwd=str(repo), capture_output=True).returncode == 0


def forgejo_web(repo, forgejo_remote):
    """(scheme://host, owner/name) of an http(s) Forgejo remote, else None (local paths)."""
    url = urlsplit(git(repo, "remote", "get-url", forgejo_remote))
    if url.scheme not in ("http", "https") or not url.hostname:
        return None
    host = url.hostname + (f":{url.port}" if url.port else "")
    return f"{url.scheme}://{host}", url.path.strip("/").removesuffix(".git")


def backport_sentence(repo, forgejo_remote, tag, main_commit):
    """One sentence for plan and verify: is the release commit of ``tag`` in Forgejo main?"""
    commit = git(repo, "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}",
                 check=False)
    if not commit:
        return f"Backport of {tag}: unknown, the tag is not on Forgejo."
    if is_ancestor(repo, commit, main_commit):
        return f"Backport of {tag}: done, the release commit is in Forgejo {FORGEJO_BRANCH}."
    branch = backport_branch(tag)
    if git(repo, "ls-remote", forgejo_remote, f"refs/heads/{branch}"):
        return (f"Backport of {tag}: still missing; merge the PR of {branch} into "
                f"{FORGEJO_BRANCH} in Forgejo (merge commit, never squash or rebase).")
    return (f"Backport of {tag}: still missing; run 'ruecktrag --version {tag[1:]}' "
            f"(creates {branch} and its PR).")


class ForgejoPulls:
    """Open or find the backport PR through the Forgejo API with the bot's git credential."""

    def __init__(self, api_base, authorization, transport=None):
        self.api_base = api_base
        self._authorization = authorization
        self.transport = transport or urllib_transport

    def call(self, method, path, body=None):
        headers = {"Authorization": self._authorization, "Accept": "application/json",
                   "User-Agent": "FlowShift-release"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        status, _, raw = self.transport(method, self.api_base + path, headers, data)
        if status not in (200, 201):
            try:
                message = json.loads(raw or b"{}").get("message", "")
            except ValueError:
                message = ""
            raise ReleaseError(f"Forgejo {method} {path.split('?')[0]}: HTTP {status} "
                               f"{message}".rstrip())
        return json.loads(raw)

    def open_pull(self, head, base, title, body):
        """The open PR head -> base, created if there is none; returns its web URL."""
        for pull in self.call("GET", "/pulls?state=open&limit=50"):
            if ((pull.get("head") or {}).get("ref") == head
                    and (pull.get("base") or {}).get("ref") == base):
                return pull.get("html_url"), False
        pull = self.call("POST", "/pulls", body={"head": head, "base": base, "title": title,
                                                 "body": body})
        return pull.get("html_url"), True


def forgejo_pulls(repo=REPO_ROOT, forgejo_remote=FORGEJO_REMOTE):
    """(ForgejoPulls, None) with the credential git uses for the remote, else (None, reason)."""
    web = forgejo_web(repo, forgejo_remote)
    if not web:
        return None, f"{forgejo_remote} is no http(s) Forgejo remote"
    base, path = web
    scheme, host = base.split("://", 1)
    query = f"protocol={scheme}\nhost={host}\npath={path}.git\n\n"
    try:
        completed = subprocess.run(
            ["git", "credential", "fill"], input=query, capture_output=True, text=True,
            cwd=str(repo), timeout=60, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"git credential failed ({type(exc).__name__})"
    fields = dict(line.split("=", 1) for line in completed.stdout.splitlines() if "=" in line)
    if completed.returncode != 0 or not fields.get("password"):
        return None, "git credential gave no Forgejo access"
    pair = f"{fields.get('username', '')}:{fields['password']}".encode("utf-8")
    return ForgejoPulls(f"{base}/api/v1/repos/{path}",
                        "Basic " + base64.b64encode(pair).decode("ascii")), None


def ruecktrag(version, repo=REPO_ROOT, forgejo_remote=FORGEJO_REMOTE, pulls=None,
              pulls_unavailable=None, out=sys.stdout):
    """Branch task/release-vX.Y.Z-nach-main = origin/main + 'git merge -s ours' of the release.

    Pushes only that new branch to Forgejo (never force, no tags, nothing to GitHub) and
    opens its PR when ``pulls`` is given. Repeatable: an existing branch or PR is reused,
    a release commit already in main needs nothing. Returns the branch commit or None.
    """
    if not _STABLE.match(f"v{version}"):
        raise ReleaseError(f"{version} is not a stable X.Y.Z version")
    tag = f"v{version}"
    branch = backport_branch(tag)
    release_ref = f"{forgejo_remote}/release/{tag}"
    git(repo, "fetch", "--quiet", forgejo_remote,
        f"+refs/heads/{FORGEJO_BRANCH}:{LOCAL_FORGEJO_REF}",
        f"+refs/heads/release/{tag}:refs/remotes/{release_ref}",
        f"refs/tags/{tag}:refs/tags/{tag}")
    main_commit = git(repo, "rev-parse", LOCAL_FORGEJO_REF)
    release_commit = git(repo, "rev-parse", f"refs/tags/{tag}^{{commit}}")
    if not is_ancestor(repo, release_commit, f"refs/remotes/{release_ref}"):
        raise ReleaseError(f"release/{tag} on Forgejo does not contain the tag {tag}")
    if is_ancestor(repo, release_commit, main_commit):
        print(f"Backport of {tag}: done, the release commit is already in Forgejo "
              f"{FORGEJO_BRANCH}; nothing to merge.", file=out)
        return None

    remote_branch = git(repo, "ls-remote", forgejo_remote, f"refs/heads/{branch}")
    if remote_branch:
        commit = remote_branch.split()[0]
        git(repo, "fetch", "--quiet", forgejo_remote,
            f"+refs/heads/{branch}:refs/remotes/{forgejo_remote}/{branch}")
        base = git(repo, "rev-parse", f"{commit}^1")
        if (not is_ancestor(repo, release_commit, commit)
                or git(repo, "diff", "--name-only", base, commit)):
            raise ReleaseError(f"{branch} exists on Forgejo but is no content-neutral merge of "
                               f"{tag}; never forced, check it by hand")
        print(f"{branch} already exists on Forgejo ({commit[:12]}), reused.", file=out)
    else:
        worktree = Path(tempfile.mkdtemp(prefix="flowshift-ruecktrag-"))
        git(repo, "worktree", "add", "--quiet", "--detach", str(worktree), main_commit)
        try:
            git(worktree, "merge", "-s", "ours", "--no-edit", "-m",
                f"Release {tag} in {FORGEJO_BRANCH} zuruecktragen (merge -s ours, Inhalt von "
                f"{FORGEJO_BRANCH} unveraendert)", release_ref)
            commit = git(worktree, "rev-parse", "HEAD")
            if git(worktree, "diff", "--name-only", main_commit, commit):
                raise ReleaseError("the backport merge changed files; nothing pushed")
            if not is_ancestor(worktree, release_commit, commit):
                raise ReleaseError("the backport merge does not contain the release commit")
            # A new branch only: no --force, no tags, nothing to GitHub.
            git(worktree, "push", "--quiet", forgejo_remote, f"{commit}:refs/heads/{branch}")
        finally:
            git(repo, "worktree", "remove", "--force", str(worktree), check=False)
        print(f"Backport branch {branch} pushed to Forgejo ({commit[:12]}): "
              f"{FORGEJO_BRANCH} + git merge -s ours {release_ref}, diff empty, VERSION "
              f"unchanged.", file=out)

    title = f"FlowShift: Release {tag} in {FORGEJO_BRANCH} zuruecktragen (merge -s ours)"
    body = (f"Bringt den Release-Commit {release_commit[:12]} von {tag} in die Historie von "
            f"{FORGEJO_BRANCH}, damit die naechste Version per Fast Forward nach GitHub geht. "
            f"Diff leer, VERSION bleibt die Entwicklungsversion.\n\n"
            "Mergen mit 'Merge-Commit erstellen', nie Squash oder Rebase (sonst fehlt der "
            "Release-Commit wieder).\n")
    url = None
    if pulls is not None:
        try:
            url, created = pulls.open_pull(branch, FORGEJO_BRANCH, title, body)
            print(f"PR {'opened' if created else 'already open'}: {url}", file=out)
        except (ReleaseError, OSError, ValueError) as exc:
            pulls_unavailable = str(exc)
    if not url:
        web = forgejo_web(repo, forgejo_remote)
        where = f"{web[0]}/{web[1]}/compare/{FORGEJO_BRANCH}...{branch}" if web else branch
        print(f"PR not opened automatically ({pulls_unavailable or 'no Forgejo API'}); open "
              f"it here: {where}", file=out)
    print(f"To merge: the PR {branch} -> {FORGEJO_BRANCH} in Forgejo, as a merge commit "
          f"(never squash or rebase). Then 'plan' no longer blocks on {tag}.", file=out)
    return commit


def local_release_payload(version, files):
    """Release object and fetcher for the built files, as GitHub will serve them."""
    base = f"https://github.com/{update_model.REPOSITORY}/releases/download/v{version}"
    contents = {f"{base}/{name}": Path(path).read_bytes() for name, path in files.items()}
    payload = {
        "tag_name": f"v{version}", "draft": False, "prerelease": False, "body": "",
        "html_url": f"https://github.com/{update_model.REPOSITORY}/releases/tag/v{version}",
        "assets": [{"name": name, "size": len(contents[f"{base}/{name}"]),
                    "browser_download_url": f"{base}/{name}"} for name in files],
    }

    def fetch(url, max_bytes, accept=""):
        body = contents[url]
        if len(body) > max_bytes:
            raise ReleaseError(f"{url} is larger than expected")
        return body
    return payload, fetch


def release_notes(plan_changes, version):
    lines = [f"FlowShift {version}", "",
             "Installation and update: download FlowShift-Setup.exe or let an installed "
             "FlowShift (channel Stable) update itself.", ""]
    if plan_changes:
        lines.append("Changes:")
        lines += [f"- {change}" for change in plan_changes[:100]]
    return "\n".join(lines) + "\n"


def check_draft(api, draft_id, files, version):
    """Read the draft back: exactly our assets, each byte-identical, updater accepts it."""
    draft = api.release(draft_id)
    if not draft.get("draft"):
        raise ReleaseError("the release became public before verification")
    assets = draft.get("assets") or []
    names = sorted(asset.get("name") for asset in assets)
    if names != sorted(files):
        raise ReleaseError(f"draft assets are {names}, expected {sorted(files)}")
    contents = {}
    for asset in assets:
        local = Path(files[asset["name"]]).read_bytes()
        local_sha = hashlib.sha256(local).hexdigest()
        if asset.get("state") != "uploaded" or asset.get("size") != len(local):
            raise ReleaseError(f"draft asset {asset['name']} is incomplete")
        digest = asset.get("digest")
        if digest is not None and digest != f"sha256:{local_sha}":
            raise ReleaseError(f"GitHub digest of {asset['name']} differs from the built file")
        remote = api.download_asset(asset, update_model.MAX_INSTALLER_SIZE)
        if hashlib.sha256(remote).hexdigest() != local_sha:
            raise ReleaseError(f"downloaded {asset['name']} differs from the built file")
        contents[asset["browser_download_url"]] = remote

    def fetch(url, max_bytes, accept=""):
        return contents[url]
    return check_release({**draft, "draft": False}, fetch, version, download_setup=True)


def build_checked(version, commit, repo=REPO_ROOT, out=sys.stdout,
                  toolchain=release_build.ensure_toolchain, build=release_build.build_assets,
                  work=None, red_webgui_tests_log_only=False, set_version=False):
    """Build the assets from a clean worktree of ``commit`` and check them like the updater.

    ``red_webgui_tests_log_only`` only relaxes the vitest run (red tests are logged with
    their number); manifest, checksums and the updater validation stay strict.
    ``set_version`` writes ``version`` into VERSION of the worktree only, as the release
    commit of ``publish`` would (nothing is committed, tagged or pushed).
    """
    work = Path(work) if work else release_build.temporary_work_dir()
    log = release_build.BuildLog(work / "build.log", out)
    print(f"build log: {work / 'build.log'}", file=out)
    tools = toolchain(log=log)
    source = work / "source"
    git(repo, "worktree", "add", "--quiet", "--detach", str(source), commit)
    try:
        if set_version:
            (source / "VERSION").write_text(f"{version}\n", encoding="utf-8")
        built = build(source, version, work, tools, log=log,
                      red_webgui_tests_log_only=red_webgui_tests_log_only)
    finally:
        git(repo, "worktree", "remove", "--force", str(source), check=False)
    files = {name: Path(built[name]) for name in release_build.ASSET_NAMES}
    payload, fetch = local_release_payload(version, files)
    descriptor = check_release(payload, fetch, version, download_setup=True)
    print(f"built and checked: Setup.exe {descriptor.installer_size} bytes, "
          f"sha256 {descriptor.installer_sha256}; files in {work / 'release'}", file=out)
    return files


def build_only(version, repo=REPO_ROOT, forgejo_remote=FORGEJO_REMOTE, out=sys.stdout,
               toolchain=release_build.ensure_toolchain, build=release_build.build_assets,
               work=None, red_webgui_tests_log_only=False):
    """Build and check from the Forgejo tag; uploads nothing and needs no token."""
    tag = f"v{version}"
    commit = remote_tag_commit(repo, forgejo_remote, tag)
    if not commit:
        raise ReleaseError(f"tag {tag} does not exist on Forgejo")
    git(repo, "fetch", "--quiet", forgejo_remote, f"refs/tags/{tag}:refs/tags/{tag}")
    return build_checked(version, commit, repo, out, toolchain, build, work,
                         red_webgui_tests_log_only)


def build_from_main(version, repo=REPO_ROOT, forgejo_remote=FORGEJO_REMOTE, out=sys.stdout,
                    toolchain=release_build.ensure_toolchain, build=release_build.build_assets,
                    work=None):
    """Release build of Forgejo main as ``version`` before any approval (no tag, no upload).

    The proof that the next release builds green, with every check strict (no option for
    red WebGUI tests); main must be at ``version``-dev.N.
    """
    if not _STABLE.match(f"v{version}"):
        raise ReleaseError(f"{version} is not a stable X.Y.Z version")
    git(repo, "fetch", "--quiet", forgejo_remote,
        f"+refs/heads/{FORGEJO_BRANCH}:{LOCAL_FORGEJO_REF}")
    commit = git(repo, "rev-parse", LOCAL_FORGEJO_REF)
    main_version = git(repo, "show", f"{commit}:VERSION")
    if stable_target(main_version) != version:
        raise ReleaseError(f"{forgejo_remote}/{FORGEJO_BRANCH} is at {main_version}, "
                           f"not a development version of {version}")
    print(f"build of {forgejo_remote}/{FORGEJO_BRANCH} {commit[:12]} ({main_version}) "
          f"as {version}, nothing is tagged or uploaded", file=out)
    return build_checked(version, commit, repo, out, toolchain, build, work,
                         red_webgui_tests_log_only=False, set_version=True)


def assets(version, freigabe, api, repo=REPO_ROOT, forgejo_remote=FORGEJO_REMOTE,
           github_url=GITHUB_URL, out=sys.stdout, toolchain=release_build.ensure_toolchain,
           build=release_build.build_assets, verify_fetch=_fetch, sleep=time.sleep,
           work=None, red_webgui_tests_log_only=False, pulls=None, pulls_unavailable=None):
    """Build, check, upload into a draft, check again, publish, verify releases/latest.

    Last step: the backport into Forgejo main (reused if publish already pushed it).
    """
    if not freigabe or len(freigabe.strip()) < 5:
        raise ReleaseError("--freigabe must say who approved, when and where (Viktor only)")
    if not _STABLE.match(f"v{version}"):
        raise ReleaseError(f"{version} is not a stable X.Y.Z version")
    tag = f"v{version}"
    forgejo_commit = remote_tag_commit(repo, forgejo_remote, tag)
    github_commit = remote_tag_commit(repo, github_url, tag)
    if not forgejo_commit or forgejo_commit != github_commit:
        raise ReleaseError(f"tag {tag} must exist on Forgejo and GitHub with the same commit "
                           f"(Forgejo {forgejo_commit or 'missing'}, GitHub {github_commit or 'missing'}); "
                           "run publish first")
    git(repo, "fetch", "--quiet", forgejo_remote, f"refs/tags/{tag}:refs/tags/{tag}")
    if git(repo, "rev-parse", f"{tag}^{{commit}}") != forgejo_commit:
        raise ReleaseError(f"local tag {tag} differs from Forgejo; never moved, check by hand")
    if git(repo, "show", f"{forgejo_commit}:VERSION") != version:
        raise ReleaseError(f"VERSION at {tag} is not {version}")

    existing = [r for r in api.releases() if r.get("tag_name") == tag]
    if any(not r.get("draft") for r in existing):
        raise ReleaseError(f"release {tag} is already published; never replaced (run verify)")
    if len(existing) > 1:
        raise ReleaseError(f"{len(existing)} drafts exist for {tag}; delete the extra ones on GitHub")

    files = build_checked(version, forgejo_commit, repo, out, toolchain, build, work,
                          red_webgui_tests_log_only)

    last = stable_tags(git(repo, "ls-remote", "--tags", github_url))
    previous = [t for t in last if parse_semver(t[1:]) < parse_semver(version)]
    changes = []
    if previous and git(repo, "rev-parse", "--verify", "--quiet", f"refs/tags/{previous[-1]}",
                        check=False):
        changes = [line for line in git(repo, "log", "--no-merges", "--format=%s",
                                        f"{previous[-1]}..{forgejo_commit}").splitlines() if line]
    if existing:
        draft = existing[0]
        for asset in draft.get("assets") or []:
            api.delete_asset(asset["id"])
        print(f"reusing draft {draft['id']} (old assets removed)", file=out)
    else:
        draft = api.create_draft(tag, f"FlowShift {version}", release_notes(changes, version))
        print(f"draft {draft['id']} created for {tag}", file=out)
    for name in release_build.ASSET_NAMES:
        api.upload(draft, name, files[name].read_bytes())
    check_draft(api, draft["id"], files, version)
    print("draft checked: assets read back byte-identical, updater validation passes", file=out)
    published = api.publish(draft["id"])
    if published.get("draft") or published.get("tag_name") != tag:
        raise ReleaseError("GitHub did not publish the release as expected")
    print(f"published {tag}: {published.get('html_url')}", file=out)
    error = None
    for attempt in range(6):
        try:
            descriptor = verify(version, fetch=verify_fetch, download_setup=True, out=out)
            break
        except (ReleaseError, OSError, ValueError) as exc:
            error = exc
            sleep(10 * (attempt + 1))
    else:
        raise ReleaseError(f"published, but releases/latest does not verify yet: {error}")
    _backport_after(version, repo, forgejo_remote, pulls, pulls_unavailable, out)
    return descriptor


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan", help="read-only overview")
    publish_parser = sub.add_parser("publish", help="publish (only with Viktor's approval)")
    publish_parser.add_argument("--version", required=True)
    publish_parser.add_argument("--freigabe", required=True,
                                help="who approved, when and where, e.g. 'Viktor 25.11. Telegram'")
    assets_parser = sub.add_parser("assets", help="build on this server, check, upload, publish "
                                                  "(only with Viktor's approval)")
    assets_parser.add_argument("--version", required=True)
    assets_parser.add_argument("--freigabe", required=True)
    red_help = ("explicit, never a default: a red 'npm test' (vitest) is only logged with the "
                "number of red tests and the build goes on (Viktor 25.11. 16:50 for v0.7.0); "
                "all other checks stay strict")
    assets_parser.add_argument("--rote-webgui-tests-nur-protokollieren", action="store_true",
                               dest="red_webgui_tests_log_only", help=red_help)
    verify_parser = sub.add_parser("verify", help="read-only check of releases/latest")
    verify_parser.add_argument("--version", required=True)
    verify_parser.add_argument("--download-setup", action="store_true")
    ruecktrag_parser = sub.add_parser(
        "ruecktrag", help="backport: branch task/release-vX.Y.Z-nach-main (merge -s ours of "
                          "the release into Forgejo main) and its PR; publish and assets do it "
                          "by themselves")
    ruecktrag_parser.add_argument("--version", required=True)
    sub.add_parser("probe", help="draft release with one asset, deleted again")
    build_parser = sub.add_parser("build", help="build and check from the Forgejo tag, "
                                                "upload nothing")
    build_parser.add_argument("--version", required=True)
    build_source = build_parser.add_mutually_exclusive_group()
    build_source.add_argument("--rote-webgui-tests-nur-protokollieren", action="store_true",
                              dest="red_webgui_tests_log_only", help=red_help)
    build_source.add_argument("--vom-main", action="store_true", dest="from_main",
                              help="build Forgejo main as X.Y.Z before the approval (no tag "
                                   "needed, VERSION set only in the temporary worktree, "
                                   "every check strict)")
    args = parser.parse_args(argv)
    try:
        if args.command == "build" and args.from_main:
            build_from_main(args.version)
            return 0
        if args.command == "build":
            build_only(args.version, red_webgui_tests_log_only=args.red_webgui_tests_log_only)
            return 0
        if args.command == "verify":
            verify(args.version, download_setup=args.download_setup, repo=REPO_ROOT)
            return 0
        if args.command == "probe":
            probe(GitHub(github_token()))
            return 0
        if args.command == "ruecktrag":
            pulls, unavailable = forgejo_pulls()
            ruecktrag(args.version, pulls=pulls, pulls_unavailable=unavailable)
            return 0
        if args.command == "assets":
            api = GitHub(github_token())
            pulls, unavailable = forgejo_pulls()
            assets(args.version, args.freigabe, api,
                   red_webgui_tests_log_only=args.red_webgui_tests_log_only,
                   pulls=pulls, pulls_unavailable=unavailable)
            return 0
        plan = make_plan()
        print_plan(plan)
        if args.command == "publish":
            pulls, unavailable = forgejo_pulls()
            publish(plan, args.version, args.freigabe, pulls=pulls,
                    pulls_unavailable=unavailable)
        return 1 if plan.problems else 0
    except (ReleaseError, release_build.BuildError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
