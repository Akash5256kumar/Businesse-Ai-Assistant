from __future__ import annotations

import httpx
from fastapi import HTTPException, UploadFile

from app.core.config import settings

_ELEVENLABS_STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"


async def transcribe_audio(file: UploadFile) -> str:
    if not settings.elevenlabs_api_key:
        raise HTTPException(status_code=503, detail="ElevenLabs API key not configured")

    audio_bytes = await file.read()

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            _ELEVENLABS_STT_URL,
            headers={"xi-api-key": settings.elevenlabs_api_key},
            files={"file": (file.filename or "audio", audio_bytes, file.content_type or "audio/mpeg")},
            data={"model_id": "scribe_v1"},
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=response.status_code,
            detail=f"ElevenLabs transcription failed: {response.text}",
        )

    return response.json().get("text", "")
