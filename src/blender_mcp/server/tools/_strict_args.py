"""
Make the runtime honour the `additionalProperties: false` this server already advertises.

FastMCP builds one pydantic model per tool from the handler's signature
(`mcp.server.fastmcp.utilities.func_metadata`), and the base it derives them from sets only
`arbitrary_types_allowed`. With no `extra` key, pydantic falls back to its own default of
`extra="ignore"`: an argument the handler never declared is dropped on the floor and the call
proceeds as though the agent had never sent it. Everything else about this surface says the
opposite. The documentation pass stamps `additionalProperties: false` onto every advertised tool
schema (`_documentation._describe_schema`), and the project's own nested patch models are
`extra="forbid"` (`_inputs.StrictModel`). Only the SDK-generated top-level model was
permissive, which is the one place a client's whole argument dict arrives.

The gap is expensive in practice, because the failure is silent and looks like success. An agent
that sends `patch={"focal_length": 28.0}` instead of `optics={"lens": 28.0}` gets a healthy
envelope back from a `configure_camera` that was called with `optics=None`, and only notices
several renders later that the camera never left 50mm. Forbidding extras turns that into a
validation error naming the offending key, before Blender is touched at all.

Doing so means reaching into `tool.fn_metadata.arg_model`, which is SDK internals: nothing in
`mcp` promises those attributes exist or that the default stays `ignore`. That is why
`tests/server/test_strict_tool_args.py` asserts the property on the registered tool objects rather
than on this function - an SDK upgrade that renames the attribute or regenerates the models must
fail a test, not quietly restore the bug - and why a missing attribute raises here instead of
being skipped, since skipping every tool would harden nothing while still reporting success.
"""

from mcp.server.fastmcp import FastMCP


def forbid_unknown_tool_arguments(server: FastMCP) -> int:
    """
    Harden every currently registered tool's generated argument model to reject unknown keys.

    Args:
        server: The FastMCP app to harden. Only tools registered by the time of the call are
            covered, so this belongs after the registration imports.

    Returns:
        int: How many argument models this call changed. Models already forbidding extras are
            counted as unchanged, so calling twice reports zero the second time.

    Raises:
        RuntimeError: If a registered tool carries no `fn_metadata`, or that metadata carries no
            `arg_model`. Both are SDK internals; if they move, the whole mechanism is inert and
            must say so loudly at import time rather than at the first silently-dropped argument.

    """
    hardened = 0
    for tool in server._tool_manager.list_tools():
        if not hasattr(tool, "fn_metadata"):
            raise RuntimeError(
                f"Tool {tool.name!r} has no 'fn_metadata': the MCP SDK's argument-model layout has changed, "
                "so unknown tool arguments would be silently ignored again. Update _strict_args.py."
            )
        metadata = tool.fn_metadata
        if not hasattr(metadata, "arg_model"):
            raise RuntimeError(
                f"Tool {tool.name!r} has no 'fn_metadata.arg_model': the MCP SDK's argument-model layout has "
                "changed, so unknown tool arguments would be silently ignored again. Update _strict_args.py."
            )
        arg_model = metadata.arg_model
        if arg_model.model_config.get("extra") == "forbid":
            continue
        arg_model.model_config["extra"] = "forbid"
        # Config is read when the core validator is built, so an already-built model keeps the old
        # behaviour until it is rebuilt; `force` is needed because pydantic considers it complete.
        arg_model.model_rebuild(force=True)
        hardened += 1
    return hardened
