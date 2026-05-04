from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.message_log import MessageLog
from app.services import ai_service
from app.services.chat_service import handle_message


class _ScalarResult:
    def __init__(self, logs: list[MessageLog]):
        self._logs = logs

    def scalars(self) -> "_ScalarResult":
        return self

    def all(self) -> list[MessageLog]:
        return self._logs


def _make_log(
    user_message: str,
    reply: str,
    ai_response: dict | None = None,
) -> MessageLog:
    return MessageLog(
        user_id=1,
        user_message=user_message,
        ai_response=ai_response,
        reply=reply,
    )


@pytest.mark.asyncio
async def test_handle_message_passes_pending_clarification_context(monkeypatch):
    db = AsyncMock()
    db.execute.return_value = _ScalarResult(
        [
            _make_log(
                user_message="500 rupaye ka sale hua",
                reply="Kis customer ka naam likhu?",
                ai_response={
                    "transactions": [],
                    "confidence": "low",
                    "clarification_needed": "Kis customer ka naam likhu?",
                },
            )
        ]
    )
    db.add = MagicMock()

    parse_mock = AsyncMock(
        return_value={
            "transactions": [],
            "confidence": "low",
            "clarification_needed": "Amount bhi bata dijiye.",
        }
    )
    monkeypatch.setattr(ai_service, "parse_message", parse_mock)

    response = await handle_message(db, 1, "Anand")

    assert response.reply == "Amount bhi bata dijiye."
    parse_mock.assert_awaited_once_with(
        "Anand",
        history=[
            {"role": "user", "content": "500 rupaye ka sale hua"},
            {"role": "assistant", "content": "Kis customer ka naam likhu?"},
        ],
        pending_clarification={
            "previous_user_message": "500 rupaye ka sale hua",
            "assistant_question": "Kis customer ka naam likhu?",
        },
    )
    db.add.assert_called_once()


@pytest.mark.asyncio
async def test_handle_message_without_history_calls_parser_cleanly(monkeypatch):
    db = AsyncMock()
    db.execute.return_value = _ScalarResult([])
    db.add = MagicMock()

    parse_mock = AsyncMock(
        return_value={
            "transactions": [],
            "confidence": "low",
            "clarification_needed": "Kiska hisaab dekhna hai?",
        }
    )
    monkeypatch.setattr(ai_service, "parse_message", parse_mock)

    response = await handle_message(db, 1, "baaki kitna hai")

    assert response.reply == "Kiska hisaab dekhna hai?"
    parse_mock.assert_awaited_once_with(
        "baaki kitna hai",
        history=[],
        pending_clarification=None,
    )
    db.add.assert_called_once()
