# SPDX-License-Identifier: Apache-2.0
"""Release metadata and open-source hygiene guard tests."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def repository_files() -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout
    return [ROOT / item.decode() for item in listed.split(b"\0") if item]


class MetadataTests(unittest.TestCase):
    def test_first_run_and_help_point_to_harness_setup(self) -> None:
        cli = ROOT / "src" / "agent_maildir" / "agent_mail.py"
        for arguments in ([], ["--help"]):
            done = subprocess.run([sys.executable, str(cli), *arguments], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0)
            self.assertIn("agent-maildir setup --claude --codex", done.stdout)

        for script in ("mailbox_channel.py", "mailbox_hook.py"):
            done = subprocess.run(
                [sys.executable, str(ROOT / "src" / "agent_maildir" / script), "--help"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(done.returncode, 0)
            self.assertIn("usage:", done.stdout)

    def test_plugin_and_mcp_json_are_valid_and_consistently_named(self) -> None:
        plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
        marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
        mcp = json.loads((ROOT / ".mcp.json").read_text())
        registry = json.loads((ROOT / "server.json").read_text())
        hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())
        self.assertEqual(plugin["name"], "agent-maildir")
        self.assertEqual(marketplace["name"], "agent-maildir")
        self.assertEqual(marketplace["plugins"][0]["name"], "agent-maildir")
        self.assertIn("agent-maildir", mcp)
        self.assertEqual(set(hooks["hooks"]), {"SessionStart", "Stop"})
        self.assertEqual(registry["name"], "io.github.shafty023/agent-maildir")
        self.assertEqual(registry["packages"][0]["identifier"], "agent-maildir")
        self.assertEqual(registry["packages"][0]["runtimeHint"], "uvx")
        self.assertEqual(registry["packages"][0]["packageArguments"], [{"type": "positional", "value": "mcp"}])
        self.assertEqual(registry["packages"][0]["transport"], {"type": "stdio"})

    def test_versions_and_distribution_name_match(self) -> None:
        pyproject = (ROOT / "pyproject.toml").read_text()
        package = (ROOT / "src" / "agent_maildir" / "__init__.py").read_text()
        channel = (ROOT / "src" / "agent_maildir" / "mailbox_channel.py").read_text()
        checker = (ROOT / "scripts" / "check_mcp_stdio.py").read_text()
        plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
        registry = json.loads((ROOT / "server.json").read_text())
        project_name = re.search(r'(?m)^name = "([^"]+)"$', pyproject)
        project_version = re.search(r'(?m)^version = "([^"]+)"$', pyproject)
        package_version = re.search(r'__version__ = "([^"]+)"', package)
        self.assertEqual(project_name.group(1), "agent-maildir")
        self.assertEqual(project_version.group(1), package_version.group(1))
        self.assertEqual(project_version.group(1), plugin["version"])
        self.assertEqual(project_version.group(1), registry["version"])
        self.assertEqual(project_version.group(1), registry["packages"][0]["version"])
        self.assertIn(f'"version": "{project_version.group(1)}"', channel)
        self.assertIn('parser.add_argument("--expected-version", required=True)', checker)
        self.assertNotIn('default="0.1.1"', checker)
        self.assertIn("dependencies = []", pyproject)
        self.assertIn('requires = ["setuptools>=77"]', pyproject)
        self.assertNotIn("License :: OSI Approved", pyproject)
        self.assertFalse((ROOT / "setup.py").exists())

    def test_workflow_actions_are_sha_pinned_and_release_separates_publish(self) -> None:
        workflows = [(ROOT / ".github" / "workflows" / name).read_text() for name in ("ci.yml", "release.yml")]
        external_uses = re.findall(r"uses:\s+([^\s#]+)", "\n".join(workflows))
        for action in external_uses:
            if action.startswith("./"):
                continue
            self.assertRegex(action, r"^[^@]+@[0-9a-f]{40}$")
        ci, release = workflows
        self.assertEqual(ci.count("persist-credentials: false"), 2)
        self.assertIn("Install wheel in a clean venv", ci)
        self.assertIn("Verify tagged commit is on main", release)
        self.assertIn("git merge-base --is-ancestor", release)
        self.assertRegex(release, r"(?s)build:.*?permissions:\n\s+contents: read.*?publish:")
        self.assertRegex(release, r"(?s)publish:.*?id-token: write")

    def test_public_docs_notice_and_installed_command_guidance(self) -> None:
        readme = (ROOT / "README.md").read_text()
        notice = (ROOT / "NOTICE").read_text()
        hook = (ROOT / "src" / "agent_maildir" / "mailbox_hook.py").read_text()
        channel = (ROOT / "src" / "agent_maildir" / "mailbox_channel.py").read_text()
        skill = (ROOT / "src" / "agent_maildir" / "skills" / "agent-maildir" / "SKILL.md").read_text()
        self.assertEqual(notice.strip(), "Copyright 2026 the agent-maildir authors")
        for badge in ("actions/workflows/ci.yml/badge.svg", "pypi/v/agent-maildir", "pypi/pyversions/agent-maildir"):
            self.assertIn(badge, readme)
        self.assertIn("Maintained by [@shafty023]", readme)
        self.assertNotIn("badges populate when the first", readme)
        self.assertIn("<!-- mcp-name: io.github.shafty023/agent-maildir -->", readme)
        self.assertIn('"ack_created": true', readme)
        self.assertIn("## Why it exists", readme)
        self.assertIn("Why not just use `mail` or Sendmail?", readme)
        self.assertIn("read only when a mailbox root is first", readme)
        self.assertNotIn("repository handoff", readme)
        self.assertNotIn("Feishu", readme)
        self.assertNotIn("timestamped adjacent backup", readme)
        self.assertNotIn("agent_mail.py", hook)
        self.assertNotIn("agent_mail.py", channel)
        self.assertNotIn("demo-ceo", skill)
        self.assertNotIn("company.example-sites", skill)

    def test_container_files_use_the_installed_mcp_entrypoint(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text()
        check = (ROOT / "scripts" / "check_mcp_stdio.py").read_text()
        self.assertRegex(dockerfile.splitlines()[0], r"^FROM python:\d+\.\d+\.\d+-slim(?:@sha256:[a-f0-9]{64})?$")
        self.assertIn('ENTRYPOINT ["agent-maildir-mcp"]', dockerfile)
        self.assertIn("AGENT_MAILDIR_ROOT=/tmp/agent-maildir", dockerfile)
        self.assertIn("disposable mailbox used only for MCP introspection", dockerfile)
        self.assertIn('"method": "initialize"', check)
        self.assertIn('"method": "tools/list"', check)

    def test_python_sources_have_spdx_headers(self) -> None:
        sources = list((ROOT / "src").rglob("*.py")) + list((ROOT / "tests").rglob("*.py"))
        for path in sources:
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertIn("SPDX-License-Identifier: Apache-2.0", "\n".join(path.read_text().splitlines()[:3]))

    def test_forbidden_private_terms_are_absent(self) -> None:
        forbidden = (
            "do" + "choa",
            "pro" + "ciq",
            "local" + "fork",
            "sites-" + "ceo",
            "PRO" + "-",
            "/" + "Users/",
            "Mac" + "book",
            "gameboy" + "45",
        )
        candidates = repository_files()
        self.assertFalse(any(".venv" in path.parts for path in candidates))
        for path in candidates:
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for term in forbidden:
                with self.subTest(path=path.relative_to(ROOT), term=term):
                    self.assertNotIn(term.lower(), text.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
