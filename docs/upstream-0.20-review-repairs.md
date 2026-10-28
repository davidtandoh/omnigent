# Upstream v0.20 helper conflict resolution

The merge introduced both upstream's `_spawner` pool and the fork's `_executor`
pool in `omnigent/inner/os_env.py`. Requests used both pools, and `close()` shut
down only `_spawner`.

The repair keeps upstream's `_spawner` name and shutdown ownership. The pool
executes the complete serialized helper request, including process creation and
restarts. No second pool is required. This preserves the fork's persistent
spawning thread and allows `close()` to release that thread.

Evidence: `git log f4ab7aec -S_executor -- omnigent/inner/os_env.py` identifies
commit `28c553c72`, which repaired the helper's spawning-thread lifetime.
The runtime-import regression exercises helper startup and restart on a thread
that survives between requests. It now also checks that the thread exits after
`close()`. Linux Bubblewrap coverage remains conditional on that backend.

Use this decision for the PR's `os_env.py` conflict-resolution entry. The outer
executor owns PR updates and publication.

The test repairs remove instruction-source phrase checks. Bundle extraction,
emitted startup prompts, and serialized handoff examples remain covered.
Both agent handoff examples now run through the shared review-cycle validator.
Claude SDK effort forwarding already has executable coverage in
`test_effort_changes_rebuild_client_with_history`. The OpenAI SDK test uses its
existing fake SDK to inspect the agent options passed to the runner.

The hook-installation test executes `just ensure` with command shims because
running the real setup would install dependencies and modify Git hooks. It
requires `just`. The Databricks launcher test reuses the deployment boot
fixture and captures the server at the existing Uvicorn execution boundary.
The neighboring Docker launcher and workspace import-isolation tests remain.

## Focused verification

One focused run on 2026-10-28 used two workers, a 180-second test timeout,
file limit 8192, and `/private/tmp/og-r7-01m61` as its temporary root. The run
selected the ten changed Python test modules plus the existing Claude SDK
module. Result: 423 passed, 5 skipped, 6 failed, and 10 subtests passed.

All added tests passed. The `just ensure` scenario skipped because `just` is
unavailable. Linux Bubblewrap cases skipped on macOS. Six existing deployment
tests failed with `ModuleNotFoundError` for `deploy.docker` or
`deploy.databricks`. These failures did not reach the launcher assertions.
The new Databricks test executes the entrypoint by path and passed. The existing
OpenAI SDK module prepends `tests/` to `sys.path`, and `tests/deploy` is a regular
package while the project deployment directory is a namespace package. This
provides a plausible import-collision explanation; no baseline comparison was
run during this repair phase.

The outer executor should retain these failures in its validation evidence.
For an isolated launcher check, use:

```sh
uv run --no-sync pytest tests/test_tunnel_limits.py tests/deploy/test_databricks_app_sandbox_config.py --reruns 0 --timeout=180 -n 2
```

Install no tools or hooks as part of this review phase. The outer executor owns
the remaining lint, test, publication, and CI gates.
