# Episode Consistency & Context Architecture

**Status:** Design — approved for planning
**Date:** 2026-09-10
**Repo:** `jpease/blender-mcp` (fork of `josuemontano/blender-mcp`, in turn of `ahujasid/blender-mcp`)

---

## 1. Problem

Two problems that share one solution.

**Consistency.** An animated series running for several years must not have characters
or key locations drift in appearance between shots or between episodes. Generative
authoring makes drift cheap and invisible, so continuity has to become a property the
system enforces and can prove, not a property artists notice in review.

**Context cost.** The server registers 287 tools. Every MCP client receives the full
`tools/list` response at connect time, which consumes a large share of a model's context
before any work begins. Recent work has reduced per-schema size; that is a constant-factor
improvement against a problem that grows linearly with tool count.

These are the same problem. The 287 tools exist because the agent must derive every
result from Blender primitives. An agent that can link a canon asset and apply a preset
does not need the modeling, retopology, rigging, or simulation surface for shot work at
all. Canon and presets shorten the tool list *and* the conversation.

## 2. Goals

1. Make cross-shot and cross-episode consistency a **server-enforced invariant** and a
   **diffable artifact**, not a prompt instruction or a review-time judgement call.
2. Provide **pre-defined starting points** (lighting rigs, camera setups, shot skeletons)
   so common work costs one call rather than a re-derived sequence.
3. Reduce the advertised tool surface for shot work to a few dozen tools **without
   deleting authoring capability**, and without depending on client cooperation.
4. Ship an artist-facing prototype as a **Blender plugin on a local workstation**, on a
   path where a **hosted deployment for Media Center** is a transport swap rather than a
   rewrite.

## 3. Non-goals

- **Maya → Blender conversion.** A separate seeding project produces the Blender canon
  library. This design consumes it and assumes it exists.
- **Choosing the final render path.** Whether Blender produces final frames or control
  imagery (depth/normal/segmentation passes) for a downstream generative model is
  undecided. Consistency is therefore defined over **scene state**, not over rendered
  pixels, which keeps this design valid under either outcome.
- **Arbitrary code execution.** `execute_blender_code` was deliberately removed from this
  fork. It is not coming back, in any form, including as a gateway capability. See §10.
- **Inventing asset versioning.** The studio's existing asset-management system owns
  versions and publishing. This design integrates through a port (§5.1).

## 4. Constraints (verified against the code, not assumed)

| Constraint | Evidence | Consequence |
|---|---|---|
| One in-flight request per socket connection; responses matched by ordering, not by ID | `server/connection.py:191-196` | Multi-artist requires N Blender processes and a router, not multiplexing. Deferred to Phase 3. |
| Commands execute on Blender's main thread, drained by a timer returning `0.05` | `bundled/addon/server_core.py:184,321` | ~25ms average latency floor per call. Favors coarse, intent-level tools over chatty multi-round-trip designs. |
| Socket listener and per-client handlers run on daemon threads | `bundled/addon/server_core.py:177,250` | A plugin that calls the MCP server from Blender's main thread **deadlocks**. See §8. |
| Client is not under our control in the hosted phase; multi-provider, generic MCP | Stated requirement | Context reduction must work server-side. No reliance on `tools/list_changed`, client-side filtering, or provider-specific features. |
| `.blend` must stay artist-refinable | Stated requirement | Non-destructive operations; live modifiers; the file is a deliverable, not scratch. |

## 5. Architecture

Five new subsystems. Each is independently testable, and three of the five contain no
`bpy` dependency at all.

```
                    ┌─────────────────────────────────────┐
                    │  Intent layer  (tools/shot.py)      │  15 tools
                    └──────┬──────────┬──────────┬────────┘
                           │          │          │
              ┌────────────▼───┐ ┌────▼──────┐ ┌─▼─────────────┐
              │ Canon registry │ │ Presets   │ │ Consistency   │
              │  (canon/)      │ │ (presets/)│ │ (consistency/)│
              └────────┬───────┘ └────┬──────┘ └─▲─────────────┘
                       │              │          │ plain dicts
        ┌──────────────▼──────────────▼──────────┴──────────────┐
        │  Mode guard (modes.py) — enforces writability per mode │
        └──────────────────────────┬─────────────────────────────┘
                                   │ socket
        ┌──────────────────────────▼─────────────────────────────┐
        │  Addon handlers: canon.py, shot.py, presets.py,        │
        │  fingerprint.py (extractor only — reads bpy, emits dicts)│
        └─────────────────────────────────────────────────────────┘
```

### 5.1 Canon registry — asset identity

A **canon entity** is a durable, versioned identity for something that must look the same
everywhere: a character, a location, a hero prop, a rig.

```python
@dataclass(frozen=True)
class CanonRef:
    kind: Literal["character", "location", "prop", "rig", "material"]
    canon_id: str          # "char_hero", "loc_kitchen"
    version: str           # "v012" — immutable, never "latest" once recorded
    uri: str               # resolved library path
    fingerprint: str       # content digest, verified on link
```

Resolution goes through a port so the studio's asset manager is an adapter, not a
dependency:

```python
class AssetResolver(Protocol):
    def list(self, kind: str, query: str | None) -> Sequence[CanonSummary]: ...
    def resolve(self, canon_id: str, version: str) -> CanonRef: ...
    def latest(self, canon_id: str) -> str: ...
    def publish(self, canon_id: str, source: Path, notes: str) -> CanonRef: ...
```

Two adapters ship: `StudioAssetResolver` (the production system) and
`LocalMirrorResolver` (a versioned directory tree, used for the Phase 1 local prototype
and for tests). Nothing above this port knows which is in use.

**Version pinning policy — decided.** *Shots pin explicit immutable versions and never
float.* `link_canon` records the resolved concrete version in the `.blend`; a re-open or
re-render months later resolves the same bytes. Upgrading is an explicit act
(`update_canon_version`), which re-runs the fingerprint and reports what changed.

Rationale: an approved shot that silently changes when an upstream asset republishes is
a continuity defect that reaches air. The cost — an episode can drift internally if some
shots upgrade and others do not — is *detectable* by the consistency check in §5.2, while
the floating alternative's failure mode is silent.

### 5.2 Consistency fingerprint — making drift diffable

This is the core invention, and the answer to "what does consistent actually mean."

A fingerprint is **not** a single hash of the scene. A single hash reports "different" and
nothing more. A fingerprint is an ordered set of **facet rows**, each independently
comparable:

```python
@dataclass(frozen=True)
class FingerprintRow:
    facet: str      # "asset" | "material" | "rig" | "color" | "lens" | "light"
    subject: str    # "char_hero" | "hero_skin" | "scene.view_transform"
    digest: str     # canonical hash of that facet's identity-bearing values
    detail: dict    # small, human-readable, for the diff message
```

Facets, and why each is identity-bearing:

| Facet | Captures | Drift it catches |
|---|---|---|
| `asset` | `(canon_id, version, library digest)` per linked entity | Someone used `v011` of the hero in shot 4 and `v012` in shot 5 |
| `material` | Node-tree topology hash + values of identity inputs, for materials tagged canon-identity | Skin tone, costume colour, hair shader nudged in one shot |
| `rig` | Armature bone rest transforms + proportion digest | A rig swap or a uniform scale that changes silhouette |
| `color` | View transform, look, display device, render engine | The single most common cause of "the character looks different" |
| `lens` | Focal length, sensor size, DOF settings | Not enforced across shots by default; recorded for review |
| `light` | Preset provenance + key/fill ratio + colour temp | A location lit differently between two shots in the same scene |

Facets are graded, because not all drift is a defect:

- **`asset`, `material`, `rig`, `color` are enforced.** A mismatch against the episode
  baseline fails `assert_consistency`.
- **`lens` and `light` are advisory.** Recorded and diffed, reported as warnings. A
  director legitimately changes lenses between shots; they do not legitimately change the
  hero's skin tone.

**Episode baseline.** An episode has a baseline: the enforced rows established by its
first approved shot, or set explicitly. Every subsequent shot asserts against it.
Advancing the baseline (because the hero was legitimately republished mid-episode) is an
explicit, recorded operation — which also gives you the list of already-approved shots
now inconsistent with it.

**Purity.** Fingerprinting splits in two:

- **Extractor** (addon side, `bpy`-dependent): walks the scene, emits plain dicts. No
  hashing, no policy.
- **Hasher/differ** (server side, pure Python): canonicalizes, hashes, compares, formats
  diffs. **Zero `bpy` imports, fully unit-testable without Blender.**

This split is where most of the test coverage lives, and it satisfies the repo's existing
rule that pure validation and serialization helpers stay isolated from Blender-dependent
code.

Fingerprints are written into the `.blend` as a custom property and returned in the
envelope, so a shot carries its own provenance even when separated from the database.

### 5.3 Modes — enforcement by construction

Two modes, with near-disjoint tool surfaces and opposite writability rules.

| | `shot` mode | `asset` mode |
|---|---|---|
| Purpose | Assemble, animate, light, and render a shot | Author or revise a canon entity |
| Canon data | **Linked**, never appended. Library overrides for pose/animation only. | Writable, through checkout → edit → publish |
| Consistency | Enforced; `assert_consistency` runs before publish | Baseline-advancing is the explicit output |
| Tool surface | Intent layer + shot-scoped core, camera, lighting, rendering (target ~40; see §5.5) | Mesh, retopology, rigging, texture, geometry nodes, simulation (~250) |
| Destructive ops | Refused | Permitted within a checkout |

Drift is prevented **structurally**: in `shot` mode the agent cannot edit a character's
material because it does not own that datablock — Blender's own linking model does the
enforcement, not a validation rule that could be bypassed or a prompt that could be
ignored.

**Enforcement is two-layer, deliberately.** Tool exposure (§5.5) is the *ergonomic*
mechanism — it keeps out-of-mode tools out of the model's context. A server-side **mode
guard** is the *correctness* mechanism: every mutating handler declares the modes it is
valid in, and a call arriving out-of-mode is refused with a structured error regardless of
how it was routed. Exposure alone is insufficient because Media Center may connect to a
process registered with a wider surface than the current mode permits.

### 5.4 Preset library — one interface, three providers

Presets are the "pre-defined starting points." All three authoring routes discussed are
supported behind a single resolver, so the choice of route is not an architectural
commitment:

```python
class PresetProvider(Protocol):
    def list(self, kind: str) -> Sequence[PresetSummary]: ...
    def describe(self, preset_id: str) -> PresetDetail: ...   # includes params JSON Schema
    def apply(self, preset_id: str, params: dict, target: TargetSpec) -> AppliedPreset: ...
```

| Provider | Authored by | Stored as | Best for |
|---|---|---|---|
| `blend` | Artists, in Blender | Versioned `.blend` in the preset library, linked or appended | Look-bearing rigs an artist should tune visually |
| `recipe` | Engineers | Parameterized Python building the setup | Setups that must scale to subject size or shot framing |
| `captured` | The agent | Serialized recipe snapshotted from a live scene | Accumulating presets from real work |

Every applied preset **stamps provenance** — `preset_id`, version, and params — as a
custom property on what it creates. The `light` fingerprint facet reads that stamp, so
"this shot was lit with `three_point_studio@v3`" is a checkable fact rather than an
inference from light positions.

### 5.5 Context mechanism — mode-scoped registration, plus a typed gateway

Three layers, in order of preference:

**1. The intent layer absorbs the common path.** Shot work needs ~15 intent tools, not 287,
because `assemble_shot` replaces a long primitive sequence. This is the largest win and
it reduces conversation length as well as tool count.

**2. Mode-scoped registration handles packaging.** `bundles.py` already resolves an env
var to a set of modules to import. Modes extend that mechanism: `shot` registers the
intent layer plus shot-relevant domains; `asset` registers the authoring surface. A
generic client connecting to a `shot`-mode process gets a small list with no client-side
cooperation required.

**`CORE_MODULES` must be split first.** Today `core` is unconditional and already carries
40 tools — including `mesh` (13), which is authoring, not shot work:

| `core` module | Tools | Shot mode? |
|---|---|---|
| `core` | 2 | yes |
| `scene` | 10 | yes |
| `mesh` | 13 | **no — authoring** |
| `model` | 3 | **no — authoring** |
| `object_animation` | 1 | yes |
| `viewport` | 5 | yes |
| `animation` | 6 | yes |

So `core` splits into `core-shared` (24) and `core-authoring` (16) — 24 + 16 = the 40 it carries today. Without this split,
every shot-mode process ships 16 tools it can never legally call under the mode guard.

**Honest arithmetic for `shot` mode.** Taking whole existing bundles gives
15 (intent) + 24 (core-shared) + 23 (camera) + 15 (lighting) + 5 (rendering)
= **82 tools** — better than 287, short of the target. Reaching ~40 requires the gateway
to absorb the *detail* of camera and lighting while their inspection and intent-level
entry points stay native: camera 23 → ~8, lighting 15 → ~7 (rig construction is largely
replaced by presets anyway). That is the concrete Phase 2 job, and it is why the gateway
is part of the design rather than a hedge.

**3. A typed gateway covers the long tail.** For operations the intent layer has not
absorbed, three stable tools — `list_capabilities(domain, query)`,
`describe_capability(name)`, `invoke_capability(name, args)` — expose the remaining
surface on demand, keeping `tools/list` constant regardless of how many capabilities exist.

**The gateway dispatches only to typed, schema-validated capabilities — the same Pydantic
models the native tools use. It never accepts Python source.** This distinction is the
whole point: the gateway recovers the *context* benefit of upstream's
`execute_blender_code` while keeping the *validation* benefit this fork bought by deleting
it. Argument errors are still caught before `bpy` is touched.

The gateway is a fallback, not the primary interface, for two measured reasons: models
construct arguments less reliably against a stringly-typed `invoke` than against native
typed tools, and each discovery round trip costs a main-thread queue hop (§4). Coverage
migrating from gateway to intent layer over time is the intended direction of travel, and
gateway call frequency per domain is the metric that tells you which intent tool to build
next.

### 5.6 Shot recipe — reproducibility

Every intent-layer call appends a structured entry to a `shot_recipe` stored in the
`.blend`: the tool, its arguments, and the resolved canon versions. This gives a
human-readable account of how a shot was assembled and the raw material for later replay.

**Phase 1 records; it does not replay.** Building the recorder now is cheap and makes the
format a first-class concern; building a replay engine before the intent layer's shape has
settled would be speculative. Recording without replay is still immediately useful for
review and debugging.

If the downstream path turns out to be generative (§3), seeds and prompts belong in this
same record, so the recipe is the natural home for them when that decision lands.

## 6. Intent layer — the tool surface

Fifteen tools, expressed in the artist's vocabulary rather than Blender's:

| Tool | Purpose |
|---|---|
| `list_canon(kind, query)` | Discover available canon entities and their versions |
| `link_canon(canon_id, version, alias)` | Link a canon entity; records the pinned version |
| `update_canon_version(alias, to_version)` | Explicit, fingerprint-checked upgrade |
| `assemble_shot(shot_id, location, characters, preset)` | Build a shot skeleton in one call |
| `place_character(alias, at, facing)` | Position a linked character in world space |
| `list_presets(kind)` / `describe_preset(id)` | Discover presets and their parameters |
| `apply_preset(preset_id, params, target)` | Apply a lighting rig, camera setup, or shot skeleton |
| `capture_preset(name, kind, scope)` | Snapshot current setup as a reusable preset |
| `set_shot_camera(preset_or_params)` | Camera placement and lens |
| `inspect_shot()` | Structured summary: linked canon, versions, presets, overrides |
| `compute_consistency_fingerprint(scope)` / `diff_consistency(a, b)` | Produce and compare fingerprints |
| `assert_consistency(baseline)` | Enforce enforced-facet parity; fails with a per-facet diff |
| `publish_shot(target, notes)` | Assert, fingerprint, and publish through the asset resolver |

All return the existing envelope shape (`ok`, `data`, `warnings`, `changed_objects`,
`changed_resources`). No new response contract; existing clients and instructions still
apply.

## 7. Module layout

New, none importing `bpy` except where noted:

```
src/blender_mcp/server/
  canon/          registry, AssetResolver port, Studio + LocalMirror adapters
  consistency/    fingerprint model, canonical hashing, differ, baseline policy   [pure]
  presets/        provider protocol, blend/recipe/captured providers, resolver
  modes.py        mode definitions, mode guard decorator
  gateway.py      capability catalog, describe, typed dispatch
  tools/shot.py   the twelve intent tools

src/blender_mcp/bundled/addon/handlers/
  canon.py        link/override/version operations                                [bpy]
  shot.py         assembly and placement                                          [bpy]
  presets.py      preset application and capture                                  [bpy]
  fingerprint.py  extractor only — reads scene, emits plain dicts                 [bpy]
```

Modified: `bundles.py` gains mode definitions; existing mutating handlers gain a mode-guard
declaration. No existing tool schema changes, so the change is backward compatible.

## 8. Plugin architecture (Phase 1 delivery vehicle)

The prototype is a Blender add-on on the artist's workstation. The plugin is the MCP
*client*; the existing addon remains the socket *server*; the MCP server sits between
them. The loop closes back into the same Blender process.

**This is retained deliberately.** Collapsing it — having the plugin call handlers
in-process — would be faster locally and would build a second code path that transfers
nothing to Media Center. Keeping the loop costs ~25ms per call against LLM inference
measured in seconds, and makes the hosted deployment a transport swap.

**Mandatory threading rule.** The plugin's agent loop **must not run on Blender's main
thread.** Calling the MCP server from the main thread deadlocks: the main thread blocks
awaiting a response, the MCP server sends a socket command, the addon queues it, and the
drain timer can never run because the main thread is blocked. The result is a hard hang
requiring a force-quit.

Required shape:
- Agent loop and MCP client I/O on a **worker thread**.
- UI updates marshalled back to the main thread via `bpy.app.timers`.
- No `bpy` access from the worker thread, per the existing threading contract.

**The plugin is disposable; the server is the asset.** Media Center will bring its own
agent loop, its own prompts, and a model we do not choose. Any consistency enforcement,
tool filtering, or retry logic implemented in the plugin is lost at that transition.
Everything that matters is therefore a server-side invariant. The plugin should be the
*best* client of a mechanism that also works correctly for an unsophisticated one — never
a substitute for that mechanism.

## 9. Error handling

- **Mode violation** — structured MCP error naming the tool, the current mode, and the
  mode required. Scene unchanged.
- **Canon resolution failure** — unknown id or version resolves to an error listing
  available versions. Never silently falls back to `latest`.
- **Fingerprint mismatch on enforced facets** — `assert_consistency` returns `ok: false`
  with a per-facet, per-subject diff. `publish_shot` refuses. Advisory facets produce
  warnings and do not block.
- **Preset application failure** — partial application rolls back via the existing
  captured-state/`try`/`finally` discipline; the envelope reports what was and was not
  created.
- **Transport vs. operation failures stay separated**, per the existing contract: a
  Blender-rejected command must not drop a healthy socket.

## 10. Security invariants

Deleting `execute_blender_code` closed the worst sink but did not close the **source**.
`search_sketchfab_models` still pulls attacker-controllable titles and descriptions into
the model's context — the exact prompt-injection vector upstream's `safe_mode.py` names.
With 287 typed tools as the sink, injected text can still steer toward deletion, render
output paths, and destructive `apply=True` operations.

Invariants:

1. **No arbitrary code execution, in any form.** Not a tool, not a gateway capability, not
   a debug path. Upstream's `safe_mode.py` is a 972-line AST allowlist whose own docstring
   concedes it is *"not a sandbox around Blender"* and covers only the MCP path while the
   addon socket still accepts raw `execute_code` from any local process. Deleting the tool
   is the stronger position and this fork keeps it.

2. **Never link or append a `.blend` from outside the canon library root.** A hostile
   `.blend` carries drivers that execute on load, which is why upstream's safe mode blocks
   `wm.link`/`wm.append` outright. This design *depends* on linking, so the mitigation is
   provenance: a path allowlist enforced **addon-side**, rooted at the canon and preset
   library paths. Third-party assets continue to arrive as glTF, which is comparatively
   inert — a property to preserve deliberately rather than by accident.

3. **Untrusted text is contained.** Asset-search tools are **not registered in `shot`
   mode** at all. Where they are registered, provider-supplied titles and descriptions are
   truncated and tagged as untrusted in the envelope.

4. **Explicit paths only.** Publishing, rendering, and export write only to paths supplied
   by the caller or resolved through the asset resolver. No silent overwrites — the
   existing repo rule, restated because the canon library raises the stakes.

## 11. Testing

The pure/impure split (§5.2) is what makes this testable in CI without Blender.

| Area | Tests | Needs Blender? |
|---|---|---|
| Fingerprint hashing, canonicalization, diffing | Golden fixtures of extractor dicts → expected rows; facet-by-facet drift cases; ordering and float-canonicalization stability | No |
| Baseline policy | Enforced vs. advisory grading; baseline advance surfaces newly-inconsistent shots | No |
| Version pinning | `link_canon` records concrete versions; `latest` never persists; `update_canon_version` re-fingerprints | No |
| `AssetResolver` adapters | `LocalMirrorResolver` against a temp tree; contract tests both adapters satisfy | No |
| Mode guard | Every mutating handler declares modes; out-of-mode calls refused; regression test asserting no handler is undeclared | No |
| Bundle/mode registration | `shot` mode registers the expected set; no transitive leakage (the class of bug `7bc50c3` fixed) | No |
| Preset providers | Each provider against the protocol; provenance stamped on application | Partly |
| Path allowlist | Traversal, symlink, and outside-root rejection | No |
| Gateway dispatch | Capability arguments validated identically to native tools; unknown capability rejected | No |

**Manual Blender 5.1 verification required** (cannot run in CI): linked-canon override
behavior, extractor correctness against real scenes, preset application and rollback, and
the plugin threading rule of §8 — specifically that the agent loop on a worker thread does
not hang Blender on the first tool call.

## 12. Phasing

| Phase | Content | Gate |
|---|---|---|
| **0 — Spike** | Plugin ↔ MCP loop on a worker thread. Prove no deadlock; measure real round-trip latency. Throwaway code. | Loop runs without hanging Blender; latency understood |
| **1 — Demo** | Canon registry + `LocalMirrorResolver`, fingerprint + baseline, preset resolver with all three providers, the fifteen intent tools, mode guard. The artist-facing prototype. | An artist assembles two shots and the system proves they are consistent |
| **2 — Portability** | `CORE_MODULES` split; mode-scoped registration; typed gateway; `StudioAssetResolver` adapter. | A generic MCP client connecting to `shot` mode receives ~40 tools and can complete a shot end to end |
| **3 — Hosted** | Session model, connection router, headless Alma deployment, concurrency. | Media Center can drive a shot end to end |

Phases 0–2 run entirely on a local workstation. Phase 3 is not built now — only not
designed out. Specifically: the mode guard, the resolver port, and the server-side
context mechanism all exist in Phases 1–2 precisely so Phase 3 is a deployment change.

## 13. Decisions recorded

| # | Question | Decision |
|---|---|---|
| 1 | What does "consistent" mean? | Facet rows with per-facet digests (§5.2). Graded: `asset`/`material`/`rig`/`color` enforced; `lens`/`light` advisory. |
| 2 | Do shots float or pin canon versions? | **Pin.** Immutable; upgrades explicit and fingerprint-checked. |
| 3 | Enforcement mechanism? | Structural (linked, not appended) in `shot` mode, backed by a server-side mode guard. |
| 4 | Preset authoring route? | All three, behind one provider protocol. Not an architectural commitment. |
| 5 | Gateway or native tools? | Native intent tools primary; typed gateway for the long tail; never arbitrary code. |
| 6 | Collapse the plugin↔MCP loop locally? | **No.** Keep it, so hosted is a transport swap. |
| 7 | Port upstream's `safe_mode.py`? | **No** — it guards a tool this fork deleted. Port its *threat model* instead (§10). |

## 14. Deferred, with the reason

- **Shot recipe replay** — format recorded in Phase 1; replay engine deferred until the
  intent layer's shape settles.
- **Concurrency and the connection router** — single-in-flight socket is correct for one
  artist and one Blender. Phase 3.
- **Headless Alma, GPU-in-container, EGL/OpenGL viewport capture** — not on the local
  path. Phase 3. `get_viewport_screenshot` works locally, which the demo benefits from.
- **Multi-provider degradation** — Phase 1 picks the model. The intent layer is partial
  insurance; real evaluation belongs to Phase 3.
- **Upstream `9224fe3`** (addon panel hierarchy) — worth revisiting when the plugin's UI
  is built, to avoid redoing panel organization already done upstream.

## 15. Open questions

1. **Which asset-management system** backs `StudioAssetResolver`, and does it expose
   immutable version URIs? Shapes the adapter, not the port. Needed before Phase 2.
2. **Which materials are identity-bearing?** The `material` facet needs a tagging
   convention (naming, custom property, or a canon-side manifest). Needed in Phase 1;
   proposed default is a canon-side manifest, since it survives artists renaming things.
3. **Does the seeding project preserve stable identifiers** across Maya → Blender? If
   canon IDs are regenerated per conversion run, pinning breaks. Worth raising with that
   project now rather than discovering it in Phase 2.
4. **Is `lens` really advisory for this show?** Some productions treat lens continuity
   within a scene as enforced. Cheap to reclassify; the facet exists either way.

---

## Appendix A — Fork lineage and upstream divergence

Verified 2026-09-10 by fetching both upstreams.

```
ahujasid/blender-mcp  →  josuemontano/blender-mcp  →  jpease/blender-mcp
```

- **0 commits behind `josuemontano`, 6 ahead.** That lineage is clean.
- **12 commits behind `ahujasid`**, diverged at `50a37a0` (2026-08-26).

Triage of the 12: nothing critical is missing.

| Commit | Verdict |
|---|---|
| `41d98fc` safe mode (972 lines) | **Skip the code** — guards `execute_blender_code`, deleted here. **Adopt the threat model** (§10). |
| `b79063e` row size caps | **N/A** — patches `trajectory.py`, absent from this fork. |
| `90f6585` PolyPizza integration | **Skip** — more tools, more untrusted text, against both goals. |
| `c5f35d9` Dockerfile | **Skip** — `8e3ad45` is better targeted at Alma. |
| `33de875` rename to "MCP for Blender" | **Consider** — Blender Foundation trademark avoidance; matters under a product name. |
| `9224fe3` addon panel hierarchy | **Revisit in Phase 1** — overlaps the plugin UI. |
| remaining 6 | READMEs and version bumps. No. |
