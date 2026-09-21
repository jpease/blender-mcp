"""Shared types for the retopology tool package; dispatch lives in `tools/_dispatch.py`."""

from typing import Literal

RetopologyProfile = Literal["CHARACTER", "HARD_SURFACE", "VFX", "GAME"]
