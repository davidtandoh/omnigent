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

The four native terminal builders merge the consumed entries into the terminal
environment. This explicit map has precedence over an inherited value. Existing
terminal `env_unset` rules still remove protected variables after the merge.

The map is not stored in session metadata or the database. A request with
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
