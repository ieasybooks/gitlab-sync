# GitHub organization → GitLab sync

Keep the repositories in `ieasybooks` mirrored into one GitLab.com group, using GitLab Free. One scheduled GitHub Actions workflow discovers repositories, creates missing GitLab projects, and copies their branches, tags, commit history, and Git LFS objects.

GitHub is the source of truth. Work in the GitHub repositories; treat the GitLab projects as managed copies. New mirrors preserve source visibility: public GitHub repositories become public GitLab projects; private or internal repositories stay private. A mirror created or used for private sources needs a fresh replacement before publication, as described below. GitLab CI/CD is disabled on all mirrors.

## Setup

### 1. Choose a GitLab group

Create or choose a dedicated **Public** group on **GitLab.com** for the mirrors. A public GitLab group can contain both public and private projects; a private group cannot contain public mirrors.

If the destination group is currently private, open **Settings → General → Naming, description, visibility**, set **Visibility level** to **Public**, and select **Save changes**. The automation reports visibility restrictions and does not change group settings for you.

On the group overview, select **Actions → Copy Group ID** to copy its numeric ID.

The GitLab account used for automation must be an **Owner of the destination group** so it can create projects and change their visibility. If you created the group, your account already has this role. GitLab Premium and its built-in pull mirroring feature are not required.

### 2. Create a GitLab token

Create a GitLab [personal access token](https://docs.gitlab.com/user/profile/personal_access_tokens/) with the **`api` scope**. This is needed for project creation and configuration as well as Git pushes. A token with only `write_repository` cannot perform the API setup.

Choose an expiration date and plan to replace the stored secret before that date. A dedicated account limited to the destination group keeps the token's access smaller. GitLab.com group access tokens require a paid plan; this setup uses a personal access token on Free.

### 3. Configure this GitHub repository

Open **Settings → Secrets and variables → Actions** in this repository.

Add these repository **secrets**:

| Secret | Required | Value |
| --- | --- | --- |
| `GITLAB_TOKEN` | Yes | The GitLab personal access token from step 2. |
| `GH_READ_TOKEN` | For private or internal GitHub repositories | A GitHub token with read access to the repositories to mirror. Omit it to use the workflow's built-in token for public source repositories. |
| `EXCLUDE_REPOS` | No | Comma-separated source repository names or `owner/name` entries to skip, for example `old-experiment,ieasybooks/large-dataset`. Matching is case-insensitive. Store this as a secret because it can contain private repository names. |

Add these repository **variables** on the **Variables** tab:

| Variable | Required | Value |
| --- | --- | --- |
| `GITLAB_GROUP_ID` | Yes | The numeric ID of the GitLab destination group. |
| `SYNC_ENABLED` | To enable scheduled runs | Set to `true` after validating a manual sync. Leave unset while setting up. |
| `SOURCE_ORG` | No | Defaults to `ieasybooks`. |

**This GitHub automation repository can remain public, including when it mirrors private source repositories.** Sync logs and summaries use aliases such as `repo-001`; see [Logs and local diagnosis](#logs-and-local-diagnosis) for what remains visible.

For private or internal sources, create a GitHub fine-grained personal access token with:

- **Resource owner:** `ieasybooks`, or the organization selected by `SOURCE_ORG`.
- **Repository access:** all repositories you want mirrored. Choose **All repositories** to include newly created repositories automatically.
- **Repository permissions:** **Contents: read-only** and **Metadata: read-only**.

Complete any organization approval or SSO requirements GitHub presents, and save the token as `GH_READ_TOKEN`. Without this optional secret, other private repositories are not discoverable through the built-in `GITHUB_TOKEN`.

Visibility preservation needs no additional secret or variable. Keep any existing `GITLAB_TOKEN` and `GITLAB_GROUP_ID` values when using the same destination group.

`SOURCE_ORG` and `GITLAB_GROUP_ID` are public configuration and can appear in GitHub's workflow environment display. Repository exclusions belong in a secret. If you previously configured `EXCLUDE_REPOS` as a **Variable**, copy its value into a repository **Secret** with the same name, then delete the old variable **before starting another workflow run**. Keep debug logging disabled during this migration: runner diagnostics can serialize plaintext variables before any job step, and changing configuration does not remove existing logs. The workflow also refuses to run while the legacy variable exists, even if the secret is present, so the old exclusion list cannot be silently ignored.

### 4. Preview the destination

After the implementation PR is merged into `main`:

1. Open **Actions → Sync to GitLab → Run workflow**.
2. Select `main` and leave **dry_run** checked.
3. Confirm that the configured `GITLAB_GROUP_ID` selects the intended destination. Review the run's counts, repository aliases, create/update actions, and target visibility. Use a local named dry run if you need to inspect the repository names.

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
| Source visibility | Public sources become public mirrors when created fresh or known to have received only public source data; private and internal sources become private mirrors. |
| New source repositories | Create a private project first, then apply source visibility after a successful sync and a fresh source check. |
| GitHub repository renamed | Reuse the project tracked by the source repository ID; retain its existing GitLab path. |
| Source repository deleted, transferred away, excluded, or no longer accessible | Retain the managed GitLab project and make it private. The automation never deletes whole projects. |
| Issues, pull requests, reviews, comments, release notes/assets, wikis, packages, and organization settings | Not synchronized. |
| GitHub Actions workflow files | Copied as repository files. They do not execute on GitLab; destination GitLab CI/CD is disabled. |

Only branch and tag refs are synchronized. GitHub's internal pull-request refs are not pushed to GitLab. Submodule entries are copied as part of Git history; their URLs are not rewritten, so mirroring a parent repository does not make its submodules use GitLab automatically.

Pruning a branch or tag does not securely erase previously transferred Git or LFS objects.

The script compares advertised branch/tag refs and the default branch before downloading repository data. If they already match, it reports `unchanged` and skips Git/LFS transfers. A changed repository is fetched into a fresh temporary bare repository; no source files are checked out or executed. LFS objects are uploaded before refs, and successful transfers are checked against the fetched source snapshot. An `unchanged` result is a ref comparison, not a full LFS integrity audit.

Public source Git and LFS transfers use anonymous access; GitHub API discovery still uses the configured read token.

### Managed project behavior

The automation marks each created project's description with its GitHub source identity. It will only update projects it recognizes as its own mirrors, and it refuses to overwrite an unrelated existing project with the same path. Keep that identity marker in the destination description.

GitLab has stricter project-name and URL-path rules than GitHub. Compatible source names keep their original destination name. Incompatible or reserved names, such as `.github`, use `github-repo-<GitHub repository ID>` for both the GitLab name and path. That pattern is reserved for generated destinations, so a source literally named `github-repo-123` also maps by its own ID instead of colliding with another mirror. The description retains the original GitHub URL. Existing managed projects continue to be found by source identity and keep their current paths, including after source renames.

New mirrors start private. Changed public mirrors are temporarily made private while data is transferred, then published only after a successful sync and a fresh GitHub identity/visibility check. Unchanged public mirrors stay public. Private and internal sources remain private, and a per-repository failure keeps or returns the managed mirror to private. Excluded or undiscovered managed mirrors are also retained as private projects.

The project description permanently records private source history before any private data transfer. Changing the source from private to public does not publish that existing mirror, even if its current refs match. Older mirrors without a visibility history marker are treated the same way. These cases remain private and report that a fresh destination is required, because GitLab can retain objects that are no longer referenced by current branches or tags.

To replace such a mirror, pause the schedule and wait for any active run to finish. Keep the old project **Private**, rename its path to an unused archival name, and remove both the `[github-sync:v1 ...]` and `[github-sync:visibility-v1 ...]` marker lines from its description. You may also archive it. Leave the source-named destination path empty and run the sync so it creates a new managed project. Re-enable the schedule after verifying the replacement. The retired project and its stored history remain private.

Visibility updates follow hourly runs, not instantly; a failed or unrun job cannot guarantee GitLab reflects the latest GitHub privacy setting.

GitLab CI/CD stays disabled on managed mirrors. When applying Git changes, project-level protected branch and tag rules are removed on those mirrors so source force pushes and ref deletions can be replicated. Group-level restrictions are not changed; an inherited restriction that blocks synchronization is reported as a failure.

An empty new repository gets an empty mirror. If a previously populated source unexpectedly advertises no branches or tags, the script keeps the populated destination and reports a failure for review instead of erasing its refs.

New tag-only repositories can be mirrored. If an existing mirror has branches and its source becomes tag-only, the script reports a failure and retains the destination default branch for manual review.

Do not use a mirror for independent GitLab development: its branches and tags can be overwritten or deleted to match GitHub. This is an up-to-date second copy, not a versioned backup against unwanted source changes.

## Operation and troubleshooting

- **Scheduling:** GitHub Actions schedules can be delayed or dropped during high load. Scheduled workflows in a public repository are also disabled after 60 days without repository activity. Monitor the workflow and re-enable it if GitHub disables it; this repository does not create automatic heartbeat commits.
- **Concurrency:** One sync run executes at a time. Starting another run does not cancel the active transfer.
- **Failures:** Check the failed Actions run for the affected repository alias, error code, and operation stage. A repository failure is recorded while other repositories continue; any failure makes the run unsuccessful. Use local named output when you need to identify a repository, then resolve the cause and rerun the workflow; existing managed projects are reused. Configure your GitHub Actions notification preferences to receive workflow failure notifications.
- **Authentication errors:** Check token expiration, the selected GitHub repositories, any organization token approvals, and the GitLab account's access to the group. Replace expired tokens in the corresponding GitHub secret.
- **Existing project collision:** Use a clean destination group or rename the unrelated GitLab project. The script deliberately does not adopt unrelated projects.
- **Public visibility rejected:** Confirm that the destination group is Public and the token's account can manage project visibility. The script reports restrictions and keeps the affected mirror private rather than changing group-wide settings.
- **Protection or push rejection:** Check inherited GitLab group restrictions and destination storage limits. The automation will not change group-wide policies.
- **Ref name conflict:** Renaming a branch from `foo` to `foo/bar` can make the first push fail while the old destination ref still exists. Remove only the conflicting obsolete ref in the dedicated mirror, then rerun; the script does not perform broad cleanup to resolve these conflicts.
- **Disable the schedule:** Remove `SYNC_ENABLED` or change it to `false`. This does not delete any mirrored data.
- **Usage:** GitHub Actions runner usage and GitLab storage limits still apply. Unchanged repositories are inexpensive to check. A changed repository downloads its reachable history again into a temporary clone, so large histories or substantial Git LFS data can make those runs slower and more costly.

The sync does not require paid GitLab pull mirroring or a workflow file in each source repository. It runs using Python's standard library, Git, and Git LFS. Official GitHub Actions are pinned to commit SHAs, with Dependabot configured to propose updates.

### Logs and local diagnosis

By default, each repository is represented by an alias such as `repo-001`. These aliases are assigned per run and are not stable identifiers. Default script output and all job summaries contain fixed status/error messages, visibility, and counts rather than private repository names, repository IDs, paths, URLs, refs, content, credentials, raw API/Git errors, or tracebacks. The workflow does not upload private repository data as artifacts or cache it between runs.

Failures include a fixed operation label when known, for example `GITLAB_HTTP_400 at project_create` or `GIT_FAILED at refs_push`. Git diagnostics distinguish source/destination ref reads, source fetch, LFS inspection/download/upload, ref push/prune, and final verification. API diagnostics distinguish project creation/preparation, visibility, protection, and default-branch updates. These labels identify the failed operation; they do not reveal the remote's raw rejection message or establish its exact cause. No command arguments or remote payloads are printed.

Public logs still reveal counts, statuses, visibility states, and timing. GitHub also displays its own run metadata and the public organization/group configuration described above. Aliased output does not conceal those facts.

Keep Actions debug logging disabled. The workflow stops when runner debug mode is enabled, before the steps that use sync credentials or exclusions. The script also rejects Actions runs with `RUNNER_DEBUG=1`, `ACTIONS_STEP_DEBUG=true`, or `ACTIONS_RUNNER_DEBUG=true`.

For repository names during local diagnosis, use `--show-repository-names` outside GitHub Actions. This flag is rejected in Actions, even for a private automation repository. It enables names in terminal output while retaining fixed, sanitized messages; any job summary still uses aliases.

## Local use and tests

Install Python 3.12+, Git, and Git LFS. Supply `GITLAB_TOKEN` and, when needed, `GH_TOKEN` through your terminal environment or secret manager; tokens must not be committed to the repository. Set `GITLAB_GROUP_ID`, and optionally `SOURCE_ORG` and `EXCLUDE_REPOS`, in the same environment.

Preview:

```sh
python3 scripts/sync.py --dry-run
```

Preview with repository names on your own machine:

```sh
python3 scripts/sync.py --dry-run --show-repository-names
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
- [GitLab project and group visibility](https://docs.gitlab.com/user/public_access/)
- [GitLab personal access tokens](https://docs.gitlab.com/user/profile/personal_access_tokens/)
- [GitLab access token scopes](https://docs.gitlab.com/security/tokens/access_token_scopes/)
- [GitLab group access token availability](https://docs.gitlab.com/user/group/settings/group_access_tokens/)
