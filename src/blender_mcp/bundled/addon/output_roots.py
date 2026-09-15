"""
Report the directories this Blender process can actually write to.

Once the MCP server and Blender stop sharing a filesystem, the server has no
way to know which paths a render or export can target - its own cwd, temp dir
and home directory mean nothing on the other side. So the addon reports its
own writable roots in the get_addon_info handshake and the server passes them
on to the agent.

The deployment-specific roots come from an environment variable rather than
being hardcoded, so nothing here needs to know whether it is running in a
container, a VM, or on the user's desktop.

Deliberately free of `bpy`: the Blender-derived candidates are passed in.
"""

import os

from collections.abc import Iterable, Mapping

# Colon-separated (os.pathsep) list of directories a deployment wants offered
# first - e.g. a container's mounted output volume.
OUTPUT_ROOTS_ENV_VAR = "BLENDERMCP_OUTPUT_ROOTS"


def configured_roots(environ: Mapping[str, str] | None = None) -> list[str]:
    """
    Read the deployment-supplied output roots from the environment.

    Args:
        environ: Mapping to read from; defaults to the real environment.

    Returns:
        list[str]: Configured paths in the order given, blanks removed.

    """
    raw = (os.environ if environ is None else environ).get(OUTPUT_ROOTS_ENV_VAR, "")
    return [entry.strip() for entry in raw.split(os.pathsep) if entry.strip()]


def writable_roots(candidates: Iterable[str | None]) -> list[str]:
    """
    Reduce candidate paths to the existing directories that are writable.

    Args:
        candidates: Paths to consider, most preferred first; blanks are ignored.

    Returns:
        list[str]: Absolute paths, deduplicated, original order preserved.

    """
    roots = []
    seen = set()
    for candidate in candidates:
        if not candidate:
            continue
        path = os.path.abspath(os.path.expanduser(str(candidate)))
        if path in seen:
            continue
        if not os.path.isdir(path) or not os.access(path, os.W_OK):
            continue
        seen.add(path)
        roots.append(path)
    return roots
