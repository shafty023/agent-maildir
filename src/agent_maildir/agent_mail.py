#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Durable Maildir messaging, topic routing, ACKs, and an orchestrator lease."""

from __future__ import annotations

import argparse
import contextlib
import email.policy
import email.utils
import fcntl
import fnmatch
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path
from typing import Any, Iterator


VERSION = 1
DEFAULT_ROOT = Path("~/.local/share/agent-maildir").expanduser()
AGENT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9._*-]{0,127}$")
ACK_STATUSES = {"processed", "rejected", "deferred"}
PRESENCE_TTL_SECONDS = 15
DEFAULT_MAX_MESSAGE_BYTES = 1024 * 1024


class MalformedMessageError(ValueError):
    """A message cannot satisfy the version-1 wire contract."""


class UnsupportedMessageVersion(ValueError):
    """A well-formed message uses a wire version this reader does not support."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def positive_ttl(value: int) -> int:
    if value < 1:
        raise ValueError("TTL must be positive")
    return value


def max_message_bytes() -> int:
    raw = os.environ.get("AGENT_MAILDIR_MAX_MESSAGE_BYTES", str(DEFAULT_MAX_MESSAGE_BYTES))
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError("AGENT_MAILDIR_MAX_MESSAGE_BYTES must be a positive integer") from error
    if value < 1:
        raise ValueError("AGENT_MAILDIR_MAX_MESSAGE_BYTES must be a positive integer")
    return value


def mailbox_message_limit(root: Path) -> int:
    protocol = root / "protocol.json"
    if not protocol.exists():
        return DEFAULT_MAX_MESSAGE_BYTES
    data = load_json(protocol)
    value = data.get("max_message_bytes", DEFAULT_MAX_MESSAGE_BYTES)
    if not isinstance(value, int) or value < 1:
        raise ValueError(f"protocol max_message_bytes must be a positive integer: {protocol}")
    return value


def message_limit_for_path(path: Path) -> int:
    for parent in path.parents:
        if (parent / "protocol.json").is_file():
            return mailbox_message_limit(parent)
    return DEFAULT_MAX_MESSAGE_BYTES


def root_path() -> Path:
    configured = os.environ.get("AGENT_MAILDIR_ROOT") or os.environ.get("AGENT_MAILBOX_ROOT")
    return Path(configured or str(DEFAULT_ROOT)).expanduser().resolve()


def secure_dir(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"refusing symlink for owned directory: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise ValueError(f"owned directory path is not a directory: {path}")
    with contextlib.suppress(OSError):
        if stat.S_IMODE(path.stat().st_mode) != 0o700:
            path.chmod(0o700)


def secure_file(path: Path) -> None:
    if path.is_symlink():
        return
    with contextlib.suppress(OSError):
        if stat.S_IMODE(path.lstat().st_mode) != 0o600:
            path.chmod(0o600)


def fsync_dir(path: Path) -> None:
    """Persist directory-entry changes on filesystems that support directory fsync."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_bytes(path: Path, data: bytes) -> None:
    secure_dir(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    tmp = Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        fsync_dir(path.parent)
        secure_file(path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def atomic_json(path: Path, value: Any) -> None:
    atomic_bytes(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode())


def ensure_layout(root: Path) -> None:
    for path in (root, root / "registry", root / "mailboxes", root / "leases", root / "journal", root / "presence", root / "sessions"):
        secure_dir(path)
    protocol = root / "protocol.json"
    if not protocol.exists():
        atomic_json(
            protocol,
            {
                "name": "agent-maildir",
                "version": VERSION,
                "created_at": iso(),
                "max_message_bytes": max_message_bytes(),
            },
        )
    for name, default in (("agents.json", {"agents": {}}), ("subscriptions.json", {"subscriptions": []})):
        path = root / "registry" / name
        if not path.exists():
            atomic_json(path, default)
    lock = root / ".lock"
    lock.touch(mode=0o600, exist_ok=True)
    secure_file(lock)


def validate_agent(value: str) -> str:
    normalized = value.strip().lower()
    if not AGENT_RE.fullmatch(normalized):
        raise ValueError(f"invalid agent id: {value!r}")
    return normalized


def validate_topic(value: str, pattern: bool = False) -> str:
    normalized = value.strip().lower()
    if not TOPIC_RE.fullmatch(normalized) or (not pattern and "*" in normalized):
        raise ValueError(f"invalid {'topic pattern' if pattern else 'topic'}: {value!r}")
    return normalized


def mailbox(root: Path, agent: str) -> Path:
    validate_agent(agent)
    base = root / "mailboxes" / agent / "Maildir"
    for name in ("tmp", "new", "cur", "bad"):
        secure_dir(base / name)
    return base


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


@contextlib.contextmanager
def locked(root: Path) -> Iterator[None]:
    ensure_layout(root)
    with (root / ".lock").open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def journal(root: Path, event: str, **metadata: Any) -> None:
    path = root / "journal" / "events.ndjson"
    secure_dir(path.parent)
    line = json.dumps({"at": iso(), "event": event, **metadata}, sort_keys=True) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def registered_agents(root: Path) -> dict[str, Any]:
    data = load_json(root / "registry" / "agents.json")
    agents = data.get("agents")
    if not isinstance(agents, dict):
        raise ValueError("agents registry is malformed")
    return agents


def require_registered(root: Path, agent: str) -> dict[str, Any]:
    agents = registered_agents(root)
    if agent not in agents:
        raise ValueError(f"agent is not registered: {agent}")
    return agents[agent]


def presence_file(root: Path, agent: str) -> Path:
    return root / "presence" / f"{validate_agent(agent)}.json"


def listener_state(root: Path, agent: str) -> dict[str, Any] | None:
    """Return the push listener record for an agent when it is fresh and its process is alive."""
    try:
        value = load_json(presence_file(root, agent))
        age = (utc_now() - parse_iso(str(value.get("updated_at", "")))).total_seconds()
        pid = int(value.get("pid", 0))
    except (OSError, ValueError, TypeError):
        return None
    if age > PRESENCE_TTL_SECONDS or pid < 1:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass
    return value


def claude_session_pid() -> int | None:
    """Process id of the Claude Code session this process runs under, if any.

    The channel server and the hooks of one session share it, which is how a
    hook finds the agents its own session is bound to.
    """
    value = os.environ.get("CLAUDE_PID", "")
    if value.isdigit():
        return int(value)
    pid = os.getppid()
    for _ in range(6):
        if pid <= 1:
            return None
        try:
            out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout.split(None, 1)
        except (OSError, subprocess.SubprocessError):
            return None
        if len(out) < 2:
            return None
        if os.path.basename(out[1].strip()) == "claude":
            return pid
        pid = int(out[0])
    return None


def session_agents(root: Path) -> list[str]:
    """Agents bound by the channel server of the current Claude Code session."""
    pid = claude_session_pid()
    if pid is None:
        return []
    found = []
    for agent in sorted(registered_agents(root)):
        state = listener_state(root, agent)
        if state and state.get("claude_pid") == pid:
            found.append(agent)
    return found


def session_file(root: Path, session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id)[:128]
    if not safe:
        raise ValueError("session id is empty")
    return root / "sessions" / f"{safe}.json"


def attached_agents(root: Path, session_id: str) -> list[str]:
    """Agent ids a Claude Code session answers for (see cmd_attach)."""
    try:
        agents = load_json(session_file(root, session_id)).get("agents", [])
    except (OSError, ValueError):
        return []
    return [agent for agent in agents if isinstance(agent, str)]


def attached_sessions(root: Path, agent: str) -> list[str]:
    found = []
    for path in sorted((root / "sessions").glob("*.json")):
        if path.name.endswith(".surfaced.json"):
            continue
        try:
            if agent in load_json(path).get("agents", []):
                found.append(path.stem)
        except (OSError, ValueError):
            continue
    return found


def push_state(root: Path, agent: str) -> str:
    """queued: no session answers for this id. bound: a session is attached and will be
    told about the message, but whether it is running right now is not known.
    listening: a running session has confirmed that pushed messages reach it."""
    state = listener_state(root, agent)
    if state and state.get("push") == "confirmed":
        return "listening"
    if state or attached_sessions(root, agent):
        return "bound"
    return "queued"


def wait_for_reply(root: Path, agent: str, message_id: str, timeout: float) -> dict[str, Any] | None:
    """Block until a message replying to message_id reaches the agent's unread folder."""
    folder = mailbox(root, agent) / "new"
    deadline = time.monotonic() + timeout
    checked: set[str] = set()
    while True:
        for path in sorted(folder.iterdir()):
            if path.name in checked or not path.is_file():
                continue
            checked.add(path.name)
            payload = safe_parse_message(path)
            if payload and payload.get("reply_to") == message_id:
                return payload
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.5)


def cmd_init(args: argparse.Namespace) -> dict[str, Any]:
    root = root_path()
    ensure_layout(root)
    return {"ok": True, "root": str(root), "version": VERSION}


def cmd_register(args: argparse.Namespace) -> dict[str, Any]:
    root = root_path()
    agent = validate_agent(args.agent)
    with locked(root):
        path = root / "registry" / "agents.json"
        data = load_json(path)
        agents = data.setdefault("agents", {})
        prior = agents.get(agent, {})
        agents[agent] = {
            "id": agent,
            "harness": args.harness,
            "role": args.role,
            "registered_at": prior.get("registered_at", iso()),
            "updated_at": iso(),
        }
        atomic_json(path, data)
        mailbox(root, agent)
        journal(root, "agent.registered", agent=agent, harness=args.harness, role=args.role)
    return {"ok": True, "agent": agents[agent], "maildir": str(mailbox(root, agent))}


def cmd_agents(args: argparse.Namespace) -> dict[str, Any]:
    root = root_path()
    ensure_layout(root)
    return {"agents": registered_agents(root)}


def cmd_subscribe(args: argparse.Namespace) -> dict[str, Any]:
    root = root_path()
    agent = validate_agent(args.agent)
    topic = validate_topic(args.topic, pattern=True)
    if topic == "all":
        raise ValueError("topic 'all' is reserved for broadcast and cannot be subscribed")
    with locked(root):
        require_registered(root, agent)
        path = root / "registry" / "subscriptions.json"
        data = load_json(path)
        items = data.setdefault("subscriptions", [])
        if not isinstance(items, list):
            raise ValueError("subscriptions registry is malformed")
        if topic.startswith("role.") and "*" in topic:
            raise ValueError("role topics must be exact and exclusively owned")
        if topic.startswith("role."):
            holders = {item.get("agent") for item in items if item.get("pattern") == topic}
            if holders and holders != {agent}:
                raise ValueError(f"exclusive role topic already held by {sorted(holders)}")
        entry = {"agent": agent, "pattern": topic}
        if entry not in items:
            items.append(entry)
            items.sort(key=lambda item: (item["pattern"], item["agent"]))
            atomic_json(path, data)
            journal(root, "topic.subscribed", agent=agent, pattern=topic)
    return {"ok": True, "agent": agent, "pattern": topic}


def cmd_unsubscribe(args: argparse.Namespace) -> dict[str, Any]:
    root = root_path()
    agent = validate_agent(args.agent)
    topic = validate_topic(args.topic, pattern=True)
    with locked(root):
        path = root / "registry" / "subscriptions.json"
        data = load_json(path)
        items = data.get("subscriptions", [])
        kept = [item for item in items if not (item.get("agent") == agent and item.get("pattern") == topic)]
        removed = len(items) - len(kept)
        data["subscriptions"] = kept
        atomic_json(path, data)
        if removed:
            journal(root, "topic.unsubscribed", agent=agent, pattern=topic)
    return {"ok": True, "removed": removed, "agent": agent, "pattern": topic}


def cmd_subscriptions(args: argparse.Namespace) -> dict[str, Any]:
    root = root_path()
    ensure_layout(root)
    return load_json(root / "registry" / "subscriptions.json")


def body_content(args: argparse.Namespace) -> dict[str, Any]:
    if args.body is not None and args.body_file is not None:
        raise ValueError("use only one of --body or --body-file")
    if args.body_file:
        value = json.loads(Path(args.body_file).expanduser().read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("body file must contain a JSON object")
        return value
    if args.body:
        try:
            value = json.loads(args.body)
        except json.JSONDecodeError:
            return {"summary": args.body}
        if not isinstance(value, dict):
            raise ValueError("--body JSON must be an object")
        return value
    return {}


def render_message(payload: dict[str, Any]) -> bytes:
    message = EmailMessage(policy=email.policy.SMTP)
    message["From"] = f"{payload['sender']}@local.agent"
    message["To"] = ", ".join(f"{item}@local.agent" for item in payload["recipients"])
    message["Subject"] = payload["subject"]
    message["Date"] = email.utils.format_datetime(parse_iso(payload["created_at"]))
    message["Message-ID"] = f"<{payload['id']}@agent-maildir.local>"
    message["X-Agent-Mail-Version"] = str(VERSION)
    message["X-Agent-From"] = payload["sender"]
    message["X-Agent-Type"] = payload["type"]
    message["X-Agent-Authority"] = payload["authority"]
    if payload.get("topic"):
        message["X-Agent-Topic"] = payload["topic"]
    if payload.get("reply_to"):
        message["In-Reply-To"] = payload["reply_to"]
    message.set_content(json.dumps(payload, indent=2, sort_keys=True), subtype="json", charset="utf-8")
    return message.as_bytes()


def validate_message_payload(payload: dict[str, Any], source: str) -> None:
    version = payload.get("version")
    if version != VERSION:
        raise UnsupportedMessageVersion(f"unsupported message version {version!r}: {source}")
    try:
        message_id = payload["id"]
        sender = payload["sender"]
        recipients = payload["recipients"]
        created_at = payload["created_at"]
        for key in ("type", "authority", "subject"):
            if not isinstance(payload[key], str) or not payload[key]:
                raise ValueError(f"{key} must be a non-empty string")
        if not isinstance(message_id, str):
            raise ValueError("id must be a UUID string")
        uuid.UUID(message_id)
        if not isinstance(sender, str):
            raise ValueError("sender must be a string")
        validate_agent(sender)
        if not isinstance(recipients, list) or not recipients:
            raise ValueError("recipients must be a non-empty list")
        for recipient in recipients:
            if not isinstance(recipient, str):
                raise ValueError("recipient ids must be strings")
            validate_agent(recipient)
        if not isinstance(created_at, str):
            raise ValueError("created_at must be a timestamp string")
        parse_iso(created_at)
        if not isinstance(payload.get("content"), dict):
            raise ValueError("content must be a JSON object")
        topic = payload.get("topic")
        if topic is not None:
            validate_topic(str(topic))
        reply_to = payload.get("reply_to")
        if reply_to is not None and not isinstance(reply_to, str):
            raise ValueError("reply_to must be a string or null")
        expires = payload.get("expires_at")
        if expires is not None:
            if not isinstance(expires, str):
                raise ValueError("expires_at must be a timestamp string or null")
            parse_iso(expires)
    except (KeyError, TypeError, ValueError) as error:
        raise MalformedMessageError(f"invalid message {source}: {error}") from error


def deliver(root: Path, recipient: str, payload: dict[str, Any]) -> Path:
    base = mailbox(root, recipient)
    name = f"{time.time_ns()}.{payload['id']}.{os.getpid()}.agent-mail"
    tmp = base / "tmp" / name
    final = base / "new" / name
    validate_message_payload(payload, "outgoing payload")
    rendered = render_message(payload)
    limit = mailbox_message_limit(root)
    if len(rendered) > limit:
        raise ValueError(f"message exceeds size limit of {limit} bytes")
    atomic_bytes(tmp, rendered)
    os.replace(tmp, final)
    fsync_dir(base / "tmp")
    fsync_dir(base / "new")
    secure_file(final)
    return final


def cmd_publish(args: argparse.Namespace) -> dict[str, Any]:
    root = root_path()
    sender = validate_agent(args.from_agent)
    topic = validate_topic(args.topic) if args.topic else None
    broadcast = bool(getattr(args, "broadcast", False))
    if broadcast and topic:
        raise ValueError("--broadcast cannot be combined with --topic; use one or topic 'all'")
    broadcast = broadcast or topic == "all"
    direct = {validate_agent(item) for item in (args.to or [])}
    if args.expires_at:
        parse_iso(args.expires_at)
    with locked(root):
        require_registered(root, sender)
        agents = registered_agents(root)
        for agent in direct:
            require_registered(root, agent)
        recipients = set(direct)
        if broadcast:
            recipients.update(agents)
            recipients.discard(sender)
        elif topic:
            subscriptions = load_json(root / "registry" / "subscriptions.json").get("subscriptions", [])
            for item in subscriptions:
                candidate = item.get("agent")
                pattern = item.get("pattern")
                if candidate in agents and isinstance(pattern, str) and fnmatch.fnmatchcase(topic, pattern):
                    if candidate != sender or candidate in direct:
                        recipients.add(candidate)
        if not recipients and not args.allow_no_subscribers:
            raise ValueError("message has no recipients")
        message_id = str(uuid.uuid4())
        payload = {
            "version": VERSION,
            "id": message_id,
            "sender": sender,
            "recipients": sorted(recipients),
            "topic": topic,
            "created_at": iso(),
            "type": args.type,
            "authority": args.authority,
            "subject": args.subject,
            "reply_to": args.reply_to,
            "expires_at": args.expires_at,
            "content": body_content(args),
        }
        paths = {recipient: str(deliver(root, recipient, payload)) for recipient in sorted(recipients)}
        journal(root, "message.published", message_id=message_id, sender=sender, recipients=sorted(recipients), topic=topic, type=args.type)
        push = {recipient: push_state(root, recipient) for recipient in sorted(recipients)}
    result: dict[str, Any] = {"ok": True, "message_id": message_id, "recipients": sorted(recipients), "paths": paths, "push": push}
    wait = getattr(args, "wait_seconds", 0) or 0
    if wait > 0:
        reply = wait_for_reply(root, sender, message_id, wait)
        result["reply"] = reply
        result["timed_out"] = reply is None
    return result


def parse_message(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise MalformedMessageError(f"message path is a symlink: {path}")
    try:
        size = path.stat().st_size
        limit = message_limit_for_path(path)
        if size > limit:
            raise MalformedMessageError(f"message exceeds size limit of {limit} bytes: {path}")
        with path.open("rb") as handle:
            message = BytesParser(policy=email.policy.default).parse(handle)
        raw = message.get_content()
        if not isinstance(raw, str):
            raw = raw.decode("utf-8")
        payload = json.loads(raw)
    except OSError:
        raise
    except MalformedMessageError:
        raise
    except Exception as error:
        raise MalformedMessageError(f"cannot parse message {path}: {error}") from error
    if not isinstance(payload, dict):
        raise MalformedMessageError(f"message body must be a JSON object: {path}")
    validate_message_payload(payload, str(path))
    payload["_path"] = str(path)
    expires = payload.get("expires_at")
    payload["_expired"] = bool(expires and parse_iso(expires) <= utc_now())
    return payload


def quarantine_message(path: Path) -> Path | None:
    """Move an unread file that is not a valid message out of the delivery path."""
    bad = path.parent.parent / "bad"
    secure_dir(bad)
    destination = bad / f"{path.name}.{time.time_ns()}.bad"
    try:
        os.replace(path, destination)
    except FileNotFoundError:
        return None
    fsync_dir(path.parent)
    fsync_dir(bad)
    secure_file(destination)
    return destination


def safe_parse_message(path: Path) -> dict[str, Any] | None:
    """Parse one message, skipping concurrent removals and quarantining bad files."""
    try:
        return parse_message(path)
    except FileNotFoundError:
        return None
    except UnsupportedMessageVersion:
        return None
    except MalformedMessageError:
        quarantine_message(path)
        return None
    except OSError:
        return None


def find_message(root: Path, agent: str, message_id: str, include_cur: bool = True) -> tuple[Path, dict[str, Any]]:
    base = mailbox(root, agent)
    folders = [base / "new"] + ([base / "cur"] if include_cur else [])
    for folder in folders:
        for path in sorted(folder.iterdir()):
            if path.is_file():
                payload = safe_parse_message(path)
                if payload is None:
                    continue
                if payload.get("id") == message_id:
                    return path, payload
    raise ValueError(f"message not found for {agent}: {message_id}")


def cmd_inbox(args: argparse.Namespace) -> dict[str, Any]:
    root = root_path()
    agent = validate_agent(args.agent)
    ensure_layout(root)
    if args.limit < 1:
        raise ValueError("inbox limit must be positive")
    require_registered(root, agent)
    topic = validate_topic(args.topic) if args.topic else None
    messages: list[dict[str, Any]] = []
    offset = max(0, int(getattr(args, "offset", 0)))
    matched = 0
    for path in sorted((mailbox(root, agent) / "new").iterdir()):
        if path.is_file():
            payload = safe_parse_message(path)
            if payload is None:
                continue
            if topic is None or payload.get("topic") == topic:
                if matched >= offset:
                    messages.append(payload)
                matched += 1
        if len(messages) >= args.limit:
            break
    return {"agent": agent, "count": len(messages), "messages": messages}


def cmd_read(args: argparse.Namespace) -> dict[str, Any]:
    root = root_path()
    agent = validate_agent(args.agent)
    _, payload = find_message(root, agent, args.message_id)
    return payload


def cmd_ack(args: argparse.Namespace) -> dict[str, Any]:
    root = root_path()
    agent = validate_agent(args.agent)
    with locked(root):
        path, original = find_message(root, agent, args.message_id, include_cur=False)
        sender = validate_agent(str(original["sender"]))
        sender_registered = sender in registered_agents(root)
        destination = mailbox(root, agent) / "cur" / f"{path.name}:2,S"
        os.replace(path, destination)
        fsync_dir(path.parent)
        fsync_dir(destination.parent)
        if original.get("type") == "ack":
            # Archiving an ACK never answers with another ACK; two push-driven
            # agents would otherwise acknowledge each other forever.
            journal(root, "message.archived", message_id=original["id"], agent=agent)
            return {"ok": True, "message_id": original["id"], "ack_id": None, "archived": str(destination), "ack_path": None}
        if not sender_registered:
            journal(root, "message.archived", message_id=original["id"], agent=agent, sender_unregistered=sender)
            return {"ok": True, "message_id": original["id"], "ack_id": None, "archived": str(destination), "ack_path": None}
        ack_id = str(uuid.uuid4())
        payload = {
            "version": VERSION,
            "id": ack_id,
            "sender": agent,
            "recipients": [sender],
            "topic": original.get("topic"),
            "created_at": iso(),
            "type": "ack",
            "authority": "informational",
            "subject": f"ACK: {original.get('subject', original['id'])}",
            "reply_to": original["id"],
            "expires_at": None,
            "content": {"status": args.status, "note": args.note},
        }
        ack_path = deliver(root, sender, payload)
        journal(root, "message.acknowledged", message_id=original["id"], ack_id=ack_id, agent=agent, status=args.status)
    return {"ok": True, "message_id": original["id"], "ack_id": ack_id, "archived": str(destination), "ack_path": str(ack_path)}


def cmd_join(args: argparse.Namespace) -> dict[str, Any]:
    """Register an agent for a role and subscribe it, so others can reach it by role topic."""
    root = root_path()
    role_topic = validate_topic(f"role.{args.role.strip().lower()}")
    role = role_topic.split(".", 1)[1]
    agent = validate_agent(args.agent or f"{args.harness}-{role}")
    topics = [role_topic]
    topics += [validate_topic(f"project.{item.strip().lower()}") for item in (args.project or [])]
    topics += [validate_topic(item, pattern=True) for item in (args.topic or [])]
    with locked(root):
        agents_path = root / "registry" / "agents.json"
        agents_data = load_json(agents_path)
        agents = agents_data.setdefault("agents", {})
        if not isinstance(agents, dict):
            raise ValueError("agents registry is malformed")
        prior = agents.get(agent)
        if prior and prior.get("harness") != args.harness:
            raise ValueError(f"agent id {agent} is registered to the {prior.get('harness')} harness")

        subscriptions_path = root / "registry" / "subscriptions.json"
        subscriptions_data = load_json(subscriptions_path)
        items = subscriptions_data.setdefault("subscriptions", [])
        if not isinstance(items, list):
            raise ValueError("subscriptions registry is malformed")
        holders = sorted({item.get("agent") for item in items if item.get("pattern") == role_topic} - {agent})
        if holders:
            return {
                "ok": False,
                "agent": agent,
                "role": role,
                "role_held_by": holders,
                "error": f"{role_topic} is held by {holders}; the holder must unsubscribe before {agent} can take the role",
            }

        now = iso()
        agents[agent] = {
            "id": agent,
            "harness": args.harness,
            "role": role,
            "registered_at": prior.get("registered_at", now) if prior else now,
            "updated_at": now,
        }
        held = {item.get("pattern") for item in items if item.get("agent") == agent}
        added = [topic for topic in topics if topic not in held]
        for topic in added:
            items.append({"agent": agent, "pattern": topic})
        items.sort(key=lambda item: (item["pattern"], item["agent"]))
        atomic_json(agents_path, agents_data)
        atomic_json(subscriptions_path, subscriptions_data)
        mailbox(root, agent)
        if prior is None:
            journal(root, "agent.registered", agent=agent, harness=args.harness, role=role)
        for topic in added:
            journal(root, "topic.subscribed", agent=agent, pattern=topic)
    announced: list[str] = []
    changed = prior is None or role_topic in added
    if args.announce or (changed and not args.no_announce):
        for topic in topics:
            if not topic.startswith("project."):
                continue
            sent = cmd_publish(
                argparse.Namespace(
                    from_agent=agent, to=None, topic=topic, subject=f"{agent} online as {role}", type="status",
                    authority="informational", reply_to=None, expires_at=None, body_file=None,
                    body=json.dumps({"summary": f"{agent} ({args.harness}) now holds {role_topic}.", "agent": agent, "role": role, "reach": [role_topic, agent], "requestedAction": "none"}),
                    allow_no_subscribers=True, wait_seconds=0,
                )
            )
            if sent["recipients"]:
                announced.append(sent["message_id"])
    attached = None
    if getattr(args, "attach", False):
        attached = cmd_attach(argparse.Namespace(agent=agent, session=getattr(args, "session", None)))
    return {
        "ok": True,
        "agent": agent,
        "harness": args.harness,
        "role": role,
        "attached": attached,
        "new": prior is None,
        "topics": topics,
        "subscribed_now": added,
        "announced": announced,
        "push": push_state(root, agent),
        "reach": {"role_topic": role_topic, "direct": agent},
    }


def current_session(args: argparse.Namespace) -> str:
    session = getattr(args, "session", None) or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not session:
        raise ValueError("no session id: run inside Claude Code or pass --session")
    return session


def cmd_attach(args: argparse.Namespace) -> dict[str, Any]:
    """Make one Claude Code session the place an agent's mail is delivered."""
    root = root_path()
    agent = validate_agent(args.agent)
    session = current_session(args)
    with locked(root):
        require_registered(root, agent)
        mine = session_file(root, session)
        moved_from = []
        for other in attached_sessions(root, agent):
            path = root / "sessions" / f"{other}.json"
            if path == mine:
                continue
            data = load_json(path)
            data["agents"] = [item for item in data.get("agents", []) if item != agent]
            atomic_json(path, data)
            moved_from.append(other)
        agents = attached_agents(root, session)
        if agent not in agents:
            agents.append(agent)
        atomic_json(mine, {"session": session, "agents": agents, "updated_at": iso()})
        journal(root, "session.attached", agent=agent, session=session, moved_from=moved_from)
    return {"ok": True, "agent": agent, "session": session, "agents": agents, "moved_from": moved_from}


def cmd_detach(args: argparse.Namespace) -> dict[str, Any]:
    root = root_path()
    agent = validate_agent(args.agent)
    session = current_session(args)
    with locked(root):
        agents = [item for item in attached_agents(root, session) if item != agent]
        atomic_json(session_file(root, session), {"session": session, "agents": agents, "updated_at": iso()})
        journal(root, "session.detached", agent=agent, session=session)
    return {"ok": True, "agent": agent, "session": session, "agents": agents}


def cmd_unregister(args: argparse.Namespace) -> dict[str, Any]:
    """Remove registration and routing; retain archived mail unless explicitly discarded."""
    root = root_path()
    agent = validate_agent(args.agent)
    with locked(root):
        require_registered(root, agent)
        for lease_path in sorted((root / "leases").glob("*.json")):
            try:
                lease = current_lease(root, lease_path.stem)
            except ValueError:
                continue
            if lease and lease["active"] and lease.get("owner") == agent:
                raise ValueError(f"{agent} holds lease {lease_path.stem!r}; release it first")
        base = mailbox(root, agent)
        unread = [path for path in (base / "new").iterdir() if path.is_file()]
        if unread and not args.discard_mail:
            raise ValueError(f"{agent} has {len(unread)} unread message(s); pass --discard-mail to remove them")
        path = root / "registry" / "agents.json"
        data = load_json(path)
        data["agents"].pop(agent, None)
        atomic_json(path, data)
        path = root / "registry" / "subscriptions.json"
        data = load_json(path)
        data["subscriptions"] = [item for item in data.get("subscriptions", []) if item.get("agent") != agent]
        atomic_json(path, data)
        for other in attached_sessions(root, agent):
            path = root / "sessions" / f"{other}.json"
            data = load_json(path)
            data["agents"] = [item for item in data.get("agents", []) if item != agent]
            atomic_json(path, data)
        with contextlib.suppress(FileNotFoundError):
            presence_file(root, agent).unlink()
        retained_archive = str(base / "cur")
        if args.discard_mail:
            for folder in ("tmp", "new", "cur", "bad"):
                for item in (base / folder).iterdir():
                    item.unlink()
                (base / folder).rmdir()
            base.rmdir()
            base.parent.rmdir()
            retained_archive = None
        journal(root, "agent.unregistered", agent=agent, retained_archive=retained_archive)
    return {"ok": True, "unregistered": agent, "retained_archive": retained_archive}


def cmd_wait(args: argparse.Namespace) -> dict[str, Any]:
    root = root_path()
    agent = validate_agent(args.agent)
    ensure_layout(root)
    require_registered(root, agent)
    if args.timeout_seconds < 0:
        raise ValueError("timeout must not be negative")
    reply = wait_for_reply(root, agent, args.reply_to, args.timeout_seconds)
    return {"ok": reply is not None, "timed_out": reply is None, "reply_to": args.reply_to, "reply": reply}


def cmd_listeners(args: argparse.Namespace) -> dict[str, Any]:
    root = root_path()
    ensure_layout(root)
    listeners = {agent: state for agent in sorted(registered_agents(root)) if (state := listener_state(root, agent))}
    return {"listeners": listeners}


def lease_file(root: Path, name: str = "orchestrator") -> Path:
    return root / "leases" / f"{validate_agent(name)}.json"


def current_lease(root: Path, name: str = "orchestrator") -> dict[str, Any] | None:
    name = validate_agent(name)
    path = lease_file(root, name)
    if not path.exists():
        return None
    try:
        value = load_json(path)
        validate_agent(str(value["owner"]))
        parse_iso(str(value["expires_at"]))
        if not isinstance(value.get("lease_id"), str):
            raise ValueError("lease_id is missing")
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(
            f"lease {name!r} is malformed: {error}; run `agent-maildir lease-repair --name {name}`"
        ) from error
    value.setdefault("name", name)
    value["active"] = parse_iso(str(value["expires_at"])) > utc_now()
    return value


def cmd_lease_acquire(args: argparse.Namespace) -> dict[str, Any]:
    root = root_path()
    agent = validate_agent(args.agent)
    name = validate_agent(getattr(args, "name", "orchestrator"))
    ttl = positive_ttl(args.ttl_seconds)
    with locked(root):
        require_registered(root, agent)
        prior = current_lease(root, name)
        if prior and prior["active"]:
            raise ValueError(f"lease {name!r} held by {prior['owner']} until {prior['expires_at']}")
        now = utc_now()
        lease = {
            "version": VERSION,
            "name": name,
            "owner": agent,
            "lease_id": str(uuid.uuid4()),
            "acquired_at": iso(now),
            "heartbeat_at": iso(now),
            "expires_at": iso(datetime.fromtimestamp(now.timestamp() + ttl, timezone.utc)),
        }
        atomic_json(lease_file(root, name), lease)
        journal(root, "lease.acquired", name=name, owner=agent, lease_id=lease["lease_id"], expires_at=lease["expires_at"])
    return {"ok": True, "lease": lease}


def cmd_lease_heartbeat(args: argparse.Namespace) -> dict[str, Any]:
    root = root_path()
    agent = validate_agent(args.agent)
    name = validate_agent(getattr(args, "name", "orchestrator"))
    ttl = positive_ttl(args.ttl_seconds)
    with locked(root):
        lease = current_lease(root, name)
        if not lease or not lease["active"] or lease.get("owner") != agent or lease.get("lease_id") != args.lease_id:
            raise ValueError("matching active lease required")
        now = utc_now()
        lease.pop("active", None)
        lease["heartbeat_at"] = iso(now)
        lease["expires_at"] = iso(datetime.fromtimestamp(now.timestamp() + ttl, timezone.utc))
        atomic_json(lease_file(root, name), lease)
        journal(root, "lease.heartbeat", name=name, owner=agent, lease_id=args.lease_id, expires_at=lease["expires_at"])
    return {"ok": True, "lease": lease}


def cmd_lease_release(args: argparse.Namespace) -> dict[str, Any]:
    root = root_path()
    agent = validate_agent(args.agent)
    name = validate_agent(getattr(args, "name", "orchestrator"))
    with locked(root):
        lease = current_lease(root, name)
        if not lease or lease.get("owner") != agent or lease.get("lease_id") != args.lease_id:
            raise ValueError("matching lease required")
        lease_file(root, name).unlink()
        fsync_dir(root / "leases")
        journal(root, "lease.released", name=name, owner=agent, lease_id=args.lease_id)
    return {"ok": True, "name": name, "released": args.lease_id, "owner": agent}


def cmd_lease_status(args: argparse.Namespace) -> dict[str, Any]:
    root = root_path()
    ensure_layout(root)
    name = validate_agent(getattr(args, "name", "orchestrator"))
    return {"lease": current_lease(root, name)}


def cmd_lease_repair(args: argparse.Namespace) -> dict[str, Any]:
    """Quarantine one malformed lease file so the named lease can be acquired again."""
    root = root_path()
    name = validate_agent(getattr(args, "name", "orchestrator"))
    with locked(root):
        path = lease_file(root, name)
        if not path.exists():
            return {"ok": True, "name": name, "repaired": False, "quarantined": None}
        try:
            current_lease(root, name)
        except ValueError:
            bad = root / "leases" / "bad"
            secure_dir(bad)
            destination = bad / f"{name}.{time.time_ns()}.json"
            os.replace(path, destination)
            fsync_dir(root / "leases")
            fsync_dir(bad)
            secure_file(destination)
            journal(root, "lease.repaired", name=name, quarantined=str(destination))
            return {"ok": True, "name": name, "repaired": True, "quarantined": str(destination)}
        raise ValueError(f"lease {name!r} is valid; refusing to repair it")


def insecure(path: Path, expected_directory: bool) -> bool:
    if path.is_symlink():
        return True
    if not path.exists():
        return False
    mode = stat.S_IMODE(path.stat().st_mode)
    expected = 0o700 if expected_directory else 0o600
    return mode != expected


def cmd_doctor(args: argparse.Namespace) -> dict[str, Any]:
    root = root_path()
    ensure_layout(root)
    errors: list[str] = []
    try:
        protocol = load_json(root / "protocol.json")
        if protocol.get("version") != VERSION:
            errors.append("protocol version mismatch")
        mailbox_message_limit(root)
        for path in (root, root / "registry", root / "mailboxes", root / "leases", root / "journal", root / "presence", root / "sessions"):
            if insecure(path, True):
                errors.append(f"directory permission drift: {path}")
        for path in (root / "protocol.json", root / ".lock", root / "registry" / "agents.json", root / "registry" / "subscriptions.json", root / "journal" / "events.ndjson"):
            if insecure(path, False):
                errors.append(f"file permission drift: {path}")
    except Exception as error:
        errors.append(f"protocol: {error}")
    try:
        agents = registered_agents(root)
        for agent in agents:
            if validate_agent(agent) != agent:
                errors.append(f"noncanonical agent id: {agent}")
            base = mailbox(root, agent)
            for folder in ("tmp", "new", "cur", "bad"):
                candidate = base / folder
                if not candidate.is_dir():
                    errors.append(f"missing Maildir folder: {agent}/{folder}")
                elif insecure(candidate, True):
                    errors.append(f"Maildir permission drift: {candidate}")
                for path in candidate.iterdir():
                    if (path.is_file() or path.is_symlink()) and insecure(path, False):
                        errors.append(f"message permission drift: {path}")
                    if folder == "new" and path.is_file() and not path.is_symlink():
                        try:
                            parse_message(path)
                        except UnsupportedMessageVersion as error:
                            errors.append(str(error))
                        except MalformedMessageError as error:
                            errors.append(str(error))
                        except OSError as error:
                            errors.append(f"message temporarily unreadable: {path}: {error}")
                if folder == "bad":
                    quarantined = [path.name for path in candidate.iterdir() if path.is_file() or path.is_symlink()]
                    if quarantined:
                        errors.append(f"quarantined message files for {agent}: {sorted(quarantined)}")
        subscriptions = load_json(root / "registry" / "subscriptions.json").get("subscriptions", [])
        role_holders: dict[str, set[str]] = {}
        for item in subscriptions:
            if item.get("agent") not in agents:
                errors.append(f"subscription has unknown agent: {item}")
            pattern = validate_topic(str(item.get("pattern", "")), pattern=True)
            if pattern.startswith("role.") and "*" not in pattern:
                role_holders.setdefault(pattern, set()).add(str(item.get("agent")))
        for pattern, holders in role_holders.items():
            if len(holders) > 1:
                errors.append(f"exclusive role topic has multiple holders: {pattern} -> {sorted(holders)}")
    except Exception as error:
        errors.append(f"registry: {error}")
    try:
        for path in sorted((root / "leases").glob("*.json")):
            lease = current_lease(root, path.stem)
            if lease and insecure(path, False):
                errors.append(f"lease permission drift: {path}")
    except Exception as error:
        errors.append(f"lease: {error}")
    result: dict[str, Any] = {"ok": not errors, "root": str(root), "errors": errors}
    if getattr(args, "integrations", False):
        try:
            from .setup_command import integration_status
        except ImportError:
            from setup_command import integration_status
        home = Path(os.environ.get("HOME", str(Path.home()))).expanduser().resolve()
        integrations = integration_status(home)
        missing = [
            f"{harness}.{surface}"
            for harness, surfaces in integrations.items()
            for surface in ("skill", "hooks", "mcp")
            if surface in surfaces and surfaces[surface] is not True
        ]
        result["integrations"] = integrations
        result["integration_errors"] = [f"integration not installed: {item}" for item in missing]
        if missing:
            result["ok"] = False
    return result


def cmd_setup(args: argparse.Namespace) -> dict[str, Any]:
    try:
        from .setup_command import run_setup
    except ImportError:
        from setup_command import run_setup
    return run_setup(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="After installation, run: agent-maildir setup --claude --codex",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def command(name: str, function: Any, help_text: str) -> argparse.ArgumentParser:
        item = sub.add_parser(name, help=help_text)
        item.set_defaults(function=function)
        return item

    command("init", cmd_init, "initialize storage")
    item = command("register", cmd_register, "register a stable logical agent")
    item.add_argument("--agent", required=True)
    item.add_argument("--harness", required=True, choices=("codex", "claude", "other"))
    item.add_argument("--role", required=True)
    command("agents", cmd_agents, "list registered agents")
    item = command("subscribe", cmd_subscribe, "subscribe an agent to a topic pattern")
    item.add_argument("--agent", required=True)
    item.add_argument("--topic", required=True)
    item = command("unsubscribe", cmd_unsubscribe, "remove a topic subscription")
    item.add_argument("--agent", required=True)
    item.add_argument("--topic", required=True)
    command("subscriptions", cmd_subscriptions, "list topic subscriptions")

    item = command("publish", cmd_publish, "publish a durable message")
    item.add_argument("--from-agent", required=True)
    item.add_argument("--to", action="append")
    item.add_argument("--topic")
    item.add_argument("--broadcast", action="store_true", help="send to every registered agent except the sender")
    item.add_argument("--subject", required=True)
    item.add_argument("--type", default="message", choices=("message", "status", "decision", "request", "handoff", "alert", "ack"))
    item.add_argument("--authority", default="informational")
    item.add_argument("--reply-to")
    item.add_argument("--expires-at")
    item.add_argument("--body")
    item.add_argument("--body-file")
    item.add_argument("--allow-no-subscribers", action="store_true")
    item.add_argument("--wait-seconds", type=float, default=0, help="block this long for an ACK or reply")
    item = command("inbox", cmd_inbox, "list unread messages")
    item.add_argument("--agent", required=True)
    item.add_argument("--topic")
    item.add_argument("--limit", type=int, default=100)
    item = command("read", cmd_read, "read one message")
    item.add_argument("--agent", required=True)
    item.add_argument("--message-id", required=True)
    item = command("wait", cmd_wait, "block until a reply or ACK to a message arrives")
    item.add_argument("--agent", required=True)
    item.add_argument("--reply-to", required=True)
    item.add_argument("--timeout-seconds", type=float, default=600)
    command("listeners", cmd_listeners, "list agents with a live push listener")
    item = command("join", cmd_join, "register for a role and subscribe to its topics")
    item.add_argument("--role", required=True)
    item.add_argument("--harness", required=True, choices=("codex", "claude", "other"))
    item.add_argument("--agent", help="agent id; defaults to <harness>-<role>")
    item.add_argument("--project", action="append", help="also subscribe to project.<name>; repeatable")
    item.add_argument("--topic", action="append", help="extra topic pattern to subscribe to; repeatable")
    item.add_argument("--announce", action="store_true", help="always announce on the project topics")
    item.add_argument("--no-announce", action="store_true")
    item.add_argument("--attach", action="store_true", help="deliver this agent's mail to the current Claude Code session")
    item.add_argument("--session", help="session id; defaults to CLAUDE_CODE_SESSION_ID")
    for name, function, help_text in (
        ("attach", cmd_attach, "deliver an agent's mail to the current Claude Code session"),
        ("detach", cmd_detach, "stop delivering an agent's mail to the current session"),
    ):
        item = command(name, function, help_text)
        item.add_argument("--agent", required=True)
        item.add_argument("--session", help="session id; defaults to CLAUDE_CODE_SESSION_ID")
    item = command("unregister", cmd_unregister, "remove an agent and its subscriptions")
    item.add_argument("--agent", required=True)
    item.add_argument("--discard-mail", action="store_true")
    item = command("ack", cmd_ack, "archive and acknowledge one unread message")
    item.add_argument("--agent", required=True)
    item.add_argument("--message-id", required=True)
    item.add_argument("--status", default="processed", choices=sorted(ACK_STATUSES))
    item.add_argument("--note", default="")

    for name, function, help_text in (
        ("lease-acquire", cmd_lease_acquire, "acquire the orchestrator lease"),
        ("lease-heartbeat", cmd_lease_heartbeat, "refresh the orchestrator lease"),
        ("lease-release", cmd_lease_release, "release the orchestrator lease"),
    ):
        item = command(name, function, help_text)
        item.add_argument("--name", default="orchestrator", help="lease name (default: orchestrator)")
        item.add_argument("--agent", required=True)
        if name != "lease-acquire":
            item.add_argument("--lease-id", required=True)
        if name != "lease-release":
            item.add_argument("--ttl-seconds", type=int, default=600)
    item = command("lease-status", cmd_lease_status, "show a named lease")
    item.add_argument("--name", default="orchestrator", help="lease name (default: orchestrator)")
    item = command("lease-repair", cmd_lease_repair, "quarantine a malformed lease file")
    item.add_argument("--name", default="orchestrator", help="lease name (default: orchestrator)")
    item = command("setup", cmd_setup, "install or remove skill, hooks, and MCP integration")
    item.add_argument("--claude", action="store_true", help="install the Claude Code skill, hooks, and MCP server")
    item.add_argument("--codex", action="store_true", help="install the Codex skill and MCP config")
    item.add_argument("--mcp", action="store_true", help="register only the Claude Code MCP channel server")
    item.add_argument("--settings", help="Claude Code settings file (default: ~/.claude/settings.json)")
    item.add_argument("--codex-config", help="Codex config file (default: ~/.codex/config.toml)")
    item.add_argument(
        "--project",
        nargs="?",
        const=".",
        help="add removable mailbox guidance to a project (default path: current directory)",
    )
    item.add_argument("--dry-run", action="store_true", help="show changes without writing or running Claude Code")
    item.add_argument("--uninstall", action="store_true", help="remove only additions recorded by setup")
    item = command("doctor", cmd_doctor, "validate storage, registries, and optional integrations")
    item.add_argument("--integrations", action="store_true", help="report Claude Code and Codex discovery setup")
    return parser


def main() -> int:
    try:
        parser = build_parser()
        if len(sys.argv) == 1:
            parser.print_help()
            return 0
        args = parser.parse_args()
        result = args.function(args)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("ok", True) else 1
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
