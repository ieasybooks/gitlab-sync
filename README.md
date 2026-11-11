# GitHub organization → GitLab sync

Keep the repositories in `ieasybooks` mirrored into one GitLab.com group, using GitLab Free. One scheduled GitHub Actions workflow discovers repositories, creates missing GitLab projects, and copies their branches, tags, commit history, and Git LFS objects.

GitHub is the source of truth. Work in the GitHub repositories; treat the GitLab projects as managed copies. All destination projects are private, and GitLab CI/CD is disabled on them.

## Setup

### 1. Choose a GitLab group

Create or choose a group on **GitLab.com** for the mirrors. Use a dedicated group so it is clear which projects the automation manages. Copy its numeric **Group ID** from the group's details/settings page.

The GitLab account used for automation must be able to create projects in this group and manage those projects. An Owner of a dedicated destination group satisfies these requirements. GitLab Premium and its built-in pull mirroring feature are not required.

### 2. Create a GitLab token

Create a GitLab [personal access token](https://docs.gitlab.com/user/profile/personal_access_tokens/) with the **`api` scope**. This is needed for project creation and configuration as well as Git pushes. A token with only `write_repository` cannot perform the API setup.

Choose an expiration date and plan to replace the stored secret before that date. A dedicated account limited to the destination group keeps the token's access smaller. GitLab.com group access tokens require a paid plan; this setup uses a personal access token on Free.

### 3. Configure this GitHub repository

Open **Settings → Secrets and variables → Actions** in this repository.

Add these repository **secrets**:

| Secret | Required | Value |
| --- | --- | --- |
| `GITLAB_TOKEN` | Yes | The GitLab personal access token from step 2. |
| `GH_READ_TOKEN` | For private GitHub repositories | A GitHub token with read access to the repositories to mirror. Omit it to use the workflow's built-in token for public source repositories. |

Add these repository **variables** on the **Variables** tab:

| Variable | Required | Value |
| --- | --- | --- |
| `GITLAB_GROUP_ID` | Yes | The numeric ID of the GitLab destination group. |
| `SYNC_ENABLED` | To enable scheduled runs | Set to `true` after validating a manual sync. Leave unset while setting up. |
| `SOURCE_ORG` | No | Defaults to `ieasybooks`. |
| `EXCLUDE_REPOS` | No | Comma-separated source repository names or `owner/name` entries to skip, for example `old-experiment,ieasybooks/large-dataset`. Matching is case-insensitive. |

For private source repositories, **make this automation repository private first**. Actions logs and summaries can identify the mirrored repositories. The script refuses to process private source repositories from a public Actions repository.

Then create a GitHub fine-grained personal access token with:

- **Resource owner:** `ieasybooks`, or the organization selected by `SOURCE_ORG`.
- **Repository access:** all repositories you want mirrored. Choose **All repositories** to include newly created repositories automatically.
- **Repository permissions:** **Contents: read-only** and **Metadata: read-only**.

Complete any organization approval or SSO requirements GitHub presents, and save the token as `GH_READ_TOKEN`. Ensure it can also read this automation repository, which the privacy check inspects. Without this optional secret, other private repositories are not discoverable through the built-in `GITHUB_TOKEN`.

### 4. Preview the destination

After the implementation PR is merged into `main`:

1. Open **Actions → Sync to GitLab → Run workflow**.
2. Select `main` and leave **dry_run** checked.
3. Review the printed destination group path and numeric ID, source count, and repository create/update actions. Confirm they match the intended destination and scope.

A dry run only reads GitHub and GitLab APIs. It does not create projects, change project settings, remove protections, or transfer Git/LFS objects. It therefore cannot verify every permission needed by the eventual write operations.

### 5. Run the first sync and enable the schedule

Run the workflow again with **dry_run unchecked**. Once it succeeds, set the repository variable **`SYNC_ENABLED=true`**.

Scheduled runs are then requested every hour at minute **17**, in UTC. Every run discovers repositories again, so newly created repositories are included when the GitHub credential has access to them. Manual runs remain available regardless of `SYNC_ENABLED`.

## What is synchronized

| Content or behavior | Result |
| --- | --- |
| Source files and full reachable commit history | Copied through Git. |
| Branches and tags | Added and updated; source force pushes and deleted refs are reflected in the managed destination. |
| Default branch | Set to the source default branch before an obsolete destination default branch is removed. |
| Git LFS objects | Fetched from GitHub and pushed to GitLab explicitly. |
| New source repositories | Create a private destination project on the next run. |
| GitHub repository renamed | Reuse the project tracked by the source repository ID; retain its existing GitLab path. |
| Source repository deleted, transferred away, excluded, or no longer accessible | Leave its GitLab project in place. The automation never deletes whole projects. |
| Issues, pull requests, reviews, comments, release notes/assets, wikis, packages, and organization settings | Not synchronized. |
| GitHub Actions workflow files | Copied as repository files. They do not execute on GitLab; destination GitLab CI/CD is disabled. |

Only branch and tag refs are synchronized. GitHub's internal pull-request refs are not pushed to GitLab. Submodule entries are copied as part of Git history; their URLs are not rewritten, so mirroring a parent repository does not make its submodules use GitLab automatically.

The script compares advertised branch/tag refs and the default branch before downloading repository data. If they already match, it reports `unchanged` and skips Git/LFS transfers. A changed repository is fetched into a fresh temporary bare repository; no source files are checked out or executed. LFS objects are uploaded before refs, and successful transfers are checked against the fetched source snapshot. An `unchanged` result is a ref comparison, not a full LFS integrity audit.

### Managed project behavior

The automation marks each created project's description with its GitHub source identity. It will only update projects it recognizes as its own mirrors, and it refuses to overwrite an unrelated existing project with the same path. Keep that identity marker in the destination description.

Managed mirrors stay private with GitLab CI/CD disabled. When applying Git changes, project-level protected branch and tag rules are removed on those mirrors so source force pushes and ref deletions can be replicated. Group-level restrictions are not changed; an inherited restriction that blocks synchronization is reported as a failure.

An empty new repository gets an empty mirror. If a previously populated source unexpectedly advertises no branches or tags, the script keeps the populated destination and reports a failure for review instead of erasing its refs.

New tag-only repositories can be mirrored. If an existing mirror has branches and its source becomes tag-only, the script reports a failure and retains the destination default branch for manual review.

Do not use a mirror for independent GitLab development: its branches and tags can be overwritten or deleted to match GitHub. This is an up-to-date second copy, not a versioned backup against unwanted source changes.

## Operation and troubleshooting

- **Scheduling:** GitHub Actions schedules can be delayed or dropped during high load. Scheduled workflows in a public repository are also disabled after 60 days without repository activity. Monitor the workflow, or use a private automation repository to avoid that specific public-repository inactivity rule.
- **Concurrency:** One sync run executes at a time. Starting another run does not cancel the active transfer.
- **Failures:** Check the failed Actions run for the affected repository and error. A repository failure is recorded while other repositories continue; any failure makes the run unsuccessful. Resolve the cause and rerun the workflow; existing managed projects are reused. Configure your GitHub Actions notification preferences to receive workflow failure notifications.
- **Authentication errors:** Check token expiration, the selected GitHub repositories, any organization token approvals, and the GitLab account's access to the group. Replace expired tokens in the corresponding GitHub secret.
- **Existing project collision:** Use a clean destination group or rename the unrelated GitLab project. The script deliberately does not adopt unrelated projects.
- **Protection or push rejection:** Check inherited GitLab group restrictions and destination storage limits. The automation will not change group-wide policies.
- **Ref name conflict:** Renaming a branch from `foo` to `foo/bar` can make the first push fail while the old destination ref still exists. Remove only the conflicting obsolete ref in the dedicated mirror, then rerun; the script does not perform broad cleanup to resolve these conflicts.
- **Disable the schedule:** Remove `SYNC_ENABLED` or change it to `false`. This does not delete any mirrored data.
- **Usage:** GitHub Actions runner usage and GitLab storage limits still apply. Unchanged repositories are inexpensive to check. A changed repository downloads its reachable history again into a temporary clone, so large histories or substantial Git LFS data can make those runs slower and more costly.

The sync does not require paid GitLab pull mirroring or a workflow file in each source repository. It runs using Python's standard library, Git, and Git LFS. Official GitHub Actions are pinned to commit SHAs, with Dependabot configured to propose updates.

## Local use and tests

Install Python 3.12+, Git, and Git LFS. Supply `GITLAB_TOKEN` and, when needed, `GH_TOKEN` through your terminal environment or secret manager; tokens must not be committed to the repository. Set `GITLAB_GROUP_ID`, and optionally `SOURCE_ORG` and `EXCLUDE_REPOS`, in the same environment.

Preview:

```sh
python3 scripts/sync.py --dry-run
```

Run a sync:

```sh
python3 scripts/sync.py
```

Run checks without production credentials:

```sh
python3 -m compileall -q scripts tests
python3 -m unittest discover -s tests -v
```

The pull-request CI workflow runs the checks without accessing the sync secrets or either production account.

## References

- [GitHub organization repository listing API](https://docs.github.com/en/rest/repos/repos#list-organization-repositories)
- [GitHub Actions token scope](https://docs.github.com/en/actions/concepts/security/github_token)
- [GitHub scheduled workflow behavior](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
- [GitHub repository mirroring and Git LFS](https://docs.github.com/en/repositories/creating-and-managing-repositories/duplicating-a-repository)
- [GitLab project creation API](https://docs.gitlab.com/api/projects/#create-a-project)
- [GitLab personal access tokens](https://docs.gitlab.com/user/profile/personal_access_tokens/)
- [GitLab access token scopes](https://docs.gitlab.com/security/tokens/access_token_scopes/)
- [GitLab group access token availability](https://docs.gitlab.com/user/group/settings/group_access_tokens/)
