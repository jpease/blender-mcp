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
