FROM python:3.13-slim

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
