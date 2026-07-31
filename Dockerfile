FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
COPY gitpilot/ gitpilot/
COPY README.md .
COPY LICENSE .

RUN pip install --no-cache-dir -e .

VOLUME /workspace
VOLUME /root/.gitpilot

ENV PYTHONUNBUFFERED=1

CMD ["gitpilotd"]