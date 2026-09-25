"""
Rows guarding the one command registry and the provenance ledger.

A row's name, gate and classification, rollback protection decided by parameter naming,
and the ledger a saved `.blend` carries.

Label prefixes: `registry:`, `authored:`.
"""

from .common import ADDON_AUTHORED, ADDON_COMMAND_REGISTRY, AUTHT, REGT, Revert

ROWS: list[Revert] = [
    # --- the one command registry: a row's name, its gate, and its classification ---
    Revert(
        "registry: a registered command names a handler this class does not have, so it dispatches to nothing",
        ADDON_COMMAND_REGISTRY,
        '        "sync_data_name": CommandSpec(),',
        '        "sync_data_name_typo": CommandSpec(),',
        (f"{REGT}::test_every_registered_command_resolves_to_a_handler",),
    ),
    Revert(
        "registry: an enabled provider's rows are dropped from the built table, so they classify nothing",
        ADDON_COMMAND_REGISTRY,
        "itertools.chain(_UNGATED_COMMAND_NAMES, *enabled_names)",
        "_UNGATED_COMMAND_NAMES",
        (f"{REGT}::test_the_whole_dispatch_table_comes_from_the_registry",),
    ),
    Revert(
        "registry: the provider gate stops gating, so a disabled integration's commands are advertised anyway",
        ADDON_COMMAND_REGISTRY,
        "zip(_PROVIDER_SCENE_FLAGS, gate, strict=True) if on",
        "zip(_PROVIDER_SCENE_FLAGS, gate, strict=True) if True",
        (f"{REGT}::test_a_disabled_provider_withholds_exactly_its_own_commands",),
    ),
    Revert(
        "registry: a second, name-keyed classification table comes back beside the registry",
        ADDON_COMMAND_REGISTRY,
        "_UNCLASSIFIED = CommandSpec()",
        "_UNCLASSIFIED = CommandSpec()\n_READ_ONLY_COMMANDS = frozenset()",
        (f"{REGT}::test_no_classification_set_survives_outside_the_registry",),
    ),
    Revert(
        "registry: an unregistered command name is answered read-only, so a client's typo skips the transaction",
        ADDON_COMMAND_REGISTRY,
        "        return COMMANDS.get(cmd_type, _UNCLASSIFIED) if isinstance(cmd_type, str) else _UNCLASSIFIED",
        "        return COMMANDS.get(cmd_type, CommandSpec(read_only=True)) if isinstance(cmd_type, str) "
        "else _UNCLASSIFIED",
        (f"{REGT}::test_an_unregistered_command_is_classified_as_an_ordinary_mutation",),
    ),
    Revert(
        "registry: ping is classified as a mutation, so a liveness check opens a transaction",
        ADDON_COMMAND_REGISTRY,
        '        "ping": CommandSpec(read_only=True),',
        '        "ping": CommandSpec(),',
        (f"{REGT}::test_ping_answers_read_only_without_a_live_blender",),
    ),
    # --- rollback protection is decided by parameter naming, and `target_names` is that decision ---
    Revert(
        "registry: the scalar object-name params are not read, so a named target gets no state captured",
        ADDON_COMMAND_REGISTRY,
        "    for key in _TARGET_NAME_PARAMS:",
        "    for key in ():",
        (
            f"{REGT}::test_target_names_reads_the_naming_convention[scalar-key]",
            f"{REGT}::test_target_names_reads_the_naming_convention[duplicates-collapse]",
            f"{REGT}::test_target_names_reads_the_naming_convention[order-follows-the-table-not-the-params]",
        ),
    ),
    Revert(
        "registry: the list-valued object-name params are not read, so a multi-target edit rolls back nothing",
        ADDON_COMMAND_REGISTRY,
        "    for key in _TARGET_NAMES_PARAMS:",
        "    for key in ():",
        (
            f"{REGT}::test_target_names_reads_the_naming_convention[list-key]",
            f"{REGT}::test_target_names_reads_the_naming_convention[non-strings-in-a-list-are-skipped]",
        ),
    ),
    Revert(
        "registry: the nested record params are not walked, so a per-record target is unprotected",
        ADDON_COMMAND_REGISTRY,
        "    for container_key, name_keys in _TARGET_RECORD_PARAMS:",
        "    for container_key, name_keys in ():",
        (
            f"{REGT}::test_target_names_reads_the_naming_convention[records-in-a-list]",
            f"{REGT}::test_target_names_reads_the_naming_convention[two-name-keys-in-one-record]",
            f"{REGT}::test_target_names_reads_the_naming_convention[a-lone-record]",
            f"{REGT}::test_target_names_reads_the_naming_convention[rigid-body-record-keys]",
        ),
    ),
    Revert(
        "registry: target names stop being deduplicated, so one object is snapshotted and restored twice",
        ADDON_COMMAND_REGISTRY,
        "    return list(dict.fromkeys(names))",
        "    return names",
        (f"{REGT}::test_target_names_reads_the_naming_convention[duplicates-collapse]",),
    ),
    Revert(
        "registry: `name` joins the target params, so create_primitive captures the object it is about to make",
        ADDON_COMMAND_REGISTRY,
        '_TARGET_NAME_PARAMS: tuple[str, ...] = (\n    "object_name",',
        '_TARGET_NAME_PARAMS: tuple[str, ...] = (\n    "name",\n    "object_name",',
        (f"{REGT}::test_target_names_reads_the_naming_convention[name-is-a-new-object-not-a-target]",),
    ),
    Revert(
        "registry: a list's elements are taken untyped, so a malformed params dict reaches find_object",
        ADDON_COMMAND_REGISTRY,
        "names.extend(name for name in value if isinstance(name, str))",
        "names.extend(value)",
        (f"{REGT}::test_target_names_reads_the_naming_convention[non-strings-in-a-list-are-skipped]",),
    ),
    Revert(
        "registry: a scalar param is taken untyped, so a number where a name belongs is treated as a target",
        ADDON_COMMAND_REGISTRY,
        "        value = params.get(key)\n        if isinstance(value, str):\n            names.append(value)",
        "        value = params.get(key)\n        if value is not None:\n            names.append(value)",
        (f"{REGT}::test_target_names_reads_the_naming_convention[a-non-string-scalar-is-not-a-name]",),
    ),
    # --- the provenance ledger a saved .blend carries ---
    Revert(
        "authored: the ledger reports newest first, so the saved file's provenance order is a lie",
        ADDON_AUTHORED,
        'return [{"collection": collection, "name": name} for collection, name in _LEDGER.entries]',
        'return [{"collection": collection, "name": name} for collection, name in reversed(_LEDGER.entries)]',
        (f"{AUTHT}::test_records_arrive_oldest_first_and_a_repeat_is_one_datablock",),
    ),
    Revert(
        "authored: the ledger stops evicting, so a long session grows unbounded and its freed names stay suppressed",
        ADDON_AUTHORED,
        "        if len(_LEDGER.entries) >= MAX_TRACKED_AUTHORED:",
        "        if False:",
        (
            f"{AUTHT}::test_overflow_drops_the_oldest_and_stops_claiming_a_complete_history",
            f"{AUTHT}::test_a_name_that_was_evicted_can_be_recorded_again",
        ),
    ),
    Revert(
        "authored: clear() forgets the entries but keeps the truncation claim, describing a database that is gone",
        ADDON_AUTHORED,
        "    _LEDGER.truncated = False",
        "    pass  # truncation claim left standing",
        (f"{AUTHT}::test_clearing_forgets_the_entries_and_the_truncation_claim",),
    ),
]
