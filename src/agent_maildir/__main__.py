# SPDX-License-Identifier: Apache-2.0
"""Run the agent-maildir CLI with ``python -m agent_maildir``."""

from .agent_mail import main


if __name__ == "__main__":
    raise SystemExit(main())
