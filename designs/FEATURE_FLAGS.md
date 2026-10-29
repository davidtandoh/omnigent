# Release feature flags

Omnigent release features are deployment-wide, temporary rollout switches. They
are not authorization controls or user preferences.

## Configuration

Set the comma-separated `OMNIGENT_FEATURES` environment variable and restart or
redeploy the server:

```bash
OMNIGENT_FEATURES=usage_page,harness_install
```

Unset or empty uses the defaults below. A name enables a feature; `-name`
disables it. Explicit disables win regardless of order, so appending
`-claude_osc_status` is a kill switch even if `claude_osc_status` is also present.
Unknown names, including unknown negative overrides, fail startup.
The former `OMNIGENT_HARNESS_INSTALL_ENABLED` switch is rejected with a
migration hint; use `OMNIGENT_FEATURES=harness_install` instead. The server resolves the set once at startup and publishes frontend-visible
values in `GET /v1/info` under `features`. Users must reload the web app after a
flag change because server capabilities are cached at page boot.

## Inventory

See `FEATURE_DEFINITIONS` in
[`omnigent/server/feature_flags.py`](../omnigent/server/feature_flags.py) for
the authoritative keys, purposes, owners, and review releases. `FeatureFlags`
in that module defines the enabled defaults. Inspect `GET /v1/info` for the
frontend-visible values resolved by a running server.

At the review release, each flag must be removed by making the feature
unconditional, removing the feature, or moving a genuinely permanent operator
policy into normal server configuration.

Runner features use the same defaults and overrides, resolved once at resource-registry
startup. The host forwards `OMNIGENT_FEATURES` to its runners, including through
the CLI's background daemon. Changing a remote server's environment alone does
not change a separate host's runners. Restart the host and affected runners
after changing their feature set; existing terminals keep their launch
configuration. See the [native-harness feature map](../feature-map/native-harnesses.md#driving-it-with-the-repro-environment)
for Claude working-status acceptance and kill-switch verification.

## Rollout and rollback

1. Deploy an immutable image with `-feature_name` in `OMNIGENT_FEATURES` to
   explicitly disable the feature, including one that defaults on.
2. Remove that disable and add `feature_name` on one deployment, consistently
   across all replicas.
3. Verify `GET /v1/info`, then reload and exercise the gated UI.
4. Expand by deployment cohort.
5. Roll back with `-feature_name` and redeploy the same image. For runner
   features, apply the override to the host/runner environment, restart those
   processes, and launch fresh terminals.

The built-in server entrypoints do not yet supply the required execution/recovery
hooks. Enabling `automations_v2` there fails startup, even if only UI changes are
needed. A deployment wrapper must provide the hooks through `create_app()`.

Before enabling `automations_v2`, deploy the schema and updated code to every server replica. Older servers cannot read `starting` runs or preserve their cleanup records when deleting tasks.

Even with the flag off, an interrupted deletion can leave a hidden task row for
cleanup. During a mixed-version deployment, older replicas can still show and
edit that row until all replicas are upgraded.

Before disabling `automations_v2`, stop new dispatch and let existing startups
finish or complete recovery, including any required resource cleanup. Disabling
the flag stops startup recovery and deleted-task sweeps; clearing scheduling
cursors does not resolve unfinished runs or remove their retained task rows.
