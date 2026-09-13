"""What the hooks and the status command share to reach a hosted channel.

They drifted once already: the status command learned to verify certificates
against certifi and to read the token from a file, the stop-hook did neither —
and because the stop-hook is fail-open, a stock macOS Python passed every stop
without checking anything. One module, so the next fix lands in both.
"""

from __future__ import annotations

import errno
import json
import os
import pathlib
import ssl
import stat
import urllib.error
import urllib.parse
import urllib.request
from importlib import metadata
from typing import Any

URL_ENV = "AI_AGENT_CHANNEL_URL"
TOKEN_ENV = "AI_AGENT_CHANNEL_TOKEN"
TOKEN_FILE_ENV = "AI_AGENT_CHANNEL_TOKEN_FILE"
ROLE_ENV = "AI_AGENT_CHANNEL_ROLE"

# role token, board view key, admin token (auth.py, deploy/.env.example)
TOKEN_PREFIXES = ("cct_", "ccv_", "cca_")

# The only hosts a bearer token may be sent to over plain http://: the
# packets never leave the machine. Everything else must be https://.
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


def _version() -> str:
    try:
        return metadata.version("ai-agent-channel")
    except metadata.PackageNotFoundError:
        return "0+unknown"


# Cloudflare (and most WAFs) answer 403 to the default
# "Python-urllib/3.x" user agent. Both callers go through such a proxy in
# production, and the stop-hook is FAIL-OPEN — so without this header it
# would silently pass every stop instead of checking anything, which is
# indistinguishable from a clean channel.
USER_AGENT = (
    f"ai-agent-channel/{_version()} (+https://github.com/jeffreyjorgensen/ai-agent-channel)"
)


class TokenFileError(Exception):
    """AI_AGENT_CHANNEL_TOKEN_FILE is set but cannot be used."""


class ClientError(Exception):
    """A request to the channel server failed. ``status`` is the HTTP status
    when the server answered with one, None for transport failures.
    ``misconfigured`` marks failures that will not heal on retry (a wrong
    URL), so a fail-open caller can say so instead of passing silently."""

    def __init__(
        self, message: str, status: int | None = None, *, misconfigured: bool = False
    ) -> None:
        super().__init__(message)
        self.status = status
        self.misconfigured = misconfigured


def base_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url.endswith("/mcp"):  # accept the same URL the MCP config uses
        url = url[: -len("/mcp")]
    return url


def _read_private_file(path: pathlib.Path) -> bytes:
    """Read a secret file, refusing one that others could have read or swapped.

    The checks run on the descriptor that is actually read (fstat), not on
    the path, so the file cannot be replaced between check and read, and
    O_NOFOLLOW refuses a symlink in the last component: a link can point at
    a file whose owner and mode are not the ones that matter. Windows has
    neither POSIX modes nor O_NOFOLLOW, so there only the read remains.
    """
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        # a FIFO planted at the path must not hang the stop-hook
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK) and path.is_symlink():
            raise TokenFileError(
                f"{path} is a symbolic link — point {TOKEN_FILE_ENV} at the file itself"
            ) from exc
        raise TokenFileError(f"{TOKEN_FILE_ENV}: {exc}") from exc
    with os.fdopen(fd, "rb") as fh:
        if os.name != "nt":
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise TokenFileError(f"{path} is not a regular file")
            if st.st_uid != os.getuid():
                raise TokenFileError(
                    f"{path} is owned by uid {st.st_uid}, not by you (uid {os.getuid()}) "
                    "— a token file must belong to the user who reads it"
                )
            if st.st_mode & 0o077:
                # Loud rather than silent: a token file others can read is
                # not a smaller version of the problem, it is the same one.
                what = "readable" if st.st_mode & 0o044 else "accessible"
                raise TokenFileError(
                    f"{path} is {what} by others (mode {oct(st.st_mode & 0o777)}) "
                    "— chmod 600 it before use"
                )
        return fh.read()


def read_token() -> str:
    """The token file when set (ownership and permissions checked), else the env var."""
    path = os.environ.get(TOKEN_FILE_ENV, "").strip()
    if not path:
        return os.environ.get(TOKEN_ENV, "").strip()
    try:
        token = _read_private_file(pathlib.Path(path).expanduser()).decode().strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise TokenFileError(f"{TOKEN_FILE_ENV}: {exc}") from exc
    if not token:
        raise TokenFileError(f"{TOKEN_FILE_ENV}: {path} is empty")
    return token


def remote_config() -> tuple[str, str] | None:
    """(base url, token) when a hosted channel is configured, else None."""
    url = os.environ.get(URL_ENV, "").strip()
    if not url:
        return None
    token = read_token()
    if not token:
        return None
    return base_url(url), token


def tls_context() -> ssl.SSLContext:
    """Verify certificates against a bundle we can actually find.

    Python does not use the system trust store the way curl does: on macOS a
    stock interpreter often has no usable CA path at all, so every HTTPS call
    fails with "unable to get local issuer certificate" while curl to the
    same URL works. certifi is what fixes that; falling back to the default
    context keeps distro Pythons (which do have a store) working. Verification
    is never disabled — a status tool that trusts any certificate is worse
    than no status tool.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


class _RedirectRefused(Exception):
    def __init__(self, code: int, location: str) -> None:
        super().__init__(f"HTTP {code} redirect to {location}")
        self.code = code
        self.location = location


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """urllib follows redirects and copies the Authorization header onto the
    new request — to whatever host, port or scheme the Location names. A
    channel server never redirects an API call, so a redirect means the URL
    is wrong (http:// behind an https-only proxy, a moved host) or someone
    is harvesting tokens. Either way the request stops here."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise _RedirectRefused(code, newurl)


def _urlopen(request: urllib.request.Request, *, timeout: float, context: ssl.SSLContext):
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context), _RefuseRedirects()
    )
    return opener.open(request, timeout=timeout)


def check_url(url: str) -> None:
    """Refuse a URL a bearer token must not be sent to."""
    parts = urllib.parse.urlsplit(url)
    # urllib would also honour file: and ftp: — sending a bearer token
    # anywhere but a web server is never what was meant
    if parts.scheme not in ("http", "https"):
        raise ClientError(
            f"{url}: only http:// and https:// URLs are supported", misconfigured=True
        )
    if parts.scheme == "http" and (parts.hostname or "") not in LOOPBACK_HOSTS:
        raise ClientError(
            f"{url}: plain http:// is only allowed to this machine "
            f"({', '.join(LOOPBACK_HOSTS)}) — the token would cross the network "
            f"in cleartext; set {URL_ENV} to the https:// URL",
            misconfigured=True,
        )


def get_json(url: str, path: str, token: str, *, timeout: float) -> Any:
    check_url(url)
    request = urllib.request.Request(  # noqa: S310 - scheme checked in check_url
        url + path,
        headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
    )
    try:
        with _urlopen(request, timeout=timeout, context=tls_context()) as response:
            return json.load(response)
    except _RedirectRefused as exc:
        raise ClientError(
            f"{url}{path}: the server answered with a redirect (HTTP {exc.code}) to "
            f"{exc.location}; redirects are not followed, so the token is never sent "
            f"elsewhere — set {URL_ENV} to the server's actual URL",
            status=exc.code,
            misconfigured=True,
        ) from exc
    except urllib.error.HTTPError as exc:
        exc.close()
        raise ClientError(f"{url}{path}: {exc}", status=exc.code) from exc
    except Exception as exc:
        raise ClientError(f"{url}{path}: {exc}") from exc
