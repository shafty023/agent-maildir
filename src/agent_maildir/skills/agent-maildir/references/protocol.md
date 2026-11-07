# Agent Mailbox Protocol

## Storage layout

```text
<root>/
  protocol.json
  .lock
  registry/
    agents.json
    subscriptions.json
  mailboxes/<agent>/Maildir/
    tmp/
    new/
    cur/
    bad/
  leases/
    <name>.json
    bad/
  presence/
    <agent>.json
  sessions/
    <session-id>.json
    <session-id>.surfaced.json
    <session-id>.lock
  journal/
    events.ndjson
```

`AGENT_MAILDIR_ROOT` overrides the default `~/.local/share/agent-maildir`.
For migration from the earlier private name, `AGENT_MAILBOX_ROOT` is honored
only when `AGENT_MAILDIR_ROOT` is unset. Directories are created with mode
`0700` and files with mode `0600` when the local platform permits it.

## Delivery model

Messages are RFC 5322 files with an `application/json` body. A publisher writes
each recipient copy into that recipient's `tmp/` and atomically renames it into
`new/`. Processing moves it to `cur/`. The immutable `Message-ID` is shared by
all recipient copies and acknowledgements reference it through `reply_to`.

The JSON payload contains:

```json
{
  "version": 1,
  "id": "UUID",
  "sender": "codex-builder",
  "recipients": ["claude-reviewer"],
  "topic": "project.demo",
  "created_at": "ISO-8601 UTC",
  "type": "handoff",
  "authority": "read-only",
  "subject": "ABC-1 handoff",
  "reply_to": null,
  "expires_at": null,
  "content": {
    "summary": "Human-readable status",
    "requestedAction": "Exact expected response",
    "evidence": ["/absolute/or/repository-relative/path"]
  }
}
```

The `authority` field describes the sender's understanding. It never expands the
recipient's authorization. The recipient must independently apply its governing
instructions and current human authority.

## Topic selection

Use stable, lowercase namespaces:

- `company.<company>` for company-wide notices.
- `project.<project>` for project coordination.
- `role.<role>` for the current holder of a responsibility.
- `ticket.<identifier>` for bounded implementation or review work.
- `alerts.<domain>` for actionable monitoring events.

Subscriptions use `fnmatch` patterns. `ticket.abc-*` matches any normalized ABC
ticket topic. `role.*` subscriptions are deliberately forbidden: every exact
`role.<role>` topic has at most one registered holder, and responsibility must be
transferred by unsubscribing the old holder before subscribing the new one.
Publication fails when a topic has no subscribers unless
`--allow-no-subscribers` is explicitly supplied. Topic messages do not deliver
back to the sender unless the sender is also a direct `--to` recipient.

`publish --broadcast` and the reserved topic `all` route to every registered
agent except the sender under the same lock used for registration. Explicit
`--to` recipients are deduplicated. `--broadcast` cannot be combined with a
different topic, and subscriptions to `all` are rejected.

## Delivery and acknowledgement rules

1. Register every stable logical agent ID before subscribing or publishing.
2. Prefer a topic for discovery and a direct recipient for accountability.
3. Include an exact requested action, relevant evidence paths, and known limits.
4. The recipient validates current state before acting.
5. ACK only after processing or explicit rejection.
6. Retry publication with a new message only when no durable recipient copy was
   created. Do not reuse an ambiguous message ID.
7. Keep secrets out of both messages and evidence paths.

## Orchestrator lease

`leases/<name>.json` contains the name, owner, opaque lease ID, acquisition
time, heartbeat time, and expiry. The default name is `orchestrator`. Locking
and atomic replacement serialize updates.

- Acquisition succeeds only when the lease is absent or expired.
- Heartbeat and release require both matching owner and lease ID.
- No force operation exists.
- A malformed lease blocks acquisition with an exact `lease-repair --name`
  path; repair quarantines the malformed file instead of overwriting it.
- An expired lease permits acquisition but does not prove the old orchestrator
  stopped. The new owner must verify live workers, commands, worktrees, and
  provider mutations before dispatching anything.

## Delivery into a session

Maildir is durable storage. Everything below is a wake signal layered on top of
it and never replaces it: a message is delivered when it is acknowledged, not
when a session is told about it.

### Session attachment

`sessions/<session-id>.json` lists the agent ids one Claude Code session
answers for. `attach` adds an id to the current session
(`CLAUDE_CODE_SESSION_ID`) and removes it from every other session file, so an
agent is attached to at most one session and the latest `attach` wins.
`sessions/<session-id>.surfaced.json` holds the ids of unread messages that
session has already been shown.

### Hooks

`agent-maildir-hook` resolves the session from the hook input's
`session_id` and its agents from the session file, the `AGENT_MAILDIR_AGENT`
environment variable, and any agent bound by the same session's channel
server.

- Plain mode on `SessionStart`/`UserPromptSubmit` returns unread mail as
  `additionalContext`; on `Stop` it returns `decision: block` with the full
  text of unread messages the session has not been shown. It never blocks when
  `stop_hook_active` is set.
- `--watch` mode is registered with `asyncRewake: true`. It takes a
  per-session lock (so a session has one watcher), then polls the unread
  folder. On an unshown message it records it as shown, writes the message to
  stderr, and exits 2, which wakes the model. It exits 0 when the session has
  no agents, when the Claude Code process is gone, or after 24 hours. While a
  confirmed channel is delivering for the session it stays quiet.

### Channel

`agent-maildir-mcp` is a stdio MCP server that declares the
`claude/channel` capability. For every bound agent it watches `new/` and emits
one `notifications/claude/channel` event per message, with `content` holding
the headers and JSON body and `meta` holding `agent`, `message_id`, `sender`,
`type`, and when present `topic`, `reply_to`, and `redelivery`.

- On start it pushes everything already in `new/`, then each new file once, and
  again with an incremented `redelivery` count after the redelivery interval.
- A file that cannot be parsed is moved to `bad/` and never pushed as a
  message; one alert event names it. A file that disappears during a concurrent
  ACK is skipped. Mail from an unregistered sender is withheld by the channel,
  but can still be archived safely by `ack`.
- It refreshes `presence/<agent>.json` (process ids, push state, timestamps)
  every few seconds and removes it on exit.
- Claude Code gives no delivery receipt and silently drops events for a session
  that did not enable the channel. The server therefore starts at `off` (the
  session's command line has no channel flag) or `unconfirmed`, sends one
  `probe` event carrying a nonce, and becomes `confirmed` only when the model
  calls `mailbox_confirm_push` with that nonce.

### What a sender is told

`publish` reports per recipient: `listening` (confirmed channel), `bound` (an
attached session or an unconfirmed channel), or `queued`. None of these proves
the message was read; the acknowledgement does. Acknowledging a message of type
`ack` archives it without sending another ACK, so two agents cannot acknowledge
each other in a loop.

Nothing reaches a session that is not running, and nothing wakes Codex. A
separately authorized broker (for example a `launchd` job watching `new/`)
could start or notify a harness; it must never execute message bodies as
shell, infer authority from a topic, expose credentials, or bypass the
orchestrator lease.

## Trust boundary and portability

This implementation is intended for harnesses running under the same trusted
Unix account on one host. Filesystem permissions provide confidentiality from
other local users, but they do not cryptographically authenticate the sender:
any process with write access to the mailbox root can forge a message. Never
elevate authority from a message header, and independently verify consequential
requests. Waking a session raises the stakes of that rule: a delivered message
lands directly in Claude's context and starts a turn, so it is a prompt-injection
path for anything that can write to the mailbox root. The channel's
instructions tell Claude that a message is evidence and never authority, and
the tools refuse to speak for an agent the session is not bound to, but the
real boundary is still the filesystem permissions on the root.

Python 3.9 or newer on Linux and macOS is supported. Windows is unsupported
because locking uses `fcntl`. That locking is reliable for the intended local
filesystem. Do not point
`AGENT_MAILDIR_ROOT` at an arbitrary network share and assume equivalent locking
or durability. For cross-machine messaging, put a separately reviewed broker in
front of the same protocol and add authenticated transport and sender signing.
