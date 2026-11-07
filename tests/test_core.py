# SPDX-License-Identifier: Apache-2.0
"""Core CLI, corruption, concurrency, routing, and lease regression tests."""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / "src" / "agent_maildir"
sys.path.insert(0, str(SOURCE))
import agent_mail as mail  # noqa: E402

MAIL = [sys.executable, str(SOURCE / "agent_mail.py")]
HOOK = [sys.executable, str(SOURCE / "mailbox_hook.py")]


class MailboxTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = {**os.environ, "AGENT_MAILDIR_ROOT": self.tmp.name}
        self.mail("init")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def mail(self, *args: str, check: bool = True) -> dict:
        done = subprocess.run(MAIL + list(args), capture_output=True, text=True, env=self.env)
        if check and done.returncode != 0:
            self.fail(done.stderr or done.stdout)
        output = done.stdout or done.stderr
        return json.loads(output) if output else {}

    def register(self, agent: str, role: str = "worker", harness: str = "other") -> None:
        self.mail("register", "--agent", agent, "--harness", harness, "--role", role)

    def publish(self, sender: str, recipient: str, subject: str = "test") -> dict:
        return self.mail("publish", "--from-agent", sender, "--to", recipient, "--subject", subject)

    def new_folder(self, agent: str) -> Path:
        return self.root / "mailboxes" / agent / "Maildir" / "new"


class RootCompatibilityTests(unittest.TestCase):
    def test_new_root_variable_wins_and_legacy_root_remains_supported(self) -> None:
        with tempfile.TemporaryDirectory() as new, tempfile.TemporaryDirectory() as legacy:
            with mock.patch.dict(
                os.environ,
                {"AGENT_MAILDIR_ROOT": new, "AGENT_MAILBOX_ROOT": legacy},
                clear=True,
            ):
                self.assertEqual(mail.root_path(), Path(new).resolve())
            with mock.patch.dict(os.environ, {"AGENT_MAILBOX_ROOT": legacy}, clear=True):
                self.assertEqual(mail.root_path(), Path(legacy).resolve())


class CorruptionTests(MailboxTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.register("codex-builder", harness="codex")
        self.register("claude-reviewer", role="reviewer", harness="claude")

    def inject_bad(self, agent: str, name: str = "000-broken") -> Path:
        path = self.new_folder(agent) / name
        path.write_bytes(b"not an RFC message")
        return path

    def test_malformed_new_file_is_quarantined_and_all_read_paths_continue(self) -> None:
        sent = self.publish("codex-builder", "claude-reviewer")
        self.inject_bad("claude-reviewer", "000-inbox")
        inbox = self.mail("inbox", "--agent", "claude-reviewer")
        self.assertEqual([item["id"] for item in inbox["messages"]], [sent["message_id"]])

        self.inject_bad("claude-reviewer", "000-read")
        self.assertEqual(self.mail("read", "--agent", "claude-reviewer", "--message-id", sent["message_id"])["id"], sent["message_id"])

        self.inject_bad("claude-reviewer", "000-find")
        with mock.patch.dict(os.environ, {"AGENT_MAILDIR_ROOT": self.tmp.name}):
            _, found = mail.find_message(self.root, "claude-reviewer", sent["message_id"])
        self.assertEqual(found["id"], sent["message_id"])

        self.mail("attach", "--agent", "claude-reviewer", "--session", "session-test")
        self.inject_bad("claude-reviewer", "000-hook")
        hook = subprocess.run(
            HOOK,
            input=json.dumps({"hook_event_name": "SessionStart", "session_id": "session-test"}),
            capture_output=True,
            text=True,
            env=self.env,
            check=True,
        )
        self.assertIn(sent["message_id"], hook.stdout)

        self.inject_bad("claude-reviewer", "000-ack")
        archived = self.mail("ack", "--agent", "claude-reviewer", "--message-id", sent["message_id"])
        self.assertEqual(archived["message_id"], sent["message_id"])

        bad = self.root / "mailboxes" / "claude-reviewer" / "Maildir" / "bad"
        self.assertGreaterEqual(len(list(bad.iterdir())), 5)
        doctor = self.mail("doctor", check=False)
        self.assertFalse(doctor["ok"])
        self.assertTrue(any("quarantined" in error for error in doctor["errors"]))

    def test_file_that_vanishes_during_iteration_is_skipped(self) -> None:
        sent = self.publish("codex-builder", "claude-reviewer")
        vanished = self.inject_bad("claude-reviewer", "000-vanish")
        original = mail.parse_message

        def racing_parse(path: Path) -> dict:
            if path == vanished:
                path.unlink()
                raise FileNotFoundError(path)
            return original(path)

        with mock.patch.dict(os.environ, {"AGENT_MAILDIR_ROOT": self.tmp.name}), mock.patch.object(mail, "parse_message", racing_parse):
            _, found = mail.find_message(self.root, "claude-reviewer", sent["message_id"])
        self.assertEqual(found["id"], sent["message_id"])

    def test_ack_archives_when_sender_was_unregistered(self) -> None:
        sent = self.publish("codex-builder", "claude-reviewer")
        self.mail("unregister", "--agent", "codex-builder")
        result = self.mail("ack", "--agent", "claude-reviewer", "--message-id", sent["message_id"])
        self.assertIsNone(result["ack_id"])
        self.assertTrue(Path(result["archived"]).is_file())

    def test_incomplete_message_is_quarantined_and_does_not_block_unregister(self) -> None:
        path = self.new_folder("claude-reviewer") / "000-incomplete"
        path.write_text('Content-Type: application/json\n\n{"version":1}\n')
        inbox = self.mail("inbox", "--agent", "claude-reviewer")
        self.assertEqual(inbox["count"], 0)
        self.assertFalse(path.exists())
        self.assertTrue(any((path.parent.parent / "bad").iterdir()))
        self.assertTrue(self.mail("unregister", "--agent", "claude-reviewer")["ok"])

    def test_transient_oserror_is_not_quarantined(self) -> None:
        path = self.inject_bad("claude-reviewer", "000-transient")
        with mock.patch.object(mail, "parse_message", side_effect=PermissionError("temporarily unavailable")):
            self.assertIsNone(mail.safe_parse_message(path))
        self.assertTrue(path.exists())
        self.assertEqual(list((path.parent.parent / "bad").iterdir()), [])

    def test_future_message_version_is_left_in_place_and_doctor_reports_it(self) -> None:
        payload = self.publish("codex-builder", "claude-reviewer")
        valid = next(self.new_folder("claude-reviewer").iterdir())
        text = valid.read_text().replace('"version": 1', '"version": 99')
        valid.write_text(text)
        self.assertEqual(self.mail("inbox", "--agent", "claude-reviewer")["count"], 0)
        self.assertTrue(valid.exists())
        doctor = self.mail("doctor", check=False)
        self.assertFalse(doctor["ok"])
        self.assertTrue(any("unsupported message version" in error for error in doctor["errors"]))

    def test_symlink_in_new_is_quarantined_without_chmodding_target(self) -> None:
        target = self.root / "outside-message"
        target.write_text('Content-Type: application/json\n\n{"version":1}\n')
        target.chmod(0o644)
        link = self.new_folder("claude-reviewer") / "000-link"
        link.symlink_to(target)
        self.assertEqual(self.mail("inbox", "--agent", "claude-reviewer")["count"], 0)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
        self.assertTrue(target.exists())
        self.assertFalse(link.exists())
        self.assertTrue(any(path.is_symlink() for path in (link.parent.parent / "bad").iterdir()))


class RegistrationAndArchiveTests(MailboxTestCase):
    def test_join_role_check_registration_and_subscription_are_atomic(self) -> None:
        args = [
            argparse.Namespace(role="reviewer", harness="codex", agent="codex-builder", project=[], topic=[], announce=False, no_announce=True, attach=False),
            argparse.Namespace(role="reviewer", harness="claude", agent="claude-reviewer", project=[], topic=[], announce=False, no_announce=True, attach=False),
        ]
        results: list[dict] = []
        errors: list[Exception] = []
        barrier = threading.Barrier(2)
        original_register = mail.cmd_register

        def delayed_register(namespace: argparse.Namespace) -> dict:
            result = original_register(namespace)
            barrier.wait(timeout=5)
            return result

        def join(namespace: argparse.Namespace) -> None:
            try:
                results.append(mail.cmd_join(namespace))
            except Exception as error:  # captured for assertions below
                errors.append(error)

        with mock.patch.dict(os.environ, {"AGENT_MAILDIR_ROOT": self.tmp.name}), mock.patch.object(mail, "cmd_register", delayed_register):
            threads = [threading.Thread(target=join, args=(item,)) for item in args]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertTrue(all(not thread.is_alive() for thread in threads), "concurrent join workers did not finish")

        self.assertFalse(errors)
        self.assertEqual(sorted(item["ok"] for item in results), [False, True])
        agents = self.mail("agents")["agents"]
        self.assertEqual(len(agents), 1, "the losing join must not leave an orphan registration")
        subscriptions = self.mail("subscriptions")["subscriptions"]
        self.assertEqual(len(subscriptions), 1)
        self.assertEqual(subscriptions[0]["agent"], next(iter(agents)))

    def test_unregister_retains_cur_unless_discard_mail_is_explicit(self) -> None:
        self.register("codex-builder", harness="codex")
        self.register("claude-reviewer", role="reviewer", harness="claude")
        sent = self.publish("codex-builder", "claude-reviewer")
        self.mail("ack", "--agent", "claude-reviewer", "--message-id", sent["message_id"])
        cur = self.root / "mailboxes" / "claude-reviewer" / "Maildir" / "cur"
        before = [path.name for path in cur.iterdir()]
        self.mail("unregister", "--agent", "claude-reviewer")
        self.assertEqual([path.name for path in cur.iterdir()], before)

        self.register("claude-reviewer", role="reviewer", harness="claude")
        self.mail("unregister", "--agent", "claude-reviewer", "--discard-mail")
        self.assertFalse(cur.parent.exists())

    def test_malformed_unrelated_lease_does_not_block_unregister(self) -> None:
        self.register("codex-builder", harness="codex")
        (self.root / "leases" / "broken.json").write_text("not json")
        self.assertTrue(self.mail("unregister", "--agent", "codex-builder")["ok"])
        doctor = self.mail("doctor", check=False)
        self.assertFalse(doctor["ok"])
        self.assertTrue(any("lease" in error and "malformed" in error for error in doctor["errors"]))


class LeaseTests(MailboxTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.register("codex-builder", harness="codex")

    def test_acquire_heartbeat_expiry_release_and_named_leases(self) -> None:
        default = self.mail("lease-acquire", "--agent", "codex-builder", "--ttl-seconds", "30")["lease"]
        named = self.mail("lease-acquire", "--name", "release", "--agent", "codex-builder", "--ttl-seconds", "30")["lease"]
        self.assertNotEqual(default["lease_id"], named["lease_id"])
        self.assertEqual(self.mail("lease-status")["lease"]["owner"], "codex-builder")
        self.assertEqual(self.mail("lease-status", "--name", "release")["lease"]["name"], "release")

        beat = self.mail(
            "lease-heartbeat", "--name", "release", "--agent", "codex-builder", "--lease-id", named["lease_id"], "--ttl-seconds", "60"
        )["lease"]
        self.assertGreater(mail.parse_iso(beat["expires_at"]), mail.parse_iso(named["expires_at"]))
        self.mail("lease-release", "--name", "release", "--agent", "codex-builder", "--lease-id", named["lease_id"])
        self.assertIsNone(self.mail("lease-status", "--name", "release")["lease"])

        path = self.root / "leases" / "orchestrator.json"
        data = json.loads(path.read_text())
        data["expires_at"] = "2000-01-01T00:00:00Z"
        path.write_text(json.dumps(data))
        replacement = self.mail("lease-acquire", "--agent", "codex-builder", "--ttl-seconds", "30")["lease"]
        self.assertNotEqual(replacement["lease_id"], default["lease_id"])
        self.mail("lease-release", "--agent", "codex-builder", "--lease-id", replacement["lease_id"])

    def test_malformed_lease_has_clear_repair_path(self) -> None:
        path = self.root / "leases" / "orchestrator.json"
        path.write_text("not json")
        failed = self.mail("lease-acquire", "--agent", "codex-builder", check=False)
        self.assertIn("lease-repair --name orchestrator", failed["error"])
        repaired = self.mail("lease-repair", "--name", "orchestrator")
        self.assertTrue(repaired["repaired"])
        self.assertTrue(Path(repaired["quarantined"]).is_file())
        self.assertTrue(self.mail("lease-acquire", "--agent", "codex-builder")["ok"])


class RoutingAndConcurrencyTests(MailboxTestCase):
    def setUp(self) -> None:
        super().setUp()
        for agent in ("sender", "exact", "wildcard", "direct", "plain"):
            self.register(agent)

    def test_wildcard_and_exact_role_routing(self) -> None:
        self.mail("subscribe", "--agent", "exact", "--topic", "role.reviewer")
        self.mail("subscribe", "--agent", "wildcard", "--topic", "ticket.abc-*")
        role = self.mail("publish", "--from-agent", "sender", "--topic", "role.reviewer", "--subject", "role")
        ticket = self.mail("publish", "--from-agent", "sender", "--topic", "ticket.abc-1", "--subject", "ticket")
        self.assertEqual(role["recipients"], ["exact"])
        self.assertEqual(ticket["recipients"], ["wildcard"])

    def test_publish_rejects_payloads_the_reader_would_quarantine(self) -> None:
        for option in ("--subject", "--authority"):
            with self.subTest(option=option):
                args = ["publish", "--from-agent", "sender", "--to", "plain", "--subject", "valid"]
                if option == "--subject":
                    args[-1] = ""
                else:
                    args += ["--authority", ""]
                result = self.mail(*args, check=False)
                self.assertFalse(result["ok"])
                self.assertIn(option.removeprefix("--"), result["error"])
        self.assertEqual(self.mail("inbox", "--agent", "plain")["count"], 0)

    def test_every_accepted_publish_is_readable_by_inbox_and_read(self) -> None:
        accepted = self.mail(
            "publish",
            "--from-agent",
            "sender",
            "--to",
            "plain",
            "--subject",
            "Readable",
            "--authority",
            "informational",
            "--type",
            "request",
            "--body",
            '{"summary":"please inspect"}',
        )
        inbox = self.mail("inbox", "--agent", "plain")
        self.assertEqual([item["id"] for item in inbox["messages"]], [accepted["message_id"]])
        read = self.mail("read", "--agent", "plain", "--message-id", accepted["message_id"])
        self.assertEqual(read["subject"], "Readable")

    def test_broadcast_reaches_all_others_once_and_combines_with_direct(self) -> None:
        sent = self.mail("publish", "--from-agent", "sender", "--broadcast", "--to", "direct", "--subject", "all hands")
        self.assertEqual(sent["recipients"], ["direct", "exact", "plain", "wildcard"])
        self.assertNotIn("sender", sent["recipients"])
        for agent in sent["recipients"]:
            self.assertEqual(self.mail("inbox", "--agent", agent)["count"], 1)
        self.assertEqual(self.mail("inbox", "--agent", "sender")["count"], 0)

        reserved = self.mail("publish", "--from-agent", "sender", "--topic", "all", "--subject", "reserved")
        self.assertEqual(reserved["recipients"], sent["recipients"])
        refused = self.mail("subscribe", "--agent", "plain", "--topic", "all", check=False)
        self.assertIn("reserved", refused["error"])
        ambiguous = self.mail(
            "publish", "--from-agent", "sender", "--broadcast", "--topic", "ticket.abc-1", "--subject", "ambiguous", check=False
        )
        self.assertIn("cannot be combined", ambiguous["error"])

    def test_broadcast_is_safe_during_concurrent_registration(self) -> None:
        gate = threading.Barrier(2)
        outputs: list[dict] = []

        def register() -> None:
            gate.wait()
            outputs.append(self.mail("register", "--agent", "late", "--harness", "other", "--role", "worker"))

        thread = threading.Thread(target=register)
        thread.start()
        gate.wait()
        sent = self.mail("publish", "--from-agent", "sender", "--broadcast", "--subject", "race")
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive(), "concurrent registration worker did not finish")
        self.assertTrue(outputs[0]["ok"])
        late_count = self.mail("inbox", "--agent", "late")["count"]
        self.assertEqual(late_count, 1 if "late" in sent["recipients"] else 0)

    def test_parallel_publish_and_ack_is_lossless(self) -> None:
        commands = [MAIL + ["publish", "--from-agent", "sender", "--to", "plain", "--subject", f"message-{index}"] for index in range(8)]
        processes = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.env) for command in commands]
        sent = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, stderr)
            sent.append(json.loads(stdout)["message_id"])
        self.assertEqual(self.mail("inbox", "--agent", "plain", "--limit", "20")["count"], 8)

        commands = [MAIL + ["ack", "--agent", "plain", "--message-id", message_id] for message_id in sent]
        processes = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.env) for command in commands]
        for process in processes:
            _, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(self.mail("inbox", "--agent", "plain")["count"], 0)
        self.assertEqual(self.mail("inbox", "--agent", "sender", "--limit", "20")["count"], 8)

    def test_multi_recipient_wait_returns_first_ack_and_leaves_other_mail(self) -> None:
        process = subprocess.Popen(
            MAIL
            + [
                "publish",
                "--from-agent",
                "sender",
                "--to",
                "exact",
                "--to",
                "plain",
                "--subject",
                "wait policy",
                "--wait-seconds",
                "10",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.env,
        )
        message_id = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            messages = self.mail("inbox", "--agent", "exact")["messages"]
            if messages:
                message_id = messages[0]["id"]
                break
            time.sleep(0.05)
        self.assertIsNotNone(message_id)
        ack = self.mail("ack", "--agent", "exact", "--message-id", str(message_id))
        stdout, stderr = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 0, stderr)
        published = json.loads(stdout)
        self.assertEqual(published["reply"]["id"], ack["ack_id"])
        self.assertFalse(published["timed_out"])
        self.assertEqual(self.mail("inbox", "--agent", "plain")["count"], 1)


class ReliabilityWinTests(MailboxTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.register("codex-builder", harness="codex")
        self.register("claude-reviewer", role="reviewer", harness="claude")

    def payload(self, message_id: str | None = None) -> dict:
        return {
            "version": mail.VERSION,
            "id": message_id or str(uuid.uuid4()),
            "sender": "codex-builder",
            "recipients": ["claude-reviewer"],
            "topic": None,
            "created_at": mail.iso(),
            "type": "message",
            "authority": "informational",
            "subject": "reliability",
            "reply_to": None,
            "expires_at": None,
            "content": {},
        }

    def test_maildir_renames_fsync_source_and_destination_directories(self) -> None:
        with mock.patch.dict(os.environ, {"AGENT_MAILDIR_ROOT": self.tmp.name}), mock.patch.object(mail, "fsync_dir") as sync:
            delivered = mail.deliver(self.root, "claude-reviewer", self.payload())
            synced = [call.args[0].name for call in sync.call_args_list]
        self.assertIn("tmp", synced)
        self.assertIn("new", synced)

        message_id = mail.parse_message(delivered)["id"]
        with mock.patch.dict(os.environ, {"AGENT_MAILDIR_ROOT": self.tmp.name}), mock.patch.object(mail, "fsync_dir") as sync:
            mail.cmd_ack(argparse.Namespace(agent="claude-reviewer", message_id=message_id, status="processed", note=""))
            synced = [call.args[0].name for call in sync.call_args_list]
        self.assertIn("new", synced)
        self.assertIn("cur", synced)

    def test_existing_secure_paths_do_not_repeat_chmod(self) -> None:
        directory = self.root / "already-secure"
        directory.mkdir(mode=0o700)
        file = directory / "file"
        file.write_text("x")
        file.chmod(0o600)
        with mock.patch.object(Path, "chmod") as chmod:
            mail.secure_dir(directory)
            mail.secure_file(file)
        chmod.assert_not_called()

    def test_hook_pages_through_more_than_fifty_messages(self) -> None:
        with mock.patch.dict(os.environ, {"AGENT_MAILDIR_ROOT": self.tmp.name}):
            for _ in range(55):
                mail.deliver(self.root, "claude-reviewer", self.payload())
        self.mail("attach", "--agent", "claude-reviewer", "--session", "session-many")
        done = subprocess.run(
            HOOK,
            input=json.dumps({"hook_event_name": "SessionStart", "session_id": "session-many"}),
            capture_output=True,
            text=True,
            env=self.env,
            check=True,
        )
        output = json.loads(done.stdout)
        self.assertIn("55 unacknowledged message(s)", output["hookSpecificOutput"]["additionalContext"])

    def test_hook_without_session_identity_does_not_share_default_state(self) -> None:
        self.publish("codex-builder", "claude-reviewer")
        env = {**self.env, "AGENT_MAILDIR_AGENT": "claude-reviewer"}
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        done = subprocess.run(
            HOOK,
            input=json.dumps({"hook_event_name": "SessionStart"}),
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        self.assertEqual(done.stdout, "")

    def test_owned_directory_permission_drift_is_tightened(self) -> None:
        self.root.chmod(0o777)
        self.mail("init")
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)

    def test_message_size_limit_rejects_oversize_delivery_and_is_configurable(self) -> None:
        payload = self.payload()
        payload["content"] = {"body": "x" * (mail.DEFAULT_MAX_MESSAGE_BYTES + 1)}
        with mock.patch.dict(os.environ, {"AGENT_MAILDIR_ROOT": self.tmp.name}, clear=False):
            with self.assertRaisesRegex(ValueError, "size limit"):
                mail.deliver(self.root, "claude-reviewer", payload)
        self.assertEqual(list(self.new_folder("claude-reviewer").iterdir()), [])
        with mock.patch.dict(os.environ, {"AGENT_MAILDIR_MAX_MESSAGE_BYTES": str(2 * 1024 * 1024)}, clear=False):
            with self.assertRaisesRegex(ValueError, "size limit"):
                mail.deliver(self.root, "claude-reviewer", payload)

        with tempfile.TemporaryDirectory() as configured:
            configured_root = Path(configured)
            with mock.patch.dict(
                os.environ,
                {"AGENT_MAILDIR_ROOT": configured, "AGENT_MAILDIR_MAX_MESSAGE_BYTES": str(2 * 1024 * 1024)},
                clear=False,
            ):
                mail.ensure_layout(configured_root)
                mail.cmd_register(argparse.Namespace(agent="reader", harness="other", role="reader"))
            with mock.patch.dict(os.environ, {"AGENT_MAILDIR_ROOT": configured}, clear=True):
                delivered = mail.deliver(configured_root, "reader", {**payload, "recipients": ["reader"]})
                self.assertTrue(delivered.is_file())
                self.assertEqual(mail.parse_message(delivered)["id"], payload["id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
