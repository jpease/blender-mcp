"""
Container healthcheck: both halves of the "remote" host must genuinely answer.

A TCP connect proves nothing - Docker accepts on a published port before
anything is listening inside - so each check does a real round-trip:

1. Blender's addon socket answers a ping (the MCP server's upstream).
2. The MCP server answers an `initialize` over streamable HTTP (what clients reach).

`docker compose up --wait` then blocks until an agent could actually use it.
"""

import json
import socket
import urllib.request

BLENDER_ADDRESS = ("127.0.0.1", 9876)
MCP_URL = "http://127.0.0.1:8000/mcp"
HTTP_OK = 200


def blender_answers() -> bool:
    """
    Ping the addon's socket.

    Returns:
        bool: Whether Blender sent any reply.

    """
    with socket.create_connection(BLENDER_ADDRESS, timeout=3) as sock:
        sock.sendall(b'{"id": "healthcheck", "type": "ping", "params": {}}\n')
        return bool(sock.recv(256))


def mcp_answers() -> bool:
    """
    Open an MCP session over streamable HTTP.

    Returns:
        bool: Whether the server accepted the initialize request.

    """
    request = urllib.request.Request(
        MCP_URL,
        data=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "healthcheck", "version": "0"},
                },
            }
        ).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        return response.status == HTTP_OK


raise SystemExit(0 if blender_answers() and mcp_answers() else 1)
