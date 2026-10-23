"""Отчётный бот для заказчиков (aiogram).

Отдельный бот (REPORT_BOT_TOKEN): показывает статистику кампании
и список лидов с контактами для связи. Отвечает только Telegram ID
из REPORT_BOT_ALLOWED_IDS (и OWNER_TG_ID для отладки).

Запуск: python report_bot.py
Нужны в .env:
    REPORT_BOT_TOKEN=<токен от @BotFather>
    REPORT_BOT_ALLOWED_IDS=<id1,id2,...>   # Telegram ID заказчиков
"""
import logging
import asyncio
import re

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import (Message, InlineKeyboardMarkup,
                           InlineKeyboardButton, ReplyKeyboardMarkup,
                           KeyboardButton)
from aiogram.filters import Command

from config import REPORT_BOT_TOKEN, REPORT_BOT_ALLOWED_IDS, OWNER_TG_ID, TG_BOT_PROXY
import database as db

logger = logging.getLogger(__name__)


def _allowed(user_id: int) -> bool:
    return user_id == OWNER_TG_ID or user_id in REPORT_BOT_ALLOWED_IDS


_shared_bot: Bot | None = None


def get_bot() -> Bot | None:
    """Ленивый синглтон Bot для пушей из воркеров (user_client и др.),
    когда отдельный ReportBot-процесс не запущен."""
    global _shared_bot
    if _shared_bot is None and REPORT_BOT_TOKEN:
        if TG_BOT_PROXY:
            _shared_bot = Bot(token=REPORT_BOT_TOKEN,
                              session=AiohttpSession(proxy=TG_BOT_PROXY))
        else:
            _shared_bot = Bot(token=REPORT_BOT_TOKEN)
    return _shared_bot


def _contact_button(username: str | None, sender_id: int | None) -> InlineKeyboardButton | None:
    """Кнопка «Написать» — по username или tg://user?id=."""
    username = (username or "").strip().lstrip("@")
    if username and re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        return InlineKeyboardButton(
            text=f"✉️ Написать @{username}", url=f"https://t.me/{username}")
    if sender_id:
        return InlineKeyboardButton(
            text="✉️ Открыть профиль", url=f"tg://user?id={int(sender_id)}")
    return None


def _main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Отчёт")],
            [KeyboardButton(text="🔥 Уговоренные"), KeyboardButton(text="🤔 Тёплые")],
            [KeyboardButton(text="📋 Все лиды")],
        ],
        resize_keyboard=True,
    )


def _stage_label(d: dict) -> str:
    if d.get("interest_level") == "HOT":
        return "🔥 согласна — получила ссылку и промокод"
    if d.get("interest_level") == "WARM":
        return "🤔 тёплая — получила ссылку, интересовалась"
    if d.get("stage") == "ENDED":
        return "⛔ диалог завершён"
    if d.get("stage") == "FORUM_OPENER":
        return "⏳ приглашение отправлено, ждём ответ"
    return "💬 в диалоге"


async def _cmd_report(message: Message):
    stats = await db.get_forum_report()
    targets = stats["targets"]
    found = sum(targets.values())
    sent = targets.get("SENT", 0)
    text = (
        "📊 Отчёт по кампании «Форум „Современная женщина“»\n\n"
        f"🔎 Найдено кандидатов в чатах: {found}\n"
        f"📨 Отправлено приглашений: {sent}\n"
        f"💬 Ответили и в диалоге: {stats['replied']}\n"
        f"🔥 Уговорены (получили билетную ссылку): {stats['hot']}\n"
        f"🤔 Тёплые (получили ссылку, интересовались): {stats.get('warm', 0)}\n"
        f"⛔ Завершено без интереса: {stats['ended']}\n"
    )
    await message.answer(text)


async def _cmd_hot(message: Message):
    leads = await db.get_forum_leads(hot_only=True)
    if not leads:
        await message.answer("Пока нет уговоренных. Как только клиент согласится — он появится здесь, и я пришлю уведомление.")
        return
    await message.answer(f"🔥 Уговоренные клиенты ({len(leads)}):")
    for d in leads:
        name = d.get("sender_name") or "—"
        uname = d.get("sender_username")
        line = f"🔥 {name}"
        if uname:
            line += f" (@{uname})"
        line += f"\n   {_stage_label(d)} · {d.get('updated_at', '')[:16]}"
        btn = _contact_button(uname, d.get("sender_id"))
        kb = InlineKeyboardMarkup(inline_keyboard=[[btn]]) if btn else None
        await message.answer(line, reply_markup=kb)


async def _cmd_warm(message: Message):
    leads = await db.get_forum_leads(hot_only=False, limit=100)
    warm = [d for d in leads if d.get("interest_level") == "WARM"]
    if not warm:
        await message.answer("Пока нет тёплых лидов. Как только клиент получит ссылку и проявит интерес — появится здесь.")
        return
    await message.answer(f"🤔 Тёплые лиды ({len(warm)}):")
    for d in warm:
        name = d.get("sender_name") or "—"
        uname = d.get("sender_username")
        line = f"🤔 {name}"
        if uname:
            line += f" (@{uname})"
        line += f"\n   {_stage_label(d)} · {d.get('updated_at', '')[:16]}"
        btn = _contact_button(uname, d.get("sender_id"))
        kb = InlineKeyboardMarkup(inline_keyboard=[[btn]]) if btn else None
        await message.answer(line, reply_markup=kb)


async def _cmd_leads(message: Message):
    leads = await db.get_forum_leads(hot_only=False, limit=200)
    if not leads:
        await message.answer("Диалогов пока нет — рассылка приглашений ещё не начата.")
        return
    total = len(leads)
    hot = sum(1 for d in leads if d.get("interest_level") == "HOT")
    warm = sum(1 for d in leads if d.get("interest_level") == "WARM")
    ended = sum(1 for d in leads if d.get("stage") == "ENDED")
    await message.answer(
        f"📋 Все диалоги: {total} шт\n"
        f"   🔥 HOT: {hot} | 🤔 WARM: {warm} | ⛔ завершён: {ended} | ⏳ активных: {total - ended}")
    # Компактно — до 20 лидов на сообщение
    chunk = []
    for d in leads:
        name = d.get("sender_name") or "—"
        uname = d.get("sender_username")
        lvl = d.get("interest_level") or ""
        tag = "🔥" if lvl == "HOT" else ("🤔" if lvl == "WARM" else "⛔" if d.get("stage") == "ENDED" else "⏳")
        dt = (d.get("created_at") or d.get("updated_at", ""))[:16]
        line = f"{tag} {name}"
        if uname:
            line += f" @{uname}"
        line += f"  ({dt})"
        chunk.append(line)
        if len(chunk) >= 20:
            await message.answer("\n".join(chunk))
            chunk = []
    if chunk:
        await message.answer("\n".join(chunk))


async def notify_hot_lead(bot: Bot, dialog: dict):
    """Пуш всем заказчикам: клиент уговорен — имя + кнопка для связи."""
    name = dialog.get("sender_name") or "Клиент"
    uname = dialog.get("sender_username")
    text = f"🔥 Новая уговоренная: {name}"
    if uname:
        text += f" (@{uname})"
    text += "\nОтправлена ссылка на билеты и промокод. Можно связаться напрямую:"
    btn = _contact_button(uname, dialog.get("sender_id"))
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn]]) if btn else None
    for uid in REPORT_BOT_ALLOWED_IDS:
        try:
            await bot.send_message(uid, text, reply_markup=kb)
        except Exception as e:
            logger.warning(f"Не удалось уведомить заказчика {uid}: {e}")


class ReportBot:
    def __init__(self):
        if TG_BOT_PROXY:
            session = AiohttpSession(proxy=TG_BOT_PROXY)
            self.bot = Bot(token=REPORT_BOT_TOKEN, session=session)
        else:
            self.bot = Bot(token=REPORT_BOT_TOKEN)
        self.dp = Dispatcher()
        self._register_handlers()

    def _register_handlers(self):
        @self.dp.message(Command("start"))
        async def cmd_start(message: Message):
            if not _allowed(message.from_user.id):
                return
            await message.answer(
                "🤖 Отчётный бот кампании «Форум „Современная женщина“».\n\n"
                "«📊 Отчёт» — сводка: найдено, отправлено, в диалоге, уговорены.\n"
                "«🔥 Уговоренные» — клиенты, согласившиеся купить билет (с контактами).\n"
                "«🤔 Тёплые» — получили ссылку, интересовались.\n"
                "«📋 Все лиды» — все диалоги.\n\n"
                "О новых уговоренных присылаю уведомление автоматически.",
                reply_markup=_main_keyboard())

        @self.dp.message(F.text == "📊 Отчёт")
        async def btn_report(message: Message):
            if _allowed(message.from_user.id):
                await _cmd_report(message)

        @self.dp.message(F.text == "🔥 Уговоренные")
        async def btn_hot(message: Message):
            if _allowed(message.from_user.id):
                await _cmd_hot(message)

        @self.dp.message(F.text == "🤔 Тёплые")
        async def btn_warm(message: Message):
            if _allowed(message.from_user.id):
                await _cmd_warm(message)

        @self.dp.message(F.text == "📋 Все лиды")
        async def btn_leads(message: Message):
            if _allowed(message.from_user.id):
                await _cmd_leads(message)

        @self.dp.message(Command("report"))
        async def cmd_report(message: Message):
            if _allowed(message.from_user.id):
                await _cmd_report(message)

        @self.dp.message(Command("hot"))
        async def cmd_hot(message: Message):
            if _allowed(message.from_user.id):
                await _cmd_hot(message)

    async def start(self):
        logger.info("Отчётный бот запущен")
        await db.init_db()
        await self.dp.start_polling(self.bot)


async def main():
    if not REPORT_BOT_TOKEN:
        print("REPORT_BOT_TOKEN не задан в .env")
        return
    if not REPORT_BOT_ALLOWED_IDS:
        print("REPORT_BOT_ALLOWED_IDS не задан — бот никому не ответит")
    bot = ReportBot()
    await bot.start()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
