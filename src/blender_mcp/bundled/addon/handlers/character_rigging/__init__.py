"""Blender-main-thread character-rigging handlers grouped by responsibility."""

from .registry import CharacterRiggingHandlersMixin as CharacterRiggingHandlersMixin
from .structure import _apply_patch_to_specs as _apply_patch_to_specs
from .structure import _hierarchy_cycles as _hierarchy_cycles
from .structure import _validate_bone_specs as _validate_bone_specs
