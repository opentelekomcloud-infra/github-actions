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

## Update Major Tag version to current tag

It is necessary to update latest Major version to current tag, to keep Github actions working with the latest update.

```bash
git tag -f v1 v1.0.2
git push origin -f v1
