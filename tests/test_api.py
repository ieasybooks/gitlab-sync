"""Validate API boundaries, credential handling, and workflow exit status."""

from contextlib import redirect_stderr, redirect_stdout
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
import subprocess
import sys
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlsplit

from scripts import sync
from test_runner import project, source


def response(value):
    return io.BytesIO(json.dumps(value).encode())


class ApiTests(unittest.TestCase):
    def test_organization_discovery_reads_every_page_and_keeps_private_repositories(self):
        records = [
            {
                "id": index,
                "name": f"repo-{index:03}",
                "owner": {"id": 118078313, "login": "ieasybooks"},
                "default_branch": "main",
                "private": index == 100,
                "visibility": "private" if index == 100 else "public",
            }
            for index in range(101)
        ]
        api = sync.GitHubAPI("github-test-token")
        api.opener = mock.Mock()
        api.opener.open.side_effect = [response(records[:100]), response(records[100:])]
        repositories = api.repositories("ieasybooks")
        self.assertEqual(len(repositories), 101)
        self.assertTrue(repositories[-1].private)
        calls = api.opener.open.call_args_list
        self.assertEqual(len(calls), 2)
        for page, call in enumerate(calls, start=1):
            request = call.args[0]
            url = urlsplit(request.full_url)
            self.assertEqual(url.path, "/orgs/ieasybooks/repos")
            self.assertEqual(parse_qs(url.query)["page"], [str(page)])
            self.assertEqual(parse_qs(url.query)["per_page"], ["100"])
            self.assertEqual(parse_qs(url.query)["type"], ["all"])
            self.assertEqual(request.get_header("Authorization"), "Bearer github-test-token")

    def test_destination_listing_excludes_shared_projects_and_subgroups(self):
        api = sync.GitLabAPI("gitlab-test-token")
        api.opener = mock.Mock()
        api.opener.open.return_value = response([])
        self.assertEqual(api.projects(42), [])
        request = api.opener.open.call_args.args[0]
        query = parse_qs(urlsplit(request.full_url).query)
        self.assertEqual(query["with_shared"], ["false"])
        self.assertEqual(query["include_subgroups"], ["false"])

    def test_new_project_is_private_empty_and_has_ci_disabled_even_for_public_source(self):
        repository = source(private=False)
        api = sync.GitLabAPI("gitlab-test-token")
        api.opener = mock.Mock()
        api.opener.open.return_value = response(project(repository))
        api.create_project(repository, 42)
        request = api.opener.open.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.method, "POST")
        self.assertEqual(body["namespace_id"], 42)
        self.assertEqual(body["visibility"], "private")
        self.assertIs(body["initialize_with_readme"], False)
        self.assertEqual(body["builds_access_level"], "disabled")
        self.assertIs(body["auto_devops_enabled"], False)
        self.assertEqual(sync.project_source_key(body), repository.key)

    def test_managed_project_is_made_private_before_git_push_without_renaming_its_path(self):
        repository = source("renamed", private=True)
        existing = project(repository, path="original-name")
        existing.update(visibility="public", builds_access_level="enabled")
        api = sync.GitLabAPI("gitlab-test-token")
        def update(method, path, *, body):
            return {**existing, **body}

        with mock.patch.object(api, "request", side_effect=update) as request:
            api.prepare_project(existing, repository)
        self.assertEqual(request.call_args.args, ("PUT", "/projects/50"))
        body = request.call_args.kwargs["body"]
        self.assertEqual(body["visibility"], "private")
        self.assertEqual(body["builds_access_level"], "disabled")
        self.assertNotIn("path", body)
        self.assertNotIn("namespace_id", body)

    def test_fresh_metadata_rejects_malformed_flags_and_treats_conflicts_as_private(self):
        valid = {
            "id": 10,
            "name": "books",
            "full_name": "ieasybooks/books",
            "owner": {"id": 118078313, "login": "ieasybooks"},
            "default_branch": "main",
            "private": False,
            "visibility": "public",
        }
        api = sync.GitHubAPI("github-test-token")
        with mock.patch.object(api, "request", return_value=valid) as request:
            repository = api.repository("ieasybooks/books")
            self.assertFalse(repository.private)
            self.assertEqual(repository.id, 10)
            request.assert_called_once_with("GET", "/repos/ieasybooks/books")
        invalid_records = [
            {**valid, "private": None},
            {**valid, "private": "false"},
            {**valid, "private": 0},
            {**valid, "visibility": "unknown"},
            {key: value for key, value in valid.items() if key != "private"},
        ]
        for record in invalid_records:
            with self.subTest(record=record):
                with mock.patch.object(api, "request", return_value=record):
                    with self.assertRaises(sync.SyncError):
                        api.repository("ieasybooks/books")
        for record in (
            {**valid, "visibility": "private"},
            {**valid, "private": True},
            {**valid, "visibility": "internal"},
        ):
            with self.subTest(ambiguous_or_internal=record):
                with mock.patch.object(api, "request", return_value=record):
                    self.assertTrue(api.repository("ieasybooks/books").private)

    def test_visibility_update_changes_the_passed_project_and_checks_the_server_response(self):
        api = sync.GitLabAPI("gitlab-test-token")
        existing = project(source())
        updated = {**existing, "visibility": "public"}
        with mock.patch.object(api, "request", return_value=updated) as request:
            result = api.set_visibility(existing, "public")
        self.assertEqual(existing["visibility"], "public")
        self.assertEqual(result["visibility"], "public")
        request.assert_called_once_with("PUT", "/projects/50", body={"visibility": "public"})

        for returned_visibility in ("private", "internal", None):
            with self.subTest(returned_visibility=returned_visibility):
                existing = project(source())
                invalid = {**existing, "visibility": returned_visibility}
                with mock.patch.object(api, "request", return_value=invalid):
                    with self.assertRaises(sync.SyncError):
                        api.set_visibility(existing, "public")

    def test_preparation_preserves_public_only_history_and_never_clears_private_history(self):
        public_source = source()
        for history_source, visibility in ((public_source, "public"), (source(private=True), "private")):
            with self.subTest(expected_visibility=visibility):
                existing = project(history_source, visibility="public")
                existing["builds_access_level"] = "enabled"
                api = sync.GitLabAPI("gitlab-test-token")

                def update(method, path, *, body):
                    return {**existing, **body}

                with mock.patch.object(api, "request", side_effect=update) as request:
                    result = api.prepare_project(existing, public_source)
                self.assertEqual(result["visibility"], visibility)
                self.assertEqual(
                    sync.visibility_history(result),
                    "public-only" if visibility == "public" else "private-history",
                )

    def test_unprotect_refuses_inherited_rules_and_url_encodes_project_rule_names(self):
        api = sync.GitLabAPI("gitlab-test-token")
        with mock.patch.object(api, "pages", return_value=[{"name": "main", "inherited": True}]):
            with mock.patch.object(api, "request") as request:
                with self.assertRaises(sync.SyncError):
                    api.unprotect(50)
                request.assert_not_called()
        with mock.patch.object(api, "pages", side_effect=[[{"name": "release/*"}], [{"name": "v*"}]]):
            with mock.patch.object(api, "request") as request:
                api.unprotect(50)
                self.assertEqual(
                    request.call_args_list,
                    [
                        mock.call("DELETE", "/projects/50/protected_branches/release%2F%2A"),
                        mock.call("DELETE", "/projects/50/protected_tags/v%2A"),
                    ],
                )

    def test_transient_read_failure_retries_but_ambiguous_creation_failure_does_not(self):
        api = sync.GitLabAPI("gitlab-test-token")
        headers = Message()
        headers["Retry-After"] = "0"
        failure = HTTPError("https://gitlab.com/api/v4/projects", 429, "Rate limited", headers, io.BytesIO(b"{}"))
        api.opener = mock.Mock()
        api.opener.open.side_effect = [failure, response([])]
        with mock.patch.object(sync.time, "sleep") as sleep:
            self.assertEqual(api.request("GET", "/projects"), [])
            sleep.assert_called_once_with(0)
        self.assertEqual(api.opener.open.call_count, 2)

        api.opener.reset_mock()
        api.opener.open.side_effect = URLError("Connection dropped after sending request")
        with mock.patch.object(sync.time, "sleep") as sleep:
            with self.assertRaises(sync.SyncError):
                api.create_project(source(), 42)
            sleep.assert_not_called()
        api.opener.open.assert_called_once()

    def test_api_error_redacts_raw_and_url_encoded_tokens(self):
        token = "sensitive/token with spaces"
        body = f"Request failed for {token} and {quote(token, safe='')}".encode()
        error = HTTPError("https://gitlab.com/api/v4/projects", 400, "Bad request", Message(), io.BytesIO(body))
        api = sync.GitLabAPI(token)
        api.opener = mock.Mock()
        api.opener.open.side_effect = error
        with self.assertRaises(sync.ApiError) as caught:
            api.request("POST", "/projects", body={})
        self.assertNotIn(token, str(caught.exception))
        self.assertNotIn(quote(token, safe=""), str(caught.exception))
        self.assertIn("[REDACTED]", str(caught.exception))

    def test_askpass_never_releases_credentials_to_lookalike_hosts(self):
        environment = {
            **os.environ,
            "SYNC_GITHUB_TOKEN": "github-test-secret",
            "SYNC_GITLAB_TOKEN": "gitlab-test-secret",
        }
        for host, expected in (
            ("github.com", "github-test-secret"),
            ("gitlab.com", "gitlab-test-secret"),
            ("github.com.attacker.invalid", None),
            ("gitlab.com@attacker.invalid", None),
        ):
            with self.subTest(host=host):
                result = subprocess.run(
                    [sys.executable, "-c", sync.ASKPASS, f"Password for 'https://{host}': "],
                    env=environment,
                    text=True,
                    capture_output=True,
                )
                if expected is None:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, "")
                else:
                    self.assertEqual(result.returncode, 0)
                    self.assertEqual(result.stdout.strip(), expected)

    def test_workflow_process_fails_when_any_repository_failed(self):
        results = [
            sync.Result("ieasybooks/one", "synced", "ok"),
            sync.Result("ieasybooks/two", "failed", "test error"),
        ]
        environment = {"GITLAB_GROUP_ID": "42", "GITLAB_TOKEN": "test-token"}
        with mock.patch.dict(os.environ, environment, clear=True):
            with mock.patch.object(sync.SyncRunner, "run", return_value=results):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(sync.main([]), 1)


class RedirectTests(unittest.TestCase):
    def test_api_does_not_follow_redirects_with_credentials(self):
        observed = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                observed.append((self.path, self.headers.get("PRIVATE-TOKEN")))
                if self.path == "/from":
                    self.send_response(302)
                    self.send_header("Location", "/sink")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"{}")

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            api = sync.ApiClient(
                f"http://127.0.0.1:{server.server_port}",
                "test",
                {"PRIVATE-TOKEN": "must-not-be-forwarded"},
                "must-not-be-forwarded",
            )
            with self.assertRaises(sync.ApiError) as caught:
                api.request("GET", "/from")
            self.assertEqual(caught.exception.status, 302)
            self.assertEqual(observed, [("/from", "must-not-be-forwarded")])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
