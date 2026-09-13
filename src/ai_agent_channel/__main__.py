from __future__ import annotations

import argparse

from .server import serve


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="ai-agent-channel",
        description=(
            "MCP mailbox for coordinating Claude Code sessions. Default: "
            "stdio transport (one local channel, identity from "
            "AI_AGENT_CHANNEL_ROLE). --http: shared multi-channel server "
            "(identity from bearer tokens, admin token required)."
        ),
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="serve streamable HTTP instead of stdio",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host")
    parser.add_argument("--port", type=int, default=8765, help="HTTP bind port")
    args = parser.parse_args(argv)
    if args.http:
        from .http import serve_http

        serve_http(host=args.host, port=args.port)
    else:
        serve()


if __name__ == "__main__":
    main()
