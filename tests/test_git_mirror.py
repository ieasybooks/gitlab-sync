"""Exercise the actual Git transport against disposable local repositories."""

from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

from scripts.sync import GitMirror, SyncError


@unittest.skipUnless(shutil.which("git"), "Git is required")
class GitMirrorIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.destination = self.root / "destination.git"
        self.environment = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
        self.git("init", "--initial-branch=main", str(self.source))
        self.git("init", "--bare", "--initial-branch=main", str(self.destination))
        self.git("config", "user.name", "Sync test", cwd=self.source)
        self.git("config", "user.email", "sync-test@example.invalid", cwd=self.source)
        self.counter = 0

    def git(self, *arguments, cwd=None):
        completed = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            env=self.environment,
            capture_output=True,
            text=True,
            check=True,
        )
        return completed.stdout.strip()

    def commit(self, message):
        self.counter += 1
        (self.source / "content.txt").write_text(
            f"{message}\n{self.counter}\n", encoding="utf-8"
        )
        self.git("add", "content.txt", cwd=self.source)
        self.git("commit", "--quiet", "-m", message, cwd=self.source)
        return self.git("rev-parse", "HEAD", cwd=self.source)

    def all_refs(self, repository):
        output = self.git(
            "for-each-ref", "--format=%(refname) %(objectname)", cwd=repository
        )
        return dict(line.split(" ", 1) for line in output.splitlines())

    def mirror(self):
        return GitMirror(str(self.source), str(self.destination))

    def test_copies_branches_and_annotated_tags_but_not_github_internal_refs(self):
        first = self.commit("Initial version")
        self.git("tag", "-a", "v1.0", "-m", "First release", cwd=self.source)
        self.git("checkout", "--quiet", "-b", "feature", cwd=self.source)
        feature = self.commit("Feature work")
        self.git("tag", "preview", cwd=self.source)
        self.git("checkout", "--quiet", "main", cwd=self.source)
        self.git("update-ref", "refs/pull/17/head", feature, cwd=self.source)

        with self.mirror() as mirror:
            self.assertEqual(
                set(mirror.refs),
                {
                    "refs/heads/main",
                    "refs/heads/feature",
                    "refs/tags/v1.0",
                    "refs/tags/preview",
                },
            )
            self.assertEqual(mirror.refs["refs/heads/main"], first)
            mirror.push_refs()
            mirror.verify()

        expected = {
            name: sha
            for name, sha in self.all_refs(self.source).items()
            if name.startswith(("refs/heads/", "refs/tags/"))
        }
        self.assertEqual(self.all_refs(self.destination), expected)
        self.assertNotEqual(
            expected["refs/tags/v1.0"],
            first,
            "The annotated tag object, not only its peeled commit, must survive",
        )

    def test_propagates_force_pushes_and_deletions_without_touching_gitlab_refs(self):
        first = self.commit("First commit")
        second = self.commit("Second commit")
        self.git("branch", "obsolete", first, cwd=self.source)
        self.git("tag", "obsolete-tag", first, cwd=self.source)
        self.git("tag", "moving-tag", second, cwd=self.source)
        with self.mirror() as mirror:
            mirror.push_refs()

        self.git(
            "update-ref", "refs/merge-requests/9/head", second, cwd=self.destination
        )
        self.git("reset", "--hard", first, cwd=self.source)
        self.git("branch", "-D", "obsolete", cwd=self.source)
        self.git("tag", "-d", "obsolete-tag", cwd=self.source)
        self.git("tag", "--force", "moving-tag", first, cwd=self.source)
        self.git("branch", "replacement", first, cwd=self.source)

        with self.mirror() as mirror:
            mirror.push_refs(prune=False)
            mirror.push_refs(prune=True)
            mirror.verify()

        actual = self.all_refs(self.destination)
        self.assertEqual(actual["refs/heads/main"], first)
        self.assertEqual(actual["refs/tags/moving-tag"], first)
        self.assertNotIn("refs/heads/obsolete", actual)
        self.assertNotIn("refs/tags/obsolete-tag", actual)
        self.assertEqual(actual["refs/merge-requests/9/head"], second)
        self.assertEqual(
            {name: sha for name, sha in actual.items() if not name.startswith("refs/merge-requests/")},
            self.all_refs(self.source),
        )

    def test_verification_detects_an_extra_destination_branch(self):
        tip = self.commit("Initial version")
        with self.mirror() as mirror:
            mirror.push_refs()
            self.git("branch", "unwanted", tip, cwd=self.destination)
            with self.assertRaises(SyncError):
                mirror.verify()

    def test_single_branch_without_tags_can_be_pushed(self):
        tip = self.commit("Only main exists")
        with self.mirror() as mirror:
            mirror.push_refs(prune=True)
            mirror.verify()
            self.assertEqual(mirror.destination_refs(), {"refs/heads/main": tip})

    def test_empty_source_has_no_phantom_default_branch(self):
        with self.mirror() as mirror:
            self.assertEqual(mirror.refs, {})
            self.assertEqual(mirror.destination_refs(), {})
            mirror.verify()

    def test_tag_only_source_can_be_mirrored_without_inventing_a_branch(self):
        self.commit("Archived history")
        self.git("tag", "-a", "archive", "-m", "Archived release", cwd=self.source)
        tag = self.git("rev-parse", "refs/tags/archive", cwd=self.source)
        self.git("update-ref", "-d", "refs/heads/main", cwd=self.source)
        with self.mirror() as mirror:
            self.assertEqual(mirror.refs, {"refs/tags/archive": tag})
            mirror.push_refs(prune=True)
            mirror.verify()
        self.assertEqual(self.all_refs(self.destination), {"refs/tags/archive": tag})


if __name__ == "__main__":
    unittest.main()
