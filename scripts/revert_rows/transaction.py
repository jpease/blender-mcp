"""
Rows guarding rollback that survives a file swap or a library reload.

Label prefixes: `transaction:`, `transaction harness:`.
"""

from .common import ADDON_OBJECT_STATE, ADDON_SERVER_CORE, ADDON_SESSION, ADDON_TRANSACTION, MUTT, TSWAPT, Revert

ROWS: list[Revert] = [
    # --- rollback that survives a file swap or a library reload -------
    Revert(
        "transaction: the library commands enter mutation_transaction, so a failed reload deletes what it reloaded",
        ADDON_SERVER_CORE,
        "            or spec.datablock_replacing\n",
        "",
        (
            f"{TSWAPT}::test_a_library_replacing_command_never_reaches_mutation_transaction",
            f"{TSWAPT}::test_a_reload_that_fails_after_churning_its_library_removes_nothing",
        ),
    ),
    Revert(
        "transaction: link_canon_library joins the datablock-replacing set, so a failed link leaks its library",
        ADDON_SERVER_CORE,
        '        "link_canon_library": CommandSpec(),',
        '        "link_canon_library": CommandSpec(datablock_replacing=True),',
        (
            f"{TSWAPT}::test_the_datablock_replacing_set_is_the_three_library_commands_and_nothing_read_only",
            f"{TSWAPT}::test_link_canon_library_still_enters_mutation_transaction",
            f"{TSWAPT}::test_a_failed_link_rolls_back_its_library_with_the_file_handlers_registered",
            f"{TSWAPT}::test_a_failed_link_never_removes_a_datablock_its_library_removal_already_freed",
        ),
    ),
    Revert(
        "transaction: Transaction.invalidate() does nothing, so a swap inside a transaction is rolled back",
        ADDON_TRANSACTION,
        (
            "        self.invalidated = True\n"
            "        self._before_ids = {}\n"
            "        self._backup_ids = frozenset()\n"
            "        invalidate_object_states(self._states)\n"
            "        self._states = []\n"
        ),
        "        return\n",
        (
            f"{TSWAPT}::test_a_swap_inside_an_open_transaction_is_not_rolled_back_and_says_so",
            f"{TSWAPT}::test_an_invalidated_geometry_backup_is_dropped_without_remove",
            f"{TSWAPT}::test_blend_import_post_during_a_flagged_reload_invalidates_the_open_transaction",
        ),
    ),
    Revert(
        "transaction: rollback ignores the invalidation and diffs against the emptied snapshot",
        ADDON_TRANSACTION,
        "        if self.invalidated:\n            return ROLLBACK_SKIPPED_WARNING\n",
        "",
        (
            f"{TSWAPT}::test_a_swap_inside_an_open_transaction_is_not_rolled_back_and_says_so",
            f"{TSWAPT}::test_an_invalidated_geometry_backup_is_dropped_without_remove",
            f"{TSWAPT}::test_blend_import_post_during_a_flagged_reload_invalidates_the_open_transaction",
        ),
    ),
    Revert(
        "transaction: a skipped rollback re-raises the original error, so the warning never reaches the envelope",
        ADDON_TRANSACTION,
        "        if warning is None:\n            raise\n",
        "        raise\n",
        (
            f"{TSWAPT}::test_a_swap_inside_an_open_transaction_is_not_rolled_back_and_says_so",
            f"{TSWAPT}::test_blend_import_post_during_a_flagged_reload_invalidates_the_open_transaction",
        ),
    ),
    Revert(
        "transaction: ObjectState.invalidate() removes the geometry backup a load may already have freed",
        ADDON_OBJECT_STATE,
        "        self.materials = []\n        self.geometry_backup = None\n\n    def discard_backup",
        "        self.materials = []\n        self.discard_backup()\n\n    def discard_backup",
        (
            f"{TSWAPT}::test_an_invalidated_geometry_backup_is_dropped_without_remove",
            f"{TSWAPT}::test_object_state_invalidate_releases_every_live_reference_without_touching_bpy",
        ),
    ),
    Revert(
        "transaction: libraries untracked, so a failed link leaks the Library datablock",
        ADDON_TRANSACTION,
        '    "libraries",\n)',
        ")",
        (
            f"{TSWAPT}::test_libraries_are_tracked",
            f"{TSWAPT}::test_a_failed_link_rolls_back_its_library_with_the_file_handlers_registered",
            f"{TSWAPT}::test_a_failed_link_never_removes_a_datablock_its_library_removal_already_freed",
        ),
    ),
    Revert(
        "transaction: libraries removed in reverse order with everything else, before their linked datablocks",
        ADDON_TRANSACTION,
        'if coll_name not in {"objects", "libraries"}]',
        'if coll_name != "objects"]',
        (f"{TSWAPT}::test_a_failed_link_never_removes_a_datablock_its_library_removal_already_freed",),
    ),
    Revert(
        "transaction: blend_import_post invalidates on every import, disarming a failed link's rollback",
        ADDON_SESSION,
        "    if library_replace_in_progress():\n        invalidate_active_transaction()\n",
        "    invalidate_active_transaction()\n",
        (
            f"{TSWAPT}::test_a_failed_link_rolls_back_its_library_with_the_file_handlers_registered",
            f"{TSWAPT}::test_blend_import_post_without_the_flag_leaves_the_transaction_armed",
        ),
    ),
    Revert(
        "transaction: the replace flag is not restored when the reload raises",
        ADDON_TRANSACTION,
        "    try:\n        yield\n    finally:\n        _DISPATCH.library_replace_in_progress = previous",
        "    yield\n    _DISPATCH.library_replace_in_progress = previous",
        (f"{TSWAPT}::test_the_replace_flag_is_cleared_when_the_reload_raises",),
    ),
    Revert(
        "transaction: load_post stops invalidating the open transaction",
        ADDON_SESSION,
        "    invalidate_active_transaction()\n    # The datablocks",
        "    # The datablocks",
        (
            f"{TSWAPT}::test_a_swap_inside_an_open_transaction_is_not_rolled_back_and_says_so",
            f"{TSWAPT}::test_an_invalidated_geometry_backup_is_dropped_without_remove",
        ),
    ),
    Revert(
        "transaction: the blend_import_post handler is never registered",
        ADDON_SESSION,
        '    ("blend_import_post", _on_blend_import_post),\n',
        "",
        (
            f"{TSWAPT}::test_blend_import_post_is_registered_once_across_disable_enable_cycles",
            f"{TSWAPT}::test_blend_import_post_during_a_flagged_reload_invalidates_the_open_transaction",
        ),
    ),
    Revert(
        "transaction: the active transaction is cleared only on success, so a failed command stays reachable",
        ADDON_TRANSACTION,
        (
            "        txn.commit()\n"
            "    finally:\n"
            "        # `finally`, not `except`: a BaseException (Esc's KeyboardInterrupt)\n"
            "        # must not leave a finished command reachable from the next handler.\n"
            "        _DISPATCH.active = previous\n"
        ),
        "        txn.commit()\n        _DISPATCH.active = previous\n",
        (
            f"{TSWAPT}::test_the_active_transaction_never_outlives_its_command[exception]",
            f"{TSWAPT}::test_the_active_transaction_never_outlives_its_command[base]",
        ),
    ),
    Revert(
        "transaction: the active transaction is never cleared",
        ADDON_TRANSACTION,
        (
            "    finally:\n"
            "        # `finally`, not `except`: a BaseException (Esc's KeyboardInterrupt)\n"
            "        # must not leave a finished command reachable from the next handler.\n"
            "        _DISPATCH.active = previous\n"
        ),
        "",
        (f"{TSWAPT}::test_the_active_transaction_never_outlives_its_command[success]",),
    ),
    Revert(
        "transaction harness: rollback stops removing new datablocks, so the regression guards stop reproducing",
        ADDON_TRANSACTION,
        "        _remove_datablocks(_new_datablocks(self._before_ids, exclude_ids=self._backup_ids))\n",
        "",
        (
            f"{MUTT}::test_regression_guard_a_transaction_unaware_of_a_file_swap_removes_the_whole_new_file",
            f"{MUTT}::test_regression_guard_a_transaction_unaware_of_a_library_reload_removes_the_reloaded_contents",
        ),
    ),
]
