# Changelog

All notable changes to this project are documented here.

## 0.1.0 - 2026-11-07

### Added

- Harness-neutral Maildir CLI with direct, topic, exclusive-role, and broadcast routing.
- Reserved `all` topic and `publish --broadcast`, excluding the sender.
- Explicit ACK lifecycle, named leases, lease repair, doctor diagnostics, and Claude Code integrations.
- PyPI-ready metadata, bundled agent skill files, zero-copy `setup`, and an experimental Claude Code plugin.
- Explicit Claude and Codex skill/MCP discovery, removable project guidance, and `doctor --integrations`.
- Linux and macOS CI on Python 3.9 and 3.13.

### Fixed

- Serialize channel presence updates and recheck confirmed push before hook wake-up, preventing one message from waking both delivery paths under load.
- Preserve user edits across repeated setup/repair/uninstall cycles by using a fresh pre-change backup and remove-only semantics for rerun records.
- Refuse additional inline, dotted-section, and MCP sub-table TOML spellings on Python 3.9.
- Make dry-run uninstall side-effect free and remove setup-created empty skill/state directories after uninstall or rollback.
- Make uninstall verify Claude MCP state last, tolerate the real absent-server wording or a missing CLI, and always finish local cleanup first.
- Use setup-owned atomic writes that follow config symlinks without chmodding user directories; centralize and clean up state backups while restoring unchanged files byte-for-byte.
- Detect quoted, spaced, dotted, section-inline, and root-inline Codex MCP definitions on Python 3.9 before writing TOML.
- Re-verify pending MCP registration and remove pending partial skills/backups during recovery.
- Validate outgoing payloads with the reader contract before delivery so accepted messages remain readable.
- Scope MCP capabilities, instructions, and tools to the detected harness.
- Persist the effective serialized-message limit in each mailbox root so writers and readers enforce the same value.
- Build cleanly with modern setuptools using the SPDX license expression.
- Preserve symlinks, Unicode, key order, and original modes during setup; keep timestamped backups and roll back failed installs.
- Refuse duplicate unmanaged Codex MCP tables and make uninstall resumable when external MCP state changes.
- Derive MCP harness identity so Codex joins as Codex and never receives Claude-only probes or channel push.
- Quarantine malformed unread files and skip files removed by a concurrent ACK.
- Validate required message fields, leave unsupported future versions for diagnosis, and never follow unread symlinks.
- Archive messages whose sender was unregistered before acknowledgement.
- Make role checking, registration, and subscription one atomic operation.
- Preserve `cur/` archives during unregister unless `--discard-mail` is explicit.
- Recover from malformed lease files through `lease-repair`.
- Skip malformed unrelated leases during unregister while retaining doctor diagnostics.

### Reliability and performance

- Fsync Maildir directories after `tmp` to `new` delivery and `new` to `cur` acknowledgement renames.
- Avoid repeated `chmod` system calls for already-secure directories and files.
- Page Claude hook reads past the former 50-message cap.
- Refuse to share the ambiguous fallback session name `default` when no session identity is available.
- Tighten owned directory permissions and enforce a configurable 1 MiB serialized-message limit.
- Keep multi-recipient `--wait-seconds` behavior simple and documented: it returns the first ACK or reply.

Considered, not done:

- Wake confirmation: Claude hooks provide no simple end-to-end acknowledgement that a wake reached the model; changing surfaced-state timing risks repeated wake loops.
- `cur/` and journal GC: retention policy is operator-specific, and automatic deletion is too destructive for 0.1.0.
- Extra mtime caching in the channel: polling already parses only new or redelivery-due files; profiling did not justify more state.
- Waiting for every broadcast ACK: this would change the established response shape and makes one slow recipient block the caller.
