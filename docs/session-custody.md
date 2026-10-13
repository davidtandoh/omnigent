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
lifecycle and require their own native survival evidence before custody admission.
The [launch receipt](runtime-launch-contract.md) reports this scope
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

Before relaunching a bound Codex runner, the API asks the authenticated host
for that exact runner's status. The host adopts verified retained owners before
answering. A live owner keeps its existing binding while its tunnel reconnects;
the API does not schedule superseded teardown. If the host connection changes
or gives no valid status, the API refuses relaunch without changing the binding.
The status query includes the exact session ID. Custody verification failure
returns `refused`, which also preserves the binding. Restoring the approved
configuration allows adoption of the same owner.

A missing or invalid session receipt refuses that session. Enumeration continues
for other sessions. The incomplete directory remains in place and cannot be
used to create a replacement owner.

When native Codex `/new` transfers the main terminal to another conversation,
custody retires the old session scope and publishes the new session scope under
the same owner generation. The runner, native process, terminals and helpers
remain alive. Binding, reattachment and end requests for the retired scope
refuse `scope_mismatch`. The host adopts the current scope through its existing
adoption path. A shared bridge handoff retires the old relay receipt; later
cleanup for the old session cannot remove the successor receipt. Session stop
and archive requests carry the session ID to the host. The host verifies custody
while holding the same operation lock used by transfer, before it signals the
runner. A failed clear-binding request cannot authorize a stop of the successor.
An explicit host-wide end remains independent of session scope.

The retained runner owns its inference config, MCP relay, policy callbacks,
native app-server and terminals. Host replacement does not rebuild them. This
approved contract uses “attachment runner” for the replaceable host attachment,
not the stable session owner. Replacement of that owner and owner-crash survival
are outside this contract. An operating-system reboot requires recovery; it cannot preserve
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
| `state` | String `starting`, `live` or `ended`. `live` follows terminal publication; it is not a claim that a turn is active. Stored `transferred` tombstones are refused by this command. |
| `terminals` | Array of `{name, session_key, socket, device, inode, tmux, pane}`. Names/socket are strings; device/inode are integers; tmux/pane are exact process identities. |
| `helpers` | Object keyed by helper name. `codex_app_server` identifies the actual native process, with exact PID/start identity. |
| `resources` | Object keyed by resource name. Values are `{path, digest}` for retained tool/policy bridge config. |
| `reattachment` | Null or `{schema_version: 1, generation: string, process: process identity, epoch: integer, state: attached or detached, owner_generation: string}`. This records the last attachment claim; it does not independently assert host liveness. |

State lives at `<data>/custody/<sha256(session_id)>/`. The custody directories
are owned by the local user with mode 0700. Receipts use private atomic writes
and bounded regular-file reads. Refusal does not remove records or signal
foreign processes. Ended tombstones remain available for diagnosis.

## Firstmate launch binding and reattachment

Use the public Codex command on the admitted macOS host. Run it from the exact
workspace with the approved config/data environment:

```sh
omnigent codex --server URL --binding-json > session-binding.json
omnigent codex --reattach-binding session-binding.json --binding-json
omnigent codex --reattach-binding session-binding.json
```

1. The first command creates one session through the existing Codex launch path.
   It emits the verified durable binding and exits without attaching a TTY.
   `--env KEY=VALUE`, `--model` and `--prompt` retain their launch meanings.
   Binding mode rejects `--resume`, `--session` and `--smart-routing`.
2. The second command validates an existing binding and returns the same binding
   as canonical JSON. It does not start a backend, native owner or terminal.
3. The third command performs the same validation, then attaches the existing
   local tmux terminal through the typed Codex terminal attachment interface.
   Detaching that TTY leaves the native turn running. Host/API recovery uses the
   maintenance operations below; this command does not start those services.

A successful machine command exits zero and emits one canonical JSON object
on stdout. Launch progress goes to stderr. The binding schema is:

| Field | Meaning |
| --- | --- |
| `schema_version` | Integer `1`. Unknown fields or schema versions refuse. |
| `session_id`, `runner_id`, `harness` | Exact session and runner binding; harness is `codex-native`. |
| `owner_generation`, `owner` | Stable owner generation and exact process identity. |
| `host_id`, `endpoint`, `workspace` | Exact launch scope, without aliases or workspace rebinding. |
| `config_digest`, `environment_digest` | Exact configuration and explicit native environment digests. No secret values. |
| `native` | Exact Codex app-server process identity. |
| `terminal` | The single `codex/main` terminal receipt, with the fields defined above. The typed attachment uses tmux target `main`. |

The binding is a projection of the durable custody receipt. Firstmate stores
that binding alongside its own task ID; Omnigent has no Firstmate task store.
Reattachment reads at most 65,536 bytes from a regular binding file. It compares
all binding identities, checks current host configuration and verifies the live
lease, native process, terminal socket and retained resources. A changed owner,
foreign scope, duplicate native terminal, stale lease or missing resource refuses.
There is no resume, launch, rebind, teardown or record-deletion fallback.
Repeated and concurrent validation returns the same binding. Multiple terminal
viewers can attach; they do not acquire or replace the session owner's lease.

Refusal exits one with `{"schema_version":1,"error":{"category":"..."}}`.
Categories are `record_mismatch`, `scope_mismatch`, `config_mismatch`,
`binding_mismatch`, `stale_attachment`, `foreign_socket`, `unsupported_custody`
and `unavailable_probe`. Fresh-launch failures also use `launch_unavailable`.
Invalid command option combinations use Click's usage error and exit two.

## Maintenance and intentional end

Run these commands only within the existing configured endpoint and ownership
scope. They preserve the configured data paths and routes.

| Operation | Command and effect |
| --- | --- |
| API maintenance stop | `omnigent server maintenance-stop --json` verifies the exact API process, port owner and auth/config signature; then stops only that API process. Returns `schema_version=1`, `endpoint`, `state=detached`. |
| API maintenance start | `omnigent server maintenance-start --json` restores the recorded fixed port, base path and auth/config. A live previous API, occupied port or config drift refuses. Returns `schema_version=1`, `endpoint`, `state=ready`. |
| Host maintenance detach | `omnigent host maintenance-detach --server URL` detaches the exact selected host, preserving admitted session owners. Start `omnigent host --server URL --no-open --non-interactive` to reattach. |
| End one session | `omnigent runtime end-session --session ID --owner-generation GENERATION --host-id HOST --server URL --json` verifies the exact scope and resources, signals only that owner, waits for intentional resource cleanup and retains the tombstone. Returns `schema_version=1`, `session_id`, `owner_generation`, `state=ended`. Repeating the same exact end is idempotent. |

API maintenance currently qualifies only header authentication, absolute local
SQLite database and artifact paths, and an optional regular config file. The
schema-2 maintenance receipt records these effective paths, the working directory,
the exact single-user
marker and configuration/environment digests. It never records secrets or raw
launch arguments. The restart uses this bounded recipe and checks the new API
process's receipt after readiness. A managed child publishes only this maintenance
receipt. The parent publishes discovery after verifying readiness and port ownership.
Package version remains part of normal runtime
reuse detection but is absent from maintenance configuration identity. The internal
`OMNIGENT_HOST_DAEMON_CONFIG_SIG` launch marker and injected
`OMNIGENT_LOG_TTY_FD` descriptor are also excluded. Logging preferences remain
part of configuration identity. Effective
auth, data, config and explicit native environment checks remain unchanged.

Non-header authentication, nonlocal databases, relative artifact paths and
a separate conversation database, execution-timeout or agent CLI overrides
refuse `unsupported_restart` before shutdown. Old unqualified receipts also
refuse. Config-file or environment drift refuses before stop or start. Restore
the approved inputs before retrying; no generic launch replay is supported.

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
and `unavailable_probe`, plus `unsupported_restart` for unqualified launch recipes.
Errors do not include raw process output or environment
values. Refusals preserve existing receipts and resources.

## Isolated evidence and remaining acceptance

`tests/e2e/test_session_custody_e2e.py` runs real Codex and tmux with temporary
HOME/config/data, a private API/host and a synthetic gated model. It proves
the same native PID/start identity and in-flight turn across host SIGKILL,
graceful host detach, repeated reattachment and API outages of 0 and at least
65 seconds. It also checks public launch bindings, concurrent consumer reattachment,
closed-helper receipt removal, retained helper terminal operation, a framework-approved
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
