# SPDX-License-Identifier: Apache-2.0
"""Setup command tests; every path is rooted in a temporary HOME."""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / "src" / "agent_maildir"
sys.path.insert(0, str(SOURCE))
import agent_mail  # noqa: E402
import setup_command  # noqa: E402


class SetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.settings = self.home / "custom" / "settings.json"
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(json.dumps({"permissions": {"allow": ["Read"]}, "hooks": {"PreToolUse": []}}))
        self.environment = mock.patch.dict(os.environ, {"HOME": self.tmp.name})
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.tmp.cleanup()

    def args(self, **overrides: object) -> argparse.Namespace:
        values = {
            "claude": False,
            "codex": False,
            "mcp": False,
            "settings": str(self.settings),
            "codex_config": None,
            "project": None,
            "dry_run": False,
            "uninstall": False,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_claude_and_codex_setup_is_idempotent_and_preserves_settings(self) -> None:
        codex_config = self.home / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        original_codex = '[model]\nname = "example"\n'
        codex_config.write_text(original_codex)
        missing = mock.Mock(returncode=1, stdout="", stderr="not found")
        ok = mock.Mock(returncode=0, stdout="ok", stderr="")
        with mock.patch.object(setup_command.shutil, "which", return_value="/tmp/claude"), mock.patch.object(
            setup_command.subprocess, "run", side_effect=[missing, ok]
        ):
            first = setup_command.run_setup(self.args(claude=True, codex=True))
            second = setup_command.run_setup(self.args(claude=True, codex=True))
        self.assertTrue(first["changed"])
        settings = json.loads(self.settings.read_text())
        self.assertEqual(settings["permissions"], {"allow": ["Read"]})
        self.assertEqual(settings["hooks"]["PreToolUse"], [])
        self.assertEqual(len(settings["hooks"]["SessionStart"]), 1)
        self.assertEqual(len(settings["hooks"]["Stop"]), 1)
        self.assertTrue((self.home / ".claude" / "skills" / "agent-maildir" / "SKILL.md").is_file())
        self.assertTrue((self.home / ".codex" / "skills" / "agent-maildir" / "agents" / "openai.yaml").is_file())
        self.assertFalse(second["changed"])
        settings = json.loads(self.settings.read_text())
        self.assertEqual(len(settings["hooks"]["SessionStart"]), 1)
        self.assertEqual(len(settings["hooks"]["Stop"]), 1)
        self.assertTrue(json.loads((self.home / setup_command.STATE_RELATIVE).read_text())["mcp"])
        codex_text = codex_config.read_text()
        self.assertTrue(codex_text.startswith(original_codex))
        self.assertEqual(codex_text.count(setup_command.CODEX_BEGIN), 1)

    def test_uninstall_removes_only_recorded_additions(self) -> None:
        missing = mock.Mock(returncode=1, stdout="", stderr="not found")
        ok = mock.Mock(returncode=0, stdout="ok", stderr="")
        with mock.patch.object(setup_command.shutil, "which", return_value="/tmp/claude"), mock.patch.object(
            setup_command.subprocess, "run", side_effect=[missing, ok, ok, ok]
        ):
            setup_command.run_setup(self.args(claude=True, codex=True))
            removed = setup_command.run_setup(self.args(uninstall=True))
        self.assertTrue(removed["changed"])
        settings = json.loads(self.settings.read_text())
        self.assertEqual(settings, {"permissions": {"allow": ["Read"]}, "hooks": {"PreToolUse": []}})
        self.assertFalse((self.home / ".claude" / "skills" / "agent-maildir").exists())
        self.assertFalse((self.home / ".codex" / "skills" / "agent-maildir").exists())
        self.assertFalse((self.home / ".codex" / "config.toml").exists())
        self.assertFalse((self.home / setup_command.STATE_RELATIVE).exists())

    def test_rerun_repairs_one_missing_recorded_hook_without_losing_uninstall_state(self) -> None:
        missing = mock.Mock(returncode=1, stdout="", stderr="not found")
        ok = mock.Mock(returncode=0, stdout="ok", stderr="")
        with mock.patch.object(setup_command.shutil, "which", return_value="/tmp/claude"), mock.patch.object(
            setup_command.subprocess, "run", side_effect=[missing, ok, ok, ok]
        ):
            setup_command.run_setup(self.args(claude=True))
            settings = json.loads(self.settings.read_text())
            settings["hooks"]["Stop"] = []
            self.settings.write_text(json.dumps(settings))
            setup_command.run_setup(self.args(claude=True))
            setup_command.run_setup(self.args(uninstall=True))
        settings = json.loads(self.settings.read_text())
        self.assertNotIn("SessionStart", settings["hooks"])
        self.assertNotIn("Stop", settings["hooks"])

    def test_uninstall_preserves_a_modified_installed_skill(self) -> None:
        setup_command.run_setup(self.args(codex=True))
        skill = self.home / ".codex" / "skills" / "agent-maildir" / "SKILL.md"
        skill.write_text(skill.read_text() + "\nlocal change\n")
        result = setup_command.run_setup(self.args(uninstall=True))
        self.assertTrue(skill.exists())
        self.assertTrue(any("preserve modified skill" in action for action in result["actions"]))

    def test_dry_run_writes_nothing(self) -> None:
        before = self.settings.read_bytes()
        result = setup_command.run_setup(self.args(claude=True, codex=True, mcp=True, dry_run=True))
        self.assertTrue(result["actions"])
        self.assertTrue(any("hooks" in action for action in result["actions"]))
        self.assertTrue(any(".claude/skills/agent-maildir" in action for action in result["actions"]))
        self.assertTrue(any("Codex MCP" in action for action in result["actions"]))
        self.assertTrue(any("register user MCP" in action for action in result["actions"]))
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertFalse((self.home / ".claude" / "skills").exists())
        self.assertFalse((self.home / setup_command.STATE_RELATIVE).exists())

    def test_project_guidance_is_idempotent_and_reversible(self) -> None:
        project = self.home / "project"
        project.mkdir()
        agents = project / "AGENTS.md"
        original = "# Existing instructions\n"
        agents.write_text(original)
        first = setup_command.run_setup(self.args(project=str(project)))
        second = setup_command.run_setup(self.args(project=str(project)))
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        self.assertEqual(agents.read_text().count(setup_command.PROJECT_BEGIN), 1)
        self.assertEqual((project / "CLAUDE.md").read_text().count(setup_command.PROJECT_BEGIN), 1)
        setup_command.run_setup(self.args(uninstall=True))
        self.assertEqual(agents.read_text(), original)
        self.assertFalse((project / "CLAUDE.md").exists())

    def test_integration_status_reports_both_harnesses(self) -> None:
        missing = mock.Mock(returncode=1, stdout="", stderr="not found")
        ok = mock.Mock(returncode=0, stdout="ok", stderr="")
        with mock.patch.object(setup_command.shutil, "which", return_value="/tmp/claude"), mock.patch.object(
            setup_command.subprocess, "run", side_effect=[missing, ok]
        ):
            setup_command.run_setup(self.args(claude=True, codex=True, settings=None))
        with mock.patch.object(setup_command.shutil, "which", return_value="/tmp/claude"), mock.patch.object(
            setup_command.subprocess, "run", return_value=ok
        ):
            status = setup_command.integration_status(self.home)
            self.assertEqual(status["claude"]["skill"], True)
            self.assertEqual(status["claude"]["hooks"], True)
            self.assertEqual(status["claude"]["mcp"], True)
            self.assertEqual(status["codex"]["skill"], True)
            self.assertEqual(status["codex"]["mcp"], True)
            mailbox_root = self.home / "mail"
            with mock.patch.dict(
                os.environ,
                {"HOME": str(self.home), "AGENT_MAILDIR_ROOT": str(mailbox_root)},
            ):
                doctor = agent_mail.cmd_doctor(argparse.Namespace(integrations=True))
        self.assertTrue(doctor["ok"])
        self.assertEqual(doctor["integration_errors"], [])

    def test_mcp_registration_and_removal_use_claude_add_json_shape(self) -> None:
        completed_missing = mock.Mock(returncode=1, stdout="", stderr="not found")
        completed_ok = mock.Mock(returncode=0, stdout="ok", stderr="")
        with mock.patch.object(setup_command.shutil, "which", return_value="/tmp/claude"), mock.patch.object(
            setup_command.subprocess, "run", side_effect=[completed_missing, completed_ok]
        ) as run:
            setup_command.run_setup(self.args(mcp=True))
        command = run.call_args_list[1].args[0]
        self.assertEqual(command[:6], ["claude", "mcp", "add-json", "--scope", "user", "agent-maildir"])
        self.assertEqual(json.loads(command[6]), setup_command.MCP_SPEC)

        with mock.patch.object(setup_command.shutil, "which", return_value="/tmp/claude"), mock.patch.object(
            setup_command.subprocess, "run", return_value=completed_ok
        ) as run:
            setup_command.run_setup(self.args(uninstall=True))
        self.assertEqual(run.call_args.args[0], ["claude", "mcp", "remove", "--scope", "user", "agent-maildir"])

    def test_symlinked_settings_preserve_link_mode_order_unicode_and_backup(self) -> None:
        target = self.home / "real-settings.json"
        target.write_text('{"z":"雪","permissions":{"allow":["Read"]}}\n')
        target.chmod(0o640)
        link = self.home / "settings-link.json"
        link.symlink_to(target)
        missing = mock.Mock(returncode=1, stdout="", stderr="not found")
        ok = mock.Mock(returncode=0, stdout="ok", stderr="")
        with mock.patch.object(setup_command.shutil, "which", return_value="/tmp/claude"), mock.patch.object(
            setup_command.subprocess, "run", side_effect=[missing, ok]
        ):
            result = setup_command.run_setup(self.args(claude=True, settings=str(link)))
        self.assertTrue(link.is_symlink())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
        rendered = target.read_text()
        self.assertIn("雪", rendered)
        self.assertLess(rendered.index('"z"'), rendered.index('"permissions"'))
        state = json.loads((self.home / setup_command.STATE_RELATIVE).read_text())
        backup = Path(state["settings"]["backup"])
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.suffix, ".bak")
        self.assertEqual(backup.parent.name, "backups")
        self.assertTrue(any(str(backup) in action for action in result["actions"]))
        with mock.patch.object(setup_command.shutil, "which", return_value="/tmp/claude"), mock.patch.object(
            setup_command.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="removed", stderr="")
        ):
            removed = setup_command.run_setup(self.args(uninstall=True))
        self.assertTrue(any(str(backup) in action for action in removed["actions"]))
        self.assertTrue(link.is_symlink())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)

    def test_codex_config_mode_survives_install_and_uninstall(self) -> None:
        config = self.home / ".codex" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text('[model]\nname = "example"\n')
        config.chmod(0o644)
        setup_command.run_setup(self.args(codex=True))
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o644)
        result = setup_command.run_setup(self.args(uninstall=True))
        self.assertTrue(result["ok"])
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o644)
        self.assertIn('[model]\nname = "example"\n', config.read_text())

    def test_codex_setup_refuses_existing_unmanaged_table(self) -> None:
        config = self.home / ".codex" / "config.toml"
        config.parent.mkdir(parents=True)
        original = '[mcp_servers.agent-maildir]\ncommand = "custom"\n'
        config.write_text(original)
        with self.assertRaisesRegex(ValueError, "already defines"):
            setup_command.run_setup(self.args(codex=True))
        self.assertEqual(config.read_text(), original)
        self.assertFalse((self.home / ".codex" / "skills" / "agent-maildir").exists())

    def test_uninstall_treats_missing_claude_mcp_as_complete(self) -> None:
        missing = mock.Mock(returncode=1, stdout="", stderr="not found")
        ok = mock.Mock(returncode=0, stdout="ok", stderr="")
        with mock.patch.object(setup_command.shutil, "which", return_value="/tmp/claude"), mock.patch.object(
            setup_command.subprocess, "run", side_effect=[missing, ok]
        ):
            setup_command.run_setup(self.args(claude=True))
        externally_removed = mock.Mock(returncode=1, stdout="", stderr="MCP server not found")
        with mock.patch.object(setup_command.shutil, "which", return_value="/tmp/claude"), mock.patch.object(
            setup_command.subprocess, "run", return_value=externally_removed
        ):
            result = setup_command.run_setup(self.args(uninstall=True))
        self.assertTrue(result["ok"])
        self.assertFalse((self.home / setup_command.STATE_RELATIVE).exists())
        self.assertFalse((self.home / ".claude" / "skills" / "agent-maildir").exists())

    def test_uninstall_accepts_real_claude_absent_wording_and_runs_mcp_last(self) -> None:
        missing = mock.Mock(returncode=1, stdout="", stderr="not found")
        ok = mock.Mock(returncode=0, stdout="ok", stderr="")
        with mock.patch.object(setup_command.shutil, "which", return_value="/tmp/claude"), mock.patch.object(
            setup_command.subprocess, "run", side_effect=[missing, ok]
        ):
            setup_command.run_setup(self.args(claude=True, codex=True))
        real_absent = mock.Mock(
            returncode=1,
            stdout='',
            stderr='No MCP server named "agent-maildir" in user scope',
        )
        with mock.patch.object(setup_command.shutil, "which", return_value="/tmp/claude"), mock.patch.object(
            setup_command.subprocess, "run", return_value=real_absent
        ) as run:
            result = setup_command.run_setup(self.args(uninstall=True))
        self.assertTrue(result["ok"])
        self.assertEqual(run.call_args.args[0][:3], ["claude", "mcp", "get"])
        self.assertFalse((self.home / ".claude" / "skills" / "agent-maildir").exists())
        self.assertFalse((self.home / ".codex" / "skills" / "agent-maildir").exists())
        self.assertFalse((self.home / setup_command.STATE_RELATIVE).exists())

    def test_uninstall_skips_and_warns_when_claude_cli_is_missing(self) -> None:
        state_path = self.home / setup_command.STATE_RELATIVE
        state_path.parent.mkdir(parents=True)
        state_path.write_text(json.dumps({"mcp": True}))
        with mock.patch.object(setup_command.shutil, "which", return_value=None):
            result = setup_command.run_setup(self.args(uninstall=True))
        self.assertTrue(result["ok"])
        self.assertTrue(any("not installed" in action for action in result["actions"]))
        self.assertFalse(state_path.exists())

    def test_setup_never_chmods_user_directories_and_follows_symlinked_config_dirs(self) -> None:
        dotfiles = self.home / "dotfiles"
        claude_target = dotfiles / "claude"
        codex_target = dotfiles / "codex"
        project = self.home / "project"
        for path in (claude_target, codex_target, project):
            path.mkdir(parents=True)
            path.chmod(0o755)
        (self.home / ".claude").symlink_to(claude_target, target_is_directory=True)
        (self.home / ".codex").symlink_to(codex_target, target_is_directory=True)
        missing = mock.Mock(returncode=1, stdout="", stderr="not found")
        ok = mock.Mock(returncode=0, stdout="ok", stderr="")
        with mock.patch.object(setup_command.shutil, "which", return_value="/tmp/claude"), mock.patch.object(
            setup_command.subprocess, "run", side_effect=[missing, ok]
        ):
            setup_command.run_setup(self.args(claude=True, codex=True, settings=None, project=str(project)))
        self.assertTrue((self.home / ".claude").is_symlink())
        self.assertTrue((self.home / ".codex").is_symlink())
        self.assertEqual(stat.S_IMODE(claude_target.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(codex_target.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(project.stat().st_mode), 0o755)

    def test_all_codex_mcp_toml_spellings_are_refused(self) -> None:
        examples = (
            '[mcp_servers]\nagent-maildir = { command = "custom" }\n',
            'mcp_servers.agent-maildir.command = "custom"\n',
            "[ mcp_servers.'agent-maildir' ]\ncommand = \"custom\"\n",
            'mcp_servers = { agent-maildir = { command = "custom" } }\n',
            '[mcp_servers]\n"agent-maildir" = { command = "custom" }\n',
            'mcp_servers."agent-maildir".command = "custom"\n',
            'mcp_servers = { "agent-maildir" = { command = "custom" } }\n',
            'mcp_servers = { other = { command = "x" }, agent-maildir = { command = "custom" } }\n',
            '[mcp_servers]\nagent-maildir.command = "custom"\n',
            '[mcp_servers.agent-maildir.env]\nTOKEN = "x"\n',
        )
        for index, original in enumerate(examples):
            with self.subTest(index=index):
                config = self.home / f"codex-{index}.toml"
                config.write_text(original)
                with self.assertRaisesRegex(ValueError, "already defines"):
                    setup_command.run_setup(self.args(codex=True, codex_config=str(config)))
                self.assertEqual(config.read_text(), original)

    def test_pending_mcp_is_reverified_and_pending_skill_is_removed(self) -> None:
        skill = self.home / ".codex" / "skills" / "agent-maildir"
        skill.mkdir(parents=True)
        (skill / "partial").write_text("incomplete")
        state_path = self.home / setup_command.STATE_RELATIVE
        state_path.parent.mkdir(parents=True)
        state_path.write_text(json.dumps({
            "mcp": "pending",
            "skills": [{"path": str(skill), "digest": "not-complete", "pending": True}],
        }))
        present = mock.Mock(returncode=0, stdout="configured", stderr="")
        with mock.patch.object(setup_command.shutil, "which", return_value="/tmp/claude"), mock.patch.object(
            setup_command.subprocess, "run", return_value=present
        ) as run:
            result = setup_command.run_setup(self.args(mcp=True))
        self.assertTrue(result["ok"])
        self.assertEqual(run.call_args.args[0][:3], ["claude", "mcp", "get"])
        self.assertEqual(json.loads(state_path.read_text())["mcp"], True)
        with mock.patch.object(setup_command.shutil, "which", return_value=None):
            setup_command.run_setup(self.args(uninstall=True))
        self.assertFalse(skill.exists())

    def test_failed_install_removes_its_state_backups(self) -> None:
        missing = mock.Mock(returncode=1, stdout="", stderr="not found")
        failure = subprocess.CalledProcessError(1, ["claude", "mcp", "add-json"], stderr="failed")
        with mock.patch.object(setup_command.shutil, "which", return_value="/tmp/claude"), mock.patch.object(
            setup_command.subprocess, "run", side_effect=[missing, failure]
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                setup_command.run_setup(self.args(claude=True))
        backup_root = self.home / setup_command.STATE_RELATIVE.parent / "backups"
        self.assertFalse(backup_root.exists() and any(backup_root.iterdir()))

    def test_uninstall_does_not_recreate_deleted_settings(self) -> None:
        missing = mock.Mock(returncode=1, stdout="", stderr="not found")
        ok = mock.Mock(returncode=0, stdout="ok", stderr="")
        with mock.patch.object(setup_command.shutil, "which", return_value="/tmp/claude"), mock.patch.object(
            setup_command.subprocess, "run", side_effect=[missing, ok]
        ):
            setup_command.run_setup(self.args(claude=True))
        self.settings.unlink()
        with mock.patch.object(setup_command.shutil, "which", return_value=None):
            result = setup_command.run_setup(self.args(uninstall=True))
        self.assertTrue(result["ok"])
        self.assertFalse(self.settings.exists())
        self.assertTrue(any("already absent" in action for action in result["actions"]))

    def test_backups_live_in_state_keep_only_newest_and_are_removed_on_uninstall(self) -> None:
        missing = mock.Mock(returncode=1, stdout="", stderr="not found")
        ok = mock.Mock(returncode=0, stdout="ok", stderr="")
        with mock.patch.object(setup_command.shutil, "which", return_value="/tmp/claude"), mock.patch.object(
            setup_command.subprocess, "run", side_effect=[missing, ok]
        ):
            setup_command.run_setup(self.args(claude=True))
        state_path = self.home / setup_command.STATE_RELATIVE
        first = Path(json.loads(state_path.read_text())["settings"]["backup"])
        self.assertTrue(first.resolve().is_relative_to((state_path.parent / "backups").resolve()))
        settings = json.loads(self.settings.read_text())
        settings["hooks"]["Stop"] = []
        self.settings.write_text(json.dumps(settings))
        setup_command.run_setup(self.args(claude=True))
        second = Path(json.loads(state_path.read_text())["settings"]["backup"])
        self.assertNotEqual(first, second)
        self.assertFalse(first.exists())
        self.assertEqual(len(list(first.parent.glob("*.bak"))), 1)
        self.assertTrue(second.exists())
        with mock.patch.object(setup_command.shutil, "which", return_value=None):
            setup_command.run_setup(self.args(uninstall=True))
        self.assertFalse(second.exists())

    def test_uninstall_restores_exact_bytes_or_preserves_current_json_style(self) -> None:
        compact = b'{"permissions":{"allow":["Read"]}}\n'
        self.settings.write_bytes(compact)
        missing = mock.Mock(returncode=1, stdout="", stderr="not found")
        ok = mock.Mock(returncode=0, stdout="ok", stderr="")
        with mock.patch.object(setup_command.shutil, "which", return_value="/tmp/claude"), mock.patch.object(
            setup_command.subprocess, "run", side_effect=[missing, ok]
        ):
            setup_command.run_setup(self.args(claude=True))
        with mock.patch.object(setup_command.shutil, "which", return_value=None):
            setup_command.run_setup(self.args(uninstall=True))
        self.assertEqual(self.settings.read_bytes(), compact)

        with mock.patch.object(setup_command.shutil, "which", return_value="/tmp/claude"), mock.patch.object(
            setup_command.subprocess, "run", side_effect=[missing, ok]
        ):
            setup_command.run_setup(self.args(claude=True))
        changed = json.loads(self.settings.read_text())
        changed["custom"] = {"text": "é"}
        self.settings.write_text(json.dumps(changed, indent=4, ensure_ascii=False) + "\n")
        with mock.patch.object(setup_command.shutil, "which", return_value=None):
            setup_command.run_setup(self.args(uninstall=True))
        rendered = self.settings.read_text()
        self.assertIn('\n    "custom"', rendered)
        self.assertIn("é", rendered)
        self.assertNotIn('"SessionStart"', rendered)

    def test_rerun_backup_does_not_erase_user_edits_on_uninstall(self) -> None:
        self.settings.write_text('{"model":"x"}\n')
        missing = mock.Mock(returncode=1, stdout="", stderr="not found")
        ok = mock.Mock(returncode=0, stdout="ok", stderr="")
        with mock.patch.object(setup_command.shutil, "which", return_value="/tmp/claude"), mock.patch.object(
            setup_command.subprocess, "run", side_effect=[missing, ok]
        ):
            setup_command.run_setup(self.args(claude=True))

        changed = json.loads(self.settings.read_text())
        changed.pop("hooks")
        changed["permissions"] = {"allow": ["USER-EDIT"]}
        self.settings.write_text(json.dumps(changed, separators=(",", ":")) + "\n")
        setup_command.run_setup(self.args(claude=True))

        with mock.patch.object(setup_command.shutil, "which", return_value=None):
            result = setup_command.run_setup(self.args(uninstall=True))
        self.assertTrue(result["ok"])
        final = json.loads(self.settings.read_text())
        self.assertEqual(final["model"], "x")
        self.assertEqual(final["permissions"], {"allow": ["USER-EDIT"]})
        self.assertNotIn("hooks", final)

    def test_dry_run_uninstall_keeps_backup_when_target_is_missing(self) -> None:
        missing = mock.Mock(returncode=1, stdout="", stderr="not found")
        ok = mock.Mock(returncode=0, stdout="ok", stderr="")
        with mock.patch.object(setup_command.shutil, "which", return_value="/tmp/claude"), mock.patch.object(
            setup_command.subprocess, "run", side_effect=[missing, ok]
        ):
            setup_command.run_setup(self.args(claude=True))
        state_path = self.home / setup_command.STATE_RELATIVE
        backup = Path(json.loads(state_path.read_text())["settings"]["backup"])
        self.settings.unlink()
        result = setup_command.run_setup(self.args(uninstall=True, dry_run=True))
        self.assertTrue(result["dry_run"])
        self.assertTrue(backup.exists())
        self.assertTrue(state_path.exists())

    def test_successful_uninstall_removes_empty_setup_directories(self) -> None:
        missing = mock.Mock(returncode=1, stdout="", stderr="not found")
        ok = mock.Mock(returncode=0, stdout="ok", stderr="")
        with mock.patch.object(setup_command.shutil, "which", return_value="/tmp/claude"), mock.patch.object(
            setup_command.subprocess, "run", side_effect=[missing, ok]
        ):
            setup_command.run_setup(self.args(claude=True))
        with mock.patch.object(setup_command.shutil, "which", return_value=None):
            setup_command.run_setup(self.args(uninstall=True))
        self.assertFalse((self.home / ".claude" / "skills").exists())
        self.assertFalse((self.home / ".config" / "agent-maildir").exists())

    def test_failed_mcp_add_rolls_back_local_setup(self) -> None:
        original = self.settings.read_bytes()
        missing = mock.Mock(returncode=1, stdout="", stderr="not found")
        failure = subprocess.CalledProcessError(1, ["claude", "mcp", "add-json"], stderr="failed")
        with mock.patch.object(setup_command.shutil, "which", return_value="/tmp/claude"), mock.patch.object(
            setup_command.subprocess, "run", side_effect=[missing, failure]
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                setup_command.run_setup(self.args(claude=True, codex=True))
        self.assertEqual(self.settings.read_bytes(), original)
        self.assertFalse((self.home / ".claude" / "skills" / "agent-maildir").exists())
        self.assertFalse((self.home / ".codex" / "skills" / "agent-maildir").exists())
        self.assertFalse((self.home / ".codex" / "config.toml").exists())
        self.assertFalse((self.home / setup_command.STATE_RELATIVE).exists())

    def test_failed_skill_copy_removes_partial_directory_and_config(self) -> None:
        destination = self.home / ".codex" / "skills" / "agent-maildir"

        def partial_copy(source: Path, target: Path) -> None:
            target.mkdir(parents=True)
            (target / "partial").write_text("incomplete")
            raise OSError("copy interrupted")

        with mock.patch.object(setup_command.shutil, "copytree", side_effect=partial_copy):
            with self.assertRaisesRegex(OSError, "copy interrupted"):
                setup_command.run_setup(self.args(codex=True))
        self.assertFalse(destination.exists())
        self.assertFalse((self.home / ".codex" / "config.toml").exists())
        self.assertFalse((self.home / setup_command.STATE_RELATIVE).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
