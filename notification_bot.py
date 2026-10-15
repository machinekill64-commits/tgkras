"""Бот уведомлений (aiogram): присылает лиды на основной аккаунт владельца."""
import logging
import asyncio
import re
from datetime import datetime
from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup,
                           InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton)
from aiogram.filters import Command
from aiogram.enums import ParseMode
from config import (NOTIF_BOT_TOKEN, OWNER_TG_ID, COLD_OUTREACH_DAILY_LIMIT, TG_BOT_PROXY,
                     CHAT_SEARCH_DAILY_LIMIT, CHAT_SEARCH_BATCH_SIZE, CHAT_SEARCH_CLEANUP_DAYS)
import database as db
import gis_importer
import cold_outreach
from lead_presentation import clip_text
from translator import get_language_label

logger = logging.getLogger(__name__)


def _lead_contact(sender_username: str | None, sender_id: int | None,
                  contact_username: str | None = None,
                  contact_source: str | None = None):
    username = (sender_username or "").strip().lstrip("@")
    if username and re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        return f"📨 Username для связи: @{username}", InlineKeyboardButton(
            text=f"Открыть @{username}", url=f"https://t.me/{username}"
        )
    text_contact = (contact_username or "").strip().lstrip("@")
    if text_contact and re.fullmatch(r"[A-Za-z0-9_]{5,32}", text_contact):
        return (f"📨 Контакт из текста: @{text_contact}", InlineKeyboardButton(
            text=f"Открыть @{text_contact}", url=f"https://t.me/{text_contact}"
        ))
    if sender_id:
        return f"📨 Username не указан · Telegram ID: {sender_id}", InlineKeyboardButton(
            text="Открыть профиль автора", url=f"tg://user?id={int(sender_id)}"
        )
    return "📨 Прямой контакт не указан (анонимный/канальный пост)", None


def _keep_url_buttons(markup) -> InlineKeyboardMarkup | None:
    """Сохраняет только URL-кнопки (контакт/источник) после edit_text колбэков."""
    if not markup:
        return None
    rows = [[b for b in row if getattr(b, "url", None)]
            for row in markup.inline_keyboard]
    rows = [r for r in rows if r]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def _lead_message_block(message_text: str, original_message_text: str = None,
                        detected_language: str = None,
                        translation_failed: bool = False) -> str:
    """Блок текста лида: перевод + оригинал, оригинал при сбое, либо сообщение."""
    if translation_failed:
        return ("Перевод временно недоступен.\nОригинал:\n"
                + clip_text(original_message_text or message_text, 1400))
    if original_message_text and original_message_text != message_text:
        lang_label = get_language_label(detected_language) if detected_language else "original"
        return (f"Перевод на русский:\n{clip_text(message_text, 1400)}\n\n"
                f"Оригинал ({lang_label}):\n{clip_text(original_message_text, 400)}")
    return f"Сообщение:\n{clip_text(message_text, 1400)}"


def _can_write_client(sender_type: str | None, sender_id: int | None) -> bool:
    """Кнопку 'Написать клиенту' показываем только когда есть реальный пользователь."""
    if sender_type:
        return sender_type == "user" and bool(sender_id)
    return bool(sender_id)


class NotificationBot:
    """Бот для уведомлений владельца о найденных лидах и управления диалогами."""

    def __init__(self, user_client=None):
        # Создаем сессию с поддержкой прокси если указан
        if TG_BOT_PROXY:
            session = AiohttpSession(proxy=TG_BOT_PROXY)
            self.bot = Bot(token=NOTIF_BOT_TOKEN, session=session)
            logger.info("Bot API: proxy configured")
        else:
            self.bot = Bot(token=NOTIF_BOT_TOKEN)
            logger.info("Bot API: прямое подключение (без прокси)")
        self.dp = Dispatcher()
        self.user_client = user_client
        self._awaiting_price_input = False
        self._price_dialog_ref: str | None = None
        # Управление чатами через бота
        self._awaiting_chat_input = False
        self._awaiting_monitoring_chat = False
        self._awaiting_broadcast_chat = False
        self._awaiting_rules_chat = False
        self._awaiting_times_chat = False
        self._awaiting_lang_chat = False
        self._awaiting_niche_chat = False
        self.scheduler = None
        self._register_handlers()

    def _main_keyboard(self) -> ReplyKeyboardMarkup:
        """Главное меню бота."""
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📊 Статус"), KeyboardButton(text="🔥 Лиды")],
                [KeyboardButton(text="💬 Диалоги"), KeyboardButton(text="🔍 Поиск чатов")],
                [KeyboardButton(text="📋 Найденные чаты"), KeyboardButton(text="❄️ Холодный обход")],
                [KeyboardButton(text="📈 Статистика"), KeyboardButton(text="⚙️ Настройки")],
                [KeyboardButton(text="💰 Указать цену")],
                [KeyboardButton(text="⏸ Остановить парсинг"), KeyboardButton(text="▶️ Запустить парсинг")],
            ],
            resize_keyboard=True,
        )

    def _leads_keyboard(self) -> ReplyKeyboardMarkup:
        """Меню лидов."""
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="🔎 Последнее сообщение")],
                [KeyboardButton(text="📋 Активные диалоги")],
                [KeyboardButton(text="⬅️ Назад")],
            ],
            resize_keyboard=True,
        )

    def _dialogs_keyboard(self) -> ReplyKeyboardMarkup:
        """Меню диалогов."""
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📋 Активные диалоги")],
                [KeyboardButton(text="📊 A/B статистика")],
                [KeyboardButton(text="⬅️ Назад")],
            ],
            resize_keyboard=True,
        )

    def _cold_keyboard(self) -> ReplyKeyboardMarkup:
        """Меню холодного обхода."""
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📦 Импорт 2GIS"), KeyboardButton(text="➡️ Следующая компания")],
                [KeyboardButton(text="📊 Статистика обхода")],
                [KeyboardButton(text="⬅️ Назад")],
            ],
            resize_keyboard=True,
        )

    def _stats_keyboard(self) -> ReplyKeyboardMarkup:
        """Меню статистики."""
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📊 A/B статистика")],
                [KeyboardButton(text="🔎 Последнее сообщение")],
                [KeyboardButton(text="🔎 Статус поиска")],
                [KeyboardButton(text="⬅️ Назад")],
            ],
            resize_keyboard=True,
        )

    def _settings_keyboard(self) -> ReplyKeyboardMarkup:
        """Меню настроек."""
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📋 Список чатов"), KeyboardButton(text="🆕 Добавить чат")],
                [KeyboardButton(text="🔔 Мониторинг"), KeyboardButton(text="📢 Рассылка")],
                [KeyboardButton(text="✅ Завершить прогрев")],
                [KeyboardButton(text="⬅️ Назад")],
            ],
            resize_keyboard=True,
        )

    def _chats_manage_keyboard(self) -> ReplyKeyboardMarkup:
        """Меню управления чатами."""
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📋 Список чатов")],
                [KeyboardButton(text="🆕 Добавить чат")],
                [KeyboardButton(text="🔔 Мониторинг")],
                [KeyboardButton(text="📢 Рассылка")],
                [KeyboardButton(text="📝 Правила чата")],
                [KeyboardButton(text="⏰ Время рассылки")],
                [KeyboardButton(text="🌐 Язык рассылки")],
                [KeyboardButton(text="🏷 Ниша чата")],
                [KeyboardButton(text="⬅️ Назад")],
            ],
            resize_keyboard=True,
        )
    def _register_handlers(self):
        # === Навигация по меню ===
        @self.dp.message(F.text == "🔥 Лиды")
        async def btn_leads(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            await message.answer("🔥 Меню лидов:", reply_markup=self._leads_keyboard())

        @self.dp.message(F.text == "💬 Диалоги")
        async def btn_dialogs(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            await message.answer("💬 Меню диалогов:", reply_markup=self._dialogs_keyboard())

        @self.dp.message(F.text == "🔍 Поиск чатов")
        async def btn_search_chats(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            await self._handle_search_chats(message)

        @self.dp.message(F.text == "❄️ Холодный обход")
        async def btn_cold(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            await message.answer("❄️ Меню холодного обхода:", reply_markup=self._cold_keyboard())

        @self.dp.message(F.text == "📈 Статистика")
        async def btn_stats(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            await message.answer("📈 Меню статистики:", reply_markup=self._stats_keyboard())

        @self.dp.message(F.text == "⚙️ Настройки")
        async def btn_settings(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            await message.answer("⚙️ Меню настроек:", reply_markup=self._settings_keyboard())

        @self.dp.message(F.text == "💰 Указать цену")
        async def btn_price(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            self._awaiting_price_input = True
            self._price_dialog_ref = None
            await message.answer(
                "Введите ID диалога или @ник и цену через пробел.\n"
                "Примеры:\n12 45000\n@username 45000"
            )

        @self.dp.message(F.text == "⏸ Остановить парсинг")
        async def btn_pause_bot(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            if self.user_client:
                if self.user_client.is_paused:
                    await message.answer("⏸ Исходящие уже заблокированы.")
                else:
                    await self.user_client.pause()
                    await message.answer("⏸ Исходящие сообщения заблокированы.\nМониторинг чатов и уведомления о лидах продолжают работать.\nНажмите «▶️ Запустить парсинг» чтобы возобновить отправки.")
            else:
                await message.answer("❌ Клиент не инициализирован")

        @self.dp.message(F.text == "▶️ Запустить парсинг")
        async def btn_resume_bot(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            if self.user_client:
                if not self.user_client.is_paused:
                    await message.answer("▶️ Исходящие уже включены.")
                else:
                    await self.user_client.resume()
                    await message.answer("▶️ Исходящие сообщения разблокированы!\nДиалоги и рассылки возобновлены.")
            else:
                await message.answer("❌ Клиент не инициализирован")

        @self.dp.message(F.text == "⬅️ Назад")
        async def btn_back(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            await message.answer("🏠 Главное меню:", reply_markup=self._main_keyboard())

        # === Кнопки действий ===
        @self.dp.message(F.text == "📊 Статус")
        async def btn_status(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            await self._handle_status(message)

        @self.dp.message(F.text == "🔎 Последнее сообщение")
        async def btn_lastmsg(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            await self._handle_lastmsg(message)

        @self.dp.message(F.text == "📋 Активные диалоги")
        async def btn_active(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            await self._handle_active(message)

        @self.dp.message(F.text == "📊 A/B статистика")
        async def btn_ab_stats(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            await self._handle_ab_stats(message)

        @self.dp.message(F.text == "📦 Импорт 2GIS")
        async def btn_import_gis(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            await self._handle_import_gis(message)

        @self.dp.message(F.text == "➡️ Следующая компания")
        async def btn_next_gis(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            await self._handle_next_gis(message)

        @self.dp.message(F.text == "📊 Статистика обхода")
        async def btn_cold_stats(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            await self._handle_cold_stats(message)

        @self.dp.message(F.text == "✅ Завершить прогрев")
        async def btn_warmup_done(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            await self._handle_warmup_done(message)

        @self.dp.message(F.text == "📋 Список чатов")
        async def btn_chats_list(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            await self._handle_chats_list(message)

        @self.dp.message(F.text == "🔎 Статус поиска")
        async def btn_search_status(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            await self._handle_search_status(message)

        @self.dp.message(F.text == "📋 Найденные чаты")
        async def btn_found_chats(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            await self._handle_found_chats(message)

        @self.dp.callback_query(F.data == "view_found_chats")
        async def cb_view_found_chats(callback: CallbackQuery):
            if callback.from_user.id != OWNER_TG_ID:
                return
            await callback.answer()
            await self._handle_view_found_chats(callback.message)

        @self.dp.callback_query(F.data == "continue_search")
        async def cb_continue_search(callback: CallbackQuery):
            if callback.from_user.id != OWNER_TG_ID:
                return
            await callback.answer("Запускаю поиск...")
            await self._handle_continue_search(callback.message)

        @self.dp.message(Command("start"))
        async def cmd_start(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            await message.answer(
                "🤖 Бот-ассистент запущен!\n\n"
                "Я буду присылать вам найденных лидов из чатов.\n"
                "Используйте кнопки меню для управления.\n\n"
                "Команды также доступны текстом:\n"
                "/status, /chats, /addchat, /active, /ab_stats,\n"
                "/search_chats, /lastmsg, /stats, /help",
                reply_markup=self._main_keyboard(),
            )

        @self.dp.message(Command("help"))
        async def cmd_help(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            await message.answer(
                "📋 Команды:\n\n"
                "/status — статус системы и анти-бан статистика\n"
                "/chats — список отслеживаемых чатов\n"
                "/addchat <chat_link> — добавить чат для мониторинга\n"
                "/addschedule <chat_id> <cron> <текст> — настроить рассылку\n"
                "/import_gis — импорт компаний из 2GIS CSV (data/gis_companies.csv)\n"
                "/next_gis — следующая компания на проверку\n"
                "/approve_gis <id> — одобрить и отправить холодное сообщение\n"
                "/cold_stats — статистика холодного обхода\n"
                "/warmup_done — завершить прогрев аккаунта\n"
                "/lastmsg — последнее прочитанное сообщение по каждому чату\n"
                "/stats — статистика лидов\n"
                "/active — активные диалоги (с кнопками закрытия)\n"
                "/ab_stats — статистика A/B тестирования первых сообщений\n"
                "/search_chats — авто-поиск бизнес-чатов по ключевым словам\n"
                "/pause — остановить парсинг (обработку сообщений и рассылки)\n"
                "/resume — запустить парсинг после паузы\n\n"
                "Когда приходит лид — нажмите «Написать клиенту» чтобы AI начал диалог.\n"
                "Когда клиент опишет задачу — укажите цену сообщением.\n"
                "Формат: /price <dialog_id> <цена>"
            )

        @self.dp.message(Command("status"))
        async def cmd_status(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            await self._handle_status(message)

        @self.dp.message(Command("pause"))
        async def cmd_pause(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            if self.user_client:
                if self.user_client.is_paused:
                    await message.answer("⏸ Бот уже на паузе.")
                else:
                    await self.user_client.pause()
                    await message.answer("⏸ Парсинг остановлен. /resume для запуска.")
            else:
                await message.answer("❌ Клиент не инициализирован")

        @self.dp.message(Command("resume"))
        async def cmd_resume(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            if self.user_client:
                if not self.user_client.is_paused:
                    await message.answer("▶️ Парсинг уже идёт.")
                else:
                    await self.user_client.resume()
                    await message.answer("▶️ Парсинг запущен!")
            else:
                await message.answer("❌ Клиент не инициализирован")

        @self.dp.message(Command("chats"))
        async def cmd_chats(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            chats = await db.get_monitored_chats()
            if not chats:
                await message.answer("Нет отслеживаемых чатов. Добавьте через /addchat")
                return
            text = "📋 Отслеживаемые чаты:\n\n"
            for c in chats:
                sched = f" (рассылка: {c['schedule_cron']})" if c.get("schedule_cron") else ""
                text += f"• {c['chat_name']} — ID: {c['chat_id']}{sched}\n"
            await message.answer(text)

        @self.dp.message(Command("addchat"))
        async def cmd_addchat(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            args = message.text.split(maxsplit=1)
            if len(args) < 2:
                await message.answer(
                    "Использование: /addchat <chat_link или username>\n"
                    "Пример: /addchat @itfreelance_ru\n"
                    "Пример: /addchat https://t.me/itfreelance_ru"
                )
                return
            chat_link = args[1]
            await message.answer(f"Попытка вступить в чат: {chat_link}...")
            if self.user_client:
                chat_id = await self.user_client.join_chat(chat_link)
                if chat_id:
                    entity = await self.user_client.client.get_entity(chat_id)
                    chat_name = getattr(entity, "title", str(chat_id))
                    await db.add_chat(chat_id, chat_name)
                    await self.user_client.reload_chats()
                    await message.answer(f"✅ Вступил в чат: {chat_name} (ID: {chat_id})")
                else:
                    await message.answer("❌ Не удалось вступить в чат")
            else:
                await message.answer("❌ Клиент не инициализирован")

        # === Управление чатами через бота ===

        @self.dp.message(F.text == "📋 Список чатов")
        async def btn_chats_list_settings(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            chats = await db.get_all_chats()
            if not chats:
                await message.answer("Нет чатов. Добавьте через /addchat")
                return
            text = "📋 Все чаты:\n\n"
            for c in chats:
                mon = "✅" if c.get("is_monitored") else "❌"
                br = "📢" if c.get("is_broadcast") else ""
                lang = c.get("broadcast_language") or ""
                niche = c.get("chat_niche") or ""
                times = c.get("broadcast_times") or ""
                line = f"{mon}{br} {c['chat_name']} — ID: {c['chat_id']}"
                if lang:
                    line += f" [{lang}]"
                if niche:
                    line += f" ({niche})"
                if times:
                    line += f" ⏰{times}"
                text += line + "\n"
            if len(text) > 4000:
                text = text[:4000] + "\n... (обрезано)"
            await message.answer(text)

        @self.dp.message(F.text == "➕ Добавить чат")
        async def btn_add_chat(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            self._awaiting_chat_input = True
            await message.answer(
                "Отправьте ссылку или @username чата:\n"
                "Пример: @itfreelance_ru\n"
                "Пример: https://t.me/itfreelance_ru"
            )

        @self.dp.message(F.text == "🔔 Мониторинг")
        async def btn_toggle_monitoring(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            self._awaiting_monitoring_chat = True
            await message.answer(
                "Отправьте ID чата для переключения мониторинга:\n"
                "Список ID: /chats"
            )

        @self.dp.message(F.text == "📢 Рассылка")
        async def btn_toggle_broadcast(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            self._awaiting_broadcast_chat = True
            await message.answer(
                "Отправьте ID чата для переключения рассылки:\n"
                "Список ID: /chats"
            )

        @self.dp.message(F.text == "📝 Правила чата")
        async def btn_chat_rules(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            self._awaiting_rules_chat = True
            await message.answer(
                "Отправьте ID чата и правила через |:\n"
                "Пример: -100123456|WTS, хэштеги обязательны, ценник в конце"
            )

        @self.dp.message(F.text == "⏰ Время рассылки")
        async def btn_broadcast_times(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            self._awaiting_times_chat = True
            await message.answer(
                "Отправьте ID чата и время (cron) через |:\n"
                "Пример: -100123456|0 10,14,18 * * *\n"
                "Это значит: каждый день в 10:00, 14:00, 18:00\n"
                "Формат: минута час день месяц день_недели"
            )

        @self.dp.message(F.text == "🌐 Язык рассылки")
        async def btn_broadcast_lang(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            self._awaiting_lang_chat = True
            await message.answer(
                "Отправьте ID чата и язык через |:\n"
                "Пример: -100123456|ru\n"
                "Доступные: ru, en, ar, pt, id, kk, uz, az"
            )

        @self.dp.message(F.text == "🏷 Ниша чата")
        async def btn_chat_niche(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            self._awaiting_niche_chat = True
            await message.answer(
                "Отправьте ID чата и нишу через |:\n"
                "Пример: -100123456|IT-фриланс\n"
                "Пример: -100123456|Маркетинг\n"
                "Пример: -100123456|Строительство"
            )

        @self.dp.message(Command("warmup_done"))
        async def cmd_warmup_done(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            if self.user_client:
                await self.user_client.anti_ban.mark_warmup_done()
                await message.answer("✅ Прогрев аккаунта завершён. Отправка сообщений разблокирована.")
            else:
                await message.answer("❌ Клиент не инициализирован")

        @self.dp.message(Command("lastmsg"))
        async def cmd_lastmsg(message: Message):
            """Диагностика: последнее прочитанное сообщение в каждом чате + вердикт фильтра.
            Позволяет проверить, что бот реально читает чаты и почему сообщение не стало лидом."""
            if message.from_user.id != OWNER_TG_ID:
                return
            await self._handle_lastmsg(message)

        @self.dp.message(Command("stats"))
        async def cmd_stats(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            async with db.aiosqlite.connect(db.DB_PATH) as conn:
                async with conn.execute(
                    "SELECT category, COUNT(*) FROM leads GROUP BY category"
                ) as cursor:
                    cat_counts = await cursor.fetchall()
                async with conn.execute(
                    "SELECT status, COUNT(*) FROM leads GROUP BY status"
                ) as cursor:
                    status_counts = await cursor.fetchall()
                async with conn.execute("SELECT COUNT(*) FROM dialogs") as cursor:
                    dialogs_count = (await cursor.fetchone())[0]

            text = "📈 Статистика:\n\n"
            text += "Лиды по категориям:\n"
            for cat, cnt in cat_counts:
                text += f"  {cat}: {cnt}\n"
            text += "\nЛиды по статусам:\n"
            for stat, cnt in status_counts:
                text += f"  {stat}: {cnt}\n"
            text += f"\nВсего диалогов: {dialogs_count}"
            await message.answer(text)

        @self.dp.message(Command("active"))
        async def cmd_active(message: Message):
            """Список активных диалогов с кнопками закрытия."""
            if message.from_user.id != OWNER_TG_ID:
                return
            await self._handle_active(message)

        @self.dp.message(Command("ab_stats"))
        async def cmd_ab_stats(message: Message):
            """Статистика A/B тестирования первых сообщений."""
            if message.from_user.id != OWNER_TG_ID:
                return
            await self._handle_ab_stats(message)

        @self.dp.message(Command("search_chats"))
        async def cmd_search_chats(message: Message):
            """Авто-поиск новых чатов по ключевым словам."""
            if message.from_user.id != OWNER_TG_ID:
                return
            await self._handle_search_chats(message)

        @self.dp.callback_query(F.data.startswith("close_"))
        async def cb_close_dialog(callback: CallbackQuery):
            if callback.from_user.id != OWNER_TG_ID:
                return
            dialog_id = int(callback.data.split("_")[1])
            await db.close_dialog(dialog_id)
            await callback.answer("Диалог закрыт")
            await callback.message.edit_text(
                callback.message.text + "\n\n✅ Закрыто",
                reply_markup=_keep_url_buttons(callback.message.reply_markup),
            )

        @self.dp.callback_query(F.data.startswith("approve_chat_"))
        async def cb_approve_chat(callback: CallbackQuery):
            if callback.from_user.id != OWNER_TG_ID:
                return
            parts = callback.data.split("_")
            # approve_chat_{folder}_{username}
            folder = parts[2]
            username = parts[3]
            await callback.answer(f"Вступаю в @{username}...")
            if self.user_client and hasattr(self.user_client, 'client'):
                import chat_finder
                from config import CHAT_JOIN_DAILY_LIMIT
                joined_today = await db.get_chat_joins_today()
                if joined_today >= CHAT_JOIN_DAILY_LIMIT:
                    await callback.message.edit_text(
                        callback.message.text + f"\n\n❌ Дневной лимит вступлений исчерпан ({joined_today}/{CHAT_JOIN_DAILY_LIMIT})",
                        reply_markup=_keep_url_buttons(callback.message.reply_markup),
                    )
                    return
                chat = {"username": username, "title": f"@{username}", "participants_count": 0}
                success = await chat_finder.join_and_organize_chat(
                    self.user_client.client, chat, folder=folder
                )
                if success:
                    await callback.message.edit_text(
                        callback.message.text + f"\n\n✅ Вступил, мьют, архив, папка: {folder}",
                        reply_markup=_keep_url_buttons(callback.message.reply_markup),
                    )
                else:
                    await callback.message.edit_text(
                        callback.message.text + "\n\n❌ Ошибка вступления",
                        reply_markup=_keep_url_buttons(callback.message.reply_markup),
                    )
            else:
                await callback.message.answer("❌ Клиент не инициализирован")

        @self.dp.callback_query(F.data.startswith("reject_chat_"))
        async def cb_reject_chat(callback: CallbackQuery):
            if callback.from_user.id != OWNER_TG_ID:
                return
            await callback.answer("Отклонено")
            await callback.message.edit_text(
                callback.message.text + "\n\n❌ Отклонено",
                reply_markup=_keep_url_buttons(callback.message.reply_markup),
            )

        @self.dp.message(Command("price"))
        async def cmd_price(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            args = message.text.split(maxsplit=2)
            if len(args) < 3:
                await message.answer(
                    "Использование: /price <ID или @ник> <цена>\n"
                    "Примеры:\n/price 12 45000\n/price @username 45000"
                )
                return
            dialog_ref = args[1]
            price = args[2].strip()
            if not price:
                await message.answer("Цена не указана.")
                return
            if self.user_client:
                success = await self.user_client.continue_dialog_with_price(dialog_ref, price)
                if success:
                    await message.answer(f"✅ Цена {price} отправлена в диалог {dialog_ref}.")
                else:
                    await message.answer("❌ Диалог не найден, завершён или сообщение не отправлено.")
            else:
                await message.answer("❌ Клиент не инициализирован")

        @self.dp.callback_query(F.data.startswith("write_"))
        async def cb_write(callback: CallbackQuery):
            if callback.from_user.id != OWNER_TG_ID:
                return
            lead_id = int(callback.data.split("_")[1])
            await callback.answer("Начинаю диалог...")
            if self.user_client:
                success = await self.user_client.start_dialog_with_lead(lead_id)
                if success:
                    await callback.message.edit_text(
                        callback.message.text + "\n\n✅ AI начал диалог с клиентом",
                        reply_markup=_keep_url_buttons(callback.message.reply_markup),
                    )
                else:
                    await callback.message.edit_text(
                        callback.message.text + "\n\n❌ Не удалось начать диалог (лимиты или ошибка)",
                        reply_markup=_keep_url_buttons(callback.message.reply_markup),
                    )
            else:
                await callback.message.answer("❌ Клиент не инициализирован")

        @self.dp.callback_query(F.data.startswith("ignore_"))
        async def cb_ignore(callback: CallbackQuery):
            if callback.from_user.id != OWNER_TG_ID:
                return
            lead_id = int(callback.data.split("_")[1])
            await db.update_lead_status(lead_id, "IGNORED")
            await callback.answer("Лид проигнорирован")
            await callback.message.edit_text(
                callback.message.text + "\n\n❌ Проигнорировано",
                reply_markup=_keep_url_buttons(callback.message.reply_markup),
            )

        @self.dp.callback_query(F.data.startswith("task_"))
        async def cb_task(callback: CallbackQuery):
            if callback.from_user.id != OWNER_TG_ID:
                return
            dialog_id = int(callback.data.split("_")[1])
            self._awaiting_price_input = True
            self._price_dialog_ref = str(dialog_id)
            await callback.answer("Введите цену следующим сообщением")
            await callback.message.answer(
                f"Введите цену для диалога #{dialog_id} обычным сообщением.\nПример: 45000"
            )

        @self.dp.callback_query(F.data.startswith("price_input_"))
        async def cb_price_input(callback: CallbackQuery):
            if callback.from_user.id != OWNER_TG_ID:
                return
            dialog_id = callback.data.split("_")[-1]
            self._awaiting_price_input = True
            self._price_dialog_ref = dialog_id
            await callback.answer("Введите цену следующим сообщением")
            await callback.message.answer(
                f"Введите цену для диалога #{dialog_id} обычным сообщением.\nПример: 45000"
            )

        @self.dp.callback_query(F.data.startswith("cold_write_"))
        async def cb_cold_write(callback: CallbackQuery):
            if callback.from_user.id != OWNER_TG_ID:
                return
            lead_id = int(callback.data.split("_")[2])
            await callback.answer("Начинаю холодный диалог...")
            if self.user_client:
                success = await self.user_client.start_dialog_with_cold_lead(lead_id)
                if success:
                    await callback.message.edit_text(
                        callback.message.text + "\n\n✅ AI начал диалог с холодным лидом",
                        reply_markup=_keep_url_buttons(callback.message.reply_markup),
                    )
                else:
                    await callback.message.edit_text(
                        callback.message.text + "\n\n❌ Не удалось начать диалог (лимиты, уже писали или ошибка)",
                        reply_markup=_keep_url_buttons(callback.message.reply_markup),
                    )
            else:
                await callback.message.answer("❌ Клиент не инициализирован")

        @self.dp.callback_query(F.data.startswith("cold_ignore_"))
        async def cb_cold_ignore(callback: CallbackQuery):
            if callback.from_user.id != OWNER_TG_ID:
                return
            lead_id = int(callback.data.split("_")[2])
            await db.update_cold_lead_status(lead_id, "IGNORED")
            await callback.answer("Холодный лид проигнорирован")
            await callback.message.edit_text(
                callback.message.text + "\n\n❌ Проигнорировано",
                reply_markup=_keep_url_buttons(callback.message.reply_markup),
            )

        @self.dp.callback_query(F.data.startswith("takeover_"))
        async def cb_takeover(callback: CallbackQuery):
            if callback.from_user.id != OWNER_TG_ID:
                return
            dialog_id = int(callback.data.split("_")[1])
            await db.update_dialog_stage(dialog_id, "ENDED")
            await callback.answer("Диалог передан вам")
            await callback.message.edit_text(
                callback.message.text + "\n\n👤 Диалог передан вам. Общайтесь напрямую.",
                reply_markup=_keep_url_buttons(callback.message.reply_markup),
            )

        @self.dp.message(Command("cold_stats"))
        async def cmd_cold_stats(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            async with db.aiosqlite.connect(db.DB_PATH) as conn:
                async with conn.execute(
                    "SELECT status, COUNT(*) FROM gis_companies GROUP BY status"
                ) as cursor:
                    gis_counts = await cursor.fetchall()
                async with conn.execute(
                    "SELECT COUNT(*) FROM cold_outreach_log WHERE DATE(sent_at) = ?",
                    (datetime.now().strftime("%Y-%m-%d"),)
                ) as cursor:
                    sent_today = (await cursor.fetchone())[0]
                async with conn.execute(
                    "SELECT COUNT(*) FROM cold_outreach_log"
                ) as cursor:
                    total_sent = (await cursor.fetchone())[0]

            text = "🧊 Холодный обход:\n\n"
            text += "2GIS компании:\n"
            for status, cnt in gis_counts:
                text += f"  {status}: {cnt}\n"
            text += f"\nОтправлено сегодня: {sent_today}/{COLD_OUTREACH_DAILY_LIMIT}\n"
            text += f"Всего отправлено: {total_sent}\n"
            await message.answer(text)

        @self.dp.message(Command("import_gis"))
        async def cmd_import_gis(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            try:
                companies = gis_importer.import_gis_csv()
                if not companies:
                    await message.answer("❌ Файл data/gis_companies.csv не найден или пуст")
                    return
                await db.import_gis_companies(companies)
                await message.answer(f"✅ Импортировано компаний из 2GIS: {len(companies)}")
            except Exception as e:
                logger.error(f"Ошибка импорта 2GIS: {e}")
                await message.answer(f"❌ Ошибка импорта: {e}")

        @self.dp.message(Command("next_gis"))
        async def cmd_next_gis(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            if not self.user_client:
                await message.answer("❌ Клиент не инициализирован")
                return
            try:
                company = await cold_outreach.prepare_next_gis_company(
                    self.user_client.client
                )
                if not company:
                    await message.answer("Нет новых 2GIS-компаний для обработки")
                    return

                text = (
                    f"🧊 2GIS компания #{company['id']}\n\n"
                    f"🏢 {company['name']}\n"
                    f"📞 {company['phone']}\n"
                    f"🆔 Telegram ID: {company['telegram_id'] or 'не найден'}\n"
                    f"👤 Username: @{company['telegram_username'] or '-'}\n\n"
                    f"💬 Сообщение для отправки:\n{company['generated_message']}\n\n"
                    f"Одобрите командой:\n/approve_gis {company['id']}"
                )
                await message.answer(text)
            except Exception as e:
                logger.error(f"Ошибка подготовки 2GIS: {e}")
                await message.answer(f"❌ Ошибка: {e}")

        @self.dp.message(Command("approve_gis"))
        async def cmd_approve_gis(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            args = message.text.split(maxsplit=1)
            if len(args) < 2:
                await message.answer("Использование: /approve_gis <id>\nПример: /approve_gis 5")
                return
            try:
                company_id = int(args[1])
            except ValueError:
                await message.answer("Неверный ID. Пример: /approve_gis 5")
                return
            if not self.user_client:
                await message.answer("❌ Клиент не инициализирован")
                return
            try:
                success = await cold_outreach.approve_and_send_cold_message(
                    self.user_client.client, company_id, self.user_client.anti_ban
                )
                if success:
                    await message.answer(f"✅ Сообщение компании #{company_id} отправлено")
                else:
                    await message.answer(f"⏳ Не удалось отправить #{company_id}: лимиты или ошибка")
            except Exception as e:
                logger.error(f"Ошибка approve_gis: {e}")
                await message.answer(f"❌ Ошибка: {e}")

        @self.dp.message(F.text)
        async def text_input_handler(message: Message):
            if message.from_user.id != OWNER_TG_ID:
                return
            text = (message.text or "").strip()

            # === Ввод цены ===
            if self._awaiting_price_input:
                if self._price_dialog_ref:
                    dialog_ref = self._price_dialog_ref
                    price = text
                else:
                    parts = text.split(maxsplit=1)
                    if len(parts) < 2:
                        await message.answer("Нужно указать ID или @ник и цену через пробел.")
                        return
                    dialog_ref, price = parts
                if not self.user_client:
                    await message.answer("❌ Клиент не инициализирован")
                    return
                success = await self.user_client.continue_dialog_with_price(dialog_ref, price)
                if success:
                    self._awaiting_price_input = False
                    self._price_dialog_ref = None
                    await message.answer(f"✅ Цена {price} отправлена в диалог {dialog_ref}.")
                else:
                    await message.answer("❌ Не удалось назначить цену. Проверьте ID или @ник.")
                return

            # === Добавление чата ===
            if self._awaiting_chat_input:
                self._awaiting_chat_input = False
                chat_link = text
                await message.answer(f"Попытка вступить в чат: {chat_link}...")
                if self.user_client:
                    chat_id = await self.user_client.join_chat(chat_link)
                    if chat_id:
                        entity = await self.user_client.client.get_entity(chat_id)
                        chat_name = getattr(entity, "title", str(chat_id))
                        await db.add_chat(chat_id, chat_name)
                        await self.user_client.reload_chats()
                        await message.answer(f"✅ Вступил в чат: {chat_name} (ID: {chat_id})")
                    else:
                        await message.answer("❌ Не удалось вступить в чат")
                else:
                    await message.answer("❌ Клиент не инициализирован")
                return

            # === Переключение мониторинга ===
            if self._awaiting_monitoring_chat:
                self._awaiting_monitoring_chat = False
                try:
                    chat_id = int(text)
                except ValueError:
                    await message.answer("❌ Неверный ID. Отправьте число.")
                    return
                chat = await db.get_chat(chat_id)
                if not chat:
                    await message.answer(f"❌ Чат {chat_id} не найден")
                    return
                new_val = 0 if chat.get("is_monitored") else 1
                await db.update_chat(chat_id, is_monitored=bool(new_val))
                if self.user_client:
                    await self.user_client.reload_chats()
                status = "включён" if new_val else "выключен"
                await message.answer(f"✅ Мониторинг чата {chat.get('chat_name')} {status}")
                return

            # === Переключение рассылки ===
            if self._awaiting_broadcast_chat:
                self._awaiting_broadcast_chat = False
                try:
                    chat_id = int(text)
                except ValueError:
                    await message.answer("❌ Неверный ID. Отправьте число.")
                    return
                chat = await db.get_chat(chat_id)
                if not chat:
                    await message.answer(f"❌ Чат {chat_id} не найден")
                    return
                new_val = 0 if chat.get("is_broadcast") else 1
                await db.update_chat(chat_id, is_broadcast=bool(new_val))
                if hasattr(self, 'scheduler') and self.scheduler:
                    await self.scheduler.reload_jobs()
                status = "включена" if new_val else "выключена"
                await message.answer(f"✅ Рассылка чата {chat.get('chat_name')} {status}")
                return

            # === Правила чата ===
            if self._awaiting_rules_chat:
                self._awaiting_rules_chat = False
                parts = text.split("|", 1)
                if len(parts) < 2:
                    await message.answer("❌ Формат: ID|правила")
                    return
                try:
                    chat_id = int(parts[0].strip())
                except ValueError:
                    await message.answer("❌ Неверный ID")
                    return
                rules = parts[1].strip()
                chat = await db.get_chat(chat_id)
                if not chat:
                    await message.answer(f"❌ Чат {chat_id} не найден")
                    return
                await db.update_chat(chat_id, chat_rules=rules)
                await message.answer(f"✅ Правила для {chat.get('chat_name')} обновлены:\n{rules}")
                return

            # === Время рассылки ===
            if self._awaiting_times_chat:
                self._awaiting_times_chat = False
                parts = text.split("|", 1)
                if len(parts) < 2:
                    await message.answer("❌ Формат: ID|cron")
                    return
                try:
                    chat_id = int(parts[0].strip())
                except ValueError:
                    await message.answer("❌ Неверный ID")
                    return
                times = parts[1].strip()
                chat = await db.get_chat(chat_id)
                if not chat:
                    await message.answer(f"❌ Чат {chat_id} не найден")
                    return
                await db.update_chat(chat_id, broadcast_times=times, schedule_cron=times)
                if hasattr(self, 'scheduler') and self.scheduler:
                    await self.scheduler.reload_jobs()
                await message.answer(f"✅ Время рассылки для {chat.get('chat_name')}:\n{times}")
                return

            # === Язык рассылки ===
            if self._awaiting_lang_chat:
                self._awaiting_lang_chat = False
                parts = text.split("|", 1)
                if len(parts) < 2:
                    await message.answer("❌ Формат: ID|язык")
                    return
                try:
                    chat_id = int(parts[0].strip())
                except ValueError:
                    await message.answer("❌ Неверный ID")
                    return
                lang = parts[1].strip().lower()
                valid = {"ru", "en", "ar", "pt", "id", "kk", "uz", "az"}
                if lang not in valid:
                    await message.answer(f"❌ Язык должен быть один из: {', '.join(sorted(valid))}")
                    return
                chat = await db.get_chat(chat_id)
                if not chat:
                    await message.answer(f"❌ Чат {chat_id} не найден")
                    return
                await db.update_chat(chat_id, broadcast_language=lang)
                await message.answer(f"✅ Язык рассылки для {chat.get('chat_name')}: {lang}")
                return

            # === Ниша чата ===
            if self._awaiting_niche_chat:
                self._awaiting_niche_chat = False
                parts = text.split("|", 1)
                if len(parts) < 2:
                    await message.answer("❌ Формат: ID|ниша")
                    return
                try:
                    chat_id = int(parts[0].strip())
                except ValueError:
                    await message.answer("❌ Неверный ID")
                    return
                niche = parts[1].strip()
                chat = await db.get_chat(chat_id)
                if not chat:
                    await message.answer(f"❌ Чат {chat_id} не найден")
                    return
                await db.update_chat(chat_id, chat_niche=niche)
                await message.answer(f"✅ Ниша для {chat.get('chat_name')}: {niche}")
                return

    async def _handle_status(self, message: Message):
        hourly = await db.get_hourly_actions_count()
        daily = await db.get_daily_actions_count()
        cold_daily = await db.get_cold_outreach_count_today()
        chats = await db.get_monitored_chats()
        warmup = self.user_client.anti_ban.is_warmup if self.user_client else "?"
        running = self.user_client.is_running if self.user_client else False

        status_text = (
            f"📊 Статус системы\n\n"
            f"• Клиент: {'✅ Запущен' if running else '❌ Остановлен'}\n"
            f"• Режим: {'⏸ Исходящие заблокированы (мониторинг идёт)' if (self.user_client and self.user_client.is_paused) else '▶️ Полный режим'}\n"
            f"• Прогрев: {'⏳ Активен' if warmup else '✅ Завершён'}\n"
            f"• Чатов отслеживается: {len(chats)}\n"
            f"• Действий за час: {hourly}\n"
            f"• Действий за день: {daily}\n"
            f"• Холодных сообщений сегодня: {cold_daily}/{COLD_OUTREACH_DAILY_LIMIT}\n"
        )
        await message.answer(status_text)

    async def _handle_active(self, message: Message):
        """Список активных диалогов с кнопками закрытия."""
        dialogs = await db.get_active_dialogs()
        if not dialogs:
            await message.answer("📭 Нет активных диалогов")
            return

        text = f"📋 Активные диалогы ({len(dialogs)}):\n\n"
        for d in dialogs:
            score = d.get("lead_score", 0) or 0
            stage = d.get("stage", "?")
            task = (d.get("task") or "")[:60]
            price = d.get("price")
            followups = d.get("followup_count", 0)
            sender = d.get("sender_name", "?")
            username = d.get("sender_username")
            country = d.get("country")
            company = d.get("company")
            interest = d.get("interest_level")
            updated = d.get("updated_at", "?")

            text += (
                f"#{d['id']} ⭐{score}/100 | {stage}"
                + (f" | follow-up:{followups}" if followups else "")
                + "\n"
                f"  👤 {sender}"
                + (f" (@{username})" if username else "")
                + "\n"
            )
            if country:
                text += f"  🌍 {country}\n"
            if company:
                text += f"  🏢 {company}\n"
            if interest:
                text += f"  🌡 {interest}\n"
            if task:
                text += f"  🎯 {task}\n"
            if price:
                text += f"  💰 {price}\n"
            text += f"  🕐 {updated}\n\n"

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"✅ Закрыть #{d['id']}",
                callback_data=f"close_{d['id']}"
            )] for d in dialogs[:10]
        ])
        if len(dialogs) > 10:
            text += f"\n(показано первые 10 из {len(dialogs)})"
        await message.answer(text, reply_markup=keyboard)

    async def _handle_ab_stats(self, message: Message):
        """Статистика A/B тестирования первых сообщений."""
        stats = await db.ab_get_stats()
        if not stats:
            await message.answer("📭 A/B статистика пуста. Пока не отправлено ни одного варианта.")
            return

        variant_names = ["Прямой", "Дружелюбный", "Вопросный"]
        text = "📊 A/B статистика первых сообщений:\n\n"
        total_sent = 0
        total_replied = 0
        for s in stats:
            v = s["variant"]
            name = variant_names[v] if v < len(variant_names) else f"Вариант {v}"
            sent = s["sent_count"]
            replied = s["replied_count"]
            rate = (replied / sent * 100) if sent > 0 else 0
            total_sent += sent
            total_replied += replied
            text += (
                f"#{v} {name}\n"
                f"  Отправлено: {sent}\n"
                f"  Ответов: {replied}\n"
                f"  Конверсия: {rate:.1f}%\n\n"
            )
        overall = (total_replied / total_sent * 100) if total_sent > 0 else 0
        text += f"📈 Всего: {total_sent} отправлено, {total_replied} ответов ({overall:.1f}%)"
        await message.answer(text)

    async def _handle_search_chats(self, message: Message):
        """Поиск чатов по ключевым словам БЕЗ AI. Сохраняет в БД для ручной проверки."""
        if not self.user_client or not hasattr(self.user_client, 'client'):
            await message.answer("❌ Клиент не инициализирован")
            return

        import chat_finder

        # Проверяем, не идёт ли уже поиск
        if chat_finder.get_search_status()["is_running"]:
            await message.answer("⏳ Поиск уже идёт. Нажмите «🔎 Статус поиска» для деталей.")
            return

        # Проверяем дневной лимит
        found_today = await db.get_found_chats_count_today()
        if found_today >= CHAT_SEARCH_DAILY_LIMIT:
            await message.answer(
                f"📊 Сегодня уже найдено {found_today}/{CHAT_SEARCH_DAILY_LIMIT} чатов.\n"
                f"Лимит исчерпан. Нажмите «� Найденные чаты» для просмотра."
            )
            return

        remaining_today = CHAT_SEARCH_DAILY_LIMIT - found_today
        batch = min(CHAT_SEARCH_BATCH_SIZE, remaining_today)

        await message.answer(
            f"🔍 Запускаю поиск чатов (без AI)...\n"
            f"📊 Найдено сегодня: {found_today}/{CHAT_SEARCH_DAILY_LIMIT}\n"
            f"🎯 Ищу батч: {batch} чатов\n\n"
            f"⏳ Поиск идёт в фоне."
        )

        async def _run_search():
            try:
                chat_finder._search_status.update({
                    "total_found": 0, "scanned": 0, "approved": 0, "rejected": 0,
                    "current_chat": "", "current_chat_title": "",
                    "is_running": True, "started_at": datetime.now().strftime("%H:%M:%S"),
                })

                chats = await chat_finder.search_chats_no_ai(
                    self.user_client.client,
                    batch_size=batch,
                )

                chat_finder._search_status["total_found"] = len(chats)
                chat_finder._search_status["is_running"] = False

                # Сохраняем в БД
                saved = 0
                for chat in chats:
                    added = await db.add_found_chat(
                        chat_id=chat["id"],
                        title=chat["title"],
                        username=chat["username"],
                        participants_count=chat["participants_count"],
                        is_channel=chat["is_channel"],
                        is_group=chat["is_group"],
                        search_keyword=chat.get("search_keyword"),
                    )
                    if added:
                        saved += 1

                total_now = await db.get_found_chats_count_today()
                await self.bot.send_message(
                    OWNER_TG_ID,
                    f"✅ Поиск завершён! Найдено {saved} новых чатов.\n"
                    f"📊 Всего за сегодня: {total_now}/{CHAT_SEARCH_DAILY_LIMIT}\n\n"
                    f"Нажмите «📋 Найденные чаты» для просмотра."
                )

            except Exception as e:
                chat_finder._search_status["is_running"] = False
                logger.error(f"Ошибка фонового поиска чатов: {e}", exc_info=True)
                await self.bot.send_message(
                    OWNER_TG_ID,
                    f"❌ Ошибка поиска чатов: {e}"
                )

        asyncio.create_task(_run_search())

    async def _handle_search_status(self, message: Message):
        """Текущий статус поиска чатов."""
        import chat_finder
        s = chat_finder.get_search_status()
        running = "🔄 Да" if s["is_running"] else "✅ Завершён"
        text = (
            f"🔎 Статус поиска чатов\n\n"
            f"🏃 Идёт: {running}\n"
            f"🕐 Начат: {s['started_at'] or '—'}\n\n"
            f"📊 Найдено всего: {s['total_found']}\n"
            f"✅ Просмотрено: {s['scanned']}\n"
            f"🔥 Одобрено: {s['approved']}\n"
            f"❌ Отклонено: {s['rejected']}\n\n"
            f"👀 Последний чат: @{s['current_chat'] or '—'}\n"
            f"� Название: {s['current_chat_title'] or '—'}"
        )
        await message.answer(text)

    async def _handle_found_chats(self, message: Message):
        """Показывает количество найденных за день чатов с кнопками просмотра и продолжения."""
        count = await db.get_found_chats_count_today()
        if count == 0:
            await message.answer(
                "📋 Найденные чаты\n\n"
                "📭 Сегодня чатов не найдено.\n"
                "Нажмите «� Поиск чатов» чтобы запустить поиск."
            )
            return

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📊 Просмотр чатов (Excel)", callback_data="view_found_chats")],
            [InlineKeyboardButton(text="🔄 Продолжить поиски", callback_data="continue_search")],
        ])
        await message.answer(
            f"📋 Найденные чаты\n\n"
            f"📊 Найдено за сегодня: {count}/{CHAT_SEARCH_DAILY_LIMIT}\n"
            f"⏳ Чаты старше {CHAT_SEARCH_CLEANUP_DAYS} дней удаляются автоматически\n\n"
            f"Нажмите кнопку ниже для действий:",
            reply_markup=keyboard
        )

    async def _handle_view_found_chats(self, message: Message):
        """Отправляет Excel-файл с найденными за сегодня чатами."""
        chats = await db.get_found_chats_today()
        if not chats:
            await message.answer("📭 Нет найденных чатов за сегодня.")
            return

        import io
        from openpyxl import Workbook
        from aiogram.types import BufferedInputFile

        wb = Workbook()
        ws = wb.active
        ws.title = "Найденные чаты"
        ws.append(["№", "Название", "Username", "Ссылка", "Участников", "Тип", "Ключевое слово", "Дата"])

        for i, chat in enumerate(chats, 1):
            chat_type = "Канал" if chat["is_channel"] else ("Группа" if chat["is_group"] else "Чат")
            ws.append([
                i,
                chat["title"],
                f"@{chat['username']}",
                f"https://t.me/{chat['username']}",
                chat["participants_count"],
                chat_type,
                chat.get("search_keyword", ""),
                chat.get("found_date", ""),
            ])

        # Авто-ширина колонок
        for col in ws.columns:
            max_len = max(len(str(cell.value or "")) for cell in col)
            ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 40)

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)

        today = datetime.now().strftime("%Y-%m-%d")
        filename = f"found_chats_{today}.xlsx"

        await message.answer_document(
            BufferedInputFile(buf.read(), filename),
            caption=f"📊 Найденные чаты за {today} ({len(chats)} шт.)"
        )

    async def _handle_continue_search(self, message: Message):
        """Продолжает поиск чатов (ещё один батч)."""
        if not self.user_client or not hasattr(self.user_client, 'client'):
            await message.answer("❌ Клиент не инициализирован")
            return

        import chat_finder

        if chat_finder.get_search_status()["is_running"]:
            await message.answer("⏳ Поиск уже идёт. Подождите...")
            return

        found_today = await db.get_found_chats_count_today()
        if found_today >= CHAT_SEARCH_DAILY_LIMIT:
            await message.answer(
                f"📊 Лимит на сегодня исчерпан: {found_today}/{CHAT_SEARCH_DAILY_LIMIT}\n"
                f"Чаты удалятся через {CHAT_SEARCH_CLEANUP_DAYS} дней."
            )
            return

        remaining = CHAT_SEARCH_DAILY_LIMIT - found_today
        batch = min(CHAT_SEARCH_BATCH_SIZE, remaining)

        await message.answer(f"🔄 Продолжаю поиск ({batch} чатов)...")

        async def _run_search():
            try:
                chat_finder._search_status.update({
                    "total_found": 0, "scanned": 0, "approved": 0, "rejected": 0,
                    "current_chat": "", "current_chat_title": "",
                    "is_running": True, "started_at": datetime.now().strftime("%H:%M:%S"),
                })

                chats = await chat_finder.search_chats_no_ai(
                    self.user_client.client,
                    batch_size=batch,
                )

                chat_finder._search_status["total_found"] = len(chats)
                chat_finder._search_status["is_running"] = False

                saved = 0
                for chat in chats:
                    added = await db.add_found_chat(
                        chat_id=chat["id"],
                        title=chat["title"],
                        username=chat["username"],
                        participants_count=chat["participants_count"],
                        is_channel=chat["is_channel"],
                        is_group=chat["is_group"],
                        search_keyword=chat.get("search_keyword"),
                    )
                    if added:
                        saved += 1

                total_now = await db.get_found_chats_count_today()
                await self.bot.send_message(
                    OWNER_TG_ID,
                    f"✅ Поиск завершён! Найдено {saved} новых чатов.\n"
                    f"� Всего за сегодня: {total_now}/{CHAT_SEARCH_DAILY_LIMIT}\n\n"
                    f"Нажмите «📋 Найденные чаты» для просмотра."
                )

            except Exception as e:
                chat_finder._search_status["is_running"] = False
                logger.error(f"Ошибка фонового поиска чатов: {e}", exc_info=True)
                await self.bot.send_message(
                    OWNER_TG_ID,
                    f"❌ Ошибка поиска чатов: {e}"
                )

        asyncio.create_task(_run_search())

    async def _handle_chats_list(self, message: Message):
        """Список отслеживаемых чатов."""
        chats = await db.get_monitored_chats()
        if not chats:
            await message.answer("Нет отслеживаемых чатов. Добавьте через /addchat")
            return
        text = "📋 Отслеживаемые чаты:\n\n"
        for c in chats:
            sched = f" (рассылка: {c['schedule_cron']})" if c.get("schedule_cron") else ""
            text += f"• {c['chat_name']} — ID: {c['chat_id']}{sched}\n"
        await message.answer(text)

    async def _handle_warmup_done(self, message: Message):
        """Завершить прогрев аккаунта."""
        if self.user_client:
            await self.user_client.anti_ban.mark_warmup_done()
            await message.answer("✅ Прогрев аккаунта завершён. Отправка сообщений разблокирована.")
        else:
            await message.answer("❌ Клиент не инициализирован")

    async def _handle_import_gis(self, message: Message):
        """Импорт компаний из 2GIS CSV."""
        try:
            count = await gis_importer.import_csv()
            await message.answer(f"✅ Импортировано {count} компаний из 2GIS CSV")
        except Exception as e:
            await message.answer(f"❌ Ошибка импорта: {e}")

    async def _handle_next_gis(self, message: Message):
        """Следующая компания 2GIS на проверку."""
        if not self.user_client:
            await message.answer("❌ Клиент не инициализирован")
            return
        company = await db.get_next_pending_gis_company()
        if not company:
            await message.answer("📭 Нет компаний на проверку. Импортируйте через «📦 Импорт 2GIS»")
            return
        text = (
            f"🏢 Компания #{company['id']}\n"
            f"Название: {company.get('name', '?')}\n"
            f"Адрес: {company.get('address', '?')}\n"
            f"Телефон: {company.get('phone', '?')}\n"
            f"Сайт: {company.get('website', '?')}\n"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_gis_{company['id']}"),
            InlineKeyboardButton(text="❌ Пропустить", callback_data=f"skip_gis_{company['id']}"),
        ]])
        await message.answer(text, reply_markup=keyboard)

    async def _handle_cold_stats(self, message: Message):
        """Статистика холодного обхода."""
        async with db.aiosqlite.connect(db.DB_PATH) as conn:
            conn.row_factory = db.aiosqlite.Row
            async with conn.execute(
                "SELECT status, COUNT(*) as cnt FROM gis_companies GROUP BY status"
            ) as cursor:
                rows = await cursor.fetchall()
            today = datetime.now().strftime("%Y-%m-%d")
            async with conn.execute(
                "SELECT COUNT(*) FROM cold_outreach_log WHERE DATE(sent_at) = ?", (today,)
            ) as cursor:
                sent_today = (await cursor.fetchone())[0]
        text = "📊 Статистика холодного обхода:\n\n"
        for r in rows:
            text += f"• {r['status']}: {r['cnt']}\n"
        text += f"\nОтправлено сегодня: {sent_today}/{COLD_OUTREACH_DAILY_LIMIT}"
        await message.answer(text)

    async def _handle_lastmsg(self, message: Message):
        """Диагностика: последнее прочитанное сообщение в каждом чате + вердикт фильтра.
        Позволяет проверить, что бот реально читает чаты и почему сообщение не стало лидом."""
        rows = await db.get_last_messages_per_monitored_chat()
        if not rows:
            await message.answer("Нет отслеживаемых чатов. Добавьте через /addchat")
            return

        verdict_labels = {
            "level0_filtered": "🗑 отсеяно (возраст/длина/бот/спам)",
            "level1_no_keywords": "🗑 нет ключевых слов",
            "no_cold_signals": "🗑 нет холодных сигналов",
            "ai_error": "⚠️ ошибка AI-классификации",
            "ai_not_lead": "➖ AI: не лид",
            "ai_not_cold_lead": "➖ AI: не холодный лид",
        }

        text = "🔎 Последнее прочитанное сообщение по чатам:\n\n"
        for r in rows:
            chat_name = r["chat_name"] or str(r["chat_id"])
            if not r.get("created_at"):
                text += f"📍 {chat_name}\n   ещё не читал ни одного сообщения\n\n"
                continue

            verdict = r.get("filter_verdict") or "⏳ обрабатывается / устарело"
            if verdict.startswith("lead_"):
                label = f"🔥 ЛИД ({verdict[5:]})"
            elif verdict.startswith("cold_"):
                label = f"❄️ холодный лид ({verdict[5:]})"
            else:
                base = verdict.split("|")[0]
                label = verdict_labels.get(base, verdict)

            when = r["created_at"]
            msg_preview = (r.get("message_text") or "")[:200]
            text += (
                f"📍 {chat_name}\n"
                f"   🕒 {when}\n"
                f"   👤 {r.get('sender_name') or '?'}\n"
                f"   💬 {msg_preview}\n"
                f"   ➜ {label}\n\n"
            )

        for chunk_start in range(0, len(text), 3500):
            await message.answer(text[chunk_start:chunk_start + 3500])

    async def notify_cold_lead(self, lead_id: int, chat_name: str, sender_name: str,
                               sender_username: str | None, message_text: str,
                               category: str, business_type: str = None,
                               pain: str = None, hook: str = None,
                               market_price: str = None, market_deadline: str = None,
                               lead_score: int = 0, sender_id: int = None,
                               source_message_id: int = None, source_url: str = None,
                               sender_type: str = None, contact_username: str = None,
                               contact_source: str = None, service: str = None,
                               evidence: str = None,
                               original_message_text: str = None,
                               detected_language: str = None,
                               translation_failed: bool = False):
        """Отправка уведомления о холодном лиде владельцу."""
        category_emoji = {"HOT_COLD": "❄️🔥", "WARM_COLD": "🧊"}.get(category, "❓")
        contact_text, contact_button = _lead_contact(
            sender_username, sender_id, contact_username, contact_source)
        action_row = []
        if _can_write_client(sender_type, sender_id):
            action_row.append(InlineKeyboardButton(
                text="✅ Написать клиенту", callback_data=f"cold_write_{lead_id}"))
        action_row.append(InlineKeyboardButton(
            text="❌ Игнорировать", callback_data=f"cold_ignore_{lead_id}"))
        keyboard_rows = [action_row]
        if contact_button:
            keyboard_rows.append([contact_button])
        if source_url:
            keyboard_rows.append([InlineKeyboardButton(
                text="🔗 Исходное сообщение", url=source_url)])
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
        text = (
            f"{category_emoji} Возможная потребность #{lead_id}\n\n"
            f"📍 Чат: {chat_name}\n"
            f"👤 Автор: {sender_name}"
            + (f" (@{sender_username})" if sender_username else "") + "\n"
            f"{contact_text}\n"
            f"📂 Категория: {category}\n"
        )
        if service:
            text += f"🛠 Услуга: {service}\n"
        if business_type:
            text += f"🏢 Тип бизнеса: {business_type}\n"
        if pain:
            text += f"💡 Потребность: {pain}\n"
        if category == "WARM_COLD":
            text += "Заказ на разработку пока не заявлен.\n"
        if evidence:
            text += f"📎 Основание: «{clip_text(evidence, 350)}»\n"
        text += "\n" + _lead_message_block(
            message_text, original_message_text, detected_language,
            translation_failed) + "\n"
        if hook:
            text += f"\n🎯 Подход: {hook}\n"
        if market_price or market_deadline:
            text += "\n📊 Рыночная оценка (совет AI):\n"
            if market_price:
                text += f"   💵 Рекомендуемая цена: {market_price}\n"
            if market_deadline:
                text += f"   ⏱ Рекомендуемый срок: {market_deadline}\n"
        await self.bot.send_message(OWNER_TG_ID, clip_text(text, 3500),
                                    reply_markup=keyboard)

    async def notify_lead(self, lead_id: int, chat_name: str, sender_name: str,
                          sender_username: str | None, message_text: str,
                          category: str, task: str = None, budget: str = None,
                          deadline: str = None, market_price: str = None,
                          market_deadline: str = None, lead_score: int = 0,
                          sender_id: int = None, dialog_id: int = None,
                          recommendation: str = None, service_description: str = None,
                          country: str = None, brief: str = None, reason: str = None,
                          source_message_id: int = None, source_url: str = None,
                          sender_type: str = None, contact_username: str = None,
                          contact_source: str = None, service: str = None,
                          evidence: str = None,
                          original_message_text: str = None,
                          detected_language: str = None,
                          translation_failed: bool = False):
        """Отправка уведомления о лиде владельцу."""

        if category == "PRICE_REQUIRED":
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="💰 Ввести цену", callback_data=f"price_input_{dialog_id}"),
                InlineKeyboardButton(text="👤 Взять на себя", callback_data=f"takeover_{dialog_id}"),
            ]])
            text = (
                f"💰 Нужна ваша цена\n\n"
                f"Диалог: #{dialog_id}\n"
                f"Клиент: {sender_name}"
                + (f" (@{sender_username})" if sender_username else "") + "\n"
                + (f"Страна: {country}\n" if country else "")
                + f"Услуга вне прайса: {service_description or 'не определена'}\n\n"
                f"Рекомендация GigaChat:\n{recommendation or 'нет рекомендации'}\n\n"
                f"Собранные данные:\n{message_text[:1800]}\n\n"
                f"Нажмите «Ввести цену» или используйте:\n"
                f"/price {dialog_id} <цена>"
            )
            await self.bot.send_message(OWNER_TG_ID, text, reply_markup=keyboard)
            return

        if category == "DEAL_READY":
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="👤 Беру диалог", callback_data=f"takeover_{dialog_id}"),
            ]])
            text = (
                f"🔥 Клиент готов к работе\n\n"
                f"Диалог: #{dialog_id}\n"
                f"Клиент: {sender_name}"
                + (f" (@{sender_username})" if sender_username else "") + "\n"
                + (f"Страна: {country}\n\n" if country else "\n")
                + f"ТЗ и данные:\n{(brief or message_text)[:3000]}\n\n"
                "AI остановлен. Продолжайте общение с клиентом вручную."
            )
            await self.bot.send_message(OWNER_TG_ID, text, reply_markup=keyboard)
            return

        if category == "HUMAN_REQUIRED":
            text = (
                f"⚠️ Нужна ручная проверка диалога #{dialog_id}\n"
                f"Клиент: {sender_name}"
                + (f" (@{sender_username})" if sender_username else "") + "\n"
                + f"Причина: {reason or 'AI не уверен'}\n\n"
                f"Сообщение:\n{message_text[:1000]}"
            )
            await self.bot.send_message(OWNER_TG_ID, text)
            return

        if category == "TASK_READY":
            # Клиент описал задачу — нужен ввод цены
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="💬 Указать цену", callback_data=f"task_{dialog_id}"),
                    InlineKeyboardButton(text="👤 Взять на себя", callback_data=f"takeover_{dialog_id}"),
                ],
            ])
            text = (
                f"📋 Клиент описал задачу!\n\n"
                f"👤 Клиент: {sender_name}"
                + (f" (@{sender_username})" if sender_username else "") + "\n"
                f"💬 Диалог #{dialog_id}\n\n"
                f"📝 Описание от клиента:\n{message_text[:500]}\n\n"
                f"Укажите цену командой:\n/price {dialog_id} <ваша_цена>\n"
                f"Или возьмите диалог на себя."
            )
            await self.bot.send_message(OWNER_TG_ID, text, reply_markup=keyboard)
            return

        if category == "TASK_UPDATE":
            # Дополнение задачи от клиента
            text = (
                f"💬 Клиент дополнил задачу:\n\n"
                f"👤 {sender_name}"
                + (f" (@{sender_username})" if sender_username else "") + "\n"
                f"📝 {message_text[:300]}\n\n"
                f"Диалог #{dialog_id}. Укажите цену: /price {dialog_id} <цена>"
            )
            await self.bot.send_message(OWNER_TG_ID, text)
            return

        # Обычный лид из чата
        category_emoji = {"HOT": "🔥", "WARM": "🌡️"}.get(category, "❓")
        contact_text, contact_button = _lead_contact(
            sender_username, sender_id, contact_username, contact_source)
        action_row = []
        if _can_write_client(sender_type, sender_id):
            action_row.append(InlineKeyboardButton(
                text="✅ Написать клиенту", callback_data=f"write_{lead_id}"))
        action_row.append(InlineKeyboardButton(
            text="❌ Игнорировать", callback_data=f"ignore_{lead_id}"))
        keyboard_rows = [action_row]
        if contact_button:
            keyboard_rows.append([contact_button])
        if source_url:
            keyboard_rows.append([InlineKeyboardButton(
                text="🔗 Исходное сообщение", url=source_url)])
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

        text = (
            f"{category_emoji} Найден лид #{lead_id}\n\n"
            f"📍 Чат: {chat_name}\n"
            f"👤 Автор: {sender_name}"
            + (f" (@{sender_username})" if sender_username else "") + "\n"
            f"{contact_text}\n"
            f"📂 Категория: {category}\n"
        )
        if service:
            text += f"🛠 Услуга: {service}\n"
        elif lead_score:
            text += f"⭐ Скор: {lead_score}/100\n"
        if task:
            text += f"🎯 Задача: {task}\n"
        if budget:
            text += f"💰 Бюджет клиента: {budget}\n"
        if deadline:
            text += f"⏰ Сроки клиента: {deadline}\n"
        if evidence:
            text += f"📎 Основание: «{clip_text(evidence, 350)}»\n"
        text += "\n" + _lead_message_block(
            message_text, original_message_text, detected_language,
            translation_failed) + "\n"
        if market_price or market_deadline:
            text += "\n📊 Рыночная оценка (совет AI, клиент её не называл):\n"
            if market_price:
                text += f"   💵 Рекомендуемая цена: {market_price}\n"
            if market_deadline:
                text += f"   ⏱ Рекомендуемый срок: {market_deadline}\n"

        await self.bot.send_message(OWNER_TG_ID, clip_text(text, 3500),
                                    reply_markup=keyboard)

    async def start(self):
        """Запуск бота (polling)."""
        await self.bot.delete_webhook(drop_pending_updates=True)
        logger.info("Бот уведомлений запущен")
        await self.dp.start_polling(self.bot)

    async def stop(self):
        try:
            await self.dp.stop_polling()
        except RuntimeError as e:
            if "Polling is not started" not in str(e):
                raise
        await self.bot.session.close()
