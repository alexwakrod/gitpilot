"""Daemon entry point: starts the FastAPI server on a loopback port."""

import logging
import logging.config
import os
import signal
import sys
import time
import json
import socket
from pathlib import Path

import uvicorn

from gitpilot.domain.policies import ensure_token_file, get_token_path
from gitpilot.domain.settings import SettingsManager
from gitpilot.infrastructure.db import initialize_database
from gitpilot.daemon.app import create_app
from gitpilot.daemon.lifecycle import DaemonLifecycle


LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"


def setup_logging(log_dir: Path) -> None:
    """Configure structured JSON logging to file and console."""
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {
                "()": "pythonjsonlogger.jsonlogger.JsonFormatter",
                "format": LOG_FORMAT,
                "datefmt": LOG_DATE_FORMAT,
            },
            "console": {
                "format": LOG_FORMAT,
                "datefmt": LOG_DATE_FORMAT,
            },
        },
        "handlers": {
            "file": {
                "class": "logging.handlers.TimedRotatingFileHandler",
                "filename": str(log_dir / "daemon.log"),
                "when": "midnight",
                "backupCount": 7,
                "encoding": "utf-8",
                "formatter": "json",
            },
            "console": {
                "class": "logging.StreamHandler",
                "stream": sys.stdout,
                "formatter": "console",
            },
        },
        "root": {
            "level": os.environ.get("LOG_LEVEL", "INFO"),
            "handlers": ["file", "console"],
        },
    })


def find_free_port() -> int:
    """Find a free TCP port on loopback."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def save_port_file(port: int) -> Path:
    """Save the chosen port to a well-known file."""
    port_path = get_token_path().with_name("daemon_port")
    port_path.write_text(str(port))
    port_path.chmod(0o600)
    return port_path


def main() -> None:
    """Start the GitPilot daemon."""
    # Token and configuration
    token = ensure_token_file()
    settings_mgr = SettingsManager()
    config = settings_mgr.load()

    # Setup logging directory
    log_dir = settings_mgr.config_path.parent / "logs"
    setup_logging(log_dir)
    logger = logging.getLogger("gitpilotd")
    logger.info("Starting GitPilot daemon")

    # Initialize database
    initialize_database()

    # Create lifecycle manager (watcher, committer, executor)
    lifecycle = DaemonLifecycle(config=config)

    # Create FastAPI application
    app = create_app(
        api_token=token,
        lifecycle=lifecycle,
        config=config,
    )

    # Find a free port and save it
    port = find_free_port()
    port_path = save_port_file(port)
    logger.info("Daemon API listening on 127.0.0.1:%d", port)

    # Configure uvicorn
    uvicorn_config = uvicorn.Config(
        app=app,
        host="127.0.0.1",
        port=port,
        log_level="info",
        log_config=None,  # we already set up logging
        access_log=False,
    )
    server = uvicorn.Server(uvicorn_config)

    # Graceful shutdown handling
    def shutdown_handler(signum, frame):
        logger.info("Received signal %d, shutting down...", signum)
        server.should_exit = True

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    try:
        # Start file watchers before running the server
        lifecycle.start()
        server.run()
    except Exception:
        logger.exception("Daemon crashed")
    finally:
        lifecycle.stop()
        if port_path.exists():
            port_path.unlink()
        logger.info("Daemon stopped")


if __name__ == "__main__":
    main()