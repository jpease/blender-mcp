"""
Report the directories this Blender process can write to.

When the MCP server and Blender do not share a filesystem, the server cannot
tell which paths a render or export may target, so the addon reports its own
writable roots in the get_addon_info handshake.

Deployment-specific roots come from environment variables, so this code need
not know whether it runs in a container or on a desktop. Free of `bpy`: callers
pass the Blender-derived candidates.
"""

import os

from collections.abc import Iterable, Mapping

# os.pathsep-separated directories to offer first, such as a container's
# mounted output volume.
OUTPUT_ROOTS_ENV_VAR = "BLENDERMCP_OUTPUT_ROOTS"
# Same format: the directories .blend file commands are confined to. Separate
# because a confinement root, such as a read-only library mount, need not be
# writable. Falls back to the output roots.
FILE_ROOTS_ENV_VAR = "BLENDERMCP_FILE_ROOTS"


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


def configured_file_roots(environ: Mapping[str, str] | None = None) -> list[str]:
    """
    Read the roots `.blend` file commands are enforced against.

    Unlike the advisory roots above, this never uses the default candidates
    (`~`, temp dirs): the boundary would become the whole home directory. A
    variable holding only blanks counts as unset. An empty result means no
    boundary is enforced; `file_paths` explains that default.

    Args:
        environ: Mapping to read from; defaults to the real environment.

    Returns:
        list[str]: `BLENDERMCP_FILE_ROOTS` entries, else `BLENDERMCP_OUTPUT_ROOTS`
        entries, as given (canonicalize before comparing).

    """
    source = os.environ if environ is None else environ
    file_roots = configured_roots({OUTPUT_ROOTS_ENV_VAR: source.get(FILE_ROOTS_ENV_VAR, "")})
    return file_roots or configured_roots(source)
