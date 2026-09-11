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
>
> **Revision 2.3** closes §3 — the file output is `.blend` plus rendered files, with USD
> possible — adds the delivery contract (§8.1), scopes §7 out (§5.1), and hardens vendor
> agnosticism into a requirement, which demotes Skills to a thin pointer and drops tool
> search from the roadmap.
>
> **Revision 2.8** replaces the context *target* with a policy — minimize rather than fill,
> since unneeded bytes buy nothing — keeps 50K (a 200K floor at 25%) as a ceiling rather
> than a goal, and adds the §13.1 task-success bar as the stopping rule that makes
> minimization safe. Also corrects a standing error: `tools/list` occupies context on
> **every** turn, not once per session.
>
> **Revision 2.7** derives the context budget from the floor context window instead of
> asserting it, and measures the five scoping moves taking shot mode from 78.1K to 53.8K
> tokens — closing the last FATAL's arithmetic. The budget is met only if intent tools hold
> a 1 KB ceiling; the floor window itself is now §17 Q11, the last open gate.
>
> **Revision 2.6** specifies the file-lifecycle and linking subsystem (§9.1) that Appendix
> E #3 found missing, schedules it as Phase 0.5, and — from the same spike — refutes half of
> Appendix E #2: Blender does **not** block Python writes to linked data, so the mode guard
> has a real refusal set after all.
>
> **Revision 2.5** resolves the review's highest-priority risk: a spike confirms
> `content_digest` is viable as SHA-256 over immutable published bytes (§6.1), so pinning
> is no longer conditional. The two FATAL findings about the context lever and the missing
> file-lifecycle code remain open.
>
> **Revision 2.4 records a second adversarial review (Appendix E) that found three FATAL
> issues. Most of its findings are OPEN and a revision 3 is required before this document
> is planned against.** Corrected here: Lever 1's verdict and arithmetic, the shot-mode
> budget gap (77K, not 65K), two `main`-vs-`docker-blender` citation errors, Appendix D's
> overstated finding, and the claim that no blocking questions remain — which was false.

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
221,105 B (~61K tokens, **15.8%** of the pre-change payload). That work attacked the right axis and should continue.

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

## 3. What Blender produces — answered

**Answered 2026-09-11.** The file output is **`.blend` plus rendered files**, both
required; **USD is possible but not committed**. Combined with the scope boundary in §5.1,
this closes the question for this project.

What it settles:

- **Scene-state consistency (§6.2) is the right guarantee**, because scene state determines
  both deliverables — the `.blend` *is* the state, and the renders are a function of it.
  The `format` facet (resolution, pixel aspect, fps) is enforced precisely because rendered
  files ship.
- **§7 stays out of scope** (§5.1). If a generative step consumes these outputs downstream,
  its identity conditioning belongs to that team; this project's obligation is the seam.
- **This document's consistency claim is scoped to the file output** and should be stated
  that way to anyone consuming it.

The delivery contract this implies is specified in §8.1. The original framing is retained
below because it explains why the answer matters.

### 3.1 Why this mattered (retained)

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
| A headless Blender container exists **on `docker-blender` only — not on `main`** | `git ls-tree main -- docker/` is empty; the files are on `docker-blender` | The extractor can be CI-tested **only after that branch merges**, which no phase currently schedules. §13's claim is conditional on a merge, not on today's `main`. |
| **The MCP client in the hosted path is a headless agent runtime behind an AI gateway, and which runtime is not decided** | Stated topology (§5.1) | Design for an unknown client. Context reduction must work server-side; no `tools/list_changed` reliance. Keeps tool search and Skills off the load-bearing path (§6.5). |
| Streamable HTTP transport is implemented and tested — **on `docker-blender` only** | `docker-blender@ee25ffc`, whose `cli.py` carries it; `main`'s `cli.py` is 51 lines with no transport | Phase 4 needs no *new* transport work, but it does need the merge. `main` today is stdio-only. |
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

#### `content_digest` — specified and validated by spike

**SHA-256 over the published `.blend` bytes.** Spike run 2026-09-11 against Blender 5.2.1
LTS; the non-mutation result was independently reproduced by the author.

The objection was real and does not apply. Re-saving a `.blend` **is** non-deterministic:
two fresh-process re-saves of the same input differ *from each other* — 146 of 583,632
bytes (**0.03%**), in 58 runs of 8 bytes at a regular 72-byte stride, holding 64-bit heap
addresses. Blender 5.2 canonicalizes most of the file; the leak is narrow but real, and no
save flag suppresses it.

It is irrelevant because **the digest is computed once at publish over bytes that are never
rewritten**:

| Operation | Effect on the published file |
|---|---|
| Open it | **No change** — hash identical before and after (reproduced) |
| Link it into a shot | **No change** |
| Copy it to a node-local cache | **Byte-identical** — cache copies are trivially verifiable |
| Re-save it | Non-deterministic — **which is why invariant 2 below forbids it** |

Cost: **~7 ms for an 18 MB / 130k-vertex asset**, I/O-bound, and **no Blender process is
required to verify** — any worker in any language can check a pinned digest.

**Invariants this imposes:**

1. **Publish uncompressed.** `compress=True` avalanches a 0.03% raw difference into **97.8%
   of compressed bytes** and diverges the file size.
2. **A published version is never re-saved.** Re-save non-determinism is reachable only by
   violating this, which a canon library must not do regardless.
3. **A Blender version upgrade is a re-publish and re-pin, not an in-place re-save.**
   (Cross-version determinism was not tested; under invariant 2 it cannot arise.)
4. **The digest covers the `.blend` only.** Unpacked external dependencies — textures,
   caches, nested libraries — are stored as *path strings*, so swapping a texture file
   changes nothing in the digest. **Canon assets must pack their textures, or publish a
   dependency-closure manifest digested alongside.** This is the same hole the `material`
   facet closes at shot level (§6.2), and it must be closed at asset level too.

**Why the digest is necessary at all — Blender's own change detection does not substitute.**
With a library mutated in place beneath a shot:

| Library mutation | Blender reports | Byte digest |
|---|---|---|
| Object renamed | `LIB: Object 'X' missing` | caught |
| Vertex moved | **silent** | caught |
| Modifier parameter changed | **silent** | caught |
| Shader node value changed | **silent** | caught |
| Object transform changed | **silent** | caught |

Blender notices only when *name resolution* fails; everything else silently binds to the
new content. `bpy.types.Library` exposes no checksum, no mtime, and no size. This is the
strongest available argument for the mechanism.

**Rejected alternative — a semantic content walk.** Built and run in the spike: stable
across every save variant and across compression, and it caught all seven single-property
mutations. But it costs **463 ms against 7 ms** (~65×), needs a full Blender process, scales
with element count rather than file size, and every property the walk forgets is a *silent
false negative*. Reserve it for cross-version or cross-DCC identity, which this design does
not need under invariant 2.

**A finding that closes off a tempting future design.** Rebuilding the same scene from an
identical script is **not** deterministic even semantically — `primitive_uv_sphere_add`
produced differing `vertex_index` / `loop_start` / UV ordering across runs with identical
vertex coordinates. Neither digest is a build-reproducibility mechanism; both are stable
only for a fixed file. "Re-derive the asset and check the digest matches" is not available.

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

**What the guard actually refuses — settled empirically (§9.1).** Writes to linked data are
*not* blocked by Blender: `ob.location.x = 5.0` on a linked, non-editable object succeeds
through Python and persists. The guard's refusal set is therefore **any mutation targeting
a datablock whose `library is not None`** — real, non-empty, and mechanically checkable at
dispatch. Drift *through overrides*, by contrast, cannot be refused without refusing
legitimate shot work, and is the fingerprint's job. **Two mechanisms, two non-overlapping
jobs.**

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

#### The budget policy — minimize, don't fill

**Decided 2026-09-11: there is no target size. The surface should be as small as still
works well; unneeded bytes buy nothing.** That reframes the budget from a quota to spend
into a ratchet, and it has three consequences worth stating plainly:

1. **The context-window arithmetic below yields a *ceiling*, not a goal.** Fitting it is
   necessary and not sufficient. A design that lands at 50K when 35K would work equally
   well has failed, even though it "met budget."
2. **Every scoping move in the ladder is taken on its merits**, whether or not a number
   forces it. `create_geometry_object` does not belong in shot mode because shot assembly
   does not create geometry — not because it happens to cost 25,279 B.
3. **Deferred surface is nearly free**, which raises the value of the gateway (§6.5) and of
   Resources (Lever 5) above where an arithmetic-driven design would rank them.

**"Smallest that still works well" needs a stopping rule, or it becomes "smallest."**
Without one, cutting continues past the point where the agent can no longer find the tool
it needs or construct its arguments — and that failure is silent at the payload level and
expensive at the task level. The rule: **cut, then measure against a task-success bar.**
See §13.1.

#### The ceiling, derived

Revision 2.3 asserted 60K tokens without derivation (Appendix E #13). It is not an
independent number: **it is a function of the smallest client context window we commit to
supporting**, which — under vendor agnosticism (decision 17) — is a decision nobody has
made.

`tools/list` is paid once per session, on the first turn, and must coexist with the system
prompt, the conversation, scene-inspection results (which are not small), and reasoning. A
defensible ceiling is **25–30% of the floor context**:

| Floor context | @25% | @30% |
|---|---|---|
| 128K | 32K | 38K |
| **200K** | 50K | **60K** |
| 1M | 250K | 300K |

**60K is exactly "200K floor at 30%."** That is the assumption the number encodes, and it
should be stated rather than inherited.

**A correction that matters here.** An earlier revision called `tools/list` a cost "paid
once per session, on the first turn." It is not: the API is stateless, tool definitions are
re-sent every request, and they **occupy** context on every turn. Prompt caching makes them
cheap in dollars, not in tokens. The percentages above are therefore *permanent occupancy*,
which argues for the stingier end of the range.

**Adopted ceiling: 50K — a 200K floor at 25%.** 200K is where every current frontier model
sits; 128K binds only for older or smaller models, and routing a long, tool-heavy shot
assembly to one is a poor choice on its own merits. 30% was rejected because the measured
projection (58.0K) would fit it with no margin at all, and a budget with no margin is a
coincidence rather than a plan.

**The ladder below reaches 53.8K before intent tools and ~58.0K after — still above the
50K ceiling.** Under the minimize policy that is not a failure to be explained away but
the current position on a ratchet that should keep turning: the next candidates are the
remaining heavy animation tools (`manage_nla_tracks` 6,955 B, `manage_animation_driver`
5,741 B, `bake_evaluated_animation` 4,950 B) and the two large camera configuration tools,
all of which are gateway-reachable.

#### The gap, and what actually closes it — measured

Where the bytes are inside shot mode: **`$defs` are 41% of the payload** (116,093 B) and the
top 10 tools are 39%. The fat is nested model definitions, not parameter count — which is
what makes scoping effective and consolidation ineffective (Appendix E #1).

| Step | Tools | Bytes | Tokens |
|---|---|---|---|
| Current shot mode | 67 | 281,237 | **78.1K** |
| − `create_geometry_object`, `remove_scene_objects`, `reset_scene` from `scene` | 64 | 252,417 | 70.1K |
| − `camera.rigs` (6 tools) → gateway | 58 | 233,816 | 64.9K |
| − light **construction** (4 tools) → replaced by presets | 54 | 198,977 | 55.3K |
| − engine scoping (drop Eevee variants) | 54 | 193,777 | **53.8K** |
| **+ 15 intent tools** | 69 | — | **see below** |

Each removal is justified by the design, not by byte-chasing: shot assembly does not
*create* geometry (its 16 `$defs` are geometry-type variants — 25,279 B alone), does not
reset or delete scenes, reaches camera rigs rarely enough for a gateway round trip, and
gets its lighting from presets (§6.4) rather than by constructing lights by hand.

**Whether the budget is met turns on the intent layer's own schema discipline:**

| Intent-tool assumption | 15 tools cost | Shot mode total | vs 60K |
|---|---|---|---|
| At the median existing tool (3,141 B) | 47,115 B / 13.1K | 240,892 B / **66.9K** | **over by 6.9K** |
| At ~1,000 B — simple signatures | 15,000 B / 4.2K | 208,777 B / **58.0K** | **fits** |

`link_canon(canon_id, version, alias)` is three strings; it has no business costing what the
median existing tool costs. **So the intent layer's 1 KB-per-tool ceiling is not a nicety —
it is the difference between meeting the budget and missing it**, and it must be a
reviewed constraint on §8, not an aspiration.

#### Bundle splits this requires

Beyond `CORE_MODULES` and `texture-lighting` (below), `scene` must split — it currently
carries `create_geometry_object` (authoring) plus `reset_scene` and `remove_scene_objects`
(destructive) into every shot-mode process, which also makes the destructive-operation story
in §6.3 incoherent. `camera` must separate `camera.rigs`, and `lighting` must separate
construction from inspection and environment.

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
shot work — mode scoping removes them wholesale. But the shot-mode set is worse than an
earlier revision stated: `core-shared` (24) + `camera` + `lighting` + `rendering` is
**67 tools / 277,083 B / ~77K tokens** — a **22% overshoot** of the 60K budget before any
of the fifteen §8 intent tools is registered. (The ~65K figure previously quoted here
omitted `core-shared`.) Scoping alone cannot reach budget; the shot-path schemas must also
shrink, and by far more than consolidation delivers. `mesh` (1,715 B/tool) and `nd` (1,460)
prove the schemas *can* be lean.

`retopology` is also missing from the table below: 27 tools / 96,777 B / ~27K tokens, the
sixth-largest family.

#### Lever 1 — Consolidation with discriminated inputs — **CONTESTED; see Appendix E #1**

> **Revision 2.3's verdict here was wrong and is retained only so the error is legible.**
> The precedent cited was not consolidation, and consolidation does not produce the saving
> claimed. Both facts are measured in Appendix E #1. The corrected lever is *variant
> scoping*, not consolidation. Do not plan against this section until revision 3.

The cited precedent — `manage_modifiers` 72,410 B → 2,419 B — was **type erasure**, not
consolidation: commit `0b052ec` changes `modifier: ModifierSpecInput` to
`modifier: dict[str, Any]` and validates internally with a `TypeAdapter`. Measured,
a discriminated union of the same 30 variants costs 37,263 B against 39,729 B for the
variants as separate schemas — a **6.2%** saving, because `oneOf` still serializes every
variant.

The two `perf:` commits did cut 221,105 B (~61K tokens, **15.8%** of the pre-change
payload) — but by erasure, which §6.5's own gateway section condemns as advertising "an
unconstrained object."

**The corrected lever: scope the variant set, not the type.** That union is ~10K tokens for
one tool. If shot mode needs six modifier types rather than thirty, exposing six typed
variants costs ~2K tokens — an ~80% cut with typing intact, and strictly better than either
consolidation (6%) or erasure (loses wire-level shape). Erasure and the gateway are then
reserved for surfaces whose variant set is genuinely open-ended.

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

#### Lever 3 — Skills — **ADOPT as a thin pointer only**

**Vendor agnosticism is a stated requirement (decision 17), not a preference.** That
demotes Skills from where revision 2.1 put them. Skills are a **host-layer** mechanism
(`.claude/skills/`) that helps only Claude Code and Claude-based hosts, so no workflow
guidance may live there *first*.

**Write the guidance once, in portable form** — MCP Resources for cross-tool workflow
content (§6.5 Lever 5) and docstrings for per-tool content — and let a
`blender-mcp-authoring` Skill be a **thin pointer** into it. That keeps the Phase 1
accelerator without creating a second source of truth that the hosted path cannot read. A
Skill that duplicates portable content will drift from it; a Skill that points at it cannot.

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
assumed. Given that vendor agnosticism is a requirement (decision 17), tool search is **not
adopted** rather than merely deferred: it may not be the mechanism the budget depends on,
and building toward it would misdirect effort that portable levers need.

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
it is `texture-lighting` (`bundles.py:30`), which must split so shot mode gets lighting
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

### 8.1 Delivery contract

A job's output is a **delivery**: an artifact set plus a manifest, produced by
`publish_shot` and returned across the §5.1 seam.

| Artifact | Status | Notes |
|---|---|---|
| `.blend` | **required** | The artist-refinable record. Carries `shot_recipe`, fingerprint, and pinned canon versions. |
| Rendered files | **required** | Paths, format, colour space, and frame range recorded in the manifest — never inferred by the consumer. |
| USD | **possible** | Not committed; see the export caveat below. |

The manifest is the seam's contract and must be sufficient on its own: artifact paths and
checksums, the shot's fingerprint rows, pinned canon `(canon_id, version, content_digest)`
for every linked entity, applied preset provenance, the Blender version that produced it,
and any `ConsistencyException` in force. A consumer must never have to open the `.blend` to
learn what it is.

**USD export caveat — design for it now, build it later.** USD is the studio's stated
interchange direction, and Blender exports it, but the export is **lossy in exactly the
places this design depends on**: evaluated modifiers lose their live definitions, linked
library overrides flatten, and canon linkage is not preserved by the format. An exported
USD that silently drops `(canon_id, version)` breaks the pinning guarantee for anything
downstream that trusts it. If USD is adopted, provenance must be written explicitly as USD
custom metadata, and the exporter must be treated as a **fingerprint-bearing surface** —
i.e. a USD delivery carries its own facet rows asserting what survived. Scheduling this is
premature; designing the `.blend` and manifest so the data exists to export is not.

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

### 9.1 File lifecycle and linking — the missing subsystem

Appendix E #3 established that Phases 0–2 depend on four primitives with **zero lines of
code** in this repo. This section specifies them. A spike on 2026-09-11 (Blender 5.2.1,
headless) established the facts below; every "verified" row was executed, not reasoned.

**The enabling fact: the MCP server survives a file load.**

| Property | Result | Why it matters |
|---|---|---|
| `bpy.types.blendermcp_server` (module-level, not file data) | **survives** `open_mainfile` | The socket and its connection state are not lost |
| `bpy.app.timers.register(..., persistent=True)` — what `server_core.py:184` already uses | **survives** | The drain loop keeps running |
| the same timer with `persistent=False` | **does not survive** | Confirms the flag is load-bearing, not incidental |
| data from the loaded file | visible immediately | |

Without those two, none of this subsystem would be possible; they hold, so it is.

**Commands to add** (addon handlers, one round trip each):

| Command | `bpy` call | Notes |
|---|---|---|
| `open_shot` | `wm.open_mainfile` | **Invalidates every datablock reference** from prior responses. Resets `bpy.context.scene`, so mode (§6.3) and the scene-flag capability set (Appendix E #5) must be re-read after. Clears undo. |
| `save_shot` | `wm.save_mainfile` / `save_as_mainfile` | Canon publishes **must** pass `compress=False` (§6.1 invariant 1). |
| `reset_session` | `wm.read_factory_settings(use_empty=True)` | The explicit pool load-or-reset step §5.2 requires. |
| `link_canon_library` | `bpy.data.libraries.load(path, link=True)` | Verified working. Must reject paths outside the allowlist (§12 invariant 2). |
| `create_override` | `collection.override_hierarchy_create(scene, view_layer, reference=instance)` | **`object.override_create()` returns `None`** — verified. The hierarchy call on the collection is the working API; an implementation that reaches for the object-level one will silently produce nothing. |
| `list_libraries` | walk `bpy.data.libraries` + digest verify | `bpy.types.Library` exposes no checksum, mtime or size (§6.1), so this must hash the file itself. |
| `reload_library` / `relocate_library` | `wm.lib_reload` / `wm.lib_relocate` | Needed for `update_canon_version`. |
| `load_post` hook | `bpy.app.handlers.load_post` + `@persistent` | Open-time digest re-verification (§6.1). |

**Untested hazard, and the first thing Phase 0 must settle.** Calling `wm.open_mainfile`
from *inside* the drain-timer callback is reentrant — the callback's own execution context
is freed mid-call. This could not be tested headlessly because `bpy.app.timers` do not fire
in `--background` (no event loop). It needs the GUI session or the container rig. **If it
crashes, `open_shot` must defer the load to a subsequent timer tick and return its response
before the file changes** — which makes `open_shot` the one command whose response cannot
describe its own result. Design for that possibility rather than discovering it.

**`transaction.py` must track `libraries`** before `link_canon_library` ships, or a failed
link leaks a library datablock (§11).

#### The mode guard is rehabilitated — Appendix E #2 was half wrong

The review argued the guard has no refusal set, because writes to linked canon *"are
already refused by Blender at the RNA level."* **That is empirically false.** Verified:

```
linked object:  is_editable = False,  library is not None
ob.location.x = 5.0   →  SUCCEEDED, and the value persisted: (5.0, 0.0, 0.0)
```

**Blender's Python API does not block writes to linked data.** `is_editable` is advisory to
the UI; RNA accepts the assignment, and the change affects everything the shot renders. For
a server that drives Blender entirely through Python, that is a live, silent drift vector
with nothing else standing in front of it. The guard has a real, non-empty refusal set:
**any mutation targeting a datablock whose `library is not None`.** That is mechanically
checkable at dispatch, and it is exactly the class of write nothing else prevents.

The review's *other* claim is confirmed, and remains a limit on what the guard can promise:

```
override material slot → link='OBJECT' → assign local material   →  SUCCEEDED
override transform write                                          →  SUCCEEDED
```

So overrides do permit drift by design, and the guard cannot refuse those without refusing
legitimate shot work. **The correct division: the guard refuses writes to linked data; the
fingerprint catches drift through overrides.** Two mechanisms, two non-overlapping jobs —
which is what §6.3 should have said instead of claiming prevention outright.

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
| `content_digest` | Stable across open, link and copy; publish rejects `compress=True`; re-save of a published version refused (invariant 2); dependency manifest covers unpacked textures (invariant 4) | Partly — open/link/copy need the container |
| Gateway dispatch | Arguments validated identically to native tools | No |
| **Extractor correctness** | **Runs in the existing `docker/blender` container** against real scenes | **In container** |

Revision 1 listed extractor correctness as manual-only. That was wrong, and it matters: the
extractor is where nearly every §6.2 hole lives, and golden fixtures of extractor output
otherwise test the hasher against an assumption about what `bpy` emits.

Still manual: linked-override behavior across Blender versions, preset rollback in a live
session, and the §10 threading rule.

### 13.1 The task-success bar — the stopping rule for minimization

§6.5 sets the surface to "as small as still works well." That is the right policy and it is
unbounded without a definition of *works well*, because over-cutting fails **silently at the
payload level and expensively at the task level**: the payload keeps shrinking and looks
like progress while the agent quietly loses the ability to find a tool or construct its
arguments.

**The bar.** A small fixed set of representative shot-assembly tasks — on the order of a
dozen, not a suite — each with a machine-checkable outcome:

| Task shape | Pass condition |
|---|---|
| Link a canon character and place it | Correct canon id and version linked; object at the requested transform |
| Apply a lighting preset to a shot | Preset provenance stamped; expected lights created |
| Set a camera to a named framing | Camera exists with the requested lens and target |
| Assemble a two-character shot from a brief | Both linked at pinned versions; fingerprint computes |
| Reproduce a shot's fingerprint and diff it against a baseline | Diff matches the expected facet rows |
| Catch a deliberately introduced drift | `assert_consistency` fails on the right facet and subject |

**Metrics that decide whether a cut is kept:** task completion rate, first-try tool-argument
validity (how often a call is accepted without a retry), and turns-to-completion. The first
two are what a shrinking surface actually damages.

**Procedure.** Cut, run the bar, keep the cut if the metrics hold. Stop when they degrade.
This makes minimization a measured ratchet rather than an aesthetic preference, and it gives
the gateway an empirical boundary: surface whose removal costs nothing on the bar belongs
behind it, and surface whose removal costs completion rate does not.

**It also settles the one question Appendix E #1 left open.** Whether `dict[str, Any]`
erasure meaningfully hurts first-try argument validity — the fork between the typed line and
the gateway — is an empirical question nobody has measured. The same bar measures it: run it
with `manage_modifiers` typed and erased, and compare. Until then, §6.5's preference for
variant scoping over erasure is reasoned, not evidenced.

**Cost discipline.** Each run exercises a model against a real Blender, so the bar must stay
small enough to run on every scoping change. A dozen tasks is a budget, not a starting point.

## 14. Phasing

Revision 1's Phase 1 was too large and, notably, **contained no plugin** — despite the
plugin being the prototype.

| Phase | Content | Gate |
|---|---|---|
| **0 — Spike** | Plugin ↔ MCP loop on a worker thread with queue + persistent timer. Prove no deadlock; measure round-trip. **Plus: does `wm.open_mainfile` called from inside the drain timer crash?** (§9.1 — needs the GUI or container rig; not testable headlessly). Throwaway. | Blender does not hang on first tool call, and the reentrancy answer is known |
| **0.5 — File lifecycle** | The §9.1 handler family: `open_shot`, `save_shot`, `reset_session`, `link_canon_library`, `create_override`, `list_libraries`, `reload_library`, `relocate_library`, `load_post` hook. `transaction.py` gains `libraries`. **This is the critical path — everything below depends on it and revision 2 omitted it entirely.** | A shot file can be opened, a canon library linked, an override created, and the result saved and reopened with the link intact |
| **1 — Demo** | Plugin (agent loop, UI panel, credentials). One canon character + one location, hand-built. `LocalMirrorResolver`. `blend` preset provider only. Six tools: `link_canon`, `apply_preset`, `set_shot_camera`, `place_character`, `compute_consistency_fingerprint`, `diff_consistency`. Extractor + hasher for `asset`, `material`, `color`, `format`. Workflow guidance authored as portable MCP Resources, with a thin `blender-mcp-authoring` Skill pointing at them (§6.5 Lever 3). | An artist builds two shots; a deliberate drift is caught and a legitimate change is not |
| **2 — Enforcement** | Addon mode guard. Baselines, exceptions, `assert_consistency`, `publish_shot`. `load_post` digest re-check. `transaction.py` library tracking. `shot_recipe` recording. | A shot cannot be published inconsistent, from any client configuration |
| **3 — Portability** | **The §13.1 task-success bar first** — minimization is unsafe without it. Then bundle splits (`CORE_MODULES`, `texture-lighting`, **`scene`**, **`camera.rigs`**, **lighting construction**); schema diet on the 20 heaviest; typed gateway; `StudioAssetResolver`; `recipe` and `captured` providers; `audit_episode`. | Payload ratcheted down as far as the bar allows, under the 50K ceiling, with the bar's metrics holding |
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
| 5 | Context target? | **Minimize, don't fill.** No target size — the surface should be as small as still works well (§6.5). A 50K ceiling (200K floor at 25%) is the safety bound, not the goal. Intent tools carry a **1 KB ceiling**. The stopping rule is the task-success bar in §13.1, without which "smallest" cuts into capability. |
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
| 17 | May the hosted client be non-Claude? | **Vendor agnostic is a requirement.** No Claude-only feature may be load-bearing. Tool search is not adopted; Skills are a thin pointer over portable content. |
| 19 | What does the file output contain? | **`.blend` + rendered files, both required; USD possible.** Delivery contract in §8.1. Closes §3 for this project. |
| 20 | USD? | **Design for, do not build.** The export is lossy exactly where pinning matters, so provenance must be explicit metadata and a USD delivery carries its own facet rows (§8.1). |
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
5. ~~**§3 — the render path.**~~ — **answered 2026-09-11** (§3, §8.1).

**"No blocking questions remain" was wrong** (Appendix E #11). Q1 and Q3 above each state
that they block the §6.1 pinning guarantee; relabelling them "scoping details" did not make
them scoping details. Both are external dependencies with no named owner and no date, and
Q9 below now outranks them.

9. ~~**Can a stable `content_digest` be computed at all?**~~ — **answered 2026-09-11 by
   spike.** Yes: SHA-256 over immutable published bytes, ~7 ms, no Blender needed to
   verify. Specified in §6.1. Pinning, the `asset` facet, open-time re-verification and the
   node-local cache are no longer conditional. Two residual items are now design
   requirements rather than open questions: canon assets must **pack textures or publish a
   digested dependency manifest** (§6.1 invariant 4), and a version upgrade is a
   **re-publish, not an in-place re-save** (invariant 3).
10. ~~**Does the variant-scoping lever actually reach budget?**~~ — **answered 2026-09-11
    by measurement** (§6.5). Five scoping moves take shot mode 78.1K → 53.8K. The budget is
    met *only if* intent tools average ~1 KB; at the median existing tool size they cost
    13.1K and the total lands 6.9K over. The 1 KB ceiling on §8 is now a hard constraint.
11. ~~**What is the minimum client context window we commit to supporting?**~~ —
    **answered 2026-09-11: minimize rather than target.** No fixed floor to fill; the
    surface should be as small as still works well, since unneeded bytes buy nothing. A
    200K-floor-at-25% **ceiling** of 50K is adopted as the safety bound (§6.5). This
    converts the remaining question from "what number?" to "what is the stopping rule?",
    answered by the task-success bar in §13.1.
6. ~~**Blender lifecycle in the hosted path**~~ — **answered 2026-09-11**: job-per-request
   baseline, possibly TTL-cached, with a warm pool for performance. Design consequences in
   §5.2. Not yet fixed, but the variants share the constraints that matter.
7. ~~**What does "Blender file output" contain**~~ — **answered 2026-09-11**: `.blend` and
   rendered files, both required; USD possible but uncommitted. See §3 and §8.1.
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

**No HIGH or CRITICAL findings.** The practical result is that `_build_command_handlers`
serves both dispatch and capability advertisement, so the mode guard must touch only the
former.

**Corrected 2026-09-11 (Appendix E #5).** This appendix originally presented that coupling
as newly discovered. It is not: `_build_command_handlers` already varies with scene state —
`server_core.py:774,784,793` gate up to 17 handlers on `blendermcp_use_polyhaven`,
`_sketchfab` and `_nd`, which are `bpy.types.Scene` properties saved in and restored from
the `.blend`. **Opening a different file already shifts the advertised capability set, and
the server's cached handshake already goes stale when it does.** The guard-at-dispatch
conclusion stands; the novelty claimed for it does not, and the pre-existing staleness is
an unlogged defect in its own right.

**Methodological note.** Two of three symbols were under-reported by the graph, in both
cases because the caller reaches the symbol through a reference class the index does not
record (a callback passed to `bpy.app.timers.register`, a method called on a
locally-constructed receiver). For a codebase this callback-heavy, graph verdicts are a
starting point and the text search is not optional.

## Appendix E — Second adversarial review (2026-09-11)

Reviewed against `main @ b95e7a6`. **Most findings are OPEN.** They are recorded here
rather than dispositioned because the first review's dispositions (Appendix B) were
themselves criticized — correctly — for documenting findings rather than fixing them. A
finding is marked FIXED below only where the spec text actually changed.

Every finding marked "verified" was independently reproduced by this author, not accepted
on the reviewer's word.

| # | Sev | Finding | Verified? | Status |
|---|---|---|---|---|
| 1 | **FATAL** | *(arithmetic now resolved — see §6.5; the mechanism correction stands)* Lever 1 refuted. The `manage_modifiers` precedent is type erasure (`0b052ec`: `ModifierSpecInput` → `dict[str, Any]`), not consolidation. A discriminated union of 30 variants costs 37,263 B vs 39,729 B split — **6.2%**, not 97%. The spec ranks the working mechanism (erasure/gateway) 5th and the non-working one 2nd. | **Reproduced exactly** | §6.5 Lever 1 corrected to *variant scoping*; priority order still needs revision 3 |
| 2 | **FATAL** | The §6.3 mode guard has no definable refusal set. Its residual candidate — writes to linked canon — is *"already refused by Blender's RNA."* | **Partly REFUTED by spike.** `ob.location.x = 5.0` on a linked, non-editable object **succeeds and persists** — Blender's Python API does not block writes to linked data. The override-drift half is **confirmed** (material swap and transform both succeed). | **PARTLY RESOLVED** — the guard has a real refusal set (`library is not None`), now stated in §6.3 and §9.1. Still open: the per-`Scene` property carrier, and the default for new/foreign/hand-edited files. |
| 3 | **FATAL** | Phases 0–2 depend on four subsystems with zero code: library **linking**, library **overrides**, `bpy.app.handlers`, and **`save_mainfile`/`open_mainfile`**. This server cannot open or save a `.blend`, which §8.1 declares a required deliverable. | **Verified by grep**, then scoped by spike | **RESOLVED** — specified in §9.1 and scheduled as **Phase 0.5**, the critical path. Spike established that the server survives a file load (module state + persistent timer), that `override_hierarchy_create` is the working API where `override_create()` returns `None`, and that one reentrancy hazard remains untestable headlessly. |
| 4 | SERIOUS | §5's table, headed "verified by execution against `main`", cited `docker/blender/*` (absent from `main`) and `cli.py:96` (`main`'s is 51 lines). Third wrong-branch error by this author. | **Verified** | **FIXED** — both rows corrected; §13 and §14 claims now stated as conditional on a merge no phase schedules |
| 5 | SERIOUS | Appendix D's "material finding" describes behavior the system already has: capabilities already vary per `.blend` via scene flags (`:774,784,793`). | **Verified** | **FIXED** — Appendix D corrected |
| 6 | SERIOUS | `transform` is worse than the `rig` facet it replaced. Pose-bone scale is writable from shot mode (`handlers/animation.py:767`) and invisible to a root-transform check. World-space bounds are also frame-dependent by design. | Verified | **OPEN** — facet must be scale-only, read the evaluated depsgraph at a declared frame, and descend to pose bones |
| 7 | SERIOUS | §6.2 still insufficient. Drift passing all six enforced facets: `hide_render`, view-layer `material_override`, constraints on the override root, drivers, pose-bone scale, `unit_settings.scale_length`, and **render determinism** (samples, denoiser, seed, compute device). | Verified against registered tools | **OPEN** — render determinism is a category error, not an omission: renders are a required deliverable and a warm pool is heterogeneous hardware |
| 8 | SERIOUS | `content_digest` is cited 6× as what makes pinning safe and is **never defined**. `.blend` files may not be byte-reproducible across saves. | **Spike 2026-09-11 (Blender 5.2.1); non-mutation independently reproduced** | **RESOLVED — TRUE BUT IRRELEVANT.** Re-save is non-deterministic (0.03%, heap addresses) but open/link/copy do not mutate, so SHA-256 over immutable published bytes works at ~7 ms. Specified in §6.1 with four invariants. The spike also showed Blender's own change detection is silent for every mutation except failed name resolution — strengthening the case for the mechanism. |
| 9 | SERIOUS | The Blender command socket has **no authentication** (0 hits for auth/token/hmac) yet dispatches 278 handlers, writes caller-supplied paths, and makes outbound calls. Not among §12's five invariants. | **Verified** | **OPEN** — dominant risk for the pooled multi-tenant deployment in §5.2 |
| 10 | SERIOUS | §8.1's USD caveat is backwards. `export_custom_properties` is default **True** and already used in this repo (`cloth/exporting.py:191`); `USDHook` (4.1+) covers layer metadata; and `UsdModelAPI.assetInfo` ships `identifier`/`name`/`version` with a pluggable resolver — i.e. USD already standardizes what §6.1 invents. The real lossiness is **materials** (`generate_preview_surface` approximates to ~5 node types), which threatens the *enforced* `material` facet. | Repo side verified; **doc claims not independently verified** | **OPEN** — rewrite decision 20; confront whether `assetInfo` + Ar replaces §6.1 rather than threatens it |
| 11 | MOD | "No blocking questions remain" is contradicted by §17's own text (Q1 "blocks §6.1's pinning guarantee"; Q3 "pinning breaks at the root"). Q1–Q4 were relabelled, not answered. | Verified | **OPEN** |
| 12 | MOD | 18.5% should be **15.8%** (divided by the post-reduction payload); shot mode is 77K not 65K; `retopology` missing from the family table; `scene` stays whole in shot mode carrying `reset_scene` and `remove_scene_objects`. | **Verified** | **PARTLY FIXED** — percentages and shot-mode arithmetic corrected; `scene` split still open |
| 13 | MOD | The 60K budget is asserted, never derived. Decision 5's "2 KB/tool" implies 108 tools against a 67+15 shot surface — two targets in one decision. | Verified | **RESOLVED** — §6.5 derives it as a function of the floor context window (60K = 200K floor at 30%) and records the floor itself as §17 Q11. The per-tool figure is now a **1 KB ceiling on intent tools specifically**, which the measurement shows is what decides whether the budget is met. |
| 14 | MOD | §7 was convenience-scoped. §3 still says building §6 alone and calling it an end-to-end guarantee is "the most expensive available mistake"; decision 18 is a transfer with no named recipient. | Verified — both sentences are in the document | **OPEN** |
| 15 | MOD | Job-per-request breaks three assumptions: cold resolvers per job, sidecar path/ownership/retention unspecified, and pool load-or-reset requires the missing file-lifecycle code (#3). | Verified | **OPEN** |
| 16 | MOD | §8.1's manifest is insufficient: missing engine build/device, samples/denoiser/seed, **facet-schema version**, external texture hashes, AOV inventory, exception expiry, and any tamper-evidence. | Verified | **OPEN** |
| 17 | MINOR | `bundles.py:29` is `retopology`; `texture-lighting` is `:30`. | Verified | **FIXED** |

**Absent entirely** (no finding number; nothing in the spec addresses these): who fingerprints
the canon library itself when it is republished; the repair workflow when `assert_consistency`
fails on 40 shots at once; cost in tokens, container-minutes and sidecar storage; concurrency
on the canon and shot registries; and facet-schema evolution invalidating stored baselines.

**Sound, per the reviewer:** §10 plugin architecture (verified verbatim against
`server_core.py:177,183-184,250,383-385`), §6.4 preset providers, and the pure-hasher /
`bpy`-extractor split in §13. Spot-checked citations that hold: `transaction.py` lacking
`libraries`, the exposure/gamma path, `polyhaven.py:362`, `connection.py:187`, one prompt
and zero resources, 285 tools, schemas at 76–77.5%, top-20 at 23%, the 57% sim-family share,
and the per-family byte table within 1.5%.
