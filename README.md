<div align="center">

# Blender MCP

**AI-driven 3D modeling and scene automation**

A Model Context Protocol (MCP) server that connects Claude to Blender for prompt-assisted scene creation, object manipulation, and procedural workflows.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

</div>

---

## Quickstart

Three steps: install `blender-mcp` with pipx, point your MCP client at the server, install the Blender addon.

**1. Install blender-mcp with pipx**

```bash
# macOS
brew install pipx
pipx ensurepath

# Linux
python3 -m pip install --user pipx
python3 -m pipx ensurepath

# Windows
py -m pip install --user pipx
py -m pipx ensurepath
```

Then, in a new shell:

```bash
pipx install blender-mcp
```

> **Warning:** Do not proceed before installing pipx and running `pipx install blender-mcp`.

**2. Add the MCP server to your client**

<details open>
<summary><b>Claude Desktop</b> — Settings → Developer → Edit Config</summary>

```json
{
    "mcpServers": {
        "blender": {
            "command": "blender-mcp"
        }
    }
}
```
</details>

<details>
<summary><b>Claude Code</b></summary>

```bash
claude mcp add blender blender-mcp
```
</details>

<details>
<summary><b>Cursor / VS Code / OpenCode / Antigravity</b></summary>

See [MCP Client Setup](#mcp-client-setup) below for per-client instructions and one-click install buttons.
</details>

**3. Install the Blender addon**

```bash
blender-mcp install-addon
```

Then in Blender: **Edit → Preferences → Add-ons** → enable **Interface: Blender MCP**.

**4. Connect**

In Blender's 3D viewport, press `N` → open the **BlenderMCP** tab → click **Start MCP Server**. That's it — ask Claude to build something.

> **Note:** Only run **one** instance of the MCP server (either Cursor or Claude Desktop), not both.

---

## Table of Contents

- [Quickstart](#quickstart)
- [Features](#features)
- [Installation](#installation)
  - [Prerequisites](#prerequisites)
  - [MCP Client Setup](#mcp-client-setup)
  - [Install the Blender Addon](#install-the-blender-addon)
- [Usage](#usage)
- [Capabilities](#capabilities)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
  - [Running Blender in Docker](#running-blender-in-docker)

---

## Features

| | |
|---|---|
| **Two-way communication** | Connect Claude AI to Blender through a socket-based server |
| **Object manipulation** | Create, modify, and delete 3D objects in Blender |
| **Material control** | Apply and modify materials and colors |
| **Scene inspection** | Get detailed information about the current Blender scene |
| **Code execution** | Run arbitrary Python code in Blender from Claude |
| **Asset & model generation** | Poly Haven assets and Sketchfab models |

---

## Installation

### Prerequisites

- **Blender** 5.1 or newer
- **Python** 3.13 or newer
- **pipx**

<details>
<summary><b>Installing pipx, per platform</b></summary>

**macOS**
```bash
brew install pipx
pipx ensurepath
```

**Windows**
```powershell
py -m pip install --user pipx
py -m pipx ensurepath
```

**Linux**
```bash
python3 -m pip install --user pipx
python3 -m pipx ensurepath
```

`pipx ensurepath` adds pipx's install location (and the binaries it installs) to your PATH — open a new shell after running it.

Otherwise, installation instructions are on their website: [Install pipx](https://pipx.pypa.io/stable/installation/).

Then install the server itself:

```bash
pipx install blender-mcp
```
</details>

> **Warning:** Do not proceed before installing pipx and running `pipx install blender-mcp`.

### Troubleshooting Installation

**spawn blender-mcp ENOENT error**

GUI clients (Claude Desktop, Cursor, VS Code from the Dock/Start menu) don't inherit your terminal's PATH. Use the full path to blender-mcp:

```bash
which blender-mcp  # macOS/Linux
where blender-mcp  # Windows
```

Then update your MCP config with the full path (e.g., `/Users/<you>/.local/bin/blender-mcp`). On Windows, you can alternatively wrap it: `"command": "cmd", "args": ["/c", "blender-mcp"]`.

After updating, fully quit and relaunch your client.

**Python version conflicts**

On machines with conda, pyenv, or asdf, pin the Python version:

```bash
pipx install blender-mcp --python python3.13
```

To reinstall clean:

```bash
pipx reinstall blender-mcp
```

---

## MCP Client Setup

### Claude for Desktop

[Watch the setup instruction video](https://www.youtube.com/watch?v=neoK_WMq92g) (assuming you have already installed blender-mcp via pipx)

Go to **Claude → Settings → Developer → Edit Config → `claude_desktop_config.json`** and include the following:

```json
{
    "mcpServers": {
        "blender": {
            "command": "blender-mcp"
        }
    }
}
```

<details>
<summary><b>Claude Code</b></summary>

Use the Claude Code CLI to add the blender MCP server:

```bash
claude mcp add blender blender-mcp
```
</details>

**Cursor, VS Code, OpenCode, Antigravity**

Refer to your editor's MCP setup instructions and use `"command": "blender-mcp"`. For Windows, wrap as `"command": "cmd", "args": ["/c", "blender-mcp"]`.

---

### Install the Blender Addon

```bash
blender-mcp install-addon
```

Then in Blender: **Edit → Preferences → Add-ons** → enable **Interface: Blender MCP** (search "Blender MCP").

If the command can't find your Blender install, manually install from `src/blender_mcp/bundled/addon/` via **Edit → Preferences → Add-ons → Install…**

### Upgrading

```bash
pipx upgrade blender-mcp
blender-mcp install-addon
```

In Blender: **Preferences → Add-ons** → disable and re-enable **Interface: Blender MCP** (or restart Blender).

Keep the addon in step with the server. The server reports a mismatch on connect rather than failing: an older addon still works, but loses features that depend on newer addon support — screenshots and render inspection fall back to writing through a shared filesystem, which only works when the server and Blender are on the same machine.

---

## Usage

1. In Blender, press `N` to open the sidebar and find the **BlenderMCP** tab
2. Click **Start MCP Server**
3. In your MCP client (Claude, Cursor, etc.), you'll see the hammer icon with Blender tools available

### Capabilities

- Get scene and object information
- Create, delete and modify shapes
- Create primitives and edit meshes directly (extrude, inset, bevel, bridge, boolean, subdivide, remesh, solidify)
- Higher-level modeling operations (mirror, array, radial array, symmetrize, blockout, refine, detail, match transform to a reference object)
- Apply or create materials for objects
- Execute any Python code in Blender
- Download the right models, assets and HDRIs through [Poly Haven](https://polyhaven.com/)
- Search and download models from [Sketchfab](https://sketchfab.com/)


## Configuration

**Credentials**

Store Sketchfab API keys in **Edit → Preferences → Add-ons → Blender MCP** or via `BLENDERMCP_SKETCHFAB_API_KEY` environment variable.

**Connection**

Configure host and port with `BLENDER_HOST` and `BLENDER_PORT` environment variables (defaults: `localhost`, `9876`).

**Client transport**

The server speaks stdio by default, which is what every client config above launches. To host it beside a Blender on another machine instead, serve streamable HTTP and point the client at its URL:

| Variable | Default | Purpose |
| --- | --- | --- |
| `BLENDERMCP_TRANSPORT` | `stdio` | `stdio`, or `http` for streamable HTTP. |
| `BLENDERMCP_HTTP_HOST` | `127.0.0.1` | Address the HTTP server binds. |
| `BLENDERMCP_HTTP_PORT` | `8000` | Port the HTTP server listens on; the endpoint is `/mcp`. |

HTTP mode has no authentication. It rejects requests whose `Host` header isn't loopback (DNS-rebinding protection), but anything that can reach the port can drive Blender, so keep it on a trusted host.

The server retries its first connection, so starting Blender and your MCP client in either order works:

| Variable | Default | Purpose |
| --- | --- | --- |
| `BLENDER_CONNECT_ATTEMPTS` | `3` | Tries when nothing is listening at all. Kept small so a Blender that simply isn't running is reported in about a second. Set to `1` for the old fail-immediately behaviour. |
| `BLENDER_STARTUP_ATTEMPTS` | `40` | Tries when the socket is accepted but Blender has not answered yet — the case where Blender is still starting behind a port forwarder. |
| `BLENDER_CONNECT_RETRY_DELAY` | `0.5` | Seconds between tries. |

**Output locations**

`BLENDERMCP_OUTPUT_ROOTS` (colon-separated) names directories Blender should offer first when asked where it can write renders and exports. The addon reports these through its handshake, along with its own temp and home directories, so the agent knows which paths are real. Leave it unset for a local Blender; it exists for setups where Blender's filesystem is not yours, such as the Docker rig below.

---

## Troubleshooting

**Connection issues**

Ensure the Blender addon server is running. Don't run `blender-mcp` manually outside your MCP client. The first connection is retried automatically while Blender finishes starting, so a failure here usually means the addon server was never started (or is on another port). Restart both Claude and Blender if problems persist.

**Timeout errors**

Break requests into smaller steps or simplify the operation.

---

## Development

*Building the project from source, rather than installing the published package with pipx.*

### Setup

```bash
pipx install poetry
poetry install --with dev
```

### Running from source

```bash
poetry run blender-mcp
```

### Lint, format, type-check, test

```bash
poetry run ruff check .
poetry run ruff format --check .
poetry run pytest
basedpyright
```

### Building a distributable package

```bash
pip install build
python -m build
```

This uses the `setuptools` backend declared in `pyproject.toml`'s `[build-system]` and produces `dist/blender_mcp-<version>-py3-none-any.whl` and `dist/blender_mcp-<version>.tar.gz`, the same as `.github/workflows/release.yml`.

### Installing the addon from a local checkout

See [Installing the Blender Addon](#installing-the-blender-addon) above — point **Preferences → Add-ons → Install…** at `src/blender_mcp/bundled/addon/` in your checkout instead of a downloaded release.

### Running Blender in Docker

`docker/blender/` builds a stand-in for a remote Blender host: headless Blender (AlmaLinux + Xvfb) with the `blender-mcp` server running beside it over streamable HTTP. Clients reach the MCP server; Blender's own socket never leaves the container.

```bash
cd docker/blender
docker compose up -d --wait      # --wait blocks until Blender and the MCP server both answer
claude mcp add --transport http blender-docker http://127.0.0.1:8000/mcp
```

```bash
docker compose logs -f           # Blender, addon, and MCP server output
docker compose down
```

Notes worth knowing before relying on it:

- **Only the MCP endpoint is published, on `127.0.0.1:8000`.** Nothing is authenticated, so anything that can reach the port can drive Blender, including reading and writing files. Don't widen the binding on a network you don't control. A local stdio setup needs a local Blender; this container doesn't expose Blender's socket.
- **Use `--wait`.** Docker accepts connections on a published port before anything is listening behind it, so a plain `up -d` returns while the container is still starting. The healthcheck round-trips both Blender's socket and an MCP `initialize`, and `--wait` gates on it.
- **Paths are the container's, not yours.** Renders and exports must target a path that exists inside the container. `./output` is mounted at `/output` and advertised through `BLENDERMCP_OUTPUT_ROOTS`, so anything written there shows up on the host. Screenshots and render previews come back as image data over MCP.
- **The repo is mounted read-only** at `/repo`. The addon is copied out of it and the server runs from it on every start, so code edits need only a `docker compose restart`. Dependency changes in `pyproject.toml` need a `docker compose build`. Tools that try to write into the project tree will fail.
- **Choosing a Blender version:** `docker compose build --build-arg BLENDER_MAJOR_MINOR=5.1 --build-arg BLENDER_VERSION=5.1.1`. Both must be set together; the image records the version so the addon is installed where that Blender looks for it.

---

## License

MIT
