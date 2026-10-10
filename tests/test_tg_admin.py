"""Command permissions are independent from ordinary Telegram message access."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import User

from app import tg_handler
from app.topics import TopicStore


COMMANDS = ["bind", "add", "profile", "intro", "del", "help"]


def _make_update(user_id):
    user = User(user_id, "Пользователь", False) if user_id is not None else None
    message = SimpleNamespace(
        text="Привет", entities=[], caption=None, caption_entities=[],
        sender_chat=None, message_thread_id=10, is_topic_message=True,
        reply_text=AsyncMock(), set_reaction=AsyncMock(),
        photo=[], voice=None, audio=None, document=None, video=None,
    )
    return SimpleNamespace(message=message, effective_user=user)


@pytest.fixture
def context(tmp_path, monkeypatch):
    store = TopicStore(str(tmp_path / "topics.json"))
    store.set_topic(42, 10, "Рабочая группа")
    client = MagicMock()
    client.open_by_link = AsyncMock(return_value={"chatId": 43})
    client.send_message = AsyncMock(return_value={"ok": True})
    client.upload_photo = AsyncMock()
    client.resolver.chat_name.side_effect = lambda cid: "Рабочая группа" if cid == 42 else str(cid)
    client.resolver.is_dm.return_value = False
    client.resolver.chats_raw = {42: {"title": "Рабочая группа"}}
    intro = AsyncMock()
    monkeypatch.setattr(tg_handler, "post_topic_intro", intro)
    bot = AsyncMock()
    bot.create_forum_topic.return_value = SimpleNamespace(message_thread_id=11)
    return SimpleNamespace(
        bot_data={
            tg_handler.ALLOWED_USER_KEY: None,
            tg_handler.MAX_CLIENT_KEY: client,
            tg_handler.TOPIC_STORE_KEY: store,
            tg_handler.SUPERGROUP_KEY: -100123456,
            tg_handler.SENDER_PREFIX_KEY: True,
        },
        args=[], bot=bot, intro=intro,
    )


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize(
    "admin_id,allowed_id,user_id",
    [(777, 100, 777), (None, 100, 100), (None, None, 100)],
    ids=["separate-admin", "legacy-fallback", "unrestricted"],
)
async def test_command_access_for_admin(command, admin_id, allowed_id, user_id, context):
    context.bot_data[tg_handler.ALLOWED_USER_KEY] = allowed_id
    if admin_id is not None:
        context.bot_data[tg_handler.ADMIN_USER_KEY] = admin_id
    context.args = ["https://max.ru/join/test"] if command == "add" else ["43", "Новый чат"]
    update = _make_update(user_id)

    await getattr(tg_handler, f"_cmd_{command}")(update, context)
    await asyncio.sleep(0)  # Let the scheduled topic intro run.

    if command in ("bind", "add"):
        assert context.bot_data[tg_handler.TOPIC_STORE_KEY].get_topic(43) == 11
        context.bot.create_forum_topic.assert_awaited_once()
        context.intro.assert_awaited_once()
        if command == "add":
            context.bot_data[tg_handler.MAX_CLIENT_KEY].open_by_link.assert_awaited_once_with(
                "https://max.ru/join/test"
            )
    elif command == "intro":
        context.intro.assert_awaited_once_with(
            context.bot, -100123456, context.bot_data[tg_handler.MAX_CLIENT_KEY], 42, 10,
        )
    else:
        update.message.reply_text.assert_awaited_once()
        response = update.message.reply_text.await_args.args[0]
        assert {
            "profile": "Рабочая группа", "del": "Удалить этот топик", "help": "max2tg",
        }[command] in response


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize(
    "admin_id,allowed_id,user_id",
    [(777, 100, 100), (None, 100, 555), (777, None, None)],
    ids=["message-user-is-not-admin", "legacy-fallback", "unknown-sender"],
)
async def test_non_admin_command_is_ignored(command, admin_id, allowed_id, user_id, context):
    context.bot_data[tg_handler.ALLOWED_USER_KEY] = allowed_id
    if admin_id is not None:
        context.bot_data[tg_handler.ADMIN_USER_KEY] = admin_id
    context.args = ["https://max.ru/join/test"] if command == "add" else ["43", "Новый чат"]
    update = _make_update(user_id)

    await getattr(tg_handler, f"_cmd_{command}")(update, context)

    update.message.reply_text.assert_not_awaited()
    assert context.bot.mock_calls == []
    assert context.bot_data[tg_handler.MAX_CLIENT_KEY].mock_calls == []
    context.intro.assert_not_awaited()
    assert context.bot_data[tg_handler.TOPIC_STORE_KEY].get_topic(42) == 10
    assert context.bot_data[tg_handler.TOPIC_STORE_KEY].get_topic(43) is None


@pytest.mark.parametrize(
    "admin_id,allowed_id,user_id,permitted",
    [(777, 100, 777, True), (777, 100, 100, False),
     (None, 100, 100, True), (None, 100, 555, False), (777, None, None, False)],
)
async def test_delete_confirmation_checks_admin(admin_id, allowed_id, user_id, permitted, context):
    context.bot_data[tg_handler.ALLOWED_USER_KEY] = allowed_id
    if admin_id is not None:
        context.bot_data[tg_handler.ADMIN_USER_KEY] = admin_id
    update = _make_update(user_id)
    update.callback_query = SimpleNamespace(
        data="del:ok:10:42", answer=AsyncMock(), edit_message_text=AsyncMock(),
    )

    await tg_handler._on_del_callback(update, context)

    if permitted:
        context.bot.delete_forum_topic.assert_awaited_once_with(
            chat_id=-100123456, message_thread_id=10,
        )
        assert context.bot_data[tg_handler.TOPIC_STORE_KEY].get_topic(42) is None
    else:
        context.bot.delete_forum_topic.assert_not_awaited()
        assert context.bot_data[tg_handler.TOPIC_STORE_KEY].get_topic(42) == 10


async def test_non_admin_cannot_cancel_delete_confirmation(context):
    context.bot_data[tg_handler.ADMIN_USER_KEY] = 777
    update = _make_update(100)
    update.callback_query = SimpleNamespace(
        data="del:cancel", answer=AsyncMock(), edit_message_text=AsyncMock(),
    )

    await tg_handler._on_del_callback(update, context)

    update.callback_query.edit_message_text.assert_not_awaited()
    assert context.bot_data[tg_handler.TOPIC_STORE_KEY].get_topic(42) == 10


@pytest.mark.parametrize("media", [False, True], ids=["text", "photo"])
async def test_admin_does_not_bypass_message_restriction(media, context):
    context.bot_data[tg_handler.ADMIN_USER_KEY] = 777
    context.bot_data[tg_handler.ALLOWED_USER_KEY] = 100
    update = _make_update(777)
    if media:
        update.message.text = None
        update.message.photo = [MagicMock()]
        await tg_handler._on_topic_media(update, context)
    else:
        await tg_handler._on_topic_message(update, context)

    client = context.bot_data[tg_handler.MAX_CLIENT_KEY]
    client.send_message.assert_not_awaited()
    client.upload_photo.assert_not_awaited()
    update.message.reply_text.assert_not_awaited()


@pytest.mark.parametrize(
    "admin_id,allowed_id,expected",
    [(None, None, None), (None, 100, 100), (777, None, 777), (777, 100, 777)],
)
def test_build_tg_app_wires_admin_and_message_user_separately(admin_id, allowed_id, expected):
    app = tg_handler.build_tg_app(
        "123456:AAABBBCCC", MagicMock(), "-100123456", MagicMock(),
        allowed_user_id=allowed_id, admin_user_id=admin_id,
    )

    assert app.bot_data[tg_handler.ADMIN_USER_KEY] == expected
    assert app.bot_data[tg_handler.ALLOWED_USER_KEY] == allowed_id
