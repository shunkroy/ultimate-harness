"""Tests for harness2.events -- OpenCode --format json and Prime --mode json.

Run from the repository root with either:

    python3 -m unittest tests.test_events -v
    python3 -m pytest tests/test_events.py -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness2.events import ParseResult, parse, parse_opencode, parse_prime, parse_text


def lines(*rows: str):
    return "\n".join(rows)


class OpenCodeParserTests(unittest.TestCase):
    """OpenCode --format json (schema verified against real CLI output)."""

    # --- success ----------------------------------------------------------

    def test_success_stream_real_payload(self):
        stream = lines(
            '{"type":"step_start","timestamp":1,"sessionID":"ses_abc","part":'
            '{"id":"p1","messageID":"m1","sessionID":"ses_abc","type":"step-start"}}',
            '{"type":"text","timestamp":2,"sessionID":"ses_abc","part":'
            '{"id":"p2","messageID":"m1","sessionID":"ses_abc","type":"text",'
            '"text":"hello-json-test","time":{"start":1,"end":2}}}',
            '{"type":"step_finish","timestamp":3,"sessionID":"ses_abc","part":'
            '{"id":"p3","reason":"stop","messageID":"m1","sessionID":"ses_abc",'
            '"type":"step-finish","tokens":{"total":10,"input":5,"output":5},'
            '"cost":0}}',
        )
        result = parse_opencode(stream.splitlines())
        self.assertEqual(result.source, "opencode")
        self.assertEqual(result.text, "hello-json-test")
        self.assertIsNone(result.error)
        self.assertIsNone(result.error_code)
        self.assertEqual(result.session_id, "ses_abc")
        self.assertEqual(result.raw_event_count, 3)
        self.assertTrue(result.saw_terminal)
        self.assertTrue(result.success)

    def test_multipart_text_concatenated_in_order(self):
        stream = [
            '{"type":"text","sessionID":"s1","part":{"type":"text","text":"first "}}',
            '{"type":"text","sessionID":"s1","part":{"type":"text","text":"second "}}',
            '{"type":"text","sessionID":"s1","part":{"type":"text","text":"third"}}',
            '{"type":"step_finish","sessionID":"s1","part":{"type":"step-finish","reason":"stop"}}',
        ]
        result = parse_opencode(stream)
        self.assertEqual(result.text, "first second third")
        self.assertTrue(result.success)

    def test_unicode_preserved(self):
        text_with_combining = "e\u0301"  # e + combining acute accent (U+0301)
        stream = [
            '{"type":"text","part":{"type":"text","text":"h\u00e9llo w\u00f6rld \U0001f389 \u4f60\u597d \u3053\u3093\u306b\u3061\u306f"}}',
            f'{{"type":"text","part":{{"type":"text","text":"{text_with_combining}"}}}}',
            '{"type":"step_finish","part":{"type":"step-finish","reason":"stop"}}',
        ]
        result = parse_opencode(stream)
        # Byte-for-byte preservation: no normalization, no escaping damage.
        self.assertEqual(result.text, "h\u00e9llo w\u00f6rld \U0001f389 \u4f60\u597d \u3053\u3093\u306b\u3061\u306f" + text_with_combining)
        self.assertIn("\U0001f389", result.text)
        self.assertIn("\u0301", result.text)

    def test_malformed_lines_tolerated(self):
        stream = [
            'not json at all {{{',
            '{"type":"text","part":{"type":"text","text":"ok"}}',
            "",
            '42',
            '"a bare string"',
            '{"type":"text","part":{"type":"text","text":" still ok"}}',
            '{"type":"step_finish","part":{"type":"step-finish","reason":"stop"}}',
            "{truncated",
        ]
        result = parse_opencode(stream)
        self.assertEqual(result.text, "ok still ok")
        self.assertEqual(result.raw_event_count, 8)
        self.assertEqual(result.malformed_count, 2)
        self.assertTrue(result.success)

    def test_strict_boundary_rejects_malformed_or_trailing_records(self):
        malformed = parse_text("opencode", lines(
            "not-json",
            '{"type":"step_finish","part":{"type":"step-finish"}}',
        ), strict=True)
        self.assertFalse(malformed.success)
        self.assertEqual(malformed.error_code, "malformed_stream")

        trailing = parse_text("opencode", lines(
            '{"type":"step_finish","part":{"type":"step-finish"}}',
            '{"type":"text","part":{"type":"text","text":"late"}}',
        ), strict=True)
        self.assertFalse(trailing.success)
        self.assertEqual(trailing.error_code, "trailing_stream")

        invalid_schema = parse_text("opencode", lines(
            "[]",
            '{"type":"step_finish"}',
        ), strict=True)
        self.assertFalse(invalid_schema.success)
        self.assertEqual(invalid_schema.error_code, "malformed_stream")

    def test_partial_output_followed_by_error(self):
        stream = [
            '{"type":"step_start","sessionID":"ses_x","part":{"type":"step-start"}}',
            '{"type":"text","sessionID":"ses_x","part":{"type":"text","text":"partial answer so far"}}',
            '{"type":"error","sessionID":"ses_x","error":{"name":"APIError","data":{"message":"provider timed out","isRetryable":false}}}',
        ]
        result = parse_opencode(stream)
        # Partial text is preserved even though the run failed.
        self.assertEqual(result.text, "partial answer so far")
        self.assertEqual(result.error, "provider timed out")
        self.assertEqual(result.error_code, "APIError")
        self.assertTrue(result.saw_terminal)
        self.assertFalse(result.success)

    def test_error_event_real_payload(self):
        stream = [
            '{"type":"error","timestamp":1786516732335,"sessionID":"ses_00b4e3982ffe1pmzRxAXRuzd0F",'
            '"error":{"name":"UnknownError","data":{"message":"Model not found: nowhere/invalid-model."}}}'
        ]
        result = parse_opencode(stream)
        self.assertEqual(result.error, "Model not found: nowhere/invalid-model.")
        self.assertEqual(result.error_code, "UnknownError")
        self.assertEqual(result.session_id, "ses_00b4e3982ffe1pmzRxAXRuzd0F")
        self.assertTrue(result.saw_terminal)
        self.assertFalse(result.success)

    def test_multiple_error_events_last_wins(self):
        # Real opencode behavior: a generic error is emitted first, the
        # specific root cause last. The harness must surface the root cause.
        stream = [
            '{"type":"error","error":{"name":"UnknownError","data":{"message":"Unexpected server error. Check server logs for details.","ref":"err_918035de"}}}',
            '{"type":"error","error":{"name":"UnknownError","data":{"message":"Model not found: nowhere/invalid-model."}}}',
        ]
        result = parse_opencode(stream)
        self.assertEqual(result.error, "Model not found: nowhere/invalid-model.")
        self.assertEqual(result.error_code, "UnknownError")
        self.assertEqual(result.raw_event_count, 2)
        self.assertTrue(result.saw_terminal)
        self.assertFalse(result.success)

    def test_error_with_string_and_flat_shapes(self):
        result = parse_opencode(['{"type":"error","error":"boom"}'])
        self.assertEqual(result.error, "boom")
        self.assertEqual(result.error_code, "error")
        self.assertFalse(result.success)

        result = parse_opencode(['{"type":"error","error":{"message":"flat","code":"E42"}}'])
        self.assertEqual(result.error, "flat")
        self.assertEqual(result.error_code, "E42")
        self.assertFalse(result.success)

    def test_empty_terminal(self):
        # step_start + step_finish with no text events: clean empty run.
        stream = [
            '{"type":"step_start","sessionID":"s","part":{"type":"step-start"}}',
            '{"type":"step_finish","sessionID":"s","part":{"type":"step-finish","reason":"stop"}}',
        ]
        result = parse_opencode(stream)
        self.assertEqual(result.text, "")
        self.assertIsNone(result.error)
        self.assertTrue(result.saw_terminal)
        self.assertTrue(result.success)

    def test_truncated_stream_is_not_success(self):
        # Text arrived but no step_finish/error: stream was cut off.
        stream = ['{"type":"text","part":{"type":"text","text":"half"}}']
        result = parse_opencode(stream)
        self.assertEqual(result.text, "half")
        self.assertFalse(result.saw_terminal)
        self.assertFalse(result.success)

    def test_step_start_and_unknown_events_ignored(self):
        stream = [
            '{"type":"step_start","part":{"type":"step-start"}}',
            '{"type":"message","part":{"type":"text","text":"ignored"}}',
            '{"type":"session.idle","sessionID":"s"}',
            '{"type":"step_finish","part":{"type":"step-finish","reason":"stop"}}',
        ]
        result = parse_opencode(stream)
        self.assertEqual(result.text, "")
        self.assertTrue(result.success)

    def test_non_text_parts_ignored(self):
        stream = [
            '{"type":"text","part":{"type":"reasoning","text":"hidden chain of thought"}}',
            '{"type":"text","part":{"type":"text","text":"visible"}}',
            '{"type":"step_finish","part":{"type":"step-finish","reason":"stop"}}',
        ]
        result = parse_opencode(stream)
        self.assertEqual(result.text, "visible")

    def test_session_id_falls_back_to_part(self):
        stream = [
            '{"type":"text","part":{"type":"text","text":"x","sessionID":"ses_from_part"}}',
            '{"type":"step_finish","part":{"type":"step-finish","reason":"stop"}}',
        ]
        result = parse_opencode(stream)
        self.assertEqual(result.session_id, "ses_from_part")

    def test_blank_lines_count_but_do_not_break(self):
        result = parse_opencode(["", "  ", '{"type":"text","part":{"type":"text","text":"a"}}', ""])
        self.assertEqual(result.raw_event_count, 4)
        self.assertEqual(result.malformed_count, 0)
        self.assertEqual(result.text, "a")


class PrimeParserTests(unittest.TestCase):
    """Prime Agent --mode json (schema from PrimeIntellect-ai/prime-agent docs)."""

    # --- success ----------------------------------------------------------

    def test_success_stream(self):
        stream = [
            '{"type":"agent_start"}',
            '{"type":"turn_start"}',
            '{"type":"message_start","message":{"role":"user","content":[{"type":"text","text":"Say hi"}]}}',
            '{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"Hello"},{"type":"text","text":" world"}],"provider":"openai","model":"gpt-4o","usage":{},"stopReason":"stop"}}',
            '{"type":"turn_end","message":{"role":"assistant","content":[{"type":"text","text":"Hello"},{"type":"text","text":" world"}],"provider":"openai","model":"gpt-4o","usage":{},"stopReason":"stop"},"toolResults":[]}',
            '{"type":"agent_end","messages":[{"role":"user","content":[{"type":"text","text":"Say hi"}]},{"role":"assistant","content":[{"type":"text","text":"Hello"},{"type":"text","text":" world"}],"provider":"openai","model":"gpt-4o","usage":{},"stopReason":"stop"}]}',
        ]
        result = parse_prime(stream)
        self.assertEqual(result.source, "prime")
        self.assertEqual(result.text, "Hello world")
        self.assertIsNone(result.error)
        self.assertTrue(result.saw_terminal)
        self.assertTrue(result.success)
        self.assertEqual(result.raw_event_count, 6)

    def test_message_end_and_turn_end_not_duplicated(self):
        # message_end and the following turn_end carry the same assistant
        # message; the text must be counted exactly once.
        stream = [
            '{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"once"}],"stopReason":"stop"}}',
            '{"type":"turn_end","message":{"role":"assistant","content":[{"type":"text","text":"once"}],"stopReason":"stop"},"toolResults":[]}',
            '{"type":"agent_end","messages":[{"role":"assistant","content":[{"type":"text","text":"once"}],"stopReason":"stop"}]}',
        ]
        result = parse_prime(stream)
        self.assertEqual(result.text, "once")
        self.assertTrue(result.success)

    def test_string_content_blocks(self):
        stream = [
            '{"type":"message_end","message":{"role":"assistant","content":["Hello ", "world"],"stopReason":"stop"}}',
            '{"type":"turn_end","message":{"role":"assistant","content":["Hello ", "world"],"stopReason":"stop"},"toolResults":[]}',
            '{"type":"agent_end","messages":[]}',
        ]
        result = parse_prime(stream)
        self.assertEqual(result.text, "Hello world")
        self.assertTrue(result.success)

    def test_mixed_content_blocks_in_order_with_thinking_excluded(self):
        stream = [
            '{"type":"message_end","message":{"role":"assistant","content":['
            '{"type":"text","text":"a"},'
            '{"type":"thinking","text":"hidden reasoning"},'
            '"b",'
            '{"type":"input_text","text":"c"},'
            '{"type":"tool_use","text":"not text"},'
            '{"type":"text","text":"d"}'
            '],"stopReason":"stop"}}',
            '{"type":"agent_end","messages":[]}',
        ]
        result = parse_prime(stream)
        self.assertEqual(result.text, "abcd")

    def test_content_as_plain_string(self):
        stream = [
            '{"type":"message_end","message":{"role":"assistant","content":"plain string body","stopReason":"stop"}}',
            '{"type":"agent_end","messages":[]}',
        ]
        result = parse_prime(stream)
        self.assertEqual(result.text, "plain string body")
        self.assertTrue(result.success)

    def test_user_and_tool_messages_do_not_leak_into_text(self):
        stream = [
            '{"type":"message_end","message":{"role":"user","content":[{"type":"text","text":"the prompt"}]}}',
            '{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"the answer"}],"stopReason":"stop"}}',
            '{"type":"message_end","message":{"role":"toolResult","toolCallId":"call_1","toolName":"bash","content":[{"type":"text","text":"tool stdout"}],"isError":false}}',
            '{"type":"agent_end","messages":[]}',
        ]
        result = parse_prime(stream)
        self.assertEqual(result.text, "the answer")
        self.assertTrue(result.success)

    def test_unicode_preserved(self):
        stream = [
            '{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"\u2603 \ud83d\ude80 \u0928\u092e\u0938\u094d\u0924\u0947 \u00e9"}],"stopReason":"stop"}}',
            '{"type":"agent_end","messages":[]}',
        ]
        result = parse_prime(stream)
        self.assertEqual(result.text, "\u2603 \ud83d\ude80 \u0928\u092e\u0938\u094d\u0924\u0947 \u00e9")

    def test_malformed_lines_tolerated(self):
        stream = [
            'this is not json',
            '{"type":"message_end","message":{"role":"assistant","content":["partial"],"stopReason":"stop"}}',
            '{"type":"agent_end","messages":[}',
            '[]',
            '{"type":"turn_start"}',
            '{"type":"agent_end","messages":[]}',
        ]
        result = parse_prime(stream)
        self.assertEqual(result.text, "partial")
        self.assertEqual(result.raw_event_count, 6)
        # 'this is not json' and '{"type":"agent_end","messages":[}' are both
        # malformed; '[]' is valid JSON but not an object and is tolerated.
        self.assertEqual(result.malformed_count, 2)
        self.assertTrue(result.success)

    def test_strict_boundary_rejects_prime_trailing_records(self):
        result = parse_text("prime", lines(
            '{"type":"agent_end","messages":[]}',
            '{"type":"turn_start"}',
        ), strict=True)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "trailing_stream")
        invalid = parse_text("prime", '{"type":"agent_end"}', strict=True)
        self.assertFalse(invalid.success)
        self.assertEqual(invalid.error_code, "malformed_stream")

    def test_partial_output_followed_by_error(self):
        stream = [
            '{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"partial answer"}],"stopReason":"error","errorMessage":"Provider stream aborted mid-response"}}',
            '{"type":"turn_end","message":{"role":"assistant","content":[{"type":"text","text":"partial answer"}],"stopReason":"error","errorMessage":"Provider stream aborted mid-response"},"toolResults":[]}',
            '{"type":"agent_end","messages":[{"role":"assistant","content":[{"type":"text","text":"partial answer"}],"stopReason":"error","errorMessage":"Provider stream aborted mid-response"}]}',
        ]
        result = parse_prime(stream)
        self.assertEqual(result.text, "partial answer")
        self.assertEqual(result.error, "Provider stream aborted mid-response")
        self.assertEqual(result.error_code, "error")
        self.assertTrue(result.saw_terminal)
        self.assertFalse(result.success)

    def test_nested_provider_error(self):
        # errorMessage is a nested object; stopReason marks the turn as failed.
        stream = [
            '{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":""}],"stopReason":"error","errorMessage":{"message":"Provider returned HTTP 400: invalid api key"}}}',
            '{"type":"agent_end","messages":[]}',
        ]
        result = parse_prime(stream)
        self.assertIsNotNone(result.error)
        self.assertEqual(result.error, "Provider returned HTTP 400: invalid api key")
        self.assertEqual(result.error_code, "error")
        self.assertFalse(result.success)

    def test_nested_error_object_with_code(self):
        stream = [
            '{"type":"message_end","message":{"role":"assistant","content":[],"stopReason":"stop","error":{"message":"rate limited","code":"RATE_LIMITED"}}}',
            '{"type":"agent_end","messages":[]}',
        ]
        result = parse_prime(stream)
        self.assertEqual(result.error, "rate limited")
        self.assertEqual(result.error_code, "RATE_LIMITED")
        self.assertFalse(result.success)

    def test_stop_reason_aborted_is_error(self):
        stream = [
            '{"type":"message_end","message":{"role":"assistant","content":[],"stopReason":"aborted"}}',
            '{"type":"agent_end","messages":[]}',
        ]
        result = parse_prime(stream)
        self.assertIsNotNone(result.error)
        self.assertEqual(result.error_code, "aborted")
        self.assertFalse(result.success)

    def test_stop_reason_length_is_completion_not_error(self):
        # "length" means the model hit its token cap: complete, not an error.
        stream = [
            '{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"long answer"}],"stopReason":"length"}}',
            '{"type":"agent_end","messages":[]}',
        ]
        result = parse_prime(stream)
        self.assertIsNone(result.error)
        self.assertEqual(result.text, "long answer")
        self.assertTrue(result.success)

    def test_later_error_overrides_earlier(self):
        # A retried/continued turn can carry a later, more definitive error.
        stream = [
            '{"type":"message_end","message":{"role":"assistant","content":[],"stopReason":"error","errorMessage":"temporary, retrying"}}',
            '{"type":"message_end","message":{"role":"assistant","content":[],"stopReason":"error","errorMessage":"final provider failure"}}',
            '{"type":"agent_end","messages":[]}',
        ]
        result = parse_prime(stream)
        self.assertEqual(result.error, "final provider failure")
        self.assertFalse(result.success)

    def test_agent_end_empty_terminal(self):
        result = parse_prime(['{"type":"agent_end","messages":[]}'])
        self.assertEqual(result.text, "")
        self.assertIsNone(result.error)
        self.assertTrue(result.saw_terminal)
        self.assertTrue(result.success)

    def test_agent_end_error_scan_on_messages(self):
        # No message_end seen; the error lives in the agent_end messages array.
        stream = [
            '{"type":"agent_start"}',
            '{"type":"agent_end","messages":['
            '{"role":"user","content":[{"type":"text","text":"q"}]},'
            '{"role":"assistant","content":[{"type":"text","text":"tried"}],"stopReason":"error","errorMessage":"upstream provider outage"}'
            ']}',
        ]
        result = parse_prime(stream)
        self.assertEqual(result.text, "tried")
        self.assertEqual(result.error, "upstream provider outage")
        self.assertEqual(result.error_code, "error")
        self.assertFalse(result.success)

    def test_truncated_stream_is_not_success(self):
        # message_end without agent_end: run never finished.
        stream = ['{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"half"}],"stopReason":"stop"}}']
        result = parse_prime(stream)
        self.assertEqual(result.text, "half")
        self.assertFalse(result.saw_terminal)
        self.assertFalse(result.success)

    def test_unknown_event_types_ignored(self):
        stream = [
            '{"type":"agent_start"}',
            '{"type":"turn_start"}',
            '{"type":"message_update","message":{"role":"assistant","content":[]},"assistantMessageEvent":{"type":"text_delta","contentIndex":0,"delta":"live"}}',
            '{"type":"tool_execution_start","toolCallId":"c1","toolName":"bash","args":{}}',
            '{"type":"tool_execution_end","toolCallId":"c1","toolName":"bash","result":{},"isError":false}',
            '{"type":"agent_end","messages":[]}',
        ]
        result = parse_prime(stream)
        # Deltas are not authoritative; message_end owns the final text.
        self.assertEqual(result.text, "")
        self.assertTrue(result.success)

    def test_session_id_sources(self):
        stream = [
            '{"type":"message_end","message":{"role":"assistant","content":[],"stopReason":"stop","sessionId":"ses_from_message"}}',
            '{"type":"agent_end","messages":[]}',
        ]
        result = parse_prime(stream)
        self.assertEqual(result.session_id, "ses_from_message")

        stream = [
            '{"type":"agent_end","sessionId":"ses_top","messages":[]}',
        ]
        result = parse_prime(stream)
        self.assertEqual(result.session_id, "ses_top")


class ParseResultAndDispatcherTests(unittest.TestCase):
    """ParseResult semantics + top-level dispatch helpers."""

    def test_success_property_combinations(self):
        self.assertTrue(ParseResult(saw_terminal=True).success)
        self.assertFalse(ParseResult(saw_terminal=False).success)
        self.assertFalse(ParseResult(error="boom", saw_terminal=True).success)
        self.assertFalse(ParseResult(error="boom", saw_terminal=False).success)
        # Default-constructed result is never a success.
        self.assertFalse(ParseResult().success)
        # Error still present with partial text: not a success.
        self.assertFalse(ParseResult(text="partial", error="boom", saw_terminal=True).success)

    def test_parse_dispatch(self):
        opencode_line = '{"type":"text","part":{"type":"text","text":"x"}}'
        prime_line = '{"type":"agent_end","messages":[]}'
        self.assertEqual(parse("opencode", [opencode_line]).source, "opencode")
        self.assertEqual(parse("prime", [prime_line]).source, "prime")
        with self.assertRaises(ValueError):
            parse("unknown-format", [])

    def test_parse_text_splits_lines(self):
        text = (
            '{"type":"text","part":{"type":"text","text":"a"}}\n'
            '{"type":"text","part":{"type":"text","text":"b"}}\n'
            '{"type":"step_finish","part":{"type":"step-finish","reason":"stop"}}\n'
        )
        result = parse_text("opencode", text)
        self.assertEqual(result.text, "ab")
        self.assertEqual(result.raw_event_count, 3)
        self.assertTrue(result.success)

    def test_parse_text_empty(self):
        self.assertFalse(parse_text("opencode", "").success)
        self.assertFalse(parse_text("prime", None).success)


if __name__ == "__main__":
    unittest.main()
