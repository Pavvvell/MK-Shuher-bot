"""Telegram photo fetching and OGG/OPUS voice transcription.

Telegram file IDs are resolved server-side. No user-supplied URL is fetched.
Telegram voice notes are converted into supported MP3 before transcription.
"""
import asyncio
import re

import httpx

MAX_PHOTO_BYTES = 6_000_000
MAX_VOICE_BYTES = 9_000_000
MAX_VOICE_SECONDS = 120
TRANSCRIBE_MODEL = "gpt-4o-mini-transcribe"


async def telegram_file_bytes(bot_call, token, file_id, limit):
    if not isinstance(file_id, str) or not file_id or len(file_id) > 320:
        raise ValueError("Некоректний Telegram файл.")
    reply = await bot_call("getFile", {"file_id": file_id})
    entry = reply.get("result") or {}
    if int(entry.get("file_size") or 0) > limit:
        raise ValueError("Файл завеликий. Надішли менше фото або коротше голосове.")
    path = entry.get("file_path")
    if (not isinstance(path, str) or
            not re.fullmatch(r"[A-Za-z0-9_./-]+", path) or
            path.startswith("/") or ".." in path.split("/")):
        raise ValueError("Telegram не повернув коректний шлях файлу.")
    url = "https://api.telegram.org/file/bot" + token + "/" + path
    chunks = []
    length = 0
    try:
        async with httpx.AsyncClient(timeout=40, follow_redirects=False) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    length += len(chunk)
                    if length > limit:
                        raise ValueError("Файл завеликий для обробки.")
                    chunks.append(chunk)
    except httpx.HTTPError:
        # Do not leak Telegram bot token embedded in the download URL.
        raise ValueError("Не вдалося завантажити файл із Telegram.")
    if not chunks:
        raise ValueError("Отримав порожній файл.")
    return b"".join(chunks)


async def ogg_to_mp3(data):
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", "pipe:0", "-vn", "-f", "mp3", "-codec:a", "libmp3lame",
            "-q:a", "5", "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        raise ValueError("На сервері немає аудіоконвертера. Повідом власнику.")
    try:
        output, stderr = await asyncio.wait_for(process.communicate(data), timeout=30)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        raise ValueError("Голосове надто довге для обробки.")
    if process.returncode != 0 or not output:
        raise ValueError("Не зміг розшифрувати формат голосового. Спробуй ще раз.")
    return output


async def transcribe_telegram_voice(msg, bot_call, token, openai):
    voice = msg.get("voice")
    if not isinstance(voice, dict) or not voice.get("file_id"):
        raise ValueError("Не знайшов голосового повідомлення.")
    duration = int(voice.get("duration") or 0)
    if duration > MAX_VOICE_SECONDS:
        raise ValueError("Голосове має бути не довше 2 хвилин.")
    if int(voice.get("file_size") or 0) > MAX_VOICE_BYTES:
        raise ValueError("Голосове завелике, запиши коротше.")
    encoded = await telegram_file_bytes(
        bot_call, token, voice["file_id"], MAX_VOICE_BYTES
    )
    converted = await ogg_to_mp3(encoded)
    answer = await openai.audio.transcriptions.create(
        model=TRANSCRIBE_MODEL,
        file=("telegram_voice.mp3", converted, "audio/mpeg"),
    )
    text = str(getattr(answer, "text", "") or "").strip()
    if not text:
        raise ValueError("Не розчув голосове. Спробуй записати ще раз.")
    return text[:3500]
