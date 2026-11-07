#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""End-to-end tests for push delivery. Run: python3 -m unittest discover -v."""

from __future__ import annotations

import json
import fcntl
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / "src" / "agent_maildir"
sys.path.insert(0, str(SOURCE))
import agent_mail as mail  # noqa: E402
import mailbox_channel as channel_module  # noqa: E402
import mailbox_hook as hook_module  # noqa: E402

MAIL = [sys.executable, str(SOURCE / "agent_mail.py")]
CHANNEL = [sys.executable, str(SOURCE / "mailbox_channel.py")]
HOOK = [sys.executable, str(SOURCE / "mailbox_hook.py")]


class Server:
    """Drive mailbox_channel.py over stdio the way Claude Code does."""

    def __init__(self, env: dict[str, str], *args: str) -> None:
        self.process = subprocess.Popen(
            CHANNEL + list(args), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env
        )
        self.lines: queue.Queue[dict] = queue.Queue()
        self.backlog: list[dict] = []
        threading.Thread(target=self._pump, daemon=True).start()
        self.next_id = 0

    def _pump(self) -> None:
        for line in self.process.stdout:
            self.lines.put(json.loads(line))

    def write(self, message: dict) -> None:
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def take(self, match, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        for index, message in enumerate(self.backlog):
            if match(message):
                return self.backlog.pop(index)
        while time.monotonic() < deadline:
            try:
                message = self.lines.get(timeout=0.1)
            except queue.Empty:
                continue
            if match(message):
                return message
            self.backlog.append(message)
        raise AssertionError("expected message did not arrive")

    def request(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        request_id = self.next_id
        self.write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        return self.take(lambda m: m.get("id") == request_id)

    def start(self, client_name: str = "claude-code") -> dict:
        result = self.request(
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": client_name, "version": "0"}},
        )
        self.write({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result["result"]

    def tool(self, name: str, arguments: dict | None = None) -> tuple[dict, bool]:
        result = self.request("tools/call", {"name": name, "arguments": arguments or {}})["result"]
        return json.loads(result["content"][0]["text"]), result["isError"]

    def event(self, timeout: float = 5.0) -> dict:
        """Next pushed mailbox message (push-check probes are a separate stream)."""
        return self.take(
            lambda m: m.get("method") == "notifications/claude/channel" and m["params"]["meta"].get("type") != "probe", timeout
        )["params"]

    def probe(self, timeout: float = 5.0) -> dict:
        return self.take(
            lambda m: m.get("method") == "notifications/claude/channel" and m["params"]["meta"].get("type") == "probe", timeout
        )["params"]

    def close(self) -> None:
        self.process.stdin.close()
        self.process.wait(timeout=5)
        self.process.stdout.close()
        self.process.stderr.close()


class PushTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # A dummy process stands in for the Claude Code session that owns the server.
        self.fake_claude = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)", "--dangerously-load-development-channels"])
        self.env = {**os.environ, "AGENT_MAILDIR_ROOT": self.tmp.name, "CLAUDE_PID": str(self.fake_claude.pid), "CLAUDE_CODE_SESSION_ID": "session-a"}
        self.env.pop("AGENT_MAILDIR_AGENT", None)
        self.mail("init")
        self.mail("register", "--agent", "codex-worker", "--harness", "codex", "--role", "worker")
        self.mail("register", "--agent", "claude-sweep", "--harness", "claude", "--role", "sweep")
        self.servers: list[Server] = []

    def tearDown(self) -> None:
        for server in self.servers:
            if server.process.poll() is None:
                server.close()
        self.fake_claude.kill()
        self.fake_claude.wait()

    def mail(self, *args: str, check: bool = True) -> dict:
        done = subprocess.run(MAIL + list(args), capture_output=True, text=True, env=self.env)
        if check and done.returncode != 0:
            raise AssertionError(done.stderr or done.stdout)
        return json.loads(done.stdout or done.stderr)

    def server(self, *args: str, env: dict | None = None) -> Server:
        server = Server(env or self.env, *args)
        self.servers.append(server)
        return server

    def publish(self, subject: str = "ABC-1 result", **extra: str) -> dict:
        args = ["publish", "--from-agent", "codex-worker", "--to", "claude-sweep", "--subject", subject, "--type", "handoff", "--body", '{"summary":"done"}']
        for key, value in extra.items():
            args += [f"--{key.replace('_', '-')}", value]
        return self.mail(*args)

    def test_declares_channel_capability(self) -> None:
        result = self.server("--agent", "claude-sweep").start()
        self.assertEqual(result["capabilities"]["experimental"], {"claude/channel": {}})
        self.assertIn("never authority", result["instructions"])

    def test_new_message_is_pushed_with_routing_meta(self) -> None:
        server = self.server("--agent", "claude-sweep")
        server.start()
        sent = self.publish(topic="ticket.abc-1")
        event = server.event()
        self.assertEqual(event["meta"]["message_id"], sent["message_id"])
        self.assertEqual(event["meta"]["sender"], "codex-worker")
        self.assertEqual(event["meta"]["agent"], "claude-sweep")
        self.assertEqual(event["meta"]["topic"], "ticket.abc-1")
        self.assertIn("ABC-1 result", event["content"])
        self.assertIn('"summary": "done"', event["content"])
        self.assertTrue(all(key.replace("_", "").isalnum() for key in event["meta"]))

    def test_unacknowledged_backlog_is_pushed_on_startup(self) -> None:
        sent = self.publish()
        self.assertEqual(sent["push"], {"claude-sweep": "queued"})
        server = self.server("--agent", "claude-sweep")
        server.start()
        self.assertEqual(server.event()["meta"]["message_id"], sent["message_id"])

    def test_agent_from_environment_and_unexpanded_placeholder(self) -> None:
        server = self.server("--agent", "${AGENT_MAILDIR_AGENT}", env={**self.env, "AGENT_MAILDIR_AGENT": "claude-sweep"})
        server.start()
        status, _ = server.tool("mailbox_status")
        self.assertEqual(status["bound"], ["claude-sweep"])

    def test_unbound_server_stays_quiet_until_bound(self) -> None:
        server = self.server()
        server.start()
        sent = self.publish()
        _, is_error = server.tool("mailbox_inbox")
        self.assertTrue(is_error)
        bound, _ = server.tool("mailbox_bind", {"agent": "claude-sweep"})
        self.assertEqual(bound["unread"], 1)
        self.assertEqual(server.event()["meta"]["message_id"], sent["message_id"])

    def test_bind_rejects_unregistered_agent(self) -> None:
        server = self.server()
        server.start()
        _, is_error = server.tool("mailbox_bind", {"agent": "nobody"})
        self.assertTrue(is_error)

    def test_ack_tool_archives_and_notifies_sender_without_ack_loop(self) -> None:
        server = self.server("--agent", "claude-sweep")
        server.start()
        sent = self.publish()
        server.event()
        ack, is_error = server.tool("mailbox_ack", {"message_id": sent["message_id"], "note": "verified"})
        self.assertFalse(is_error)
        self.assertEqual(self.mail("inbox", "--agent", "claude-sweep")["count"], 0)
        sender_inbox = self.mail("inbox", "--agent", "codex-worker")
        self.assertEqual(sender_inbox["messages"][0]["type"], "ack")
        self.assertEqual(sender_inbox["messages"][0]["reply_to"], sent["message_id"])
        # the sender archives the ACK; that must not bounce another ACK back
        archived = self.mail("ack", "--agent", "codex-worker", "--message-id", ack["ack_id"])
        self.assertIsNone(archived["ack_id"])
        self.assertEqual(self.mail("inbox", "--agent", "claude-sweep")["count"], 0)

    def test_reply_tool_and_sender_wait(self) -> None:
        server = self.server("--agent", "claude-sweep")
        server.start()
        sent = self.publish()
        server.event()
        waiter: dict = {}
        thread = threading.Thread(
            target=lambda: waiter.update(self.mail("wait", "--agent", "codex-worker", "--reply-to", sent["message_id"], "--timeout-seconds", "10"))
        )
        thread.start()
        reply, is_error = server.tool("mailbox_publish", {"to": ["codex-worker"], "subject": "Re: ABC-1", "reply_to": sent["message_id"], "body": {"summary": "accepted"}})
        self.assertFalse(is_error)
        thread.join(timeout=15)
        self.assertFalse(thread.is_alive(), "reply waiter did not finish")
        self.assertEqual(waiter["reply"]["id"], reply["message_id"])
        self.assertEqual(waiter["reply"]["content"], {"summary": "accepted"})

    def test_wait_times_out_with_failure_exit(self) -> None:
        sent = self.publish()
        done = subprocess.run(MAIL + ["wait", "--agent", "codex-worker", "--reply-to", sent["message_id"], "--timeout-seconds", "1"], capture_output=True, text=True, env=self.env)
        self.assertEqual(done.returncode, 1)
        self.assertTrue(json.loads(done.stdout)["timed_out"])

    def test_publish_reports_listening_recipient_and_presence_clears(self) -> None:
        server = self.server("--agent", "claude-sweep")
        server.start()
        server.tool("mailbox_status")
        self.assertEqual(self.publish()["push"], {"claude-sweep": "bound"})
        nonce = server.probe()["meta"]["nonce"]
        _, is_error = server.tool("mailbox_confirm_push", {"nonce": "wrong"})
        self.assertTrue(is_error)
        confirmed, _ = server.tool("mailbox_confirm_push", {"nonce": nonce})
        self.assertEqual(confirmed["push"]["state"], "confirmed")
        self.assertEqual(self.publish()["push"], {"claude-sweep": "listening"})
        self.assertIn("claude-sweep", self.mail("listeners")["listeners"])
        server.close()
        self.assertEqual(self.mail("listeners")["listeners"], {})
        self.assertEqual(self.publish()["push"], {"claude-sweep": "queued"})

    def test_reminder_after_redelivery_interval(self) -> None:
        server = self.server("--agent", "claude-sweep", "--redeliver-seconds", "2")
        server.start()
        sent = self.publish()
        self.assertNotIn("redelivery", server.event()["meta"])
        reminder = server.event(timeout=6)
        self.assertEqual(reminder["meta"]["message_id"], sent["message_id"])
        self.assertEqual(reminder["meta"]["redelivery"], "1")
        self.assertIn("still unacknowledged", reminder["content"])

    def test_tools_cannot_speak_for_an_unbound_agent(self) -> None:
        server = self.server("--agent", "claude-sweep")
        server.start()
        _, is_error = server.tool("mailbox_publish", {"from_agent": "codex-worker", "to": ["claude-sweep"], "subject": "spoof"})
        self.assertTrue(is_error)

    def test_publish_tool_rejects_empty_subject_before_delivery(self) -> None:
        server = self.server("--agent", "claude-sweep")
        server.start()
        result, is_error = server.tool("mailbox_publish", {"to": ["codex-worker"], "subject": ""})
        self.assertTrue(is_error)
        self.assertIn("subject", result["error"])
        self.assertEqual(self.mail("inbox", "--agent", "codex-worker")["count"], 0)

    def test_malformed_file_raises_one_alert_and_server_survives(self) -> None:
        server = self.server("--agent", "claude-sweep")
        server.start()
        (Path(self.tmp.name) / "mailboxes" / "claude-sweep" / "Maildir" / "new" / "junk").write_text("not a message")
        self.assertEqual(server.event()["meta"]["type"], "alert")
        sent = self.publish()
        self.assertEqual(server.event()["meta"]["message_id"], sent["message_id"])

    def test_doctor_accepts_presence_directory(self) -> None:
        server = self.server("--agent", "claude-sweep")
        server.start()
        server.tool("mailbox_status")
        self.assertTrue(self.mail("doctor")["ok"])

    def test_push_is_reported_off_without_channel_flag(self) -> None:
        plain = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        try:
            server = self.server("--agent", "claude-sweep", env={**self.env, "CLAUDE_PID": str(plain.pid)})
            server.start()
            status, _ = server.tool("mailbox_status")
            self.assertEqual(status["push"]["state"], "off")
            self.assertEqual(self.publish()["push"], {"claude-sweep": "bound"})
        finally:
            plain.kill()
            plain.wait()

    def test_join_registers_subscribes_and_makes_role_reachable(self) -> None:
        joined = self.mail("join", "--harness", "claude", "--role", "reviewer", "--project", "demo", "--attach")
        self.assertEqual(joined["agent"], "claude-reviewer")
        self.assertEqual(joined["topics"], ["role.reviewer", "project.demo"])
        self.assertEqual(joined["attached"]["session"], "session-a")
        # the sender only needs the role, not the agent id or the session
        sent = self.mail("publish", "--from-agent", "codex-worker", "--topic", "role.reviewer", "--subject", "hello")
        self.assertEqual(sent["recipients"], ["claude-reviewer"])
        self.assertEqual(sent["push"], {"claude-reviewer": "bound"})
        again = self.mail("join", "--harness", "claude", "--role", "reviewer", "--project", "demo")
        self.assertFalse(again["new"])
        self.assertEqual(again["subscribed_now"], [])

    def test_join_refuses_a_role_another_agent_holds(self) -> None:
        self.mail("join", "--harness", "codex", "--role", "reviewer")
        done = subprocess.run(MAIL + ["join", "--harness", "claude", "--role", "reviewer"], capture_output=True, text=True, env=self.env)
        self.assertEqual(done.returncode, 1)
        self.assertEqual(json.loads(done.stdout)["role_held_by"], ["codex-reviewer"])
        self.assertNotIn("claude-reviewer", self.mail("agents")["agents"])

    def test_join_announces_to_project_subscribers_once(self) -> None:
        self.mail("subscribe", "--agent", "codex-worker", "--topic", "project.demo")
        first = self.mail("join", "--harness", "claude", "--role", "reviewer", "--project", "demo")
        self.assertEqual(len(first["announced"]), 1)
        inbox = self.mail("inbox", "--agent", "codex-worker")["messages"]
        self.assertEqual(inbox[0]["content"]["reach"], ["role.reviewer", "claude-reviewer"])
        second = self.mail("join", "--harness", "claude", "--role", "reviewer", "--project", "demo")
        self.assertEqual(second["announced"], [])

    def test_attach_moves_the_address_to_the_latest_session(self) -> None:
        self.mail("attach", "--agent", "claude-sweep", "--session", "session-a")
        moved = self.mail("attach", "--agent", "claude-sweep", "--session", "session-b")
        self.assertEqual(moved["moved_from"], ["session-a"])
        self.assertEqual(self.hook({"hook_event_name": "Stop", "session_id": "session-a"}), "")
        self.publish()
        self.assertIn("decision", self.hook({"hook_event_name": "Stop", "session_id": "session-b"}))

    def test_join_tool_binds_session_and_reports_push_honestly(self) -> None:
        server = self.server()
        server.start()
        joined, is_error = server.tool("mailbox_join", {"role": "reviewer", "project": ["demo"]})
        self.assertFalse(is_error)
        self.assertEqual(joined["bound"], ["claude-reviewer"])
        self.assertEqual(joined["push"]["state"], "unconfirmed")
        self.assertIn(server.probe()["meta"]["nonce"], joined["push"]["note"])

    def test_codex_client_joins_as_codex_without_claude_probe_or_push(self) -> None:
        server = self.server()
        initialized = server.start(client_name="codex-cli")
        self.assertNotIn("experimental", initialized["capabilities"])
        self.assertNotIn("<channel", initialized["instructions"])
        tools = server.request("tools/list")["result"]["tools"]
        self.assertNotIn("mailbox_confirm_push", {tool["name"] for tool in tools})
        joined, is_error = server.tool("mailbox_join", {"role": "reviewer", "project": ["demo"]})
        self.assertFalse(is_error)
        self.assertEqual(joined["agent"], "codex-reviewer")
        self.assertEqual(joined["push"]["state"], "off")
        self.assertIsNone(joined["push"]["launch"])
        self.mail("publish", "--from-agent", "codex-worker", "--to", "codex-reviewer", "--subject", "poll me")
        inbox, is_error = server.tool("mailbox_inbox", {"agent": "codex-reviewer"})
        self.assertFalse(is_error)
        self.assertEqual(inbox["count"], 1)

    def test_unregister_removes_agent_and_refuses_unread_mail(self) -> None:
        self.mail("join", "--harness", "claude", "--role", "temp", "--attach")
        self.mail("publish", "--from-agent", "codex-worker", "--to", "claude-temp", "--subject", "x")
        done = subprocess.run(MAIL + ["unregister", "--agent", "claude-temp"], capture_output=True, text=True, env=self.env)
        self.assertEqual(done.returncode, 2)
        self.mail("unregister", "--agent", "claude-temp", "--discard-mail")
        self.assertNotIn("claude-temp", self.mail("agents")["agents"])
        self.assertEqual(self.mail("subscriptions")["subscriptions"], [])
        self.assertTrue(self.mail("doctor")["ok"])

    def hook(self, event: dict, *args: str, env: dict | None = None) -> str:
        done = subprocess.run(HOOK + list(args), input=json.dumps(event), capture_output=True, text=True, env=env or self.env)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def test_hook_is_silent_for_unattached_session_or_empty_inbox(self) -> None:
        self.publish()
        self.assertEqual(self.hook({"hook_event_name": "Stop", "session_id": "session-a"}), "")
        self.assertEqual(self.hook({"hook_event_name": "SessionStart", "session_id": "session-a"}), "")
        self.mail("attach", "--agent", "codex-worker")
        self.assertEqual(self.hook({"hook_event_name": "Stop", "session_id": "session-a"}), "")

    def test_hook_adds_context_on_session_start(self) -> None:
        self.mail("attach", "--agent", "claude-sweep")
        sent = self.publish()
        out = json.loads(self.hook({"hook_event_name": "SessionStart", "session_id": "session-a"}))
        self.assertIn(sent["message_id"], out["hookSpecificOutput"]["additionalContext"])

    def test_stop_hook_blocks_once_per_message(self) -> None:
        self.mail("attach", "--agent", "claude-sweep")
        sent = self.publish()
        stop = {"hook_event_name": "Stop", "session_id": "session-a"}
        first = json.loads(self.hook(stop))
        self.assertEqual(first["decision"], "block")
        self.assertIn(sent["message_id"], first["reason"])
        self.assertIn('"summary": "done"', first["reason"])
        self.assertEqual(self.hook(stop), "")
        second = self.publish(subject="another")
        again = json.loads(self.hook(stop))
        self.assertIn(second["message_id"], again["reason"])
        self.assertNotIn(sent["message_id"], again["reason"])
        self.publish(subject="third")
        self.assertEqual(self.hook({**stop, "stop_hook_active": True}), "")

    def test_environment_agent_still_works_without_attach(self) -> None:
        sent = self.publish()
        out = self.hook({"hook_event_name": "Stop", "session_id": "session-z"}, env={**self.env, "AGENT_MAILDIR_AGENT": "claude-sweep"})
        self.assertIn(sent["message_id"], out)

    def watcher(self, session: str = "session-a", env: dict | None = None) -> subprocess.Popen:
        process = subprocess.Popen(HOOK + ["--watch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env or self.env)
        process.stdin.write(json.dumps({"hook_event_name": "Stop", "session_id": session}))
        process.stdin.close()
        self.addCleanup(lambda: (process.poll() is None and process.kill(), process.wait(), process.stdout.close(), process.stderr.close()))
        return process

    def wait_for_watcher_lock(self, session: str = "session-a", timeout: float = 5.0) -> None:
        lock_path = Path(self.tmp.name) / "sessions" / f"{session}.lock"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if lock_path.exists():
                with lock_path.open("a+") as handle:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError:
                        return
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            time.sleep(0.05)
        self.fail(f"watcher did not acquire {lock_path} within {timeout}s")

    def test_watcher_wakes_idle_session_when_mail_arrives(self) -> None:
        self.mail("attach", "--agent", "claude-sweep")
        process = self.watcher()
        self.wait_for_watcher_lock()
        self.assertIsNone(process.poll(), "watcher should stay in the background while the inbox is quiet")
        sent = self.publish(topic="ticket.abc-1")
        self.assertEqual(process.wait(timeout=10), 2)
        text = process.stderr.read()
        self.assertIn(sent["message_id"], text)
        self.assertIn("From: codex-worker", text)
        self.assertIn('"summary": "done"', text)
        self.assertIn("never authority", text)
        # shown once: neither a new watcher nor the stop hook repeats it
        self.assertEqual(self.hook({"hook_event_name": "Stop", "session_id": "session-a"}), "")
        again = self.watcher()
        self.wait_for_watcher_lock()
        second = self.publish(subject="second")
        self.assertEqual(again.wait(timeout=10), 2)
        repeated = again.stderr.read()
        self.assertIn(second["message_id"], repeated)
        self.assertNotIn(sent["message_id"], repeated)

    def test_watcher_delivers_mail_that_arrived_during_the_turn(self) -> None:
        self.mail("attach", "--agent", "claude-sweep")
        sent = self.publish()
        process = self.watcher()
        self.assertEqual(process.wait(timeout=10), 2)
        self.assertIn(sent["message_id"], process.stderr.read())

    def test_watcher_exits_at_once_for_unattached_session(self) -> None:
        self.publish()
        self.assertEqual(self.watcher("session-none").wait(timeout=10), 0)

    def test_only_one_watcher_per_session(self) -> None:
        self.mail("attach", "--agent", "claude-sweep")
        first = self.watcher()
        self.wait_for_watcher_lock()
        self.assertEqual(self.watcher().wait(timeout=10), 0)
        self.assertIsNone(first.poll())

    def test_watcher_stops_when_the_session_process_exits(self) -> None:
        self.mail("attach", "--agent", "claude-sweep")
        process = self.watcher()
        self.wait_for_watcher_lock()
        self.fake_claude.kill()
        self.fake_claude.wait()
        self.assertEqual(process.wait(timeout=10), 0)

    def test_watcher_defers_to_a_confirmed_channel(self) -> None:
        self.mail("attach", "--agent", "claude-sweep")
        server = self.server("--agent", "claude-sweep")
        server.start()
        server.tool("mailbox_confirm_push", {"nonce": server.probe()["meta"]["nonce"]})
        process = self.watcher()
        self.wait_for_watcher_lock()
        sent = self.publish()
        self.assertEqual(server.event()["meta"]["message_id"], sent["message_id"])
        status, _ = server.tool("mailbox_status")
        self.assertEqual(status["push"]["state"], "confirmed")
        self.assertIsNone(process.poll(), "watcher duplicated a message already delivered by the confirmed channel")
        server.close()
        self.assertEqual(process.wait(timeout=10), 2, "watcher did not resume hook delivery after the channel stopped")

    def test_confirmed_presence_cannot_be_overwritten_by_an_older_heartbeat(self) -> None:
        with mock.patch.dict(os.environ, self.env):
            channel = channel_module.Channel(["claude-sweep"], redeliver_seconds=0, harness="claude")
            original_atomic_json = channel_module.mail.atomic_json
            heartbeat_blocked = threading.Event()
            release_heartbeat = threading.Event()
            confirmation_started = threading.Event()
            errors: list[BaseException] = []

            def delayed_atomic_json(path: Path, value: dict) -> None:
                if value.get("push") == "unconfirmed" and not heartbeat_blocked.is_set():
                    heartbeat_blocked.set()
                    if not release_heartbeat.wait(timeout=5):
                        raise AssertionError("test did not release the older heartbeat")
                original_atomic_json(path, value)

            def confirm() -> None:
                try:
                    confirmation_started.set()
                    channel.call("mailbox_confirm_push", {"nonce": channel.nonce})
                except BaseException as error:
                    errors.append(error)

            with mock.patch.object(channel_module.mail, "atomic_json", side_effect=delayed_atomic_json):
                heartbeat = threading.Thread(target=channel.write_presence)
                heartbeat.start()
                self.assertTrue(heartbeat_blocked.wait(timeout=5), "older heartbeat did not reach the write boundary")
                confirmation = threading.Thread(target=confirm)
                confirmation.start()
                self.assertTrue(confirmation_started.wait(timeout=5), "confirmation did not start")
                # Without serialized presence writes, confirmation finishes while
                # the older heartbeat is paused and that heartbeat then wins.
                confirmation.join(timeout=1)
                release_heartbeat.set()
                heartbeat.join(timeout=5)
                confirmation.join(timeout=5)

        self.assertFalse(heartbeat.is_alive(), "older heartbeat did not finish")
        self.assertFalse(confirmation.is_alive(), "confirmation did not finish")
        self.assertEqual(errors, [])
        state = mail.load_json(mail.presence_file(Path(self.tmp.name), "claude-sweep"))
        self.assertEqual(state["push"], "confirmed")

    def test_watcher_rechecks_confirmation_immediately_before_waking(self) -> None:
        message = {"id": "message-1", "sender": "codex-worker", "type": "handoff", "authority": "none", "subject": "result", "content": {}}
        with (
            mock.patch.object(hook_module, "session_agents", return_value=["claude-sweep"]),
            mock.patch.object(hook_module, "channel_confirmed", side_effect=[False, True]),
            mock.patch.object(hook_module, "unread", return_value=[("claude-sweep", message)]),
            mock.patch.object(hook_module, "load_surfaced", return_value=set()),
            mock.patch.object(hook_module, "mark_surfaced") as mark_surfaced,
            mock.patch.object(hook_module, "alive", side_effect=[True, False]),
            mock.patch.object(hook_module.time, "sleep"),
        ):
            self.assertEqual(hook_module.watch(Path(self.tmp.name), "session-race"), 0)
        mark_surfaced.assert_not_called()

    def test_watcher_tolerates_one_delayed_channel_heartbeat(self) -> None:
        root = Path(self.tmp.name)
        state = {
            "agent": "claude-sweep",
            "transport": "claude-channel",
            "pid": os.getpid(),
            "claude_pid": self.fake_claude.pid,
            "push": "confirmed",
            "updated_at": mail.iso(mail.utc_now() - timedelta(seconds=mail.PRESENCE_TTL_SECONDS + 1)),
        }
        mail.atomic_json(mail.presence_file(root, "claude-sweep"), state)
        self.assertIsNone(mail.listener_state(root, "claude-sweep"), "ordinary listener reporting should use the normal TTL")
        with mock.patch.object(hook_module.mail, "claude_session_pid", return_value=self.fake_claude.pid):
            self.assertTrue(hook_module.channel_confirmed(root, ["claude-sweep"]))

    def test_watcher_cleanup_precedes_tempdir_cleanup(self) -> None:
        self.mail("attach", "--agent", "claude-sweep")
        process = self.watcher()
        self.wait_for_watcher_lock()
        root = Path(self.tmp.name)
        self.doCleanups()
        self.assertIsNotNone(process.poll())
        self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
