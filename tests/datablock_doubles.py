"""
Stand-ins for `bpy.data` datablocks and collections, shared by the suites that drive the add-on.

`FakeCollection` is identity-based, as Blender's collections are; `TRACKED_COLLECTIONS`
is every collection `transaction.py` snapshots; `replace_whole_database` and
`reload_library_contents` model the two events that hand every affected datablock a
fresh `session_uid`.
"""

import contextlib
import itertools

_UID = itertools.count(1)


def _next_uid():
    return next(_UID)


class FakeMatrix:
    """Stand-in for a mathutils.Matrix: only .copy()/.inverted() are exercised."""

    def __init__(self, value=(0.0, 0.0, 0.0)) -> None:
        self.value = value

    def copy(self):
        return FakeMatrix(self.value)

    def inverted(self):
        return FakeMatrix(self.value)


class FakeDatablock:
    """
    Stand-in for a bpy ID.

    `users`, `use_fake_user` and `library` exist on every real ID, so they exist
    here: production reads them with a `getattr` default, and a fake missing them
    would pass the persistence checks for the wrong reason.
    """

    def __init__(self, name) -> None:
        self.name = name
        self.session_uid = _next_uid()
        self.users = 1
        self.use_fake_user = False
        self.library = None


class FakeMesh:
    """Mesh datablock whose .copy() registers a fresh backup in its collection."""

    def __init__(self, name, meshes) -> None:
        self.name = name
        self.session_uid = _next_uid()
        self._meshes = meshes

    def copy(self):
        clone = FakeMesh(f"{self.name}.backup", self._meshes)
        self._meshes[clone.name] = clone
        return clone


class FakeCollection:
    """
    Stand-in for a bpy.data.* collection.

    Real Blender collections are identity-based: renaming a datablock and then
    allocating a new one under the freed name leaves *both* present. A
    name-keyed dict would clobber the renamed entry, hiding exactly the bug the
    identity-tracking fix guards against - so this holds items in a list and
    resolves lookups by each datablock's current .name.
    """

    def __init__(self) -> None:
        self._items = []

    def __setitem__(self, _name, db) -> None:
        # Setup convenience (tests seed collections with objects[name] = obj);
        # mirrors Blender allocating a datablock, so it just appends.
        self._items.append(db)

    def __getitem__(self, name):
        for db in self._items:
            if db.name == name:
                return db
        raise KeyError(name)

    def get(self, name, default=None):
        for db in self._items:
            if db.name == name:
                return db
        return default

    def new(self, name, *_args, **_kwargs):
        db = FakeDatablock(name)
        self._items.append(db)
        return db

    def remove(self, db, do_unlink=True) -> None:
        for index, existing in enumerate(self._items):
            if existing is db:
                del self._items[index]
                return

    def __contains__(self, name) -> bool:
        return any(db.name == name for db in self._items)

    def __iter__(self):
        return iter(list(self._items))

    def values(self) -> list:
        """
        List the datablocks, as `bpy_prop_collection.values` does.

        Returns:
            list: Every datablock, in allocation order.

        """
        return list(self._items)

    def __len__(self) -> int:
        return len(self._items)


class FakeModifierStack(list):
    def remove(self, mod) -> None:
        with contextlib.suppress(ValueError):
            list.remove(self, mod)


class FakeMutableObject:
    """Object rich enough for object_state.ObjectState to capture and restore."""

    def __init__(self, name, *, mesh=None) -> None:
        self.name = name
        self.session_uid = _next_uid()
        self.data = mesh
        self.matrix_basis = FakeMatrix()
        self.parent = None
        self.matrix_parent_inverse = FakeMatrix()
        self.material_slots = []
        self.modifiers = FakeModifierStack()


# Every collection name transaction._TRACKED_COLLECTIONS iterates - a
# mutating-path test must stock all of these or _snapshot_ids() raises
# AttributeError, which is exactly how the read-only tests in
# test_mutation_transaction.py prove the wrapper was skipped: their bpy.data
# has none of these attributes at all.
TRACKED_COLLECTIONS = (
    "objects",
    "meshes",
    "curves",
    "materials",
    "textures",
    "images",
    "node_groups",
    "worlds",
    "actions",
    "armatures",
    "cameras",
    "lights",
    "collections",
)


# The datablocks each reproduction links from its stub library.
LINKED_FROM_CANON = (("objects", "HeroBody"), ("meshes", "HeroMesh"), ("collections", "CanonHero"))


class FakeLinkedDatablock(FakeDatablock):
    """A datablock linked from a library: `library` names the `Library` it came from."""

    def __init__(self, name: str, library: FakeDatablock) -> None:
        """
        Allocate the datablock with a fresh session_uid.

        Args:
            name: The datablock's name.
            library: The stub `Library` it is linked from.

        """
        super().__init__(name)
        self.library = library


def replace_whole_database(data: dict[str, FakeCollection]) -> dict[str, list[int]]:
    """
    Model a file load: every tracked datablock is freed and the new file's take their place.

    A load gives every datablock a fresh `session_uid`, even one whose name is
    unchanged.

    Args:
        data: The stub `bpy.data` collections, keyed by collection name.

    Returns:
        dict[str, list[int]]: The new file's session_uids, per collection.

    """
    loaded = {}
    for coll_name, collection in data.items():
        names = [db.name for db in collection] or [f"{coll_name}.from_new_file"]
        for db in list(collection):
            collection.remove(db)
        loaded[coll_name] = [collection.new(name).session_uid for name in names]
    return loaded


def reload_library_contents(data: dict[str, FakeCollection], library: FakeDatablock) -> list[int]:
    """
    Model `lib.reload()`: every datablock linked from `library` comes back with a fresh `session_uid`.

    Local datablocks and the `Library` itself keep theirs.

    Args:
        data: The stub `bpy.data` collections, keyed by collection name.
        library: The stub library being reloaded.

    Returns:
        list[int]: The reloaded datablocks' new session_uids.

    """
    reloaded = []
    for collection in data.values():
        for db in [db for db in collection if getattr(db, "library", None) is library]:
            collection.remove(db)
            fresh = FakeLinkedDatablock(db.name, library)
            collection[db.name] = fresh
            reloaded.append(fresh.session_uid)
    return reloaded
