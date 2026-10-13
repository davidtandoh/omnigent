# Session custody and maintenance

API or host maintenance must keep a live native turn and its tools available.
For admitted Codex sessions, the runner remains the session owner. The host and
API server are replaceable attachments.

```text
Phone / Firstmate → API server ↔ host attachment
                                  │ exclusive attachment claim
                                  ▼
                        stable session runner
                          ├─ Codex app-server
                          ├─ private tmux terminals and helpers
                          └─ MCP tool relay and policy callbacks
```

Replacing an attachment preserves the runner, native process and resources.

## Ownership and adoption

The host admits durable custody for `codex-native` on macOS. The session must
have an exact session ID. Other harnesses and platforms retain their existing
lifecycle. The [launch receipt](runtime-launch-contract.md) reports this scope
through `harnesses[].durable_custody`.

Before starting native resources, the runner acquires the private session's
`owner.lock`. The runner records a new owner generation and its exact process
identity. The owner watchdog checks the session lease across threads. Loss of
the launch parent does not end a valid lease. Loss of the lease still requests
shutdown. Existing orphan sweeps and resource cleanup remain active.

A host acquires `attachment.lock` before adopting a session. Adoption verifies
the host ID, endpoint, config identity, held owner lease, exact process start
times, helper processes, socket identities and retained bridge config digests.
The host never launches another native agent for a live session. Repeated
adoption by the same attachment is idempotent. Concurrent adoption refuses
`owner_busy`. A replacement increments the attachment epoch once.

The retained runner owns its inference config, MCP relay, policy callbacks,
native app-server and terminals. Host replacement does not rebuild them. This
contract does not replace the session runner itself or promise survival of a
runner crash. An operating-system reboot requires recovery; it cannot preserve
the same live process.

## Read the session receipt

Use the selected runtime executable and existing approved data/config paths:

```sh
/absolute/runtime/bin/omnigent runtime session-contract \
  --session conv_example --json
```

The command is read-only. Success prints one canonical JSON object and exits
zero. Refusal exits one with `schema_version=1` and `error.category`. A live
receipt verifies the owner lease and resources. An ended receipt is a retained
tombstone and does not claim resource liveness.

| Field | Type and meaning |
| --- | --- |
| `schema_version` | Integer `1`. Reject other schemas. |
| `session_id`, `runner_id` | Exact durable session ID and token-bound runner ID. |
| `harness` | String `codex-native`. |
| `host_id`, `endpoint`, `workspace` | Exact host scope, server URL and launch workspace. |
| `owner_generation` | Opaque generation string. Required when ending a session. |
| `owner` | Process identity: integer `pid`, numeric `started_at` in Unix seconds, string `pid_ns`, string or null `boot_id`. Start identity and local user ownership reject PID reuse. macOS uses `pid_ns=none` and null boot ID; the live kernel lease remains required. |
| `owner_runtime_generation` | Measured `sha256:` runtime generation of the retained runner. A replacement attachment may have another generation; the receipt does not relabel the owner. |
| `config_digest` | SHA-256 hex digest of the global config path/bytes and scoped config/data/native-launch path variables. |
| `environment_digest` | SHA-256 hex digest of canonical validated explicit native environment values. Values are not persisted here. |
| `config_files` | Array of `{path: string, digest: string}` for retained inference config. Digests use SHA-256. |
| `state` | String `starting`, `live` or `ended`. `live` follows terminal publication; it is not a claim that a turn is active. |
| `terminals` | Array of `{name, session_key, socket, device, inode, tmux, pane}`. Names/socket are strings; device/inode are integers; tmux/pane are exact process identities. |
| `helpers` | Object keyed by helper name. `codex_app_server` identifies the actual native process, with exact PID/start identity. |
| `resources` | Object keyed by resource name. Values are `{path, digest}` for retained tool/policy bridge config. |
| `reattachment` | Null or `{schema_version: 1, generation: string, process: process identity, epoch: integer, state: attached or detached, owner_generation: string}`. This records the last attachment claim; it does not independently assert host liveness. |

State lives at `<data>/custody/<sha256(session_id)>/`. The custody directories
are owned by the local user with mode 0700. Receipts use private atomic writes
and bounded regular-file reads. Refusal does not remove records or signal
foreign processes. Ended tombstones remain available for diagnosis.

## Maintenance and intentional end

Run these commands only within the existing configured endpoint and ownership
scope. They preserve the configured data paths and routes.

| Operation | Command and effect |
| --- | --- |
| API maintenance stop | `omnigent server maintenance-stop --json` verifies the exact API process, port owner and auth/config signature; then stops only that API process. Returns `schema_version=1`, `endpoint`, `state=detached`. |
| API maintenance start | `omnigent server maintenance-start --json` restores the recorded fixed port, base path and auth/config. A live previous API, occupied port or config drift refuses. Returns `schema_version=1`, `endpoint`, `state=ready`. |
| Host maintenance detach | `omnigent host maintenance-detach --server URL` detaches the exact selected host, preserving admitted session owners. Start `omnigent host --server URL --no-open --non-interactive` to reattach. |
| End one session | `omnigent runtime end-session --session ID --owner-generation GENERATION --host-id HOST --server URL --json` verifies the exact scope and resources, signals only that owner, waits for intentional resource cleanup and retains the tombstone. Returns `schema_version=1`, `session_id`, `owner_generation`, `state=ended`. Repeating the same exact end is idempotent. |

Normal `host stop` and combined `server stop` retain intentional session teardown.
The maintenance commands provide the distinct detach operation. They do not
change credentials or grant native tool permissions.

An already connected loopback host retries API connection refusal beyond the
old 100-attempt horizon, with bounded backoff. An unproven endpoint still has
the original refusal limit. HTTP 401/403 remain explicit authentication denial;
the launch receipt reports `auth_denied`. A network outage is not authorization
to discard or replace credentials.

## Refusal categories

Custody commands use `owner_busy`, `scope_mismatch`, `config_mismatch`,
`record_mismatch`, `foreign_socket`, `stale_attachment`, `process_pending` and
`unavailable_probe`. Internal creation can also refuse `unsupported_custody`.
Host relaunch adds `binding_mismatch` and `harness_mismatch`. API maintenance
uses `record_mismatch`, `config_mismatch`, `occupied_port`, `process_pending`
and `unavailable_probe`. Errors do not include raw process output or environment
values. Refusals preserve existing receipts and resources.

## Isolated evidence and remaining acceptance

`tests/e2e/test_session_custody_e2e.py` runs real Codex and tmux with temporary
HOME/config/data, a private API/host and a synthetic gated model. It proves
the same native PID/start identity and in-flight turn across host SIGKILL,
graceful host detach, repeated reattachment and API outages of 0 and at least
65 seconds. It also checks helper terminal operation, a framework-approved
native MCP call, native policy denial, phone-style terminal read/input,
no duplicate input, and intentional native/socket teardown.

Focused tests reproduce 101 connection refusals deterministically, past the
old reconnect horizon, without killing the retained owner. Subprocess tests
prove concurrent exclusive attachment claims. Refusal tests cover stale/reused
PIDs, foreign boot/namespace/socket, changed config/environment, an occupied
API port, symlinked state and changed owner authority.

These tests do not use a production phone or shared session. Live acceptance,
immutable runtime installation, destination backup/schema checks and the
separate maintenance window remain required before primary switchover.
