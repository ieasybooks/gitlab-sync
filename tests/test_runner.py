"""Check ownership, dry-run, and ordering with recording API/Git doubles."""

from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import replace
import io
import os
import unittest
from unittest import mock

from scripts.sync import (
    Settings,
    GitLabAPI,
    SourceRepository,
    SyncError,
    SyncRunner,
    project_description,
    source_marker,
    visibility_history,
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


def project(repository, project_id=50, path=None, namespace_id=42, visibility="private"):
    path = path or repository.name
    return {
        "id": project_id,
        "name": path,
        "path": path,
        "path_with_namespace": f"mirrors/{path}",
        "namespace": {"id": namespace_id, "full_path": "mirrors"},
        "http_url_to_repo": f"https://gitlab.com/mirrors/{path}.git",
        "description": project_description(repository),
        "default_branch": "main",
        "visibility": visibility,
        "builds_access_level": "disabled",
        "archived": False,
    }


class RecordingGitHub:
    def __init__(self, repositories, automation_private=True, refreshed=None, events=None, owner_id=118078313):
        self.values = repositories
        self.automation_private = automation_private
        self.refreshed = refreshed or {}
        self.events = events if events is not None else []
        self.owner_id = owner_id

    def organization_id(self, organization):
        return self.owner_id

    def repositories(self, organization):
        return self.values

    def repository(self, full_name):
        self.events.append(("refresh", full_name))
        value = self.refreshed.get(full_name)
        if isinstance(value, list):
            value = value.pop(0)
        if isinstance(value, Exception):
            raise value
        if value is not None:
            return value
        for repository in self.values:
            if repository.full_name == full_name:
                return repository
        raise SyncError("The repository is no longer accessible")

    def automation_is_private(self, full_name):
        return self.automation_private


class RecordingGitLab:
    def __init__(self, projects, events, group_visibility="public"):
        self.values = deepcopy(list(projects))
        self.events = events
        self.group_visibility = group_visibility
        self.created_projects = []

    def group(self, group_id):
        return {"id": group_id, "full_path": "mirrors", "path": "mirrors", "visibility": self.group_visibility}

    def projects(self, group_id):
        return self.values

    def create_project(self, repository, group_id):
        self.events.append(("create", repository.id))
        created = project(repository, project_id=100 + repository.id, namespace_id=group_id)
        self.created_projects.append(deepcopy(created))
        self.values.append(created)
        return created

    def prepare_project(self, value, repository):
        self.events.append(("prepare", value["id"]))
        private_history = repository.private or visibility_history(value) != "public-only"
        visibility = "public" if not private_history and value.get("visibility") == "public" else "private"
        self.set_visibility(value, visibility)
        value.update(
            builds_access_level="disabled",
            description=project_description(repository, private_history=private_history),
        )
        return value

    def set_visibility(self, value, visibility):
        if value.get("visibility") != visibility:
            self.events.append(("visibility", value["id"], visibility))
            value["visibility"] = visibility
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

    def run_sync(self, repositories, projects=(), settings=None, github=None, group_visibility="public"):
        self.gitlab = RecordingGitLab(projects, self.events, group_visibility)
        self.github = github or RecordingGitHub(repositories, events=self.events)
        self.github.events = self.events
        runner = SyncRunner(
            settings or self.settings,
            self.github,
            self.gitlab,
            mirror_factory=self.mirror_factory,
        )
        self.output = io.StringIO()
        with redirect_stdout(self.output):
            return runner.run()

    def test_dry_run_discovers_new_and_existing_projects_without_mutations(self):
        existing = source()
        new = source("new", repository_id=11, private=True)
        settings = Settings(
            source_org="ieasybooks", group_id=42, gitlab_token="secret", dry_run=True
        )
        results = self.run_sync([existing, new], [project(existing)], settings=settings)
        self.assertEqual({result.status for result in results}, {"would_create", "would_sync"})
        details = {result.repo: result.detail for result in results}
        self.assertIn("public", details[existing.full_name])
        self.assertIn("private", details[new.full_name])
        self.assertTrue(all(event[0] == "refresh" for event in self.events))
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

    def test_private_sources_sync_from_public_actions_without_probing_the_sync_repository(self):
        private_source = source("private-project", private=True)
        github = RecordingGitHub([private_source], automation_private=False)
        github.automation_is_private = mock.Mock(side_effect=AssertionError("Unexpected self-repository probe"))
        with mock.patch.dict(
            os.environ,
            {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "ieasybooks/gitlab-sync"},
        ):
            results = self.run_sync([private_source], github=github)
        self.assertEqual([result.status for result in results], ["synced"])
        self.assertEqual(self.gitlab.values[0]["visibility"], "private")
        self.assertEqual(len(self.factory_calls), 1)
        github.automation_is_private.assert_not_called()
        self.assertNotIn(private_source.full_name, self.output.getvalue())

    def test_public_source_is_staged_private_then_published_after_verification_and_refresh(self):
        repository = source()
        results = self.run_sync([repository])
        self.assertEqual([result.status for result in results], ["synced"])
        self.assertEqual(self.gitlab.created_projects[0]["visibility"], "private")
        self.assertEqual(self.gitlab.values[0]["visibility"], "public")
        publication = self.events.index(("visibility", 110, "public"))
        self.assertLess(self.events.index(("verify",)), publication)
        refreshes = [index for index, event in enumerate(self.events) if event == ("refresh", repository.full_name)]
        self.assertEqual(len(refreshes), 2)
        self.assertLess(self.events.index(("verify",)), refreshes[-1])
        self.assertLess(refreshes[-1], publication)
        args, kwargs = self.factory_calls[0]
        self.assertEqual(kwargs.get("github_token", args[2] if len(args) > 2 else None), "")

    def test_private_source_hides_a_formerly_public_mirror_before_git_operations(self):
        repository = source(private=True)
        results = self.run_sync([repository], [project(repository, visibility="public")])
        self.assertEqual([result.status for result in results], ["synced"])
        self.assertEqual(self.gitlab.values[0]["visibility"], "private")
        self.assertLess(self.events.index(("visibility", 50, "private")), self.events.index(("open",)))
        self.assertNotIn(("visibility", 50, "public"), self.events)
        args, kwargs = self.factory_calls[0]
        self.assertEqual(
            kwargs.get("github_token", args[2] if len(args) > 2 else None), self.settings.github_token
        )

    def test_changed_public_mirror_is_hidden_during_transfer_then_republished(self):
        repository = source()
        results = self.run_sync([repository], [project(repository, visibility="public")])
        self.assertEqual([result.status for result in results], ["synced"])
        self.assertLess(self.events.index(("visibility", 50, "private")), self.events.index(("lfs",)))
        self.assertLess(self.events.index(("verify",)), self.events.index(("visibility", 50, "public")))
        final_refresh = max(index for index, event in enumerate(self.events) if event == ("refresh", repository.full_name))
        self.assertLess(self.events.index(("verify",)), final_refresh)
        self.assertLess(final_refresh, self.events.index(("visibility", 50, "public")))

    def test_unchanged_public_mirror_does_not_toggle_visibility(self):
        repository = source()
        self.destination_refs = dict(self.source_refs)
        results = self.run_sync([repository], [project(repository, visibility="public")])
        self.assertEqual([result.status for result in results], ["unchanged"])
        self.assertEqual(self.gitlab.values[0]["visibility"], "public")
        self.assertIn(("refresh", repository.full_name), self.events)
        self.assertFalse(any(event[0] in {"visibility", "lfs", "push"} for event in self.events))

    def test_source_becoming_private_mid_run_is_not_republished(self):
        repository = source()
        github = RecordingGitHub(
            [repository], refreshed={repository.full_name: [repository, replace(repository, private=True)]}
        )
        results = self.run_sync([repository], [project(repository, visibility="public")], github=github)
        self.assertEqual([result.status for result in results], ["synced"])
        self.assertEqual(self.gitlab.values[0]["visibility"], "private")
        self.assertNotIn(("visibility", 50, "public"), self.events)

    def test_failed_refresh_or_changed_source_identity_hides_even_an_unchanged_mirror(self):
        repository = source()
        self.destination_refs = dict(self.source_refs)
        for refreshed in (
            SyncError("GitHub metadata refresh failed"),
            replace(repository, id=999),
            replace(repository, owner_id=999),
        ):
            with self.subTest(refreshed=refreshed):
                self.events.clear()
                github = RecordingGitHub([repository], refreshed={repository.full_name: [repository, refreshed]})
                results = self.run_sync([repository], [project(repository, visibility="public")], github=github)
                self.assertEqual([result.status for result in results], ["failed"])
                self.assertEqual(self.gitlab.values[0]["visibility"], "private")
                self.assertNotIn(("visibility", 50, "public"), self.events)

    def test_transfer_failure_leaves_a_formerly_public_mirror_private(self):
        repository = source()
        original_factory = self.mirror_factory

        def broken_transfer(*args, **kwargs):
            mirror = original_factory(*args, **kwargs)
            mirror.transfer_lfs = mock.Mock(side_effect=SyncError("Missing LFS object"))
            return mirror

        self.mirror_factory = broken_transfer
        results = self.run_sync([repository], [project(repository, visibility="public")])
        self.assertEqual([result.status for result in results], ["failed"])
        self.assertEqual(self.gitlab.values[0]["visibility"], "private")
        self.assertNotIn(("visibility", 50, "public"), self.events)

    def test_missing_or_excluded_public_mirror_is_retained_privately(self):
        repository = source()
        for excluded in (False, True):
            with self.subTest(excluded=excluded):
                self.events.clear()
                settings = replace(self.settings, exclude_repos=frozenset({repository.name})) if excluded else self.settings
                results = self.run_sync(
                    [repository] if excluded else [],
                    [project(repository, visibility="public")],
                    settings=settings,
                )
                self.assertEqual([result.status for result in results], ["retained_private"])
                self.assertEqual(self.gitlab.values[0]["visibility"], "private")
                self.assertEqual(self.events, [("visibility", 50, "private")])
                self.assertEqual(self.factory_calls, [])

    def test_dry_run_reports_missing_public_mirror_without_changing_visibility(self):
        repository = source()
        results = self.run_sync(
            [], [project(repository, visibility="public")], settings=replace(self.settings, dry_run=True)
        )
        self.assertEqual([result.status for result in results], ["would_retain_private"])
        self.assertEqual(self.gitlab.values[0]["visibility"], "public")
        self.assertEqual(self.events, [])

    def test_missing_mirror_owned_by_another_organization_is_untouched(self):
        foreign = replace(source(), owner_id=999, full_name="another-org/books")
        results = self.run_sync([], [project(foreign, visibility="public")])
        self.assertEqual(results, [])
        self.assertEqual(self.gitlab.values[0]["visibility"], "public")
        self.assertEqual(self.events, [])

    def test_nonpublic_destination_group_blocks_public_sources_and_hides_existing_mirror(self):
        repository = source()
        for group_visibility in ("private", "internal"):
            with self.subTest(group_visibility=group_visibility):
                self.events.clear()
                results = self.run_sync(
                    [repository],
                    [project(repository, visibility="public")],
                    group_visibility=group_visibility,
                )
                self.assertEqual([result.status for result in results], ["failed"])
                self.assertEqual(self.gitlab.values[0]["visibility"], "private")
                self.assertEqual(self.factory_calls, [])
                self.assertNotIn(("visibility", 50, "public"), self.events)

    def test_private_source_can_sync_to_a_private_destination_group(self):
        repository = source(private=True)
        results = self.run_sync([repository], group_visibility="private")
        self.assertEqual([result.status for result in results], ["synced"])
        self.assertEqual(self.gitlab.values[0]["visibility"], "private")

    def test_former_private_or_legacy_mirror_is_not_published_for_a_now_public_source(self):
        repository = source()
        descriptions = (
            project_description(replace(repository, private=True)),
            source_marker(repository),
        )
        for description in descriptions:
            with self.subTest(description=description):
                self.events.clear()
                existing = project(repository)
                existing["description"] = description
                results = self.run_sync([repository], [existing])
                self.assertEqual([result.status for result in results], ["failed"])
                self.assertEqual(self.gitlab.values[0]["visibility"], "private")
                self.assertNotIn(("visibility", 50, "public"), self.events)

    def test_public_orchestrator_hides_and_syncs_a_private_sources_existing_mirror(self):
        repository = source("now-private", private=True)
        github = RecordingGitHub([repository], automation_private=False)
        github.automation_is_private = mock.Mock(side_effect=AssertionError("Unexpected self-repository probe"))
        with mock.patch.dict(
            os.environ,
            {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "ieasybooks/gitlab-sync"},
        ):
            results = self.run_sync([repository], [project(repository, visibility="public")], github=github)
        self.assertEqual([result.status for result in results], ["synced"])
        self.assertEqual(self.gitlab.values[0]["visibility"], "private")
        self.assertEqual(len(self.factory_calls), 1)
        github.automation_is_private.assert_not_called()
        self.assertNotIn(repository.full_name, self.output.getvalue())

    def test_lost_publication_response_triggers_an_actual_private_rollback_request(self):
        repository = source()
        server_visibility = {"value": "private"}
        requests = []

        def request(fake_api, method, path, *, body):
            requests.append((method, path, body.copy()))
            server_visibility["value"] = body["visibility"]
            if body["visibility"] == "public":
                raise SyncError("Connection lost after GitLab accepted publication")
            return {**fake_api.values[0], "visibility": "private"}

        with mock.patch.object(RecordingGitLab, "set_visibility", GitLabAPI.set_visibility):
            with mock.patch.object(RecordingGitLab, "request", request, create=True):
                results = self.run_sync([repository], [project(repository)])
        self.assertEqual([result.status for result in results], ["failed"])
        self.assertEqual(
            requests,
            [
                ("PUT", "/projects/50", {"visibility": "public"}),
                ("PUT", "/projects/50", {"visibility": "private"}),
            ],
        )
        self.assertEqual(server_visibility["value"], "private")
        self.assertEqual(self.gitlab.values[0]["visibility"], "private")


if __name__ == "__main__":
    unittest.main()
