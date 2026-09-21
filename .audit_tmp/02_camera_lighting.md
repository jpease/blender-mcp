# Camera & Lighting Tools Audit — Rubric Sections 7–8

**Audit Date:** 2026-09-05  
**Blender Runtime:** Blender 5.2.1 LTS at `/opt/homebrew/bin/blender`  
**Scope:** `src/blender_mcp/server/tools/camera/`, `src/blender_mcp/server/tools/lighting/`, and corresponding addon handlers in `src/blender_mcp/bundled/addon/handlers/camera/` and `.../lighting/`.

---

## Executive Summary

- **Tool Coverage:** 23 camera tools + 15 lighting tools = **38 total tools** across both domains.
- **API Validation:** All load-bearing Blender 5.2 APIs confirmed present and functional: camera DOF (focus_object, focus_distance), light_linking (receiver_collection, blocker_collection), light types/properties, Sky Texture nodes, color management.
- **Reliability:** `create_studio_lighting` demonstrates exemplary orchestration pattern with complete rollback on failure; most tools use defensive validation and state restoration.
- **Boundaries:** Clean separation between camera rigs (hierarchy building) vs. shots (markers/render gate); lighting construction (light objects) vs. environment (world shader graph) vs. rendering (engine/display settings).
- **Missing Capabilities:** Volumetric lighting indirect (via light.volume_factor + EEVEE volumetric settings); all other cinematic, composition, and lighting control workflows present.

---

## Tool Inventory

### Camera Tools (23 total)

| File:Line | Tool | Purpose | Key Params | Abstraction | Verdict |
|-----------|------|---------|-----------|-------------|---------|
| `animation.py:31` | `keyframe_camera_rig` | Animate camera rig with per-owner-type data paths | `owner_type` {OBJECT\|CAMERA_DATA\|CONSTRAINT\|DOF}, `keyframes` list ≤500, `frame_start`/`frame_end` | High; validates owner-specific data_path per type | **KEEP** — necessary for production animation workflows |
| `animation.py:60` | `set_camera_interpolation` | Set keyframe interpolation on one channel/frame range | `channel` (lens, location.x, etc.), `interpolation_type` (LINEAR\|BEZIER\|CONSTANT) | High; operates on existing keys | **KEEP** — idempotent, well-scoped |
| `animation.py:91` | `create_focus_pull` | Animate DOF focus over frame range | `mode` (DISTANCE\|FOCUS_CONTROL), `start_subject`/`end_subject`, `start_frame < end_frame` | High; encodes focus pull cinematography pattern | **KEEP** — production focus-cinematography tool |
| `animation.py:125` | `create_dolly_zoom` | Vertigo effect: camera move + lens zoom | `subject_object_name\|subject_point`, `start_distance`/`end_distance` > 0, `framing_axis` (HORIZONTAL\|VERTICAL) | High; specific cinematic technique | **KEEP** — niche but essential |
| `animation.py:160` | `add_camera_shake` | Procedural deterministic shake on parent control | `translation_strength`/`rotation_strength`, `noise_scale` > 0, `depth` 0-8 | High; preserves authored curves, creates new parent | **KEEP** — non-destructive procedural shaking |
| `core.py:41` | `create_camera` | Create/link camera object in scene | `orientation` source (one of: normal/look_at/orbit/lookat_axes), `optics`/`display` patches, projection validation | Medium; validates exactly one orientation source | **KEEP** — foundational camera creation |
| `core.py:106` | `configure_camera` | Patch optics and display on existing camera | `patch` (optics/display; ≥1 field required), does NOT touch render resolution | High; explicit boundary (render gate is separate) | **KEEP** — non-destructive patching |
| `core.py:160` | `set_scene_camera` | Bind camera to scene + optional marker | `marker_name`/`marker_frame` must pair, `replace_marker` guard | Medium; simple bind | **KEEP** — foundational |
| `core.py:195` | `configure_camera_dof` | Patch DOF (aperture, focus intent) | `focus_intent` one-of (OBJECT/DISTANCE/POINT), creates/reuses focus target, defaults `focus_collection_name="MCP Camera Controls"` | High; manages focus target creation/reuse lifecycle | **KEEP** — essential for DOF workflows |
| `inspection.py:31` | `get_camera_rig_info` | Paginated read-only inventory | `descendant_depth` ≤12, `child_limit`/`child_offset` (bounded), returns transforms/optics/DOF/constraints/drivers/animations/render gate/markers/rig metadata | High; comprehensive snapshot | **KEEP** — diagnostic |
| `inspection.py:59` | `validate_camera_rig` | Read-only structural validation | `object_names` ≤500, `sample_frames` ≤24, validates rig hierarchy consistency | Medium; read-only validation | **KEEP** — pre-edit verification |
| `rigs.py:34` | `create_orbit_camera_rig` | Orbit rig (Empty+Camera+DampedTrack) | `pivot` location, `radius` > 0, `azimuth`/`elevation`/`roll`, tags UUID/role/schema/owner | Medium; building-block hierarchy | **KEEP** — foundational rig |
| `rigs.py:89` | `create_dolly_camera_rig` | Dolly rig (root+height-control+camera) | `rail_direction` (non-zero local metadata), base_height/length > 0, stores direction as metadata | Medium; hierarchy + metadata | **KEEP** — foundational rig |
| `rigs.py:128` | `create_crane_camera_rig` | Crane rig (base/pivot/boom/head/camera hierarchy) | `base_height`/`arm_length` > 0, `elevation`/`pan`/`tilt`/`roll`, parent-safe transforms | Medium; complex hierarchy | **KEEP** — foundational rig |
| `rigs.py:178` | `create_camera_path_rig` | Attach camera to path (curve following) | `curve_object_name\|path_points` (≥2), `start_frame < end_frame`, keys constraint `offset_factor` 0→1 | Medium; constraint-based path following | **KEEP** — production path-animation tool |
| `rigs.py:232` | `match_camera_transform` | Copy transform/optics from source to target | `MatchPolicy` (TRANSFORM_ONLY\|OPTICS_ONLY\|FULL), `source_object_name\|world_transform`, optics requires source camera | High; multi-policy tool | **KEEP** — useful for rig synchronization |
| `rigs.py:287` | `duplicate_camera_rig` | Clone rig with linked/copied data policies | `DataPolicy`/`AnimationPolicy`/`ExternalTargetPolicy` (COPY/LINK/SHARE/REJECT) | High; orchestrates rig duplication with choice of data sharing | **KEEP** — production asset duplication |
| `shots.py:26` | `create_camera_markers` | Manage scene markers (LIST/CREATE/UPDATE/REMOVE) | `MarkerAction` enum, `MarkerEdit` list ≤200, validates LIST rejects edits | High; scoped marker CRUD | **KEEP** — essential shot organization |
| `shots.py:78` | `configure_camera_render_gate` | Patch render resolution / border / safe areas / composition guides | `RenderGatePatch` (resolution 4-65536, %), `RenderBorderPatch`, `SafeAreasPatch` (0-1 tuples), `CameraGuidesPatch` (show_* flags); ≥1 field required | High; complex multi-area patch | **KEEP** — production render-output tool |
| `targeting.py:31` | `point_camera_at` | One-shot rotation (not a constraint) | exactly one of `target_object_name\|target_point`, `subtarget` requires target_object | Medium; simple one-shot | **KEEP** — diagnostic aiming |
| `targeting.py:64` | `create_camera_target` | Create/reuse tagged Empty aim control | exactly one of `location\|target_object_name`, `reuse` opt-in (tagged-only), optional cameras receive DAMPED_TRACK constraint | Medium; control creation + optional constraint binding | **KEEP** — reusable aim control |
| `targeting.py:102` | `frame_camera_on_objects` | Fit objects in camera frame | `FramePolicy` (MOVE_CAMERA\|CHANGE_LENS\|CHANGE_ORTHO_SCALE), `margin` 0-0.9, returns solved distance/optics | High; multi-policy framing | **KEEP** — production framing tool |
| `targeting.py:136` | `add_camera_constraint` | Generic typed constraint tool | 11 constraint types (TRACK_TO/DAMPED_TRACK/LOCKED_TRACK/FOLLOW_PATH/CHILD_OF/COPY_*/LIMIT_*), type-specific validation (targeted vs. limit), exposes all sub-config params | Very High; powerful but complex | **KEEP** — necessary for advanced rigging |

**Camera Summary:** 23 tools, all production-quality. No overlaps; clear domain separation (rigs vs. shots; animation vs. targeting). All KEEP.

---

### Lighting Tools (15 total)

| File:Line | Tool | Purpose | Key Params | Abstraction | Verdict |
|-----------|------|---------|-----------|-------------|---------|
| `construction.py:88` | `create_light` | Create one light (POINT/SPOT/AREA/SUN) | `light_type`, world-space location/rotation, `LightSettings` (energy, exposure, color, temperature, shape, size, spot_size, spread, shadows, factors...) | Medium; validates type-specific settings | **KEEP** — foundational light creation |
| `construction.py:122` | `configure_light` | Patch settings on existing light | `LightPatch` (same fields as LightSettings, all optional), rejects type-mismatched settings, handles shared datablocks | High; defensive applicability checking | **KEEP** — non-destructive patching |
| `construction.py:141` | `aim_light` | Aim light at target (static or live constraint) | exactly one of `target_point\|target_object_name` (optional `target_bone_name`), `bounds_position` (CENTER/TOP/BOTTOM), `method` (STATIC_ROTATION/TRACK_TO/DAMPED_TRACK), live aiming requires `helper_name` | High; multi-method aiming | **KEEP** — essential aiming tool |
| `construction.py:188` | `configure_light_linking` | Set receiver/blocker collections for light | `receiver_collection_name\|blocker_collection_name` (mutually exclusive with clear_* flags), expands effective membership in result, reports engine-support caveats | High; validates API presence, manages linkage lifecycle | **KEEP** — production light-linking tool (Blender 5.2+ only) |
| `construction.py:230` | `create_studio_lighting` | Orchestrate key/fill/rim AREA-light preset rig | `mood` (SOFT/HIGH_CONTRAST/BEAUTY), `key_ratio` (optional), sized to target's evaluated bounds, positioned via camera direction, orchestrates create_light + aim_light, returns rig metadata + previews via render_lighting_preview | Very High; sophisticated orchestration | **KEEP** — exemplary production rig builder |
| `environment.py:40` | `configure_world_background` | Create/patch simple world background | `color`/`strength`/`transparent_film` (at least one required), manages Background/World Output nodes only, reuses unrelated nodes | Medium; non-destructive node patching | **KEEP** — foundational background |
| `environment.py:77` | `configure_hdri_environment` | Attach HDRI image to world via managed node chain | `image_path` (absolute .hdr/.exr), `strength`/`rotation` (radians), `projection` (EQUIRECTANGULAR/MIRROR_BALL), `replacement_policy` (REPLACE_MANAGED/ERROR_IF_MANAGED), reports OCIO color space | High; manages Texture Coordinate → Mapping → Environment → Background → World Output chain, preserves unrelated nodes | **KEEP** — production HDRI tool |
| `environment.py:122` | `configure_procedural_sky` | Configure Sky Texture + optional Sun light sync | `ProceduralSkySettings` (sky_type {MULTIPLE_SCATTERING/SINGLE_SCATTERING/PREETHAM/HOSEK_WILKIE}, sun_elevation/rotation, altitude, air_density, aerosol_density, ozone_density, sun_size/intensity, sun_disc, background_strength), `target_engine` (BOTH/CYCLES/EEVEE), `sync_sun` creates/updates Sun light, warns on sky disc + Sun double-illumination | Very High; complex physics-based sky setup | **KEEP** — production sky tool |
| `inspection.py:14` | `list_lights` | Paginated light inventory (non-destructive) | `collection_name\|light_type` filters optional, `limit` ≤200, `offset` ≤9999, returns full light state (transform, energy, color, constraints, linking, groups, type-specific shape) | High; comprehensive snapshot | **KEEP** — diagnostic |
| `inspection.py:43` | `inspect_light` | Detailed inspection of one light | returns local/world transforms, all shared/type-specific settings, constraints, linking, animation, shader nodes (bounded), external image/IES dependencies, compatibility notes (shared/Cycles-only/EEVEE) | High; deep diagnostic | **KEEP** — pre-edit inspection |
| `inspection.py:58` | `inspect_lighting_setup` | Scene-level lighting snapshot | returns active engine, units, camera, color management, world graph, paginated light inventory, emissive/volume/probe inventories, hidden lights, light links/groups, Cycles/EEVEE quality settings | Very High; comprehensive scene diagnostic | **KEEP** — pre-edit scene understanding |
| `inspection.py:78` | `validate_lighting_setup` | Audit lighting readiness (non-destructive) | `target_engine` (BOTH/CYCLES/EEVEE), optional `subject_object_names` ≤100, paginated findings (severity/code/evidence/remediation) on camera/engine availability, power extremes, coincident lights, disabled shadows, broken files, linking, scale/exposure, EEVEE probe risks, directional aim-away detection | Very High; comprehensive quality audit | **KEEP** — production validation tool |
| `rendering.py:54` | `configure_lighting_quality` | Patch render engine quality presets | `preset` (PREVIEW/BALANCED/FINAL) or explicit `cycles`/`eevee` patches, `target_engine` (CYCLES/EEVEE/BOTH), validates applicability, never touches output size/path/color management/camera/light energy | High; scoped engine settings | **KEEP** — production quality tuning |
| `rendering.py:93` | `configure_color_management` | Set display transform and exposure | `view_transform` (validated at setattr time against OCIO), `look`, `exposure` (±32 stops), `gamma` > 0, reports 2^exposure multiplier, does not adjust light energy | Medium; defensive setattr validation | **KEEP** — production color workflow |
| `rendering.py:153` | `render_lighting_preview` | Render bounded still or Cycles/EEVEE comparison | `target_engine` (CYCLES/EEVEE/BOTH), dimensions 16-1024, samples ≤1024 (Cycles >64 requires confirm_long_render), output paths optional (temp PNG inline or explicit absolute .png), returns Image list + envelope, cleans temp files | Very High; bounded rendering with dual-engine comparison | **KEEP** — production preview tool |

**Lighting Summary:** 15 tools, all production-quality. Clean domain separation. All KEEP.

---

## Runtime API Validation

### Test Environment
- **Blender:** 5.2.1 LTS at `/opt/homebrew/bin/blender`
- **Command:** `/opt/homebrew/bin/blender --background --factory-startup --python /path/to/check_script.py`

### 1. Camera DOF Properties

**Verified Present (via `CameraDOFSettings`):**
- `use_dof` (bool)
- `focus_object` (Object reference)
- `focus_distance` (float)
- `aperture_fstop` (float)
- `aperture_blades` (int)
- `aperture_rotation` (float)
- `aperture_ratio` (float)

**Status:** ✅ **All properties present and functional.** Code usage in `src/blender_mcp/bundled/addon/handlers/camera/core.py:260–284` correctly assigns/restores focus_object and focus_distance.

---

### 2. Light Types & Properties

**Test Results:**

| Light Type | Present Properties | Missing Properties | Type-Specific? |
|------------|-------------------|-------------------|---|
| POINT | energy, color, shadow controls, diffuse/specular/transmission/volume factors, custom distance, soft falloff, temperature, normalization, exposure | angle, spread, shape, size, spot_size, spot_blend, show_cone | ✅ Correct |
| SUN | energy, color, shadow controls, factors, custom distance, soft falloff, angle, temperature, normalization, exposure | spread, shape, size, spot_size, spot_blend, show_cone, use_soft_falloff | ✅ Correct |
| SPOT | energy, color, shadow, factors, custom distance, soft falloff, spot_size, spot_blend, show_cone, temperature, normalization, exposure | angle, spread, shape, size | ✅ Correct |
| AREA | energy, color, shadow, factors, custom distance, soft falloff, spread, shape, size, temperature, normalization, exposure | angle, spot_size, spot_blend, show_cone, use_soft_falloff | ✅ Correct |

**Status:** ✅ **All type-specific property assignments verified.** Code in `src/blender_mcp/server/tools/lighting/_shared.py:LightSettings` (line 16–51) and `src/blender_mcp/bundled/addon/handlers/lighting/construction.py:validate_light_patch` (line 366+) correctly enforce type-specific applicability.

---

### 3. Light Linking (Critical for Cycles/EEVEE Selectivity)

**Test Results:**
```
Object has light_linking: True
light_linking has receiver_collection: True
light_linking has blocker_collection: True
SET receiver_collection OK, now: <bpy_struct, Collection("TestLinkColl")>
```

**Status:** ✅ **`bpy.types.Object.light_linking` is fully present in Blender 5.2.** Receiver and blocker collection assignments work correctly. Code in `src/blender_mcp/bundled/addon/handlers/lighting/construction.py:338–376` (`configure_light_linking` handler) correctly wraps this API with fallback error if unavailable (line 350–352).

---

### 4. World Background & Sky Texture Nodes

**Verified Present:**
- `ShaderNodeBackground`, `ShaderNodeTexEnvironment`, `ShaderNodeTexSky`
- Sky node properties: `sky_type`, `sun_disc`, `sun_size`, `sun_intensity`, `sun_elevation`, `sun_rotation`, `altitude`, `air_density`, `ozone_density`
- **Important:** `dust_density` property does NOT exist in Blender 5.2; renamed to `aerosol_density`.
- Color management: `view_transform` (dynamic enum, not static), `look`, `exposure`, `gamma`

**Status:** ✅ **Mostly correct.** Code in `src/blender_mcp/bundled/addon/handlers/lighting/environment.py:353` correctly maps `dust_density` → `aerosol_density` via field_map. However, **MCP tool `ProceduralSkySettings` (server-side, line 24) still uses `dust_density`** — this creates a minor impedance mismatch: the MCP tool exposes a parameter name that doesn't match the native Blender API. The adapter layer correctly translates at `src/blender_mcp/bundled/addon/handlers/lighting/environment.py:353, 360, 432`, but clients debugging via Blender inspect UI will see "aerosol_density" and be confused.

---

### 5. Color Management (View Transform & Look)

**Quirk Discovered:** Blender 5.2 enum introspection returns static `['NONE']` but runtime setattr validates against the real OCIO palette.

```python
# Static introspection returns:
view_transform enum items: ['NONE']

# Runtime setattr accepts (and validates):
'Standard', 'ACES 1.3', 'ACES 2.0', 'Khronos PBR Neutral', 'AgX', 'Filmic', 'Filmic Log', 'False Color', 'Raw'

# Invalid values are correctly rejected:
TypeError: enum "NotARealTransform" not found in (...)
```

**Status:** ✅ **Correctly handled.** Code in `src/blender_mcp/bundled/addon/handlers/lighting/rendering.py:226` uses `patch_properties` which applies setattr-time validation (line 357), correctly rejecting invalid view_transform/look values. **The MCP tool `configure_color_management` (server-side, line 94–120) cannot enumerate valid options at the MCP boundary** because static introspection is broken, but this is acceptable: Blender's runtime validation is correct, and clients should supply known-good values.

---

## Reliability Review

### `create_studio_lighting` Orchestration Pattern

**Location:** `src/blender_mcp/bundled/addon/handlers/lighting/construction.py:384–514`

**Validation Phase (lines 400–428):**
1. Scene/target/camera object existence checks
2. Camera type assertion
3. Mood enum validation
4. Positive key_ratio check
5. Name collision pre-check (all 3 lights + 3 light datablocks must not exist)
6. Bounds evaluation; degenerate bounds rejection

**Mutation Phase with Rollback (lines 444–502):**
```python
created_roles = []  # Track successful creations
try:
    for role in ("key", "fill", "rim"):
        # ... compute settings ...
        created = self.create_light(...)  # May raise
        created_roles.append(role)        # Only mark after success
        self.aim_light(...)               # May raise
        lights.append(...)                # Collect metadata
except Exception:
    # COMPLETE ROLLBACK:
    for role in created_roles:
        obj = bpy.data.objects.get(...)   # Safe lookup
        if obj:
            bpy.data.objects.remove(obj, do_unlink=True)
            if data and data.users == 0:
                bpy.data.lights.remove(data)  # Clean dangling datablocks
    # Collection cleanup (only if collection was created, not pre-existing)
    if collection is not None and not collection_was_in_scene:
        linked = scene.collection.children.get(collection.name)
        if linked == collection:
            scene.collection.children.unlink(collection)
    if collection is not None and not collection_existed and collection.users == 0:
        bpy.data.collections.remove(collection)
    raise  # Re-raise to MCP envelope
```

**Result (lines 504–514):**
- Returns rig names, target, camera, mood, key_ratio, collection, light list with transform snapshots
- Changed objects/resources tracked correctly

**Verdict:** ⭐ **EXCELLENT.** This is the gold-standard reliability pattern for multi-step orchestration:
- Validates all inputs before any mutation
- Creates incrementally; tracks successes
- On any failure, completely reverses all changes
- Cleans up dangling datablocks (object deletion without datablock cleanup = resource leak)
- Collection creation is tracked separately (only rolls back if tool created it, not if pre-existing)
- Does not mask the original exception

**This pattern should be a template for other multi-step tools** in the codebase.

---

### State Restoration Patterns (Sample Audit)

| Handler | Pattern | File:Lines | Assessment |
|---------|---------|-----------|------------|
| `camera/core.py::configure_camera_dof` | Snapshots old focus_object/focus_distance; restores on exception | 260–289 | ✅ Correct try/finally restoration |
| `lighting/construction.py::aim_light` | Snapshots constraints before mutation; no explicit rollback (relies on exception propagation) | ~300+ | ⚠️ Acceptable; constraints are additive, but no snapshot-restore; see if inverse operations exist |
| `camera/rigs.py::create_dolly_camera_rig` | Creates hierarchy; validates inputs upfront; no explicit rollback (creation is atomic or all-or-nothing) | ~100–180 | ⚠️ Create-only tools don't need rollback, but should validate before starting |

**Overall:** ✅ **Most tools follow correct patterns.** Exception-based rollback or validate-before-mutate are the standard.

---

## Missing Capabilities Analysis

### 1. Volumetric Lighting
**Status:** ⚠️ **Indirect only.** No dedicated volumetric tools, but:
- Light `volume_factor` (0-1) is exposed in `LightSettings` and can be patched via `configure_light`
- EEVEE volumetric settings (tile size, samples, ray depth) are exposed in `rendering.py:EeveeLightingQuality` (lines 37–51)
- No explicit volumetric material nodes or fog volume tools

**Verdict:** Functional for basic volumetric workflows; no smoke/fog object creation tool.

### 2. HDRI Environment Lighting
**Status:** ✅ **Complete.** `configure_hdri_environment` (environment.py:77–119):
- Loads absolute .hdr/.exr paths
- Manages Texture Coordinate → Mapping → Environment → Background → World Output chain
- Supports rotation, strength, projection (EQUIRECTANGULAR/MIRROR_BALL)
- Preserves unrelated user nodes

**Verdict:** Production-ready.

### 3. Procedural Sky + Sun Sync
**Status:** ✅ **Complete.** `configure_procedural_sky` (environment.py:122–160):
- Full Sky Texture node setup (4 sky models: MULTIPLE_SCATTERING, SINGLE_SCATTERING, PREETHAM, HOSEK_WILKIE)
- Physical atmosphere controls (altitude, air_density, aerosol_density, ozone_density)
- Optional Sun light sync with dual-illumination warning

**Verdict:** Production-ready.

### 4. Light Linking (Per-Collection Selectivity)
**Status:** ✅ **Complete.** `configure_light_linking` (construction.py:188–227):
- Sets receiver_collection (which objects receive this light)
- Sets blocker_collection (which objects block this light)
- Supports Cycles 5.2+ and EEVEE 5.2+

**Verdict:** Production-ready.

### 5. Cinematic Camera Workflows
**Status:** ✅ **Complete.**
- **Orbit rig:** `create_orbit_camera_rig` (rigs.py:34–85)
- **Dolly rig:** `create_dolly_camera_rig` (rigs.py:89–125)
- **Crane rig:** `create_crane_camera_rig` (rigs.py:128–175)
- **Path-following rig:** `create_camera_path_rig` (rigs.py:178–230)
- **Focus pull:** `create_focus_pull` (animation.py:91–122)
- **Dolly zoom (Vertigo):** `create_dolly_zoom` (animation.py:125–158)
- **Camera shake:** `add_camera_shake` (animation.py:160–191)

**Verdict:** Comprehensive cinematic toolkit.

### 6. DOF & Focus Management
**Status:** ✅ **Complete.**
- Camera DOF configuration: `configure_camera_dof` (core.py:195–312)
- Focus target creation/reuse: `create_camera_target` (targeting.py:64–99)
- Focus pull animation: `create_focus_pull` (animation.py:91–122)
- All DOF properties (aperture, blades, rotation, ratio, focus_distance, focus_object)

**Verdict:** Production-ready.

### 7. Product Photography Lighting Presets
**Status:** ✅ **Complete.** `create_studio_lighting` (construction.py:230–284):
- Three moods: SOFT, HIGH_CONTRAST, BEAUTY
- Key/fill/rim AREA-light preset rig
- Proportional to target bounds
- Aimed relative to camera direction

**Verdict:** Production-ready preset system.

### 8. Exposure & Color Management
**Status:** ✅ **Complete.** `configure_color_management` (rendering.py:93–120):
- View transform (AgX, ACES, Filmic, etc.)
- Look (OCIO-dependent)
- Exposure (±32 stops)
- Gamma (>0)

**Verdict:** Production-ready.

### 9. Lighting Quality Presets & Optimization
**Status:** ✅ **Complete.** `configure_lighting_quality` (rendering.py:54–90):
- Three presets: PREVIEW, BALANCED, FINAL
- Cycles: samples, adaptive sampling, denoising, light sampling, bounces, light paths
- EEVEE: render samples, shadow resolution, ray-tracing, fast GI, volumetric settings

**Verdict:** Production-ready optimization toolkit.

---

## Boundary & Composability Assessment

### Camera Domain: Rigs vs. Shots

| Aspect | Rigs (`rigs.py`) | Shots (`shots.py`) | Overlap? |
|--------|-----------------|-----------------|---------|
| **Scope** | Build reusable rig hierarchies (Empty+Camera+constraints) with metadata tagging | Manage scene markers and render output configuration | ❌ None |
| **Mutation** | Creates/duplicates parent hierarchies | Updates markers; patches render resolution/border/guides | ❌ None |
| **Data** | Rig UUID, role, schema version, owner (tagged on root) | Marker frame/camera, render gate settings | ❌ None |
| **Idempotency** | Rig creation is one-shot; duplication is explicit | Marker CRUD is idempotent (same edit applied twice = same result) | ✅ Good |
| **Coupling** | Rigs can be used independently; no shot requirement | Shots reference rigs by name but don't create them | ✅ Clean dependency |

**Verdict:** ✅ **Cleanly separated.** Rigs are building blocks; shots are organizational containers. No overlap.

---

### Lighting Domain: Construction vs. Environment vs. Rendering

| Aspect | Construction | Environment | Rendering | Overlap? |
|--------|-------------|-------------|-----------|---------|
| **Scope** | Create/configure lights; aim; link to collections | Configure world background/HDRI/sky; manage world shader graph | Configure engine quality; color management; render previews | ❌ None |
| **Mutation** | Object creation, light datablock patching, constraint binding, collection membership | World material nodes, Background/Environment nodes | Scene render settings, scene.view_settings | ❌ None |
| **Idempotency** | `configure_light` is idempotent; `create_light` is one-shot | `configure_hdri_environment` with `REPLACE_MANAGED` is idempotent; `configure_world_background` is idempotent | `configure_lighting_quality` and `configure_color_management` are idempotent | ✅ Good |
| **Dependencies** | Independent; can be used without environment/rendering tools | Independent; can be used without construction tools | Reads lights/world; does not mutate them | ✅ Clean one-way |

**Verdict:** ✅ **Cleanly separated.** Construction mutates light objects; environment mutates world shader graph; rendering configures display/engine without mutation. No overlap.

---

## Findings Summary

### Strengths
1. ✅ **Complete tool coverage:** 38 tools across camera and lighting cover production workflows end-to-end (creation, configuration, aiming, rigging, animation, composition, output, quality, color).
2. ✅ **Robust API validation:** All load-bearing Blender 5.2 APIs (DOF, light_linking, Sky Texture, color management) verified functional; defensive runtime validation used where static introspection fails.
3. ✅ **Exemplary orchestration:** `create_studio_lighting` demonstrates gold-standard reliability (validate-upfront, mutate-incrementally, rollback-completely-on-failure).
4. ✅ **Clean boundaries:** Camera rigs/shots, lighting construction/environment/rendering cleanly separated; no overlaps; one-way dependencies.
5. ✅ **Cinematic toolkit:** Orbit, dolly, crane, path rigs; focus pulls; dolly zoom; camera shake; all non-destructive.
6. ✅ **Lighting selectivity:** Light linking (receiver/blocker collections) fully implemented and functional for Cycles/EEVEE selectivity.

### Weaknesses / Minor Issues
1. ⚠️ **API impedance mismatch:** MPC tool `ProceduralSkySettings.dust_density` → adapter maps to Blender's `aerosol_density`. Clients inspecting Blender's UI will see the native name and be confused. **Recommendation:** Add deprecation note or rename parameter in next major version.
2. ⚠️ **Color management enum limitation:** Static introspection returns `['NONE']`; runtime setattr validates against real OCIO palette. MCP clients cannot enumerate valid options at the boundary. **Not a correctness issue; acceptable trade-off; consider documenting known view_transform values in tool docstrings.**
3. ⚠️ **Volumetric lighting:** Only indirect via light.volume_factor and EEVEE volumetric settings; no smoke/fog object creation or volume material tools. **Out of scope for camera/lighting audit, but future enhancement.**

### Single Biggest Finding

**`create_studio_lighting` demonstrates a sophisticated and reliable orchestration pattern for multi-step, reversible scene mutations: validate all inputs before any change; create incrementally while tracking successes; on any failure, completely roll back all changes (including dangling datablock cleanup); do not mask the original exception. This pattern ensures that partial failures leave the scene unchanged and is the gold standard for production reliability. All other multi-step tools in the codebase should follow this template.**

---

## Conclusion

Camera and Lighting tools comprehensively cover production workflows. API coverage is complete and correct for Blender 5.2. Reliability patterns are mature (validate-upfront, state-restore-on-error, orchestration rollback). Boundaries are clean. Ready for production use.

**Rubric Sections 7–8 Verdict:** ✅ **PASS** — comprehensive, robust, production-ready.

---

## Verification pass (2026-09-21)

The findings above were written against the tree of early September. This pass re-reads every
load-bearing claim against the tree as it stands today and records the result. Nothing above was
edited; this section supersedes it where the two disagree.

**Scope note on runtime claims.** No Blender process was launched during this pass — the live
Blender slot was in use for an unrelated measurement. Claims that rest on the original headless
introspection run are therefore not re-executed here; each is instead resolved against the
standing smoke suite, which runs the same APIs through a real Blender in `just smoke`, and is
marked UNVERIFIABLE where no such coverage exists.

### Tool total, freshly counted

Counted by parsing every `@mcp.tool()`-decorated coroutine in the two packages and checking each
name against `mcp.list_tools()` after import:

| Module | Tools |
|---|---|
| `server/tools/camera/animation.py` | 5 |
| `server/tools/camera/core.py` | 4 |
| `server/tools/camera/inspection.py` | 2 |
| `server/tools/camera/shots.py` | 2 |
| `server/tools/camera/targeting.py` | 4 |
| `server/tools/camera/rigs.py` | 6 |
| `server/tools/lighting/construction.py` | 5 |
| `server/tools/lighting/environment.py` | 3 |
| `server/tools/lighting/inspection.py` | 4 |
| `server/tools/lighting/rendering.py` | 3 |
| **Total** | **23 camera + 15 lighting = 38** |

The headline count is unchanged and all 38 register on the app. **But the count is no longer the
surface an agent sees.** The domain is split across four bundles (`server/bundles.py:36-37`,
`53-56`) and the `shot` mode (`server/bundles.py:85`) omits `camera.rigs` entirely: 32 of the 38
are exposed by default, and the six rig builders this slice praises as the cinematic toolkit
require the opt-in `camera-rigs` bundle. Every line number quoted in the inventory tables above
is stale by 20-30 lines; the tables' *contents* are accurate.

### Claim-by-claim result

| # | Claim (line above) | Result | Evidence in the current tree |
|---|---|---|---|
| 1 | 23 camera + 15 lighting = 38 tools (11, 21, 53) | **VERIFIED** | Counted above; all 38 present in `mcp.list_tools()`. Incomplete as stated: 6 are opt-in, `server/bundles.py:35-37`, `85` |
| 2 | `create_studio_lighting` validates everything before any mutation; complete rollback (13, 164-212, 346, 358) | **STALE — now false at the tool level** | The handler's guard is intact (`bundled/addon/handlers/lighting/construction.py:419-424`, `446-504`), but the tool is now two-phase: rig dispatch at `server/tools/lighting/construction.py:263-275`, then a **mandatory** preview at `276-288`. The preview's own argument checks run only there — `server/tools/lighting/rendering.py:197-198` and `142-166` — i.e. after the lights exist. See the worked case below |
| 3 | `aim_light` has "no explicit rollback; relies on exception propagation" (221) | **STALE — improved** | It now reverses everything: created constraint removed, pre-existing constraint fields restored, created helper removed, pre-existing helper matrix restored (`handlers/lighting/construction.py:307-318`) |
| 4 | `configure_camera_dof` snapshots and restores focus_object/focus_distance (220) | **VERIFIED — understated** | `handlers/camera/core.py:259-294` also restores every patched DOF field, restores a reused focus target's world matrix, and removes a focus Empty it created |
| 5 | Camera rig builders validate up front, no rollback needed (222) | **VERIFIED, with a gap the slice missed** | Scalars are validated before any creation (`handlers/camera/rigs.py:88-95`), but there is no name pre-flight: `_new_empty` and `_new_camera_object` call `bpy.data.objects.new` (`handlers/camera/_shared.py:498`, `handlers/camera/rigs.py:41`), so a second rig under the same `rig_name` silently yields `.001` members instead of an error. `create_studio_lighting` refuses (`handlers/lighting/construction.py:419-424`) and `create_camera_target` requires opt-in `reuse` (`handlers/camera/targeting.py:200-201`); the rig builders do neither |
| 6 | Camera DOF properties present and correctly assigned (94) | **VERIFIED (code side)** | `_DOF_FIELDS` covers `use_dof`/`aperture_*` (`handlers/camera/_shared.py:305-309`); assignment and restore at `handlers/camera/core.py:241-294`. RNA presence itself not re-introspected this pass |
| 7 | Type-specific light property applicability enforced (109) | **VERIFIED (code side)** | `LightSettings`/`LightPatch` allowlists at `server/tools/lighting/construction.py:16-85`; per-type applicability enforced handler-side before assignment |
| 8 | `Object.light_linking` present and correctly wrapped (123) | **VERIFIED, by proxy** | Capability check and both-collection rollback at `handlers/lighting/construction.py:351-373`; exercised against real Blender at `tests/blender_lighting_smoke.py:97-98` |
| 9 | `dust_density` → `aerosol_density` adapter mapping is correct (135, 352) | **UNVERIFIABLE — and untested** | The map still exists at `handlers/lighting/environment.py:356`, with the same literal duplicated inline at `435`. `dust_density`/`aerosol_density` appear **nowhere in `tests/`**, and the sky smoke call omits the field (`tests/blender_lighting_smoke.py:141-156`). The one rename the slice flagged as an API risk is the one translation nothing checks |
| 10 | Server-side `ProceduralSkySettings` still exposes `dust_density` (135) | **VERIFIED** | `server/tools/lighting/environment.py:24`, also in the cross-field validator at `38` |
| 11 | `view_transform` validated at setattr time, not enumerable at the boundary (154, 353) | **VERIFIED** | Boundary takes a bare `str` (`server/tools/lighting/rendering.py:113`); real validation is Blender's. Exercised at `tests/blender_lighting_smoke.py:213-215` |
| 12 | Camera domain: rigs vs. shots, "❌ None" overlap (49, 318-324) | **STALE — false** | All nine fields of `_CAMERA_GUIDES` (`handlers/camera/shots.py:15-25`) are a subset of `_CAMERA_DISPLAY` (`handlers/camera/_shared.py:23-38`). `configure_camera(display=…)` and `configure_camera_render_gate(guides=…)` write the same nine camera-data properties, and neither docstring mentions the other (`server/tools/camera/core.py:125-130`, `server/tools/camera/shots.py:112`) |
| 13 | Lighting domain: construction / environment / rendering independent (335-337) | **STALE — false for construction** | `construction.py` imports and calls `rendering.render_lighting_preview` (`server/tools/lighting/construction.py:13`, `276`); `server/bundles.py:54-56` documents having to list `lighting.rendering` in `lighting-construction` because of it. Construction cannot be loaded without rendering |
| 14 | "Clean boundaries … no overlaps" as a domain verdict (14, 347) | **STALE — three cross-domain duplications** | (a) `RenderGatePatch` (`server/tools/camera/shots.py:26-30`) duplicates five `RenderSettingsPatch` fields with identical bounds (`server/tools/rendering.py:35-39`), both writing `scene.render`; (b) `CyclesLightingQuality`/`EeveeLightingQuality` (`server/tools/lighting/rendering.py:21-24`, `46-47`) duplicate `CyclesPatch`/`EeveePatch` (`server/tools/rendering.py:134-140`, `170-171`); (c) `configure_world_background(transparent_film=…)` (`server/tools/lighting/environment.py:50`) duplicates `film_transparent`/`FilmPatch.transparent` (`server/tools/rendering.py:45`, `84`). The audit only compared within the domain; the render domain has since grown into it |
| 15 | `create_camera` is a sound foundational tool (30) | **STALE — reachable un-rolled-back partial mutation** | The camera datablock and object are created and linked at `handlers/camera/core.py:107-109`, *before* `_validate_optics` (`111`) and `_look_quaternion` (`122`) can raise. Both are reachable: `panorama_type requires projection='PANO'` (`handlers/camera/core.py:56`) and "Camera and aim target cannot occupy the same world position" (`handlers/camera/_shared.py:358`). Neither path is wrapped, so the failure returns an error envelope and leaves an orphan camera object plus an orphan `<name> Data` camera datablock |
| 16 | Client input validated at the MCP boundary (implicit throughout) | **STALE for one field** | `panorama_type: str \| None` (`server/tools/camera/core.py:33`) is the domain's only un-enumerated enum, and nothing checks it against `projection`. Executed at the boundary: both `panorama_type="totally-made-up"` and the contradictory `projection="PERSP"` + `panorama_type=…` pair were accepted and forwarded verbatim in the `optics` payload; only the handler rejects the latter, and only after creation. Contrary to `AGENTS.md:69` |
| 17 | Volumetric lighting is indirect-only, no fog/volume authoring (15, 231-236, 354) | **VERIFIED** | `volume_factor` at `server/tools/lighting/construction.py:29`, `67`; EEVEE volumetrics at `server/tools/lighting/rendering.py:49-51`. The only new volume capability is `VolumeGeometry` (`server/tools/scene_authoring.py:272-279`), which imports an OpenVDB file in another domain — it is not lighting authoring. The master rubric's D bullet "volumetric ✅" (`COMPREHENSIVE_AUDIT_FINDINGS.md:102`) is not supported by this domain |
| 18 | HDRI / procedural sky / light linking / cinematic rigs / DOF / studio presets / color management / quality presets complete (239-308) | **VERIFIED** | Tools and handlers all present as described; smoke coverage for sky, HDRI, studio rig, quality and preview at `tests/blender_lighting_smoke.py:107-206` |
| 19 | `validate_lighting_setup` is a comprehensive audit (68) | **VERIFIED** | 15 finding codes, from `MISSING_CAMERA` to `EXTREME_DISPLAY_EXPOSURE`, including an explicit cross-engine-difference INFO (`handlers/lighting/inspection.py:290-490`) |
| 20 | `validate_camera_rig` is structural pre-edit verification (35) | **VERIFIED — understated** | 18 finding codes including parent cycles, driver targets, and evaluated-depsgraph aim/focus-behind-camera checks, plus an honest scope disclaimer that visual correctness was not inferred (`handlers/camera/inspection.py:155-423`) |

### The `create_studio_lighting` regression, worked

Calling the tool with `preview_engine="CYCLES", preview_samples=128` and no `confirm_long_render`,
or with a preview path that is relative or not `.png`, dispatches `create_studio_lighting` to
Blender and *then* raises `ToolError` from the preview layer. The caller receives a failure while
three AREA lights, three light datablocks and possibly a new `Studio Lighting` collection remain
in the scene. Worse, the retry is blocked: the handler refuses a rig whose member names already
exist (`handlers/lighting/construction.py:419-424`), so the same call cannot
succeed until the user deletes the lights by hand. Every one of these checks
(`server/tools/lighting/rendering.py:142-166`, `197-198`) is a pure argument check that could run
before the rig dispatch at `server/tools/lighting/construction.py:263-275`.

All three cases were executed against the tool functions with a stub Blender connection, so this
is observed dispatch order rather than inference: in each one the recorded command list was
`['create_studio_lighting']` and the call then raised `Cycles previews above 64 samples require
confirm_long_render=true` / `EEVEE output path must be an absolute .png path`.

This matters out of proportion to its size, because "validate all inputs before any mutation" is
precisely the property that made this tool the codebase's cited reliability template (line 212,
`COMPREHENSIVE_AUDIT_FINDINGS.md:62`). The handler still deserves that praise. The tool no longer
does.

### Findings the earlier pass did not record

- **Light groups are reported but not authorable.** `light_group` is published in every
  `inspect_light` / `list_lights` detail record (`handlers/lighting/_shared.py:323`), and that read
  is the *only* occurrence of `lightgroup` in `src/`. Cycles light-group AOVs, the standard way to
  split a lighting render for compositing, cannot be set up through this domain.
- **The six rig replies are outside the reply-byte budget catalog.** `scripts/measure_reply_sizes.py`
  carries a representative payload for every other tool in the domain (camera block `1157-1364`,
  lighting block `1365-1596`) but none for `create_orbit_camera_rig`, `create_dolly_camera_rig`,
  `create_crane_camera_rig`, `create_camera_path_rig`, `match_camera_transform` or
  `duplicate_camera_rig`. Their `_rig_result` shape lists every rig member and constraint
  (`handlers/camera/rigs.py:521-525`), the widest fan-out in the domain, so these are the replies
  least safe to leave unmeasured.
- **`configure_camera_dof` does not enable DOF.** It will set a focus intent while `use_dof` stays
  false unless the caller passes it in the patch (`server/tools/camera/core.py:64`,
  `handlers/camera/core.py:241-284`), producing no visible depth of field and no warning saying so —
  while `create_focus_pull` sets `use_dof = True` for the caller (`handlers/camera/animation.py:409`,
  `428`). The domain is inconsistent about who owns the switch.

### Reliability patterns that hold up

Worth recording, because they are the domain's real strength and the slice above only gestured at
them: `_patch_values` (`handlers/camera/_shared.py:362-381`) reverses in order every property it
assigned before re-raising, so `configure_camera`, `configure_camera_render_gate` and the optics
path of `create_camera` are each atomic with respect to property writes. On top of that,
`configure_camera` restores optics if the display patch fails (`handlers/camera/core.py:144-149`),
`set_scene_camera` restores the previous scene camera and either removes a created marker or
restores a mutated one (`handlers/camera/core.py:198-210`), `configure_camera_render_gate` restores
all four patch areas (`handlers/camera/shots.py:181-190`), and `configure_light_linking` restores
both collections (`handlers/lighting/construction.py:370-373`).

---

## Score estimate

**Score estimate**: 7/10 (breadth and validation tooling are genuinely top-tier; the orchestration
pattern that earned the perfect preliminary score has regressed, four "no overlap" boundaries are
false, and the most foundational tool in the domain leaks datablocks on a reachable error)

| Category | Score | Evidence |
|---|---|---|
| **Tool completeness** | 9/10 | 38 tools freshly counted and all registered; rigs, animation, targeting, composition, construction, environment, quality, colour, preview all present. Gaps: no light-group authoring (`handlers/lighting/_shared.py:323`), no volume/fog lighting authoring (`server/tools/lighting/rendering.py:49-51` is the ceiling) |
| **Reliability** | 6/10 | `_patch_values` atomicity and the DOF/marker/linking/gate restore paths are exemplary (`handlers/camera/_shared.py:362-381`, `handlers/camera/core.py:198-210`, `259-294`). Against that: `create_studio_lighting` mutates before validating its own preview arguments and blocks its own retry (`server/tools/lighting/construction.py:263-288`, `handlers/lighting/construction.py:419-424`), and `create_camera` orphans an object and a datablock on a reachable validation error (`handlers/camera/core.py:107-122`) |
| **Boundaries & redundancy** | 6/10 | Within the domain, `configure_camera(display=)` and `configure_camera_render_gate(guides=)` write the same nine properties (`handlers/camera/_shared.py:23-38` ⊇ `handlers/camera/shots.py:15-25`). Across domains, three more duplications with `server/tools/rendering.py` (resolution/aspect, engine sampling, film transparency). `lighting.construction` also hard-depends on `lighting.rendering` (`server/tools/lighting/construction.py:13`) |
| **Validation** | 9/10 | `validate_camera_rig` 18 codes with evaluated-transform checks and an honest scope note (`handlers/camera/inspection.py:155-423`); `validate_lighting_setup` 15 codes including cross-engine caveats (`handlers/lighting/inspection.py:290-490`). Deducted only for the unvalidated `panorama_type` string at the boundary (`server/tools/camera/core.py:33`) |
| **Test & budget discipline** | 7/10 | Three live smoke scripts cover the domain's load-bearing APIs (`tests/blender_lighting_smoke.py`, `tests/blender_camera_smoke.py`, `tests/blender_camera_framing_smoke.py`), and 32 of 38 replies carry a measured sample payload. The `dust_density`→`aerosol_density` translation has no test at all, and the six rig replies have no sample |
| **Agentability** | 7/10 | Docstrings state spaces, boundaries and confirmations well, and `create_studio_lighting` is still the right shape of one-call preset. But the cinematic rig toolkit is withheld from the default `shot` mode (`server/bundles.py:35-37`, `85`), the four overlapping tools give an agent two ways to set resolution and guides with no cross-reference, and `configure_camera_dof` silently leaves DOF off |

### Mapping onto rubric categories A-J

| Category | Direction from this domain | Basis |
|---|---|---|
| **A. Architecture & Abstraction (15)** | **Down.** The A entry cites "Camera/Lighting: 38 tools, exemplary orchestration ✅" (`COMPREHENSIVE_AUDIT_FINDINGS.md:78`); the orchestration claim no longer holds, and the domain now contributes redundancy rather than only clean structure | Claims 2, 12, 13, 14 above |
| **B. Tool Quality & Redundancy (15)** | **Down.** B's evidence list has no camera/lighting redundancy entry. There are four: guides/display, render gate/render settings, lighting quality/engine patches, transparent film | Claim 12, 14 |
| **C. Scene & Asset Pipeline (10)** | **Neutral.** No camera/lighting bearing either way; `VolumeGeometry` belongs to scene authoring | Claim 17 |
| **D. Lighting & Camera (10)** | **Down, materially.** See verdict below | Claims 1, 2, 12, 14, 15, 17 |
| **E. Animation/Rigging/Simulation (10)** | **Slightly down.** Camera animation (focus pull, dolly zoom, shake, interpolation) is solid, but the camera *rig* builders are opt-in and have no member-name pre-flight | Claims 1, 5 |
| **F. Rendering (15)** | **Neutral to slightly down.** `render_lighting_preview` is well-bounded and cleans its temp files (`server/tools/lighting/rendering.py:226-231`), but it now overlaps `render_scene` in capability and duplicates engine-quality surface | Claim 14 |
| **G. Compositing (5)** | **Down.** G scores 1/5 for having no authoring. Light groups being readable but not writable (`handlers/lighting/_shared.py:323`) closes off the per-light AOV split that compositing needs, so this domain reinforces the gap rather than mitigating it | New finding |
| **H. Validation & Reliability (10)** | **Split.** Validation pulls up hard — 33 finding codes across the two validators, with honest scoping. Reliability pulls down — two reachable partial-mutation paths | Claims 15, 19, 20; claim 2 |
| **I. Agentability & NL (5)** | **Down.** I already docks for confusing multiplicity. This domain adds two ways to set composition guides, two ways to set resolution, two ways to set engine samples, and hides the rig builders from the default mode | Claims 12, 14; bundle finding |
| **J. Production Completeness (5)** | **Neutral.** J's "camera, lighting … core complete ✅" (`COMPREHENSIVE_AUDIT_FINDINGS.md:149`) still stands on coverage; light-group authoring is the one addition worth listing as missing | Claim 1; new finding |

### Verdict on D = 10/10

**D = 10/10 does not survive verification. It should be 7/10.**

D's preliminary 10/10 rests on four bullets (`COMPREHENSIVE_AUDIT_FINDINGS.md:99-104`). Re-checked:

1. *"Camera: 23 tools covering rigs/animation/composition ✅✅"* — count holds, but 6 of the 23 are
   withheld from the default `shot` mode (`server/bundles.py:35-37`, `85`).
2. *"Lighting: 15 tools covering construction/environment/quality ✅✅"* — holds.
3. *"Light linking, volumetric, HDRI, procedural sky ✅"* — three of four hold. **"Volumetric" was
   never true**: this slice itself recorded volumetrics as indirect-only (line 231), and there is
   still no volume or fog authoring in the domain.
4. *"Exemplary orchestration ✅✅"* — **fails.** The property being praised, validate-everything-then-
   mutate, is no longer what the tool does, and the resulting partial mutation cannot be retried
   without manual cleanup.

Two and a half of four bullets survive, and verification additionally surfaced a datablock leak in
`create_camera`, an unvalidated enum at the MCP boundary, four duplicated boundaries, and an
untested API translation. A 10/10 would mean a domain with nothing to fix; this one has a concrete
list. 7/10 is the score the evidence supports — level with the materials and animation slices
rather than above every other domain, and the deductions are all repairable without new tools:
hoist the preview argument checks above the rig
dispatch, wrap `create_camera`'s post-creation validation, make `panorama_type` a `Literal`,
collapse the guides/gate/quality duplications onto one owner each, and add a `dust_density` case to
the lighting smoke script.
