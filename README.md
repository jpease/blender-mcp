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

> **Note:** By default the server only registers its core scene/mesh/object toolset (~40 tools) to
> keep `tools/list` small. See [Tool Bundles](#tool-bundles) below to add domains like cloth, liquid,
> or rigging. Running more than one server process for the *same* bundle selection (e.g. two default
> `blender` entries in both Cursor and Claude Desktop) is redundant — each opens its own connection to
> the same addon — so stick to one process per bundle set you're using this session.

---

## Table of Contents

- [Quickstart](#quickstart)
- [Features](#features)
- [Installation](#installation)
  - [Prerequisites](#prerequisites)
  - [MCP Client Setup](#mcp-client-setup)
  - [Tool Bundles](#tool-bundles)
  - [Install the Blender Addon](#install-the-blender-addon)
- [Usage](#usage)
- [Capabilities](#capabilities)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

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

<details>
<summary><b>Codex CLI</b> — <code>~/.codex/config.toml</code></summary>

```toml
[mcp_servers.blender]
command = "blender-mcp"
```
</details>

---

## Tool Bundles

blender-mcp registers close to 300 tools in total. Sending all of them to a client on every
connection can be large enough to eat into the context available for the actual task, so by
default a server process only registers its **core** bundle — scene inspection, mesh/object
editing, viewport, and animation (~40 tools). Everything else is opt-in per domain, selected with
the `BLENDER_MCP_TOOLSETS` environment variable (a comma-separated list of bundle names, or `all`
for the previous everything-registered behavior):

| Bundle | Adds |
|---|---|
| *(default, always on)* | scene inspection, mesh/object editing, viewport, animation |
| `camera` | camera placement, framing, shots |
| `cloth` | cloth simulation |
| `liquid` | fluid/liquid simulation |
| `rigid-body` | rigid body physics, scene physics |
| `geometry-nodes` | geometry nodes, ND toolkit |
| `character-rigging` | armatures, rigging |
| `retopology` | retopology workflows |
| `texture-lighting` | materials/textures, lighting |
| `rendering` | render + inspect render output |
| `assets` | Poly Haven, Sketchfab |

Add one MCP server entry per bundle set you want available this session — set the env var on that
entry, not globally, so each client config controls exactly which tools it sees:

**Claude Desktop / Claude Code** (`claude_desktop_config.json`, or `claude mcp add`'s `--env` flag):

```json
{
    "mcpServers": {
        "blender": {
            "command": "blender-mcp"
        },
        "blender-cloth": {
            "command": "blender-mcp",
            "env": { "BLENDER_MCP_TOOLSETS": "cloth,liquid" }
        }
    }
}
```

**Codex CLI** (`~/.codex/config.toml`):

```toml
[mcp_servers.blender]
command = "blender-mcp"

[mcp_servers.blender-cloth]
command = "blender-mcp"
env = { BLENDER_MCP_TOOLSETS = "cloth,liquid" }
```

Both clients then show one hammer-icon server per entry; enable only the ones relevant to what
you're working on, and disable the rest for that session.

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

**Tool bundles**

Configure which tool domains a server process registers with `BLENDER_MCP_TOOLSETS` — see [Tool Bundles](#tool-bundles).

---

## Troubleshooting

**Connection issues**

Ensure the Blender addon server is running. Don't run `blender-mcp` manually outside your MCP client. If the first command fails, try again—it often works after that. Restart both Claude and Blender if problems persist.

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

---

## License

MIT
