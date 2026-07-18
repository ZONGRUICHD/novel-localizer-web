"""ASGI entrypoint used by the production systemd unit."""

from .app import create_app

app = create_app()
