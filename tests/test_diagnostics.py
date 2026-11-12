"""Failures identify fixed operations without exposing Git/API diagnostics."""

from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from scripts import sync
from test_runner import RecordingGitHub, source


CANARY = "private-stage-canary-7183"
RAW_ERROR = f"https://example.invalid/{CANARY}/secret-branch\n::error::{CANARY}"
SHA = "a" * 40


def git_action(command):
    if "ls-remote" in command:
        remote = command[command.index("ls-remote") + 2]
        return "source_refs" if remote == "github" else "destination_refs"
    if "lfs" in command:
        action = command[command.index("lfs") + 1]
        return {"version": "lfs_check", "ls-files": "lfs_scan", "fetch": "lfs_fetch", "push": "lfs_push"}[action]
    if "fetch" in command:
        return "source_fetch"
    if "for-each-ref" in command:
        return "source_inspect"
    if "push" in command:
        return "refs_prune" if "--prune" in command else "refs_push"
    return "git_setup"


class GitDiagnosticTests(unittest.TestCase):
    def test_each_git_failure_preserves_its_specific_safe_stage(self):
        stages = (
            "git_setup", "source_refs", "destination_refs", "source_fetch", "source_inspect",
            "lfs_check", "lfs_scan", "lfs_fetch", "lfs_push", "refs_push", "refs_prune", "refs_verify",
        )
        for expected_stage in stages:
            with self.subTest(stage=expected_stage):
                failing_action = "destination_refs" if expected_stage == "refs_verify" else expected_stage

                def run(command, **kwargs):
                    action = git_action(command)
                    if action == failing_action:
                        return subprocess.CompletedProcess(command, 128, stdout="", stderr=RAW_ERROR)
                    output = ""
                    if action == "source_refs":
                        output = f"{SHA}\trefs/heads/main\n"
                    elif action == "source_inspect":
                        output = f"{SHA} refs/heads/main\n"
                    elif action == "lfs_scan":
                        output = f"{SHA} - {CANARY}.bin\n"
                    return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

                with mock.patch.object(sync.subprocess, "run", side_effect=run):
                    with self.assertRaises(sync.SyncError) as raised:
                        with sync.GitMirror(
                            f"https://github.com/example/{CANARY}.git",
                            f"https://gitlab.com/example/{CANARY}.git",
                        ) as mirror:
                            if expected_stage == "destination_refs":
                                mirror.destination_refs()
                            elif expected_stage in {"source_fetch", "source_inspect"}:
                                mirror.fetch()
                            elif expected_stage.startswith("lfs_"):
                                mirror.transfer_lfs()
                            elif expected_stage in {"refs_push", "refs_prune"}:
                                mirror.push_refs(prune=expected_stage == "refs_prune")
                            elif expected_stage == "refs_verify":
                                mirror.verify()
                self.assertEqual(raised.exception.stage, expected_stage)
                result = sync.Result(
                    CANARY, "failed", str(raised.exception),
                    failure_code=sync.failure_code(raised.exception), stage=raised.exception.stage,
                )
                output = io.StringIO()
                with redirect_stdout(output):
                    sync.emit_result(result, 1)
                self.assertIn(expected_stage, output.getvalue())
                self.assertIn("repo-001", output.getvalue())
                self.assertNotIn(CANARY, output.getvalue())
                self.assertNotIn("https://example.invalid", output.getvalue())

    def test_project_creation_http_error_has_a_safe_fixed_api_stage(self):
        repository = source(CANARY)
        settings = sync.Settings("ieasybooks", 42, "gitlab-test-token", "github-test-token")
        github = RecordingGitHub([repository])
        gitlab = sync.GitLabAPI("gitlab-test-token")
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.object(gitlab, "group", return_value={"id": 42, "visibility": "public"}):
            with mock.patch.object(gitlab, "projects", return_value=[]):
                with mock.patch.object(gitlab, "request", side_effect=sync.ApiError("GitLab", 400, RAW_ERROR)):
                    with redirect_stdout(output), redirect_stderr(errors):
                        results = sync.SyncRunner(settings, github, gitlab).run()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "failed")
        self.assertEqual(results[0].failure_code, "GITLAB_HTTP_400")
        self.assertEqual(results[0].stage, "project_create")
        self.assertIn("project_create", output.getvalue())
        self.assertIn("GITLAB_HTTP_400", output.getvalue())
        self.assertNotIn(CANARY, output.getvalue() + errors.getvalue())

    def test_only_allowed_failure_stages_appear_in_logs_or_summaries(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = Path(directory) / "summary.md"
            with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}, clear=True):
                valid = sync.Result(CANARY, "failed", RAW_ERROR, failure_code="GIT_FAILED", stage="source_fetch")
                output = io.StringIO()
                with redirect_stdout(output):
                    sync.emit_result(valid, 1)
                    sync.write_summary([valid], False)
                self.assertIn("source_fetch", output.getvalue())
                self.assertIn("source_fetch", summary.read_text())

                for stage in (RAW_ERROR, [CANARY], {"stage": CANARY}):
                    with self.subTest(stage_type=type(stage).__name__):
                        summary.write_text("")
                        result = sync.Result(CANARY, "failed", RAW_ERROR, failure_code="GIT_FAILED", stage=stage)
                        output = io.StringIO()
                        with redirect_stdout(output):
                            sync.emit_result(result, 1)
                            sync.write_summary([result], False)
                        self.assertNotIn(CANARY, output.getvalue() + summary.read_text())

                summary.write_text("")
                success = sync.Result(CANARY, "synced", RAW_ERROR, stage="source_fetch")
                output = io.StringIO()
                with redirect_stdout(output):
                    sync.emit_result(success, 1)
                    sync.write_summary([success], False)
                self.assertNotIn("source_fetch", output.getvalue() + summary.read_text())
                self.assertNotIn(CANARY, output.getvalue() + summary.read_text())


if __name__ == "__main__":
    unittest.main()
