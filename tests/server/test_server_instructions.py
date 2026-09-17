"""
The server instructions carry the cross-cutting conventions no single tool owns.

Object-name resolution after a library override spans nine object tools and
`get_object_info`. It is stated once here rather than on each tool: measured
post-Phase-2, a sentence on every tool costs 1,302 B of `shot`-mode catalog
(186 B x the 7 tools served there) every session, against 252 chars once here, for
a refusal that only fires when several libraries link one name and no local
object has it. `payload_report` measures `tools/list` only, so nothing else pins
this text: without this test the rule could be deleted and every gate stay green.
"""

from blender_mcp.server.app import SERVER_INSTRUCTIONS, mcp

# Compared with line wraps collapsed, so re-flowing the paragraph cannot break a
# test whose subject is what the sentence says, not where it breaks.
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
