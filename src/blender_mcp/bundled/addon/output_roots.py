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


def split_roots(raw: str | None) -> list[str]:
    """
    Split one `os.pathsep`-separated root list into its entries.

    Pure, and the one place the variable format is spelled out, so both readers
    below agree on it. A trailing separator or a stray space would otherwise
    produce an empty root, which every containment test would accept.

    Args:
        raw: The variable's value, or None when it is unset.

    Returns:
        list[str]: The entries in the order given, trimmed, blanks removed.

    """
    return [entry.strip() for entry in (raw or "").split(os.pathsep) if entry.strip()]


def configured_roots(environ: Mapping[str, str] | None = None) -> list[str]:
    """
    Read the deployment-supplied output roots from the environment.

    Args:
        environ: Mapping to read from; defaults to the real environment.

    Returns:
        list[str]: Configured paths in the order given, blanks removed.

    """
    return split_roots((os.environ if environ is None else environ).get(OUTPUT_ROOTS_ENV_VAR))


def normalized_candidates(candidates: Iterable[str | None]) -> list[str]:
    """
    Absolutize and deduplicate candidate roots without touching the filesystem.

    No filesystem call, so the ordering and dedup rules are testable without
    creating directories; `~` and a relative candidate are still resolved against
    process state (`HOME`, the working directory). Order is the preference
    ranking, so a repeat must not demote its first appearance.

    Args:
        candidates: Paths to consider, most preferred first; blanks are ignored.

    Returns:
        list[str]: Absolute paths, deduplicated, original order preserved.

    """
    normalized: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate:
            continue
        path = os.path.abspath(os.path.expanduser(str(candidate)))
        if path in seen:
            continue
        seen.add(path)
        normalized.append(path)
    return normalized


def writable_roots(candidates: Iterable[str | None]) -> list[str]:
    """
    Reduce candidate paths to the existing directories that are writable.

    The probing shell over `normalized_candidates`: two stats per distinct
    candidate, which is why the handshake caches the result (`server_core`).

    Args:
        candidates: Paths to consider, most preferred first; blanks are ignored.

    Returns:
        list[str]: Absolute paths, deduplicated, original order preserved.

    """
    return [path for path in normalized_candidates(candidates) if os.path.isdir(path) and os.access(path, os.W_OK)]


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
    return split_roots(source.get(FILE_ROOTS_ENV_VAR)) or split_roots(source.get(OUTPUT_ROOTS_ENV_VAR))
