"""The deploy/ files: settings documented in .env.example reach the
container, the image checks its own health, and the proxies send HSTS."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
COMPOSE_FILES = ["docker-compose.yml", "docker-compose.vps.yml"]


def _example_settings() -> set[str]:
    text = (DEPLOY / ".env.example").read_text(encoding="utf-8")
    return set(re.findall(r"^#?(AI_AGENT_CHANNEL_[A-Z0-9_]+)=", text, re.MULTILINE))


def test_the_example_documents_the_tunable_settings():
    assert {
        "AI_AGENT_CHANNEL_ADMIN_TOKEN",
        "AI_AGENT_CHANNEL_ALLOWED_HOSTS",
        "AI_AGENT_CHANNEL_TRUST_PROXY",
        "AI_AGENT_CHANNEL_STATUS_MAX_WAITERS",
        "AI_AGENT_CHANNEL_STATUS_MAX_WAITERS_PER_TOKEN",
        "AI_AGENT_CHANNEL_VIEW_KEY_TTL_DAYS",
    } <= _example_settings()


@pytest.mark.parametrize("compose", COMPOSE_FILES)
def test_every_example_setting_reaches_the_container(compose):
    """A variable set in .env but not listed under `environment:` never
    reaches the process — the setting silently does nothing."""
    text = (DEPLOY / compose).read_text(encoding="utf-8")
    missing = [
        var
        for var in sorted(_example_settings())
        if not re.search(rf"^\s+{var}: \$\{{{var}:[-?]", text, re.MULTILINE)
    ]
    assert not missing, f"{compose} does not pass through: {missing}"


def test_the_vps_compose_file_requires_nothing_but_the_admin_token():
    """The live deployment's .env holds only the admin token: an upgrade
    must not stop it from starting."""
    text = (DEPLOY / "docker-compose.vps.yml").read_text(encoding="utf-8")
    assert re.findall(r"\$\{(\w+):\?", text) == ["AI_AGENT_CHANNEL_ADMIN_TOKEN"]


def _healthcheck_command() -> list[str]:
    text = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
    match = re.search(r"^HEALTHCHECK [^\n]*\\\n\s*CMD (\[.*\])$", text, re.MULTILINE)
    assert match, "the image has no HEALTHCHECK in exec form"
    return json.loads(match.group(1))


def test_the_image_healthcheck_needs_no_curl():
    command = _healthcheck_command()
    assert command[:2] == ["python", "-c"]
    assert "/healthz" in command[2]


def test_the_image_healthcheck_passes_against_a_live_server_and_fails_without(http_server):
    code = _healthcheck_command()[2]
    port = str(urlsplit(http_server).port)
    assert "8765" in code
    alive = subprocess.run(
        [sys.executable, "-c", code.replace("8765", port)], capture_output=True, timeout=30
    )
    assert alive.returncode == 0, alive.stderr
    dead = subprocess.run(
        [sys.executable, "-c", code.replace("8765", "1")], capture_output=True, timeout=30
    )
    assert dead.returncode != 0


def test_the_proxies_send_hsts():
    caddy = (DEPLOY / "Caddyfile").read_text(encoding="utf-8")
    assert re.search(r'^\s*header Strict-Transport-Security "max-age=31536000"', caddy, re.M)
    nginx = (DEPLOY / "nginx-channel.conf").read_text(encoding="utf-8")
    assert re.search(
        r'^\s*add_header Strict-Transport-Security "max-age=31536000" always;', nginx, re.M
    )
