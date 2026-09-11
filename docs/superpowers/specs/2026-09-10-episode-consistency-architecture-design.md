# Episode Consistency & Context Architecture

**Status:** Design — revision 2, after adversarial review
**Created:** 2026-09-10 · **Revised:** 2026-09-11
**Repo:** `jpease/blender-mcp` (fork of `josuemontano/blender-mcp`, in turn of `ahujasid/blender-mcp`)

> **Revision 2** rewrites §3, §6.2, §6.3, §6.5, §10, §12, §13 and §14 after an adversarial
> review found two fatal errors in revision 1. Every measurement and code citation below
> was re-verified by execution against `main`. Findings and disposition: Appendix B.

---

## 1. Problem

Two problems that share one solution.

**Consistency.** A series running for several years must not have characters or key
locations drift in appearance between shots or between episodes. Generative authoring
makes drift cheap and invisible, so continuity must become something the system checks and
can prove — not something artists catch in review.

**Context cost.** The server registers 285 tools. Every MCP client receives the whole
`tools/list` response at connect time. Measured on `main` via `asyncio.run(mcp.list_tools())`,
compact JSON, ~3.6 bytes/token:

| Selection | Tools | Payload | Tokens |
|---|---|---|---|
| `core` only — current default | 40 | 122,155 B | **~34K** |
| `core` + `camera,texture-lighting,rendering` | 104 | 382,187 B | **~106K** |
| `all` | 285 | 1,198,576 B | **~333K** |

**Input schemas are 76% of the payload**, and the distribution is heavy-tailed — the 20
largest tools are **22% of all bytes**:

| Tool | Bytes | In shot path? |
|---|---|---|
| `configure_cloth` | 34,619 | no |
| `create_geometry_object` | 25,279 | **yes — in `scene`, i.e. core** |
| `configure_render_settings` | 19,639 | **yes — rendering** |
| `setup_liquid_shot` | 18,514 | no |
| `create_character_cloth_setup` | 17,748 | no |
| `add_cloth_simulation` | 14,090 | no |

**Schema size, not tool count, is the dominant term.** The two `perf:` commits on `main`
cut `manage_modifiers` from 72,410 B to 2,419 B and reduced the full `all` payload by
221,105 B (~61K tokens, 18.5%). That work attacked the right axis and should continue.

These remain one problem: the 285 tools exist because the agent derives every result from
primitives. An agent that links canon and applies a preset needs neither the modeling
surface nor its schemas.

## 2. Goals

1. Make consistency a **checkable, diffable artifact** with an enforcement point that
   cannot be bypassed by configuration.
2. Provide **pre-defined starting points** so common setups cost one call.
3. Bring the shot-mode `tools/list` payload under a **stated token budget**, without
   deleting authoring capability and without depending on client cooperation.
4. Ship an artist-facing prototype as a **Blender plugin on a local workstation**, on a
   path where hosting it for Media Center is a deployment change, not a rewrite.

## 3. Scope decision required: what does Blender produce?

**This is not a non-goal.** It determines whether the rest of this document is a
consistency guarantee or merely a precondition to one. Revision 1 deferred it and claimed
the design was "valid under either outcome." That claim was wrong.

**Outcome A — Blender renders final pixels.** Scene-state consistency (§6.2) is close to
sufficient. This document stands as written.

**Outcome B — Blender renders control passes** (depth / normal / segmentation / pose) that
condition a downstream generative model which produces the delivered image. Then what
drifts is *the model's rendering of the character*, and **identical control passes do not
prevent it**: two shots with byte-identical depth passes can return different faces.
Consistency there is **identity conditioning** — reference embeddings, adapter/LoRA
weights, base-model version, sampler, scheduler, seed, prompt — a second canon of a
different kind, needing the same versioning rigor as the first.

Under Outcome B this document specifies the *input* half of a two-part problem; §7
specifies the second half conditionally. **Until this is decided, treat §7 as unfunded
scope, not absent scope.** Building §6 alone and calling it a consistency guarantee is the
most expensive available mistake.

## 4. Non-goals

- **Maya → Blender conversion.** A separate seeding project produces the canon library.
  This design consumes it — but see §17 Q3: that project's identifier stability is a hard
  dependency, not a detail.
- **Arbitrary code execution.** `execute_blender_code` was deliberately removed from this
  fork and is not returning, including as a gateway capability (§12).
- **Inventing asset versioning.** The studio's asset-management system owns versions and
  publishing; this design integrates through a port (§6.1).

## 5. Constraints (verified by execution against `main`)

| Constraint | Evidence | Consequence |
|---|---|---|
| One in-flight request per connection. A UUID is carried and checked, but as a desync backstop that raises — not a correlation mechanism permitting overlap | `connection.py:191-196,202,222-230` | Multi-artist needs N Blender processes and a router. Phase 4. |
| Commands execute on Blender's main thread, drained by a timer returning `0.05` | `addon/server_core.py:184,321` | ~25 ms latency floor per call; favors coarse tools. |
| Socket listener and client handlers are daemon threads | `addon/server_core.py:177,250` | A plugin calling the MCP server **from the main thread deadlocks** (§10). |
| `bpy.app.timers.register()` must not be called off the main thread — *"not thread-safe and the callback can be silently lost"* | `addon/server_core.py:383-385` | Plugin must use a queue drained by **one persistent timer registered once on the main thread** (§10). |
| `bpy.ops.render.render(write_still=True)` runs synchronously inside the drain timer; client timeout is 180 s | `handlers/rendering.py:612-615`; `connection.py:160,222` | A render over 180 s desyncs the stream and freezes the UI (§11). |
| Image bytes now cross the socket (base64) for protocol ≥ 31 | `docker-blender@75a7abf` | Response payloads are now large as well as slow; worsens the row above. Bounded by `_MAX_MESSAGE_BYTES` (64 MiB). |
| Each command pushes its own undo step | `addon/transaction.py:137,157` | A 12-command `assemble_shot` yields 12 undo steps unless batched (§10). |
| Rollback does not track `libraries` | `addon/transaction.py:17-36` | A failed `link_canon` leaks a library datablock (§11). |
| **Multiple MCP server processes against one Blender is the documented configuration** | `README.md:243+` — *"Add one MCP server entry per bundle set"* | **A server-process-scoped mode guard is not an invariant** (§6.3). |
| The addon has no concept of mode; it dispatches anything arriving on `:9876` | `addon/server_core.py:100,476` | Enforcement must live in the addon or the file (§6.3). |
| A headless Blender container already exists | `docker/blender/{Dockerfile,entrypoint.sh,start_server.py}` | The extractor **can** be CI-tested (§13), and hosting is nearer than revision 1 assumed. |
| Client is not ours in the hosted phase; multi-provider, generic MCP | Stated requirement | Context reduction must work server-side; no `tools/list_changed` reliance. |

## 6. Architecture

### 6.1 Canon registry — asset identity

A **canon entity** is a durable, versioned identity for something that must look the same
everywhere: a character, location, hero prop, or rig.

```python
@dataclass(frozen=True)
class CanonRef:
    kind: Literal["character", "location", "prop", "rig", "material"]
    canon_id: str          # "char_hero"
    version: str           # "v012" — immutable; never "latest" once recorded
    content_digest: str    # of the library's *content*, not its path
```

Resolution goes through a port, so the studio's asset manager is an adapter:

```python
class AssetResolver(Protocol):
    def list(self, kind: str, query: str | None) -> Sequence[CanonSummary]: ...
    def resolve(self, canon_id: str, version: str) -> CanonRef: ...
    def latest(self, canon_id: str) -> str: ...
    def publish_asset(self, canon_id: str, source: Path, notes: str) -> CanonRef: ...
    def publish_shot(self, shot_id: str, source: Path, fingerprint: Fingerprint,
                     notes: str) -> ShotRef: ...
```

`publish_shot` is separate because a shot is not a canon entity — revision 1 routed shot
publishing through `publish_asset`, which the interface could not represent.

Two adapters ship: `StudioAssetResolver` and `LocalMirrorResolver` (a versioned directory
tree, used for the prototype and tests).

**Version pinning policy — pinned, never floating.** `link_canon` records the resolved
concrete version. Upgrading is explicit (`update_canon_version`) and re-fingerprints.

An approved shot that silently changes when an upstream asset republishes is a continuity
defect that reaches air. The cost — an episode drifting internally as some shots upgrade —
is *detectable*, but only given the three mechanisms revision 1 omitted:

1. **Re-verification on open, not only on link.** A `load_post` handler in the addon
   re-checks each linked library's `content_digest` against what the `.blend` recorded.
   Blender links by *path*; if a version is republished in place or served through a
   `latest` symlink, path-pinning alone silently resolves different bytes.
2. **An episode-level audit** (`audit_episode`) that opens every shot headlessly — using
   the existing `docker/blender` container — and reports drift across the set. Pairwise
   `diff_consistency` and a publish-time gate cannot answer "is episode 4 coherent?"
3. **A batch upgrade driver.** `update_canon_version` operates on one open file over a
   single-in-flight socket. Re-pinning 200 shots interactively is not viable; the upgrade
   path is the same headless runner as the audit.

**Hard dependency.** Pinning assumes the asset system exposes immutable, content-stable
version references. §17 Q1 is unresolved. Until it is, this policy is *chosen* but not
*safe* — hence `content_digest` rather than a path in `CanonRef`.

### 6.2 Consistency fingerprint — making drift diffable

Not a single scene hash — that reports "different" and nothing more. A fingerprint is an
ordered set of independently comparable **facet rows**:

```python
@dataclass(frozen=True)
class FingerprintRow:
    facet: str          # "asset" | "material" | "transform" | "color" | "format" | "light"
    subject: str        # library-qualified: (library_id, datablock_name)
    digest: str         # canonical hash — the fast path
    values: dict        # the canonicalized values themselves — the verdict
```

**Digest is the fast path; `values` is the verdict.** Digest equality cannot express
tolerance, and float32 round-trips through save/load make exact equality fragile. Equal
digests pass immediately; unequal digests fall through to a tolerance comparison on
`values` before anything is reported as drift.

| Facet | Captures | Grade |
|---|---|---|
| `asset` | `(canon_id, version, content_digest)` per linked entity | enforced |
| `material` | Node topology keyed by socket **identifier** (not display name), identity-input values, **plus content hashes of referenced image files** | enforced |
| `transform` | World-space bounds and scale of each linked entity's override root | enforced |
| `color` | View transform, look, display device, **exposure, gamma**, world, **OCIO config identity**, render engine | enforced |
| `format` | Resolution, pixel aspect, fps, frame range | enforced |
| `light` | Preset provenance stamp **and** actual light params (position, energy, color, size, spread) | advisory |

Six changes from revision 1, each closing a verified hole:

- **`material` now hashes texture content.** A linked material's node graph and the
  library's bytes are unchanged when someone repaints `hero_skin_albedo.png`. Revision 1
  passed that shot. Canon materials must use packed textures or have their image files
  content-hashed.
- **`rig` became `transform`.** Rest transforms of a linked armature cannot be edited —
  they equal the pinned version by construction, so the old facet was tautological. The
  drift it was meant to catch (a scale that changes silhouette) is an *object-level*
  transform on the override root. That is what `transform` measures.
- **`color` gained exposure, gamma, world, and OCIO identity.** `configure_color_management`
  sets `exposure` and `gamma` (`handlers/lighting/rendering.py:226`, exposed at
  `tools/lighting/rendering.py:99-100`) and is in the shot path. Revision 1 could not see
  a two-stop exposure change on the hero. "AgX" also names a transform in whichever config
  `$OCIO` loaded; the string is stable while the curve is not.
- **`format` is new.** Resolution, fps, and pixel aspect are among the most common
  continuity defects on a series, and `configure_render_settings` sets all of them from
  inside shot mode.
- **`light` records parameters, not just the stamp.** A provenance stamp survives moving
  the key light and changing its color. The positions are the fact; the stamp is the
  inference. Revision 1 had this backwards.
- **`subject` is library-qualified.** Linked datablocks from different libraries can share
  a name with each other and with local data.

**Canonicalization is specified, not deferred.** The extractor must: key node sockets by
identifier; exclude `location`, `width`, `label`, and auto-generated `.001` suffixes;
quantize floats to float32 precision before hashing; sort by a stable key; and record the
Blender version that produced the reading. Node trees are versioned in memory on load, so
the same pinned library read under two Blender releases can differ — the recorded version
is what lets an audit distinguish real drift from a version artifact.

**Baseline, exceptions, and where they live.** An episode's baseline is the set of enforced
rows it must match. Baselines, episodes, shots, and approvals are entities in the resolver,
not loose files. Because a legitimate per-shot deviation exists (a burned costume, a
wet-hair variant), the model includes explicit exceptions:

```python
@dataclass(frozen=True)
class ConsistencyException:
    shot_id: str; facet: str; subject: str; reason: str; approved_by: str
```

Without a per-shot waiver, the only escape from a failing enforced facet is advancing the
episode-global baseline — and a check whose only remedy is that broad is a check people
stop running.

**Purity.** Extractor (addon, `bpy`, emits plain dicts) is separate from
hasher/differ/policy (server, pure Python, no `bpy`). The pure half holds most of the
tests; the extractor is tested in the headless container (§13).

### 6.3 Modes and enforcement — the correction

Revision 1 claimed `shot` mode makes drift "structurally impossible" because canon is
linked, not appended. **That was false**, for two independent reasons:

1. **Linking freezes datablock bytes, not appearance.** On an editable override an agent
   can set `material_slots[i].link = 'OBJECT'` and assign a local material, add modifiers,
   constraints or drivers, and scale the root. Scene-local state — `scene.world`, the
   compositor tree, view-layer passes, light linking, object color, `hide_render` — is
   unlinked by definition. Blender has no per-property editability policy on an override;
   "overrides for pose and animation only" was an intention, not a mechanism.
2. **The guard was in the wrong process.** Tool registration is import-time from an env
   var, so mode was a property of a *server process* — and `README.md:243+` recommends
   running several against one Blender. An `asset`-mode process beside a `shot`-mode one
   is the documented configuration, and neither knows about the other.

**Enforcement moves into the addon, keyed on the open file.** The `.blend` carries a
`mode` property. The addon checks it in `_build_command_handlers` dispatch, before any
handler runs, and refuses out-of-mode mutations regardless of which process sent them or
how that process was configured. This is the only layer every path traverses.

| | `shot` mode | `asset` mode |
|---|---|---|
| Purpose | Assemble, animate, light, render | Author or revise a canon entity |
| Canon data | Linked; overrides permitted but audited by `transform`/`material` facets | Writable within a checkout |
| Enforcement | Addon-side guard + fingerprint check | Baseline advance is the explicit output |
| Tool surface | Intent layer + shot-scoped domains (§6.5) | Authoring surface |

Tool exposure remains the *ergonomic* mechanism — it keeps irrelevant schemas out of
context. The addon guard is the *correctness* mechanism. Neither substitutes for the other,
and the design no longer claims prevention where it provides detection.

### 6.4 Preset library — one interface, three providers

```python
class PresetProvider(Protocol):
    def list(self, kind: str) -> Sequence[PresetSummary]: ...
    def describe(self, preset_id: str) -> PresetDetail: ...   # params JSON Schema
    def apply(self, preset_id: str, params: dict, target: TargetSpec) -> AppliedPreset: ...
```

| Provider | Authored by | Stored as | Phase |
|---|---|---|---|
| `blend` | Artists, in Blender | Versioned `.blend` in the preset library | 1 |
| `recipe` | Engineers | Parameterized Python | 2 |
| `captured` | The agent | Serialized recipe from a live scene | 3 |

Applied presets stamp provenance (`preset_id`, version, params) on what they create; the
`light` facet reads it *alongside* measured parameters, not instead of them.

### 6.5 Context budget — stated in tokens, enforced per tool

The target is **a shot-mode payload under 60K tokens (~216 KB)**, not a tool count.
Revision 1's "~40 tools" was the wrong unit: 40 tools is already 34K tokens today.

Three mechanisms, in order of leverage:

**1. A per-tool schema budget of 2 KB, with the 20 heaviest tools as the work item.**
They are 22% of all bytes. `create_geometry_object` (25.3 KB) and
`configure_render_settings` (19.6 KB) are both in the shot path and both must come down;
the `manage_modifiers` fix (72.4 KB → 2.4 KB) is the proof the technique works and the
template for the rest.

**2. `CORE_MODULES` must split.** `core` is unconditional and carries 40 tools:

| module | tools | shot mode? |
|---|---|---|
| `core` | 2 | yes |
| `scene` | 10 | yes |
| `mesh` | 13 | **no — authoring** |
| `model` | 3 | **no — authoring** |
| `object_animation` | 1 | yes |
| `viewport` | 5 | yes |
| `animation` | 6 | yes |

`core-shared` (24) and `core-authoring` (16). Note `lighting` is not a bundle — it is
`texture-lighting` (`bundles.py:29`), which must also split so shot mode gets lighting
without the texture-authoring surface.

**3. A typed gateway for the fat tail.** Three stable tools —
`list_capabilities(domain, query)`, `describe_capability(name)`,
`invoke_capability(name, args)` — expose the remainder on demand.

**The gateway dispatches only to typed, schema-validated capabilities. It never accepts
Python source.** Server-side validation is preserved; what is lost is *decode-time*
constraint, since `args` advertises as an unconstrained object and providers that constrain
generation against the schema cannot do so. The honest framing: **the gateway defers cost
for tools never used in a session and saves nothing for tools that are** — a described
capability's schema lands in context anyway, plus a full inference turn. It is lazy loading,
and its win is proportional to unused surface. That is why it ranks third, behind schema
dieting and mode scoping.

Naming: the addon handshake already uses `capabilities` for handler names
(`server_core.py:1156`, gated in `connection.py:187`). The gateway must not reuse that term
in the protocol.

### 6.6 Shot recipe — reproducibility

Each intent-layer call appends a structured entry — tool, arguments, resolved canon
versions — to a `shot_recipe` in the `.blend`. Phase 2 records; replay is deferred until
the intent layer settles. Under Outcome B (§3), seeds, prompts, and conditioning references
belong in this record, which is the natural home for them.

**Crash exposure:** the recipe and fingerprint live in the `.blend`, i.e. in memory until
saved. A crash loses the provenance the design says a shot carries. Mitigation: write both
to a sidecar on each intent call, not only on save.

## 7. Generative identity conditioning — conditional on §3 Outcome B

Specified here so it is visible and costed, not built until §3 resolves.

If Blender feeds a generative model, a second canon is required, versioned with the same
rigor as the first:

```python
@dataclass(frozen=True)
class IdentityRef:
    canon_id: str                # same identity as the Blender-side entity
    version: str
    base_model: str              # provider + model + version
    adapter_digest: str | None   # LoRA / adapter weights content hash
    reference_digest: str        # identity reference images or embeddings
    sampler: str; scheduler: str; steps: int; cfg: float
    prompt_digest: str
```

This adds a `generative` fingerprint facet (enforced), binds a shot's control passes to the
`IdentityRef` that consumed them, and extends `shot_recipe` with seeds and prompts.

**It is a peer of §6, not an appendix to it.** Under Outcome B, §6 without §7 proves the
input is consistent while the output drifts. Two shots can pass every facet in §6.2 and
still show different faces.

## 8. Intent layer

Fifteen tools, in the artist's vocabulary. All return the existing envelope
(`ok`, `data`, `warnings`, `changed_objects`, `changed_resources`) — no new response
contract.

| Tool | Purpose |
|---|---|
| `list_canon(kind, query)` | Discover canon entities and versions |
| `link_canon(canon_id, version, alias)` | Link; records the pinned version and digest |
| `update_canon_version(alias, to_version)` | Explicit, fingerprint-checked upgrade |
| `assemble_shot(shot_id, location, characters, preset)` | Shot skeleton in one call |
| `place_character(alias, at, facing)` | Position a linked character |
| `list_presets(kind)` | Discover presets |
| `describe_preset(id)` | Preset parameters |
| `apply_preset(preset_id, params, target)` | Apply a rig or setup |
| `capture_preset(name, kind, scope)` | Snapshot current setup (Phase 3) |
| `set_shot_camera(preset_or_params)` | Camera placement and lens |
| `inspect_shot()` | Linked canon, versions, presets, overrides |
| `compute_consistency_fingerprint(scope)` | Produce a fingerprint |
| `diff_consistency(a, b)` | Compare two fingerprints |
| `assert_consistency(baseline)` | Enforce parity; per-facet diff on failure |
| `publish_shot(target, notes)` | Assert, fingerprint, publish |

Plus `audit_episode(episode_id)` (§6.1), which runs headlessly rather than against an
interactive session.

## 9. Module layout

```
src/blender_mcp/server/
  canon/          registry, AssetResolver port, Studio + LocalMirror adapters
  consistency/    facets, canonical hashing, differ, baselines, exceptions   [pure]
  presets/        provider protocol, providers, resolver
  modes.py        mode definitions (guard itself lives in the addon)
  gateway.py      capability catalog and typed dispatch
  tools/shot.py   the fifteen intent tools

src/blender_mcp/bundled/addon/
  mode_guard.py   dispatch-time enforcement keyed on the open .blend        [bpy]
  handlers/canon.py, shot.py, presets.py                                    [bpy]
  handlers/fingerprint.py   extractor only — reads scene, emits dicts       [bpy]
```

Modified: `bundles.py` (core and texture-lighting splits), `transaction.py` (track
`libraries`), `server_core.py` (mode-guard hook in dispatch).

## 10. Plugin architecture

The plugin runs inside Blender and is the MCP *client*; the existing addon remains the
socket *server*. The loop closes back into the same process. This is retained deliberately:
collapsing it would build a second code path that transfers nothing to Media Center, and
~25 ms per call is noise against inference.

**Deadlock.** The agent loop **must not run on Blender's main thread**. A main-thread call
blocks awaiting the MCP response; the MCP server sends a socket command; the addon queues
it; the drain timer can never run because the main thread is blocked. Hard hang.

**Marshalling — corrected.** Revision 1 said "marshal UI updates via `bpy.app.timers`",
which contradicts the addon's own rule at `server_core.py:383-385`: *never* call
`timers.register()` off the main thread. The required shape is the addon's existing
pattern: agent loop on a worker thread, results onto a `queue.Queue`, drained by **one
persistent timer registered once on the main thread**. No `bpy` access from the worker.

**Reentrancy hazards to design against:**

- **Long renders.** `bpy.ops.render.render()` runs synchronously inside the drain timer
  against a 180 s client timeout; with image bytes now crossing the socket the response is
  large too. Over 180 s the stream desyncs and the UI is frozen throughout. Renders and
  full fingerprints need to become polled async jobs, or carry a negotiated per-command
  timeout.
- **Artist and agent share one main thread and one file.** If the artist is in Edit Mode on
  a mesh the agent mutates, edit-mesh and `mesh.data` desync and one side's work is lost.
  Rule: the agent refuses to run while a modal operator is active or the file is in Edit
  Mode, and `wm.open_mainfile` invalidates every datablock reference from prior responses.
- **Undo granularity.** Each command pushes its own step, so a 12-command `assemble_shot`
  costs twelve Ctrl+Z presses to reverse. One undo step per intent call.
- **LLM transport.** HTTP/TLS from a worker thread inside Blender's bundled interpreter,
  with studio proxy configuration and credential storage — unspecified and needed in
  Phase 1.

**The plugin is disposable; the server is the asset.** Media Center brings its own agent
loop, prompts, and a model we do not choose. Anything enforced only in the plugin is lost
at that transition — which is precisely why §6.3's guard moved into the addon.

## 11. Error handling

- **Mode violation** — refused at addon dispatch, naming tool, file mode, and required
  mode. Scene unchanged.
- **Canon resolution failure** — lists available versions; never silently falls back to
  `latest`.
- **Digest mismatch on open** — `load_post` reports which library drifted; the shot opens
  read-only pending an explicit decision.
- **Enforced-facet mismatch** — `ok: false` with a per-facet, per-subject diff; a matching
  `ConsistencyException` downgrades it to a warning. `publish_shot` refuses without one.
- **Preset failure** — rolls back via the captured-state discipline. `transaction.py` must
  add `libraries` to `_TRACKED_COLLECTIONS` first, or a failed `link_canon` leaks a library.
- **Transport vs. operation failures stay separated**, per the existing contract.

## 12. Security invariants

Removing `execute_blender_code` closed the worst sink, not the source:
`search_sketchfab_models` still pulls attacker-controllable titles and descriptions into
context — the prompt-injection vector upstream's `safe_mode.py` names.

1. **No arbitrary code execution, in any form** — not a tool, not a gateway capability, not
   a debug path.
2. **Never link or append a `.blend` from outside an allowlisted library root.** A hostile
   `.blend` carries drivers that execute on load; this design depends on linking, so the
   mitigation is a path allowlist enforced **addon-side**, rooted at the canon and preset
   libraries.
3. **Open finding — `handlers/polyhaven.py:362` violates #2 today.** It calls
   `bpy.data.libraries.load(main_file_path, link=False)` on a `.blend` **downloaded from
   the network**. Revision 1 asserted third-party assets arrive as glTF; that is true of
   Sketchfab and false of Poly Haven. **This is a live vulnerability in `main`, independent
   of this design, and should be fixed on its own schedule** — restrict Poly Haven to
   glTF/FBX/OBJ, or route `.blend` downloads through the allowlist.
4. **Untrusted text is contained** — asset-search tools are not registered in `shot` mode;
   where registered, provider titles and descriptions are truncated and tagged untrusted in
   the envelope. **Not yet implemented**; revision 1 stated this as an invariant when it is
   a work item.
5. **Explicit paths only** for publishing, rendering, and export. No silent overwrites.

## 13. Testing

| Area | Tests | Blender? |
|---|---|---|
| Canonical hashing, tolerance comparison, differ | Golden extractor dicts → expected rows; per-facet drift; float32 round-trip stability | No |
| Baseline and exception policy | Enforced vs advisory grading; waiver downgrades; baseline advance lists newly-inconsistent shots | No |
| Version pinning | Concrete versions recorded; `latest` never persisted; digest re-check on open | No |
| `AssetResolver` adapters | Contract tests both satisfy; `LocalMirrorResolver` on a temp tree | No |
| Mode guard | Out-of-mode refused at dispatch; regression test that no mutating handler is undeclared; **two processes with different toolsets cannot bypass it** | No |
| Bundle splits | `shot` registers the expected set; payload stays under the §6.5 budget; no transitive leakage | No |
| Path allowlist | Traversal, symlink, outside-root rejection; Poly Haven `.blend` path | No |
| Gateway dispatch | Arguments validated identically to native tools | No |
| **Extractor correctness** | **Runs in the existing `docker/blender` container** against real scenes | **In container** |

Revision 1 listed extractor correctness as manual-only. That was wrong, and it matters: the
extractor is where nearly every §6.2 hole lives, and golden fixtures of extractor output
otherwise test the hasher against an assumption about what `bpy` emits.

Still manual: linked-override behavior across Blender versions, preset rollback in a live
session, and the §10 threading rule.

## 14. Phasing

Revision 1's Phase 1 was too large and, notably, **contained no plugin** — despite the
plugin being the prototype.

| Phase | Content | Gate |
|---|---|---|
| **0 — Spike** | Plugin ↔ MCP loop on a worker thread with queue + persistent timer. Prove no deadlock; measure round-trip. Throwaway. | Blender does not hang on first tool call |
| **1 — Demo** | Plugin (agent loop, UI panel, credentials). One canon character + one location, hand-built. `LocalMirrorResolver`. `blend` preset provider only. Six tools: `link_canon`, `apply_preset`, `set_shot_camera`, `place_character`, `compute_consistency_fingerprint`, `diff_consistency`. Extractor + hasher for `asset`, `material`, `color`, `format`. | An artist builds two shots; a deliberate drift is caught and a legitimate change is not |
| **2 — Enforcement** | Addon mode guard. Baselines, exceptions, `assert_consistency`, `publish_shot`. `load_post` digest re-check. `transaction.py` library tracking. `shot_recipe` recording. | A shot cannot be published inconsistent, from any client configuration |
| **3 — Portability** | `CORE_MODULES` and `texture-lighting` splits; schema diet on the 20 heaviest; typed gateway; `StudioAssetResolver`; `recipe` and `captured` providers; `audit_episode`. | Generic MCP client gets a shot-mode payload under 60K tokens and completes a shot |
| **4 — Hosted** | Session model, connection router, concurrency. | Media Center drives a shot end to end |

The Docker work already on `docker-blender` moves headless closer than revision 1 assumed;
Phase 3's audit and batch-upgrade runners depend on it, so that branch is on the critical
path rather than parallel to it.

## 15. Decisions recorded

| # | Question | Decision |
|---|---|---|
| 1 | What does "consistent" mean? | Six facet rows, digest as fast path and tolerance on values as verdict (§6.2). |
| 2 | Float or pin canon versions? | **Pin** — with open-time re-verification, episode audit, and a batch upgrade path. |
| 3 | Enforcement mechanism? | **Addon-side guard keyed on the open `.blend`.** Reversed from revision 1: process-scoped guards are not invariants. |
| 4 | Is drift structurally prevented? | **No.** Detected. Revision 1's claim was false (§6.3). |
| 5 | Context target? | **Tokens, not tool count.** Under 60K for shot mode; 2 KB per-tool budget. |
| 6 | Gateway role? | Third priority, behind schema dieting and mode scoping; typed only; win proportional to unused surface. |
| 7 | Collapse the plugin↔MCP loop locally? | **No.** |
| 8 | Port upstream's `safe_mode.py`? | **No** — it guards a deleted tool. Port its threat model (§12). |
| 9 | Generative identity conditioning? | **Blocked on §3.** Specified in §7, unfunded until the render path is decided. |

## 16. Deferred

- Shot recipe **replay** — format recorded in Phase 2; engine deferred.
- Concurrency and the connection router — Phase 4.
- Multi-provider degradation — Phase 1 picks the model; evaluation belongs to Phase 4.
- Animation and performance continuity (walk cycles, facial rig conventions, control-rig
  version). `transform` catches proportion, not performance. **Named, not solved.**
- Audio and lipsync — out of scope, flagged as an acknowledged gap.
- Multi-file shots (layout/anim/lighting files linking each other), which is the normal
  series structure. This design assumes one `.blend` per shot; revisit before Phase 3.
- Upstream `9224fe3` (addon panel hierarchy) — revisit when the plugin UI is built.

## 17. Open questions

1. **Which asset-management system**, and does it expose immutable, content-stable version
   references? Blocks §6.1's pinning guarantee. Needed before Phase 2.
2. **Which materials are identity-bearing?** Needs a tagging convention; proposed default
   is a canon-side manifest, which survives artists renaming things.
3. **Does the Maya → Blender seeding project preserve stable identifiers** across
   conversion runs? If IDs are regenerated per run, pinning breaks at the root. **Raise
   with that project now**, not in Phase 3.
4. **Is `format` (fps, resolution) episode-global or shot-level?** Treated as enforced
   per-shot against the episode baseline; confirm with editorial.
5. **§3 — the render path.** The largest open question in this document.

---

## Appendix A — Fork lineage and upstream divergence

Verified 2026-09-10 by fetching both upstreams into `refs/tmp/`; re-verified 2026-09-11
(divergence 12 commits; `safe_mode.py` exactly 972 lines).

```
ahujasid/blender-mcp  →  josuemontano/blender-mcp  →  jpease/blender-mcp
```

0 commits behind `josuemontano`; 12 behind `ahujasid`, diverged at `50a37a0` (2026-08-26).
Nothing critical is missing.

| Commit | Verdict |
|---|---|
| `41d98fc` safe mode (972 lines) | **Skip the code** — guards `execute_blender_code`, deleted here. **Adopt the threat model** (§12). |
| `b79063e` row size caps | **N/A** — patches `trajectory.py`, absent from this fork. |
| `90f6585` PolyPizza | **Skip** — more tools, more untrusted text. |
| `c5f35d9` Dockerfile | **Skip** — the `docker-blender` work is better targeted. |
| `33de875` rename to "MCP for Blender" | **Consider** — Blender Foundation trademark avoidance. |
| `9224fe3` addon panel hierarchy | **Revisit in Phase 1** — overlaps the plugin UI. |
| remaining 6 | READMEs and version bumps. No. |

## Appendix B — Adversarial review findings and disposition

Reviewed 2026-09-11. Claims were re-verified by execution before acceptance.

| # | Finding | Verified | Disposition |
|---|---|---|---|
| 1 | "Structurally impossible" is false; guard is process-scoped while README recommends multiple processes | **Yes** — `README.md:243+`, `server_core.py:100,476` | §6.3 rewritten; guard moved into the addon |
| 2 | Context arithmetic counts the wrong unit; schemas are 76% of payload | **Yes** — measured | §1 and §6.5 rewritten around a token budget |
| 3 | `color` facet misses exposure/gamma | **Yes** — `tools/lighting/rendering.py:99-100` | Facet extended |
| 4 | `material` facet misses external texture content | Accepted (mechanism certain) | Facet extended; packed textures or content hashing |
| 5 | `rig` facet tautological under linking | Accepted | Replaced with `transform` |
| 6 | No re-verification on open; no episode audit; no batch upgrade; no waivers | **Yes** — absent from revision 1 | All four added (§6.1, §6.2) |
| 7 | `AssetResolver.publish` cannot represent a shot | **Yes** | `publish_shot` added to the port |
| 8 | `bpy.app.timers` marshalling contradicts `server_core.py:383-385` | **Yes** | §10 corrected to queue + persistent timer |
| 9 | Render blocks main thread vs 180 s timeout; undo granularity; `libraries` untracked in rollback | **Yes** — `rendering.py:612-615`, `transaction.py:137,157,17-36` | §10, §11 |
| 10 | Poly Haven loads network `.blend` | **Yes** — `handlers/polyhaven.py:362` | §12.3, flagged as a live vulnerability |
| 11 | Extractor can be CI-tested via existing container | **Yes** — `docker/blender/` | §13 corrected |
| 12 | Phase 1 too large and omits the plugin | **Yes** | §14 rewritten |
| 13 | Missing: performance continuity, editorial format, audio, crash recovery, multi-file shots | **Yes** | `format` facet added; rest named in §16 |
| 14 | No generative AI integration anywhere | **Yes** | §3 elevated to a decision; §7 added |
| 15 | Appendix A unverifiable — "only `origin` exists" | **No — reviewer error.** Refs were in `refs/tmp/`; re-verified | Appendix A stands |
| 16 | "Matched by ordering, not ID" misreads the protocol | **Partly** — a UUID is carried and checked as a desync backstop | §5 wording corrected; conclusion unchanged |
| 17 | Tool count is 285, not 287 | **Yes** | Corrected throughout |

**Measurement caveat.** The review and this author's first measurements both ran against a
working tree on `docker-blender`, which lacks the two `perf:` commits **and has no
`bundles.py` at all** — its `tools/__init__.py` imports every submodule unconditionally, so
every process there advertises 285 tools (~394K tokens) regardless of
`BLENDER_MCP_TOOLSETS`. All figures in this revision were re-measured against `main` with
the imported source asserted. See §18.

## Appendix C — Note for the `docker-blender` branch

`docker-blender` branched from `b1c7037`, before bundle selection existed. On that branch
the Docker image ships a server that registers all 285 tools (~394K tokens) with no way to
scope it. Since the hosted deployment is the case where context matters most and the client
is least controllable, `docker-blender` should be rebased onto `main` — or at minimum take
`bundles.py` and the two `perf:` commits — before the container is used for anything
beyond local testing.
