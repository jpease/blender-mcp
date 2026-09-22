"""Public lighting enums; the input base lives in `tools/_inputs.py`, dispatch in `tools/_dispatch.py`."""

from typing import Literal

TargetEngine = Literal["BOTH", "CYCLES", "EEVEE"]
LightType = Literal["POINT", "SPOT", "AREA", "SUN"]
StudioLightingMood = Literal["SOFT", "HIGH_CONTRAST", "BEAUTY"]
