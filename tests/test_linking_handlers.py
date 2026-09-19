"""
The linking commands against a stub `bpy` that copies Blender 5.2.2's behaviour.

The stub's `override_hierarchy_create` places the override beside each instance
(or at the scene root), makes system overrides unless `do_fully_editable=True`,
and returns None for a local or override collection. A raise inside
`libraries.load` creates no `Library`; `libraries.remove` frees the linked
datablocks and their override objects. The error strings are Blender's own.
"""

from __future__ import annotations

import ast
import inspect
import itertools
import os
import shutil
import sys
import types

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from conftest import REPO_ROOT
from test_mutation_transaction import _load_addon

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "blend" / "empty_gzip.blend"
LINKING_SOURCE = REPO_ROOT / "src" / "blender_mcp" / "bundled" / "addon" / "handlers" / "linking.py"
LINKING_COMMANDS = (
    "link_canon_library",
    "create_override",
    "list_libraries",
    "reload_library",
    "relocate_library",
    "unlink_libraries",
)

# --- Blender 5.2.2's text for a failed reload ---
_RELOAD_WORK = "/var/folders/87/ykdcq2j525x7kkhl13f1lrm80000gn/T/t7_reload_4i12xuvn"
RELOAD_ABSOLUTE = f"Error: Trying to reload library 'LIcanon.blend' from invalid path '{_RELOAD_WORK}/gone.blend'\n"
RELOAD_RELATIVE_SMITH = (
    f"Error: Trying to reload library 'LIcanon.blend' from invalid path '{_RELOAD_WORK}/Smith, John/canon.blend'\n"
)
# --- Blender 5.2.2's text for a failed link (an OSError) ---
_LIFECYCLE_WORK = "/var/folders/87/ykdcq2j525x7kkhl13f1lrm80000gn/T/t7_lifecycle_g5s7jxf0"
LINK_TRUNCATED = f"Error: Failed to read blend file '{_LIFECYCLE_WORK}/truncated.blend': Missing DNA block\n"

_UID = itertools.count(50_000)
PAGE = 2
MANY = 150
LISTED_CAP = 100
NAME_CAP = 10
_COLLECTIONS = (
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
    "pointclouds",
    "volumes",
    "metaballs",
    "lattices",
    "grease_pencils",
    "libraries",
    "scenes",
)


# ---------------------------------------------------------------------------
# stub bpy
# ---------------------------------------------------------------------------


class StubOverride:
    """`ID.override_library`: what the override points back at."""

    def __init__(self, reference: StubID, hierarchy_root: StubID | None, *, system: bool) -> None:
        """
        Record the override's reference, its root and its system flag.

        Args:
            reference: The linked datablock this overrides.
            hierarchy_root: The root override of the hierarchy.
            system: `is_system_override`.

        """
        self.reference = reference
        self.hierarchy_root = hierarchy_root
        self.is_system_override = system


class StubID:
    """A datablock: identity is `session_uid`, never `name`."""

    def __init__(self, world: World, name: str, id_type: str, *, library: StubLibrary | None = None) -> None:
        """
        Allocate a datablock with a fresh session_uid.

        Args:
            world: The stub database.
            name: The datablock name.
            id_type: Blender's `ID.id_type`.
            library: The library it is linked from, if any.

        """
        self.world = world
        self.name = name
        self.id_type = id_type
        self.session_uid = next(_UID)
        self.library = library
        self.override_library: StubOverride | None = None
        self.users = 1
        self.use_fake_user = False
        self.is_library_indirect = False
        self.is_editable = library is None
        self.freed = False
        self.is_missing = False
        self.uses: list[StubID] = []


class StubChildren(list):
    """`Collection.children` / `.objects`: identity membership, `link` raises on a duplicate."""

    def link(self, item: StubID) -> None:
        """
        Add an item, as Blender does.

        Args:
            item: The datablock.

        Raises:
            RuntimeError: When it is already a member, as Blender raises.

        """
        if any(existing is item for existing in self):
            raise RuntimeError("Collection already in collection")
        self.append(item)

    def unlink(self, item: StubID) -> None:
        """
        Remove an item by identity.

        Args:
            item: The datablock.

        """
        for index, existing in enumerate(self):
            if existing is item:
                del self[index]
                return


class StubCollectionID(StubID):
    """`bpy.types.Collection`, with Route C's measured `override_hierarchy_create`."""

    def __init__(self, world: World, name: str, *, library: StubLibrary | None = None) -> None:
        """
        Allocate an empty collection.

        Args:
            world: The stub database.
            name: Its name.
            library: The library it is linked from, if any.

        """
        super().__init__(world, name, "COLLECTION", library=library)
        self.children = StubChildren()
        self.objects = StubChildren()

    @property
    def all_objects(self) -> list[StubID]:
        """
        Every object in this collection and its children.

        Returns:
            list[StubID]: The objects.

        """
        return [*self.objects, *(obj for child in self.children for obj in child.all_objects)]

    @property
    def children_recursive(self) -> list[StubCollectionID]:
        """
        Every collection inside this one, at any depth.

        Returns:
            list[StubCollectionID]: The collections.

        """
        return [*self.children, *(inner for child in self.children for inner in child.children_recursive)]

    def override_hierarchy_create(self, scene: StubScene, view_layer: object, **kwargs: object) -> StubID | None:
        """
        Record the call and create the override hierarchy the way Blender does.

        Args:
            scene: The scene the call passed.
            view_layer: The view layer the call passed.
            **kwargs: Exactly the keyword arguments the handler passed.

        Returns:
            StubID | None: The override collection; None for a local or override collection.

        """
        self.world.override_calls.append((self, scene, view_layer, kwargs))
        if self.world.override_error is not None:
            raise self.world.override_error
        if self.library is None or self.world.override_returns_none or self.name in self.world.override_none_for:
            return None
        system = kwargs.get("do_fully_editable", False) is not True
        override = StubCollectionID(self.world, self.name)
        override.override_library = StubOverride(self, override, system=system)
        for obj in self.objects:
            copy = StubID(self.world, obj.name, "OBJECT")
            copy.override_library = StubOverride(obj, override, system=system)
            override.objects.link(copy)
            self.world.data["objects"].append(copy)
        self.world.data["collections"].append(override)
        # Blender also overrides every linked collection inside it.
        for child in [child for child in self.children if child.library is not None]:
            nested = StubCollectionID(self.world, child.name)
            nested.override_library = StubOverride(child, override, system=system)
            override.children.link(nested)
            self.world.data["collections"].append(nested)
        parents = [parent for parent in self.world.parents() if any(child is self for child in parent.children)]
        for parent in parents or [scene.collection]:
            parent.children.link(override)
        return override


class StubScene(StubID):
    """`bpy.types.Scene` with its master collection and one view layer."""

    def __init__(self, world: World, name: str) -> None:
        """
        Allocate a scene.

        Args:
            world: The stub database.
            name: Its name.

        """
        super().__init__(world, name, "SCENE")
        self.collection = StubCollectionID(world, "Scene Collection")
        self.view_layers = [types.SimpleNamespace(name="ViewLayer")]


class StubLibrary(StubID):
    """`bpy.types.Library`: `users_id` is read from the database, `reload()` churns uids."""

    def __init__(self, world: World, filepath: str) -> None:
        """
        Allocate a library for a file.

        Args:
            world: The stub database.
            filepath: `Library.filepath` as Blender stores it.

        """
        super().__init__(world, os.path.basename(filepath), "LIBRARY")
        self.filepath = filepath
        self.is_missing = False
        self.version = (5, 2, 44)
        self.needs_liboverride_resync = False
        self.parent = None
        self.reload_error: BaseException | None = None
        self.filepath_during_reload: list[str] = []

    @property
    def users_id(self) -> list[StubID]:
        """
        The datablocks linked from this library.

        Returns:
            list[StubID]: Every datablock whose `library` is this one.

        """
        return [db for name in _COLLECTIONS for db in self.world.data[name] if db.library is self]

    def reload(self) -> None:
        """Record the call; raise `reload_error` when set, or give every linked datablock a fresh session_uid."""
        self.world.reload_calls.append((self, self.world.bpy.transaction_flag()))
        self.filepath_during_reload.append(self.filepath)
        if self.reload_error is not None:
            raise self.reload_error
        contents = self.world.files.get(self.filepath, {"collections": {}, "objects": []})
        for datablock in self.users_id:
            datablock.session_uid = next(_UID)
            # A datablock the new file lacks stays as a placeholder with `ID.is_missing` True.
            datablock.is_missing = datablock.name not in {*contents["collections"], *contents["objects"]}


class StubData(list):
    """A `bpy.data` collection: identity-based, and `remove()` on a freed datablock raises."""

    def __init__(self, world: World) -> None:
        """
        Start empty.

        Args:
            world: The stub database.

        """
        super().__init__()
        self.world = world
        self.removed: list[StubID] = []

    def remove(self, datablock: StubID, do_unlink: bool = True) -> None:
        """
        Remove a datablock, as Blender does.

        Args:
            datablock: What to remove.
            do_unlink: Blender's flag; unused here.

        Raises:
            ReferenceError: For a datablock already freed, as Blender raises.

        """
        if datablock.freed:
            raise ReferenceError("StructRNA of type ID has been removed")
        self.removed.append(datablock)
        self.world.free(datablock)

    def new(self, name: str) -> StubID:
        """
        Allocate a local datablock.

        Args:
            name: Its name.

        Returns:
            StubID: The new datablock.

        """
        datablock = StubID(self.world, name, "MATERIAL")
        self.append(datablock)
        return datablock


class StubLoad:
    """The `libraries.load` context manager, with the measured failure behaviour."""

    def __init__(self, world: World, filepath: str, relative: bool) -> None:
        """
        Prepare a link from one file.

        Args:
            world: The stub database.
            filepath: The path the handler passed.
            relative: The `relative` flag the handler passed.

        """
        self.world = world
        self.filepath = filepath
        self.relative = relative
        contents = world.files.get(filepath, {"collections": {}, "objects": []})
        self.data_from = types.SimpleNamespace(
            collections=list(contents["collections"]), objects=list(contents["objects"])
        )
        self.data_to = types.SimpleNamespace(collections=[], objects=[])

    def __enter__(self) -> tuple[object, object]:
        """
        Open the file.

        Returns:
            tuple: `(data_from, data_to)`; raises the world's load error for this file first, when set.

        """
        if self.filepath in self.world.load_errors:
            raise self.world.load_errors[self.filepath]
        return self.data_from, self.data_to

    def __exit__(self, exc_type: object, *_rest: object) -> None:
        """
        Link what was requested; nothing at all when the block raised.

        Args:
            exc_type: The exception type leaving the block, if any.
            *_rest: The rest of the exception triple.

        """
        if exc_type is not None or not (self.data_to.collections or self.data_to.objects):
            return
        stored = f"//{os.path.basename(self.filepath)}" if self.relative else self.filepath
        library = next((lib for lib in self.world.data["libraries"] if lib.filepath == stored), None)
        if library is None:
            library = StubLibrary(self.world, stored)
            self.world.data["libraries"].append(library)
        contents = self.world.files[self.filepath]
        self.data_to.collections = [
            self.world.linked_collection(library, name, contents["collections"][name])
            if name in contents["collections"]
            else None
            for name in self.data_to.collections
        ]
        self.data_to.objects = [
            self.world.linked_object(library, name) if name in contents["objects"] else None
            for name in self.data_to.objects
        ]


class StubLibraries(StubData):
    """`bpy.data.libraries`: `load()` links, `remove()` frees what the library linked and its overrides."""

    def load(self, filepath: str, link: bool = False, relative: bool = False, **kwargs: object) -> StubLoad:
        """
        Record the call and return the link context manager.

        Args:
            filepath: The file to link from.
            link: Blender's `link` flag.
            relative: Blender's `relative` flag.
            **kwargs: Every other flag, recorded so a test can assert on its absence.

        Returns:
            StubLoad: The context manager.

        """
        self.world.load_calls.append((filepath, {"link": link, "relative": relative, **kwargs}))
        return StubLoad(self.world, filepath, relative)

    def remove(self, datablock: StubID, do_unlink: bool = True) -> None:
        """
        Free every datablock linked from the library and every override of one, then the library.

        Args:
            datablock: The library.
            do_unlink: Blender's flag. Raises the world's `remove_error` first, when set.

        """
        if self.world.remove_error is not None:
            raise self.world.remove_error
        if not datablock.freed:
            linked = datablock.users_id  # type: ignore[attr-defined]
            for item in linked:
                self.world.free(item)
            for name in ("objects",):
                for item in list(self.world.data[name]):
                    if item.override_library is not None and item.override_library.reference in linked:
                        self.world.free(item)
        super().remove(datablock, do_unlink)


class World:
    """The whole stub database, plus the files a link can read and the calls the handlers made."""

    def __init__(self, bpy: types.ModuleType) -> None:
        """
        Build an empty database with one scene.

        Args:
            bpy: The stub module this world backs.

        """
        self.bpy = bpy
        self.data: dict[str, StubData] = {name: StubData(self) for name in _COLLECTIONS}
        self.data["libraries"] = StubLibraries(self)
        self.files: dict[str, dict[str, Any]] = {}
        self.load_errors: dict[str, BaseException] = {}
        self.load_calls: list[tuple[str, dict[str, object]]] = []
        self.override_calls: list[tuple[object, object, object, dict[str, object]]] = []
        self.reload_calls: list[tuple[StubLibrary, bool]] = []
        self.override_error: BaseException | None = None
        self.override_returns_none = False
        self.override_none_for: set[str] = set()
        # Collection name -> names of the collections inside it, in every library file.
        self.nesting: dict[str, list[str]] = {}
        self.remove_error: BaseException | None = None
        self.batch_removed: list[StubID] = []
        self.data["scenes"].append(StubScene(self, "Scene"))

    def parents(self) -> list[StubCollectionID]:
        """
        Every collection that can hold an instance: scene roots and local collections.

        Returns:
            list[StubCollectionID]: The candidate parents.

        """
        roots = [scene.collection for scene in self.data["scenes"]]
        return [*roots, *(c for c in self.data["collections"] if c.library is None)]

    def free(self, datablock: StubID) -> None:
        """
        Remove a datablock from its collection and drop the users it held.

        Args:
            datablock: The datablock.

        """
        for collection in self.data.values():
            index = next((i for i, existing in enumerate(collection) if existing is datablock), None)
            if index is not None:
                del collection[index]
        for parent in self.parents():
            parent.children.unlink(datablock)
        for used in datablock.uses:
            used.users -= 1
        datablock.freed = True

    def linked_collection(self, library: StubLibrary, name: str, object_names: list[str]) -> StubCollectionID:
        """
        Return the linked collection, reusing one already linked from the same library.

        Args:
            library: Its library.
            name: The collection name.
            object_names: The objects inside it.

        Returns:
            StubCollectionID: The linked collection.

        """
        for existing in self.data["collections"]:
            if existing.library is library and existing.name == name:
                return existing  # type: ignore[return-value]
        collection = StubCollectionID(self, name, library=library)
        for object_name in object_names:
            collection.objects.link(self.linked_object(library, object_name))
        for child_name in self.nesting.get(name, []):
            contents = self.files[library.filepath]["collections"]
            collection.children.link(self.linked_collection(library, child_name, contents[child_name]))
        self.data["collections"].append(collection)
        return collection

    def linked_object(self, library: StubLibrary, name: str) -> StubID:
        """
        Return the linked object, reusing one already linked from the same library.

        Args:
            library: Its library.
            name: The object name.

        Returns:
            StubID: The linked object.

        """
        for existing in self.data["objects"]:
            if existing.library is library and existing.name == name:
                return existing
        obj = StubID(self, name, "OBJECT", library=library)
        self.data["objects"].append(obj)
        return obj

    def batch_remove(self, ids: list[StubID]) -> None:
        """
        `bpy.data.batch_remove`: free exactly the ids given.

        Args:
            ids: The datablocks.

        """
        for datablock in ids:
            self.batch_removed.append(datablock)
            self.free(datablock)

    @property
    def scene(self) -> StubScene:
        """
        The first scene.

        Returns:
            StubScene: It.

        """
        return self.data["scenes"][0]  # type: ignore[return-value]


class AllIds:
    """`bpy.data.all_ids`: every datablock in the database, again."""

    def __init__(self, world: World) -> None:
        """
        Read from the world.

        Args:
            world: The stub database.

        """
        self.world = world

    def __iter__(self) -> Iterator[StubID]:
        """
        Iterate every datablock in every collection.

        Returns:
            Iterator[StubID]: The datablocks.

        """
        return iter([db for collection in self.world.data.values() for db in collection])


class OperatorTripwire:
    """`bpy.ops.wm`: records every operator the handlers reach for, and refuses to run it."""

    def __init__(self) -> None:
        """Start with nothing touched."""
        self.touched: list[str] = []

    def __getattr__(self, name: str) -> object:
        """
        Record the operator name and fail the call.

        Args:
            name: The operator.

        Returns:
            object: A callable that raises.

        """
        self.touched.append(name)

        def refuse(**_kwargs: object) -> None:
            raise AssertionError(f"bpy.ops.wm.{name} was called")

        return refuse


def _abspath(bpy: types.ModuleType, path: str) -> str:
    """
    Mimic `bpy.path.abspath`, including the unsaved-session behaviour.

    Args:
        bpy: The stub module.
        path: The path.

    Returns:
        str: The expanded path.

    """
    if not path.startswith("//"):
        return path
    base = bpy.data.filepath
    return os.path.join(os.path.dirname(base), path[2:]) if base else path[2:]


def _server(monkeypatch: pytest.MonkeyPatch, *, filepath: str = "") -> tuple[object, types.ModuleType, World]:
    """
    Build a real server over the full addon, backed by the stub database.

    Args:
        monkeypatch: The test's monkeypatch.
        filepath: The open file; `""` for an unsaved session.

    Returns:
        tuple: The server, the stub `bpy`, and the world.

    """
    monkeypatch.delenv("BLENDERMCP_FILE_ROOTS", raising=False)
    monkeypatch.delenv("BLENDERMCP_OUTPUT_ROOTS", raising=False)
    addon, bpy = _load_addon(monkeypatch, data={})
    world = World(bpy)
    transaction = sys.modules[f"{addon.__name__}.transaction"]
    bpy.transaction_flag = transaction.library_replace_in_progress
    for name, collection in world.data.items():
        setattr(bpy.data, name, collection)
    bpy.data.filepath = filepath
    bpy.data.is_dirty = False
    bpy.data.batch_remove = world.batch_remove
    # `all_ids` repeats every datablock under fixed type `ID`, as in Blender.
    bpy.data.all_ids = AllIds(world)
    bpy.data.bl_rna = types.SimpleNamespace(
        properties=[
            types.SimpleNamespace(identifier="filepath", type="STRING", fixed_type=None),
            *(
                types.SimpleNamespace(
                    identifier=name, type="COLLECTION", fixed_type=types.SimpleNamespace(identifier="")
                )
                for name in _COLLECTIONS
            ),
            types.SimpleNamespace(
                identifier="all_ids", type="COLLECTION", fixed_type=types.SimpleNamespace(identifier="ID")
            ),
        ]
    )
    # A decoy: a handler that reads the scene from bpy.context overrides into the wrong one.
    decoy = StubScene(world, "ContextDecoy")
    for flag in ("blendermcp_use_polyhaven", "blendermcp_use_sketchfab", "blendermcp_use_nd"):
        setattr(decoy, flag, False)
    bpy.context.scene = decoy
    bpy.context.view_layer = types.SimpleNamespace(name="ContextDecoyLayer")
    bpy.path = types.SimpleNamespace(abspath=lambda path: _abspath(bpy, path))
    bpy.context.preferences = types.SimpleNamespace(
        filepaths=types.SimpleNamespace(use_scripts_auto_execute=False),
        edit=types.SimpleNamespace(use_global_undo=True),
    )
    bpy.ops.wm = OperatorTripwire()
    return addon.BlenderMCPServer(), bpy, world


def _run(server: object, cmd_type: str, **params: object) -> dict:
    """
    Dispatch a command through production's own `execute_command`.

    Args:
        server: The server.
        cmd_type: The command.
        **params: Its parameters.

    Returns:
        dict: The response envelope.

    """
    return server.execute_command({"type": cmd_type, "params": params})  # type: ignore[attr-defined]


def _canon(tmp_path: Path, world: World, name: str = "canon.blend") -> str:
    """
    Put a real `.blend` on disk and describe its contents to the stub.

    Args:
        tmp_path: Where to put it.
        world: The stub database.
        name: Its file name.

    Returns:
        str: Its canonical path, as the handler hands it to Blender.

    """
    target = tmp_path / name
    shutil.copyfile(FIXTURE, target)
    canonical = os.path.realpath(target)
    world.files[canonical] = {
        "collections": {"CanonHero": ["HeroBody"], "CanonProp": ["PropBody"]},
        "objects": ["HeroBody", "Crate"],
    }
    return canonical


def _linked(server: object, world: World, canonical: str) -> tuple[StubLibrary, StubCollectionID]:
    """
    Link `CanonHero` through the real handler and return its library and collection.

    Args:
        server: The server.
        world: The stub database.
        canonical: The library file.

    Returns:
        tuple: The library and the linked collection.

    """
    response = _run(server, "link_canon_library", filepath=canonical, collections=["CanonHero"])
    assert response["status"] == "success", response
    library = next(lib for lib in world.data["libraries"] if lib.filepath == canonical)
    collection = next(c for c in world.data["collections"] if c.library is library)
    return library, collection  # type: ignore[return-value]


def _uids(world: World) -> set[int]:
    """
    Every session_uid in the database.

    Args:
        world: The stub database.

    Returns:
        set[int]: The uids.

    """
    return {db.session_uid for collection in world.data.values() for db in collection}


def _assert_no_path(text: str, *fragments: str) -> None:
    """
    Assert that no absolute path, and no named layout fragment, reached the client.

    Args:
        text: The client-visible text.
        *fragments: Directory names that must not appear.

    """
    for marker in ("/var/", "/private/", "/Users/", "/tmp", "folders", *fragments):
        assert marker not in text, text


# ---------------------------------------------------------------------------
# link_canon_library
# ---------------------------------------------------------------------------


def test_link_validates_its_path_before_blender_reads_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A missing file, a non-`.blend` and an unsaved `//` path are refused before `libraries.load`."""
    server, _bpy, world = _server(monkeypatch)

    for path in (str(tmp_path / "missing.blend"), str(tmp_path), "//canon.blend"):
        response = _run(server, "link_canon_library", filepath=path, collections=["CanonHero"])
        assert response["status"] == "error", path
        _assert_no_path(response["message"])
    assert world.load_calls == []


def test_link_enforces_the_file_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A canon library outside `BLENDERMCP_FILE_ROOTS` is refused; inside is linked."""
    server, _bpy, world = _server(monkeypatch)
    inside, outside = tmp_path / "inside", tmp_path / "outside"
    inside.mkdir()
    outside.mkdir()
    allowed, refused = _canon(inside, world), _canon(outside, world)
    monkeypatch.setenv("BLENDERMCP_FILE_ROOTS", str(inside))

    assert _run(server, "link_canon_library", filepath=refused, collections=["CanonHero"])["status"] == "error"
    assert world.load_calls == []
    assert _run(server, "link_canon_library", filepath=allowed, collections=["CanonHero"])["status"] == "success"


def test_link_refuses_a_name_absent_from_the_file_and_leaves_no_library(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An absent name would still create a `Library`; refusing inside the block creates none."""
    server, _bpy, world = _server(monkeypatch)
    canonical = _canon(tmp_path, world)

    response = _run(server, "link_canon_library", filepath=canonical, collections=["CanonHero", "NoSuchHero"])

    assert response["status"] == "error"
    assert "NoSuchHero" in response["message"]
    assert len(world.load_calls) == 1
    assert list(world.data["libraries"]) == []
    assert _uids(world) == {world.scene.session_uid}


def test_a_link_that_fails_after_linking_rolls_its_library_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Through the real handler and transaction, nothing the failed request created survives."""
    server, _bpy, world = _server(monkeypatch)
    canonical = _canon(tmp_path, world)
    before = _uids(world)
    world.override_returns_none = True

    response = _run(server, "link_canon_library", filepath=canonical, collections=["CanonHero"], as_override=True)

    assert response["status"] == "error"
    assert list(world.data["libraries"]) == []
    assert _uids(world) == before


def test_link_instances_what_it_linked_so_a_save_keeps_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Blender drops an uninstanced linked collection on save."""
    server, _bpy, world = _server(monkeypatch)
    canonical = _canon(tmp_path, world)

    response = _run(server, "link_canon_library", filepath=canonical, collections=["CanonHero"], objects=["Crate"])

    assert response["status"] == "success", response
    result = response["result"]
    root = world.scene.collection
    assert [c.session_uid for c in root.children] == [result["collections"][0]["session_uid"]]
    assert [o.session_uid for o in root.objects] == [result["objects"][0]["session_uid"]]
    library = world.data["libraries"][0]
    assert result["library"]["session_uid"] == library.session_uid
    assert result["library"]["version"] == [5, 2, 44]


def test_link_names_default_to_none_and_are_not_shared_across_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A mutable default would let the second call relink the first call's names."""
    server, _bpy, world = _server(monkeypatch)
    canonical = _canon(tmp_path, world)
    parameters = inspect.signature(server.link_canon_library).parameters  # type: ignore[attr-defined]

    assert parameters["collections"].default is None
    assert parameters["objects"].default is None
    assert _run(server, "link_canon_library", filepath=canonical, collections=["CanonHero"])["status"] == "success"
    second = _run(server, "link_canon_library", filepath=canonical)
    third = _run(server, "link_canon_library", filepath=canonical)

    assert second["status"] == "error" and third["status"] == "error"
    assert "collections" in second["message"] and "objects" in second["message"]
    assert len(world.load_calls) == 1


def test_link_as_override_uses_route_c_not_create_liboverrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Route A's objects stay locked, so the override is a separate Route C call."""
    server, _bpy, world = _server(monkeypatch)
    canonical = _canon(tmp_path, world)

    response = _run(server, "link_canon_library", filepath=canonical, collections=["CanonHero"], as_override=True)

    assert response["status"] == "success", response
    kwargs = world.load_calls[0][1]
    assert kwargs.get("create_liboverrides", False) is False
    assert kwargs["link"] is True
    assert len(world.override_calls) == 1
    assert world.override_calls[0][3].get("do_fully_editable") is True
    override = response["result"]["overrides"][0]
    assert override["override"]["is_system_override"] is False
    assert [c.override_library is not None for c in world.scene.collection.children] == [True]


def test_link_reports_the_objects_it_brought_into_the_scene(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A linked collection's members count, not only the objects named in the request."""
    server, _bpy, world = _server(monkeypatch)
    canonical = _canon(tmp_path, world)

    response = _run(server, "link_canon_library", filepath=canonical, collections=["CanonHero"], objects=["Crate"])

    assert response["status"] == "success", response
    assert response["result"]["changed_objects"] == ["Crate", "HeroBody"]


def test_link_as_override_reports_the_override_objects(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The objects are the editable overrides, which the report names."""
    server, _bpy, world = _server(monkeypatch)
    canonical = _canon(tmp_path, world)

    response = _run(server, "link_canon_library", filepath=canonical, collections=["CanonHero"], as_override=True)

    assert response["status"] == "success", response
    assert response["result"]["changed_objects"] == ["HeroBody"]


def test_create_override_reports_the_override_objects(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Overriding an already-linked collection reports the override's objects too."""
    server, _bpy, world = _server(monkeypatch)
    canonical = _canon(tmp_path, world)
    linked = _run(server, "link_canon_library", filepath=canonical, collections=["CanonHero"])["result"]

    response = _run(server, "create_override", collection_uid=linked["collections"][0]["session_uid"])

    assert response["status"] == "success", response
    assert response["result"]["changed_objects"] == ["HeroBody"]


def test_link_refuses_as_override_with_objects(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Route C overrides collection hierarchies, so an object request is refused up front."""
    server, _bpy, world = _server(monkeypatch)
    canonical = _canon(tmp_path, world)

    response = _run(server, "link_canon_library", filepath=canonical, objects=["Crate"], as_override=True)

    assert response["status"] == "error"
    assert world.load_calls == []


def test_link_refuses_relative_in_a_never_saved_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Blender would silently store an absolute path there."""
    server, _bpy, world = _server(monkeypatch)
    canonical = _canon(tmp_path, world)

    response = _run(server, "link_canon_library", filepath=canonical, collections=["CanonHero"], relative=True)

    assert response["status"] == "error"
    assert "relative" in response["message"]
    assert world.load_calls == []


@pytest.mark.parametrize("flag", ["as_override", "relative"])
def test_link_flags_must_be_real_bools(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, flag: str) -> None:
    """`"false"` is truthy; the session is saved, so a truthy `relative` would otherwise be accepted."""
    server, _bpy, world = _server(monkeypatch, filepath=str(tmp_path / "shot.blend"))
    canonical = _canon(tmp_path, world)

    response = _run(server, "link_canon_library", filepath=canonical, collections=["CanonHero"], **{flag: "false"})

    assert response["status"] == "error"
    assert world.load_calls == []


def test_a_blender_link_failure_reaches_the_client_without_its_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A truncated `.blend` passes the magic-byte check and fails inside `libraries.load` with its path."""
    server, _bpy, world = _server(monkeypatch)
    canonical = _canon(tmp_path, world)
    world.load_errors[canonical] = OSError(LINK_TRUNCATED.replace(f"{_LIFECYCLE_WORK}/truncated.blend", canonical))

    response = _run(server, "link_canon_library", filepath=canonical, collections=["CanonHero"])

    assert response["status"] == "error"
    assert "Missing DNA block" in response["message"]
    _assert_no_path(response["message"], tmp_path.name)
    assert list(world.data["libraries"]) == []


# ---------------------------------------------------------------------------
# create_override
# ---------------------------------------------------------------------------


def test_create_override_passes_do_fully_editable_true_explicitly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The default is False, which silently makes system overrides."""
    server, _bpy, world = _server(monkeypatch)
    _library, collection = _linked(server, world, _canon(tmp_path, world))

    response = _run(server, "create_override", collection_uid=collection.session_uid)

    assert response["status"] == "success", response
    assert len(world.override_calls) == 1
    kwargs = world.override_calls[0][3]
    assert "do_fully_editable" in kwargs, "do_fully_editable was inherited from Blender's default (False)"
    assert kwargs["do_fully_editable"] is True
    assert response["result"]["override"]["is_system_override"] is False


def test_create_override_takes_the_scene_from_bpy_data_not_bpy_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`bpy.context` is a decoy scene here; the call must receive the database's scene and its view layer."""
    server, bpy, world = _server(monkeypatch)
    _library, collection = _linked(server, world, _canon(tmp_path, world))

    _run(server, "create_override", collection_uid=collection.session_uid)

    _target, scene, view_layer, _kwargs = world.override_calls[0]
    assert scene is world.scene and scene is not bpy.context.scene
    assert view_layer is world.scene.view_layers[0] and view_layer is not bpy.context.view_layer


def test_create_override_refuses_to_guess_between_scenes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With two scenes and no `scene_uid`, the refusal lists both uids; with one, it overrides into that scene."""
    server, _bpy, world = _server(monkeypatch)
    _library, collection = _linked(server, world, _canon(tmp_path, world))
    second = StubScene(world, "Scene.001")
    world.data["scenes"].append(second)

    refused = _run(server, "create_override", collection_uid=collection.session_uid)
    chosen = _run(server, "create_override", collection_uid=collection.session_uid, scene_uid=second.session_uid)

    assert refused["status"] == "error"
    assert str(world.scene.session_uid) in refused["message"] and str(second.session_uid) in refused["message"]
    assert chosen["status"] == "success", chosen
    assert world.override_calls[-1][1] is second


def test_create_override_resolves_by_session_uid_among_same_named_collections(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """After an override `CanonHero` names two collections; the uid picks the linked one, the override is refused."""
    server, _bpy, world = _server(monkeypatch)
    _library, collection = _linked(server, world, _canon(tmp_path, world))
    first = _run(server, "create_override", collection_uid=collection.session_uid)
    override_uid = first["result"]["override"]["session_uid"]

    again = _run(server, "create_override", collection_uid=collection.session_uid)
    on_override = _run(server, "create_override", collection_uid=override_uid)

    assert [c.name for c in world.data["collections"]] == ["CanonHero", "CanonHero"]
    assert again["status"] == "error" and str(override_uid) in again["message"]
    assert on_override["status"] == "error" and "linked" in on_override["message"]
    assert len(world.override_calls) == 1


@pytest.mark.parametrize("uid", [True, 1.0])
def test_create_override_refuses_a_uid_that_is_not_an_integer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, uid: object
) -> None:
    """`True == 1 == 1.0`, so neither may resolve the collection whose uid is 1."""
    server, _bpy, world = _server(monkeypatch)
    _library, collection = _linked(server, world, _canon(tmp_path, world))
    collection.session_uid = 1

    response = _run(server, "create_override", collection_uid=uid)

    assert response["status"] == "error"
    assert world.override_calls == []


def test_create_override_reports_same_named_linked_and_override_objects_distinguishably(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Route C leaves two `HeroBody` objects; the report tells them apart by uid and state."""
    server, _bpy, world = _server(monkeypatch)
    _library, collection = _linked(server, world, _canon(tmp_path, world))
    linked_body = collection.objects[0]

    result = _run(server, "create_override", collection_uid=collection.session_uid, detail=True)["result"]

    assert [o.name for o in world.data["objects"]].count("HeroBody") == len(("override", "linked original"))
    (reported,) = result["objects"]["records"]
    assert reported["name"] == "HeroBody"
    assert reported["session_uid"] != linked_body.session_uid
    assert reported["reference_uid"] == linked_body.session_uid
    assert reported["is_override"] is True and reported["is_system_override"] is False
    assert result["override"]["reference_uid"] == collection.session_uid


def test_create_override_counts_the_objects_it_made_and_leaves_their_names_to_changed_objects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Repeating the names inside `objects` would send the same list twice in one reply."""
    server, _bpy, world = _server(monkeypatch)
    _library, collection = _linked(server, world, _canon(tmp_path, world))

    result = _run(server, "create_override", collection_uid=collection.session_uid)["result"]

    assert result["objects"] == {"total": 1, "by_type": {"OBJECT": 1}}
    assert result["changed_objects"] == ["HeroBody"]
    assert result["override"]["session_uid"] != collection.session_uid


def test_create_override_replaces_the_linked_instance_it_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Route C adds the override beside the instance, which would draw the asset twice."""
    server, _bpy, world = _server(monkeypatch)
    _library, collection = _linked(server, world, _canon(tmp_path, world))

    result = _run(server, "create_override", collection_uid=collection.session_uid)["result"]

    children = world.scene.collection.children
    assert [c.session_uid for c in children] == [result["override"]["session_uid"]]
    assert result["replaced_instances"] == 1
    assert collection in world.data["collections"]


def test_create_override_that_blender_declines_is_an_error_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`override_hierarchy_create` returns None rather than raising; that is not a success."""
    server, _bpy, world = _server(monkeypatch)
    _library, collection = _linked(server, world, _canon(tmp_path, world))
    before = _uids(world)
    world.override_returns_none = True

    response = _run(server, "create_override", collection_uid=collection.session_uid)

    assert response["status"] == "error"
    assert "created no override" in response["message"]
    assert _uids(world) == before


def test_an_override_failure_reaches_the_client_sanitized(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every `except` around a data-API call sanitizes, including this one."""
    server, _bpy, world = _server(monkeypatch)
    canonical = _canon(tmp_path, world)
    _library, collection = _linked(server, world, canonical)
    world.override_error = RuntimeError(f"Error: cannot override from '{canonical}'")

    response = _run(server, "create_override", collection_uid=collection.session_uid)

    assert response["status"] == "error"
    _assert_no_path(response["message"], tmp_path.name)


# ---------------------------------------------------------------------------
# list_libraries
# ---------------------------------------------------------------------------


def test_list_libraries_paginates_and_reports_what_a_reload_decision_needs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`is_missing`, `version`, resync, users, uid, and each linked datablock with its uid under `detail`."""
    server, _bpy, world = _server(monkeypatch)
    libraries = [_linked(server, world, _canon(tmp_path, world, f"canon{index}.blend"))[0] for index in range(3)]
    libraries[1].is_missing = True
    libraries[1].needs_liboverride_resync = True

    page = _run(server, "list_libraries", limit=PAGE, offset=1, detail=True)["result"]

    assert (page["total"], page["offset"], page["limit"]) == (len(libraries), 1, PAGE)
    assert (page["returned_count"], page["truncated"], page["next_offset"]) == (2, False, None)
    first, second = page["libraries"]
    assert first["session_uid"] == libraries[1].session_uid
    assert first["is_missing"] is True and first["needs_liboverride_resync"] is True
    assert first["version"] == [5, 2, 44] and first["users"] == 1
    records = first["datablocks"]["records"]
    assert {d["session_uid"] for d in records} == {db.session_uid for db in libraries[1].users_id}
    assert {d["name"] for d in records} == {"CanonHero", "HeroBody"}
    assert second["session_uid"] == libraries[2].session_uid and second["is_missing"] is False
    shortened = _run(server, "list_libraries", limit=PAGE)["result"]
    assert (shortened["truncated"], shortened["next_offset"]) == (True, PAGE)


@pytest.mark.parametrize(
    "params",
    [{"limit": 0}, {"limit": 101}, {"limit": True}, {"limit": "5"}, {"offset": -1}, {"offset": False}],
    ids=["zero", "over-bound", "bool-limit", "string-limit", "negative-offset", "bool-offset"],
)
def test_list_libraries_bounds_its_page(monkeypatch: pytest.MonkeyPatch, params: dict[str, object]) -> None:
    """A limit that is zero, too large, a bool or a string is refused, as is a negative or bool offset."""
    server, _bpy, _world = _server(monkeypatch)

    assert _run(server, "list_libraries", **params)["status"] == "error"


def test_list_libraries_bounds_the_datablocks_it_lists_per_library(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A canon library can link thousands of datablocks; the default says how many of what, not their names."""
    server, _bpy, world = _server(monkeypatch)
    library, _collection = _linked(server, world, _canon(tmp_path, world))
    for index in range(MANY):
        world.data["meshes"].append(StubID(world, f"M{index}", "MESH", library=library))

    (entry,) = _run(server, "list_libraries")["result"]["libraries"]

    listed = entry["datablocks"]
    assert listed["total"] == MANY + len(("CanonHero", "HeroBody"))
    assert listed["by_type"] == {"COLLECTION": 1, "MESH": MANY, "OBJECT": 1}
    assert (listed["returned_count"], listed["truncated"], listed["next_offset"]) == (NAME_CAP, True, NAME_CAP)
    assert listed["names"] == ["HeroBody", *(f"M{index}" for index in range(NAME_CAP - 1))]
    assert "records" not in listed


def test_list_libraries_lists_the_datablock_records_only_on_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`detail` is the only way to the uids, and it is still capped: the names page never carries them."""
    server, _bpy, world = _server(monkeypatch)
    library, _collection = _linked(server, world, _canon(tmp_path, world))
    for index in range(MANY):
        world.data["meshes"].append(StubID(world, f"M{index}", "MESH", library=library))

    (entry,) = _run(server, "list_libraries", detail=True)["result"]["libraries"]

    listed = entry["datablocks"]
    assert listed["total"] == MANY + len(("CanonHero", "HeroBody"))
    assert (listed["returned_count"], listed["truncated"], listed["next_offset"]) == (LISTED_CAP, True, LISTED_CAP)
    assert len(listed["records"]) == LISTED_CAP
    assert {record["session_uid"] for record in listed["records"]} <= {db.session_uid for db in library.users_id}
    assert "names" not in listed


def test_list_libraries_is_a_read_only_command_and_never_enters_a_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pure read must not pay for a snapshot and a rollback wrapper."""
    server, _bpy, _world = _server(monkeypatch)

    assert "list_libraries" in server._READ_ONLY_COMMANDS  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# reload_library / relocate_library
# ---------------------------------------------------------------------------


def test_reload_library_uses_the_data_api_inside_the_replace_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`lib.reload()`, wrapped exactly in `replacing_library_contents`; no operator is touched."""
    server, bpy, world = _server(monkeypatch)
    library, _collection = _linked(server, world, _canon(tmp_path, world))
    before = {db.session_uid for db in library.users_id}

    response = _run(server, "reload_library", library_uid=library.session_uid, detail=True)

    assert response["status"] == "success", response
    assert world.reload_calls == [(library, True)]
    assert bpy.ops.wm.touched == []
    assert response["result"]["library"]["session_uid"] == library.session_uid
    records = response["result"]["datablocks"]["records"]
    assert {d["session_uid"] for d in records} == {db.session_uid for db in library.users_id}
    assert before.isdisjoint(db.session_uid for db in library.users_id)


@pytest.mark.parametrize("command", ["reload_library", "relocate_library"])
def test_a_reload_reports_what_it_replaced_by_type_without_the_records(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str
) -> None:
    """The uids all changed, so the default names what was replaced and counts it; the records are `detail`."""
    server, _bpy, world = _server(monkeypatch)
    library, _collection = _linked(server, world, _canon(tmp_path, world))
    extra = {"filepath": _canon(tmp_path, world, "canon_v2.blend")} if command == "relocate_library" else {}

    result = _run(server, command, library_uid=library.session_uid, **extra)["result"]

    listed = result["datablocks"]
    assert listed["total"] == len(("CanonHero", "HeroBody"))
    assert listed["by_type"] == {"COLLECTION": 1, "OBJECT": 1}
    assert listed["names"] == ["HeroBody", "CanonHero"]
    assert (listed["truncated"], listed["next_offset"]) == (False, None)
    assert "records" not in listed
    assert result["library"]["session_uid"] == library.session_uid


def test_no_wm_lib_operator_exists_in_the_linking_module() -> None:
    """The module's AST holds no `lib_reload` / `lib_relocate` call."""
    tree = ast.parse(LINKING_SOURCE.read_text(encoding="utf-8"))
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}

    assert "lib_reload" not in attributes
    assert "lib_relocate" not in attributes
    assert "reload" in attributes


def test_reload_failure_reaches_the_client_sanitized_from_a_captured_blender_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A deleted file behind an absolute link: the cause and the library name survive, the path does not."""
    server, _bpy, world = _server(monkeypatch)
    library, _collection = _linked(server, world, _canon(tmp_path, world))
    library.filepath = f"{_RELOAD_WORK}/gone.blend"
    library.reload_error = RuntimeError(RELOAD_ABSOLUTE)
    uids = _uids(world)

    response = _run(server, "reload_library", library_uid=library.session_uid)

    assert response["status"] == "error"
    assert "invalid path" in response["message"] and "canon.blend" in response["message"]
    _assert_no_path(response["message"], "t7_reload", "gone.blend")
    assert _uids(world) == uids


def test_reload_failure_of_a_relative_link_under_a_comma_directory_is_sanitized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    A deleted file behind a `//` link in `Smith, John`: the whole quoted path goes, comma and all.

    Blender reports the expanded path, not the stored `//` form. The quoted shape
    is also removed structurally, so this passes even without known paths.
    """
    server, bpy, world = _server(monkeypatch)
    library, _collection = _linked(server, world, _canon(tmp_path, world))
    bpy.data.filepath = f"{_RELOAD_WORK}/Smith, John/shot.blend"
    library.filepath = "//canon.blend"
    library.reload_error = RuntimeError(RELOAD_RELATIVE_SMITH)

    response = _run(server, "reload_library", library_uid=library.session_uid)

    assert response["status"] == "error"
    assert "invalid path" in response["message"]
    _assert_no_path(response["message"], "Smith", "John", "t7_reload")


def test_a_reload_failure_in_an_unsaved_session_still_names_the_library(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no file open a `//` link expands to a bare `canon.blend`, which must not be a known path."""
    server, _bpy, world = _server(monkeypatch)
    library, _collection = _linked(server, world, _canon(tmp_path, world))
    library.filepath = "//canon.blend"
    library.reload_error = RuntimeError(RELOAD_ABSOLUTE)

    response = _run(server, "reload_library", library_uid=library.session_uid)

    assert response["status"] == "error"
    assert "library 'canon.blend'" in response["message"], response["message"]


def test_relocate_assigns_the_canonical_path_reloads_and_reports_the_name_both_sides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The data-API relocate: `filepath` is the validated canonical form when `reload()` runs, not what was sent."""
    server, bpy, world = _server(monkeypatch)
    library, _collection = _linked(server, world, _canon(tmp_path, world))
    target = _canon(tmp_path, world, "canon_v2.blend")
    spelled = os.path.join(os.path.dirname(target), ".", "canon_v2.blend")

    response = _run(server, "relocate_library", library_uid=library.session_uid, filepath=spelled)

    assert response["status"] == "success", response
    assert library.filepath_during_reload == [target]
    assert world.reload_calls == [(library, True)]
    assert response["result"]["name_before"] == "canon.blend"
    assert response["result"]["name_after"] == "canon.blend"
    assert response["result"]["library"]["session_uid"] == library.session_uid
    assert bpy.ops.wm.touched == []


def test_relocate_validates_the_new_path_through_the_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The new file is a read on an unauthenticated socket, like a link."""
    server, _bpy, world = _server(monkeypatch)
    inside, outside = tmp_path / "inside", tmp_path / "outside"
    inside.mkdir()
    outside.mkdir()
    library, _collection = _linked(server, world, _canon(inside, world))
    refused = _canon(outside, world, "elsewhere.blend")
    monkeypatch.setenv("BLENDERMCP_FILE_ROOTS", str(inside))

    for path in (refused, str(inside / "missing.blend")):
        response = _run(server, "relocate_library", library_uid=library.session_uid, filepath=path)
        assert response["status"] == "error"
        _assert_no_path(response["message"], tmp_path.name)
    assert world.reload_calls == []


def test_relocate_refuses_a_file_another_library_already_links(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Two `Library` datablocks for one file is not a state this command should create."""
    server, _bpy, world = _server(monkeypatch)
    first, _c1 = _linked(server, world, _canon(tmp_path, world, "a.blend"))
    second, _c2 = _linked(server, world, _canon(tmp_path, world, "b.blend"))

    response = _run(server, "relocate_library", library_uid=first.session_uid, filepath=second.filepath)

    assert response["status"] == "error"
    assert str(second.session_uid) in response["message"]
    assert world.reload_calls == []


def test_a_failed_relocate_restores_the_previous_path_and_is_sanitized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed reload leaves the contents intact, so the library must not be left pointing at the new file."""
    server, _bpy, world = _server(monkeypatch)
    library, _collection = _linked(server, world, _canon(tmp_path, world))
    previous = library.filepath
    target = _canon(tmp_path, world, "canon_v2.blend")
    library.reload_error = RuntimeError(RELOAD_ABSOLUTE.replace(f"{_RELOAD_WORK}/gone.blend", target))

    response = _run(server, "relocate_library", library_uid=library.session_uid, filepath=target)

    assert response["status"] == "error"
    assert library.filepath == previous
    _assert_no_path(response["message"], tmp_path.name)


@pytest.mark.parametrize("command", ["reload_library", "relocate_library"])
def test_an_unknown_library_uid_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str) -> None:
    """A uid read before a load or a reload names nothing; the refusal says so."""
    server, _bpy, world = _server(monkeypatch)
    extra = {"filepath": _canon(tmp_path, world)} if command == "relocate_library" else {}

    response = _run(server, command, library_uid=999_999_999, **extra)

    assert response["status"] == "error"
    assert "list_libraries" in response["message"]


# ---------------------------------------------------------------------------
# unlink_libraries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("confirm", [False, "true", 1], ids=["false", "string", "int"])
def test_unlink_refuses_without_a_real_confirmation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, confirm: object
) -> None:
    """Deleting linked data needs `confirm=True`, and only the bool."""
    server, _bpy, world = _server(monkeypatch)
    library, _collection = _linked(server, world, _canon(tmp_path, world))
    uids = _uids(world)

    response = _run(server, "unlink_libraries", library_uids=[library.session_uid], confirm=confirm)

    assert response["status"] == "error"
    assert _uids(world) == uids
    assert world.data["libraries"].removed == []


def test_unlink_never_touches_a_library_that_was_not_named(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The unnamed library and every datablock it linked keep their uids."""
    server, _bpy, world = _server(monkeypatch)
    named, _c1 = _linked(server, world, _canon(tmp_path, world, "a.blend"))
    kept, _c2 = _linked(server, world, _canon(tmp_path, world, "b.blend"))
    kept_uids = {kept.session_uid, *(db.session_uid for db in kept.users_id)}

    response = _run(server, "unlink_libraries", library_uids=[named.session_uid], confirm=True)

    assert response["status"] == "success", response
    assert world.data["libraries"].removed == [named]
    assert kept_uids <= _uids(world)
    assert response["result"]["other_libraries_removed"] == []


def test_unlink_resolves_every_uid_before_removing_anything(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """One unknown uid refuses the whole request; a half-applied unlink is not reversible."""
    server, _bpy, world = _server(monkeypatch)
    library, _collection = _linked(server, world, _canon(tmp_path, world))
    uids = _uids(world)

    response = _run(server, "unlink_libraries", library_uids=[library.session_uid, 999_999_999], confirm=True)

    assert response["status"] == "error"
    assert "999999999" in response["message"]
    assert _uids(world) == uids


@pytest.mark.parametrize("shape", ["empty", "bool", "string", "big"])
def test_unlink_requires_an_explicit_bounded_uid_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, shape: str
) -> None:
    """
    Never "everything", never a coerced handle, never unbounded.

    The library's uid is 1, so each shape would reach it if accepted.
    """
    server, _bpy, world = _server(monkeypatch)
    library, _collection = _linked(server, world, _canon(tmp_path, world))
    library.session_uid = 1
    uids = _uids(world)
    value = {"empty": [], "bool": [True], "string": ["1"], "big": [1] * 101}[shape]

    response = _run(server, "unlink_libraries", library_uids=value, confirm=True)

    assert response["status"] == "error"
    assert _uids(world) == uids


def test_unlink_reports_exactly_what_it_removed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The library, its linked datablocks and the override objects Blender freed with them."""
    server, _bpy, world = _server(monkeypatch)
    library, collection = _linked(server, world, _canon(tmp_path, world))
    _run(server, "create_override", collection_uid=collection.session_uid)
    before = _uids(world)

    result = _run(server, "unlink_libraries", library_uids=[library.session_uid, library.session_uid], confirm=True)[
        "result"
    ]

    removed = before - _uids(world)
    assert result["removed_libraries"][0]["session_uid"] == library.session_uid
    assert len(result["removed_libraries"]) == 1
    assert set(result["removed_uids"]) == removed
    assert result["removed_count"] == len(removed)
    assert result["removed_by_type"] == {"libraries": 1, "collections": 1, "objects": 2}
    assert result["purged_orphans"] == 0


def test_unlink_refuses_an_indirect_library(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Removing a library reached only through another one edits the parent library's contents."""
    server, _bpy, world = _server(monkeypatch)
    library, _collection = _linked(server, world, _canon(tmp_path, world))
    for datablock in library.users_id:
        datablock.is_library_indirect = True

    response = _run(server, "unlink_libraries", library_uids=[library.session_uid], confirm=True)

    assert response["status"] == "error"
    assert "indirect" in response["message"]
    assert world.data["libraries"].removed == []


def test_unlink_purges_only_when_asked_and_only_what_it_orphaned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`orphans_purge` would also delete the user's own zero-user datablock."""
    server, _bpy, world = _server(monkeypatch)
    for purge in (False, True):
        library, _collection = _linked(server, world, _canon(tmp_path, world, f"lib{purge}.blend"))
        orphaned = world.data["materials"].new(f"HeroPaint{purge}")
        library.users_id[0].uses.append(orphaned)
        scratch = world.data["materials"].new(f"UserScratch{purge}")
        scratch.users = 0

        result = _run(
            server, "unlink_libraries", library_uids=[library.session_uid], confirm=True, purge_orphans=purge
        )["result"]

        assert scratch in world.data["materials"]
        assert (orphaned in world.data["materials"]) is not purge
        assert result["purged_orphans"] == (1 if purge else 0)
    assert world.batch_removed == [orphaned], "duplicates mean the aggregate all_ids collection was walked"


def test_unlink_never_removes_a_datablock_an_earlier_removal_freed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Each uid is re-resolved just before its removal; the stub raises `ReferenceError` on a freed one."""
    server, _bpy, world = _server(monkeypatch)
    first, _c1 = _linked(server, world, _canon(tmp_path, world, "a.blend"))
    second, _c2 = _linked(server, world, _canon(tmp_path, world, "b.blend"))
    real_remove = world.data["libraries"].remove

    def cascading_remove(datablock: StubID, do_unlink: bool = True) -> None:
        """
        Free the second library along with the first, as a cascade would.

        Args:
            datablock: The library.
            do_unlink: Passed through.

        """
        real_remove(datablock, do_unlink)
        if datablock is first:
            world.free(second)

    world.data["libraries"].remove = cascading_remove  # type: ignore[method-assign]

    response = _run(server, "unlink_libraries", library_uids=[first.session_uid, second.session_uid], confirm=True)

    assert response["status"] == "success", response
    assert [lib["session_uid"] for lib in response["result"]["removed_libraries"]] == [first.session_uid]
    assert response["result"]["other_libraries_removed"] == []
    assert response["result"]["already_removed_uids"] == [second.session_uid]


def test_an_unlink_failure_reaches_the_client_sanitized(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`libraries.remove` is wrapped like every other data-API call."""
    server, _bpy, world = _server(monkeypatch)
    canonical = _canon(tmp_path, world)
    library, _collection = _linked(server, world, canonical)
    world.remove_error = RuntimeError(f"Error: cannot free library '{canonical}'")

    response = _run(server, "unlink_libraries", library_uids=[library.session_uid], confirm=True)

    assert response["status"] == "error"
    _assert_no_path(response["message"], tmp_path.name)


# ---------------------------------------------------------------------------
# name resolution, transaction routing, and the command contract
# ---------------------------------------------------------------------------


def test_the_name_resolution_helper_refuses_ambiguity_listing_uids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A name that matches two datablocks is an error naming both uids, never the first match."""
    server, _bpy, world = _server(monkeypatch)
    _library, collection = _linked(server, world, _canon(tmp_path, world))
    override_uid = _run(server, "create_override", collection_uid=collection.session_uid)["result"]["override"][
        "session_uid"
    ]
    linking = sys.modules[f"{type(server).__module__.rsplit('.', 1)[0]}.handlers.linking"]

    with pytest.raises(ValueError, match="more than one") as refused:
        linking.resolve_unique_name(world.data["collections"], "CanonHero", "collection")
    assert str(collection.session_uid) in str(refused.value) and str(override_uid) in str(refused.value)
    assert linking.resolve_unique_name(world.data["libraries"], "canon.blend", "library") is world.data["libraries"][0]
    with pytest.raises(ValueError, match="no collection"):
        linking.resolve_unique_name(world.data["collections"], "Nope", "collection")


def test_the_three_replacing_commands_never_enter_a_transaction_and_the_link_does(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Through the real handlers, only the link and the override enter a transaction."""
    server, _bpy, world = _server(monkeypatch)
    server_core = sys.modules[type(server).__module__]
    real_transaction = server_core.mutation_transaction
    entered: list[str] = []

    def recording_transaction(cmd_type: str, *args: object) -> object:
        """
        Record the wrapped command, then run the real transaction.

        Args:
            cmd_type: The command being wrapped.
            *args: Passed through.

        Returns:
            object: The real context manager.

        """
        entered.append(cmd_type)
        return real_transaction(cmd_type, *args)

    monkeypatch.setattr(server_core, "mutation_transaction", recording_transaction)
    library, collection = _linked(server, world, _canon(tmp_path, world))
    target = _canon(tmp_path, world, "canon_v2.blend")
    responses = [
        _run(server, "create_override", collection_uid=collection.session_uid),
        _run(server, "list_libraries"),
        _run(server, "reload_library", library_uid=library.session_uid),
        _run(server, "relocate_library", library_uid=library.session_uid, filepath=target),
        _run(server, "unlink_libraries", library_uids=[library.session_uid], confirm=True),
    ]

    assert [r["status"] for r in responses] == ["success"] * 5, responses
    assert entered == ["link_canon_library", "create_override"]


def test_no_linking_command_takes_a_datablock_name_as_a_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every handle is a uid; `collections` / `objects` name contents of the library *file*."""
    server, _bpy, _world = _server(monkeypatch)
    library_file_names = {("link_canon_library", "collections"), ("link_canon_library", "objects")}
    non_handles = {"filepath", "limit", "offset", "confirm", "purge_orphans", "as_override", "relative", "detail"}

    for command in LINKING_COMMANDS:
        for name in inspect.signature(getattr(server, command)).parameters:
            if (command, name) in library_file_names or name in non_handles:
                continue
            assert name.endswith(("_uid", "_uids")), f"{command}({name}) is not a session_uid handle"


def test_the_linking_commands_are_dispatchable_and_advertised(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every linking command is advertised in the handshake and dispatchable; only `list_libraries` is read-only."""
    server, _bpy, _world = _server(monkeypatch)
    capabilities = server.get_addon_info()["capabilities"]  # type: ignore[attr-defined]

    for name in LINKING_COMMANDS:
        assert name in capabilities
        assert name in server._build_command_handlers()  # type: ignore[attr-defined]
    read_only = set(LINKING_COMMANDS) & set(server._READ_ONLY_COMMANDS)  # type: ignore[attr-defined]
    assert read_only == {"list_libraries"}


# ---------------------------------------------------------------------------
# script auto-execution
# ---------------------------------------------------------------------------


def _scripts_auto_execute(bpy: types.ModuleType, value: object) -> None:
    """
    Set the preference, or make it unreadable when `value` is `...`.

    Args:
        bpy: The stub module.
        value: The preference value, or `...` for a preferences object without it.

    """
    filepaths = types.SimpleNamespace() if value is ... else types.SimpleNamespace(use_scripts_auto_execute=value)
    bpy.context.preferences.filepaths = filepaths


@pytest.mark.parametrize("value", [True, ...], ids=["on", "unreadable"])
def test_link_refuses_while_scripts_auto_execute_is_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: object
) -> None:
    """Nothing is loaded while the preference is on or unreadable."""
    server, bpy, world = _server(monkeypatch)
    canonical = _canon(tmp_path, world)
    _scripts_auto_execute(bpy, value)

    response = _run(server, "link_canon_library", filepath=canonical, collections=["CanonHero"])

    assert response["status"] == "error"
    assert "link_canon_library refuses" in response["message"]
    assert "use_scripts_auto_execute" in response["message"]
    assert world.load_calls == []


def test_link_proceeds_while_scripts_auto_execute_is_off(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The factory default, False, does not block a link."""
    server, bpy, world = _server(monkeypatch)
    canonical = _canon(tmp_path, world)
    _scripts_auto_execute(bpy, False)

    response = _run(server, "link_canon_library", filepath=canonical, collections=["CanonHero"])

    assert response["status"] == "success", response
    assert len(world.load_calls) == 1


@pytest.mark.parametrize("value", [True, ...], ids=["on", "unreadable"])
def test_reload_and_relocate_refuse_while_scripts_auto_execute_is_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: object
) -> None:
    """Neither reloads, and a refused relocate leaves the library's path exactly as it was."""
    server, bpy, world = _server(monkeypatch)
    library, _collection = _linked(server, world, _canon(tmp_path, world))
    previous = library.filepath
    target = _canon(tmp_path, world, "canon_v2.blend")
    _scripts_auto_execute(bpy, value)

    reload_response = _run(server, "reload_library", library_uid=library.session_uid)
    relocate_response = _run(server, "relocate_library", library_uid=library.session_uid, filepath=target)

    assert reload_response["status"] == "error" and "reload_library refuses" in reload_response["message"]
    assert relocate_response["status"] == "error" and "relocate_library refuses" in relocate_response["message"]
    assert world.reload_calls == []
    assert library.filepath == previous


def test_reload_and_relocate_proceed_while_scripts_auto_execute_is_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The off branch of the same check."""
    server, bpy, world = _server(monkeypatch)
    library, _collection = _linked(server, world, _canon(tmp_path, world))
    target = _canon(tmp_path, world, "canon_v2.blend")
    _scripts_auto_execute(bpy, False)

    assert _run(server, "reload_library", library_uid=library.session_uid)["status"] == "success"
    assert _run(server, "relocate_library", library_uid=library.session_uid, filepath=target)["status"] == "success"
    assert len(world.reload_calls) == len(("reload", "relocate"))


# ---------------------------------------------------------------------------
# override placement, missing datablocks, nesting and hostile names
# ---------------------------------------------------------------------------


def _root_uids(world: World) -> list[int]:
    """
    Read the scene root's children by uid.

    Args:
        world: The stub database.

    Returns:
        list[int]: Their session_uids, in order.

    """
    return [child.session_uid for child in world.scene.collection.children]


def test_a_refused_multi_collection_override_keeps_the_existing_placement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    Every collection is validated before the first override replaces an instance.

    Otherwise a refused second collection loses the first one's placement, and a
    save keeps the loss.
    """
    server, _bpy, world = _server(monkeypatch)
    canonical = _canon(tmp_path, world)
    placed = _run(server, "link_canon_library", filepath=canonical, collections=["CanonHero", "CanonProp"])["result"]
    _run(server, "create_override", collection_uid=placed["collections"][1]["session_uid"])
    before = _root_uids(world)

    response = _run(
        server, "link_canon_library", filepath=canonical, collections=["CanonHero", "CanonProp"], as_override=True
    )

    assert response["status"] == "error"
    assert "already overridden" in response["message"]
    assert _root_uids(world) == before


def test_a_later_override_failure_restores_the_instances_earlier_overrides_replaced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Rollback removes the created overrides but not `children` links, so the handler re-links what it unlinked."""
    server, _bpy, world = _server(monkeypatch)
    canonical = _canon(tmp_path, world)
    _run(server, "link_canon_library", filepath=canonical, collections=["CanonHero", "CanonProp"])
    before = _root_uids(world)
    world.override_none_for = {"CanonProp"}

    response = _run(
        server, "link_canon_library", filepath=canonical, collections=["CanonHero", "CanonProp"], as_override=True
    )

    assert response["status"] == "error"
    # Re-linking appends, so sibling order may change.
    assert sorted(_root_uids(world)) == sorted(before)


@pytest.mark.parametrize("command", ["reload_library", "relocate_library"])
def test_datablocks_the_file_no_longer_holds_are_reported_missing_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str
) -> None:
    """The library reads `is_missing=False` while its collection is a placeholder, so the warning counts datablocks."""
    server, _bpy, world = _server(monkeypatch)
    canonical = _canon(tmp_path, world)
    library, _collection = _linked(server, world, canonical)
    target = _canon(tmp_path, world, "other.blend")
    world.files[target if command == "relocate_library" else canonical] = {"collections": {}, "objects": []}
    extra = {"filepath": target} if command == "relocate_library" else {}

    result = _run(server, command, library_uid=library.session_uid, detail=True, **extra)["result"]

    assert result["library"]["is_missing"] is False
    assert [entry["is_missing"] for entry in result["datablocks"]["records"]] == [True, True]
    assert result["warnings"] == [
        "2 datablocks linked from this library are missing from its file; they are placeholders until relinked"
    ]


def test_a_reload_that_finds_everything_carries_no_warning(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The warning is a count of missing datablocks, not a fixed banner."""
    server, _bpy, world = _server(monkeypatch)
    library, _collection = _linked(server, world, _canon(tmp_path, world))

    result = _run(server, "reload_library", library_uid=library.session_uid, detail=True)["result"]

    assert "warnings" not in result
    assert {entry["is_missing"] for entry in result["datablocks"]["records"]} == {False}


def test_relocate_refuses_an_indirect_library(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Relocating a library reached through another breaks the parent's link."""
    server, _bpy, world = _server(monkeypatch)
    library, _collection = _linked(server, world, _canon(tmp_path, world))
    for datablock in library.users_id:
        datablock.is_library_indirect = True
    previous = library.filepath

    response = _run(
        server, "relocate_library", library_uid=library.session_uid, filepath=_canon(tmp_path, world, "v2.blend")
    )

    assert response["status"] == "error"
    assert "indirect" in response["message"]
    assert world.reload_calls == []
    assert library.filepath == previous


@pytest.mark.parametrize("value", [True, ...], ids=["on", "unreadable"])
def test_create_override_refuses_while_scripts_auto_execute_is_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: object
) -> None:
    """`create_override` refuses under the same check as the loading commands."""
    server, bpy, world = _server(monkeypatch)
    _library, collection = _linked(server, world, _canon(tmp_path, world))
    _scripts_auto_execute(bpy, value)

    response = _run(server, "create_override", collection_uid=collection.session_uid)

    assert response["status"] == "error"
    assert "create_override refuses" in response["message"]
    assert world.override_calls == []


def test_create_override_proceeds_while_scripts_auto_execute_is_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The off branch of the same check."""
    server, bpy, world = _server(monkeypatch)
    _library, collection = _linked(server, world, _canon(tmp_path, world))
    _scripts_auto_execute(bpy, False)

    assert _run(server, "create_override", collection_uid=collection.session_uid)["status"] == "success"
    assert len(world.override_calls) == 1


def test_a_nested_request_is_refused_before_it_overrides_a_collection_twice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    Each collection is re-checked just before its own override.

    Otherwise `[Parent, Child]` overrides `Child` twice and draws its object twice.
    The stub does not model Blender's choice of hierarchy root, so the
    `[Child, Parent]` order is untested here.
    """
    server, _bpy, world = _server(monkeypatch)
    # Disabled so the re-check, not the up-front refusal, is what refuses.
    monkeypatch.setattr(_linking_module(server), "_refuse_nested_requests", lambda _collections: None)
    canonical = _canon(tmp_path, world, "nest.blend")
    world.files[canonical] = {"collections": {"Parent": [], "Child": ["ChildBody"]}, "objects": ["ChildBody"]}
    _run(server, "link_canon_library", filepath=canonical, collections=["Parent", "Child"])
    linked = {c.name: c for c in world.data["collections"] if c.library is not None}
    linked["Parent"].children.link(linked["Child"])
    before_root, before_uids = sorted(_root_uids(world)), _uids(world)

    response = _run(server, "link_canon_library", filepath=canonical, collections=["Parent", "Child"], as_override=True)

    assert response["status"] == "error", response
    assert "already overridden" in response["message"]
    assert sorted(_root_uids(world)) == before_root
    assert _uids(world) == before_uids


def test_an_absolute_library_name_is_a_known_path_so_no_relative_tail_survives(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An apostrophe-space in the name ends the quoted-name match early, so the whole name goes first."""
    server, _bpy, world = _server(monkeypatch)
    library, _collection = _linked(server, world, _canon(tmp_path, world))
    library.name = "/Users/o' brien/shots/canon.blend"
    library.filepath = f"{_RELOAD_WORK}/gone.blend"
    library.reload_error = RuntimeError(RELOAD_ABSOLUTE.replace("LIcanon.blend", f"LI{library.name}"))

    response = _run(server, "reload_library", library_uid=library.session_uid)

    assert response["status"] == "error"
    assert "invalid path" in response["message"]
    _assert_no_path(response["message"], "brien", "shots")


def _linking_module(server: object) -> types.ModuleType:
    """
    Find the `handlers.linking` module the server was built from.

    Args:
        server: The server.

    Returns:
        types.ModuleType: The module.

    """
    return sys.modules[f"{type(server).__module__.rsplit('.', 1)[0]}.handlers.linking"]


def _nest(tmp_path: Path, world: World) -> str:
    """
    Describe `nest.blend`: `Parent` holding `Child`, which holds `ChildBody`.

    Args:
        tmp_path: Where the real file goes.
        world: The stub database.

    Returns:
        str: The canonical path.

    """
    canonical = _canon(tmp_path, world, "nest.blend")
    world.files[canonical] = {"collections": {"Parent": [], "Child": ["ChildBody"]}, "objects": ["ChildBody"]}
    world.nesting = {"Parent": ["Child"]}
    return canonical


@pytest.mark.parametrize("route", ["create_override", "link_canon_library"])
def test_overriding_a_parent_whose_inner_collection_is_already_overridden_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, route: str
) -> None:
    """
    Overriding `Child` then `Parent` would leave two `ChildBody` overrides.

    The refusal names the existing override so the client can remove it or override only the child.
    """
    server, _bpy, world = _server(monkeypatch)
    canonical = _nest(tmp_path, world)
    child_uid = _run(server, "link_canon_library", filepath=canonical, collections=["Child"])["result"]["collections"][
        0
    ]["session_uid"]
    child_override = _run(server, "create_override", collection_uid=child_uid)["result"]["override"]["session_uid"]
    if route == "create_override":
        linked = _run(server, "link_canon_library", filepath=canonical, collections=["Parent"])["result"]
        before = _uids(world)
        response = _run(server, "create_override", collection_uid=linked["collections"][0]["session_uid"])
    else:
        before = _uids(world)
        response = _run(server, "link_canon_library", filepath=canonical, collections=["Parent"], as_override=True)

    assert response["status"] == "error", response
    assert str(child_override) in response["message"] and "override only" in response["message"]
    assert _uids(world) == before


def test_a_request_naming_a_collection_and_one_inside_it_is_refused_up_front(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Otherwise the client is told a collection is "already overridden" by an override this request built and undid."""
    server, _bpy, world = _server(monkeypatch)
    canonical = _nest(tmp_path, world)
    before = _uids(world)

    response = _run(server, "link_canon_library", filepath=canonical, collections=["Child", "Parent"], as_override=True)

    assert response["status"] == "error"
    assert "'Child'" in response["message"] and "is inside 'Parent'" in response["message"]
    assert "request only the outermost collection" in response["message"]
    assert world.override_calls == []
    assert _uids(world) == before


def test_library_names_are_reduced_without_touching_the_filesystem(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`Library.name` is chosen by a `.blend` author; the list, the relocate report and a refusal must not stat it."""
    server, _bpy, world = _server(monkeypatch)
    library, _collection = _linked(server, world, _canon(tmp_path, world))
    target = _canon(tmp_path, world, "v2.blend")
    library.name = "/Users/victim/shots/canon.blend"

    real_isdir = os.path.isdir

    def refuse_the_name(path: object) -> bool:
        # Paths (the `filepath` field, the relocate target) are still checked; only the name must not be.
        if path == library.name:
            raise AssertionError("a library name was stat-ed")
        return real_isdir(path)  # type: ignore[arg-type]

    monkeypatch.setattr(os.path, "isdir", refuse_the_name)
    listed = _run(server, "list_libraries")
    relocated = _run(server, "relocate_library", library_uid=library.session_uid, filepath=target)
    for datablock in library.users_id:
        datablock.is_library_indirect = True
    refused = _run(server, "unlink_libraries", library_uids=[library.session_uid], confirm=True)

    assert listed["status"] == "success", listed
    assert listed["result"]["libraries"][0]["name"] == "canon.blend"
    assert relocated["status"] == "success", relocated
    assert relocated["result"]["name_before"] == "canon.blend"
    assert "'canon.blend'" in refused["message"], refused
