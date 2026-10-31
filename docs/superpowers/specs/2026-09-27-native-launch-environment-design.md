# Native Launch Environment Design

## Goal

Let a caller explicitly set environment variables for one native terminal
launch. Support Claude, Codex, Kiro, and Antigravity (`agy`) without copying
the caller's ambient environment.

## Boundary

The command-line interface accepts repeatable `--env KEY=VALUE` options. The
client sends only those entries in the runner-launch request. The server keeps
the map transient and includes it in the authorized host launch frame. The
host daemon validates the map and puts one encoded envelope in the new runner's
environment. The runner consumes and removes that envelope before it creates a
terminal.

The four native terminal builders merge the consumed entries into the launch
environment. Codex applies the entries to both its tool-running app-server and
its attached terminal UI. This explicit map has precedence over an inherited
value. For terminal processes, existing `env_unset` rules still remove
protected variables after the merge.

The Claude account check uses the terminal environment builder and shared
`env` wrapper parser before trust seeding or terminal creation. It does not use
the ambient readiness cache. See the [native launch usage](../../../README.md#2-start-your-first-agent)
for the account selection contract and probe failure policy.

For named Claude accounts, the CLI sends the account name with session creation.
The host resolves the recorded name from its own config and adds
`CLAUDE_CONFIG_DIR` to the runner's
launch environment before runner reuse and custody checks. The CLI also resolves
the folder locally for settings and transcript reads; that folder stays local.
The runner checks the session account and the resolved agent provider against
its launch environment before sign-in probing or transcript preparation. When
`CLAUDE_CONFIG_DIR` is selected, the runner skips provider credential injection.
Before model probing or terminal creation, the runner uses
`managed_claude_gateway_signal` to reject managed gateway or `apiKeyHelper`
settings that override the selected account.
The shared auth-unset helper removes inherited credentials and routing variables
from the terminal and model probes. Model probes use the selected account, and
the launch-catalog fingerprint includes its config folder.
Client-side resume lookup and relocation use the recorded account's locally
resolved folder, or the explicit map when no account is recorded.
Client-side effort resolution uses `selected_claude_config_dir` to read the
account's settings through the existing effort reader before session creation.
The runner reads the explicit value, not its own process environment, when it
locates Claude's `projects` and `sessions` folders for resume and status.

The environment map is not stored in session metadata or the database; the
account name is stored separately in `session_overrides.account`. A request with
`--env` fails if the session already has a live runner because changing a
running process's environment is not possible. A cold resume that launches a
new runner uses the new map.

## Input contract

- Each option must contain `=`. The first `=` separates the key and value.
- A key must match `[A-Za-z_][A-Za-z0-9_]*`.
- Duplicate keys are rejected.
- An empty value is preserved. For example, `--env NAME=` clears an inherited
  non-empty value for the new terminal.
- The request is limited to 64 variables, 128 bytes per key, 32 KiB per value,
  and 64 KiB total.
- Error messages can name a key but do not include values.

Secrets are never selected from the caller's environment. A secret moves only
when the caller includes its complete `KEY=VALUE` entry explicitly. The value
then crosses the configured Omnigent server and host-daemon control channel, so
callers must use only a trusted server and host. Command-line values can also
appear in shell history and process inspection. The public documentation tells
callers not to use this option for secrets.

## Failure and compatibility

Invalid input fails before daemon startup when it comes from the Omnigent CLI.
The server and daemon validate the map again because API and host-frame inputs
are trust boundaries. A malformed runner envelope fails the runner closed.

Older servers reject the new request field. Older hosts ignore no data because
the server and host protocol change ships as one Omnigent release. No
compatibility shim or durable migration is required.

## Verification

Focused tests cover parsing, duplicates, empty values, size limits, API-to-host
transport, daemon-to-runner transport, envelope removal, and all four native
terminal builders. Live verification uses harmless sentinels with Codex and
Claude and reads them from each wrapped agent's tool shell.

## Account usage and configuration readers

`omnigent usage` includes an Account column in the per-session table.
`GET /v1/usage` reads each session's recorded `session_overrides.account`.
An empty account selects the default login. Only the session owner receives
an extra account name; collaborators receive no account name.
The summary continues to use the existing per-user daily cost rollup.

The status-line wrapper, native skill discovery, and MCP inventory use
`selected_claude_config_dir` to follow the runner's selected account folder.
The explicit runner selection takes precedence over the ambient environment.
Account folder paths stay on the host.

Omnigent has no quota-axi integration. Per-account 5-hour and 7-day quota
windows and reset times remain follow-up work; session costs are estimates,
not provider quota measurements.
