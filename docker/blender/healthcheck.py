"""
Container healthcheck: Blender's addon socket and the MCP server must both answer.

Each check makes a real request, because an open port does not mean a server is
serving on it. Exits 0 only when both answer, so `docker compose up --wait`
returns once an agent can use the container.
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
