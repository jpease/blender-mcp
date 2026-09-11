# Episode Consistency & Context Architecture

**Status:** Design — revision 2, after adversarial review
**Created:** 2026-09-10 · **Revised:** 2026-09-11
**Repo:** `jpease/blender-mcp` (fork of `josuemontano/blender-mcp`, in turn of `ahujasid/blender-mcp`)

> **Revision 2** rewrites §3, §6.2, §6.3, §6.5, §10, §12, §13 and §14 after an adversarial
> review found two fatal errors in revision 1. Every measurement and code citation below
> was re-verified by execution against `main`. Findings and disposition: Appendix B.
>
> **Revision 2.1** folds the context-lever evaluation into §6.5 (five levers, verdicts and
> a priority order), records the GitNexus impact analysis §6.3 and §10 required
> (Appendix D), and corrects the Phase 4 transport assumption: streamable HTTP is
> already implemented and tested on `docker-blender`.
>
> **Revision 2.2** records the hosted topology (§5.1) and corrects the client constraint:
> the MCP client is a headless agent runtime behind an AI gateway, not Media Center, and
> which runtime is undecided.

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

**Partial evidence (2026-09-11).** The reported hosted pipeline (§5.1) shows no generative
image or video step in the Blender path — it ends `Blender file output → Media Center`.
Either Blender produces the deliverable (Outcome A, and §7 closes as unfunded), or the
generative step runs **inside Media Center**, downstream — in which case identity
conditioning belongs to that team and §7 shrinks to a contract: this server records which
`IdentityRef` a shot's passes were built for, and does not own the conditioning canon.
Either reading reduces §7. Neither resolves the question.

**Outcome B — Blender renders control passes** (depth / normal / segmentation / pose) that
condition a downstream generative model which produces the delivered image. Then what
drifts is *the model's rendering of the character*, and **identical control passes do not
prevent it**: two shots with byte-identical depth passes can return different faces.
Consistency there is **identity conditioning** — reference embeddings, adapter/LoRA
weights, base-model version, sampler, scheduler, seed, prompt — a second canon of a
different kind, needing the same versioning rigor as the first.

Under Outcome B this document specifies the *input* half of a two-part problem. The second
half is **out of this project's scope** (§5.1, §7) whoever answers §3 — but it does not
stop existing. **Building §6 alone and calling it an end-to-end consistency guarantee
remains the most expensive available mistake**; the guarantee this project can make is
scoped to the file output, and someone must own the rest.

What §3 still decides for this project is what the file output must *contain* — final
pixels, or control passes plus the identity binding that lets a downstream step stay
consistent.

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
| **The MCP client in the hosted path is a headless agent runtime behind an AI gateway, and which runtime is not decided** | Stated topology (§5.1) | Design for an unknown client. Context reduction must work server-side; no `tools/list_changed` reliance. Keeps tool search and Skills off the load-bearing path (§6.5). |
| **Streamable HTTP transport is implemented and tested** | `docker-blender@ee25ffc`; `cli.py:16-18,96`; `tests/server/test_cli_transport.py`; `tests/test_docker_rig.py` | Remote hosting needs no transport work. Phase 4 is smaller than revision 2 assumed. |
| The handshake advertises handler names as `capabilities`, and the server gates on it | `server_core.py:1156`; `connection.py:187` | **The mode guard must not filter this set** — a mode-dependent handshake would make protocol negotiation non-deterministic (§6.3, Appendix D). |

### 5.1 Hosted topology

Reported 2026-09-11. The hosted path has five hops before a file comes back:

```
Media Center  →  AIGW  →  Headless Claude  →  Blender MCP  →  Headless Blender
                                                                     ↓
                                              Media Center  ←  Blender file output
```

What this settles:

- **The MCP client is not Media Center.** It is a headless agent runtime, reached through
  an AI gateway. **Which runtime is not decided, and AIGW abstracts it** — so client
  identity stays an unknown, and the portability bias in §6.5 stands on that basis rather
  than on any multi-provider commitment.
- **Credentials and provider routing are AIGW's concern** for the hosted path, closing that
  half of §10's open item. The local plugin still needs its own answer.
- **The deliverable is a file returned to Media Center**, not a live session. That favors a
  job-per-request or pooled model over long-lived interactive sessions, and makes the
  session-model question in §16 concrete rather than theoretical.
- **Blender is headless**, so the viewport and GPU concerns deferred in §16 apply to this
  path — even though image bytes now cross the socket (`75a7abf`), a headless Blender may
  have no viewport to capture.

**Scope of this project within that pipeline.** This design owns the middle three hops:

```
Media Center  →  AIGW  →  Headless Claude  │  Blender MCP  →  Headless Blender  →  file output  │  →  Media Center
     └──────── other teams ────────────────┘  └────────── this project ─────────────────────────┘
```

Everything upstream of the MCP boundary — gateway, runtime selection, prompting, model
choice — belongs to other teams. Everything downstream of the file output likewise. This
project is responsible for the contract at both seams: what a client may ask for, and what
the file output guarantees.

### 5.2 Execution model

Reported 2026-09-11, not yet fixed: **job-per-request as the baseline**, possibly with a
TTL cache of loaded files, and a warm pool for performance. Consequences that hold across
all three variants:

- **No long-lived in-memory scene.** The `.blend` on disk is the state carrier, which is
  why the fingerprint and `shot_recipe` must be written to a sidecar on each intent call
  rather than only on save (§6.6) — a job that ends without saving otherwise loses them.
- **Two session models, and the server must assume neither.** The local plugin path
  (Phases 0-2) is long-lived and interactive; the hosted path is short and batch. Anything
  that assumes a persistent session breaks one of them.
- **Cold start is the performance risk, and canon linking is its largest term.** Each job
  opens a shot and resolves its linked canon libraries. If those resolve across a network
  mount, per-job cost scales with the number of linked entities. The canon library should
  be node-local or cached, and `content_digest` (§6.1) is what makes a local cache safe to
  trust.
- **A pooled worker must fully load the target file before serving a job.** The mode guard
  (§6.3) is keyed on the open `.blend`; a worker reused across jobs while still holding a
  previous file would evaluate the guard against the wrong mode. Pool reuse requires an
  explicit load-or-reset step, not an assumption that the previous job left the process
  clean.
- **`audit_episode` and the batch upgrade path (§6.1) fit this model naturally** — both are
  already headless batch jobs rather than interactive operations.

What remains unsettled: what "Blender file output" contains — see §17 Q7.

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
`mode` property. The addon checks it at command dispatch, before any handler runs, and
refuses out-of-mode mutations regardless of which process sent them or how that process
was configured. This is the only layer every path traverses.

**Constraint surfaced by impact analysis (Appendix D).** `_build_command_handlers` also
populates the handshake's `capabilities` set (`server_core.py:1156`), which the server
gates on (`connection.py:187`). The guard must therefore reject **at dispatch** and leave
the advertised capability set unchanged. Filtering the handler map by mode would make the
handshake vary per open file — a client that connected under one mode would see its
protocol negotiation shift when the artist opened a different shot.

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

### 6.5 Context budget — lever evaluation

The target is **a shot-mode payload under 60K tokens (~216 KB)**, not a tool count.
Revision 1's "~40 tools" was the wrong unit: 40 tools is already 34K tokens today.

**Where the bytes actually are** (measured on `main`, `BLENDER_MCP_TOOLSETS=all`):

| Component | Bytes | Tokens | Share |
|---|---|---|---|
| Input schemas | 915,287 | ~254K | **76%** |
| Descriptions | 196,343 | ~55K | 16% |
| Everything else | ~87K | ~24K | 7% |

Per family, worst first:

| Family | Tools | Bytes | Tokens | Avg B/tool | Shot mode? |
|---|---|---|---|---|---|
| `liquid` | 37 | 190,719 | ~53K | 5,154 | no |
| `rigid_body` | 29 | 141,102 | ~39K | 4,865 | no |
| `cloth` | 19 | 126,942 | ~35K | 6,681 | no |
| `character_rigging` | 22 | 120,797 | ~34K | 5,490 | no |
| `geometry_nodes` | 28 | 106,893 | ~30K | 3,817 | no |
| **`camera`** | 23 | 91,102 | **~25K** | 3,960 | **yes** |
| `texture` | 21 | 72,248 | ~20K | 3,440 | partly |
| **`lighting`** | 15 | 63,539 | **~18K** | 4,235 | **yes** |
| **`scene`** | 10 | 48,190 | **~13K** | 4,819 | **yes (core)** |
| **`rendering`** | 5 | 33,079 | ~9K | **6,615** | **yes** |
| `mesh` | 13 | 22,296 | ~6K | 1,715 | no |
| `nd` | 10 | 14,607 | ~4K | 1,460 | no |

Two readings. The five simulation/rigging families are **57% of the payload** and none are
shot work — mode scoping removes them wholesale. But `camera` + `lighting` + `scene` +
`rendering` alone are **~65K tokens**, already over budget before the intent layer is
added. Shot mode cannot hit 60K by scoping alone; the heavy shot-path tools must also
shrink. `mesh` (1,715 B/tool) and `nd` (1,460) prove the schemas *can* be lean.

#### Lever 1 — Consolidation with discriminated inputs — **ADOPT**

Already proven here: `manage_modifiers` went 72,410 B → 2,419 B, and the two `perf:`
commits cut 221,105 B (~61K tokens, 18.5%) from the full payload.

| Consolidate | Keep split | Why |
|---|---|---|
| `camera` (23 → ~8), `lighting` (15 → ~7) | — | Highest shot-path leverage; rig construction is largely replaced by presets anyway |
| `liquid`, `cloth`, `rigid_body` configure/setup families | their `apply`/`bake`/destructive members | CLAUDE.md gates destructive operations; collapsing a `bake` into a config union hides the irreversible case behind a discriminator |
| `rendering` (6,615 B/tool, worst average) | `render_scene` | Long-running and side-effecting; stays its own tool |
| — | `nd`, `mesh` | Already lean; consolidation would cost clarity for ~no bytes |

Portability: **fully portable.** Works for every MCP client and model.

#### Lever 2 — Programmatic Tool Calling — **REJECT (PTC); DEFER (batch fallback)**

**Verified current, not stale:** PTC is no longer beta-gated (no beta header;
`code_execution_20260120` plus `allowed_callers` on a custom tool, Opus 4.5+/Sonnet 4.5+),
but it remains **explicitly incompatible with MCP tools**, alongside `strict: true`,
`disable_parallel_tool_use`, and forced `tool_choice`. It cannot attach to tools proxied
through an MCP server. Not available to this project at any engineering cost.

The fallback — a server-side `batch`/`script` tool composing several Blender operations in
one round trip — is **deferred, not rejected**, and must not become `execute_blender_code`
by another name (§12 invariant 1). A defensible version is a **declarative** sequence of
*already-validated* tool invocations (each step a named tool plus schema-checked args, with
bounded step count and no expression evaluation), executed under one undo step. That shape
is worth revisiting because it also fixes the undo-granularity problem in §10 — a 12-step
`assemble_shot` currently costs twelve Ctrl+Z presses. An imperative scripting API is not
worth revisiting.

Portability: portable (it is just another MCP tool), but it trades against §12.

#### Lever 3 — Skills — **ADOPT for Phase 1, with eyes open**

Skills are a **host-layer** mechanism (`.claude/skills/`), not part of the MCP server, and
they help **only Claude Code and Claude-based hosts — not Media Center's other providers**.
They are nonetheless worth building now, because Phases 0–2 are Claude Code plus your own
plugin, and the repo already has six `gitnexus-*` skills as a working model.

| Belongs in a Skill | Belongs in the tool docstring |
|---|---|
| Multi-tool workflow ("inspect before mutating"; safe ND boolean chains; how to assemble a shot) | What one tool does, its arguments, its envelope |
| Policy and judgement (when to ask before a destructive op) | Parameter ranges and units |
| Cross-tool sequencing and recovery | Per-tool failure modes |

Rule of thumb: if it spans more than one tool, it is a Skill; if it describes one tool, it
is a docstring — and docstrings are 16% of payload, so moving *workflow prose* out of them
is a real saving that also improves the non-Claude experience.

Portability: **Claude-only.** Therefore never the place for a correctness invariant — same
reasoning as §10's "the plugin is disposable."

#### Lever 4 — Tool search vs. server splitting — **ADOPT splitting; DEFER tool search**

Both verified current. Tool search is real: `tool_search_tool_regex_20251119` /
`tool_search_tool_bm25_20251119`, with `defer_loading: true` on deferred tools, returning
`tool_search_tool_result`. It has a genuine advantage I had not credited — **it appends
tool schemas rather than swapping them, preserving the prompt cache**, where adding or
removing tools mid-session otherwise invalidates it. Constraint: the search tool itself
must not be deferred and at least one tool must stay non-deferred, or the API returns 400.

But it is **Anthropic-API-specific**: it requires the model and host to support it, and
whether `defer_loading` can be applied to tools arriving through an `mcp_toolset` entry is
**not something this evaluation could confirm** — treat it as unverified rather than
assumed. Media Center is explicitly multi-provider, so tool search cannot be the primary
mechanism.

**Server splitting is protocol-level and portable**, which is why it ranks first at
comparable cost. Mode-scoped registration (§6.3) plus the `CORE_MODULES` and
`texture-lighting` splits below deliver the same outcome for every client.

`CORE_MODULES` is unconditional and carries 40 tools:

| module | tools | shot mode? |
|---|---|---|
| `core` | 2 | yes |
| `scene` | 10 | yes |
| `mesh` | 13 | **no — authoring** |
| `model` | 3 | **no — authoring** |
| `object_animation` | 1 | yes |
| `viewport` | 5 | yes |
| `animation` | 6 | yes |

Split into `core-shared` (24) and `core-authoring` (16). `lighting` is also not a bundle —
it is `texture-lighting` (`bundles.py:29`), which must split so shot mode gets lighting
without the texture-authoring surface.

#### Lever 5 — MCP Resources — **ADOPT, narrowly**

The server currently exposes **one `@mcp.prompt()`** (`asset_creation_strategy`,
`prompts.py:6`) and **zero `@mcp.resource()`**. Everything else is a Tool.

Honest assessment of the ceiling: `SERVER_INSTRUCTIONS` is only ~944 tokens and is sent
once per session, so moving it saves little. The real fit is **catalog content this design
is about to add** — canon entity listings, preset catalogs, episode baselines. Those are
static, enumerable reference data that a client can fetch on demand; as tools they would
add schemas to every session's `tools/list` for data most sessions never read.

Portability: **fully portable** — Resources are an MCP protocol primitive.

#### Priority order

By context-saving per unit of engineering, portable levers first:

1. **Mode scoping + `CORE_MODULES`/`texture-lighting` splits** (Lever 4) — removes 57% of
   payload for shot work; mechanism already exists in `bundles.py`; portable.
2. **Schema diet on the shot-path heavies** (Lever 1) — `camera`, `lighting`, `scene`,
   `rendering`; proven technique; portable; required because scoping alone misses budget.
3. **Resources for catalog data** (Lever 5) — small now, prevents a new class of bloat as
   canon and presets land; portable.
4. **Skills for workflow prose** (Lever 3) — improves Phase 1 immediately, trims
   docstrings for everyone, but Claude-only so never load-bearing.
5. **Typed gateway** (below) — third-priority fallback for the long tail.
6. **Tool search** (Lever 4) — revisit only if Media Center turns out to be Claude-only.
7. **PTC** — unavailable.

#### The typed gateway

Three stable tools — `list_capabilities(domain, query)`, `describe_capability(name)`,
`invoke_capability(name, args)` — expose the remainder on demand.

**The gateway dispatches only to typed, schema-validated capabilities. It never accepts
Python source.** Server-side validation is preserved; what is lost is *decode-time*
constraint, since `args` advertises as an unconstrained object. The honest framing: **the
gateway defers cost for tools never used in a session and saves nothing for tools that
are** — a described capability's schema lands in context anyway, plus a full inference
turn. Its win is proportional to unused surface.

Naming: the addon handshake already uses `capabilities` for handler names
(`server_core.py:1156`, gated in `connection.py:187`). The gateway must not reuse that term
in the protocol — see §6.3 and Appendix D.
### 6.6 Shot recipe — reproducibility

Each intent-layer call appends a structured entry — tool, arguments, resolved canon
versions — to a `shot_recipe` in the `.blend`. Phase 2 records; replay is deferred until
the intent layer settles. Under Outcome B (§3), seeds, prompts, and conditioning references
belong in this record, which is the natural home for them.

**Crash exposure:** the recipe and fingerprint live in the `.blend`, i.e. in memory until
saved. A crash loses the provenance the design says a shot carries. Mitigation: write both
to a sidecar on each intent call, not only on save.

## 7. Generative identity conditioning — out of scope, contract only

**Resolved by scope, not by §3.** This project's responsibility ends at the Blender file
output (§5.1). Identity conditioning operates on or after that output, so it belongs to
whichever team owns the generative step — regardless of how §3 is answered. §3 still
determines what the file output must *contain*; it no longer determines whether this
project builds a second canon.

What remains this project's obligation is the **seam**: if the downstream step is
generative, a shot must record which identity its passes were built for, so the two canons
can be joined later. That is one field in `shot_recipe`, not a subsystem.

The shape below is retained as the contract to hand to that team, not as work to schedule.

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

**It is a peer of §6, owned elsewhere.** Under Outcome B, §6 without this proves the input
is consistent while the output drifts: two shots can pass every facet in §6.2 and still
show different faces. That risk does not disappear because the work sits with another
team — it becomes a handoff to manage rather than a subsystem to build, and this document's
consistency claims must be stated as scoped to the Blender file output.

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

**The plugin is disposable; the server is the asset.** The hosted path drives this same
server through a different, headless agent runtime with its own loop and prompts, selected
behind a gateway (§5.1). Anything enforced only in the plugin is lost at that transition —
which is precisely why §6.3's guard moved into the addon.

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
| **1 — Demo** | Plugin (agent loop, UI panel, credentials). One canon character + one location, hand-built. `LocalMirrorResolver`. `blend` preset provider only. Six tools: `link_canon`, `apply_preset`, `set_shot_camera`, `place_character`, `compute_consistency_fingerprint`, `diff_consistency`. Extractor + hasher for `asset`, `material`, `color`, `format`. A `blender-mcp-authoring` Skill for cross-tool workflow guidance (§6.5 Lever 3). | An artist builds two shots; a deliberate drift is caught and a legitimate change is not |
| **2 — Enforcement** | Addon mode guard. Baselines, exceptions, `assert_consistency`, `publish_shot`. `load_post` digest re-check. `transaction.py` library tracking. `shot_recipe` recording. | A shot cannot be published inconsistent, from any client configuration |
| **3 — Portability** | `CORE_MODULES` and `texture-lighting` splits; schema diet on the 20 heaviest; typed gateway; `StudioAssetResolver`; `recipe` and `captured` providers; `audit_episode`. | Generic MCP client gets a shot-mode payload under 60K tokens and completes a shot |
| **4 — Hosted** | Session model, connection router, concurrency. **Transport is done** (`ee25ffc`); this phase is orchestration only. | Media Center drives a shot end to end |

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
| 9 | Generative identity conditioning? | **Out of scope** — superseded by decision 18. §3 still governs what the file output contains. |
| 10 | Consolidation (Lever 1)? | **Adopt** — `camera`/`lighting`/`rendering`; keep destructive members split per CLAUDE.md gating. |
| 11 | Programmatic Tool Calling? | **Reject** — verified still incompatible with MCP tools. Declarative batch fallback **deferred**, never imperative scripting. |
| 12 | Skills? | **Adopt for Phase 1**, never load-bearing — Claude-only, useless to Media Center's other providers. |
| 13 | Tool search vs. server splitting? | **Splitting** — portable. Tool search deferred: Anthropic-specific, and `defer_loading` on `mcp_toolset` is unverified. |
| 14 | MCP Resources? | **Adopt narrowly** — for canon/preset catalog data, not for `SERVER_INSTRUCTIONS` (~944 tokens, poor return). |
| 15 | Project scope within the pipeline? | **MCP → headless Blender → file output** (§5.1). Upstream and downstream hops belong to other teams; this project owns the contract at both seams. |
| 16 | Hosted execution model? | **Job-per-request baseline**, TTL cache and warm pool as performance options. The server must assume neither this nor the plugin's long-lived session (§5.2). |
| 17 | May the hosted client be non-Claude? | **Leave it open.** Portability stays the tiebreak in §6.5; revisit only if it becomes a blocker. |
| 18 | Does §7 belong to this project? | **No** — resolved by scope (§5.1), independent of §3. This project owns one `shot_recipe` field, not a second canon. |

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
6. ~~**Blender lifecycle in the hosted path**~~ — **answered 2026-09-11**: job-per-request
   baseline, possibly TTL-cached, with a warm pool for performance. Design consequences in
   §5.2. Not yet fixed, but the variants share the constraints that matter.
7. **What does "Blender file output" contain** — a `.blend`, rendered frames, control
   passes, or all three? **Inquiry in progress.** The single highest-value open question
   remaining: it likely answers §3 in practice, and it defines the downstream seam this
   project is responsible for.
8. ~~**Can the hosted MCP client be non-Claude?**~~ — **answered 2026-09-11**: left open
   deliberately. Portability remains the tiebreak in §6.5; revisit only if it blocks.

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
the imported source asserted. See Appendix C.

## Appendix C — `docker-blender` branch status

Re-verified 2026-09-11 (second check; the branch is moving quickly).

`docker-blender` branched from `b1c7037`, **before bundle selection existed**. Its
`tools/__init__.py` imports every submodule unconditionally and it has no `bundles.py`, so
a server started from that container registers all 285 tools (~394K tokens) regardless of
`BLENDER_MCP_TOOLSETS`. Since the hosted deployment is where context matters most and the
client is least controllable, the branch should take `bundles.py` and the two `perf:`
commits — or rebase onto `main` — before the container is used beyond local testing.

**What it does have, and what this design got wrong about it.** An earlier revision of this
analysis stated that `BLENDERMCP_TRANSPORT` was documented in the branch README but not
implemented. **That was measured at `75a7abf` and is no longer true** — and it was always a
claim about a branch in flight rather than a defect:

| Capability | Evidence |
|---|---|
| Streamable HTTP transport | `ee25ffc`; `cli.py:16-18` (env vars), `cli.py:96` (`mcp.run(transport="streamable-http")`) |
| Transport unit tests | `tests/server/test_cli_transport.py` — defaults, overrides, bad-port fallback, `sse` rejection |
| Remote-box container rig | `d185e00`, `tests/test_docker_rig.py` |
| Loopback binding + readiness | `89c6a93` |
| Pinned Blender version, locked deps | `998d523`, `93ebdf7` |

Consequence for §14: **Phase 4 needs no transport work.** Remote hosting is closer than
revision 2 assumed, and the remaining Phase 4 scope is session management, the connection
router, and concurrency.

**Process note.** Two claims in this document have now been wrong because a branch advanced
between measurement and writing. Any statement here about `docker-blender` should be read
as "as of the commit cited," and re-checked before it is acted on.

## Appendix D — GitNexus impact analysis

Run 2026-09-11 after re-indexing (`analyze --index-only`; the prior index was stale at
`a1f6175`, orphaned by the branch split). Revision 2 proposed changes to `bundles.py`,
`transaction.py` and `server_core.py` **without** this analysis, which CLAUDE.md requires;
this appendix closes that gap.

| Symbol | Index verdict | Text-search confirmation | Assessment |
|---|---|---|---|
| `resolve_toolset_modules` | LOW — 0 processes, 0 modules | — | Genuinely low. The `CORE_MODULES` split (§6.5) is contained. |
| `drain_command_queue` | **UNKNOWN** — "No callers resolved" | **8 references** — `server_core.py:183,184,195,196,267`; `tests/server/test_threading.py:130,175,225` | **False negative.** It is a `bpy.app.timers` callback passed by reference, which produces no call edge — precisely the case CLAUDE.md says not to read as safe. Real risk is moderate, and `test_threading.py:225` asserts registration. |
| `_build_command_handlers` | LOW — 1 module, with "2 call sites dropped at index time" | **9 references**, incl. `server_core.py:899`, **`:1156` (handshake `capabilities`)**, and 7 test files | **Understated.** The mode guard (§6.3) touches the protocol handshake surface. See the constraint recorded in §6.3. |

**No HIGH or CRITICAL findings.** The material result is not a risk score but the handshake
coupling: `_build_command_handlers` serves both dispatch and capability advertisement, and
the mode guard must touch only the former.

**Methodological note.** Two of three symbols were under-reported by the graph, in both
cases because the caller reaches the symbol through a reference class the index does not
record (a callback passed to `bpy.app.timers.register`, a method called on a
locally-constructed receiver). For a codebase this callback-heavy, graph verdicts are a
starting point and the text search is not optional.
