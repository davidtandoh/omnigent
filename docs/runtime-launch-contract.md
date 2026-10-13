# Runtime launch contract

Firstmate needs to find the session runtime during a trace outage. The CLI
publishes a read-only receipt independently of the collector or trace store.

```text
Firstmate → runtime launch-contract → executable + endpoint + capabilities
Firstmate → external telemetry policy → admit or deny a new session
Existing session → runner → native terminal + MCP + policy resources
```

The launch receipt never starts a service or stops an existing session.

## Read the receipt

Run the exact executable selected by the runtime installer:

```sh
/absolute/runtime/bin/omnigent runtime launch-contract \
  --server http://127.0.0.1:6767 --json
```

The endpoint must use HTTP or HTTPS without credentials, query or fragment.
The command checks `/health` with a three-second timeout. It bypasses ambient
HTTP proxies and does not follow redirects. A successful command prints one
canonical JSON object and exits zero. A refusal exits one and prints
`{"error":{"category":"..."},"schema_version":1}`.

| Field | Contract |
| --- | --- |
| `schema_version` | Integer `1`. Reject unknown schemas. |
| `runtime.executable` | Absolute, executable console entry point beside the running Python interpreter. A direct shebang or the bounded uv/distlib shell trampoline must resolve to that interpreter in the same runtime directory. The check does not execute shell text. |
| `runtime.python` | Absolute Python interpreter used by this invocation. |
| `runtime.generation` | `sha256:` followed by 64 hex characters. Digest of the launcher and sorted package Python paths and bytes. A version label alone is insufficient. |
| `runtime.package_version` | Installed distribution version. Informational. |
| `runtime.source_commit` | Installed VCS or build receipt commit, or `null` for an editable or unqualified install. Consumers requiring an immutable source pin must refuse `null`. |
| `runtime.state_schema` | Integer `1`. Identifies this contract's state format; it is not the chat database migration revision. |
| `endpoint` | Exact endpoint, with trailing slash removed. Existing loopback port and tailnet route stay owned by the installer. |
| `availability` | `ready` after mandatory health verification. |
| `allowed_client_environment` | Sorted exact names: `CODEX_HOME`, `OMNIGENT_AUTH_PROVIDER`, `OMNIGENT_CODEX_PATH`, `OMNIGENT_CONFIG_HOME`, `OMNIGENT_DATA_DIR`, `OMNIGENT_LOCAL_SERVER_PORT`, `OMNIGENT_WEB_BASE_PATH`. The list includes every selector in the session custody `config_digest`. `CODEX_HOME` selects the Codex config/auth home. `OMNIGENT_CODEX_PATH` selects the native Codex executable. |
| `client_environment` | Only present values of those names. Provider secrets and telemetry variables are excluded. The policy owner must approve environment values before invoking the runtime. |
| `native_environment` | Explicit `--env KEY=VALUE` interface. At most 64 entries and 65,536 total UTF-8 bytes. No ambient forwarding. Existing validation rejects duplicate names, malformed names and NUL bytes. |
| `harnesses` | Registry-derived rows: `id`, wrapper `command`, `capabilities`, and `durable_custody`. Wrapper support is separate from installed CLI/auth readiness. |
| `telemetry` | Exactly `{"owner":"external","admission":"external_policy"}`. No trace-delivery or collection-policy claim. |

## Refusals

`invalid_endpoint`, `endpoint_unavailable`, `endpoint_mismatch`, `auth_denied`,
`runtime_unverified`, and `runtime_mismatch` are stable error categories.
Errors contain no environment values, transcripts or captured subprocess output.
A 401 or 403 is `auth_denied`; connection refusal, timeout, redirect and other
HTTP failures are `endpoint_unavailable`. A foreign health response is
`endpoint_mismatch`.

## Policy and evidence

The external telemetry owner decides new admission. Personal sessions can start
during a trace gap. If policy requires collection, deny new admission when
collection is unavailable. Keep custody of existing sessions. Work exclusion
keeps content and telemetry off. Enabled exports must use the approved redactor;
this interface does not authorize a raw-content fallback.

All native wrappers remain listed from the existing registry. No harness
reports `durable_custody=true` yet; see [current scope](session-custody.md#current-scope)
for what admission requires. Direct wrapper availability does not establish
custody. See [session custody](session-custody.md) for the exact owner,
reattachment and maintenance contract. The same document defines the public
`omnigent codex --binding-json` launch result and
`omnigent codex --reattach-binding FILE` attachment command. Consumers must keep
that exact session binding and use it when reconnecting a retained owner.

The isolated CLI test in `tests/e2e/test_launch_contract_e2e.py` verifies receipt
availability during a synthetic trace outage. The separate custody end-to-end
test is skipped until a harness is admitted. Neither test authorizes deployment
or primary migration.
