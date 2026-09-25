"""
Rows guarding an install that leaves exactly one add-on for Blender to list.

Label prefixes: `install:`, `dispatch:`, `get_addon_status:`.
"""

from .common import ADDON_MANAGER, ADDON_SERVER_CORE, AMT, CORET, MUTT, SERVER_CORE_TOOL, Revert

ROWS: list[Revert] = [
    # --- installing the addon leaves exactly one addon for Blender to list ---
    Revert(
        "install: the backup is kept inside the directory Blender scans for addons",
        ADDON_MANAGER,
        '    backup = backup_directory(path.parent) / (path.name + ".bak")\n',
        '    backup = path.with_name(path.name + ".bak")\n',
        (
            f"{AMT}::test_repeat_installs_leave_one_addon_for_blender_to_load",
            f"{AMT}::test_repeat_install_preserves_original_backup",
        ),
    ),
    Revert(
        "install: the installer treats its own backup as an install and backs that up too",
        ADDON_MANAGER,
        '        if path.name.endswith(".bak"):\n            stale_backups.append(str(path))\n            continue\n',
        "",
        (f"{AMT}::test_install_leaves_an_older_installers_backup_alone_and_names_it",),
    ),
    Revert(
        "install: a development symlink is copied through instead of being left alone",
        ADDON_MANAGER,
        "    if target.is_symlink():\n",
        "    if False:\n",
        (f"{AMT}::test_install_refuses_to_write_through_a_development_symlink",),
    ),
    Revert(
        "dispatch: a refused request is logged as a fault, traceback and all",
        ADDON_SERVER_CORE,
        "        except ValueError as refusal:\n",
        "        except _NeverRaised as refusal:\n",
        (f"{MUTT}::test_a_refused_request_is_logged_without_a_traceback",),
        also="\nclass _NeverRaised(Exception):\n    pass\n",
    ),
    Revert(
        "get_addon_status: get_addon_status hardcodes the policy as unenforced",
        SERVER_CORE_TOOL,
        '        "file_roots_enforced": result.file_roots_enforced,',
        '        "file_roots_enforced": False,',
        (f"{CORET}::test_get_addon_status_reports_the_file_path_policy",),
    ),
    Revert(
        "get_addon_status: the file-policy keys go undocumented",
        SERVER_CORE_TOOL,
        '"file_roots"/"file_roots_enforced"',
        "file roots and whether enforced",
        (f"{CORET}::test_get_addon_status_documents_every_key_it_returns",),
    ),
]
