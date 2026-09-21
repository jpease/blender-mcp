"""
The server instructions carry the cross-cutting conventions no single tool owns.

How an object name resolves after a library override applies to many object tools. It is
stated once here, because repeating it on each tool would grow every session's catalog. No
payload check covers the instructions, so only these tests stop the rule being deleted.
"""

from blender_mcp.server.app import SERVER_INSTRUCTIONS, mcp

# Whitespace collapsed, so re-flowing the paragraph cannot break these tests.
_PROSE = " ".join(SERVER_INSTRUCTIONS.split())


def test_the_instructions_state_how_an_overridden_name_resolves() -> None:
    """An agent must learn the common case - the name means the editable override - before it edits."""
    assert "resolves to the editable override" in _PROSE


def test_the_instructions_name_the_way_out_of_the_ambiguity_refusal() -> None:
    """The rare refusal is only actionable if the agent knows the uid and the tool that resolves it."""
    assert "refused with each library's session_uid" in _PROSE
    assert "override one with create_override" in _PROSE


def test_the_served_instructions_are_the_ones_stated_here() -> None:
    """A rule written in the constant but not handed to FastMCP reaches no client."""
    assert mcp.instructions == SERVER_INSTRUCTIONS


def test_the_instructions_state_the_three_parameter_naming_conventions() -> None:
    """A guessed `type=`, a missing `collection_name`, or flat kwargs each cost one failed call."""
    assert "never bare `type`" in _PROSE
    assert "always takes `collection_name` explicitly" in _PROSE
    assert "named `patch`" in _PROSE


def test_the_instructions_state_that_one_shot_is_one_action() -> None:
    """Two actions do not play together, and the second silently wins - the costliest surprise here."""
    assert "an ID holds one action" in _PROSE


def test_the_instructions_name_the_tool_that_moves_the_playhead() -> None:
    """Every inspection tool reports the current frame, so this is the whole animation review loop."""
    assert "`set_scene_frame` is how any inspection tool" in _PROSE


def test_the_instructions_point_a_held_contact_at_the_ik_tool() -> None:
    """The reported failure was a sliding foot authored with repeated FK rotation."""
    assert "`keyframe_bone_reach`" in _PROSE
    assert "repeated FK rotation slides it" in _PROSE
