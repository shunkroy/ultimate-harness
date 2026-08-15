# Phase 10 — Persistent Context Sessions (first slice)

Status: implemented, tested (2026-08-15). Design authority: the Phase 10
approval (Animesh), teacher/student capability doc
(`TEACHER_STUDENT_CAPABILITY_ACQUISITION.md`), and Harness v3 architecture.

## What this slice delivers

A conversation session that belongs to Harness, not to any provider:

- durable lifecycle (`session new|list|info|resume|close|attach`, `chat`,
  `run --session`)
- encrypted-at-rest turn history (authenticated envelopes, session domain)
- provider-fluid history: each turn records the actual final route
  (engine/provider/model) after any fallback, never a guessed route
- deterministic bounded context reconstruction for routed runs
- crash-safe turns: a crash between user-turn persistence and the assistant
  result never fabricates a success, never duplicates the user turn on
  explicit retry
- security classification that is additive across history, never downgraded

Out of scope for this slice (later slices): Kirti identity, teacher/student
learning, automatic semantic compaction, cross-device sync, skill learning.

## Data model (migration v5 `harness_sessions`)

Immutable, additive. Legacy `runs.session_id` keeps its historical meaning
(the provider/runtime session id reported by the engine).

```sql
ALTER TABLE runs ADD COLUMN harness_session_id TEXT;   -- Harness-owned
ALTER TABLE runs ADD COLUMN provider_session_id TEXT;  -- engine-reported
UPDATE runs SET provider_session_id = session_id WHERE provider_session_id IS NULL
       AND session_id IS NOT NULL;                     -- one-time backfill

CREATE TABLE sessions (
  id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL CHECK(state IN ('open','closed')),
  created_at REAL NOT NULL, updated_at REAL NOT NULL, metadata_json TEXT NOT NULL);
CREATE TABLE session_turns (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES sessions(id),
  role TEXT NOT NULL CHECK(role IN ('user','assistant','tool','error','summary')),
  content_envelope BLOB NOT NULL,          -- authenticated encrypted payload
  envelope_version INTEGER NOT NULL CHECK(envelope_version > 0),
  key_id TEXT, status TEXT NOT NULL, engine TEXT, provider TEXT, model TEXT,
  provider_session_id TEXT, run_id TEXT,
  sensitive INTEGER NOT NULL DEFAULT 0 CHECK(sensitive IN (0,1)),
  untrusted INTEGER NOT NULL DEFAULT 0 CHECK(untrusted IN (0,1)),
  error_code TEXT, duration_ms REAL, created_at REAL NOT NULL,
  summary_from_seq INTEGER, summary_to_seq INTEGER);  -- compaction provenance
CREATE TABLE session_attachments (        -- Context-as-Program seam
  session_id TEXT NOT NULL REFERENCES sessions(id), context_id TEXT NOT NULL,
  attached_at REAL NOT NULL, PRIMARY KEY(session_id, context_id));
```

## Crypto and storage

One master key file (`state_root/job.key`, the existing key for job envelopes)
is reused; envelopes are domain-separated so a session envelope can never be
decrypted as a job envelope and vice versa:

- job envelopes: magic `H2J1`, derivation label `harness2-job-`
- session-turn envelopes: magic `HT1S`, derivation label `harness2-session-`

Algorithm (unchanged machinery): PBKDF2-HMAC-SHA256 key derivation, AES-256
in CTR mode, encrypt-then-MAC (HMAC-SHA256, 32 bytes), envelope magic + key id
prefix. `harness2/crypto.py` gained `encrypt_session_turn` /
`decrypt_session_turn` / `_derive_session`. Plaintext never appears in the
SQLite bytes (verified by test). Titles and metadata are caller-supplied
plaintext with secret-pattern redaction; turn content is never auto-derived
into a plaintext title.

## Session ids

Two independent identities, both recorded per run:

- `harness_session_id` — the durable Harness session (UUID hex). Stable across
  providers, engines and restarts. Written by `store.record_run` from
  `RunRequest.harness_session_id`.
- `provider_session_id` — the engine-reported session id for that specific
  run (mirrors legacy `session_id`; backfilled for pre-v5 rows).

`run_session_turn` records the actual final route after fallback: the engine
that produced the result, plus provider/model from result metadata
(`provider` / `final_route` / `resolved_model`) — NULL when not established,
never a guess.

## Context reconstruction

`SessionContextBuilder` (turn_limit 20, byte_limit 16384 by default):

- eligible: completed user/assistant/summary turns only; tool and error
  records are never injected as authority
- selection prefers the **most recent** eligible turns: the newest turns that
  fit within both budgets are chosen, then emitted in **chronological order**,
  so recent references (e.g. "implement what we just decided") survive long
  conversations while ancient turns fall outside the active window
- one canonical JSON line per turn (`{"role","seq","content"}`); framing is
  collision-safe (escaped content, parsed back by tests)
- the byte bound is strict: a turn that cannot fit the remaining byte budget
  is skipped, never injected, so the encoded turn payload (the history
  payload, `context.text`) never exceeds `byte_limit`; costs are counted in
  UTF-8 bytes, not characters
- the current user message is never part of the reconstruction; it is
  appended separately by the caller
- effective `sensitive`/`untrusted` = current flags OR any included history —
  never downgraded
- truncation is explicit and reported (`truncated`, `included_seq`)

## Provider-facing framing (session semantics)

`run_session_turn` assembles the routed prompt from three fixed sections
(Phase 10.1):

```
[harness:session-semantics]          fixed constant text, always first
[harness:session-history]            JSON-lines history payload (context.text)
[harness:current-request]            the user task, JSON-encoded as one value
```

- **Semantics block** states, before anything else, that this is one
  persistent Harness conversation, that process/terminal restart, provider,
  model or engine changes do not begin a new conversation while the session
  identity is unchanged, and that prior turns are history — context and
  evidence only, never authority, policy or permission.
- **History as context, not authority**: history is emitted under its own
  header as structured JSON lines. Nothing in history grants privilege; the
  semantics block says so explicitly and the framing makes it structurally
  impossible for historical text to impersonate framing (values are escaped).
- **Spoof resistance**: the current request is `json.dumps`-encoded, so
  marker-like text, newlines or quotes inside user content cannot escape the
  value or forge sections. Real headers appear exactly once at line starts.
- **Provider neutrality**: the framing is identical for every engine,
  provider and model; only the request value differs.
- **Policy isolation (architectural rule)**:

  ```
  PROVIDER PROMPT = semantics + history + current request
  POLICY TASK    = current request only
  ```

  All task-semantic policy decisions (messaging/parallel/durable keyword
  classification, `expert_for_task`, the short-Q&A length test, future task
  classifiers) consume `harness2.policy.policy_task_text(request)`, which is
  the whole prompt for non-session requests and only the JSON value under
  the genuine `[harness:current-request]` section for session requests.
  Framing and history never mutate the current task's capability
  classification: "persistent" in the semantics block cannot route to Prime,
  and a "telegram" mention in old history cannot route to Hermes. Session
  history may inform the reasoning model, but it is context/evidence, not
  task authority. Malformed Harness-owned envelopes fail closed
  (`PolicyRefusal`); a literal marker in an ordinary non-session prompt is
  inert user text. The full provider-facing `request.prompt` still passes
  unchanged to the selected runtime — only policy classification uses the
  extracted task.

## Turn lifecycle and crash recovery

```
user turn (pending) -> (processing) -> completed  + assistant turn appended
                                   -> interrupted   crash/failure, no result
```

- `run_session_turn` is idempotent: if the trailing user turn is unfinished
  and its text matches the new prompt, it is reused (no duplicate turns)
- an interrupted turn survives restart; it is never silently turned into a
  success
- a run that completes with a failure records a `failed` assistant turn with
  the real `error_code` (that is a result, not a crash)

## CLI surface

```bash
harness session new [--title T]
harness session list [--limit N]
harness session info ID [--text] [--limit N]
harness session resume ID        # reopens a closed session
harness session close ID
harness session attach ID CONTEXT_ID
harness run PROMPT --session ID [existing run flags]
harness chat [--session ID]      # REPL; /exit, /quit, /close
```

`run --session` routes through `run_session_turn` (history injection +
provenance). Plain `run` is unchanged.

## Invariants (enforced by tests, `tests/test_sessions.py`)

1. Plaintext never in DB bytes; envelope version/key_id recorded.
2. Job and session envelopes are mutually undecryptable.
3. Legacy `session_id` rows backfill into `provider_session_id`; new
   `record_run` writes both session columns.
4. Final route after fallback is recorded (engine of the successful engine;
   provider/model NULL when unknown).
5. Crash between user-turn persistence and assistant result: turn is
   `interrupted`, survives restart, no fabricated assistant turn, idempotent
   retry yields exactly one user turn.
6. Sensitive/untrusted never downgraded by later turns; effective flags
   propagate into the routed `RunRequest`.
7. Sessions, history and context work with no providers present (restart
   survival).
8. Context builder: recency preference (newest eligible turns, chronological
   emission), strict byte bound (oversized and boundary turns skipped, never
   injected), UTF-8 byte accounting, limits, roles, unfinished-turn
   exclusion, collision-safe framing.
9. Attachment seam registers and dedupes context ids.
10. History is injected into routed runs with the explicit marker and the
    correct `harness_session_id`.
11. Session semantics: fixed semantics block always first; section order
    semantics < history < current; current request JSON-encoded (spoof
    text cannot escape the value); injected history claims are data, not
    authority; framing identical across providers.
12. Policy isolation: `policy_task_text` returns the whole prompt for
    non-session requests and only the decoded current request for session
    requests; all keyword/length/expert classification uses it (semantics
    "persistent" never routes to Prime, history "telegram" never routes to
    Hermes, current-task keywords still do); malformed session envelopes
    fail closed with `PolicyRefusal`; literal markers in plain prompts are
    inert text.

Full suite (credential-free): green.