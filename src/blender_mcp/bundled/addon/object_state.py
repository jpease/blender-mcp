"""
Capture and restore the pre-mutation state of the objects a handler touches.

The mutation transaction removes datablocks a failed request *created*; this
module is its counterpart for existing objects, so a failed request can also
undo the changes it made to objects that were already in the scene.

What is captured/restored per target object (all keyed by the object's stable
`session_uid`, never its name):

- object name and its data-block name;
- local transform (`matrix_basis`, so it is rotation-mode agnostic);
- parent, parent type/bone, and `matrix_parent_inverse`;
- collection membership;
- material-slot material assignments;
- the set of modifier names present before the request - restore removes any
  modifier added during a failed request. Handlers only ever *add* modifiers
  (model_*/add_*_modifier), so there is nothing to re-add;
- optionally, a detached backup copy of the mesh (`capture_geometry=True`),
  swapped back on failure for the mesh-editing commands.

Deliberately NOT covered (documented limitations, not silent gaps): resurrecting
deleted datablocks and applied modifiers (irreversible)
side effects, and object state a handler changes that is not one of the fields
above. A mesh restored from a geometry backup gets a fresh `session_uid` - an
accepted cost on the exceptional rollback path.
"""

import contextlib


class ObjectState:
    """Pre-mutation snapshot of one existing object's restorable state."""

    def __init__(self, obj, *, capture_geometry) -> None:
        self.obj = obj
        self.name = obj.name
        self.data_name = obj.data.name if obj.data is not None else None
        # matrix_basis is the object's own local transform, independent of
        # rotation_mode - copy it so later mutation can't alias our snapshot.
        self.matrix_basis = obj.matrix_basis.copy()
        self.parent = obj.parent
        self.parent_type = getattr(obj, "parent_type", "OBJECT")
        self.parent_bone = getattr(obj, "parent_bone", "")
        self.matrix_parent_inverse = obj.matrix_parent_inverse.copy()
        self.collections = list(getattr(obj, "users_collection", ()))
        self.materials = [slot.material for slot in obj.material_slots]
        self.modifier_names = {mod.name for mod in obj.modifiers}
        # A detached copy of the mesh (no scene users) kept only as a rollback
        # source. None when this command doesn't edit geometry.
        self.geometry_backup = None
        self.geometry_collection_name = {
            "CURVE": "curves",
            "MESH": "meshes",
            "POINTCLOUD": "pointclouds",
            "GREASEPENCIL": "grease_pencils",
        }.get(getattr(obj, "type", "MESH"), "meshes")
        if capture_geometry and obj.data is not None:
            self.geometry_backup = obj.data.copy()

    def restore(self) -> None:
        """
        Best-effort restore of this object's captured state.

        Each field is restored in isolation so one failure (e.g. a material
        that was itself rolled back and removed) doesn't abort the rest of the
        restore.
        """
        obj = self.obj

        if self.geometry_backup is not None:
            self._restore_geometry()

        with contextlib.suppress(Exception):
            obj.name = self.name
        with contextlib.suppress(Exception):
            if obj.data is not None and self.data_name is not None:
                obj.data.name = self.data_name
        with contextlib.suppress(Exception):
            obj.matrix_basis = self.matrix_basis
        with contextlib.suppress(Exception):
            obj.parent = self.parent
            if hasattr(obj, "parent_type"):
                obj.parent_type = self.parent_type
            if hasattr(obj, "parent_bone"):
                obj.parent_bone = self.parent_bone
            obj.matrix_parent_inverse = self.matrix_parent_inverse
        self._restore_collections()
        self._restore_materials()
        self._remove_added_modifiers()

    def _restore_geometry(self) -> None:
        """
        Swap the pristine backup mesh back into the object.

        The mutated mesh is removed and the backup takes its place, then the
        backup is renamed to the original data name (freed by the removal).
        Only this object's `data` pointer moves - other users of a shared mesh
        are left as-is, which matches the fact that the edit targeted this
        object. Guarded end to end so a partial swap can't leave the object
        without usable data.
        """
        obj = self.obj
        backup = self.geometry_backup
        with contextlib.suppress(Exception):
            import bpy

            mutated = obj.data
            obj.data = backup
            if mutated is not None and mutated is not backup:
                collection = getattr(bpy.data, self.geometry_collection_name, None)
                if collection is not None:
                    collection.remove(mutated, do_unlink=True)
            if self.data_name is not None:
                backup.name = self.data_name
        # The backup is now the live mesh (or the swap failed); either way it
        # must not be discarded as an unused backup afterwards.
        self.geometry_backup = None

    def _restore_materials(self) -> None:
        obj = self.obj
        slots = obj.material_slots
        if len(slots) != len(self.materials):
            # Slot count changed (slots added/removed) - reassigning by index
            # could bind the wrong material, so leave slots alone rather than
            # corrupt them.
            return
        for slot, material in zip(slots, self.materials, strict=False):
            with contextlib.suppress(Exception):
                slot.material = material

    def _restore_collections(self) -> None:
        """Restore exact object-to-collection links for collection batch rollback."""
        obj = self.obj
        for collection in list(getattr(obj, "users_collection", ())):
            if collection not in self.collections:
                with contextlib.suppress(Exception):
                    collection.objects.unlink(obj)
        for collection in self.collections:
            with contextlib.suppress(Exception):
                if collection.objects.get(obj.name) is None:
                    collection.objects.link(obj)

    def _remove_added_modifiers(self) -> None:
        obj = self.obj
        added = [mod for mod in obj.modifiers if mod.name not in self.modifier_names]
        for mod in added:
            with contextlib.suppress(Exception):
                obj.modifiers.remove(mod)

    def invalidate(self) -> None:
        """
        Release every datablock reference after the database was replaced.

        A load or library reload may have freed each of these, so nothing here
        reads or removes them: `remove()` on a freed geometry backup is a
        use-after-free that the `suppress(Exception)` around the other removal
        paths would hide. A backup that survives (a library reload leaves local
        session_uids unchanged, measured by
        `scripts/blender_probes/transaction_library_rollback.py` case B) is
        presumably left as an unused orphan - inferred, not measured: that case
        holds no geometry backup. A leak is recoverable; a wrong removal is not.
        """
        self.obj = None
        self.parent = None
        self.collections = []
        self.materials = []
        self.geometry_backup = None

    def discard_backup(self) -> None:
        """Drop the geometry backup on the success path (nothing to restore)."""
        if self.geometry_backup is None:
            return
        with contextlib.suppress(Exception):
            import bpy

            collection = getattr(bpy.data, self.geometry_collection_name, None)
            if collection is not None:
                collection.remove(self.geometry_backup, do_unlink=True)
        self.geometry_backup = None


def capture_object_states(objects, *, capture_geometry):
    """
    Snapshot each object's restorable state before a mutating request runs.

    Args:
        objects: Existing objects the request declares it will touch.
        capture_geometry: When True, also back up each object's mesh data so a
            failed geometry edit can be swapped back.

    Returns:
        list[ObjectState]: One snapshot per object, in the given order.

    """
    return [ObjectState(obj, capture_geometry=capture_geometry) for obj in objects]


def restore_object_states(states) -> None:
    """
    Restore captured object state after a failed request.

    Args:
        states: Snapshots from capture_object_states().

    """
    for state in states:
        state.restore()


def backup_datablock_ids(states):
    """
    Session UIDs of the geometry-backup meshes created during capture.

    The transaction excludes these from its "newly created" removal set: they
    are our own rollback scaffolding, not datablocks the handler created.

    Args:
        states: Snapshots from capture_object_states().

    Returns:
        set[int]: session_uid of each live geometry backup.

    """
    ids = set()
    for state in states:
        backup = state.geometry_backup
        if backup is None:
            continue
        uid = getattr(backup, "session_uid", None)
        if uid is not None:
            ids.add(uid)
    return ids


def invalidate_object_states(states: "list[ObjectState]") -> None:
    """
    Release the datablock references held by each snapshot, without touching bpy.

    Args:
        states: Snapshots from capture_object_states().

    """
    for state in states:
        state.invalidate()


def discard_backups(states) -> None:
    """
    Drop all geometry backups on the success path.

    Args:
        states: Snapshots from capture_object_states().

    """
    for state in states:
        state.discard_backup()
