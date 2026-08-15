"""Phase 10.1: session framing never influences task-based routing."""

import unittest

from harness2.policy import _user_task_length


class UserTaskLengthTests(unittest.TestCase):
    def test_envelope_measures_decoded_task_not_boilerplate(self):
        prompt = (
            "[harness:session-semantics]\nThis request belongs to one persistent "
            "Harness conversation.\n\n"
            "[harness:session-history]\n{\"role\":\"user\",\"seq\":1,\"content\":\"x\"}\n\n"
            "[harness:current-request]\n\"short q\""
        )
        self.assertEqual(_user_task_length(prompt), 7)

    def test_without_framing_whole_prompt_is_the_task(self):
        self.assertEqual(_user_task_length("short q"), 7)
        self.assertEqual(_user_task_length("x" * 500), 500)

    def test_garbage_after_marker_does_not_crash(self):
        prompt = "[harness:current-request]\nnot-json-at-all"
        self.assertEqual(_user_task_length(prompt), len("not-json-at-all"))


if __name__ == "__main__":
    unittest.main()
