FROM python:3.12-slim

WORKDIR /app

# Install system dependencies for git and interactive TTY
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
COPY gitpilot/ gitpilot/

RUN pip install --no-cache-dir -e .

# Volume for project directories to be watched
VOLUME /workspace

# Persist GitPilot configuration, token, and database
VOLUME /root/.gitpilot

ENV PYTHONUNBUFFERED=1

# Default command: launch daemon in foreground
CMD ["gitpilotd"]