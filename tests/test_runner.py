"""Check ownership, dry-run, and ordering with recording API/Git doubles."""

from contextlib import redirect_stdout
from copy import deepcopy
import io
import os
import unittest
from unittest import mock

from scripts.sync import (
    Settings,
    SourceRepository,
    SyncError,
    SyncRunner,
    source_marker,
)


def source(name="books", repository_id=10, default_branch="main", private=False):
    return SourceRepository(
        id=repository_id,
        owner_id=118078313,
        name=name,
        full_name=f"ieasybooks/{name}",
        default_branch=default_branch,
        private=private,
    )


def project(repository, project_id=50, path=None, namespace_id=42):
    path = path or repository.name
    return {
        "id": project_id,
        "name": path,
        "path": path,
        "path_with_namespace": f"mirrors/{path}",
        "namespace": {"id": namespace_id, "full_path": "mirrors"},
        "http_url_to_repo": f"https://gitlab.com/mirrors/{path}.git",
        "description": source_marker(repository),
        "default_branch": "main",
        "visibility": "private",
        "builds_access_level": "disabled",
        "archived": False,
    }


class RecordingGitHub:
    def __init__(self, repositories, automation_private=True):
        self.values = repositories
        self.automation_private = automation_private

    def repositories(self, organization):
        return self.values

    def automation_is_private(self, full_name):
        return self.automation_private


class RecordingGitLab:
    def __init__(self, projects, events):
        self.values = deepcopy(list(projects))
        self.events = events

    def group(self, group_id):
        return {"id": group_id, "full_path": "mirrors", "path": "mirrors"}

    def projects(self, group_id):
        return self.values

    def create_project(self, repository, group_id):
        self.events.append(("create", repository.id))
        created = project(repository, project_id=100 + repository.id, namespace_id=group_id)
        self.values.append(created)
        return created

    def prepare_project(self, value, repository):
        self.events.append(("prepare", value["id"]))
        value.update(
            visibility="private",
            builds_access_level="disabled",
            description=source_marker(repository),
        )
        return value

    def unprotect(self, project_id):
        self.events.append(("unprotect", project_id))

    def set_default_branch(self, project_id, branch):
        self.events.append(("default", project_id, branch))
        for value in self.values:
            if value["id"] == project_id:
                value["default_branch"] = branch


class RecordingMirror:
    def __init__(self, events, source_refs, destination_refs):
        self.events = events
        self.refs = dict(source_refs)
        self.destination = dict(destination_refs)

    def __enter__(self):
        self.events.append(("open",))
        return self

    def __exit__(self, *args):
        self.events.append(("close",))

    def destination_refs(self):
        return dict(self.destination)

    def transfer_lfs(self):
        self.events.append(("lfs",))

    def push_refs(self, prune=False):
        self.events.append(("push", prune))
        if prune:
            self.destination = dict(self.refs)
        else:
            self.destination.update(self.refs)

    def verify(self):
        self.events.append(("verify",))
        if self.destination != self.refs:
            raise SyncError("Destination references differ")


class SyncRunnerTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.factory_calls = []
        self.source_refs = {"refs/heads/main": "a" * 40}
        self.destination_refs = {}
        self.settings = Settings(
            source_org="ieasybooks",
            group_id=42,
            gitlab_token="test-gitlab-secret",
            github_token="test-github-secret",
        )
        self.environment = mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "false"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def mirror_factory(self, *args, **kwargs):
        self.factory_calls.append((args, kwargs))
        return RecordingMirror(self.events, self.source_refs, self.destination_refs)

    def run_sync(self, repositories, projects=(), settings=None, github=None):
        self.gitlab = RecordingGitLab(projects, self.events)
        runner = SyncRunner(
            settings or self.settings,
            github or RecordingGitHub(repositories),
            self.gitlab,
            mirror_factory=self.mirror_factory,
        )
        with redirect_stdout(io.StringIO()):
            return runner.run()

    def test_dry_run_discovers_new_and_existing_projects_without_mutations(self):
        existing = source()
        new = source("new", repository_id=11)
        settings = Settings(
            source_org="ieasybooks", group_id=42, gitlab_token="secret", dry_run=True
        )
        results = self.run_sync([existing, new], [project(existing)], settings=settings)
        self.assertEqual({result.status for result in results}, {"would_create", "would_sync"})
        self.assertEqual(self.events, [])
        self.assertEqual(self.factory_calls, [])

    def test_existing_unmanaged_project_is_never_overwritten(self):
        repository = source()
        unmanaged = project(repository)
        unmanaged["description"] = "Maintained by a human"
        results = self.run_sync([repository], [unmanaged])
        self.assertEqual([result.status for result in results], ["failed"])
        self.assertEqual(self.events, [])
        self.assertEqual(self.factory_calls, [])

    def test_recreated_source_cannot_take_over_the_old_repository_mirror(self):
        original = source(repository_id=10)
        replacement = source(repository_id=11)
        results = self.run_sync([replacement], [project(original)])
        self.assertEqual([result.status for result in results], ["failed"])
        self.assertEqual(self.events, [])

    def test_marker_for_another_github_owner_cannot_authorize_overwrite(self):
        repository = source()
        foreign = SourceRepository(
            id=repository.id,
            owner_id=999,
            name=repository.name,
            full_name=f"another-org/{repository.name}",
            default_branch="main",
            private=False,
        )
        results = self.run_sync([repository], [project(foreign)])
        self.assertEqual([result.status for result in results], ["failed"])
        self.assertEqual(self.events, [])

    def test_renamed_source_reuses_the_existing_destination_path_by_identity(self):
        old = source("old-name")
        renamed = source("new-name")
        results = self.run_sync([renamed], [project(old)])
        self.assertEqual([result.status for result in results], ["synced"])
        self.assertNotIn(("create", renamed.id), self.events)
        args, kwargs = self.factory_calls[0]
        destination = kwargs.get("destination_url", args[1] if len(args) > 1 else None)
        self.assertEqual(destination, "https://gitlab.com/mirrors/old-name.git")

    def test_pushes_lfs_and_new_default_before_pruning_the_old_default(self):
        repository = source(default_branch="trunk")
        self.source_refs = {"refs/heads/trunk": "a" * 40}
        self.destination_refs = {"refs/heads/main": "b" * 40}
        results = self.run_sync([repository], [project(repository)])
        self.assertEqual([result.status for result in results], ["synced"])
        self.assertLess(self.events.index(("lfs",)), self.events.index(("push", False)))
        self.assertLess(
            self.events.index(("push", False)), self.events.index(("default", 50, "trunk"))
        )
        self.assertLess(
            self.events.index(("default", 50, "trunk")), self.events.index(("push", True))
        )
        unprotect_positions = [
            index for index, event in enumerate(self.events) if event == ("unprotect", 50)
        ]
        self.assertTrue(any(index < self.events.index(("push", False)) for index in unprotect_positions))
        self.assertTrue(any(
            self.events.index(("default", 50, "trunk")) < index < self.events.index(("push", True))
            for index in unprotect_positions
        ))
        self.assertLess(self.events.index(("push", True)), self.events.index(("verify",)))

    def test_empty_source_does_not_erase_existing_destination_history(self):
        repository = source()
        self.source_refs = {}
        self.destination_refs = {"refs/heads/main": "a" * 40}
        results = self.run_sync([repository], [project(repository)])
        self.assertEqual([result.status for result in results], ["failed"])
        self.assertFalse(any(event[0] == "push" for event in self.events))

    def test_empty_source_and_destination_are_successful_without_pushes(self):
        self.source_refs = {}
        results = self.run_sync([source()])
        self.assertEqual([result.status for result in results], ["empty"])
        self.assertFalse(any(event[0] == "push" for event in self.events))

    def test_tag_only_source_syncs_without_setting_a_nonexistent_default_branch(self):
        self.source_refs = {"refs/tags/archive": "a" * 40}
        results = self.run_sync([source(default_branch="main")])
        self.assertEqual([result.status for result in results], ["synced"])
        self.assertFalse(any(event[0] == "default" for event in self.events))
        self.assertIn(("push", True), self.events)

    def test_tag_only_source_retains_existing_destination_default_for_manual_review(self):
        repository = source(default_branch="main")
        self.source_refs = {"refs/tags/archive": "a" * 40}
        self.destination_refs = {"refs/heads/main": "b" * 40}
        results = self.run_sync([repository], [project(repository)])
        self.assertEqual([result.status for result in results], ["failed"])
        self.assertFalse(any(event[0] in {"push", "default"} for event in self.events))

    def test_one_repository_failure_does_not_prevent_other_repositories_syncing(self):
        broken = source("broken", repository_id=10)
        healthy = source("healthy", repository_id=11)
        original_factory = self.mirror_factory

        def sometimes_fails(*args, **kwargs):
            source_url = kwargs.get("source_url", args[0] if args else "")
            if "broken" in source_url:
                raise SyncError("Test transport failure")
            return original_factory(*args, **kwargs)

        self.mirror_factory = sometimes_fails
        results = self.run_sync([broken, healthy])
        self.assertEqual([result.status for result in results], ["failed", "synced"])

    def test_missing_source_repository_does_not_delete_destination_projects(self):
        old = source("not-visible-anymore")
        results = self.run_sync([], [project(old)])
        self.assertEqual(results, [])
        self.assertEqual(self.events, [])

    def test_private_sources_are_rejected_before_public_actions_logs_or_writes(self):
        private_source = source("private-project", private=True)
        github = RecordingGitHub([private_source], automation_private=False)
        with mock.patch.dict(
            os.environ,
            {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "ieasybooks/gitlab-sync"},
        ):
            with self.assertRaises(SyncError):
                self.run_sync([private_source], github=github)
        self.assertEqual(self.events, [])
        self.assertEqual(self.factory_calls, [])


if __name__ == "__main__":
    unittest.main()
