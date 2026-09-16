# Phase 4 work item — Socket authentication

**Design:** spec §4.8 (`docs/superpowers/specs/2026-09-11-episode-consistency-architecture-design.md`),
written against `f24e01e`. **Status:** not started. **Produced by:** Phase 2 Task 8 (design only, no code).

This is the implementation plan §4.8 hands to Phase 4. It builds the chosen mechanism — a per-user secret
file, proven by HMAC-SHA256 challenge-response once per connection in `handle_client`, enforced unless
`BLENDERMCP_SOCKET_AUTH=off` — and nothing else. Out of scope, with owners: HTTP transport authentication
(Phase 5); per-command authorization (none planned while one Blender process serves one tenant); the
hostile-`.blend` controls 1-3 (Phase 2 Tasks 5, 6 and 7). Control 4, session auto-exec flag detection, is
Task 9 below and is P1 for pooled or untrusted deployments.

## Before starting: re-derive every citation

Line numbers in §4.8 are at `f24e01e` and Phase 2 Task 7 moves `server_core.py`. Re-run these and
reconcile against §4.8's audit table before writing code. A site whose construct no longer interpolates an
exception, prints a traceback or echoes peer text drops off the list, and the change is recorded.

```bash
# Addon: interpolated exceptions and tracebacks (at f24e01e: 14 and 10, one of the 10 inside a comment)
git grep -nE 'print\(f".*\{(e|exc|err|error)(!s|!r)?\}' -- src/blender_mcp/bundled/addon
git grep -n 'traceback.print_exc()' -- src/blender_mcp/bundled/addon
# Multi-line prints the one-line pattern misses
git grep -n -A3 'print($' -- src/blender_mcp/bundled/addon | grep -E '\{(e|exc|err)'
# Addon: exception text returned to a client
git grep -nE '"message": str\((e|exc)\)' -- src/blender_mcp/bundled/addon/server_core.py
# Server: log and error sites on the connection path
grep -nE 'logger\.(error|warning|info|debug)\(f".*\{e|raise Exception\(f".*\{e|response_data\[' src/blender_mcp/server/connection.py
grep -nE '\{e\}|\{e!s\}' src/blender_mcp/addon_manager.py src/blender_mcp/server/app.py
# Tool layer: exception text into an agent's context (at f24e01e: 47)
git grep -nE 'ToolError\(f".*\{(e|exc|err)(!s|!r)?\}' -- src/blender_mcp/server/tools | wc -l
# Every client of the addon socket (all must speak the exchange)
git grep -n 'create_connection\|\.connect((' -- src scripts docker
# Every construction of the addon server (all bind the default host at f24e01e)
git grep -n 'BlenderMCPServer(' -- src scripts docker
```

## Tasks, in order

### 1. Proof construction and secret file, `bpy`-free, on both sides

- **Files:** new `src/blender_mcp/bundled/addon/socket_auth.py`; new `src/blender_mcp/server/socket_auth.py`.
  Two copies, because the addon installs as a self-contained package and must not import the server
  (the precedent is Phase 2's T3-11 control-character block, duplicated and pinned by a test).
- **Content:** nonce generation (`secrets.token_bytes(32)`, hex on the wire); `proof(secret, role, client_nonce,
  server_nonce)` = HMAC-SHA256 over `b"blender-mcp-socket-auth-v1\n" + role + b"\n" + client_nonce + b"\n" +
  server_nonce`, roles `addon` and `client`; `verify` via `hmac.compare_digest`; secret-file resolution
  (`BLENDERMCP_SOCKET_SECRET_FILE`, else a per-user config default); addon-only `ensure_secret_file` using
  a temp file in the target directory (`O_CREAT | O_EXCL | O_WRONLY`, `0o600`), then `os.link` to the final
  name — which fails if a winner exists, in which case read the winner's — so no reader sees an empty file.
  The reader opens with `O_NOFOLLOW` and refuses group/other mode bits, `st_uid != os.geteuid()`, and
  contents shorter than 32 bytes.
- **Errors:** every exception raised by these modules carries constant text naming
  `BLENDERMCP_SOCKET_SECRET_FILE` and the failure kind. No path of the secret's *contents*, no `{e}`.
- **Tests:** `tests/test_socket_auth.py` — a fixed vector computed once and asserted identically against
  both copies; role separation (an `addon` proof fails as a `client` proof); a nonce change fails
  verification; two racing `ensure_secret_file` calls end with one file both read and no reader ever sees it
  empty; a `0o644` file, a symlink, a file owned by another uid, and an empty or 31-byte file are each refused
  on POSIX; a planted secret appears in no raised exception's `str()` or `repr()` across every error branch.
- **Acceptance:** both copies agree on the vector; `basedpyright` clean; neither module imports `bpy`
  (asserted by a test that imports each with `bpy` absent from `sys.modules`).

### 2. Addon: authenticate in `handle_client`, before anything is queued

- **Files:** `src/blender_mcp/bundled/addon/server_core.py` (`start`, `_server_loop`, `handle_client`, a new
  `_authenticate_frame`); `src/blender_mcp/bundled/addon/__init__.py` (read the opt-out).
- **Behaviour:** `start()` loads or creates the secret before binding; a load failure prints constant text and
  does not start. Per connection, until authenticated, each line goes to `_authenticate_frame`, never to
  `_decode_and_queue_frame`. Accepted: `auth_hello{client_nonce}` (exactly 64 hex characters, i.e. 32 bytes;
  anything else is refused) → `{scheme, protocol_version, server_nonce,
  addon_proof}`, then `auth_proof{client_proof}` → `{authenticated: true}`. Anything else, a second
  `auth_hello`, or a bad proof: one error frame with constant text, then close. Pre-auth frame cap (4 KiB),
  pre-auth deadline (5 s, measured against the slowest rig host before fixing the number), and a cap on
  concurrently unauthenticated connections enforced in `_server_loop`. Frames pipelined after a valid
  `auth_proof` in the same `recv` buffer are processed normally. No lockout.
- **Refusal text** must contain neither `unknown command` nor `get_addon_info` (`addon_manager.py`'s outdated
  classifier matches both, case-insensitively).
- **Tests:** in `tests/` beside the existing `handle_client` framing tests: an unauthenticated
  `{"type": "save_shot", ...}` is never queued and the socket closes; a JSON command inside an HTTP POST body
  is never queued; a short, long or non-hex `client_nonce` is refused; an oversized pre-auth frame closes; a silent peer is closed at the deadline; the cap refuses
  the next unauthenticated peer while authenticated peers keep working; a valid exchange followed by a
  pipelined command in one buffer queues exactly that command; two connections authenticate independently
  and their commands interleave in the shared queue.
- **Acceptance:** at no point does an unauthenticated connection put an entry in `command_queue` (asserted
  on the queue, not on the response); Phase 2's drain-path and barrier tests pass unchanged.

### 3. Server: the exchange inside `BlenderConnection.connect()`

- **Files:** `src/blender_mcp/server/connection.py`.
- **Behaviour:** `connect()` runs the exchange after the TCP connect and before returning `True`, so the
  silent reconnect in `send_command_locked` cannot skip it. It verifies `addon_proof` before sending its own.
  An addon answering `Unknown command type: auth_hello` fails closed with an actionable message (update the
  addon, or set `BLENDERMCP_SOCKET_AUTH=off` on both sides). The secret is re-read on every `connect()`, and
  is not stored on the dataclass (its generated `repr` would include it).
- **Tests:** `tests/server/` — a fake addon with the right secret connects; wrong secret, a missing file and a
  squatter that cannot produce `addon_proof` each fail with the constant message; a reconnect after a dropped
  socket re-authenticates; the handshake still runs once per process after the first successful connect.
- **Acceptance:** with a planted secret, the secret's bytes are absent from every log record and every raised
  exception on all failure paths (`caplog` over the whole test).

### 4. Publish the mode; bump the protocol once

- **Files:** `server_core.py` (`get_addon_info`), `bundled/addon/__init__.py` (`ADDON_PROTOCOL_VERSION`),
  `src/blender_mcp/addon_manager.py` (`EXPECTED_ADDON_PROTOCOL_VERSION`, `AddonHandshake.socket_auth_enforced`,
  its parse), `src/blender_mcp/server/tools/core.py` (`get_addon_status`), `README.md` (Connection section).
- **Protocol:** the next unused number. 32 is reserved for the inline image transport (Phase 2 decision 5);
  confirm what is taken before choosing.
- **Tests:** the existing protocol-pair test; handshake defaults `socket_auth_enforced` to `False` for an older
  payload; `test_get_addon_status_documents_every_key_it_returns` covers the new key; the shot payload stays
  under its pinned ceiling (trim, do not raise — Phase 2 decision 6).
- **Acceptance:** a client can tell enforced from opted-out without attempting a command.

### 5. The opt-out, and its refusal beside a non-loopback bind

- **Files:** `server_core.py` (`start`), `connection.py`, `README.md`.
- **Behaviour:** `BLENDERMCP_SOCKET_AUTH` is read with `os.environ` **only** — never `_get_config_value`, addon
  preferences or a Scene property, so an opened `.blend` cannot switch authentication off. `off` disables the exchange on the side that sets it, prints or logs a
  warning naming the cost, and publishes `socket_auth_enforced: false`. The addon refuses to start with the
  opt-out and a host other than loopback. A value other than `off` or unset is refused by name.
- **Tests:** each branch; a mismatched pair (server off, addon on) fails with the constant refusal; a Scene
  property or addon preference named like the variable has no effect.
- **Acceptance:** authentication is off only when the process environment says `off`, and every such process
  publishes `socket_auth_enforced: false` and warns; the addon never starts opted-out on a non-loopback host.

### 6. Every other client of the socket

- **Files, from the grep at `f24e01e`:** `docker/blender/healthcheck.py`, `docker/blender/entrypoint.sh`
  (the readiness probe's inline Python), `scripts/blender_rig.py` (three `create_connection` sites),
  `scripts/rig_scenarios/scenario_file_swap_barrier.py`, `scripts/revert_matrix.py`. Re-run the grep: Phase 2
  Tasks 7 and 10 may add more. Each uses the server-side `socket_auth` helper rather than a third copy.
- **Tests:** for each script, the nearest existing test (or a new one beside it) drives the client against a
  fake authenticated addon and asserts it completes the exchange, and fails with the constant refusal against
  a wrong secret; the rig's unit tests cover `BLENDERMCP_SOCKET_SECRET_FILE` propagation to Blender.
- **Pool:** a fresh secret per job, written to a job-scoped path set through `BLENDERMCP_SOCKET_SECRET_FILE`
  — a per-user default file outlives the job, and a process left from the previous job would authenticate
  into the next (spec §4.8; the same uid objection that rejected the Unix socket).
- **Rig:** the rig isolates user resources under `--work-dir`; it passes `BLENDERMCP_SOCKET_SECRET_FILE`
  explicitly to both Blender and itself instead of relying on the default path.
- **Container:** the compose file mounts nothing new — both processes start from one entrypoint as one user
  and share the default path; confirm, don't assume.
- **Phase 3 plugin (§4.7):** runs inside Blender and reads the same file; no special path.
- **Acceptance:** `docker compose ... up --build --wait` reaches healthy; the rig's Phase 2 scenarios pass.

### 7. The leak test for the class, not the instances

- **Files:** new `tests/test_socket_auth_leakage.py`.
- **Behaviour:** plant a recognisable secret; drive every failure path of Tasks 2, 3 and 5 (bad proof, missing
  file, unreadable file, wrong mode, deadline, cap, oversize, squatter, old addon, reconnect failure) plus one
  failing ordinary command after a successful exchange; capture stdout, stderr, every log record and every
  frame written in both directions, **and every MCP tool result** — `ToolError` text and successful tool
  payloads, driven through at least `get_addon_status` and one tool per failing connect path. Assert the
  secret's bytes, hex and base64 forms are absent.
- **Audit sites it must exercise** (§4.8's table, at `f24e01e`; re-derive first): `server_core.py:252`,
  `:1578`, `:1581` (where addon-side auth failures land); `connection.py:130` and `server/app.py:54` (where
  connect-path failures land); the tool layer's 47 `raise ToolError(f"…{e}")` sites, e.g. `tools/mesh.py:72`,
  `tools/core.py:117` (where they reach an agent's context). Not connect-path, recorded so they are not
  re-added: `connection.py:302`/`:305` (`connect()` is called at `:227`, outside the `try` at `:232`, and
  swallows its own exceptions), `connection.py:333` and `addon_manager.py:900` (handshake, after a successful
  connect), `addon_manager.py:185`/`:191` (`check_addon_status_on_startup` scans folders, never connects).
  The dispatch-path sites (`server_core.py:501-502`, `:560-561`, `:697-699`, `:1343-1344`, `:1603-1605`,
  `:2086-2088`, `:2511-2513`, `handlers/file_lifecycle.py:480`, `transaction.py:209`) are covered by the
  post-authentication failing command, which proves the secret is not in `command`.
- **Acceptance:** green; and a deliberate revert that adds the secret to one `print` makes it fail (record the
  revert in the revert matrix).

### 8. Live verification

- **Files:** no production files. Transcripts go to the Phase 4 task state; any probe goes under
  `scripts/blender_probes/` and is committed with the evidence.
- GUI rig on the current Blender: the exchange succeeds; `hmac` and `secrets` import in Blender's bundled
  Python; an unauthenticated `nc 127.0.0.1 <port>` sending a `save_shot` frame writes no file.
- Container: healthy; the MCP HTTP endpoint still works end to end.
- **Windows:** verify the default secret path, that the created file is readable by the owning user only,
  and that the rig runs. Not measured in Phase 2; if the file ACL is not user-only, the default path changes
  before this item closes.
- **Acceptance:** (1) the rig transcript shows a successful exchange and an unauthenticated `save_shot` frame
  leaving no file on disk; (2) the container reaches healthy with authentication enforced and
  `socket_auth_enforced: true` in `get_addon_status`; (3) the Windows result is recorded, and the default path
  is changed if the file is not user-only; (4) transcripts are committed, not left in a scratchpad.

### 9. Detect Blender's session auto-exec flag and refuse (P1 for pooled or untrusted deployments)

Independent of Tasks 1-8; may run in parallel. Spec §4.8, control 4.

- **Why:** a linked library's Python driver is gated by `G.f & G_FLAG_SCRIPT_AUTOEXEC`, not by the
  preference Phase 2 checks. Measured on 5.2.2 (Phase 2 Task 7's critic): under `-y` the preference reads
  `False` and a hostile driver ran after `link_canon_library`, `create_override` and `reload_library`; the
  flag survives `reset_session`; "Reload Trusted" sets it without `-y`; `open_shot`'s explicit
  `use_scripts=False` clears it. `bpy.app` has no reader (`autoexec_fail*` only report a blocked driver).
- **Files:** `src/blender_mcp/bundled/addon/handlers/file_lifecycle.py` (a `_refuse_session_autoexec` beside
  `_refuse_scripts_auto_execute`), its call sites in the linking handlers and `handlers/polyhaven.py`,
  `server_core.py` (publish `session_autoexec` in `get_addon_info`), `README.md`.
- **Mechanism (canary):** evaluate a scripted driver `__import__('math').floor(1.5)` on a throwaway
  datablock; `1.0` means scripts run, `0.0` means blocked. Refuse the load on `1.0` or on any failure to
  evaluate. Do **not** clear the flag by re-assigning the preference: it dirties preferences and silently
  revokes a trust the user granted. Gated on deployment: enforced when `BLENDERMCP_SOCKET_AUTH` is not `off`
  *and* file roots are enforced, or by an explicit `BLENDERMCP_REFUSE_SESSION_AUTOEXEC=1`; the local and
  gateway-fronted shapes keep Blender's own trust (user decision, 2026-09-16).
- **Measure first, and record:** whether creating and removing the canary marks `bpy.data.is_dirty` (it
  must not turn a clean session dirty, or `open_shot`'s dirty guard starts refusing); whether it leaves an
  orphan, a depsgraph update, an undo step or a `autoexec_fail` latch; its cost per call; and whether one
  evaluation per command is needed or the flag can only change at a load/revert (so it can be cached per
  session epoch).
- **Tests:** unit tests with the canary result stubbed (refuse on `1.0`, on an exception, and on an
  unexpected value; pass on `0.0`); a **real-Blender** probe under `scripts/blender_probes/` and a rig
  scenario that launch Blender with `-y`, confirm `use_scripts_auto_execute` reads `False`, and assert
  `link_canon_library`, `reload_library` and `import_polyhaven_asset` (against a local fixture) are refused
  and a hostile library's driver did not run; the same scenario without `-y` passes; a Reload Trusted
  session is refused; `open_shot` then clears it and loads succeed again.
- **Acceptance:** under `-y`, no hostile driver runs through any library-loading command in an enforced
  deployment; the canary leaves `is_dirty`, preferences and the undo stack as measured-unchanged (or the
  deviation is recorded and mitigated); `get_addon_info` reports the flag state so a pool can refuse the
  instance before dispatching a job.

## Phase 2 hardening backlog, prioritised

Ranked by realistic exploitability **once §4.8 is built**, per deployment (L = local workstation, D = Docker
rig, P = hosted pool). After authentication the realistic senders are an authenticated-but-manipulated
client (a prompt-injected agent), a party with write access inside a root, and the *content* of a file —
not an arbitrary socket peer. **P1**: fix before the deployment it names ships — or, for the Docker rig, which already ships, before its
exposure widens beyond host loopback. **P2**: fix in Phase 4 or
before pooling. **P3**: fix opportunistically. **Moot** means the finding's exploit needed an
unauthenticated peer.

Source: PHASE2_TASK_STATE "Hardening backlog (decision 13)", every row in table order, then the Task 3
residuals and rulings that name Task 8.

| # | Source | Finding | Priority | Reasoning | Moot under §4.8 |
|---|---|---|---|---|---|
| 1 | Task 4 | Nested transactions invalidate only the innermost | P3 | Unreachable: one caller, never nested; a latent bug for a future call site, not an attack surface | No |
| 2 | Task 4 | Unflagged `lib.reload()` inside a transaction destroys contents | P3 | No such call in `src/` at `f24e01e`; re-check when Task 7's reload commands land | No |
| 3 | Task 4 | `_on_load_post` invalidates before moving the epoch | P3 | Unreachable while invalidation only assigns attributes | No |
| 4 | Task 4 | Probe scripts leave `.blend` files in the temp dir | P3 | Developer-machine clutter; in no deployment | No |
| 5 | Task 5 | Sanitizer leaves a relative tail in some messages | P3 | Directory-name fragments, never absolute, and closed wherever callers pass `known_paths` | No |
| 6 | Task 5 | `file:///`, `path:/`, `{/…}`, backtick path forms undetected | P3 | No real Blender message produces them | No |
| 7 | Task 5 | `known_paths` replaced as plain substrings | P3 | Callers pass validated paths; the failure is garbled text, not disclosure | No |
| 8 | Task 5 | `_has_ancestor_directory` trusts inode numbers on FUSE/SMB | P2 (P), P3 (L, D) | Canon on a network mount is the expected pool shape, and there containment is the only bound on a manipulated agent | No |
| 9 | Task 5 | Second main-thread stat walk; a dead mount stalls the drain loop | P2 (P), P3 (L, D) | Availability, not access: a dead canon mount stalls every client of the instance; authentication removes stranger-triggered handshakes, not the stall | No |
| 10 | Task 5 | `sanitize_blender_error` output skips control-character stripping | P2 | Blender error text can quote file-author names from a hostile `.blend`, which authentication does not filter; one call to add | No |
| 11 | Task 5 | Symlink swapped between validation and open (TOCTOU) | P2 (P), P3 (L, D) | Needs write access inside a root; in a pool that is another job if roots are shared — answer with per-job roots | No |
| 12 | Task 5 | No resolve test with a space in a directory name | P3 | Coverage gap; `os.path` handles spaces | No |
| 13 | Task 6 | A refused swap discards every other process's queued commands | P3 | The hostile half (`{"type": "reset_session"}` from anyone) needs an unauthenticated peer; the benign multi-process half costs resends only | **Hostile half moot** |
| 14 | Task 6 | Every `save_shot` ends the drain tick, even a refused one | P3 | A throttling flood needs a hostile peer; what remains is throughput | **Moot** as an attack |
| 15 | Task 6 | TOCTOU on `<target>@` before the save operator | P2 (P), P3 (L, D) | Same precondition as row 11; not closable from Python, so the pool answer is per-job writable roots | No |
| 16 | Task 6 | A third-party save plus an edit clears `is_dirty`; a later open destroys the edit | P3 | Needs another add-on's save path; no socket command reaches it | No |
| 17 | Task 6 | `_unresolvable_relative_paths` runs outside any `try` | P3 | Could not be made to raise; the cost is a refused save or a spurious warning | No |
| 18 | Task 6 | `scene_name` via `client_safe_text`, not the leaf allowlist | P3 | File-author text already stripped of control characters | No |
| 19 | Task 6 | No headless test drives Task 6 handlers through `_drain_batch` | P3 | Coverage, not exposure — but land it before work-item Task 2, which edits the adjacent receive path | No |
| 20 | Task 6 | T3-7: same-path re-open during the handler gap leaves stale capabilities | P3 | Single local user, no data loss; no remote sender triggers it | No |
| 21 | Task 6 | `client_safe_leaf`'s `isdir` existence oracle with roots unset | P3 | With roots unset an authenticated client can already open any path; probing adds nothing | **Moot** |
| 22 | Task 3 residual (Task 8 / Task 9) | 60 `result.get(` sites put addon values into tool payloads without hygiene | P2 | Authentication does not filter file-author text (object, material, library names) on its way into an agent's context; Task 9 lands it | No |
| 23 | T3-14 | `current_filepath` published absolute | P2 (P), P3 (L, D) | Its "marginal disclosure is zero" argument rested on the socket being open to all; once authenticated the reader is the tenant's own agent, and pool layout then enters model context. Remedy: `_library_summary`'s leaf-plus-relative shape, under an hour | No — the justification inverts |
| 24 | Decision 10 / Task 1 residual | MCP HTTP endpoint unauthenticated; `Host` validation is not access control | **P1 (P); P1 (D) before its publish widens**, P3 (L, stdio) | §4.8 does not touch it, and it is the outer door onto the same commands; the shipped rig's safety is compose's `127.0.0.1` publish alone | No |

**Found by Task 8, not in the backlog:**

| # | Finding | Priority | Reasoning |
|---|---|---|---|
| 25 | `_refuse_scripts_auto_execute` has one call site (`open_shot`); Task 7's `link_canon_library` / `reload_library` / `relocate_library` did not call it at `f24e01e`. Poly Haven's import (`handlers/polyhaven.py:391` at `f24e01e`) had no check. **Closed for the preference in Phase 2 Task 7** (linking commands and Poly Haven); the session-flag gap is row 28 | **Done in Phase 2** (preference half) | Decision #7; Poly Haven's is the one load whose bytes come from the network |
| 26 | `render_scene`, `save_texture_image`, `get_viewport_screenshot`, the three `export_*` commands, `load_texture_image` and `inspect_render_output` take paths that `enforce_roots` never sees | P1 (P), P2 (L, D) | Once authenticated, roots are the only bound on a manipulated agent, and these read or write anywhere the process can |
| 27 | Scene properties gate Poly Haven, Sketchfab and ND commands, and `_get_config_value` reads a Scene-level Sketchfab key, so an opened `.blend` can widen the command set and supply a credential | P2 | Authentication does not filter file content; pool jobs open files they did not author |
| 28 | Blender's session auto-exec flag (`-y`, Reload Trusted, surviving `reset_session`) runs linked-library Python drivers while the preference reads `False`; `bpy.app` has no reader (measured on 5.2.2, Task 7 critic) | **P1 (P, and any untrusted deployment)**; accepted for L and gateway-fronted (user decision, 2026-09-16) | Arbitrary code execution through `link_canon_library` / `create_override` / `reload_library` / Poly Haven import; authentication does not close it. Work-item Task 9 |

**Becomes moot under §4.8:** rows 13 (the hostile half), 14 and 21. Row 23's rationale does not become moot;
it reverses. Everything else is independent of who may connect.
