# Release feature flags

Omnigent release features are deployment-wide, temporary rollout switches. They
are not authorization controls or user preferences.

## Configuration

Set the comma-separated `OMNIGENT_FEATURES` environment variable and restart or
redeploy the server:

```bash
OMNIGENT_FEATURES=usage_page,harness_install
```

Unset or empty uses the built-in defaults. The configured names add to those
defaults; omitting a default-enabled feature does not disable it. Unknown names
fail startup. Harnesses settings is always available in current clients; the
server enables `harness_settings_ui` by default for older clients.
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

## Rollout and rollback

These steps apply to optional features that are off by default.

1. Deploy an immutable image with the feature absent from `OMNIGENT_FEATURES`.
2. Enable it on one deployment, consistently across all replicas.
3. Verify `GET /v1/info`, then reload and exercise the gated UI.
4. Expand by deployment cohort.
5. Roll back by removing the key and redeploying the same image.
