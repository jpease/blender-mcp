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

    6. When two characters must touch, solve the contact, don't hand-build it.
        - Read the real positions first: get_character_rig_info(armature_object_name=...,
          bone_names=["Hand.R", "Shoulder.R", ...]) returns each named pose bone's world-space
          matrix, so you never have to guess FK rotations or chain forward from bone lengths.
          The bone_names filter is there so naming three bones off a 187-bone rig costs one
          call per character, not a paginated crawl.
        - Pick ONE world point for the contact and give that same point to both characters.
          Two separately-eyeballed points are two different points, and the hands will miss.
        - Drive each character to it with solve_bone_reach(armature_object_name=...,
          reaches=[{"tip_bone": <wrist-class bone>, "target": <the shared point>}]) - one call
          per armature. tip_bone is the bone whose POSITION must be exact, so it is the wrist,
          not the hand or a fingertip; pose the hand's own orientation and grip separately with
          set_character_pose. Check each reach's converged/out_of_reach before moving on.
        - Watch the geometry that catches everyone: two characters facing each other along one
          axis who each reach straight forward do not meet - their right shoulders sit on
          opposite sides of the line between them, so the hands pass by roughly the shoulder
          offset apart. Put the shared point where both arms can actually reach it (typically
          offset toward each character's reaching side), rather than nudging rotations until it
          looks closer.

    7. Stop-and-check gates - do not continue past these without addressing them:
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


@mcp.prompt()
def character_animation_strategy() -> str:
    """
    Define the staged workflow agents should follow when animating a character.

    Returns:
        str: Result produced by the operation.

    """
    return """Animate a character in these stages. A walk cycle authored by guessing FK rotations
    comes out stiff, with straight knees and a planted foot that slides - each of those is a
    specific tool being skipped, not a matter of taste.

    1. Read the rig before posing it.
        - list_character_bones(armature_object_name=..., bone_names=[...], rest_axes=True) for
          the bones you intend to drive. Which way a bone's local X/Y/Z point is rig-specific
          and not guessable from its name, so take the up_axis each bone reports and the
          reply's length_axis straight into aim_at. Do not work either out from rest_axes: a
          head aimed with a hand-derived up axis shipped 90 degrees over.
        - get_character_rig_info(armature_object_name=..., bone_names=[...]) for world-space
          pose-bone matrices. Never chain FK forward from bone lengths by hand.

    2. Key the body first, the feet second, into ONE action.
        - Root and hip travel: keyframe_object_transform(action_name="<shot>") and
          keyframe_character_pose(action_name="<shot>"). Give both the same action_name: an ID
          holds one action, so a pose keyed into a second one stops the first driving the rig.
        - Do this BEFORE the feet. keyframe_bone_reach solves each frame against the evaluated
          parent pose at that frame, so the hips must already be travelling when it runs.

    3. Plant the feet with keyframe_bone_reach, not with repeated FK rotation.
        - One entry per foot, with the SAME world target repeated across every contact frame,
          and targets that move across the swing frames. A foot slides because its target moved
          during contact - interpolation cannot fix that, and no amount of easing will.
        - The foot stays put while the hips travel over it because the IK re-solves the leg at
          each frame. That is the whole trick.

    4. Bend the knee on purpose.
        - Pass a pole_target roughly one leg-length in front of the knee. A straight-legged rest
          pose makes automatic pole inference refuse, by design - there is no bend to infer from.
        - Pass a hinge on the shin bone (axis="X", min_degrees=0, max_degrees=150 or whatever
          that rig's knee axis is) so the solver cannot invert the joint. The limit is temporary
          and removed before the call returns.
        - Check converged on every frame of every reach reply before moving on. out_of_reach
          means no pose of that chain reaches that point: move the target, do not raise
          iterations.

    5. Break the machine feel deliberately.
        - Do not key every bone on the same frames. Offset the spine, arms and head two to four
          frames after the hips; simultaneous keys on everything is what reads as robotic.
        - interpolation="SINE" with easing="EASE_IN_OUT" on weight shifts.
        - handle_left="VECTOR"/handle_right="VECTOR" on a contact key, so the foot does not ease
          through the floor on its way in.

    6. Loop it.
        - set_action_cycle(..., mode_after="REPEAT_OFFSET") so each repeat starts where the last
          ended and the character keeps travelling. Plain REPEAT teleports it back to the origin.
        - data_path_prefix scopes the cycle to one limb or to the root's travel alone.

    7. Verify by looking, frame by frame.
        - set_scene_frame(frame=N) then get_viewport_screenshot. Every inspection tool reports
          the current frame, so without this you are looking at frame 1 and nothing else.
        - At minimum check the contact, down, passing and up frames of each step.
        - Pair the screenshot with get_character_rig_info at that frame: a screenshot cannot
          tell you a foot moved by three millimetres, and world positions can.
    """
