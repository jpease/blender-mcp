"""
Tell the next reply when the scene changed between two commands: an undo, a redo, or an edit made outside.

Between two agent calls the user can undo, redo, or edit things in Blender's UI, and the agent
would keep acting on what it read before. `undo_post` and `redo_post` are counted, and a
`depsgraph_update_post` that fires outside command execution raises a coarse "edited outside"
flag naming a few of the objects it touched. The next reply, of any command on any connection,
carries one warning saying so, and the state starts over.

It is deliberately coarse; three limits follow from what Blender reports:

- The signal is per add-on, not per MCP session: every client sharing this add-on sees one
  notice, given to whichever command is answered next.
- A command's own re-evaluation fires `depsgraph_update_post` after the command returned, so
  updates within `QUIET_PERIOD_SECONDS` of a command's end are ignored. An edit made in that
  window is missed. Measured with a Blender 5.2 window, the re-evaluation of an edit made in a
  timer callback (as the drain timer runs commands) arrives 0.06-0.24 ms after the callback
  returns, far inside the window.
- Selecting, activating, hiding and scene settings such as `frame_end` all fire a Scene-only
  update with every `is_updated_*` False, which cannot be told apart; such an update is ignored,
  so a scene-setting edit is missed rather than every viewport click being reported.

Only the liquid bake leaves work running past its reply: START_BAKE and RESUME start Blender's
fluid job with `INVOKE_DEFAULT`, so the handler marks the domain with `JOB_MARKER_KEY`, and a
marked object's updates are not outside edits. Every other bake, render and import the add-on runs
is an `EXEC_DEFAULT` call that finishes before the command returns. In a Blender 5.2 window, cloth
and rigid-body point-cache bakes, a Geometry Nodes bake, an `object.bake` and a `render.render`
preview each left no notice on a read 3 s later, while a timer edit did. ND operators run with
`INVOKE_DEFAULT`; one that goes modal is answered with an error, and anything it changes afterwards
follows the user's input, so a notice for it is not false. An async job added later must mark its
objects the same way, or it raises a false notice.

Main thread only: Blender runs the handlers there, and `server_core` dispatches commands there.
"""

import time

import bpy

from bpy.app.handlers import persistent

# How long after a command's end an update is taken to be that command's own re-evaluation.
QUIET_PERIOD_SECONDS = 0.5

# The custom property an object carries while an async job a command started is still writing it.
# The liquid bake's pending-bake record; its value is persisted in .blend files, so it must not change.
JOB_MARKER_KEY = "blendermcp_liquid_pending_bake"

# Commands the MCP server sends on its own behalf, whose replies no agent reads: they must neither
# carry the notice nor consume it.
_SERVER_PROBES = frozenset({"get_addon_info", "ping"})

_NAMED_OBJECTS_LIMIT = 5
_UPDATE_FLAGS = ("is_updated_transform", "is_updated_geometry", "is_updated_shading")


class _Watch:
    """
    What changed since the last reply, and whether a command is running.

    Attributes:
        undos: `undo_post` firings outside command execution.
        redos: `redo_post` firings outside command execution.
        edited: True once an outside update touched anything.
        edited_objects: session_uid of every distinct object an outside update touched.
        named_objects: The first `_NAMED_OBJECTS_LIMIT` of those objects' names, in order.
        executing: True while `server_core` runs a command.
        command_ended_at: `time.monotonic()` when the last command finished, or None.

    """

    def __init__(self) -> None:
        """Start with nothing seen and no command run."""
        self.executing = False
        self.command_ended_at: float | None = None
        self.reset()

    def reset(self) -> None:
        """Forget every change seen; the command bookkeeping is kept."""
        self.undos = 0
        self.redos = 0
        self.edited = False
        self.edited_objects: set[int] = set()
        self.named_objects: list[str] = []


_WATCH = _Watch()


def _in_quiet_period() -> bool:
    """
    Say whether an update now belongs to the command that just ended.

    Returns:
        bool: True while a command runs or within `QUIET_PERIOD_SECONDS` of its end.

    """
    if _WATCH.executing:
        return True
    return _WATCH.command_ended_at is not None and time.monotonic() - _WATCH.command_ended_at < QUIET_PERIOD_SECONDS


def _is_outside_edit(update) -> bool:
    """
    Say whether one depsgraph update is a change the agent did not make.

    Args:
        update: A `DepsgraphUpdate`.

    Returns:
        bool: False for a Scene-only update with no `is_updated_*` flag (a selection, an active
        object, a hide toggle) and for an object an async job marks; True otherwise.

    """
    original = update.id.original
    if original.id_type == "SCENE" and not any(getattr(update, flag) for flag in _UPDATE_FLAGS):
        return False
    return not (original.id_type == "OBJECT" and original.get(JOB_MARKER_KEY))


def _record_edit(original) -> None:
    """
    Raise the edit flag, naming the datablock when it is an object not seen yet.

    Args:
        original: The original datablock an update touched.

    """
    _WATCH.edited = True
    if original.id_type != "OBJECT" or original.session_uid in _WATCH.edited_objects:
        return
    _WATCH.edited_objects.add(original.session_uid)
    if len(_WATCH.named_objects) < _NAMED_OBJECTS_LIMIT:
        _WATCH.named_objects.append(original.name)


@persistent
def _on_undo_post(_scene=None, _unused=None) -> None:
    """
    Count an undo made between commands.

    Args:
        _scene: The scene Blender passes; unused.
        _unused: Blender passes a second positional argument, always None.

    """
    if not _WATCH.executing:
        _WATCH.undos += 1


@persistent
def _on_redo_post(_scene=None, _unused=None) -> None:
    """
    Count a redo made between commands.

    Args:
        _scene: The scene Blender passes; unused.
        _unused: Blender passes a second positional argument, always None.

    """
    if not _WATCH.executing:
        _WATCH.redos += 1


@persistent
def _on_load_post(_file_path="", _unused=None) -> None:
    """
    Forget every change seen: the session epoch already reports a swap.

    Args:
        _file_path: The .blend Blender loaded; unused.
        _unused: Blender passes a second positional argument, always None.

    """
    _WATCH.reset()


@persistent
def _on_depsgraph_update_post(_scene, depsgraph) -> None:
    """
    Record the objects an update made outside command execution touched.

    Keyed on `update.id.original`, because the update's own id is the evaluated copy, whose
    session_uid is 0.

    Args:
        _scene: The scene Blender passes; unused.
        depsgraph: The depsgraph that was just evaluated.

    """
    if _in_quiet_period():
        return
    for update in depsgraph.updates:
        if _is_outside_edit(update):
            _record_edit(update.id.original)


def _times(count: int) -> str:
    """
    Spell a repeat count.

    Args:
        count: How many times.

    Returns:
        str: "once" or "N times".

    """
    return "once" if count == 1 else f"{count} times"


def _object_list() -> str:
    """
    Name the edited objects, the first few by name and the rest by count.

    Returns:
        str: For example "Cube, Lamp and 3 more", or "Cube and Lamp".

    """
    names = _WATCH.named_objects
    more = len(_WATCH.edited_objects) - len(names)
    if more:
        return f"{', '.join(names)} and {more} more"
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} and {names[-1]}"


def pending_notice() -> str | None:
    """
    Word what changed since the last reply as one warning.

    An undo or redo re-evaluates every object, so when either happened its notice stands for the
    edit flag too.

    Returns:
        str | None: The warning, or None when nothing changed.

    """
    if _WATCH.undos or _WATCH.redos:
        moves = [
            f"{verb} {_times(count)}" for verb, count in (("undone", _WATCH.undos), ("redone", _WATCH.redos)) if count
        ]
        return f"The scene was {' and '.join(moves)} since the last command - re-inspect anything you read before."
    if not _WATCH.edited:
        return None
    if _WATCH.named_objects:
        return (
            f"The scene was edited outside this session since the last command ({_object_list()}) - "
            "re-read them before relying on earlier values."
        )
    return (
        "The scene was edited outside this session since the last command - "
        "re-read anything you rely on before using earlier values."
    )


def command_started() -> None:
    """Mark a command as running, so the updates and undos it causes are its own."""
    _WATCH.executing = True


def command_finished() -> None:
    """Mark the command as finished and start the quiet period."""
    _WATCH.executing = False
    _WATCH.command_ended_at = time.monotonic()


def annotated_response(cmd_type, response: dict, *, session_swap: bool) -> dict:
    """
    Attach the pending notice to one command's response, and start over once it is attached.

    A session swap replaces what the notice describes, so it starts over without one. A success
    whose result is not a dict has nowhere to put it, so the notice waits for the next reply.

    Args:
        cmd_type: The command answered.
        response: Its `{"status": ...}` response.
        session_swap: Whether the command replaces the open database.

    Returns:
        dict: The response, with the notice appended to the result's `warnings` or, for an
        error, to its message.

    """
    if cmd_type in _SERVER_PROBES:
        return response
    if session_swap:
        _WATCH.reset()
        return response
    notice = pending_notice()
    if notice is None:
        return response
    if response.get("status") == "error":
        response = {**response, "message": f"{response.get('message', '')} {notice}".strip()}
    elif isinstance(response.get("result"), dict):
        result = {**response["result"], "warnings": [*response["result"].get("warnings", []), notice]}
        # A handler that returned a failure shape is reported by its "error" text alone (the
        # server's `connection.ad_hoc_failure_message`), so the notice has to be in that text too.
        if result.get("error") and not result.get("cancelled"):
            result["error"] = f"{result['error']} {notice}"
        response = {**response, "result": result}
    else:
        return response
    _WATCH.reset()
    return response


# One table, so registration, removal and the duplicate check cannot drift apart.
_HANDLER_BINDINGS = (
    ("undo_post", _on_undo_post),
    ("redo_post", _on_redo_post),
    ("load_post", _on_load_post),
    ("depsgraph_update_post", _on_depsgraph_update_post),
)


def register_handlers() -> None:
    """Attach the handlers, exactly once; Blender's handler lists accept duplicates."""
    for list_name, handler in _HANDLER_BINDINGS:
        handler_list = getattr(bpy.app.handlers, list_name)
        if handler not in handler_list:
            handler_list.append(handler)


def unregister_handlers() -> None:
    """Detach the handlers, including any a previous cycle stacked."""
    for list_name, handler in _HANDLER_BINDINGS:
        handler_list = getattr(bpy.app.handlers, list_name)
        while handler in handler_list:
            handler_list.remove(handler)
