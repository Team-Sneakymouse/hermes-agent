"""Lossless clarification presentation: full options in content, numeric controls."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import utf16_len
from plugins.platforms.discord.adapter import DiscordAdapter


LONG_CHOICES = [
    "I’ll supply the script/command used for the manual run so you can reproduce its general behavior.",
    "Add an explicit reference-font compatibility mode that reproduces this manual output exactly.",
    "Match the manual sampling, pixels, and glyphs, but keep chronological frame order.",
]


def capture_adapter():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="test-token"))
    channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=123)))
    adapter._client = SimpleNamespace(get_channel=lambda _: channel)
    return adapter, channel


@pytest.mark.asyncio
async def test_long_choices_visible_in_full_with_number_only_buttons():
    adapter, channel = capture_adapter()
    result = await adapter.send_clarify("555", "How should we proceed?", LONG_CHOICES, "lossless", "sk")
    assert result.success
    payload = channel.send.call_args.kwargs
    for index, choice in enumerate(LONG_CHOICES, 1):
        assert f"{index}. {choice}" in payload["content"]
    assert [button.label for button in payload["view"].children[:-1]] == ["1", "2", "3"]
    assert "Other" in payload["view"].children[-1].label
    assert utf16_len(payload["content"]) <= 2000


@pytest.mark.asyncio
@pytest.mark.parametrize("question,choices,error", [
    ("Pick", ["x" * 2100], "2000"),
    ("Pick", ["😀" * 1000], "2000"),
    ("q" * 2100, ["short"], "2000"),
    ("Pick", [str(i) for i in range(25)], "24"),
])
async def test_oversized_choice_prompt_fails_without_sending_partial_options(question, choices, error):
    adapter, channel = capture_adapter()
    result = await adapter.send_clarify("555", question, choices, "oversize", "sk")
    assert not result.success
    assert error in result.error
    channel.send.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("choices", [
    ["Use the same shared prefix " * 4 + "but preserve chronology (Recommended)",
     "Use the same shared prefix " * 4 + "but reproduce manual output exactly"],
    ["字形 😀 e\u0301 " * 12 + "(Recommended)", "字形 😀 e\u0301 " * 12 + "保持帧顺序"],
])
async def test_displayed_options_resolve_to_canonical_text_once(choices):
    from tools import clarify_gateway as cm

    adapter, channel = capture_adapter()
    adapter._allowed_user_ids = {"42"}
    entry = cm.register("mapping", "mapping-sk", "Pick", choices)
    try:
        result = await adapter.send_clarify("555", "Pick", choices, entry.clarify_id, entry.session_key)
        assert result.success
        payload = channel.send.call_args.kwargs
        for i, choice in enumerate(choices, 1):
            assert f"{i}. {choice}" in payload["content"]
        view = payload["view"]
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=42, display_name="Tester", roles=[]),
            response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock(), defer=AsyncMock()),
            message=SimpleNamespace(embeds=[]),
        )
        await view.children[1].callback(interaction)
        assert entry.event.is_set()
        assert entry.response == choices[1]
        assert all(button.disabled for button in view.children)
        await view.children[0].callback(interaction)
        assert entry.response == choices[1]
        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.call_args.kwargs["ephemeral"] is True
    finally:
        cm.wait_for_response(entry.clarify_id, timeout=0.01)


@pytest.mark.asyncio
async def test_other_numeric_prompt_captures_typed_answer():
    from tools import clarify_gateway as cm

    adapter, channel = capture_adapter()
    adapter._allowed_user_ids = {"42"}
    entry = cm.register("other", "other-sk", "Pick", LONG_CHOICES)
    try:
        result = await adapter.send_clarify("555", "Pick", LONG_CHOICES, entry.clarify_id, entry.session_key)
        assert result.success
        view = channel.send.call_args.kwargs["view"]
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=42, display_name="Tester", roles=[]),
            response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock(), defer=AsyncMock()),
            message=SimpleNamespace(embeds=[]),
        )
        await view.children[-1].callback(interaction)
        assert entry.awaiting_text
        assert not entry.event.is_set()
        assert all(button.disabled for button in view.children)
        assert cm.attempt_text_response_for_session(entry.session_key, "my custom answer") == cm.TEXT_RESOLVED
        assert entry.response == "my custom answer"
    finally:
        cm.wait_for_response(entry.clarify_id, timeout=0.01)

