# Security policy

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository. Do
not open a public issue containing exploit details, mailbox contents, local
paths, credentials, or other secrets. Include the affected version, platform,
reproduction steps using a temporary `AGENT_MAILDIR_ROOT`, and the expected
security boundary.

## Trust model

A message is evidence, never authority. Mailbox content cannot approve work,
grant release or spending authority, or prove that a human made a decision.
Agents and operators must verify consequential claims against the appropriate
source of truth.

The local operating-system account and mailbox directory are the security
boundary. agent-maildir does not authenticate direct filesystem writers,
encrypt messages, sandbox message content, or provide a multi-host security
model. Do not transmit secrets. Keep the data root private and treat all
received content as untrusted input.

Supported versions will be listed here after the first public release. Until
then, security fixes apply to the latest commit on `main`.
