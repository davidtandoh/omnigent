# Session custody and maintenance

API maintenance must keep a live native turn and its tools available. The
runner is the session owner. The host daemon and the API server are replaceable
attachments.

```text
Phone / Firstmate → API server ↔ host attachment
                                  │ exclusive attachment claim
                                  ▼
                        stable session runner
                          ├─ native agent process and app-server
                          ├─ private tmux terminals and helpers
                          └─ MCP tool relay and policy callbacks
```

Replacing the API server preserves the host, the runner and the native
process. Replacing the host preserves only sessions that the host has admitted
to durable custody.

## Current scope

No harness is admitted to durable custody yet. `SUPPORTED_HARNESSES` is empty,
and the [launch receipt](runtime-launch-contract.md) reports
`durable_custody=false` for every harness. The custody library, its receipts and
its exact-end procedure are present and unit-tested, but the host does not
install custody for any session until a harness passes a real native survival
proof on the owner-keyed custody record. Admission therefore changes only when
that proof lands, not through configuration.

What works today for every harness:

- API maintenance stop and start with the host, runners and native processes
  retained.
- Host retention of its runners beyond the loopback refusal horizon during an
  API outage.
- Truthful terminal teardown: failed termination produces a named refusal,
  never a successful ending or deletion event.

Terminal cleanup closes the terminal before releasing its environment, skills,
native bridge files or app-server. Exit finalization propagates failure before
publishing `TerminalExitEvent`. Session cleanup and terminal DELETE propagate
named JSON refusals. Shutdown attempts each independent terminal and releases
that session's dependencies only after its terminal cleanup succeeds. A shutdown
refusal reaches the session's forwarder through its existing cancellation path
before the event loop exits. Both Codex forwarder cleanup paths preserve the
app-server and routers when cancellation carries that named refusal.

A kill-failed live terminal becomes unregistered without claimed ownership.
Its private directory and dependent resources remain intact on refusal. This
slice does not track that terminal for subsequent cleanup, guarantee eventual
cleanup, or prove durable custody. Failed launches have no separate retained
state, reuse refusal or reaper exclusion. Real custody remains later work.

Close, launch and transfer retain the existing per-instance exclusion. Transfer
acquires both source and destination locks; contention refuses with `owner_busy`.
Watcher exit callbacks run after releasing the instance lock. If native Codex
rotation refuses, forwarding stops with `native_rotation_refused` in the existing
bridge error record. It does not consume later events against the old thread or
claim transfer success. There is no rotation retry in this slice.

What refuses today: `omnigent codex --binding-json`, `--reattach-binding`,
`runtime session-contract` and `runtime end-session` return
`unsupported_custody` or `record_mismatch` because no custody record exists.

## Ownership and adoption

Before starting native resources, an admitted runner acquires the private
session's `owner.lock`. The runner records a new owner generation and its exact
process identity. The owner watchdog checks the session lease across threads.
Loss of the launch parent does not end a valid lease. Loss of the lease still
requests shutdown. Existing orphan sweeps and resource cleanup remain active.

A host acquires `attachment.lock` before adopting a session. Adoption verifies
the host ID, endpoint, config identity, held owner lease, exact process start
times, helper processes, socket identities and retained bridge config digests.
The host never launches another native agent for a live session. Repeated
adoption by the same attachment is idempotent. Concurrent adoption refuses
`owner_busy`. A replacement increments the attachment epoch once.

Before relaunching a bound runner, the API asks the authenticated host for that
exact runner's status. The host adopts verified retained owners before
answering. A live owner keeps its existing binding while its tunnel reconnects.
If the host connection changes or gives no valid status, the API refuses
relaunch without changing the binding.

A custody record is keyed by the session ID that launched the owner. A native
conversation rotation (`/new`, `/clear`, `/fork`) moves the main terminal
resource to the successor session in the runner's registry only. The custody
record does not follow that rotation; the owner-keyed record that tracks the
active conversation as a label is the next change. A custody-owned helper
terminal cannot leave its owner's session scope: a transfer to another session
refuses `scope_mismatch`.

The retained runner owns its inference config, MCP relay, policy callbacks,
native app-server and terminals. Host replacement does not rebuild them.
Replacement of the owner and owner-crash survival are outside this contract. An
operating-system reboot requires recovery; it cannot preserve the same live
process.

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
| `harness` | String `codex-native`. The record schema admits this value only; other harnesses arrive with their own survival proof. |
| `host_id`, `endpoint`, `workspace` | Exact host scope, server URL and launch workspace. |
| `owner_generation` | Opaque generation string. Required when ending a session. |
| `owner` | Process identity: integer `pid`, numeric `started_at` in Unix seconds, string `pid_ns`, string or null `boot_id`. Start identity and local user ownership reject PID reuse. macOS uses `pid_ns=none` and null boot ID; the live kernel lease remains required. |
| `owner_runtime_generation` | Measured `sha256:` runtime generation of the retained runner. A replacement attachment may have another generation; the receipt does not relabel the owner. |
| `config_digest` | SHA-256 hex digest of the global config path/bytes and the `OMNIGENT_CONFIG_HOME`, `OMNIGENT_DATA_DIR`, `CODEX_HOME` and `OMNIGENT_CODEX_PATH` selectors. The launch receipt lists each selector as an allowed client input. |
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

## Firstmate launch binding and reattachment

The public Codex binding commands exist and keep their schema:

```sh
omnigent codex --server URL --binding-json > session-binding.json
omnigent codex --reattach-binding session-binding.json --binding-json
omnigent codex --reattach-binding session-binding.json
```

The binding is a projection of the custody receipt: schema version `1` with
`session_id`, `runner_id`, `harness`, `owner_generation`, `host_id`,
`endpoint`, `workspace`, `config_digest`, `environment_digest`, `owner`,
`native` and `terminal`. Unknown fields or schema versions refuse. While no
harness is admitted, `--binding-json` refuses `unsupported_custody` and
reattachment refuses `record_mismatch`. A consumer that already follows this
schema needs no change; the owner-keyed record will add the active conversation
alongside the launch session under a new schema version.

## Maintenance and intentional end

Run these commands only within the existing configured endpoint and ownership
scope. They preserve the configured data paths and routes.

| Operation | Command and effect |
| --- | --- |
| API maintenance stop | `omnigent server maintenance-stop --json` verifies the exact API process, port owner and auth/config signature; then stops only that API process. Returns `schema_version=1`, `endpoint`, `state=detached`. |
| API maintenance start | `omnigent server maintenance-start --json` restores the recorded fixed port, base path and auth/config. A live previous API, occupied port or config drift refuses. Returns `schema_version=1`, `endpoint`, `state=ready`. |
| Host maintenance detach | `omnigent host maintenance-detach --server URL` detaches the exact selected host. Admitted session owners are preserved; runners without custody are terminated, as on `host stop`. Start `omnigent host --server URL --no-open --non-interactive` to reattach. |
| End one session | `omnigent runtime end-session --session ID --owner-generation GENERATION --host-id HOST --server URL --json` verifies the exact scope and resources, signals only that owner, waits for intentional resource cleanup and retains the tombstone. Returns `schema_version=1`, `session_id`, `owner_generation`, `state=ended`. Repeating the same exact end is idempotent. |

API maintenance currently qualifies only header authentication, absolute local
SQLite database and artifact paths, and an optional regular config file. The
schema-2 maintenance receipt records these effective paths, the working
directory, the exact single-user marker and configuration/environment digests.
It never records secrets or raw launch arguments. The restart uses this bounded
recipe and checks the new API process's receipt after readiness. A managed
child publishes only this maintenance receipt. The parent publishes discovery
after verifying readiness and port ownership. A failed start terminates and
reaps only the owned child. Package version remains part of normal runtime
reuse detection but is absent from maintenance configuration identity. The
internal `OMNIGENT_HOST_DAEMON_CONFIG_SIG` launch marker and injected
`OMNIGENT_LOG_TTY_FD` descriptor are also excluded. Logging preferences remain
part of configuration identity.

Non-header authentication, nonlocal databases, relative artifact paths and a
separate conversation database, execution-timeout or agent CLI overrides
refuse `unsupported_restart` before shutdown. Old unqualified receipts also
refuse. Config-file or environment drift refuses before stop or start. Restore
the approved inputs before retrying; no generic launch replay is supported.

Normal `host stop` and combined `server stop` retain intentional session
teardown. `server stop` and global `stop` end each custody record separately
and report refusals by category; the daemon stays up while any custody remains
unresolved. The maintenance commands provide the distinct detach operation.
They do not change credentials or grant native tool permissions.

### Exact end

Exact end holds the session's `operation.lock` for the whole operation. It
requires the exact session ID, owner generation, host ID and endpoint. It does
not compare the caller's current config digest. Unrelated global config bytes or
a changed `CODEX_HOME` or `OMNIGENT_CODEX_PATH` therefore do not block the end
of an already authorized owner. Adoption and strict reattachment still refuse
`config_mismatch`.

1. If the owner lease is held, end verifies the owner and every recorded
   resource. Then it sends SIGTERM to that owner only. The wait is bounded by the
   owner's recorded cleanup budget: a fixed margin, the Codex app-server close
   budget per helper and the terminal close budget per terminal.
2. If the owner lease is free, end acquires that lease. It then proves that the
   owner, each helper and each terminal tmux/pane process are absent. Each
   terminal socket path and each policy resource path must also be absent.
3. Under both locks, end rereads the receipt. If the receipt still matches the
   proven receipt, end writes the `ended` tombstone.

A live, reused or unknown process identity, a held or unknown lease, a retained
socket, helper or policy resource, a foreign socket inode, a foreign scope or a
changed receipt refuses. A refusal does not signal, delete or rewrite anything.
A concurrent end refuses `owner_busy`; a repeated end returns the same result.
A dead PID or a free lease alone is not sufficient evidence.

The host uses the same owner cleanup budget when it stops a retained owner.
When the host detaches, it releases each attachment claim independently. A
malformed or foreign attachment receipt stays unchanged and is logged by
category only. The host then continues with the remaining claims.

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
and `unavailable_probe`, plus `unsupported_restart` for unqualified launch
recipes. Errors do not include raw process output or environment values.
Refusals preserve existing receipts and resources.

## Evidence and next steps

Unit and integration suites cover the custody records, locks, receipts, exact
end, launch receipt, API maintenance and host retention:
`tests/runner/test_session_custody.py`, `tests/runner/test_session_custody_end.py`,
`tests/runner/test_session_binding.py`, `tests/terminals/test_registry_custody.py`,
`tests/host/test_launch_contract.py`, `tests/host/test_server_maintenance.py`,
`tests/host/test_connect.py` and `tests/e2e/test_local_server_lifecycle_e2e.py`.

`tests/e2e/test_session_custody_e2e.py` drives real Codex and tmux through host
death, adoption and API outages. It is skipped while no harness is admitted and
returns with the owner-keyed custody record.

Planned in order, each as its own change: the owner-keyed custody record with
active and retired session labels and a pending-rotation field; Claude
rotation retry from its durable hook record plus a real Claude survival test,
which admits `claude-native`; Codex rotation publication from the pending
record within the native delivery and finite-buffer limits, which re-admits
`codex-native`; then the binding schema that reports the active conversation.
