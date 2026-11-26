# github-actions Workflow Templates

This repository contains central workflows to be used in our Ecosystem infrastructure.

## Strix security scan (`.github/workflows/strix.yaml`)

Reusable AI security scan ([Strix](https://github.com/usestrix/strix)), called from a short
workflow in each repository ([templates/strix-caller.yml](templates/strix-caller.yml)).

**When it runs**

- A pull request is scanned once when it is **approved**, or on demand by adding the
  **`strix-scan`** label (removed again after the run, so it can be re-added) or with
  *Run workflow* and a PR number. New commits after a scan are not scanned automatically;
  re-approve or re-add the label.
- The default branch is scanned **nightly**, covering only the commits since the previous
  nightly scan. Results are comments on the `strix-nightly` tracking issue.

**What it costs and what it skips**

- Only the changed files are scanned (`--scope-mode diff`), `quick` mode, at most
  `$5` (`max-budget`) and `150` turns per agent. Strix is interrupted after 35 minutes
  (`scan-timeout-minutes`, inside the 45-minute job limit), so a slow LLM still yields a
  report with the findings so far, marked *incomplete*; it does not count as scanned.
- The budget is enforced from the cost the LiteLLM proxy reports per request, so the
  proxy's model entries need `input_cost_per_token`/`output_cost_per_token`. A bare alias
  in `LITELLM_MODEL` (e.g. `recommended`) is called as `litellm_proxy/<alias>` and without
  streaming: Strix cannot price a proxy alias itself, and LiteLLM drops the proxy's cost
  from streamed replies, which left `--max-budget` at $0 and never enforced.
- No scan at all for: fork PRs (never get secrets or the self-hosted runners), requests
  (approval, label, manual run) from an account without *write* access to the repository
  (on public repositories anyone can approve), changes
  touching only `skip-paths` (docs, `*.md`, `*.rst`), and a PR commit that already has a
  completed scan (recorded in the PR comment; a scanner error does not count).
- The scan runs on `ubuntu-dind` (at most 5 runners for the org), so nightly times are
  staggered per repository.

**Result**

- One check, **`Strix scan`** (the scan job): green, or red for findings at or above
  `fail-on` (default `high`), a scan stopped at the time limit, or a scanner error.
  Severity counts are in its job summary.
- One PR comment, updated in place, saying which of the three a red check is (a timeout
  or scanner error is not a finding: re-run). On **public** repositories only counts are published;
  the full report is kept only as an artifact encrypted with `STRIX_REPORT_KEY`
  (`openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass env:STRIX_REPORT_KEY -in strix-report.tar.gz.enc | tar -xz`).
  Without that secret, public repositories keep no report. Private repositories keep the
  report as a plain artifact for 14 days.
- Strix telemetry is disabled (`STRIX_TELEMETRY=0`); the checkout carries no token.

**Secrets per repository** (organisation secrets do not reach private repositories on
the current plan): `LITELLM_MODEL`, `LITELLM_KEY`, `LITELLM_API_URL`, and
`STRIX_REPORT_KEY` for public repositories.

**Rollout**: `tools/rollout-strix.sh` lists the active repositories (dry run), and with
`--apply` opens one PR per repository adding `.github/workflows/strix.yml`, creates the
labels and, with `--set-secrets`, sets the secrets from the environment. It also removes
the old per-push `ai-security-scan.yml`. Callers are pinned to an exact release (`--ref`,
default `v1.2.0`), not to `v1`; re-run with `--ref vX.Y.Z` to roll out a newer one.

**Bumping Strix**: change `strix-version` and `strix-sha256` together; take the sha256 from
the release asset digest (`gh api repos/usestrix/strix/releases/tags/vX.Y.Z`) and confirm
it with `sha256sum` on the downloaded archive.

## Help Center redirections (`.github/actions/hc-redirections`)

Composite action that regenerates a documentation repository's Help Center redirect
maps from its merged content and opens the pull request in
`opentelekomcloud-infra/system-config`.

**When it runs**: `push` to `main` of the calling repository, i.e. after a merge. The
merged content is the source, so a redirect is only created for a page that is really
gone from `main`.

**What it does**: checks out the caller, mints a GitHub App installation token (fallback:
a PAT), clones `system-config` and the metadata repository, runs
`tools/generate_hc_redirections.py` from system-config `main` with
`-g github -e public -r <owner>/<repo> -c --no-pull --pr`, and lets the generator push
the branch and open the pull request (its body is filled from system-config's
`.github/pull_request_template.md`). A repository without documents for the requested
edition ends green with a `::notice::` instead of a failure.

**Caller**: one short workflow per docs repository, next to the doc builds. It runs
after a merge, so the redirects describe the content of `main`:

```yaml
name: Generate Help Center redirections
on:
  push:
    branches: [main]
permissions:
  contents: read
jobs:
  redirections:
    runs-on: ubuntu-internal
    container:
      image: catthehacker/ubuntu:act-24.04
    steps:
      - uses: actions/checkout@v4
      - uses: opentelekomcloud-infra/github-actions/.github/actions/hc-redirections@main
        with:
          gitea_token: ${{ secrets.GITEA_TOKEN }}
          app_id: ${{ secrets.SYSTEM_CONFIG_APP_ID }}
          app_private_key: ${{ secrets.SYSTEM_CONFIG_APP_PRIVATE_KEY }}
          github_token: ${{ secrets.SYSTEM_CONFIG_TOKEN }}
```

Use `@main` for now: `v1` still points at the previous release (`v1.1.6`) and its
tree does not contain this action, so `@v1` fails with "Can't find 'action.yml'".
Move the caller to `@v1` once a release that includes it is cut and the major tag
is moved (see "Update Major Tag version to current tag" below).

**Inputs**: `git_hosting` (default `github`), `environment` (default `public`),
`repository` (default: the calling repository),
`system_config_repo` (default `opentelekomcloud-infra/system-config`),
`metadata_repo`, `gitea_token` (read access to the metadata repository, required),
`app_id` + `app_private_key` (preferred) or `github_token` (fallback) with Contents
and Pull requests: write on system-config, `python_version`.

**Credentials on disk**: the App installation token and the Gitea token are handed
to git through `GIT_CONFIG_*` environment variables, scoped to the system-config and
metadata repository URLs, and never written to `$HOME/.gitconfig` — a self-hosted
runner whose home survives the job would otherwise keep them in plaintext.

**Secrets per repository**: `GITEA_TOKEN` (metadata read), `SYSTEM_CONFIG_APP_ID` +
`SYSTEM_CONFIG_APP_PRIVATE_KEY` (or `SYSTEM_CONFIG_TOKEN`).

## Update Major Tag version to current tag

It is necessary to update latest Major version to current tag, to keep Github actions working with the latest update.

```bash
git tag -f v1 v1.0.2
git push origin -f v1
