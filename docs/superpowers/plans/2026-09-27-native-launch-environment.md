# Native Launch Environment Implementation Plan

**Goal:** Add an explicit, transient environment handoff for native Claude,
Codex, Kiro, and Antigravity launches.

**Risk:** High. The change adds a public command option and crosses a process
trust boundary. Values can be sensitive when a caller explicitly supplies
them. The route must not inspect or copy ambient environment values.

## Acceptance map

| Criterion | Owner | Change | Evidence |
| --- | --- | --- | --- |
| Explicit opt-in | Native CLI | Repeatable `--env KEY=VALUE` | CLI tests for four wrappers |
| Cross daemon boundary | Host launch protocol | Transient request and frame field | API/frame/daemon tests |
| Four terminal wrappers | Runner native orchestration | Shared explicit terminal env merge | One focused test per wrapper |
| Safe input | Shared native launch environment | Syntax, duplicate, empty, and size rules | Unit tests |
| No implicit secrets | Shared contract and docs | Never read ambient values; value-free errors | Unit tests and review |
| Live behavior | Native Codex and Claude | Harmless sentinel in tool shell | Manual host evidence |
| Public contract | Repository documentation | Command and security boundary | Documentation review |

## Implementation sequence

1. Add the shared parser, validator, envelope, and runner-owned environment
   accessor.
2. Add `--env` to Claude, Codex, Kiro, and Antigravity/`agy`.
3. Thread the validated map through each wrapper's daemon preparation path.
4. Extend the runner launch request and host launch frame without persisting the
   map.
5. Let the daemon encode the validated map for the runner. Consume it at runner
   startup.
6. Merge the map in the four runner-owned terminal builders.
7. Add focused unit and integration tests, then run the relevant aggregate
   gate.
8. Verify Codex and Claude live with non-secret sentinels.

## Non-goals

- Do not copy the complete client environment.
- Do not store values in session metadata or configuration.
- Do not mutate the environment of an already-running terminal.
- Do not add a compatibility path for mixed Omnigent client/server/host
  versions.

## Recovery

Revert this change. No migration or cleanup is required because the handoff is
transient and creates no durable data.
