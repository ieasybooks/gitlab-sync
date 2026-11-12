#!/usr/bin/env python3
"""Mirror a GitHub organization to GitLab.com Free, preserving visibility.

Only branches and tags are synchronized. GitHub is authoritative; GitLab project
metadata and host-specific refs are not treated as Git data. No third-party
Python packages are required.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class SyncError(Exception):
    """An actionable configuration, API, or Git synchronization failure."""

    def __init__(self, message: str, *, code: str = "SYNC_FAILED", stage: str | None = None):
        super().__init__(message)
        self.code = code
        self.stage = stage


class ApiError(SyncError):
    def __init__(self, service: str, status: int, detail: str):
        self.status = status
        prefix = {"GitHub": "GITHUB", "GitLab": "GITLAB"}.get(service)
        code = f"{prefix}_HTTP_{status}" if prefix and type(status) is int and 100 <= status <= 599 else "SYNC_FAILED"
        super().__init__(f"{service} API returned HTTP {status}: {detail}", code=code)


# Public output is built exclusively from these fixed messages and typed state.
# Never render exception text, API payloads, Git output, or Result.detail here:
# replacing known names cannot cover encoded names, refs, paths, or content.
FAILURE_MESSAGES = {
    "CONFIG": "Check the required repository variables and secrets in the README.",
    "SOURCE_METADATA": "GitHub returned invalid or inconsistent source metadata.",
    "DESTINATION_METADATA": "GitLab returned invalid or inconsistent destination metadata.",
    "DESTINATION_CONFLICT": "The destination path or ownership markers conflict; check the managed mirrors locally.",
    "VISIBILITY_FAILED": "GitLab did not confirm the requested visibility; check destination Owner permissions.",
    "PUBLIC_GROUP_REQUIRED": "Set the destination GitLab group visibility to Public to publish public sources.",
    "PRIVATE_HISTORY": "The mirror may retain private history; keep it private and follow the fresh-mirror procedure in the README.",
    "PROTECTED_REFS": "An inherited protection rule prevents mirroring; use a dedicated mirror group.",
    "SOURCE_CHANGED": "Source identity or refs changed during this run; check the source and retry.",
    "SOURCE_EMPTY": "The source would remove the destination's remaining branches; review locally before retrying.",
    "GIT_FAILED": "A Git or Git LFS operation failed; check credentials, repository access, and storage limits locally.",
    "GIT_TIMEOUT": "A Git operation exceeded its timeout; retry on the next run.",
    "VERIFY_FAILED": "Destination branches and tags did not match the fetched snapshot; retry the sync.",
    "API_CONNECTION": "An API request could not complete; check service availability and retry.",
    "API_RESPONSE": "An API returned an unexpected response; retry or review locally.",
    "SYNC_FAILED": "Synchronization failed; use the local named dry run to identify the affected repository.",
    "UNEXPECTED_ERROR": "An unexpected error interrupted synchronization; review locally without publishing raw diagnostics.",
    "PRIVACY_ROLLBACK_FAILED": "Could not confirm private destination visibility; check the GitLab group immediately and retry.",
    "UNSAFE_LOGGING": "Disable Actions debug logging and run named diagnostics only outside GitHub Actions.",
    "INTERRUPTED": "Synchronization was interrupted; check destination state before retrying.",
}
STATUS_MESSAGES = {
    "would_create": "New mirror planned.",
    "would_sync": "Existing managed mirror selected.",
    "would_retain_private": "Unavailable or excluded source; private retention planned.",
    "retained_private": "Unavailable or excluded source; mirror retained privately.",
    "empty": "Source and destination have no branches or tags.",
    "unchanged": "Branches, tags, and default branch already match.",
    "synced": "Branches and tags synchronized and verified.",
    "failed": "Synchronization failed.",
}
HTTP_FAILURE = re.compile(r"^(GITHUB|GITLAB)_HTTP_([1-5][0-9]{2})$")
STAGES = frozenset({
    "git_setup", "source_refs", "destination_refs", "source_fetch", "source_inspect",
    "lfs_check", "lfs_scan", "lfs_fetch", "lfs_push", "refs_push", "refs_prune", "refs_verify",
    "source_refresh", "project_create", "project_prepare", "visibility_update",
    "protection_update", "default_branch_update", "git_transfer",
})


def safe_stage(stage: object) -> str:
    return stage if isinstance(stage, str) and stage in STAGES else "unknown"


def failure_stage(error: BaseException) -> str:
    return safe_stage(error.stage) if isinstance(error, SyncError) else "unknown"


@contextmanager
def operation(stage: str):
    """Attach a fixed operation label without exposing error text or inputs."""
    try:
        yield
    except SyncError as exc:
        if failure_stage(exc) == "unknown":
            exc.stage = safe_stage(stage)
        raise
    except Exception:
        raise SyncError("An unexpected error interrupted the operation.",
                        code="UNEXPECTED_ERROR", stage=safe_stage(stage)) from None


def safe_failure_code(code: object, fallback: str = "SYNC_FAILED") -> str:
    if isinstance(code, str) and (code in FAILURE_MESSAGES or HTTP_FAILURE.fullmatch(code)):
        return code
    return fallback


def failure_code(error: BaseException) -> str:
    if isinstance(error, KeyboardInterrupt):
        return "INTERRUPTED"
    if isinstance(error, SyncError):
        return safe_failure_code(error.code)
    return "UNEXPECTED_ERROR"


def failure_message(code: str) -> str:
    code = safe_failure_code(code)
    if code in FAILURE_MESSAGES:
        return FAILURE_MESSAGES[code]
    service, status = HTTP_FAILURE.fullmatch(code).groups()
    service = {"GITHUB": "GitHub", "GITLAB": "GitLab"}[service]
    return f"{service} API request failed (HTTP {status}); check token permissions, configuration, and service limits."


def validate_logging(show_repository_names: bool = False):
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    debugging = os.environ.get("RUNNER_DEBUG") == "1" or any(
        os.environ.get(name, "").strip().lower() in ("true", "1")
        for name in ("ACTIONS_STEP_DEBUG", "ACTIONS_RUNNER_DEBUG")
    )
    if show_repository_names or debugging:
        raise SyncError("Unsafe logging requested in GitHub Actions.", code="UNSAFE_LOGGING")


def redact(message: str, *secrets: str) -> str:
    for secret in sorted(filter(None, secrets), key=len, reverse=True):
        message = message.replace(secret, "[REDACTED]")
        message = message.replace(quote(secret, safe=""), "[REDACTED]")
    return message


@dataclass(frozen=True)
class Settings:
    source_org: str
    group_id: int
    gitlab_token: str = field(repr=False)
    github_token: str = field(default="", repr=False)
    exclude_repos: frozenset[str] = frozenset()
    dry_run: bool = False

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, dry_run: bool = False):
        env = os.environ if environ is None else environ
        org = env.get("SOURCE_ORG", "ieasybooks").strip() or "ieasybooks"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", org):
            raise SyncError("SOURCE_ORG must be a GitHub organization login.", code="CONFIG")
        group = env.get("GITLAB_GROUP_ID", "").strip()
        if not group.isdecimal() or int(group) <= 0:
            raise SyncError("Set GITLAB_GROUP_ID to the destination group's numeric ID.", code="CONFIG")
        token = env.get("GITLAB_TOKEN", "").strip()
        if not token:
            raise SyncError("Set the GITLAB_TOKEN secret to a GitLab personal access token.", code="CONFIG")
        exclusions = frozenset(
            name.strip().casefold()
            for name in env.get("EXCLUDE_REPOS", "").split(",")
            if name.strip()
        )
        return cls(org, int(group), token, env.get("GH_TOKEN", "").strip(), exclusions, dry_run)


@dataclass(frozen=True)
class SourceRepository:
    id: int
    owner_id: int
    name: str
    full_name: str
    default_branch: str | None
    private: bool

    @property
    def key(self) -> tuple[int, int]:
        return self.owner_id, self.id

    @property
    def clone_url(self) -> str:
        return f"https://github.com/{self.full_name}.git"


MARKER = re.compile(r"^\[github-sync:v1 owner_id=(\d+) repository_id=(\d+)\]$", re.MULTILINE)
VISIBILITY_MARKER = re.compile(
    r"^\[github-sync:visibility-v1 (public-only|private-history)\]$", re.MULTILINE
)
GITLAB_RESERVED_PROJECT_PATHS = frozenset({
    "-", "badges", "blame", "blob", "builds", "commits", "create", "create_dir", "edit",
    "environments/folders", "files", "find_file", "gitlab-lfs/objects", "info/lfs/objects",
    "new", "preview", "raw", "refs", "tree", "update", "wikis",
})


def destination_name(source: SourceRepository) -> str:
    """Keep compatible names; reserve an ID-based namespace for other sources.

    GitLab rejects names such as .github for both project name and URL path.
    Reserving github-repo-<digits> prevents a literal source name from colliding
    with a fallback for a different source. Existing mirrors remain ID-tracked.
    """
    name = source.name
    key = name.casefold()
    if (len(name) <= 255 and re.fullmatch(r"[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*", name)
            and key not in GITLAB_RESERVED_PROJECT_PATHS
            and not key.endswith((".git", ".atom"))
            and not re.fullmatch(r"github-repo-[0-9]+", key)):
        return name
    if type(source.id) is not int or source.id <= 0:
        raise SyncError("Invalid source repository ID for destination naming.", code="SOURCE_METADATA")
    return f"github-repo-{source.id}"


def source_marker(source: SourceRepository) -> str:
    return f"[github-sync:v1 owner_id={source.owner_id} repository_id={source.id}]"


def project_description(source: SourceRepository, private_history: bool | None = None) -> str:
    history = "private-history" if source.private or private_history else "public-only"
    return (
        f"Mirror of https://github.com/{source.full_name}\n\n"
        "Managed by ieasybooks/gitlab-sync. Make changes on GitHub.\n"
        f"{source_marker(source)}\n[github-sync:visibility-v1 {history}]"
    )


def project_source_key(project: dict) -> tuple[int, int] | None:
    matches = MARKER.findall(project.get("description") or "")
    if len(matches) > 1:
        raise SyncError(f"GitLab project {project['id']} contains multiple ownership markers.", code="DESTINATION_CONFLICT")
    return tuple(map(int, matches[0])) if matches else None


def visibility_history(project: dict) -> str | None:
    matches = VISIBILITY_MARKER.findall(project.get("description") or "")
    if len(matches) > 1:
        raise SyncError("The mirror contains conflicting visibility-history markers.", code="DESTINATION_CONFLICT")
    return matches[0] if matches else None


class NoRedirect(HTTPRedirectHandler):
    """Do not forward API credentials to a redirected URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ApiClient:
    def __init__(self, base_url: str, service: str, headers: dict, secret: str = ""):
        self.base_url = base_url.rstrip("/")
        self.service = service
        self.headers = {"Accept": "application/json", "User-Agent": "ieasybooks-gitlab-sync", **headers}
        self.secret = secret
        self.opener = build_opener(NoRedirect())

    def request(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None):
        url = self.base_url + path
        if params:
            url += "?" + urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        headers = dict(self.headers)
        if data is not None:
            headers["Content-Type"] = "application/json"
        attempts = 4 if method == "GET" else 1
        for attempt in range(attempts):
            try:
                request = Request(url, data=data, headers=headers, method=method)
                with self.opener.open(request, timeout=45) as response:
                    payload = response.read()
                return json.loads(payload) if payload else None
            except HTTPError as exc:
                retryable = exc.code in (429, 500, 502, 503, 504) or (
                    exc.code == 403 and exc.headers.get("X-RateLimit-Remaining") == "0"
                )
                if retryable and attempt + 1 < attempts:
                    delay = exc.headers.get("Retry-After", "")
                    time.sleep(min(float(delay), 60) if delay.isdecimal() else 2 ** attempt)
                    exc.close()
                    continue
                detail = exc.read(2000).decode("utf-8", errors="replace")
                exc.close()
                raise ApiError(self.service, exc.code, redact(detail, self.secret)) from None
            except (URLError, TimeoutError, OSError) as exc:
                if attempt + 1 < attempts:
                    time.sleep(2 ** attempt)
                    continue
                raise SyncError(f"{self.service} API connection failed: {redact(str(exc), self.secret)}", code="API_CONNECTION") from None
            except (ValueError, UnicodeError):
                raise SyncError(f"{self.service} API returned invalid JSON.", code="API_RESPONSE") from None

    def pages(self, path: str, **params) -> list[dict]:
        # Numeric pagination never follows an untrusted Link header with tokens.
        items = []
        for page in range(1, 10001):
            batch = self.request("GET", path, params={**params, "per_page": 100, "page": page})
            if not isinstance(batch, list):
                raise SyncError(f"{self.service} API returned a non-list page.", code="API_RESPONSE")
            items.extend(batch)
            if len(batch) < 100:
                return items
        raise SyncError(f"{self.service} API pagination exceeded its safety limit.", code="API_RESPONSE")


class GitHubAPI(ApiClient):
    def __init__(self, token: str):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        super().__init__("https://api.github.com", "GitHub", headers, token)

    @staticmethod
    def _repository(record: dict, org: str) -> SourceRepository:
        owner = record["owner"]
        if owner["login"].casefold() != org.casefold():
            raise SyncError("GitHub returned a repository outside SOURCE_ORG.", code="SOURCE_METADATA")
        name = record["name"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise SyncError("GitHub returned an unsupported repository name.", code="SOURCE_METADATA")
        if type(record.get("private")) is not bool:
            raise SyncError("GitHub did not return a valid repository privacy flag.", code="SOURCE_METADATA")
        visibility = record.get("visibility")
        if visibility is not None and visibility not in ("public", "private", "internal"):
            raise SyncError("GitHub returned an unknown repository visibility.", code="SOURCE_METADATA")
        # Internal GitHub repositories must never become public on GitLab.com.
        private = record["private"] or visibility in ("private", "internal")
        return SourceRepository(
            int(record["id"]), int(owner["id"]), name,
            f"{owner['login']}/{name}", record.get("default_branch"), private,
        )

    def organization_id(self, org: str) -> int:
        return int(self.request("GET", f"/orgs/{quote(org, safe='')}")["id"])

    def repositories(self, org: str) -> list[SourceRepository]:
        records = self.pages(f"/orgs/{quote(org, safe='')}/repos", type="all", sort="full_name", direction="asc")
        repos = {}
        for record in records:
            repo = self._repository(record, org)
            repos[repo.id] = repo
        return sorted(repos.values(), key=lambda repo: repo.full_name.casefold())

    def repository(self, full_name: str) -> SourceRepository:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", full_name):
            raise SyncError("Invalid GitHub repository name.", code="SOURCE_METADATA")
        return self._repository(self.request("GET", f"/repos/{full_name}"), full_name.split("/", 1)[0])


class GitLabAPI(ApiClient):
    def __init__(self, token: str):
        super().__init__("https://gitlab.com/api/v4", "GitLab", {"PRIVATE-TOKEN": token}, token)

    def group(self, group_id: int) -> dict:
        return self.request("GET", f"/groups/{group_id}")

    def projects(self, group_id: int) -> list[dict]:
        return self.pages(f"/groups/{group_id}/projects", with_shared="false", include_subgroups="false")

    def create_project(self, source: SourceRepository, group_id: int) -> dict:
        name = destination_name(source)
        return self.request("POST", "/projects", body={
            "namespace_id": group_id,
            "name": name,
            "path": name,
            "description": project_description(source),
            "visibility": "private",
            "initialize_with_readme": False,
            "builds_access_level": "disabled",
            "auto_devops_enabled": False,
        })

    def prepare_project(self, project: dict, source: SourceRepository) -> dict:
        # A mirror that has ingested private (or unknown legacy) history must
        # not be published merely because its currently advertised refs match.
        # GitLab can retain unreachable Git objects and cached commit content.
        private_history = source.private or visibility_history(project) != "public-only"
        desired = {
            "visibility": "private" if private_history else project.get("visibility", "private"),
            "builds_access_level": "disabled",
            "auto_devops_enabled": False,
            "description": project_description(source, private_history),
        }
        if all(project.get(key) == value for key, value in desired.items()):
            return project
        project["visibility"] = "unknown"
        updated = self.request("PUT", f"/projects/{project['id']}", body=desired)
        if not isinstance(updated, dict) or updated.get("id") != project["id"] or updated.get("visibility") != desired["visibility"]:
            raise SyncError("GitLab did not confirm the requested mirror visibility.", code="VISIBILITY_FAILED")
        project.update(updated)
        return project

    def set_visibility(self, project: dict, visibility: str) -> dict:
        if visibility not in ("private", "public"):
            raise SyncError("Unsupported mirror visibility.", code="VISIBILITY_FAILED")
        if project.get("visibility") != visibility:
            # A timeout may occur after GitLab applied a write. Mark the local
            # state unknown so failure handling sends a real private update.
            project["visibility"] = "unknown"
            updated = self.request("PUT", f"/projects/{project['id']}", body={"visibility": visibility})
            if not isinstance(updated, dict) or updated.get("id") != project["id"] or updated.get("visibility") != visibility:
                raise SyncError("GitLab did not confirm the requested mirror visibility.", code="VISIBILITY_FAILED")
            project.update(updated)
        return project

    def unprotect(self, project_id: int):
        # Operate only on projects whose provenance was checked by SyncRunner.
        for kind in ("protected_branches", "protected_tags"):
            rules = self.pages(f"/projects/{project_id}/{kind}")
            if any(rule.get("inherited") for rule in rules):
                raise SyncError("An inherited GitLab protection rule prevents exact mirroring. Use a dedicated mirror group.", code="PROTECTED_REFS")
            for rule in rules:
                self.request("DELETE", f"/projects/{project_id}/{kind}/{quote(rule['name'], safe='')}")

    def set_default_branch(self, project_id: int, branch: str):
        self.request("PUT", f"/projects/{project_id}", body={"default_branch": branch})


ASKPASS = r'''import os, re, sys
from urllib.parse import urlsplit
prompt = sys.argv[1] if len(sys.argv) > 1 else ""
match = re.search(r"https://[^'\s]+", prompt)
host = urlsplit(match.group(0)).hostname if match else None
credentials = {
    "github.com": ("x-access-token", os.environ.get("SYNC_GITHUB_TOKEN", "")),
    "gitlab.com": ("oauth2", os.environ.get("SYNC_GITLAB_TOKEN", "")),
}
if host not in credentials:
    sys.exit(1)
username, password = credentials[host]
if prompt.lower().startswith("username"):
    print(username)
elif prompt.lower().startswith("password") and password:
    print(password)
else:
    sys.exit(1)
'''


class GitMirror:
    """An isolated bare Git copy; never checks out or executes source code."""

    def __init__(self, source_url: str, destination_url: str, github_token: str = "", gitlab_token: str = ""):
        self.source_url = source_url
        self.destination_url = destination_url
        self.github_token = github_token
        self.gitlab_token = gitlab_token
        self.refs: dict[str, str] = {}
        self._temporary = None
        self._fetched = False

    def __enter__(self):
        self._temporary = tempfile.TemporaryDirectory(prefix="gitlab-sync-")
        directory = Path(self._temporary.name)
        self.path = directory / "repository.git"
        askpass = directory / "askpass.py"
        askpass.write_text(f"#!{sys.executable}\n{ASKPASS}")
        askpass.chmod(0o700)
        # Git and LFS tracing can write directly to a file (including the job
        # summary), bypassing captured stdout/stderr. Do not inherit those
        # switches or environment-injected Git configuration.
        inherited = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("GIT_TRACE", "GIT_CONFIG_")) and key not in {
                "GIT_CONFIG", "GIT_CURL_VERBOSE", "GIT_TRANSFER_TRACE",
                "GIT_LFS_TRACE", "GIT_LFS_DEBUG", "GIT_LFS_PROGRESS",
            }
        }
        self.env = {**inherited,
            "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": str(askpass),
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_LFS_SKIP_SMUDGE": "1", "GIT_LFS_SKIP_PUSH": "0",
            "SYNC_GITHUB_TOKEN": self.github_token, "SYNC_GITLAB_TOKEN": self.gitlab_token,
            "LC_ALL": "C",
        }
        try:
            self._git("init", "--bare", "--quiet", str(self.path), cwd=directory)
            self._git("remote", "add", "github", self.source_url)
            self._git("remote", "add", "gitlab", self.destination_url)
            # Comparing advertised refs first avoids downloading unchanged repos
            # and their LFS objects on every hourly run.
            self.refs = self._remote_refs("github")
            return self
        except BaseException:
            self._temporary.cleanup()
            raise

    def __exit__(self, exc_type, exc_value, traceback):
        if self._temporary:
            self._temporary.cleanup()

    def _git(self, *args: str, cwd: Path | None = None, stage: str = "git_setup") -> str:
        command = ["git", "-c", "credential.helper=", "-c", f"core.hooksPath={os.devnull}",
                   "-c", "http.followRedirects=false", "-c", "protocol.ext.allow=never", *args]
        try:
            process = subprocess.run(command, cwd=cwd or self.path, env=self.env,
                                     text=True, encoding="utf-8", errors="replace",
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=1800)
        except subprocess.TimeoutExpired:
            raise SyncError("A Git operation exceeded the 30-minute timeout; it can be retried on the next run.",
                            code="GIT_TIMEOUT", stage=stage) from None
        except OSError as exc:
            raise SyncError(f"Could not run Git: {exc}", code="GIT_FAILED", stage=stage) from None
        if process.returncode:
            detail = redact(process.stderr or process.stdout, self.github_token, self.gitlab_token)
            raise SyncError(f"Git operation failed: {detail.strip()[-2500:]}", code="GIT_FAILED", stage=stage)
        return process.stdout

    def _remote_refs(self, remote: str, stage: str | None = None) -> dict[str, str]:
        stage = stage or ("source_refs" if remote == "github" else "destination_refs")
        output = self._git("ls-remote", "--refs", remote, "refs/heads/*", "refs/tags/*", stage=stage)
        return {ref: sha for sha, ref in (line.split("\t", 1) for line in output.splitlines())}

    def destination_refs(self) -> dict[str, str]:
        return self._remote_refs("gitlab")

    def fetch(self):
        if self._fetched:
            return
        self._git("fetch", "--quiet", "--force", "--prune", "--no-tags", "github",
                  "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*", stage="source_fetch")
        output = self._git("for-each-ref", "--format=%(objectname) %(refname)", "refs/heads/", "refs/tags/",
                           stage="source_inspect")
        self.refs = {ref: sha for sha, ref in (line.split(" ", 1) for line in output.splitlines())}
        self._fetched = True

    def transfer_lfs(self):
        self.fetch()
        self._git("lfs", "version", stage="lfs_check")
        # No pointer files means no LFS network request. Scan history, not HEAD.
        if not self._git("lfs", "ls-files", "--all", stage="lfs_scan").strip():
            return
        for remote, url, operation in (("github", self.source_url, "fetch"), ("gitlab", self.destination_url, "push")):
            endpoint = url.rstrip("/") + "/info/lfs"
            self._git("-c", f"lfs.url={endpoint}", "-c", f"lfs.pushurl={endpoint}",
                      "-c", f"remote.{remote}.lfsurl={endpoint}", "-c", f"remote.{remote}.lfspushurl={endpoint}",
                      "-c", "lfs.allowincompletepush=false", "-c", "lfs.skipdownloaderrors=false",
                      "lfs", operation, "--all", remote, stage="lfs_fetch" if operation == "fetch" else "lfs_push")

    def push_refs(self, prune: bool = False):
        self.fetch()
        args = ["push", "--atomic", "--force"]
        if prune:
            args.append("--prune")
        self._git(*args, "gitlab", "refs/heads/*:refs/heads/*", "refs/tags/*:refs/tags/*",
                  stage="refs_prune" if prune else "refs_push")

    def verify(self):
        if self._remote_refs("gitlab", stage="refs_verify") != self.refs:
            raise SyncError("Destination branches/tags differ from the fetched source snapshot after pushing.",
                            code="VERIFY_FAILED", stage="refs_verify")


@dataclass(frozen=True)
class Result:
    repo: str
    status: str
    detail: str
    visibility: str | None = None
    failure_code: str | None = None
    stage: str | None = None


def result_fields(result: Result) -> tuple[str, str, str]:
    status = result.status if isinstance(result.status, str) and result.status in STATUS_MESSAGES else "failed"
    visibility = result.visibility if isinstance(result.visibility, str) and result.visibility in ("public", "private") else "unknown"
    if status == "failed":
        code = safe_failure_code(result.failure_code)
        stage = safe_stage(result.stage)
        context = f" at {stage}" if stage != "unknown" else ""
        message = f"{code}{context}: {failure_message(code)}"
    else:
        message = STATUS_MESSAGES[status]
    return status, visibility, message


def emit_result(result: Result, index: int, show_repository_names: bool = False):
    validate_logging(show_repository_names)
    # Named diagnostics are an explicit local option; JSON escaping prevents
    # source names from injecting terminal controls or extra log lines.
    label = json.dumps(result.repo, ensure_ascii=True) if show_repository_names else f"repo-{index:03d}"
    status, visibility, message = result_fields(result)
    print(f"{label}: {status}; visibility: {visibility} — {message}", flush=True)


class SyncRunner:
    def __init__(self, settings: Settings, github: GitHubAPI, gitlab: GitLabAPI,
                 mirror_factory=GitMirror, show_repository_names: bool = False):
        self.settings = settings
        self.github = github
        self.gitlab = gitlab
        self.mirror_factory = mirror_factory
        self.show_repository_names = show_repository_names

    def _validate_project(self, project: dict, source: SourceRepository):
        if project.get("namespace", {}).get("id") != self.settings.group_id:
            raise SyncError("The GitLab project is outside the configured destination group.", code="DESTINATION_METADATA")
        if project_source_key(project) != source.key:
            raise SyncError("The GitLab project ownership marker does not match this GitHub repository.", code="DESTINATION_CONFLICT")
        if project.get("archived"):
            raise SyncError("The managed GitLab project is archived; unarchive it to resume syncing.", code="DESTINATION_METADATA")
        url = project.get("http_url_to_repo", "")
        parsed = urlsplit(url)
        expected = f"/{project.get('path_with_namespace', '')}.git"
        if (parsed.scheme != "https" or parsed.netloc != "gitlab.com" or parsed.path != expected
                or parsed.query or parsed.fragment):
            raise SyncError("The GitLab project returned an unexpected repository URL.", code="DESTINATION_METADATA")

    def run(self) -> list[Result]:
        validate_logging(self.show_repository_names)
        # Complete discovery before modifying anything. A lost page cannot look
        # like a source repository deletion; whole projects are never deleted.
        sources = self.github.repositories(self.settings.source_org)
        owner_id = self.github.organization_id(self.settings.source_org)
        if any(source.owner_id != owner_id for source in sources):
            raise SyncError("GitHub repository ownership does not match SOURCE_ORG.", code="SOURCE_METADATA")
        sources = [source for source in sources if source.name.casefold() not in self.settings.exclude_repos
                   and source.full_name.casefold() not in self.settings.exclude_repos]
        group = self.gitlab.group(self.settings.group_id)
        if group.get("id") != self.settings.group_id:
            raise SyncError("The GitLab group response does not match GITLAB_GROUP_ID.", code="DESTINATION_METADATA")
        if group.get("visibility") not in ("public", "private", "internal"):
            raise SyncError("GitLab did not return a valid destination group visibility.", code="DESTINATION_METADATA")
        projects = self.gitlab.projects(self.settings.group_id)
        by_key: dict[tuple[int, int], dict] = {}
        by_path = {}
        for project in projects:
            if project.get("namespace", {}).get("id") != self.settings.group_id:
                raise SyncError("GitLab returned a shared project or a project outside the destination group.", code="DESTINATION_METADATA")
            key = project_source_key(project)
            if key is not None:
                if key in by_key:
                    raise SyncError("Multiple GitLab projects claim the same GitHub source. Resolve duplicate ownership markers first.", code="DESTINATION_CONFLICT")
                by_key[key] = project
            by_path[project["path"].casefold()] = project
        results = []
        blocked = {}
        selected = {source.key: source for source in sources}
        # Losing access to a newly-private source must not leave its old public
        # mirror exposed. Retain absent/excluded managed projects privately.
        # This also covers sources the read token can no longer discover.
        for key, project in by_key.items():
            source = selected.get(key)
            if key[0] != owner_id or (source is not None and not source.private):
                continue
            if project.get("visibility") == "private":
                continue
            if self.settings.dry_run:
                if source is None:
                    results.append(Result(f"GitLab project {project['id']}", "would_retain_private",
                                          "Source unavailable or excluded; retain the project privately", visibility="private"))
                continue
            try:
                with operation("visibility_update"):
                    self.gitlab.set_visibility(project, "private")
                if source is None:
                    results.append(Result(f"GitLab project {project['id']}", "retained_private",
                                          "Source unavailable or excluded; project retained privately", visibility="private"))
            except Exception as exc:
                blocked[key] = exc
                if source is None:
                    results.append(Result(f"GitLab project {project['id']}", "failed",
                                          "Could not confirm private visibility for an unavailable/excluded source.",
                                          failure_code="PRIVACY_ROLLBACK_FAILED", stage="visibility_update"))
        mode = "Dry run" if self.settings.dry_run else "Sync"
        print(f"{mode}: {len(sources)} accessible source repositories selected.", flush=True)
        for index, result in enumerate(results, 1):
            emit_result(result, index, self.show_repository_names)
        for source in sources:
            project = by_key.get(source.key)
            owned = project is not None  # Namespace and source key checked above.
            try:
                if source.key in blocked:
                    raise SyncError("Cannot confirm this mirror is private.", code="VISIBILITY_FAILED")
                if project is None and destination_name(source).casefold() in by_path:
                    raise SyncError("A GitLab project already occupies this path without the matching source ownership marker; refusing to overwrite it.", code="DESTINATION_CONFLICT")
                if project is not None:
                    self._validate_project(project, source)
                source = self._refresh_source(source)
                if source.private:
                    if owned and not self.settings.dry_run:
                        with operation("visibility_update"):
                            self.gitlab.set_visibility(project, "private")
                elif group["visibility"] != "public":
                    raise SyncError("Set the destination GitLab group visibility to Public to publish public source repositories.", code="PUBLIC_GROUP_REQUIRED")
                if project is not None and not source.private and visibility_history(project) != "public-only":
                    raise SyncError("This mirror may retain private or unknown history. Keep it private and replace it with a fresh mirror to publish the now-public source; see the README.", code="PRIVATE_HISTORY")
                target_visibility = "private" if source.private else "public"
                if self.settings.dry_run:
                    result = Result(source.full_name, "would_sync" if project else "would_create",
                                    f"{'Existing managed mirror' if project else 'New GitLab project'}; target visibility: {target_visibility}",
                                    visibility=target_visibility)
                else:
                    if project is None:
                        with operation("project_create"):
                            project = self.gitlab.create_project(source, self.settings.group_id)
                            self._validate_project(project, source)
                        owned = True
                        with operation("visibility_update"):
                            self.gitlab.set_visibility(project, "private")
                        by_key[source.key] = project
                        by_path[project["path"].casefold()] = project
                    with operation("project_prepare"):
                        project = self.gitlab.prepare_project(project, source)
                        self._validate_project(project, source)
                    with operation("git_transfer"):
                        result = self._sync(source, project)
                    with operation("visibility_update"):
                        visibility = self._finish_visibility(source, project, group)
                    result = Result(result.repo, result.status, f"{result.detail}; visibility: {visibility}", visibility=visibility)
            except Exception as exc:
                code = failure_code(exc)
                stage = failure_stage(exc)
                visibility = None
                if owned and not self.settings.dry_run:
                    try:
                        self.gitlab.set_visibility(project, "private")
                        visibility = "private"
                    except Exception:
                        code = "PRIVACY_ROLLBACK_FAILED"
                        stage = "visibility_update"
                result = Result(source.full_name, "failed", failure_message(code),
                                visibility=visibility, failure_code=code, stage=stage)
            results.append(result)
            emit_result(result, len(results), self.show_repository_names)
        return results

    def _refresh_source(self, source: SourceRepository) -> SourceRepository:
        with operation("source_refresh"):
            current = self.github.repository(source.full_name)
            if current.key != source.key:
                raise SyncError("The GitHub source identity changed during synchronization; retry after checking the source.", code="SOURCE_CHANGED")
        return current

    def _finish_visibility(self, source: SourceRepository, project: dict, group: dict) -> str:
        current = self._refresh_source(source)
        if current.private:
            # This also permanently records private history before a future run
            # could download the now-private source with authenticated Git.
            with operation("project_prepare"):
                self.gitlab.prepare_project(project, current)
            return "private"
        if group["visibility"] != "public":
            raise SyncError("The GitLab group must be Public to publish public source repositories.", code="PUBLIC_GROUP_REQUIRED")
        if visibility_history(project) != "public-only":
            raise SyncError("The source is now public, but this mirror may retain private history. Replace it with a fresh mirror before publishing; see the README.", code="PRIVATE_HISTORY")
        with operation("visibility_update"):
            self.gitlab.set_visibility(project, "public")
        return "public"

    def _sync(self, source: SourceRepository, project: dict) -> Result:
        with self.mirror_factory(source.clone_url, project["http_url_to_repo"],
                                 self.settings.github_token if source.private else "", self.settings.gitlab_token) as mirror:
            destination = mirror.destination_refs()
            if not mirror.refs:
                if destination:
                    raise SyncError("The source has no branches or tags but the mirror is nonempty. Retaining the destination for manual review.", code="SOURCE_EMPTY")
                return Result(source.full_name, "empty", "Source and destination have no branches or tags")
            has_branches = any(ref.startswith("refs/heads/") for ref in mirror.refs)
            desired_default = source.default_branch if has_branches else None
            if not has_branches and any(ref.startswith("refs/heads/") for ref in destination):
                raise SyncError("The source has only tags but the mirror has branches. Retaining the destination default branch for manual review.", code="SOURCE_EMPTY")
            if desired_default and f"refs/heads/{desired_default}" not in mirror.refs:
                raise SyncError("The source default branch changed during discovery; retry the sync.", code="SOURCE_CHANGED")
            if mirror.refs == destination and project.get("default_branch") == desired_default:
                return Result(source.full_name, "unchanged", "Branches, tags, and default branch already match")
            # Public mirrors remain public when unchanged, but transfers are
            # staged privately until refs and fresh source visibility agree.
            with operation("visibility_update"):
                self.gitlab.set_visibility(project, "private")
            # LFS objects must arrive before refs: GitLab rejects missing objects.
            mirror.transfer_lfs()
            if (not mirror.refs
                    or has_branches != any(ref.startswith("refs/heads/") for ref in mirror.refs)
                    or (desired_default and f"refs/heads/{desired_default}" not in mirror.refs)):
                raise SyncError("Source refs changed during fetch; retry the sync before updating GitLab.", code="SOURCE_CHANGED")
            with operation("protection_update"):
                self.gitlab.unprotect(project["id"])
            mirror.push_refs(prune=False)
            # GitLab never permits deleting its current default branch. Install
            # the new one first, switch the setting, then prune obsolete refs.
            if desired_default and project.get("default_branch") != desired_default:
                with operation("default_branch_update"):
                    self.gitlab.set_default_branch(project["id"], desired_default)
            with operation("protection_update"):
                self.gitlab.unprotect(project["id"])
            mirror.push_refs(prune=True)
            mirror.verify()
            return Result(source.full_name, "synced", f"Verified {len(mirror.refs)} branches and tags")


def write_summary(results: list[Result], dry_run: bool):
    fields = [result_fields(result) for result in results]
    counts = Counter(status for status, _, _ in fields)
    heading = "GitLab sync dry run" if dry_run else "GitLab sync"
    total = f"{len(results)} repositories; " + ", ".join(f"{n} {status}" for status, n in sorted(counts.items()))
    print(total)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        lines = [f"## {heading}", "", total, "",
                 "| Repository alias | Result | Visibility | Detail |", "| --- | --- | --- | --- |"]
        # Summaries always use aliases, including local named diagnostics.
        for index, (status, visibility, message) in enumerate(fields, 1):
            lines.append(f"| `repo-{index:03d}` | {status} | {visibility} | {message} |")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally echoes invalid input; it could itself contain a
        # private identifier or secret. The static help remains available.
        raise SyncError("Invalid command-line options; use --help.", code="CONFIG")


def main(argv=None) -> int:
    try:
        parser = SafeArgumentParser(description=__doc__)
        parser.add_argument("--dry-run", action="store_true", help="Discover and validate destinations without making changes or transferring Git data.")
        parser.add_argument("--show-repository-names", action="store_true",
                            help="Show JSON-escaped repository labels locally; forbidden in GitHub Actions. Raw errors remain hidden.")
        args = parser.parse_args(argv)
        validate_logging(args.show_repository_names)
        settings = Settings.from_env(dry_run=args.dry_run)
        runner = SyncRunner(settings, GitHubAPI(settings.github_token), GitLabAPI(settings.gitlab_token),
                            show_repository_names=args.show_repository_names)
        results = runner.run()
        write_summary(results, settings.dry_run)
        return 1 if any(result.status == "failed" for result in results) else 0
    except (Exception, KeyboardInterrupt) as exc:
        code = failure_code(exc)
        stage = failure_stage(exc)
        context = f" at {stage}" if stage != "unknown" else ""
        print(f"Sync failed [{code}{context}]: {failure_message(code)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
