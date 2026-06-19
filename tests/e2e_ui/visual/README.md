# UI diff snapshot tests

A single visual-regression baseline of the default empty view (`/`) — the open
left sidebar plus the `NewChatLandingScreen` ("What should we do?") hero and
composer, captured full-viewport at 1280×800 — gated in CI.

The landing's data calls (agent catalog, host list, session list, filesystem)
are stubbed via `page.route` with fixed fixtures, so the rendered view is a pure
function of the committed bundle and needs no element masking. `live_server`
still serves the SPA bundle; only `/v1/info` / `/v1/me` reach the real (and
deterministic) server.

- Test: [`test_landing_snapshot.py`](test_landing_snapshot.py)
- Baseline (committed): `snapshots/test_landing_snapshot/test_empty_landing_matches_baseline/test_empty_landing_matches_baseline[chromium][linux].png`
- Gate workflow: [`.github/workflows/ui-snapshot.yml`](../../../.github/workflows/ui-snapshot.yml)
- Plugin: [`pytest-playwright-visual-snapshot`](https://github.com/iloveitaly/pytest-playwright-visual-snapshot)

## Why CI is the only renderer

Screenshots differ across operating systems (font rasterizer, hinting,
anti-aliasing), and no diff threshold can reconcile two rendering engines. So we
never compare across OSes: the baseline and the PR comparison are both produced
on a **pinned `ubuntu-24.04`** GitHub runner. That is the single source of
truth. You do not need Docker or a Linux machine locally.

The test is marked `@pytest.mark.visual`; the main e2e-ui suite (unpinned
`ubuntu-latest`) excludes it via `-m "not visual"`. Only `ui-snapshot.yml` runs
it.

## How the gate behaves

- On every PR, `ui-snapshot.yml` renders the default `/` view and compares it to
  the committed baseline. Any pixel difference fails the check.
- **Every run (pass or fail)** uploads one artifact and links it in the job
  summary, so the screenshots are always one click away:
  - `ui-snapshot-<run_id>` — this run's render (`snapshots/`); on a mismatch
    `snapshot_failures/` also carries the `expected_` (baseline), `actual_`
    (current) and `diff_` PNGs. That single artifact is baseline + current +
    diff.
- The baseline is **never** changed by the compare gate. The only ways to change
  it are the update flows below.

## Updating the baseline (when a UI change is intentional)

### Same-repo branch — label the PR (recommended)

1. Push your branch and open the PR.
2. Add the **`update-ui-snapshot`** label.
   [`ui-snapshot-update.yml`](../../../.github/workflows/ui-snapshot-update.yml)
   regenerates the baseline with `--update-snapshots` on the same pinned runner
   and commits the new PNG back to your branch, then removes the label and
   comments the result.
3. **Review the committed PNG** in the bot's commit.
4. The bot pushes with the `OMNIGENT_BOT_APP` token, so the push re-fires the
   PR's checks automatically — no manual re-run. (If the App isn't configured it
   falls back to `GITHUB_TOKEN`, which won't re-trigger CI; the bot's comment
   says so and you push any commit to re-run.)

This works for **same-repo branches only** — Actions tokens can't push to a fork.

### Fork PR

CI can't push to a fork branch, and you can't regenerate locally (cross-OS
rendering). But the compare run already rendered your change on the canonical
runner, so:

1. On the failing **UI Snapshot** check, open the run and download the
   `ui-snapshot-<run_id>` artifact (also linked in the job summary).
2. Take `snapshot_failures/.../actual_*.png` — that's your change rendered on
   `ubuntu-24.04` — and **review it**.
3. Commit it over the baseline at the path above and push. Your push re-runs all
   checks; the compare job now passes.

When the check fails on a fork PR,
[`ui-snapshot-fork-help.yml`](../../../.github/workflows/ui-snapshot-fork-help.yml)
posts these same steps as a PR comment (it runs as `workflow_run` so it can
comment without ever executing fork code).

### Workflow dispatch (non-PR branches)

1. Push your branch.
2. GitHub → Actions → **UI Snapshot** → **Run workflow**, set `ref` to your
   branch (CLI: `gh workflow run ui-snapshot.yml -f ref=<your-branch>`). It runs
   with `--update-snapshots` (intentionally fails); the regenerated PNG is in the
   `ui-snapshot-<run_id>` artifact.
3. Download the artifact, **review the image**, and copy the PNG over the
   committed baseline at the path above.
4. Commit the updated baseline and push. The PR compare job now passes.

## Running locally (debugging only — do not commit the result)

You can exercise the test locally, but a baseline rendered on any machine other
than the CI runner will not match it, so never commit a locally generated PNG.

```bash
uv sync --extra all --extra dev
uv run playwright install --with-deps chromium
cd ap-web && npm ci --legacy-peer-deps && npm run build && cd ..
# First run with no baseline creates one (and fails); subsequent runs compare:
uv run pytest tests/e2e_ui/visual -m visual --ui-skip-build
```
