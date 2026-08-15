"""Phase 10 first slice: persistent encrypted context sessions.

Acceptance coverage (see docs/architecture/PHASE10_SESSIONS.md):
  1. encrypted-at-rest turn content (plaintext never in the SQLite bytes)
  2. domain-separated envelopes (job envelopes and session envelopes cannot
     be decrypted with each other's keys)
  3. legacy run-session compatibility (backfill provider_session_id from
     session_id; new record_run writes both columns)
  4. actual final-route provenance after fallback
  5. crash between user-turn persistence and assistant result (no fabricated
     success; idempotent retry reuses the trailing unfinished turn)
  6. sensitive/untrusted classification is never downgraded
  7. no-provider survival (list/info/history work after restart)
  8. deterministic bounded context reconstruction (roles, limits, framing)
  9. attachment seam (Context-as-Program registration)
 10. context injection into routed runs (harness_session_id + explicit marker)
"""

import json
import os
import sqlite3
import tempfile
import unittest

try:
    from .test_provider_routing import FakeEngine
except ImportError:  # pragma: no cover - discovery mode
    from test_provider_routing import FakeEngine

from harness2.crypto import (
    CryptoError, decrypt, decrypt_session_turn, encrypt, encrypt_session_turn,
    load_or_create_key,
)
from harness2.kernel.migrations import MIGRATIONS, Migrator
from harness2.models import CapabilityStatus, EngineResult, EngineStatus, RoutingDecision, RunRequest
from harness2.orchestrator import Orchestrator
from harness2.sessions import (
    SESSION_ENVELOPE_VERSION, SessionClosedError, SessionContextBuilder, SessionError,
    SessionService, run_session_turn,
)
from harness2.store import SCHEMA, Store


def make_store() -> tuple[Store, str]:
    tmp = tempfile.mkdtemp(prefix="harness2-sessions-")
    return Store(os.path.join(tmp, "harness.sqlite")), tmp


def make_service(store: Store, tmp: str) -> SessionService:
    return SessionService(store, key_path=os.path.join(tmp, "job.key"))


def make_decision(engine: str = "local") -> RoutingDecision:
    return RoutingDecision(engine, None, None, "test")


class FakeForeground:
    def __init__(self, result):
        self._result = result
        self.calls: list = []

    def run(self, request: RunRequest):
        self.calls.append(request)
        if isinstance(self._result, Exception):
            raise self._result
        return make_decision(), self._result, "run-1"


class SessionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.store, self.tmp = make_store()
        self.service = make_service(self.store, self.tmp)

    def test_lifecycle_new_list_info_resume_close(self):
        created = self.service.create(title="kirti planning")
        sid = created["id"]
        self.assertEqual(created["state"], "open")
        listing = self.service.list()
        self.assertTrue(any(s["id"] == sid and s["title"] == "kirti planning" for s in listing))
        info = self.service.info(sid)
        self.assertEqual(info["state"], "open")
        self.assertEqual(info["turns"], 0)
        self.service.close(sid)
        with self.assertRaises(SessionClosedError):
            self.service.append_user_turn(sid, "hello?")
        resumed = self.service.resume(sid)
        self.assertEqual(resumed["state"], "open")
        self.service.append_user_turn(sid, "hello again")

    def test_turn_append_and_metadata(self):
        sid = self.service.create()["id"]
        user = self.service.append_user_turn(sid, "remember kirti", sensitive=True)
        self.assertEqual(user.status, "pending")
        self.assertTrue(user.sensitive)
        assistant = self.service.append_assistant_turn(
            sid, user.seq, text="noted", engine="opencode", provider="groq",
            model="qwen", provider_session_id="prov-1", run_id="run-9",
            sensitive=True, untrusted=False, success=True, duration_ms=12.5,
        )
        self.assertEqual(assistant.status, "completed")
        info = self.service.info(sid, include_text=True)
        self.assertEqual([t["status"] for t in info["history"]], ["completed", "completed"])
        self.assertEqual(info["history"][1]["engine"], "opencode")
        self.assertEqual(info["history"][1]["provider"], "groq")
        self.assertEqual(info["history"][1]["model"], "qwen")
        self.assertEqual(info["history"][1]["provider_session_id"], "prov-1")
        self.assertEqual(info["history"][1]["run_id"], "run-9")
        self.assertEqual(info["history"][1]["duration_ms"], 12.5)
        self.assertEqual(info["history"][0]["text"], "remember kirti")

    def test_info_without_text_does_not_decrypt(self):
        sid = self.service.create()["id"]
        self.service.append_user_turn(sid, "kirti-secret-text")
        info = self.service.info(sid)
        self.assertEqual(info["history"][0]["text"], "")
        info_text = self.service.info(sid, include_text=True)
        self.assertEqual(info_text["history"][0]["text"], "kirti-secret-text")

    def test_unknown_session_errors(self):
        with self.assertRaises(SessionError):
            self.service.append_user_turn("nope", "hi")
        with self.assertRaises(SessionError):
            self.service.info("nope")
        with self.assertRaises(SessionError):
            self.service.close("nope")

    def test_metadata_is_redacted_in_store(self):
        token = "sk-abcdefghijklmnopqrstuvwxyz123456"
        sid = self.service.create(title="t", metadata={"note": f"token {token} plus ordinary text"})["id"]
        with self.store.connect() as con:
            raw = con.execute("SELECT metadata_json FROM sessions WHERE id=?", (sid,)).fetchone()[0]
        self.assertNotIn(token, raw)
        self.assertIn("[REDACTED]", raw)
        self.assertIn("ordinary text", raw)


class EncryptionAtRestTests(unittest.TestCase):
    def setUp(self):
        self.store, self.tmp = make_store()
        self.service = make_service(self.store, self.tmp)

    def test_plaintext_never_in_db_bytes(self):
        secret = "super-secret-kirti-1a2b3c"
        sid = self.service.create()["id"]
        self.service.append_user_turn(sid, secret)
        self.service.append_assistant_turn(sid, 1, text=secret + "-reply", engine="local",
                                           provider=None, model=None, provider_session_id=None,
                                           run_id=None, sensitive=False, untrusted=False, success=True)
        with open(self.store.path, "rb") as fh:
            blob = fh.read()
        self.assertNotIn(secret.encode(), blob)

    def test_envelopes_are_domain_separated(self):
        key = load_or_create_key(os.path.join(self.tmp, "job.key"))
        job = encrypt(key, b"job-plaintext-zz")
        session = encrypt_session_turn(key, b"session-plaintext-zz")
        with self.assertRaises(CryptoError):
            decrypt_session_turn(key, job)
        with self.assertRaises(CryptoError):
            decrypt(key, session)

    def test_envelope_version_and_key_id_recorded(self):
        sid = self.service.create()["id"]
        self.service.append_user_turn(sid, "hello")
        with self.store.connect() as con:
            row = con.execute(
                "SELECT content_envelope,envelope_version,key_id FROM session_turns WHERE session_id=?",
                (sid,),
            ).fetchone()
        self.assertEqual(row[1], SESSION_ENVELOPE_VERSION)
        self.assertEqual(row[2], "job.key")
        self.assertNotEqual(row[0], b"hello")

    def test_corrupt_envelope_raises_honest_error(self):
        sid = self.service.create()["id"]
        self.service.append_user_turn(sid, "hello")
        with self.store.connect() as con:
            con.execute("UPDATE session_turns SET content_envelope=? WHERE session_id=?",
                        (b"garbage-not-an-envelope", sid))
        with self.assertRaises(SessionError):
            self.service.info(sid, include_text=True)


class LegacyRunCompatibilityTests(unittest.TestCase):
    def test_backfill_provider_session_id_from_legacy_session_id(self):
        tmp = tempfile.mkdtemp(prefix="harness2-legacy-")
        path = os.path.join(tmp, "legacy.sqlite")
        with sqlite3.connect(path) as con:
            con.executescript(SCHEMA)
        Migrator(path, migrations=MIGRATIONS[:4]).migrate()
        with sqlite3.connect(path) as con:
            con.execute(
                "INSERT INTO runs(id,started_at,finished_at,task_hash,engine,agent,model,provider,"
                "status,exit_code,duration_ms,session_id,error_code,detail) "
                "VALUES('legacy-1',1,2,'h','opencode',NULL,'m','p','completed',0,1.0,"
                "'provider-legacy-id',NULL,'{}')"
            )
        Migrator(path).migrate()
        with sqlite3.connect(path) as con:
            row = con.execute(
                "SELECT session_id,provider_session_id,harness_session_id FROM runs WHERE id='legacy-1'"
            ).fetchone()
        self.assertEqual(row[0], "provider-legacy-id")
        self.assertEqual(row[1], "provider-legacy-id")
        self.assertIsNone(row[2])

    def test_new_record_run_writes_both_session_columns(self):
        store, tmp = make_store()
        service = make_service(store, tmp)
        request = RunRequest(prompt="hi", engine="auto", harness_session_id="hs-77")
        decision = make_decision("local")
        result = EngineResult("local", True, text="ok", session_id="prov-new-9", metadata={})
        run_id = store.record_run(request, decision, result, started=time_now())
        with store.connect() as con:
            row = con.execute(
                "SELECT session_id,provider_session_id,harness_session_id FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
        self.assertEqual(row[0], "prov-new-9")
        self.assertEqual(row[1], "prov-new-9")
        self.assertEqual(row[2], "hs-77")

    def test_schema_migration_v5_present(self):
        store, _ = make_store()
        with store.connect() as con:
            tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            cols = {row[1] for row in con.execute("PRAGMA table_info(runs)")}
        self.assertIn("sessions", tables)
        self.assertIn("session_turns", tables)
        self.assertIn("session_attachments", tables)
        self.assertIn("harness_session_id", cols)
        self.assertIn("provider_session_id", cols)
        self.assertTrue(any(m.version == 5 and m.name == "harness_sessions" for m in MIGRATIONS))

    def test_turn_role_check_constraint(self):
        store, tmp = make_store()
        service = make_service(store, tmp)
        sid = service.create()["id"]
        with self.assertRaises(sqlite3.IntegrityError):
            with store.connect() as con:
                con.execute(
                    "INSERT INTO session_turns(session_id,role,content_envelope,envelope_version,"
                    "key_id,status,created_at) VALUES(?,?,?,?,?,?,?)",
                    (sid, "bogus", b"x", 1, "job.key", "pending", 1.0),
                )


def time_now():
    import time
    return time.time()


class FinalRouteProvenanceTests(unittest.TestCase):
    def test_actual_final_route_recorded_after_fallback(self):
        store, tmp = make_store()
        failing = FakeEngine(
            "direct", available=True, healthy=True, enabled=True,
            capabilities=("reason.general",), detail="",
            result=EngineResult("direct", False, error="boom", error_code="APIError", exit_code=1),
        )
        succeeding = FakeEngine(
            "opencode", available=True, healthy=True, enabled=True,
            capabilities=("reason.general", "coding"), detail="",
        )
        orchestrator = Orchestrator({"direct": failing, "opencode": succeeding}, store)
        service = make_service(store, tmp)
        sid = service.create()["id"]
        outcome = run_session_turn(service, orchestrator, sid, "short q", engine="auto")
        assistant = outcome["assistant_turn"]
        self.assertTrue(outcome["success"])
        self.assertEqual(assistant["engine"], "opencode")
        self.assertIsNone(assistant["provider"])
        self.assertEqual(failing.calls, 1)
        self.assertEqual(succeeding.calls, 1)
        with store.connect() as con:
            row = con.execute(
                "SELECT engine,provider,model,run_id FROM session_turns "
                "WHERE session_id=? AND role='assistant'", (sid,),
            ).fetchone()
        self.assertEqual(row[0], "opencode")

    def test_direct_engine_records_resolved_model(self):
        store, tmp = make_store()
        engine = FakeEngine(
            "direct", available=True, healthy=True, enabled=True,
            capabilities=("reason.general",), detail="",
            result=EngineResult("direct", True, text="ok", metadata={"resolved_model": "qwen3-4b"}),
        )
        orchestrator = Orchestrator({"direct": engine}, store)
        service = make_service(store, tmp)
        sid = service.create()["id"]
        outcome = run_session_turn(service, orchestrator, sid, "hi there", engine="auto")
        self.assertEqual(outcome["assistant_turn"]["engine"], "direct")
        self.assertEqual(outcome["assistant_turn"]["model"], "qwen3-4b")


class CrashRecoveryTests(unittest.TestCase):
    def test_crash_between_user_turn_and_result(self):
        store, tmp = make_store()
        service = make_service(store, tmp)
        sid = service.create()["id"]
        foreground = FakeForeground(RuntimeError("simulated crash"))
        with self.assertRaises(RuntimeError):
            run_session_turn(service, foreground, sid, "hello kirti")
        info = service.info(sid, include_text=True)
        self.assertEqual(len(info["history"]), 1)
        self.assertEqual(info["history"][0]["role"], "user")
        self.assertEqual(info["history"][0]["status"], "interrupted")
        self.assertFalse(any(t["role"] == "assistant" for t in info["history"]))

    def test_interrupted_survives_restart(self):
        store, tmp = make_store()
        service = make_service(store, tmp)
        sid = service.create()["id"]
        with self.assertRaises(RuntimeError):
            run_session_turn(service, FakeForeground(RuntimeError("crash")), sid, "persist me")
        service2 = SessionService(store, key_path=os.path.join(tmp, "job.key"))
        info = service2.info(sid, include_text=True)
        self.assertEqual(info["history"][0]["status"], "interrupted")
        self.assertEqual(info["history"][0]["text"], "persist me")

    def test_idempotent_retry_reuses_trailing_turn(self):
        store, tmp = make_store()
        service = make_service(store, tmp)
        sid = service.create()["id"]
        with self.assertRaises(RuntimeError):
            run_session_turn(service, FakeForeground(RuntimeError("crash")), sid, "retry me")
        foreground = FakeForeground(EngineResult("local", True, text="recovered", metadata={}))
        outcome = run_session_turn(service, foreground, sid, "retry me")
        self.assertTrue(outcome["success"])
        info = service.info(sid, include_text=True)
        user_turns = [t for t in info["history"] if t["role"] == "user"]
        self.assertEqual(len(user_turns), 1)
        self.assertEqual(user_turns[0]["status"], "completed")
        self.assertEqual(len(foreground.calls), 1)
        self.assertEqual(len(info["history"]), 2)

    def test_new_prompt_after_crash_appends_new_turn(self):
        store, tmp = make_store()
        service = make_service(store, tmp)
        sid = service.create()["id"]
        with self.assertRaises(RuntimeError):
            run_session_turn(service, FakeForeground(RuntimeError("crash")), sid, "first")
        foreground = FakeForeground(EngineResult("local", True, text="ok", metadata={}))
        run_session_turn(service, foreground, sid, "second different")
        info = service.info(sid, include_text=True)
        user_turns = [t for t in info["history"] if t["role"] == "user"]
        self.assertEqual(len(user_turns), 2)
        self.assertEqual(user_turns[0]["status"], "interrupted")
        self.assertEqual(user_turns[1]["status"], "completed")


class NoDowngradeTests(unittest.TestCase):
    def test_sensitive_never_downgraded_by_later_history(self):
        store, tmp = make_store()
        service = make_service(store, tmp)
        sid = service.create()["id"]
        service.append_user_turn(sid, "secret plan", sensitive=True)
        service.append_assistant_turn(sid, 1, text="ok", engine="local", provider=None,
                                      model=None, provider_session_id=None, run_id=None,
                                      sensitive=True, untrusted=False, success=True)
        service.append_user_turn(sid, "regular chat", sensitive=False)
        context = service.build_context(sid, current_sensitive=False)
        self.assertTrue(context.effective_sensitive)
        context_flagged = service.build_context(sid, current_sensitive=True)
        self.assertTrue(context_flagged.effective_sensitive)

    def test_untrusted_never_downgraded_by_later_history(self):
        store, tmp = make_store()
        service = make_service(store, tmp)
        sid = service.create()["id"]
        service.append_user_turn(sid, "paste from random site", untrusted=True)
        service.append_assistant_turn(sid, 1, text="ok", engine="local", provider=None,
                                      model=None, provider_session_id=None, run_id=None,
                                      sensitive=False, untrusted=True, success=True)
        context = service.build_context(sid, current_untrusted=False)
        self.assertTrue(context.effective_untrusted)

    def test_effective_flags_propagate_into_routed_run(self):
        store, tmp = make_store()
        service = make_service(store, tmp)
        sid = service.create()["id"]
        service.append_user_turn(sid, "sensitive background", sensitive=True)
        service.append_assistant_turn(sid, 1, text="ok", engine="local", provider=None,
                                      model=None, provider_session_id=None, run_id=None,
                                      sensitive=True, untrusted=False, success=True)
        foreground = FakeForeground(EngineResult("local", True, text="ok", metadata={}))
        run_session_turn(service, foreground, sid, "hi", sensitive=False)
        routed = foreground.calls[-1]
        self.assertTrue(routed.sensitive)
        self.assertFalse(routed.untrusted)
        self.assertEqual(routed.harness_session_id, sid)


class NoProviderSurvivalTests(unittest.TestCase):
    def test_sessions_survive_restart_without_any_providers(self):
        store, tmp = make_store()
        service = make_service(store, tmp)
        sid = service.create(title="durable")["id"]
        service.append_user_turn(sid, "one")
        service.append_assistant_turn(sid, 1, text="one-ok", engine="local", provider=None,
                                      model=None, provider_session_id=None, run_id=None,
                                      sensitive=False, untrusted=False, success=True)
        service.append_user_turn(sid, "two", sensitive=True)
        service.append_assistant_turn(sid, 3, text="two-ok", engine="local", provider=None,
                                      model=None, provider_session_id=None, run_id=None,
                                      sensitive=True, untrusted=False, success=True)
        service2 = SessionService(store, key_path=os.path.join(tmp, "job.key"))
        listing = service2.list()
        self.assertTrue(any(s["id"] == sid and s["turns"] == 4 for s in listing))
        info = service2.info(sid, include_text=True)
        self.assertEqual(len(info["history"]), 4)
        self.assertEqual(info["history"][-1]["text"], "two-ok")
        self.assertTrue(info["history"][-1]["sensitive"])
        context = service2.build_context(sid)
        self.assertIn("one-ok", context.text)
        self.assertTrue(context.effective_sensitive)


class ContextBuilderTests(unittest.TestCase):
    def build_turns(self, roles):
        from harness2.sessions import TurnRecord
        return [
            TurnRecord(seq=i, session_id="s", role=role, status="completed",
                       text=f"content-{i}", created_at=float(i))
            for i, role in enumerate(roles, start=1)
        ]

    def test_roles_limits_and_truncation(self):
        turns = self.build_turns(["user", "assistant"] * 30)
        builder = SessionContextBuilder(turn_limit=5, byte_limit=16384)
        context = builder.build(turns)
        self.assertEqual(tuple(context.included_seq), (56, 57, 58, 59, 60))
        self.assertTrue(context.truncated)
        self.assertIn("content-60", context.text)
        self.assertNotIn("content-1", context.text)

    def test_recent_reference_survives_long_conversation(self):
        turns = self.build_turns(["user", "assistant"] * 25)
        builder = SessionContextBuilder(turn_limit=6, byte_limit=16384)
        context = builder.build(turns)
        self.assertEqual(tuple(context.included_seq), (45, 46, 47, 48, 49, 50))
        self.assertIn("content-50", context.text)
        self.assertIn("content-45", context.text)
        self.assertNotIn("content-1", context.text)
        self.assertNotIn("content-44", context.text)

    def test_oversized_turn_never_bypasses_byte_limit(self):
        from harness2.sessions import TurnRecord
        turns = [
            TurnRecord(seq=1, session_id="s", role="user", status="completed",
                       text="z" * 4000, created_at=1.0),
            TurnRecord(seq=2, session_id="s", role="user", status="completed",
                       text="ok", created_at=2.0),
        ]
        builder = SessionContextBuilder(turn_limit=10, byte_limit=512)
        context = builder.build(turns)
        self.assertEqual(tuple(context.included_seq), (2,))
        self.assertTrue(context.truncated)
        self.assertNotIn("zzzz", context.text)
        self.assert_payload_within_budget(context, 512)

    def test_boundary_turn_exceeding_remaining_budget_skipped(self):
        from harness2.sessions import TurnRecord
        turns = [
            TurnRecord(seq=1, session_id="s", role="user", status="completed",
                       text="a" * 190, created_at=1.0),
            TurnRecord(seq=2, session_id="s", role="user", status="completed",
                       text="b" * 390, created_at=2.0),
            TurnRecord(seq=3, session_id="s", role="user", status="completed",
                       text="c" * 190, created_at=3.0),
        ]
        builder = SessionContextBuilder(turn_limit=10, byte_limit=512)
        context = builder.build(turns)
        self.assertEqual(tuple(context.included_seq), (1, 3))
        self.assertTrue(context.truncated)
        self.assert_payload_within_budget(context, 512)

    def test_byte_budget_counts_utf8_bytes_not_characters(self):
        from harness2.sessions import TurnRecord
        hindi = "किर्ति" * 300
        turns = [
            TurnRecord(seq=1, session_id="s", role="user", status="completed",
                       text=hindi, created_at=1.0),
            TurnRecord(seq=2, session_id="s", role="user", status="completed",
                       text="short", created_at=2.0),
        ]
        builder = SessionContextBuilder(turn_limit=10, byte_limit=256)
        context = builder.build(turns)
        self.assertEqual(tuple(context.included_seq), (2,))
        self.assertTrue(context.truncated)
        self.assert_payload_within_budget(context, 256)

    def assert_payload_within_budget(self, context, budget):
        body = context.text.split("</harness-session-context>")[0]
        body = body.split("<harness-session-context>\n", 1)[1]
        payload = "\n".join(line for line in body.splitlines() if line.strip())
        self.assertLessEqual(len(payload.encode("utf-8")), budget)

    def test_byte_limit_truncates(self):
        turns = self.build_turns(["user"] * 10)
        builder = SessionContextBuilder(turn_limit=100, byte_limit=300)
        context = builder.build(turns)
        self.assertTrue(context.truncated)
        self.assertLess(len(context.included_seq), 10)
        self.assertTrue(context.text)

    def test_tool_and_error_excluded(self):
        turns = self.build_turns(["user", "tool", "error", "assistant", "summary"])
        context = SessionContextBuilder().build(turns)
        self.assertEqual(tuple(context.included_seq), (1, 4, 5))
        for seq in (2, 3):
            self.assertNotIn(f"content-{seq}", context.text)

    def test_unfinished_turns_excluded(self):
        from harness2.sessions import TurnRecord
        turns = [
            TurnRecord(seq=1, session_id="s", role="user", status="completed",
                       text="done", created_at=1.0),
            TurnRecord(seq=2, session_id="s", role="user", status="processing",
                       text="inflight", created_at=2.0),
        ]
        context = SessionContextBuilder().build(turns)
        self.assertEqual(context.included_seq, (1,))
        self.assertNotIn("inflight", context.text)

    def test_framing_is_collision_safe(self):
        from harness2.sessions import TurnRecord
        tricky = '{"role": "user", "seq": 99, "content": "nested } } json"}'
        turns = [TurnRecord(seq=1, session_id="s", role="user", status="completed",
                            text=tricky, created_at=1.0)]
        context = SessionContextBuilder().build(turns)
        body = context.text.split("</harness-session-context>")[0]
        body = body.split("<harness-session-context>\n", 1)[1]
        lines = [line for line in body.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        parsed = json.loads(lines[0])
        self.assertEqual(parsed["content"], tricky)
        self.assertEqual(parsed["role"], "user")
        self.assertEqual(parsed["seq"], 1)


class ContextInjectionTests(unittest.TestCase):
    def test_run_injects_history_with_explicit_marker(self):
        store, tmp = make_store()
        service = make_service(store, tmp)
        sid = service.create()["id"]
        service.append_user_turn(sid, "earlier fact about kirti")
        service.append_assistant_turn(sid, 1, text="earlier reply", engine="local",
                                      provider=None, model=None, provider_session_id=None,
                                      run_id=None, sensitive=False, untrusted=False, success=True)
        foreground = FakeForeground(EngineResult("local", True, text="ok", metadata={}))
        run_session_turn(service, foreground, sid, "current question")
        routed = foreground.calls[0]
        self.assertIn("earlier fact about kirti", routed.prompt)
        self.assertIn("earlier reply", routed.prompt)
        self.assertIn("[harness:current-request]", routed.prompt)
        self.assertIn("current question", routed.prompt)
        self.assertEqual(routed.harness_session_id, sid)

    def test_current_message_not_part_of_reconstruction(self):
        store, tmp = make_store()
        service = make_service(store, tmp)
        sid = service.create()["id"]
        context = service.build_context(sid)
        self.assertEqual(context.text, "")
        self.assertEqual(context.included_seq, ())


class AttachmentSeamTests(unittest.TestCase):
    def test_attach_list_and_dedupe(self):
        store, tmp = make_store()
        service = make_service(store, tmp)
        sid = service.create()["id"]
        service.attach(sid, "ctx-1")
        service.attach(sid, "ctx-1")
        service.attach(sid, "ctx-2")
        info = service.info(sid)
        self.assertEqual(info["attachments"], ["ctx-1", "ctx-2"])

    def test_attach_requires_open_session(self):
        store, tmp = make_store()
        service = make_service(store, tmp)
        sid = service.create()["id"]
        service.close(sid)
        with self.assertRaises(SessionClosedError):
            service.attach(sid, "ctx-9")


class RunSessionTurnTests(unittest.TestCase):
    def test_failure_records_failed_assistant_turn(self):
        store, tmp = make_store()
        service = make_service(store, tmp)
        sid = service.create()["id"]
        foreground = FakeForeground(EngineResult("local", False, text="", error="boom",
                                                 error_code="APIError", metadata={}))
        outcome = run_session_turn(service, foreground, sid, "will fail")
        self.assertFalse(outcome["success"])
        info = service.info(sid, include_text=True)
        self.assertEqual(info["history"][1]["status"], "failed")
        self.assertEqual(info["history"][1]["error_code"], "APIError")

    def test_requires_open_session(self):
        store, tmp = make_store()
        service = make_service(store, tmp)
        sid = service.create()["id"]
        service.close(sid)
        with self.assertRaises(SessionClosedError):
            run_session_turn(service, FakeForeground(EngineResult("local", True, text="ok", metadata={})),
                             sid, "hi")


if __name__ == "__main__":
    unittest.main()