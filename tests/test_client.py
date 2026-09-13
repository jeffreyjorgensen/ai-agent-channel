"""The HTTP/TLS/token code the stop-hook and the status command share."""

from __future__ import annotations

import io
import os
import ssl
import threading
import urllib.error
from collections.abc import Iterator
from contextlib import contextmanager
from email.message import Message
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib import metadata
from typing import Any

import pytest

from ai_agent_channel import client


@pytest.fixture(autouse=True)
def _no_channel_env(monkeypatch):
    for name in (client.URL_ENV, client.TOKEN_ENV, client.TOKEN_FILE_ENV):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    "url", ["https://host/mcp", "https://host/mcp/", "https://host", " https://host/ "]
)
def test_mcp_url_is_accepted_verbatim(url):
    """Users paste the URL from their MCP config; stripping /mcp here saves
    a class of confusing 404s."""
    assert client.base_url(url) == "https://host"


def test_no_url_means_local_mode(monkeypatch):
    monkeypatch.setenv(client.TOKEN_ENV, "cct_x")
    assert client.remote_config() is None


def test_remote_config_from_env(monkeypatch):
    monkeypatch.setenv(client.URL_ENV, "https://host/mcp")
    monkeypatch.setenv(client.TOKEN_ENV, " cct_x\n")
    assert client.remote_config() == ("https://host", "cct_x")


def test_token_file_wins_over_the_env_var(tmp_path, monkeypatch):
    secret = tmp_path / "token"
    secret.write_text("cct_from_file\n")
    secret.chmod(0o600)
    monkeypatch.setenv(client.URL_ENV, "https://host")
    monkeypatch.setenv(client.TOKEN_ENV, "cct_from_env")
    monkeypatch.setenv(client.TOKEN_FILE_ENV, str(secret))
    assert client.remote_config() == ("https://host", "cct_from_file")


def test_token_file_must_not_be_readable_by_others(tmp_path, monkeypatch):
    secret = tmp_path / "token"
    secret.write_text("cct_x")
    secret.chmod(0o644)
    monkeypatch.setenv(client.TOKEN_FILE_ENV, str(secret))
    with pytest.raises(client.TokenFileError, match="readable by others"):
        client.read_token()
    secret.chmod(0o600)
    assert client.read_token() == "cct_x"


def test_missing_token_file_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv(client.TOKEN_FILE_ENV, str(tmp_path / "nope"))
    with pytest.raises(client.TokenFileError, match=client.TOKEN_FILE_ENV):
        client.read_token()


def test_empty_token_file_is_an_error_not_local_mode(tmp_path, monkeypatch):
    secret = tmp_path / "token"
    secret.write_text("\n")
    secret.chmod(0o600)
    monkeypatch.setenv(client.URL_ENV, "https://host")
    monkeypatch.setenv(client.TOKEN_FILE_ENV, str(secret))
    with pytest.raises(client.TokenFileError, match="empty"):
        client.remote_config()


def test_user_agent_carries_the_package_version():
    assert client.USER_AGENT.startswith(f"ai-agent-channel/{metadata.version('ai-agent-channel')} ")


def test_version_falls_back_when_not_installed(monkeypatch):
    def not_installed(_name):
        raise metadata.PackageNotFoundError

    monkeypatch.setattr(client.metadata, "version", not_installed)
    assert client._version() == "0+unknown"


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_get_json_sends_headers_and_verifies_certificates(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout=None, context=None):
        seen.update(
            url=request.full_url,
            ua=request.get_header("User-agent"),
            auth=request.get_header("Authorization"),
            context=context,
        )
        return _Response(b'{"ok": true}')

    monkeypatch.setattr("ai_agent_channel.client._urlopen", fake_urlopen)
    assert client.get_json("https://host", "/status", "cct_x", timeout=1) == {"ok": True}
    assert seen["url"] == "https://host/status"
    assert seen["ua"] == client.USER_AGENT
    assert seen["auth"] == "Bearer cct_x"
    assert seen["context"].verify_mode == ssl.CERT_REQUIRED
    assert seen["context"].check_hostname is True


def test_get_json_reports_the_http_status(monkeypatch):
    def rejected(request, timeout=None, context=None):
        raise urllib.error.HTTPError(
            request.full_url, 401, "Unauthorized", Message(), io.BytesIO(b"")
        )

    monkeypatch.setattr("ai_agent_channel.client._urlopen", rejected)
    with pytest.raises(client.ClientError) as err:
        client.get_json("https://host", "/hook-status", "cct_x", timeout=1)
    assert err.value.status == 401
    assert "/hook-status" in str(err.value)


def test_get_json_transport_failure_has_no_status(monkeypatch):
    def refused(request, timeout=None, context=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("ai_agent_channel.client._urlopen", refused)
    with pytest.raises(client.ClientError) as err:
        client.get_json("https://host", "/status", "cct_x", timeout=1)
    assert err.value.status is None


# --- redirects never carry the token -----------------------------------------


class _Recorder(BaseHTTPRequestHandler):
    def do_GET(self):
        server = self.server
        server.seen.append((self.path, self.headers.get("Authorization")))  # type: ignore[attr-defined]
        if server.redirect_to:  # type: ignore[attr-defined]
            self.send_response(server.code)  # type: ignore[attr-defined]
            self.send_header("Location", server.redirect_to)  # type: ignore[attr-defined]
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


@contextmanager
def _recording_server(redirect_to: str = "", code: int = 302) -> Iterator[HTTPServer]:
    httpd = HTTPServer(("127.0.0.1", 0), _Recorder)
    httpd.seen = []  # type: ignore[attr-defined]
    httpd.redirect_to = redirect_to  # type: ignore[attr-defined]
    httpd.code = code  # type: ignore[attr-defined]
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_a_redirect_is_refused_and_the_token_goes_nowhere_else(code):
    """urllib copies Authorization onto the redirected request, to whatever
    host and port the Location names."""
    with _recording_server() as elsewhere:
        target = f"http://127.0.0.1:{elsewhere.server_port}/collect"
        with _recording_server(redirect_to=target, code=code) as channel:
            with pytest.raises(client.ClientError) as err:
                client.get_json(
                    f"http://127.0.0.1:{channel.server_port}", "/status", "cct_secret", timeout=5
                )
            assert channel.seen == [("/status", "Bearer cct_secret")]  # type: ignore[attr-defined]
        assert elsewhere.seen == []  # type: ignore[attr-defined]
    assert err.value.status == code
    assert err.value.misconfigured is True
    assert client.URL_ENV in str(err.value)


def test_a_plain_answer_from_loopback_still_works():
    with _recording_server() as channel:
        url = f"http://localhost:{channel.server_port}"
        assert client.get_json(url, "/status", "cct_x", timeout=5) == {"ok": True}


@pytest.mark.parametrize(
    "url",
    [
        "http://channel.example.com",
        "http://10.0.0.5:8765",
        "http://127.0.0.1.nip.io",
        "http://localhost.example.com",
    ],
)
def test_plain_http_is_refused_except_to_loopback(url, monkeypatch):
    def must_not_connect(*args, **kwargs):
        raise AssertionError("a request was made")

    monkeypatch.setattr("ai_agent_channel.client._urlopen", must_not_connect, raising=False)
    with pytest.raises(client.ClientError, match="https://") as err:
        client.get_json(url, "/status", "cct_x", timeout=1)
    assert err.value.misconfigured is True


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]"])
def test_plain_http_to_loopback_is_allowed(host):
    client.check_url(f"http://{host}:8765")


def test_other_schemes_are_refused():
    with pytest.raises(client.ClientError, match="only http"):
        client.get_json("file:///etc", "/passwd", "cct_x", timeout=1)


# --- the token file is opened safely -----------------------------------------

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and modes")


@posix_only
def test_a_symlinked_token_file_is_refused(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.write_text("cct_x")
    real.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.setenv(client.TOKEN_FILE_ENV, str(link))
    with pytest.raises(client.TokenFileError, match="symbolic link"):
        client.read_token()


@posix_only
def test_a_token_file_owned_by_someone_else_is_refused(tmp_path, monkeypatch):
    secret = tmp_path / "token"
    secret.write_text("cct_x")
    secret.chmod(0o600)
    owner = secret.stat().st_uid
    monkeypatch.setattr(os, "getuid", lambda: owner + 1)
    monkeypatch.setenv(client.TOKEN_FILE_ENV, str(secret))
    with pytest.raises(client.TokenFileError, match="owned by uid"):
        client.read_token()


@posix_only
def test_a_group_writable_token_file_is_refused(tmp_path, monkeypatch):
    secret = tmp_path / "token"
    secret.write_text("cct_x")
    secret.chmod(0o620)
    monkeypatch.setenv(client.TOKEN_FILE_ENV, str(secret))
    with pytest.raises(client.TokenFileError, match="chmod 600"):
        client.read_token()


@posix_only
def test_a_directory_is_not_a_token_file(tmp_path, monkeypatch):
    folder = tmp_path / "dir"
    folder.mkdir(mode=0o700)
    monkeypatch.setenv(client.TOKEN_FILE_ENV, str(folder))
    with pytest.raises(client.TokenFileError):
        client.read_token()


@posix_only
def test_a_fifo_does_not_hang_the_reader(tmp_path, monkeypatch):
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o600)
    monkeypatch.setenv(client.TOKEN_FILE_ENV, str(fifo))
    with pytest.raises(client.TokenFileError, match="not a regular file"):
        client.read_token()


def test_a_token_file_that_is_not_text_is_an_error(tmp_path, monkeypatch):
    secret = tmp_path / "token"
    secret.write_bytes(b"\xff\xfe")
    secret.chmod(0o600)
    monkeypatch.setenv(client.TOKEN_FILE_ENV, str(secret))
    with pytest.raises(client.TokenFileError, match=client.TOKEN_FILE_ENV):
        client.read_token()
