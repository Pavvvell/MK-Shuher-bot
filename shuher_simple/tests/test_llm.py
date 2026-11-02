"""Offline Responses API contract tests for the flagship GPT-6 model."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from llm import log_usage


class LLMTests(unittest.IsolatedAsyncioTestCase):
    def response(self,text="Вітаю",status="completed"):
        return SimpleNamespace(
            status=status,
            output_text=text,
            model="gpt-6.1-sol",
            usage=SimpleNamespace(
                input_tokens=534,
                output_tokens=85,
                input_tokens_details=SimpleNamespace(cached_tokens=120),
                output_tokens_details=SimpleNamespace(reasoning_tokens=19),
            ),
        )

    def client(self,response):
        client=SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=response)))
        return client


    def test_usage_logging_never_logs_content(self):
        response=self.response()
        with self.assertLogs("shuher-lite.usage",level="INFO") as cm:
            log_usage(response,"gpt-6.1-sol")
        self.assertIn("input_tokens=534",cm.output[0])
        self.assertIn("output_tokens=85",cm.output[0])
        self.assertIn("cached_tokens=120",cm.output[0])
        self.assertIn("reasoning_tokens=19",cm.output[0])


if __name__=="__main__":
    unittest.main()
