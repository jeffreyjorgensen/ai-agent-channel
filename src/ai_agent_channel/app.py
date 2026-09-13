"""The single FastMCP instance every tool is registered on."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("ai-agent-channel")
