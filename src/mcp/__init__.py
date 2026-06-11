"""Optional MCP integration for REQL.

Importing this package does not require any MCP SDK. The pure tool handlers live
in :mod:`mcp.tools`; the stdio JSON-RPC transport lives in :mod:`mcp.server`.
"""
from __future__ import annotations

from .tools import MCPToolError, READ_ONLY_TOOLS, WRITE_TOOLS, call_tool, list_tools

__all__ = ["MCPToolError", "READ_ONLY_TOOLS", "WRITE_TOOLS", "call_tool", "list_tools"]
