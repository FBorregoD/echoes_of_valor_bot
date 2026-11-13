"""First-DM welcome: greeted once, only brand-new users, never blocks the
command. (conftest gives every test its own empty tasks.db.)"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import scheduler
from welcome import build_welcome_message, welcome_if_first_dm


def _message(user_id=42, name="Fulano"):
    msg = MagicMock()
    msg.author.id = user_id
    msg.author.display_name = name
    msg.channel.send = AsyncMock()
    return msg


def test_new_user_is_welcomed_exactly_once():
    assert scheduler.claim_first_contact(42) is True
    assert scheduler.claim_first_contact(42) is False
    assert scheduler.claim_first_contact(43) is True


def test_users_who_already_used_the_bot_are_not_welcomed():
    scheduler.record_command_usage(ts=datetime.now(timezone.utc), user_id=7, user_name="old",
                                   command="matches", invoked_as="m", location="DM", success=True,
                                   error=None, duration_ms=5, sheet_fetches=0)
    scheduler.add_subscription(8, "Scorium", False)
    assert scheduler.claim_first_contact(7) is False
    assert scheduler.claim_first_contact(8) is False


def test_welcome_message_content():
    text = build_welcome_message("Fulano", ["Echoes of Valor"])
    assert text.startswith("👋 Hi Fulano, welcome! I'm the bot for **Echoes of Valor**.")
    for command in ("`!m <player>`", "`!sum <player>`", "`!sum_ext <player>`", "`!sub <player>`", "`!help`"):
        assert command in text
    assert "**Echoes of Valor** and **Meridian Ascension**" in build_welcome_message(
        "x", ["Echoes of Valor", "Meridian Ascension"])


@pytest.mark.asyncio
async def test_welcome_sent_on_first_dm_only():
    first = _message()
    assert await welcome_if_first_dm(first, ["Echoes of Valor"]) is True
    assert "welcome" in first.channel.send.call_args.args[0]

    second = _message()
    assert await welcome_if_first_dm(second, ["Echoes of Valor"]) is False
    second.channel.send.assert_not_called()


@pytest.mark.asyncio
async def test_welcome_problem_never_raises():
    msg = _message()
    with patch("scheduler.claim_first_contact", side_effect=RuntimeError("db locked")):
        assert await welcome_if_first_dm(msg, ["Echoes of Valor"]) is False
    msg.channel.send.assert_not_called()
