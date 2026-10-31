"""Offline Responses API contract tests for the flagship GPT-6 model."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from llm import response_input, generate, log_usage


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

    def test_convert_text_and_history(self):
        messages=[
            {"role":"system","content":"Відповідай українською"},
            {"role":"user","content":"Привіт"},
            {"role":"assistant","content":"Вітаю"},
            {"role":"user","content":"Поясни"},
        ]
        self.assertEqual(response_input(messages),messages)

    def test_convert_photo_image_to_responses_schema(self):
        content=[{"type":"text","text":"Подивись на фото"},
                 {"type":"image_url","image_url":{
                     "url":"data:image/jpeg;base64,aGVsbG8=",
                     "detail":"auto"}}]
        items=response_input([{"role":"user","content":content}])
        self.assertEqual(items[0]["content"][0],{"type":"input_text","text":"Подивись на фото"})
        self.assertEqual(items[0]["content"][1]["type"],"input_image")
        self.assertEqual(items[0]["content"][1]["image_url"],
                         "data:image/jpeg;base64,aGVsbG8=")

    async def test_sol_uses_responses_without_temperature(self):
        client=self.client(self.response())
        result=await generate(client,"gpt-6.1-sol",[
            {"role":"system","content":"Ти Шухер"},
            {"role":"user","content":"Привіт"},
        ],max_tokens=800)
        self.assertEqual(result,"Вітаю")
        kwargs=client.responses.create.call_args.kwargs
        self.assertEqual(kwargs["model"],"gpt-6.1-sol")
        self.assertEqual(kwargs["reasoning"],{"effort":"medium"})
        self.assertEqual(kwargs["max_output_tokens"],4000)
        self.assertFalse(kwargs["store"])
        self.assertNotIn("temperature",kwargs)
        self.assertNotIn("response_format",kwargs)
        self.assertNotIn("messages",kwargs)

    async def test_json_routing_uses_low_reasoning_and_json_format(self):
        client=self.client(self.response('{"intent":"other"}'))
        result=await generate(client,"gpt-6.1-sol",[
            {"role":"system","content":"Поверни JSON"},
            {"role":"user","content":"Привіт"},
        ],max_tokens=350,json_mode=True)
        self.assertEqual(result,'{"intent":"other"}')
        kwargs=client.responses.create.call_args.kwargs
        self.assertEqual(kwargs["text"],{"format":{"type":"json_object"}})
        self.assertEqual(kwargs["reasoning"],{"effort":"low"})
        self.assertEqual(kwargs["max_output_tokens"],1800)

    async def test_empty_and_incomplete_model_responses_fail_explicitly(self):
        for response in [self.response(text=""),self.response(status="incomplete")]:
            client=self.client(response)
            with self.assertRaisesRegex(ValueError,"response"):
                await generate(client,"gpt-6.1-sol",[
                    {"role":"user","content":"Hello"},
                ])

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
