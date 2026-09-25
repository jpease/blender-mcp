import bpy

from ..helpers import (
    MAX_LISTED_NAMES,
    count_by_type,
    counted_page,
    exit_edit_mode,
    get_mesh_object,
    mesh_counts,
    nd_call,
    nd_configure_object_as_util,
    nd_view3d_override,
    preserve_mode_and_selection,
    record_page,
    select_objects,
)


class NDHandlersMixin:
    """Provide HugeMenace non-destructive workflow handlers."""

    # region ND (HugeMenace) non-destructive workflow tools
    def nd_boolean(self, object_name, cutter_object_name, mode="DIFFERENCE"):
        """
        ND non-destructive boolean: live Boolean modifier on object_name, cutter_object_name becomes a wireframe
        utility parented to it.

        Args:
            object_name: Name of the Blender object to operate on.
            cutter_object_name: Name of the cutter object.
            mode: Value for mode.

        Returns:
            Result produced by the operation.

        Raises:
            ValueError: If the operation cannot be completed.

        """
        mode = str(mode).upper()
        if mode not in {"UNION", "DIFFERENCE", "INTERSECT"}:
            raise ValueError(f"Invalid mode: {mode}. Must be one of UNION, DIFFERENCE, INTERSECT")
        if object_name == cutter_object_name:
            raise ValueError(f"cutter_object_name must differ from object_name (both are '{object_name}')")
        target = get_mesh_object(object_name)
        cutter = get_mesh_object(cutter_object_name)
        with preserve_mode_and_selection(), nd_view3d_override():
            select_objects([cutter.name, target.name], active_name=target.name)
            # execute() reads attributes bool_vanilla only sets inside invoke(); INVOKE_DEFAULT's
            # synthetic event always has shift/alt False, so this always converts+cleans the cutter.
            _result, cancelled = nd_call("bool_vanilla", "INVOKE_DEFAULT", mode=mode)
        return {"name": target.name, "cutter_name": cutter.name, "cancelled": cancelled, **mesh_counts(target)}

    def nd_mark_as_util(self, object_names, unmark=False, parent_to=None):
        """
        Mark/unmark objects as ND utility objects (wireframe display, hidden from render).

        When parent_to is given (mark path only), also reparents each object to it while
        preserving world transform - matrix_parent_inverse is recomputed so the object doesn't
        visually jump - replicating the parenting half of ND's real mark_as_util operator. The
        keyboard-modifier-driven behaviors of the real operator (Ctrl-revert, Alt-skip-parenting,
        Shift-recursive-children) have no scriptable equivalent and are not replicated; unmark=True
        already covers reverting the visibility/display properties.

        Args:
            object_names: Names of Blender objects to operate on.
            unmark: Value for unmark.
            parent_to: Name of an object to reparent each marked object to, preserving world transform.

        Returns:
            Result produced by the operation.

        Raises:
            ValueError: If the operation cannot be completed.

        """
        if not object_names:
            raise ValueError("At least one object name is required")
        if parent_to is not None and unmark:
            raise ValueError("parent_to cannot be combined with unmark=True")
        parent_obj = None
        if parent_to is not None:
            parent_obj = bpy.data.objects.get(parent_to)
            if not parent_obj:
                raise ValueError(f"Object not found: {parent_to}")
        objs = []
        for name in object_names:
            obj = bpy.data.objects.get(name)
            if not obj:
                raise ValueError(f"Object not found: {name}")
            objs.append(obj)
        for obj in objs:
            nd_configure_object_as_util(obj, util=not unmark)
            if parent_obj is not None:
                world_matrix = obj.matrix_world.copy()
                obj.parent = parent_obj
                obj.matrix_parent_inverse = parent_obj.matrix_world.inverted()
                obj.matrix_world = world_matrix
        return {
            "names": [obj.name for obj in objs],
            "marked_as_util": not unmark,
            "parent": parent_obj.name if parent_obj else None,
        }

    def nd_clean_utils(self, confirm=False):
        """
        Remove orphaned boolean/array/mirror/lattice modifiers and their ND utility objects, scene-wide.

        Reports exactly what was removed by diffing bpy.data.objects (and each
        surviving object's modifiers) before and after the call - a true
        dry-run isn't feasible without reimplementing ND's own cleanup logic.

        Args:
            confirm: Must be True to run - this is scene-wide and destructive with no way to scope it.

        Returns:
            dict: `removed_objects` (the deleted utility objects, counted by type beside a sample
            of names), `removed_modifiers` (counted by modifier type beside a sample of
            `{object, modifier, type}` records), and `changed_objects`: the surviving objects
            that lost a modifier, which are what the caller inspects next.

        Raises:
            ValueError: If the operation cannot be completed.

        """
        if not confirm:
            raise ValueError(
                "Pass confirm=True to run nd_clean_utils - it removes orphaned ND utility objects/modifiers "
                "scene-wide with no way to scope or preview the change"
            )
        before_types = {obj.name: obj.type for obj in bpy.data.objects}
        before_modifiers = {obj.name: [(mod.name, mod.type) for mod in obj.modifiers] for obj in bpy.data.objects}
        with preserve_mode_and_selection(), nd_view3d_override():
            _result, cancelled = nd_call("clean_utils", "INVOKE_DEFAULT")
        after_objects = {obj.name for obj in bpy.data.objects}
        removed_objects = sorted(before_types.keys() - after_objects)
        removed_modifiers = []
        for name, mods in before_modifiers.items():
            obj = bpy.data.objects.get(name)
            if obj is None:
                continue
            after_mods = {(mod.name, mod.type) for mod in obj.modifiers}
            for mod_name, mod_type in mods:
                if (mod_name, mod_type) not in after_mods:
                    removed_modifiers.append({"object": name, "modifier": mod_name, "type": mod_type})
        # A scene-wide cleanup can remove any number of either; both are counted, not listed.
        return {
            "status": "cleaned",
            "removed_objects": counted_page(removed_objects, type_of=before_types.__getitem__, name_of=str),
            # A modifier is named only together with its object, so the sample carries records.
            "removed_modifiers": {
                "total": len(removed_modifiers),
                "by_type": count_by_type(record["type"] for record in removed_modifiers),
                **record_page("records", removed_modifiers, dict, MAX_LISTED_NAMES),
            },
            "cancelled": cancelled,
            "changed_objects": sorted({record["object"] for record in removed_modifiers}),
        }

    def nd_create_id_material(self, object_names, material_name):
        """
        Create/assign an ND ID material to the given mesh/curve objects.

        Args:
            object_names: Names of Blender objects to operate on.
            material_name: Name of the material.

        Returns:
            Result produced by the operation.

        """
        with preserve_mode_and_selection():
            objs = select_objects(object_names)
            _result, cancelled = nd_call("create_id_material", material_name=material_name)
        return {"names": [obj.name for obj in objs], "material_name": material_name, "cancelled": cancelled}

    def nd_bulk_create_id_materials(self, object_names):
        """
        Assign a random distinct ND ID material to each given mesh/curve object.

        Reports exactly which materials were created by diffing bpy.data.materials
        before and after the call.

        Args:
            object_names: Names of Blender objects to operate on.

        Returns:
            Result produced by the operation.

        """
        before_materials = {mat.name for mat in bpy.data.materials}
        with preserve_mode_and_selection():
            objs = select_objects(object_names)
            _result, cancelled = nd_call("bulk_create_id_materials")
        material_names = sorted({mat.name for mat in bpy.data.materials} - before_materials)
        return {"names": [obj.name for obj in objs], "material_names": material_names, "cancelled": cancelled}

    def nd_set_lod_suffix(self, object_names, mode="HIGH"):
        """
        Suffix object (and data) names with _high or _low, replacing any existing LOD suffix.

        Args:
            object_names: Names of Blender objects to operate on.
            mode: Value for mode.

        Returns:
            Result produced by the operation.

        Raises:
            ValueError: If the operation cannot be completed.

        """
        mode = str(mode).upper()
        if mode not in {"HIGH", "LOW"}:
            raise ValueError(f"Invalid mode: {mode}. Must be one of HIGH, LOW")
        with preserve_mode_and_selection():
            objs = select_objects(object_names)
            _result, cancelled = nd_call("set_lod_suffix", mode=mode)
        return {"names": [obj.name for obj in objs], "cancelled": cancelled}

    def nd_single_vertex(self, location=(0, 0, 0)):
        """
        Create an ND single-vertex sketch object at location, left in Object mode.

        Identifies the created object by diffing bpy.data.objects before/after the
        operator call rather than reading the active object - a cancelled call (or
        one that unexpectedly creates none/more than one object) would otherwise
        report a stale pre-existing active object, or raise if nothing was active.

        Args:
            location: World-space location.

        Returns:
            Result produced by the operation; "name"/"location" are None if the
            operator was cancelled or didn't create exactly one new object.

        """
        prev_cursor = tuple(bpy.context.scene.cursor.location)
        bpy.context.scene.cursor.location = tuple(location)
        before_names = {obj.name for obj in bpy.data.objects}
        try:
            with preserve_mode_and_selection():
                try:
                    _result, cancelled = nd_call("single_vertex")
                finally:
                    if bpy.context.mode != "OBJECT":
                        exit_edit_mode()
                created_names = {obj.name for obj in bpy.data.objects} - before_names
        finally:
            bpy.context.scene.cursor.location = prev_cursor
        if len(created_names) != 1:
            return {"name": None, "location": None, "cancelled": cancelled}
        obj = bpy.data.objects[next(iter(created_names))]
        return {
            "name": obj.name,
            "location": [obj.location.x, obj.location.y, obj.location.z],
            "cancelled": cancelled,
        }

    def nd_apply_modifiers(self, object_names):
        """
        Apply modifiers on the given objects via ND (always REGULAR mode - SOFT/HARD/duplicate need real modifier
        keys, unreachable from a script).

        Args:
            object_names: Names of Blender objects to operate on.

        Returns:
            Result produced by the operation.

        """
        with preserve_mode_and_selection(), nd_view3d_override():
            objs = select_objects(object_names)
            _result, cancelled = nd_call("apply_modifiers", "INVOKE_DEFAULT")
        return {"names": [obj.name for obj in objs], "cancelled": cancelled}

    _ND_PULSE_TOGGLES = {
        "CLEAR_VIEW": "toggle_clear_view",
        "CUSTOM_VIEW": "toggle_custom_view",
        "UTILS": "toggle_utils",
    }

    def nd_pulse_viewport_toggle(self, toggle):
        """
        Pulse an ND viewport toggle that has no readable on/off state of its own.

        ND exposes no readable state for CLEAR_VIEW, CUSTOM_VIEW, or UTILS in what's vendored
        here, so each call just flips ND's internal toggle operator - it is NOT guaranteed
        idempotent. ND's SILHOUETTE toggle is a genuine modal operator and is intentionally not
        exposed here. For the native Blender viewport overlays (cavity, wireframes, face
        orientation), use set_viewport_overlay instead - those are true idempotent setters.

        Args:
            toggle: One of CLEAR_VIEW, CUSTOM_VIEW, UTILS.

        Returns:
            Result produced by the operation.

        Raises:
            ValueError: If the operation cannot be completed.

        """
        toggle = str(toggle).upper()
        op_name = self._ND_PULSE_TOGGLES.get(toggle)
        if op_name is None:
            raise ValueError(f"Invalid toggle: {toggle}. Must be one of {sorted(self._ND_PULSE_TOGGLES)}")
        with nd_view3d_override():
            _result, cancelled = nd_call(op_name)
        return {"toggle": toggle, "cancelled": cancelled}

    def nd_capture_utils(self):
        """
        Display and select all ND utility objects in the scene.

        Returns:
            Result produced by the operation.

        """
        if bpy.context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        with nd_view3d_override():
            _result, cancelled = nd_call("capture_utils", "INVOKE_DEFAULT")
        return {"status": "captured", "cancelled": cancelled}

    def get_nd_status(self):
        """
        Get the current status of the ND (HugeMenace) non-destructive workflow integration.

        Returns:
            Result produced by the operation.

        """
        enabled = bpy.context.scene.blendermcp_use_nd
        nd_installed = hasattr(bpy.ops, "nd") and hasattr(bpy.ops.nd, "bool_vanilla")
        if not enabled:
            return {
                "enabled": False,
                "message": """ND integration is currently disabled. To enable it:
                            1. In the 3D Viewport, find the BlenderMCP panel in the sidebar (press N if hidden)
                            2. Check the 'Use ND (non-destructive hard-surface tools)' checkbox
                            3. Restart the connection to Claude""",
            }
        if not nd_installed:
            return {
                "enabled": False,
                "message": "ND integration is enabled in BlenderMCP, but the ND addon "
                "(https://extensions.blender.org/add-ons/nd/) does not appear to be "
                "installed/enabled in this Blender instance.",
            }
        return {
            "enabled": True,
            "message": "ND integration is enabled and the ND addon is installed and ready to use.",
        }

    # endregion
