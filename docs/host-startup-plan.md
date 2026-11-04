# Host startup contract repair

Status: implemented in this feature checkout; not activated on the host.
Request: 2026-11-04, owner requested startup fixes and a maintained restart
command. The owner permits requested full restarts to interrupt jobs.

## Contract

1. A slow optional capability probe must not delay `host.hello` or the tunnel
   receive loop. The host publishes completed discovery through the existing
   harness-readiness frame. Cached metadata remains available on reconnect.
2. Successful and in-flight discovery are retained across reconnects. Failed
   discovery can retry. Connection cancellation does not cancel shared
   discovery or duplicate still-running worker probes.
3. Startup logs identify the probe and registration stages with monotonic
   elapsed durations and bounded, content-free outcomes. Reuse
   `StartupProfiler`; do not log authentication output or configuration.
4. The existing event-loop watchdog can monitor the host and capture bounded
   frame locations during a stall. Reuse its implementation, not a new sampler.
5. `host status --no-quotas --server '' --json` reports local server-confirmed
   host status without triggering quota work. Existing status behavior remains
   the default because interactive consumers use quota output.
6. Registration still requires server confirmation and retains its finite
   deadline. This change does not claim a measured new timeout or a proven
   explanation for the historical stall.

## Components and reuse

- `host/connect.py`: detach optional discovery from registration; publish its
  result through the existing refresh loop; reuse `StartupProfiler`.
- `util/event_loop_watchdog.py`: extend the existing role validation to host;
  wrap the production host run without changing server or runner behavior.
- `cli.py`: add an explicit quota-free status option.
- Existing host, CLI and watchdog tests: blocked discovery, fast discovery,
  reconnection, cancellation, initial readiness publication, status isolation,
  malformed status and bounded diagnostics.

## Risk and verification

High risk: host lifecycle, public CLI and job availability. Keep process
ownership, authentication, session custody, data stores and launch gates
unchanged. A host reporting unknown metadata must still run existing launch
readiness checks. Use failing regressions, owning-component test suites,
lint/type checks. The owner waived external review on 2026-11-04
("no need to do external review. Just cook"). A production startup and
real worker turn remain operational acceptance; do not infer them from mocks.

## Authority

Implementation and feature-branch publication are authorized by the request
and standing global publication instruction. No merge is granted.
External review is waived by the owner; no external reviewer was used.

## Startup flow

```text
accepted tunnel → host.hello → receive requests
                       ↑
background discovery → readiness update
```

Host registration and advisory discovery advance independently. Each worker
launch still checks the requested harness before starting it.
Probe logs contain stage names, monotonic elapsed time and outcomes only.
The host watchdog reuses the bounded event-loop stack sampler when the loop
stops advancing. It does not record credentials or local variable values.

## Verification recorded on 2026-11-04

The first regressions reproduced blocked registration, unsupported
`--no-quotas`, an unsupported watchdog host role and overlapping refresh
after a cancelled worker await. The targeted sandbox run passed 27 tests.
Its process-inspection restriction prevented successful pytest cleanup.

The subsequent normal-host run completed the three owning suites with
505 passed, two failed and one skipped. Both failures came from the same
real-socket test peer: it decoded the initial readiness update as a heartbeat
reply, rejected the frame and closed the connection. The production server
already accepts both frame types. The test now verifies both messages in
either order before the deliberate close. Its heartbeat, close-code,
connection-identity and in-flight-send assertions remain in place.

The first rerun found a second fixture mismatch: the peer's empty harness map
is valid in a hello but invalid in a readiness update. The fixture now provides
a valid sample harness. Direct readiness and heartbeat encoding/decoding checks
pass. The final normal-host rerun passes both real-socket close cases.

The final owning-suite run passes 507 tests with one platform skip in 55.01
seconds, with retries disabled. The skip covers filenames that APFS rejects.
All applicable repository hooks pass, including Ruff, the full project type
check and custom lint. No production ownership guard was relaxed to
accommodate the sandbox.

GitHub validation then found three credential E2E cases that read capability
maps immediately after registration. The existing API poll now waits for a
published map for both foreground and background hosts before applying the
unchanged credential assertions. All three focused cases pass on the normal
host in 52.38 seconds, with retries disabled and no model requests. The other
GitHub workflows passed on the initial repair commit; the corrected test is
submitted for its own CI run.

The historical stall's exact blocked frame remains unknown. This patch
removes advisory work from the registration path and adds diagnostics for
future stalls. It does not establish cold-start percentiles or a latency
service-level objective. A host startup and one real worker turn remain
required before claiming operational recovery.
