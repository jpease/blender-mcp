"""MCP prompts."""

from .app import mcp


@mcp.prompt()
def asset_creation_strategy() -> str:
    """
    Define the staged workflow agents should follow when creating or editing Blender scenes.

    Returns:
        str: Result produced by the operation.

    """
    return """Work through this Blender task in stages. Do not skip ahead to editing before the
    earlier stages have been done, and stop at any gate below rather than pushing forward on a
    result you haven't checked.

    1. Verify capability first.
        - Call get_addon_status() before any other tool call. If up_to_date is False or a
          warning is present, treat that as a real constraint (a schema this server sends
          may not be understood by the connected addon) - do not just proceed and hope.
        - Only check get_integration_status(provider=...) for "polyhaven", "sketchfab", or
          "nd" if the task actually needs that provider (importing an asset/HDRI/texture, or
          an ND-specific hard-surface workflow). Do not check every provider on every task.

    2. Inspect before touching anything.
        - Call list_scene_objects() to see what exists. It is paginated ("limit"/"offset" in,
          "truncated"/"next_offset" out inside "data") and each entry is only
          {"name", "type", "location"} with location in local/object-space - it does not carry
          bounding boxes, modifiers, or materials.
        - For a specific target object, follow up with get_object_info(name) for its local
          transform, world_bounding_box (mesh objects only), dimensions, and modifiers - one
          call per object, so only fetch world_bounding_box for objects where spatial
          relationship or clipping actually matters for this task, not for every object in
          the scene by default.
        - For a mesh edit that needs vertex/edge/face indices, call get_mesh_data(name) (also
          paginated) to get current indices before building the edit.

    3. Use dedicated, validated tools for every operation.
        - Primitives: create_primitive_object() (cube, sphere, cylinder, cone, torus, plane,
          curve; purpose="blockout" for placeholder proxies).
        - Direct mesh edits: mesh_extrude(), mesh_inset(), mesh_bevel(), mesh_bridge(),
          mesh_boolean(), mesh_subdivide(), mesh_remesh(), mesh_solidify(), mesh_symmetrize().
        - Higher-level modeling: copy_object_transform(); manage_modifiers() for
          Mirror/Array/Subdivision Surface/Displace/etc. (ADD/PATCH/MOVE/REMOVE/APPLY on any
          allowlisted modifier type); add_radial_array_modifier() for a pivot-driven radial
          array (it manages a helper empty, so it's not covered by manage_modifiers()).
        - Cleanup/data: clear_materials(), clear_vertex_groups(), clear_edge_marks(),
          sync_data_name().
        - Viewport: set_viewport_overlay() for native overlays (cavity, wireframe, face
          orientation).
        - Non-destructive hard-surface work (utility booleans, ID materials, LOD naming): the
          ND tools - nd_boolean(), nd_mark_as_util(), nd_clean_utils(),
          nd_create_id_material(), nd_bulk_create_id_materials(), nd_set_lod_suffix(),
          nd_single_vertex(), nd_apply_modifiers(), nd_pulse_viewport_toggle(),
          nd_capture_utils() - only after confirming get_integration_status(provider="nd").
        - Asset/material/HDRI needs, only after confirming the provider is enabled: PolyHaven's
          import_polyhaven_asset() (asset_type="models"/"textures"/"hdris") and
          apply_polyhaven_texture(); Sketchfab's search_sketchfab_models() then
          import_sketchfab_model(uid). For a specific existing real-world object, try
          Sketchfab first, then PolyHaven; for generic objects/furniture, try PolyHaven first;
          for lighting, use PolyHaven HDRIs.
        - Modifier tools take apply: bool. apply=False (default) keeps a live, reversible
          modifier - prefer this. apply=True bakes it into the mesh: irreversible from this
          server's perspective and it invalidates any vertex/edge/face indices you fetched
          earlier (see stage 4).
        - If no dedicated tool covers an operation, report the missing capability instead of
          executing arbitrary Python in Blender.

    4. Re-query after anything that changes topology.
        - mesh_extrude/inset/bevel/bridge/boolean/subdivide/remesh/symmetrize, and any call
          made with apply=True, invalidate previously-fetched vertex/edge/face indices. If the
          tool's response includes a warning about this, call get_mesh_data(name) again before
          reusing indices in a further edit - do not reuse stale indices.
        - get_mesh_data coordinates are local/object-space (modifiers not evaluated);
          get_object_info's world_bounding_box is world-space. These are not directly
          comparable for a parented or transformed object, and there is no matrix_world field
          to convert between them yourself.

    5. Verify with structured state, not just a screenshot.
        - A screenshot (get_viewport_screenshot()) is useful for placement, lighting, and
          silhouette, but it cannot confirm topology, units, hierarchy, modifiers, or
          materials. Pair any visual check with list_scene_objects()/get_object_info()/
          get_mesh_data() to confirm the things a screenshot can't show.
        - Use a screenshot before/after a visually-meaningful change (placement, deformation,
          lighting, material) - not as a substitute for the structured checks above.
        - A screenshot is the live viewport, not a render, and render_scene itself returns
          only the written file's path/size/status, not pixels. To actually see rendered
          pixels: for the real scene's final render, call
          inspect_render_output(output_path=<render_scene's "last_file">)
          afterward (or with no arguments, to read the in-memory Render Result); for a
          bounded preview render, use render_lighting_preview or render_pbr_material_preview
          instead (a disposable staging scene, not the real one).

    6. Stop-and-check gates - do not continue past these without addressing them:
        - "ok": false means the request reached Blender but nothing changed - this includes an
          ND operator the user cancelled (Esc): "error" stays null, "changed_objects" is
          empty, and the scene is unchanged. Don't retry the same call expecting a different
          result; tell the user or pick a different approach.
        - Any non-empty "warnings" list - read it before doing anything else with that result.
        - A raised tool error means Blender rejected the input (bad name, invalid value) - fix
          the input, don't repeat the same call.
        - "truncated": true in a paginated result means you haven't seen everything - page
          through with next_offset before concluding the scene doesn't contain something.
        - A capability mismatch surfaced in stage 1.

    When reporting completion, state what actually changed - "changed_objects" and
    "changed_resources" from the tool responses, not just "done" - and disclose any
    irreversible action taken (apply=True, a cleanup tool) plus any limitation you hit
    (e.g. couldn't get world-space mesh coordinates, a provider was disabled).
    """
