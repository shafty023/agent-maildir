FROM python:3.13.9-slim@sha256:326df678c20c78d465db501563f3492d17c42a4afe33a1f2bf5406a1d56b0e86

# This is a disposable mailbox used only for MCP introspection in the container.
ENV AGENT_MAILDIR_ROOT=/tmp/agent-maildir \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE NOTICE MANIFEST.in ./
COPY src ./src

RUN python -m pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 agentmaildir \
    && install -d -o agentmaildir -g agentmaildir "${AGENT_MAILDIR_ROOT}"

USER agentmaildir

ENTRYPOINT ["agent-maildir-mcp"]
