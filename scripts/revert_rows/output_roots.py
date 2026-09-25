"""
Rows guarding the ported `output_roots` module.

Label prefixes: `output_roots:`, `output_roots control:`, `output_roots wiring:`.
"""

from .common import ADDON_OUTPUT_ROOTS, ADDON_SERVER_CORE, ROOTST, Revert

ROWS: list[Revert] = [
    # --- the ported output_roots module ---
    Revert(
        "output_roots: the environment variable is no longer split on os.pathsep",
        ADDON_OUTPUT_ROOTS,
        '(raw or "").split(os.pathsep)',
        '[raw or ""]',
        (f"{ROOTST}::test_configured_roots_splits_the_environment_variable",),
    ),
    # The whole line, because `split_roots` reads the same variable for the file roots:
    # a bare `get(OUTPUT_ROOTS_ENV_VAR)` anchor matches twice.
    Revert(
        "output_roots: an unset variable falls back to a hardcoded root",
        ADDON_OUTPUT_ROOTS,
        "    return split_roots((os.environ if environ is None else environ).get(OUTPUT_ROOTS_ENV_VAR))",
        '    return split_roots((os.environ if environ is None else environ).get(OUTPUT_ROOTS_ENV_VAR, "/default"))',
        (f"{ROOTST}::test_configured_roots_is_empty_when_unset",),
    ),
    Revert(
        "output_roots: blank entries are no longer stripped out",
        ADDON_OUTPUT_ROOTS,
        '    return [entry.strip() for entry in (raw or "").split(os.pathsep) if entry.strip()]',
        '    return [entry for entry in (raw or "").split(os.pathsep) if entry]',
        (
            f"{ROOTST}::test_configured_roots_ignores_blank_entries",
            # The same revert at the pure helper both readers share: without the
            # `.strip()` a padded entry stays padded and `"  "` stays a root.
            f"{ROOTST}::test_split_roots_trims_each_entry_and_drops_the_blanks",
        ),
    ),
    Revert(
        "output_roots: an unset variable is stringified, so a variable nobody set reads as one root named None",
        ADDON_OUTPUT_ROOTS,
        '    return [entry.strip() for entry in (raw or "").split(os.pathsep) if entry.strip()]',
        '    return [entry.strip() for entry in f"{raw}".split(os.pathsep) if entry.strip()]',
        (f"{ROOTST}::test_split_roots_reads_an_unset_variable_as_no_roots",),
    ),
    Revert(
        "output_roots control: writable_roots keeps nothing at all",
        ADDON_OUTPUT_ROOTS,
        "        seen.add(path)\n        normalized.append(path)",
        "        seen.add(path)",
        (f"{ROOTST}::test_writable_roots_keeps_existing_writable_directories",),
    ),
    # The next three share an anchor - the one comprehension `writable_roots` is now -
    # and revert that one line three different ways.
    Revert(
        "output_roots: a path that does not exist is kept",
        ADDON_OUTPUT_ROOTS,
        "if os.path.isdir(path) and os.access(path, os.W_OK)]",
        "if not (os.path.isfile(path) or (os.path.isdir(path) and not os.access(path, os.W_OK)))]",
        (f"{ROOTST}::test_writable_roots_drops_paths_that_do_not_exist",),
    ),
    Revert(
        "output_roots: a plain file is accepted as a root",
        ADDON_OUTPUT_ROOTS,
        "if os.path.isdir(path) and os.access(path, os.W_OK)]",
        "if os.path.exists(path) and os.access(path, os.W_OK)]",
        (f"{ROOTST}::test_writable_roots_drops_files",),
    ),
    Revert(
        "output_roots: a read-only directory is offered as writable",
        ADDON_OUTPUT_ROOTS,
        "if os.path.isdir(path) and os.access(path, os.W_OK)]",
        "if os.path.isdir(path)]",
        (f"{ROOTST}::test_writable_roots_drops_read_only_directories",),
    ),
    Revert(
        "output_roots: duplicates are no longer collapsed",
        ADDON_OUTPUT_ROOTS,
        "        if path in seen:\n            continue\n",
        "",
        (f"{ROOTST}::test_writable_roots_dedupes_while_preserving_order",),
    ),
    Revert(
        "output_roots: roots are reported relative to the addon's cwd",
        ADDON_OUTPUT_ROOTS,
        "        path = os.path.abspath(os.path.expanduser(str(candidate)))",
        "        path = os.path.expanduser(str(candidate))",
        (f"{ROOTST}::test_writable_roots_reports_absolute_paths",),
    ),
    Revert(
        "output_roots: empty and None candidates reach the scan",
        ADDON_OUTPUT_ROOTS,
        "        if not candidate:\n            continue\n",
        "",
        (f"{ROOTST}::test_writable_roots_ignores_empty_candidates",),
    ),
    # `normalized_candidates` is the half of `writable_roots` that touches nothing, so
    # its own test uses paths that do not exist: a probe put back in empties the result.
    Revert(
        "output_roots: the pure half probes the filesystem again, so a root is dropped before it is offered",
        ADDON_OUTPUT_ROOTS,
        "        seen.add(path)\n        normalized.append(path)",
        "        seen.add(path)\n        if os.path.isdir(path):\n            normalized.append(path)",
        (f"{ROOTST}::test_normalized_candidates_expand_and_dedupe_without_probing_anything",),
    ),
    Revert(
        "output_roots wiring: the handshake ignores the deployment-configured roots",
        ADDON_SERVER_CORE,
        "            *configured_roots(),\n",
        "",
        (f"{ROOTST}::test_get_addon_info_reports_writable_output_roots",),
    ),
    Revert(
        "output_roots wiring: an unconfigured Blender reports no writable root at all",
        ADDON_SERVER_CORE,
        '            getattr(bpy.app, "tempdir", None),\n            tempfile.gettempdir(),\n'
        '            os.path.expanduser("~"),\n',
        "",
        (f"{ROOTST}::test_get_addon_info_reports_roots_without_any_configuration",),
    ),
]
