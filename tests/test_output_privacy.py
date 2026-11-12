"""Public Actions logs must never expose source metadata or remote errors."""

import base64
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib.parse import quote

from scripts import sync
from test_runner import RecordingGitHub, RecordingGitLab, RecordingMirror, project, source


class PublicOutputPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.name = "canary-private-repository-7a19"
        self.destination_name = "canary-destination-project-5c83"
        self.namespace = "canary-destination-namespace-4d62"
        self.repository_id = 783129064215
        self.project_id = 634287105923
        self.repository = source(self.name, repository_id=self.repository_id, private=True)
        self.project = project(self.repository, project_id=self.project_id, path=self.destination_name)
        self.project["path_with_namespace"] = f"{self.namespace}/{self.destination_name}"
        self.project["http_url_to_repo"] = f"https://gitlab.com/{self.project['path_with_namespace']}.git"
        self.source_token = "github-canary-token-tail-45df"
        self.destination_token = "gitlab-canary-token-tail-8a21"
        private_url = f"https://github.com/{self.repository.full_name}/tree/canary-private-branch"
        self.canaries = (
            self.name,
            self.destination_name,
            self.namespace,
            str(self.repository_id),
            str(self.project_id),
            private_url,
            quote(private_url, safe=""),
            base64.b64encode(private_url.encode()).decode(),
            self.source_token,
            self.destination_token,
            "token-fragment-canary-489a",
        )
        self.raw_error = "\n::error::" + " | ".join(self.canaries) + "\n<script>canary</script>"

    def assert_sanitized(self, *outputs):
        for output in outputs:
            for canary in self.canaries:
                self.assertNotIn(canary, output)
            self.assertNotIn("Traceback (most recent call last)", output)
            self.assertNotIn("<script>", output)

    def execute(self, *, repositories=None, projects=None, transfer_error=None,
                refresh_error=None, discovery_error=None, rollback_error=None,
                argv=(), environment=None):
        values = [self.repository] if repositories is None else repositories
        destinations = [self.project] if projects is None else projects
        events = []
        github = RecordingGitHub(values, automation_private=False, events=events)
        github.automation_is_private = mock.Mock(side_effect=AssertionError(self.raw_error))
        gitlab = RecordingGitLab(destinations, events)
        gitlab.group = mock.Mock(return_value={"id": 42, "full_path": self.namespace, "visibility": "public"})
        if discovery_error is not None:
            github.repositories = mock.Mock(side_effect=discovery_error)
        if refresh_error is not None:
            github.repository = mock.Mock(side_effect=refresh_error)
        if rollback_error is not None:
            original_set_visibility = gitlab.set_visibility
            private_calls = 0

            def fail_rollback(value, visibility):
                nonlocal private_calls
                if visibility == "private":
                    private_calls += 1
                    if private_calls > 1:
                        raise rollback_error
                return original_set_visibility(value, visibility)

            gitlab.set_visibility = fail_rollback

        def mirror_factory(*args, **kwargs):
            mirror = RecordingMirror(events, {"refs/heads/main": "a" * 40}, {})
            if transfer_error is not None:
                mirror.transfer_lfs = mock.Mock(side_effect=transfer_error)
            return mirror

        runner_type = sync.SyncRunner

        def runner_factory(settings, source_api, destination_api, **kwargs):
            return runner_type(settings, source_api, destination_api, mirror_factory=mirror_factory, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            summary = Path(directory) / "summary.md"
            env = {
                "GITHUB_ACTIONS": "true",
                "GITHUB_REPOSITORY": "ieasybooks/gitlab-sync",
                "GITHUB_STEP_SUMMARY": str(summary),
                "GITLAB_GROUP_ID": "42",
                "GITLAB_TOKEN": self.destination_token,
                "GH_TOKEN": self.source_token,
            }
            env.update(environment or {})
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, env, clear=True):
                with mock.patch.object(sync, "GitHubAPI", return_value=github) as source_api:
                    with mock.patch.object(sync, "GitLabAPI", return_value=gitlab) as destination_api:
                        with mock.patch.object(sync, "SyncRunner", side_effect=runner_factory) as runner:
                            with redirect_stdout(stdout), redirect_stderr(stderr):
                                status = sync.main(list(argv))
            outputs = (stdout.getvalue(), stderr.getvalue(), summary.read_text() if summary.exists() else "")
        return status, outputs, github, gitlab, events, source_api, destination_api, runner

    def test_private_success_in_public_actions_uses_aliases_and_never_probes_its_own_repo(self):
        status, outputs, github, gitlab, *_ = self.execute()
        self.assertEqual(status, 0)
        self.assertEqual(gitlab.values[0]["visibility"], "private")
        github.automation_is_private.assert_not_called()
        self.assert_sanitized(*outputs)
        self.assertIn("repo-001", outputs[0])
        self.assertIn("repo-001", outputs[2])

    def test_remote_git_api_and_unexpected_errors_are_safe_in_all_output_channels(self):
        errors = (
            sync.SyncError(f"Git operation failed: {self.raw_error}"),
            sync.ApiError("GitLab", 403, self.raw_error),
            RuntimeError(self.raw_error),
            ValueError(self.raw_error),
        )
        for error in errors:
            with self.subTest(error_type=type(error).__name__):
                status, outputs, *_ = self.execute(transfer_error=error)
                self.assertEqual(status, 1)
                self.assert_sanitized(*outputs)
                self.assertIn("repo-001", outputs[0])
                self.assertIn("repo-001", outputs[2])

    def test_unexpected_discovery_and_metadata_exceptions_never_produce_tracebacks(self):
        for options in (
            {"discovery_error": RuntimeError(self.raw_error)},
            {"refresh_error": RuntimeError(self.raw_error)},
        ):
            with self.subTest(stage=next(iter(options))):
                status, outputs, *_ = self.execute(**options)
                self.assertEqual(status, 1)
                self.assert_sanitized(*outputs)

    def test_failed_privacy_rollback_does_not_print_its_error_detail(self):
        public = replace(self.repository, private=False)
        destination = project(public, project_id=self.project_id, path=self.destination_name, visibility="public")
        status, outputs, *_ = self.execute(
            repositories=[public],
            projects=[destination],
            transfer_error=sync.SyncError(self.raw_error),
            rollback_error=RuntimeError(self.raw_error),
        )
        self.assertEqual(status, 1)
        self.assert_sanitized(*outputs)

    def test_dry_run_cleanup_and_visibility_downgrade_do_not_print_names_or_ids(self):
        public_destination = {**self.project, "visibility": "public"}
        for options in (
            {"argv": ("--dry-run",)},
            {"repositories": [], "projects": [public_destination]},
            {"repositories": [], "projects": [public_destination], "argv": ("--dry-run",)},
            {"projects": [public_destination]},
        ):
            with self.subTest(options=tuple(options)):
                status, outputs, *_ = self.execute(**options)
                self.assertEqual(status, 0)
                self.assert_sanitized(*outputs)
                self.assertIn("repo-001", outputs[0])
                self.assertIn("repo-001", outputs[2])

    def test_action_debug_and_named_output_are_rejected_before_api_clients_are_used(self):
        cases = (
            {"environment": {"RUNNER_DEBUG": "1"}},
            {"environment": {"ACTIONS_STEP_DEBUG": "true"}},
            {"environment": {"ACTIONS_RUNNER_DEBUG": "true"}},
            {"argv": ("--show-repository-names",)},
        )
        for options in cases:
            with self.subTest(options=options):
                status, outputs, _, _, _, source_api, destination_api, runner = self.execute(**options)
                self.assertEqual(status, 1)
                self.assert_sanitized(*outputs)
                source_api.assert_not_called()
                destination_api.assert_not_called()
                runner.assert_not_called()

    def test_local_named_output_still_redacts_errors_and_uses_aliases_in_summaries(self):
        status, outputs, *_ = self.execute(
            argv=("--show-repository-names",),
            environment={"GITHUB_ACTIONS": "false"},
            transfer_error=sync.SyncError(self.raw_error),
        )
        self.assertEqual(status, 1)
        self.assertIn(json.dumps(self.repository.full_name), outputs[0])
        self.assert_sanitized(outputs[1], outputs[2])
        for canary in self.canaries[1:]:
            self.assertNotIn(canary, outputs[0])
        self.assertIn("repo-001", outputs[2])

    def test_reporters_reject_malicious_status_visibility_detail_and_error_codes(self):
        error = sync.SyncError(self.raw_error)
        error.code = self.raw_error
        codes = (
            self.raw_error,
            sync.failure_code(error),
            sync.failure_code(sync.ApiError(self.name, self.repository_id, self.raw_error)),
        )
        with tempfile.TemporaryDirectory() as directory:
            summary = Path(directory) / "summary.md"
            with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}, clear=True):
                for code in codes:
                    result = sync.Result(
                        self.repository.full_name,
                        self.raw_error,
                        self.raw_error,
                        visibility=self.raw_error,
                        failure_code=code,
                    )
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        sync.emit_result(result, 1)
                        sync.write_summary([result], False)
                    self.assert_sanitized(stdout.getvalue(), stderr.getvalue(), summary.read_text())
                    self.assertIn("repo-001", stdout.getvalue())

    def test_local_repository_names_are_json_escaped_and_raw_detail_is_never_printed(self):
        name = self.repository.full_name + "\n::warning::injected"
        result = sync.Result(name, "failed", self.raw_error)
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}, clear=True):
            with redirect_stdout(stdout):
                sync.emit_result(result, 1, show_repository_names=True)
        self.assertIn(json.dumps(name), stdout.getvalue())
        self.assertNotIn("\n::warning::injected", stdout.getvalue())
        self.assertNotIn(self.destination_name, stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
