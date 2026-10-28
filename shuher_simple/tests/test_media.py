"""Offline tests for Telegram media safety and voice transcription bridge."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from media import telegram_file_bytes, transcribe_telegram_voice, MAX_VOICE_SECONDS


class TelegramMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_oversized_telegram_file_before_download(self):
        async def bot_call(method, payload):
            self.assertEqual(method,"getFile")
            return {"ok":True,"result":{"file_size":10000000,"file_path":"photos/a.jpg"}}
        with self.assertRaisesRegex(ValueError,"завеликий"):
            await telegram_file_bytes(bot_call,"TOKEN","file_id",500000)

    async def test_rejects_malicious_path(self):
        async def bot_call(method,payload):
            return {"ok":True,"result":{"file_size":100,"file_path":"../../admin"}}
        with self.assertRaisesRegex(ValueError,"шлях"):
            await telegram_file_bytes(bot_call,"TOKEN","file_id",500000)

    async def test_rejects_long_voice_message(self):
        api=AsyncMock()
        msg={"voice":{"file_id":"voice-1","duration":MAX_VOICE_SECONDS+1}}
        with self.assertRaisesRegex(ValueError,"2 хвилин"):
            await transcribe_telegram_voice(msg,AsyncMock(),"TOKEN",api)
        api.audio.transcriptions.create.assert_not_awaited()

    async def test_voice_is_transcribed_to_text(self):
        api=SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(
            create=AsyncMock(return_value=SimpleNamespace(text="Потрібно 10 гайок М12")))))
        with patch("media.telegram_file_bytes",new=AsyncMock(return_value=b"ogg")),\
             patch("media.ogg_to_mp3",new=AsyncMock(return_value=b"mp3")):
            text=await transcribe_telegram_voice(
                {"voice":{"file_id":"voice-1","duration":4}},AsyncMock(),"TOKEN",api)
        self.assertEqual(text,"Потрібно 10 гайок М12")
        self.assertEqual(api.audio.transcriptions.create.await_args.kwargs["model"],
                         "gpt-4o-mini-transcribe")
        self.assertEqual(api.audio.transcriptions.create.await_args.kwargs["file"][0],
                         "telegram_voice.mp3")


if __name__=="__main__":
    unittest.main()
