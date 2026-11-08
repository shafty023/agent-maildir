#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Exercise an MCP stdio command with initialize and tools/list requests."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any


REQUESTS = [
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "agent-maildir-container-check", "version": "1"},
        },
    },
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
]


def response_by_id(responses: list[dict[str, Any]], request_id: int) -> dict[str, Any]:
    for response in responses:
        if response.get("id") == request_id:
            return response
    raise ValueError(f"missing response for request id {request_id}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required after --")

    payload = "".join(json.dumps(request, separators=(",", ":")) + "\n" for request in REQUESTS)
    try:
        completed = subprocess.run(command, input=payload, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired as error:
        print(json.dumps({"command": command, "error": f"timed out after {error.timeout}s"}, indent=2), file=sys.stderr)
        return 1

    parsed: list[dict[str, Any]] = []
    parse_errors: list[str] = []
    for line in completed.stdout.splitlines():
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError as error:
            parse_errors.append(f"{error}: {line}")

    transcript = {
        "command": command,
        "exit_code": completed.returncode,
        "sent": REQUESTS,
        "received": parsed,
        "stderr": completed.stderr,
    }
    print(json.dumps(transcript, indent=2, sort_keys=True))

    try:
        if completed.returncode != 0:
            raise ValueError(f"command exited {completed.returncode}")
        if parse_errors:
            raise ValueError("non-JSON stdout: " + "; ".join(parse_errors))
        initialized = response_by_id(parsed, 1)
        tools_list = response_by_id(parsed, 2)
        if initialized.get("error"):
            raise ValueError(f"initialize error: {initialized['error']}")
        server_info = initialized.get("result", {}).get("serverInfo", {})
        if server_info.get("name") != "agent-maildir":
            raise ValueError(f"unexpected server name: {server_info.get('name')!r}")
        if server_info.get("version") != args.expected_version:
            raise ValueError(f"unexpected server version: {server_info.get('version')!r}")
        if tools_list.get("error"):
            raise ValueError(f"tools/list error: {tools_list['error']}")
        tools = tools_list.get("result", {}).get("tools")
        if not isinstance(tools, list) or not tools:
            raise ValueError("tools/list returned no tools")
    except ValueError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1

    print(f"PASS: initialize reported agent-maildir {args.expected_version}; tools/list returned {len(tools)} tools")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
