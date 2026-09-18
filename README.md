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

> **Note:** By default the server only registers its core scene/mesh/object toolset to
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
default a server process only registers its **core** bundle — scene inspection, object editing,
viewport, animation, and file lifecycle/linking (~31 tools). Everything else is opt-in, selected
with the `BLENDER_MCP_TOOLSETS` environment variable (a comma-separated list of names, or `all`
for the previous everything-registered behavior).

A name is either a **mode** — one word for the surface an artist is working in — or a **bundle**,
for fine-grained control. Modes are curated presets over bundles and compose with them, e.g.
`shot,retopology`:

| Mode | Selects |
|---|---|
| `shot` | assembling, animating, lighting and rendering a scene: `camera`, `lighting`, `rendering` |
| `asset` | authoring or revising canon: `core-authoring`, `scene-authoring`, `texture`, `retopology`, `geometry-nodes` |

| Bundle | Adds |
|---|---|
| *(default, always on)* | scene inspection, object editing, viewport, animation, file lifecycle (open/save/reset a shot, link/override/list/reload/relocate/unlink canon libraries) |
| `core-authoring` | mesh and model creation/editing |
| `camera` | camera placement, framing, shots (rig construction is separate, see `camera-rigs`) |
| `camera-rigs` | orbit/dolly/crane/path rig construction |
| `scene-authoring` | declarative geometry creation, scene reset, object removal |
| `cloth` | cloth simulation |
| `liquid` | fluid/liquid simulation |
| `rigid-body` | rigid body physics, scene physics |
| `geometry-nodes` | geometry nodes, ND toolkit |
| `character-rigging` | armatures, rigging |
| `retopology` | retopology workflows |
| `lighting` | lighting inspection, environment, render-quality settings (construction is separate, see `lighting-construction`) |
| `lighting-construction` | creating/aiming/linking lights, studio-lighting presets (also carries all of `lighting`'s render-quality tools — `configure_lighting_quality`, `configure_color_management`, `render_lighting_preview` — since the studio-lighting preset calls the last one directly) |
| `texture` | materials/textures |
| `texture-lighting` | **deprecated**, kept for existing configs: `texture` + `lighting` + `lighting-construction` |
| `rendering` | render + inspect render output |
| `assets` | Poly Haven, Sketchfab |

Add one MCP server entry per mode or bundle set you want available this session — set the env var
on that entry, not globally, so each client config controls exactly which tools it sees:

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

**Transport**

The server speaks **stdio** by default, which is what every MCP client config above launches. Set `BLENDERMCP_TRANSPORT=http` to serve streamable HTTP instead. `BLENDERMCP_HTTP_HOST` and `BLENDERMCP_HTTP_PORT` default to `127.0.0.1` and `8000`. A bad transport name, host or port is reported by its variable name and the server exits rather than falling back.

**The HTTP endpoint has no authentication of any kind**, so its bind address is the whole of its access control, and the only supported deployment binds loopback. Reach it from another machine through an SSH tunnel, so that somebody else's authentication sits in front of it. The container rig below is the loopback case too: it binds `0.0.0.0` inside the container because that is where Docker's port forward arrives, and compose publishes it on the host's `127.0.0.1` alone.

A non-loopback bind is therefore refused unless you also set `BLENDERMCP_HTTP_ALLOW_REMOTE=1`, which logs a warning saying what it costs. An empty `BLENDERMCP_HTTP_HOST` is refused outright rather than treated as the default, because an empty value binds every interface — a compose key with nothing after the colon used to be enough to do that silently.

One thing not to mistake for access control: a client that reaches the port from off-box and asks for it by its real address gets `421 Misdirected Request`. That is FastMCP's DNS-rebinding protection, it is aimed at browsers, and `Host` is a header the client chooses — so a non-browser client sending `Host: 127.0.0.1` is served normally from anywhere on the network. Loopback binding, not that check, is what keeps the server unreachable.

**File paths**

Commands that open, save or link a `.blend` take a path from an unauthenticated socket, so the addon's path policy (`src/blender_mcp/bundled/addon/file_paths.py`) is what those commands apply before Blender sees the path. Those commands are `open_shot`, `save_shot`, `link_canon_library` and `relocate_library`, plus the Poly Haven model import, which validates its downloaded file's header and keeps it inside its own download directory. A path must be a string naming a `.blend` (case-insensitive; a trailing dot or space is refused), it is resolved through `~`, `abspath` and `realpath` so `..` and symlinks cannot hide where it leads, a file being read must carry a real `.blend` header (uncompressed, zstd or gzip), and a save target's directory must be writable, and must already exist unless the save explicitly asks for it to be created.

- **`BLENDERMCP_FILE_ROOTS`** (`os.pathsep`-separated) confines those commands to the listed directories. When it is unset or blank, **`BLENDERMCP_OUTPUT_ROOTS`** is used instead. They are separate because a read-only canon library mount is a place to read `.blend` files from, not a place renders should be offered.
- **Unset means permissive.** With neither variable set, no containment is enforced: the local case is a GUI Blender on your own machine behind a loopback socket, where deny-by-default would break ordinary use. A container or pooled deployment should set the variable (the compose file sets `BLENDERMCP_OUTPUT_ROOTS: /output`, so the rig is enforced). `get_addon_status` reports `file_roots` and `file_roots_enforced`, so a client can tell which mode it is in.
- The enforced roots come only from those variables. The `writable_output_roots` the handshake also reports (which include `~` and the temp directory) are an advisory ranking of where renders can go, and never widen the boundary.
- **Overwriting** an existing `.blend` requires an explicit `confirm_overwrite=True`; the default refuses.
- **Creating directories** is opt-in: `save_shot(create_directories=True)` makes the target's missing parents, so a
  `canon/shots/` layout needs no pre-created directories. It is the one command here that creates filesystem
  structure, it runs only after the roots and every other refusal have passed, and in permissive mode (no roots set)
  it can therefore create a directory anywhere Blender itself can write. The default refuses a missing directory.
- **Embedded scripts never run.** A `.blend` can carry Python that Blender executes on load. `use_scripts` is never a tool parameter, a file command's load passes `use_scripts=False` explicitly, and Blender's *Auto Run Python Scripts* preference (`preferences.filepaths.use_scripts_auto_execute`) is part of the same risk and must be checked before a load.
- Blender's own error text contains absolute paths; file commands replace them with `<path>` before an error reaches a client.

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

### Live Blender rig

Some behaviour cannot be tested without a running Blender: the addon **refuses to start under `blender --background`**, and `bpy.app.timers` never fire there, so the drain loop that answers socket commands does not run. Anything that depends on a command actually being dequeued needs a real event loop. There are two rigs, and they do **not** reach the same layers.

**Local (macOS) — `scripts/blender_rig.py`.** Launches a GUI Blender, stages *this checkout's* addon into a directory you supply, waits until that Blender answers a `ping`, and runs a scenario against it:

```bash
python scripts/blender_rig.py \
    --work-dir /tmp/rig --scenario my_scenario.py \
    --blend fixture=/path/to/fixture.blend
```

A scenario defines `run(rig)` and drives the socket with `rig.send("ping")`; it fails by raising. The scenario runs outside Blender, so when it needs Blender to do something of its own accord, pass that work as `--blender-script`. It gets `--scenario-timeout` seconds (default 300) before the rig abandons it and tears Blender down, and each command waits `--command-timeout` seconds (default 180, matching the real client) for its reply.

What the isolation actually is, since the details matter:

- The rig sets **both** `BLENDER_USER_RESOURCES` and `BLENDER_USER_SCRIPTS` to `--work-dir`, so config, datafiles and extensions land there too, and points `TMPDIR` at `<work-dir>/tmp` so Blender's session temp directory — autosaves, `quit.blend`, render previews — lands there as well. `BLENDER_USER_SCRIPTS` alone redirects only scripts — your own `recent-files.txt` is still rewritten the moment a scenario opens or saves a `.blend`. What keeps `userpref.blend` out of the run is `--factory-startup`, not an environment variable.
- The launched Blender **leads with** `--work-dir` in the roots it advertises, through `BLENDERMCP_OUTPUT_ROOTS`. It does *not* advertise only that: the addon appends the open blend's directory, `bpy.app.tempdir`, `tempfile.gettempdir()` and `~` after it, so `$HOME` is still in the list. That list is an advisory preference ranking, not an enforced root set; `.blend` file commands are confined by `BLENDERMCP_FILE_ROOTS` instead (see [Configuration](#configuration)). `--blend` fixtures are **copied** into the work dir — a scenario that saves cannot write through to your original.
- Every inherited `BLENDER*` variable is dropped (`BLENDER_SYSTEM_SCRIPTS` redirects Blender's *system* scripts tree), along with `PYTHONPATH`, `PYTHONHOME` and `PYTHONSTARTUP`, so neither the checkout's `src/` nor a stray Python environment can shadow the staged addon.
- Blender's output is teed to `<work-dir>/blender.log` by a reader thread that keeps draining even if it cannot decode a byte or cannot open the log, because an undrained pipe deadlocks Blender in `write()` on its main thread.
- `--work-dir` must be empty, absent, or already carry the rig's own `.blender-rig-owned` marker; anything else is refused, which is what stops a real Blender resources root (`config/`, `datafiles/`, `extensions/`, `scripts/`, `userpref.blend`) or an auto-executing `startup/`/`modules/` tree from being adopted. Inside a marked work dir the rig replaces only `addons/blender_mcp` and its own `blends/` copies, so add-ons you installed alongside it survive.
- `--port` defaults to a free ephemeral port rather than the addon's 9876, and the rig **aborts** if something is already listening on the port it picked, instead of driving your running Blender and reporting its answers as the rig's.

Every file the rig's own process writes, and every Blender-side path the rig controls — user resources, scripts, the session temp dir, staged fixtures and the log — lives under `--work-dir`. What the rig cannot promise is a *scenario* that asks Blender to write somewhere else: the advertised roots are advisory, and a scenario may name any path.

This mode speaks the **addon's socket protocol only and stands up no MCP server**, so a scenario can call addon commands but not MCP tools — `get_addon_status`, for instance, is server-side and unreachable here. The MCP wrapper layer is covered by ordinary `pytest`.

**Container — `docker/blender/`.** Xvfb + Blender + the real MCP server, the only mode that runs both layers:

```bash
docker compose -f docker/blender/docker-compose.yml up --wait
```

Four things to know about it:

- **It runs emulated on Apple Silicon.** Blender ships x86_64 Linux builds only, so compose forces `platform: linux/amd64` — correct, but slow. There is no Xvfb on macOS, which is why the local rig is a GUI Blender instead.
- **Blender's socket has no authentication and must stay on loopback.** Compose publishes `127.0.0.1:8000` (the MCP server) and nothing else; Blender's own port never leaves the container. Publishing either on `0.0.0.0` would hand scene control plus file read/write to anyone who can reach the host.
- `BLENDER_MCP_TOOLSETS` is pinned to `shot` so the rig exercises the shot pipeline's surface rather than the smaller `core` default. See [Tool Bundles](#tool-bundles).
- **The entrypoint waits for Blender before starting the MCP server**, by round-tripping a `ping` on Blender's socket with a deadline (`BLENDER_READY_TIMEOUT_SECONDS`, default 600). The server makes a single connection attempt at startup and does not retry, and under emulation Blender takes far longer to open its socket than the server takes to give up. Either process then exiting takes the container down, so a dead Blender is never hidden behind an MCP port that still answers.

---

## License

MIT
