"""Phase 10.1 final review: policy task isolation from session framing.

PROVIDER PROMPT = semantics + history + current request
POLICY TASK    = current request only

Session history may inform the reasoning model, but it must never mutate
the current task's capability classification. These tests prove the
framing/history envelope cannot trigger messaging/parallel/durable routing
and that genuine current-task keywords still can.
"""

import json
import unittest

from harness2.models import CapabilityStatus, EngineStatus, RunRequest
from harness2.policy import PolicyRefusal, PolicyRouter, policy_task_text
from harness2.sessions import _SEMANTICS_TEXT, assemble_session_prompt


def status(name, available=True, healthy=True, enabled=True):
    return EngineStatus(
        name, available, healthy, enabled,
        CapabilityStatus.ACTIVE if healthy else CapabilityStatus.IMPLEMENTED,
        "", ("reason.general",), "private-file", "unknown",
    )


def router(**overrides):
    base = {
        "local": status("local"),
        "hermes": status("hermes"),
        "opencode": status("opencode"),
        "prime": status("prime"),
        "direct": status("direct"),
    }
    base.update(overrides)
    return PolicyRouter(base)


def request(prompt, session_id=None, **kwargs):
    return RunRequest(prompt=prompt, harness_session_id=session_id, **kwargs)


def envelope(task, history=()):
    """A realistic Harness session envelope via the canonical assembler."""
    history_payload = "\n".join(
        json.dumps({"role": "user", "seq": index + 1, "content": text}, ensure_ascii=False)
        for index, text in enumerate(history)
    )
    return assemble_session_prompt(history_payload, task)


class PolicyTaskTextTests(unittest.TestCase):
    def test_non_session_prompt_is_the_task_exactly(self):
        raw = "What is 2+2? [harness:current-request]"
        self.assertEqual(policy_task_text(request(raw)), raw)

    def test_session_request_decodes_only_current_request(self):
        prompt = envelope("short q", history=["telegram send message"])
        self.assertEqual(policy_task_text(request(prompt, session_id="s")), "short q")

    def test_value_mentioning_marker_cannot_forge_a_header(self):
        task = "[harness:current-request]\nsecond line"
        prompt = envelope(task, history=["old fact"])
        self.assertEqual(policy_task_text(request(prompt, session_id="s")), task)

    def test_malformed_session_envelope_fails_closed(self):
        for prompt in (
            "no marker at all",
            "[harness:current-request]\nnot-json",
            "[harness:current-request]\n123",
            "[harness:current-request]\n",
        ):
            with self.assertRaises(PolicyRefusal, msg=repr(prompt)):
                policy_task_text(request(prompt, session_id="s"))


class SessionTaskIsolationTests(unittest.TestCase):
    def test_semantics_envelope_does_not_trigger_durable(self):
        # Pin the exact regression: the real semantics block contains
        # "persistent", which _DURABLE used to match.
        self.assertIn("persistent", _SEMANTICS_TEXT)
        decision = router().decide(request(envelope("What is 2+2?"), session_id="s"))
        self.assertEqual(decision.engine, "direct")
        self.assertEqual(decision.task_class, "fast")
        self.assertNotEqual(decision.task_class, "durable")

    def test_history_cannot_trigger_messaging(self):
        decision = router().decide(
            request(envelope("What number did I mention?", history=["telegram send message"]),
                    session_id="s")
        )
        self.assertEqual(decision.engine, "direct")
        self.assertEqual(decision.task_class, "fast")

    def test_history_cannot_trigger_parallel_or_durable(self):
        decision = router().decide(
            request(envelope("What is the weather?",
                             history=["parallel agents", "persistent background agent"]),
                    session_id="s")
        )
        self.assertEqual(decision.engine, "direct")
        self.assertEqual(decision.task_class, "fast")

    def test_current_task_can_trigger_capability_routing(self):
        messaging = router().decide(
            request(envelope("send a Telegram message", history=["unrelated"]), session_id="s")
        )
        self.assertEqual(messaging.engine, "hermes")
        self.assertEqual(messaging.task_class, "messaging")

        durable = router().decide(
            request(envelope("run a persistent background agent", history=["unrelated"]),
                    session_id="s")
        )
        self.assertEqual(durable.engine, "prime")
        self.assertEqual(durable.task_class, "durable")

        parallel = router().decide(
            request(envelope("delegate in parallel agents", history=["unrelated"]), session_id="s")
        )
        self.assertEqual(parallel.engine, "hermes")
        self.assertEqual(parallel.task_class, "parallel")

    def test_session_fast_path_remains_intact_with_large_history(self):
        decision = router().decide(
            request(envelope("short q", history=["x" * 500] * 3), session_id="s")
        )
        self.assertEqual(decision.engine, "direct")
        self.assertEqual(decision.task_class, "fast")


class NonSessionMarkerSpoofTests(unittest.TestCase):
    def test_marker_in_plain_prompt_is_inert_literal_text(self):
        raw = "What is 2+2? [harness:current-request]"
        decision = router().decide(request(raw))
        self.assertEqual(decision.engine, "direct")
        self.assertEqual(decision.task_class, "fast")

    def test_marker_does_not_shorten_length_classification(self):
        raw = "[harness:current-request]\n" + "x" * 300
        decision = router().decide(request(raw))
        self.assertNotEqual(decision.task_class, "fast")
        self.assertEqual(decision.engine, "opencode")
        self.assertEqual(decision.task_class, "control")

    def test_literal_keywords_around_marker_still_classify(self):
        decision = router().decide(request("send a telegram message [harness:current-request]"))
        self.assertEqual(decision.engine, "hermes")
        self.assertEqual(decision.task_class, "messaging")


if __name__ == "__main__":
    unittest.main()
