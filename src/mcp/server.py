"""Dependency-free MCP server for REQL.

The server implements the minimal JSON-RPC methods needed by MCP clients:
``initialize``, ``tools/list`` and ``tools/call``. Tool behavior lives in
``mcp.tools`` so the core package and tests do not depend on a transport.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, TextIO

from memory.config import (
    CONFIG_OVERRIDES_ENV,
    CONFIG_PATH_ENV,
    ConfigError,
    parse_config_override_assignments,
    parse_config_overrides,
)
from .tools import MCPToolError, call_tool, list_tools

SERVER_NAME = "reql-memory"
PROTOCOL_VERSION = "2026-06-16"


DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8765
DEFAULT_API_KEY_ENV = "REQL_MCP_API_KEY"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the optional REQL MCP server")
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default=None,
        help="Transport to use. Defaults to stdio unless HTTP-specific options are provided.",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Expose only read-only tools. Write tools are hidden from tools/list and rejected.",
    )
    parser.add_argument("--host", default=None, help=f"HTTP bind address, for example 127.0.0.1 or 0.0.0.0. Default: {DEFAULT_HTTP_HOST}.")
    parser.add_argument("--port", type=int, default=None, help=f"HTTP bind port. Default: {DEFAULT_HTTP_PORT}.")
    parser.add_argument("--api-key", default=None, help="HTTP API key. Required for HTTP transport unless provided through --api-key-env.")
    parser.add_argument("--config", default=None, help="Path to conf.yaml used by MCP tools")
    parser.add_argument(
        "--set",
        dest="config_overrides",
        action="append",
        default=[],
        metavar="SECTION.OPTION=VALUE",
        help="Override a config value for MCP tools; may be repeated",
    )
    parser.add_argument(
        "--api-key-env",
        default=DEFAULT_API_KEY_ENV,
        help=f"Environment variable used for the HTTP API key. Default: {DEFAULT_API_KEY_ENV}.",
    )
    args = parser.parse_args(argv)

    transport = args.transport
    if transport is None:
        transport = "http" if args.host is not None or args.port is not None or args.api_key is not None else "stdio"

    if args.config:
        os.environ[CONFIG_PATH_ENV] = args.config
    if args.config_overrides:
        try:
            existing = parse_config_overrides(os.environ.get(CONFIG_OVERRIDES_ENV, ""))
            existing.update(parse_config_override_assignments(args.config_overrides))
        except ConfigError as exc:
            parser.error(str(exc))
        os.environ[CONFIG_OVERRIDES_ENV] = json.dumps(existing, separators=(",", ":"))

    if transport == "stdio":
        return serve(sys.stdin, sys.stdout, include_write=not args.read_only)

    api_key = _resolve_api_key(args.api_key, args.api_key_env)
    if not api_key:
        parser.error(f"HTTP transport requires --api-key or a non-empty {args.api_key_env} environment variable")
    host = args.host or DEFAULT_HTTP_HOST
    port = args.port if args.port is not None else DEFAULT_HTTP_PORT
    return serve_http(host, port, api_key=api_key, include_write=not args.read_only)


def serve(stdin: TextIO, stdout: TextIO, *, include_write: bool = True) -> int:
    allowed_tools = {tool["name"] for tool in list_tools(include_write=include_write)}
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            response = _handle_request(request, include_write=include_write, allowed_tools=allowed_tools)
        except Exception as exc:  # keep the server alive after malformed client messages
            response = _error_response(None, -32700, f"Invalid JSON-RPC message: {exc}")
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            stdout.flush()
    return 0


def serve_http(host: str, port: int, *, api_key: str, include_write: bool = True) -> int:
    """Serve JSON-RPC MCP requests over HTTP until interrupted."""
    server = create_http_server(host, port, api_key=api_key, include_write=include_write)
    bound_host, bound_port = server.server_address[:2]
    print(f"REQL MCP HTTP server listening on http://{bound_host}:{bound_port}/mcp", file=sys.stderr)
    print("HTTP authorization: Authorization: Bearer <api-key>", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
    return 0


def create_http_server(host: str, port: int, *, api_key: str, include_write: bool = True) -> ThreadingHTTPServer:
    """Create an HTTP MCP server instance.

    Tests can bind to port 0 and drive the returned server in a background
    thread without shelling out.
    """
    if not isinstance(api_key, str) or not api_key:
        raise ValueError("api_key must be a non-empty string")
    allowed_tools = {tool["name"] for tool in list_tools(include_write=include_write)}

    class MCPHTTPHandler(BaseHTTPRequestHandler):
        server_version = "REQLMCPHTTP/0.1"

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler naming
            if self.path != "/health":
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "server": SERVER_NAME,
                    "transport": "http",
                    "read_only": not include_write,
                },
            )

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler naming
            if self.path not in {"/", "/mcp"}:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            if not self._authorized():
                self._discard_body()
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"}, {"WWW-Authenticate": "Bearer"})
                return
            length_header = self.headers.get("Content-Length", "0")
            try:
                length = int(length_header)
            except ValueError:
                self._send_json(HTTPStatus.BAD_REQUEST, _error_response(None, -32600, "Invalid Content-Length"))
                return
            try:
                body = self.rfile.read(length).decode("utf-8")
                request = json.loads(body)
                response = _handle_request(request, include_write=include_write, allowed_tools=allowed_tools)
            except Exception as exc:
                response = _error_response(None, -32700, f"Invalid JSON-RPC message: {exc}")
            if response is None:
                self._send_json(HTTPStatus.ACCEPTED, {})
                return
            self._send_json(HTTPStatus.OK, response)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _authorized(self) -> bool:
            expected = f"Bearer {api_key}"
            provided = self.headers.get("Authorization", "")
            return hmac.compare_digest(provided, expected)

        def _discard_body(self) -> None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return
            if length > 0:
                self.rfile.read(length)

        def _send_json(self, status: HTTPStatus, payload: dict[str, Any], headers: dict[str, str] | None = None) -> None:
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            if headers:
                for name, value in headers.items():
                    self.send_header(name, value)
            self.end_headers()
            self.wfile.write(encoded)

    return ThreadingHTTPServer((host, port), MCPHTTPHandler)


def _handle_request(
    request: dict[str, Any],
    *,
    include_write: bool,
    allowed_tools: set[str],
) -> dict[str, Any] | None:
    if not isinstance(request, dict):
        return _error_response(None, -32600, "Request must be a JSON object")
    request_id = request.get("id")
    method = request.get("method")
    params = request.get("params") or {}

    if request_id is None and method and method.startswith("notifications/"):
        return None

    if method == "initialize":
        return _result_response(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "serverInfo": {"name": SERVER_NAME, "version": "0.1.0"},
                "capabilities": {"tools": {"listChanged": False}},
            },
        )
    if method == "tools/list":
        return _result_response(request_id, {"tools": list_tools(include_write=include_write)})
    if method == "tools/call":
        if not isinstance(params, dict):
            return _error_response(request_id, -32602, "tools/call params must be an object")
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name not in allowed_tools:
            return _error_response(request_id, -32602, f"Tool is not available: {name}")
        try:
            payload = call_tool(str(name), arguments)
        except MCPToolError as exc:
            return _error_response(request_id, -32602, str(exc))
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        return _result_response(request_id, {"content": [{"type": "text", "text": text}], "isError": False})
    if method == "ping":
        return _result_response(request_id, {})
    return _error_response(request_id, -32601, f"Method not found: {method}")


def _result_response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error_response(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _resolve_api_key(explicit: str | None, env_name: str | None) -> str | None:
    if explicit:
        return explicit
    if not env_name:
        return None
    value = os.environ.get(env_name)
    return value or None


if __name__ == "__main__":
    raise SystemExit(main())
