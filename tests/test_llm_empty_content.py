from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from core.llm import common
from core.llm.common import call_json_completion


class ScriptedClient:
    """Returns one scripted completion per call, in order."""

    def __init__(self, *responses: dict) -> None:
        self.responses = list(responses)
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        del kwargs
        spec = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        message = SimpleNamespace(
            content=spec.get("content"),
            model_extra={"reasoning_content": spec["reasoning"]} if spec.get("reasoning") else {},
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason=spec.get("finish_reason", "stop"))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )


def parse(content: str | None) -> dict | None:
    try:
        return json.loads(common.clean_json_content(content))
    except json.JSONDecodeError:
        return None


def call(client: ScriptedClient, **overrides):
    return call_json_completion(
        client=client,
        model="fake-model",
        operation="probe",
        messages=[{"role": "user", "content": "hi"}],
        parser=parse,
        **overrides,
    )


class EmptyContentRetryTest(unittest.TestCase):
    def test_blank_answer_is_retried_and_the_retry_wins(self) -> None:
        client = ScriptedClient({"content": ""}, {"content": '{"ok": true}'})

        self.assertEqual({"ok": True}, call(client))
        self.assertEqual(2, client.calls)

    def test_whitespace_only_answer_counts_as_blank(self) -> None:
        client = ScriptedClient({"content": "  \n "}, {"content": '{"ok": true}'})

        self.assertEqual({"ok": True}, call(client))
        self.assertEqual(2, client.calls)

    def test_retries_are_bounded(self) -> None:
        client = ScriptedClient({"content": ""})

        self.assertIsNone(call(client))
        self.assertEqual(common.EMPTY_CONTENT_RETRIES + 1, client.calls)

    def test_retry_budget_is_configurable(self) -> None:
        client = ScriptedClient({"content": ""})

        self.assertIsNone(call(client, empty_content_retries=0))
        self.assertEqual(1, client.calls)

    def test_unparseable_answer_is_not_retried(self) -> None:
        client = ScriptedClient({"content": "sorry, no JSON today"})

        self.assertIsNone(call(client))
        self.assertEqual(1, client.calls)

    def test_last_blank_attempt_salvages_json_from_the_thinking_channel(self) -> None:
        client = ScriptedClient(
            {"content": "", "reasoning": '先想一下……那么答案是 {"ok": true} 就这样'},
        )

        self.assertEqual({"ok": True}, call(client, empty_content_retries=0))

    def test_thinking_channel_without_json_stays_a_failure(self) -> None:
        client = ScriptedClient({"content": "", "reasoning": "想了半天也没想出来"})

        self.assertIsNone(call(client, empty_content_retries=0))


class ResponseStatusTest(unittest.TestCase):
    def test_broken_json_at_the_token_cap_is_labelled_truncated(self) -> None:
        self.assertEqual("truncated", common._invalid_response_status('{"a": "b', "length"))
        self.assertEqual("invalid_json", common._invalid_response_status('{"a": "b', "stop"))

    def test_valid_json_the_parser_rejected_is_not_a_json_problem(self) -> None:
        self.assertEqual("invalid_response", common._invalid_response_status('{"a": 1}', "stop"))


class JsonTailTest(unittest.TestCase):
    def test_picks_the_last_complete_object(self) -> None:
        text = 'draft {"n": 1} then better {"n": 2}'
        self.assertEqual('{"n": 2}', common._json_object_tail(text))

    def test_ignores_an_unterminated_object(self) -> None:
        self.assertEqual('{"n": 1}', common._json_object_tail('{"n": 1} and then {"n": '))

    def test_none_when_there_is_no_object(self) -> None:
        self.assertIsNone(common._json_object_tail("no braces here"))
        self.assertIsNone(common._json_object_tail(None))


if __name__ == "__main__":
    unittest.main()
