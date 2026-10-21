"""Telethon клиент: мониторинг чатов, отправка сообщений, ведение диалогов."""
import json
import logging
import asyncio
import hashlib
import re
import time
from datetime import datetime
from telethon import TelegramClient, events, connection, utils
from telethon.tl.custom import Message
from telethon.tl import types as tl_types
from telethon.errors import PeerFloodError, FloodWaitError
from config import (TG_API_ID, TG_API_HASH, TG_PHONE, TG_SESSION_NAME,
                    TG_MTPROTO_HOST, TG_MTPROTO_PORT, TG_MTPROTO_SECRET,
                    TG_PROXY_TYPE, TG_PROXY_HOST, TG_PROXY_PORT,
                    TG_PROXY_USERNAME, TG_PROXY_PASSWORD,
                    COLD_LEAD_ENABLED, FOLLOWUP_MAX,
                    FORUM_OUTREACH_ENABLED, HARVEST_MIN_MSG_LEN,
                    FORUM_CLOSING_TEXT, FORUM_FOLLOWUP_TEXT,
                    FORUM_FOLLOWUP_ENABLED,
                    FORUM_OPENER_VARIANTS,
                    FORUM_DECLINE_TEXT,
                    FORUM_MSG_COOLDOWN_MIN, FORUM_MSG_COOLDOWN_MAX,
                    DIALOG_REPLY_DELAY_MIN, DIALOG_REPLY_DELAY_MAX,
                    FORUM_REPLY_TEMPLATES, FORUM_SPEAKERS, FORUM_PROGRAM, FORUM_TIME,
                    FORUM_DATE, FORUM_VENUE, FORUM_PROMO_CODE, FORUM_DISCOUNT,
                    FORUM_TICKET_URL, FORUM_INSTAGRAM)
import database as db
from anti_ban import AntiBan
from lead_qualification import qualify_lead

logger = logging.getLogger(__name__)

_text_contact_pattern = re.compile(
    r"(?i)(?:пишите|контакт|связь|связаться|обращаться|contact)\s*[:\-—]?\s*@([A-Za-z0-9_]{5,32})"
    r"|(?:t\.me|telegram\.me)/([A-Za-z0-9_]{5,32})"
)


def _active_usernames(entity) -> list[str]:
    """Основной и активные дополнительные username сущности."""
    names = []
    primary = getattr(entity, "username", None)
    if primary:
        names.append(primary)
    for alias in getattr(entity, "usernames", None) or []:
        name = getattr(alias, "username", None)
        if name and getattr(alias, "active", True) and name not in names:
            names.append(name)
    return names


def _sender_metadata(sender, text: str) -> dict:
    """Безопасно извлекает данные об отправителе и контакте из текста."""
    sender_id = getattr(sender, "id", None)
    sender_name = (getattr(sender, "first_name", "") or
                   getattr(sender, "title", "") or "Unknown")
    usernames = _active_usernames(sender) if sender else []
    sender_username = usernames[0] if usernames else None

    if sender is None:
        sender_type = "unknown"
    elif isinstance(sender, tl_types.User):
        sender_type = "bot" if getattr(sender, "bot", False) else "user"
    else:
        sender_type = "channel"

    contact_username = None
    contact_source = None
    if sender_username:
        contact_username, contact_source = sender_username, "author"
    m = _text_contact_pattern.search(text or "")
    if m:
        text_contact = m.group(1) or m.group(2)
        if text_contact and text_contact.lower() != (sender_username or "").lower():
            if not contact_username:
                contact_username, contact_source = text_contact, "text"

    return {
        "sender_id": sender_id,
        "sender_name": sender_name,
        "sender_username": sender_username,
        "sender_type": sender_type,
        "contact_username": contact_username,
        "contact_source": contact_source,
    }


def _source_url(chat_entity, chat_id: int, message_id: int) -> str | None:
    """Ссылка на исходное сообщение: public-чат или private supergroup."""
    usernames = _active_usernames(chat_entity)
    if usernames:
        return f"https://t.me/{usernames[0]}/{message_id}"
    marked = str(chat_id or "")
    if marked.startswith("-100"):
        return f"https://t.me/c/{marked[4:]}/{message_id}"
    return None


def _build_telegram_client() -> TelegramClient:
    """Создаёт TelegramClient, при необходимости через прокси."""
    kwargs = {
        "connection_retries": 5,
        "retry_delay": 2,
    }
    # Приоритет 1: SOCKS5/HTTP прокси (TG_PROXY_TYPE)
    if TG_PROXY_TYPE and TG_PROXY_HOST and TG_PROXY_PORT:
        proxy_dict = {
            "proxy_type": TG_PROXY_TYPE,
            "addr": TG_PROXY_HOST,
            "port": TG_PROXY_PORT,
        }
        if TG_PROXY_USERNAME:
            proxy_dict["username"] = TG_PROXY_USERNAME
        if TG_PROXY_PASSWORD:
            proxy_dict["password"] = TG_PROXY_PASSWORD
        logger.info(
            "Telethon: %s proxy %s:%s",
            TG_PROXY_TYPE.upper(), TG_PROXY_HOST, TG_PROXY_PORT,
        )
        kwargs["proxy"] = proxy_dict
    # Приоритет 2: MTProto прокси (старый формат)
    elif TG_MTPROTO_HOST and TG_MTPROTO_PORT and TG_MTPROTO_SECRET:
        logger.info(
            "Telethon: MTProto proxy %s:%s",
            TG_MTPROTO_HOST, TG_MTPROTO_PORT,
        )
        kwargs["connection"] = connection.ConnectionTcpMTProxyRandomizedIntermediate
        kwargs["proxy"] = (TG_MTPROTO_HOST, TG_MTPROTO_PORT, TG_MTPROTO_SECRET)
    else:
        logger.info("Telethon: прямое подключение (без прокси)")

    return TelegramClient(TG_SESSION_NAME, TG_API_ID, TG_API_HASH, **kwargs)


class UserClient:
    """Управление рабочим аккаунтом через Telethon."""

    def __init__(self, notification_callback=None):
        self.client = _build_telegram_client()
        self.anti_ban = AntiBan()
        self.notification_callback = notification_callback
        self._monitored_chat_ids: set[int] = set()
        self._harvest_chat_ids: set[int] = set()
        self._running = False
        self._paused = False
        self._me_id: int | None = None
        self._ai_semaphore = asyncio.Semaphore(5)
        self._lead_semaphore = asyncio.Semaphore(2)
        self._dialog_locks: dict[int, asyncio.Lock] = {}
        # Дебаунс сообщений: ждём пока клиент допишет серию сообщений
        self._dialog_timers: dict[int, asyncio.TimerHandle] = {}
        self._pending_messages: dict[int, list[str]] = {}
        self._dialog_debounce_seconds = 4.0  # ждать 4 секунды после последнего сообщения
        # Кэш дедупликации спамеров: (sender_id, text_hash) -> first_seen_timestamp
        # Если тот же отправитель пишет то же сообщение повторно — пропускаем AI
        self._spam_cache: dict[tuple[int, str], float] = {}
        self._spam_cache_ttl = 86400  # 24 часа
        # Кэш input-entity отправителей ЛС: sender_id -> InputPeer.
        # Входящее ЛС от незнакомца приходит как UpdateShortMessage без
        # access_hash — без этого кэша send_message(sender_id) падает с
        # "Could not find the input entity".
        self._input_peers: dict[int, object] = {}

    @property
    def is_paused(self) -> bool:
        """Исходящие сообщения заблокированы владельцем (мониторинг продолжает работать)."""
        return self._paused

    async def pause(self):
        """Заблокировать исходящие сообщения (сохраняется в БД, переживает рестарт)."""
        self._paused = True
        await db.set_setting("outbound_paused", "1")
        logger.info("Исходящие сообщения заблокированы владельцем (мониторинг активен)")

    async def resume(self):
        """Снять блокировку исходящих сообщений."""
        self._paused = False
        await db.set_setting("outbound_paused", "0")
        logger.info("Блокировка исходящих сообщений снята владельцем")

    async def start(self):
        """Запуск клиента и авторизация."""
        await self.client.start(phone=TG_PHONE)
        me = await self.client.get_me()
        logger.info(f"Рабочий аккаунт: {me.first_name} (@{me.username}, ID: {me.id})")

        self._paused = (await db.get_setting("outbound_paused")) == "1"
        if self._paused:
            logger.warning("Исходящие сообщения заблокированы (outbound_paused=1) — только мониторинг")

        await self.anti_ban.init_warmup()
        await self._load_monitored_chats()
        self._me_id = me.id
        self._register_handlers()
        self._running = True
        if FORUM_OUTREACH_ENABLED and self._harvest_chat_ids:
            logger.info(f"Forum outreach: сбор активных из {len(self._harvest_chat_ids)} чатов")

    async def stop(self):
        self._running = False
        await self.client.disconnect()

    async def _load_monitored_chats(self):
        """Загружает список отслеживаемых чатов из БД."""
        chats = await db.get_monitored_chats()
        self._monitored_chat_ids = {c["chat_id"] for c in chats}
        self._harvest_chat_ids = await db.get_harvest_chat_ids()
        logger.info(f"Отслеживается чатов: {len(self._monitored_chat_ids)}, "
                    f"сбор участников: {len(self._harvest_chat_ids)}")

    async def reload_chats(self):
        """Перезагрузка списка чатов (вызывается при добавлении нового чата)."""
        await self._load_monitored_chats()

    def _register_handlers(self):
        """Регистрация обработчиков событий Telethon."""

        @self.client.on(events.NewMessage())
        async def on_new_message(event):
            """Обработка нового сообщения в чате."""
            chat_id = event.chat_id
            if chat_id not in self._monitored_chat_ids and chat_id not in self._harvest_chat_ids:
                return

            # Игнорируем свои собственные сообщения (кэшированный ID)
            if event.sender_id == self._me_id:
                return

            # Запускаем обработку без блокировки следующих сообщений
            asyncio.create_task(self._process_chat_message_safe(event))

        @self.client.on(events.NewMessage(incoming=True))
        async def on_private_message(event):
            """Обработка входящих личных сообщений (ответы клиентов в диалогах)."""
            if event.is_private:
                if event.sender_id == self._me_id:
                    return
                asyncio.create_task(self._process_private_message(event))

        @self.client.on(events.NewMessage(outgoing=True))
        async def on_outgoing_message(event):
            """Перехват исходящих ЛС — если владелец пишет сам, бот останавливает диалог."""
            if not event.is_private:
                return
            # Это сообщение от нас (владельца) — проверяем, есть ли активный диалог
            asyncio.create_task(self._handle_owner_intervention(event))

    async def _process_chat_message_safe(self, event):
        """Обёртка над _process_chat_message с обработкой ошибок и семафором."""
        try:
            await self._process_chat_message(event)
        except Exception as e:
            logger.error(f"Ошибка обработки сообщения из чата {event.chat_id}: {e}", exc_info=True)

    async def _handle_owner_intervention(self, event):
        """Владелец сам написал в ЛС клиенту — останавливаем диалог, передаём владельцу.

        Если владелец пишет сообщение в ЛС клиенту, с которым бот ведёт диалог,
        бот немедленно прекращает общение с этим клиентом и переводит диалог
        в стадию HANDOFF — чтобы бот больше не отвечал автоматически.
        """
        try:
            recipient_id = event.chat_id
            text = event.message.text or ""

            # Ищем активный диалог с этим получателем
            dialog = await db.get_dialog_by_sender(recipient_id)
            if not dialog:
                return  # нет диалога — не наше дело

            # Если диалог уже завершён — ничего не делаем
            if dialog["stage"] in ("ENDED", "CLOSED", "HANDOFF"):
                return

            # Логируем сообщение владельца
            await db.log_message(0, recipient_id, dialog["sender_name"],
                                 f"[ВЛАДЕЛЕЦ] {text}", event.message.id, "outgoing")

            # Переводим диалог в HANDOFF — бот больше не будет отвечать
            await db.update_dialog_stage(dialog["id"], "HANDOFF")
            await db.update_dialog_sales_data(dialog["id"], handoff_notified=1)

            logger.info(
                f"ВЛАДЕЛЕЦ вмешался в диалог #{dialog['id']} с {dialog['sender_name']} "
                f"— диалог переведён в HANDOFF, бот больше не отвечает"
            )

            # Уведомляем владельца через бот-нотификатор
            if self.notification_callback:
                await self.notification_callback(
                    lead_id=dialog["lead_id"], chat_name="ЛС",
                    sender_name=dialog["sender_name"],
                    sender_username=dialog.get("sender_username"),
                    message_text=f"[Ваше сообщение клиенту]: {text}",
                    category="HUMAN_REQUIRED",
                    sender_id=dialog["sender_id"], dialog_id=dialog["id"],
                    reason="Вы сами написали клиенту — бот остановлен для этого диалога",
                )

            # Отменяем pending debounce если есть
            old_timer = self._dialog_timers.pop(recipient_id, None)
            if old_timer:
                old_timer.cancel()
            self._pending_messages.pop(recipient_id, None)

        except Exception as e:
            logger.error(f"Ошибка обработки вмешательства владельца: {e}", exc_info=True)

    async def _process_chat_message(self, event):
        """Обработка сообщения из отслеживаемого чата."""
        message = event.message
        text = message.text or ""
        sender = await event.get_sender()
        meta = _sender_metadata(sender, text)

        # Логируем сообщение (sender может быть None — анонимный админ/удалённый;
        # в БД пишем 0, т.к. колонка NOT NULL в старой схеме)
        sender_name = meta["sender_name"]
        await db.log_message(event.chat_id, meta["sender_id"] or 0, sender_name, text,
                             message.id, "incoming")

        # Дедупликация спамеров: если тот же отправитель уже писал это же
        # сообщение в последние 24ч — пропускаем AI-классификацию (экономим квоту)
        sender_is_bot = meta["sender_type"] == "bot"
        normalized = re.sub(r"\s+", " ", text.strip().lower())
        text_hash = hashlib.sha256(normalized.encode()).hexdigest()
        spam_key = (meta["sender_id"] or event.chat_id, text_hash)
        now = time.time()
        # Чистим устаревшие записи раз в 100 сообщений
        if len(self._spam_cache) > 500:
            self._spam_cache = {k: v for k, v in self._spam_cache.items()
                                if now - v < self._spam_cache_ttl}
        if spam_key in self._spam_cache and now - self._spam_cache[spam_key] < self._spam_cache_ttl:
            logger.info(f"Дубликат сообщения от {sender_name} (спам-кэш) — пропускаем AI")
            await db.update_message_verdict(event.chat_id, message.id, "spam_duplicate")
            return
        self._spam_cache[spam_key] = now

        # Сбор активных участников для приглашений на форум (без AI-квалификации)
        if FORUM_OUTREACH_ENABLED and event.chat_id in self._harvest_chat_ids:
            await self._harvest_candidate(event, meta, text)

        # Лид-пайплайн работает только по чатам с флагом мониторинга
        if event.chat_id not in self._monitored_chat_ids:
            return

        # Квалификация лида — единственный финальный фильтр (семафор на AI-вызовы)
        async with self._lead_semaphore:
            result = await qualify_lead(text, message.date, sender_is_bot,
                                        include_indirect=COLD_LEAD_ENABLED)
        if not result.is_lead:
            if result.reason == 'review_unavailable':
                self._spam_cache.pop(spam_key, None)
            await db.update_message_verdict(event.chat_id, message.id, result.reason)
            return
        if result.category == 'WARM_COLD':
            await db.update_message_verdict(event.chat_id, message.id, 'cold_WARM_COLD')
            await self._process_cold_lead(event, result, text)
            return

        # Лид найден — проверяем дубликат (тот же отправитель уже дал лид недавно)
        if meta["sender_id"] and await db.has_recent_lead_from_sender(meta["sender_id"], hours=24):
            logger.info(f"Дубликат лида от {sender_name} (уже есть за 24ч) — пропускаем")
            await db.update_message_verdict(event.chat_id, message.id, "duplicate_lead")
            return

        chat_entity = await event.get_chat()
        chat_name = getattr(chat_entity, "title", str(event.chat_id))
        sender_username = meta["sender_username"]
        source_url = _source_url(chat_entity, event.chat_id, message.id)
        lead_meta = {
            "source_message_id": message.id,
            "source_url": source_url,
            "sender_type": meta["sender_type"],
            "contact_username": meta["contact_username"],
            "contact_source": meta["contact_source"],
            "service": result.service,
            "evidence": result.evidence,
        }

        lead_id = await db.add_lead(
            chat_id=event.chat_id,
            chat_name=chat_name,
            sender_id=meta["sender_id"] or 0,
            sender_name=sender_name,
            sender_username=sender_username,
            message_text=text,
            category=result.category,
            task=result.task,
            budget=result.budget,
            deadline=result.deadline,
            market_price=result.market_price,
            market_deadline=result.market_deadline,
            lead_score=result.lead_score,
            **lead_meta,
        )

        logger.info(f"ЛИД #{lead_id} [{result.category}] от {sender_name} в чате {chat_name}")
        await db.update_message_verdict(event.chat_id, message.id, f"lead_{result.category}")

        # Уведомление владельцу через бота
        if self.notification_callback:
            await self.notification_callback(
                lead_id=lead_id,
                chat_name=chat_name,
                sender_name=sender_name,
                sender_username=sender_username,
                message_text=text,
                category=result.category,
                task=result.task,
                budget=result.budget,
                deadline=result.deadline,
                market_price=result.market_price,
                market_deadline=result.market_deadline,
                lead_score=result.lead_score,
                sender_id=meta["sender_id"],
                **lead_meta,
            )

    async def _harvest_candidate(self, event, meta: dict, text: str):
        """Сохраняет активного участника harvest-чата как кандидата на приглашение."""
        try:
            sender_id = meta.get("sender_id")
            if not sender_id or meta.get("sender_type") != "user":
                return
            if sender_id == self._me_id:
                return
            if len((text or "").strip()) < HARVEST_MIN_MSG_LEN:
                return  # отсев команд, стикеров, односложных реплик
            chat_entity = await event.get_chat()
            chat_name = getattr(chat_entity, "title", str(event.chat_id))
            await db.upsert_outreach_target(
                chat_id=event.chat_id, chat_name=chat_name,
                sender_id=sender_id, sender_name=meta["sender_name"],
                sender_username=meta.get("sender_username"),
                message_text=text,
            )
        except Exception as e:
            logger.error(f"Ошибка сбора кандидата на приглашение: {e}")

    @staticmethod
    def _looks_like_business_name(name: str) -> bool:
        """Имя похоже на магазин/сервис/автоответчик, а не на человека."""
        if not name:
            return False
        biz_words = (r"delivery|deli|shop|store|market|opt|sales?|service|"
                     r"доставка|магазин|сервис|услуги|опт\b|заказ|прайс|шоп|"
                     r"маркет|салон|офис|компания")
        if re.search(rf"(?i)\b({biz_words})", name):
            return True
        # Полностью заглавная латиница из 2+ слов — типичный бренд
        letters = re.sub(r"[^A-Za-zА-Яа-я ]", "", name).strip()
        if " " in letters and re.fullmatch(r"[A-Z ]{4,}", letters):
            return True
        return False

    async def send_forum_invite(self, target: dict) -> str:
        """Отправка приглашения на форум активному участнику.
        Возвращает статус: 'sent' | 'skipped' | 'limited' | 'failed'.
        """
        import ai_engine

        if self._paused:
            return "limited"
        sender_id = target["sender_id"]
        if not sender_id or sender_id == self._me_id:
            return "skipped"
        if await db.has_seen_user(sender_id):
            return "skipped"
        if not await self.anti_ban.can_send_cold():
            return "limited"

        # Имя-бренд/магазин — это не женщина-кандидат, а автоответчик
        sender_name = target.get("sender_name") or ""
        if self._looks_like_business_name(sender_name):
            logger.info(f"Кандидат {sender_name} — имя-бренд/магазин, пропуск")
            return "skipped"

        # Предварительная проверка пола по имени и грамматике сообщений
        gender = await ai_engine.detect_person_gender(
            target.get("sender_name", ""),
            target.get("sample_texts") or target.get("last_message_text") or "")
        if gender == "male":
            logger.info(f"Кандидат {target.get('sender_name')} определён как мужчина — пропуск")
            return "skipped_male"

        # Предварительная проверка города по сообщениям
        city_verdict = await ai_engine.detect_person_city(
            target.get("sample_texts") or target.get("last_message_text") or "")
        if city_verdict == "other":
            logger.info(f"Кандидат {target.get('sender_name')} явно не из Краснодара — пропуск")
            return "skipped_city"

        # Первое касание — опенер «чем занимаетесь?» (A/B по весам конверсии)
        import random
        chosen_variant = 0
        if FORUM_OPENER_VARIANTS:
            weights = await db.ab_get_weights(len(FORUM_OPENER_VARIANTS))
            chosen_variant = random.choices(
                range(len(FORUM_OPENER_VARIANTS)), weights=weights, k=1)[0]
            template = FORUM_OPENER_VARIANTS[chosen_variant]
            text = await self._fill_invite_template(template, target)
        else:
            text = None
        if not text:
            return "failed"

        await self._forum_cooldown()
        success = await self._send_message(
            sender_id, text, via_chat_id=target.get("chat_id"))
        if not success:
            return "failed"

        await db.mark_user_seen(sender_id)
        await self.anti_ban.record_cold_action(
            sender_id, target.get("sender_name", ""), text)
        dialog_id = await db.create_dialog(
            lead_id=0, sender_id=sender_id,
            sender_name=target.get("sender_name", ""),
            sender_username=target.get("sender_username"),
            lead_source="forum_outreach",
        )
        messages = [
            {"role": "system", "content": "Приглашение на форум отправлено организатором."},
            {"role": "assistant", "content": text},
        ]
        await db.update_dialog_messages(dialog_id, json.dumps(messages, ensure_ascii=False))
        await db.update_dialog_stage(dialog_id, "FORUM_OPENER")
        await db.set_dialog_ab_variant(dialog_id, chosen_variant)
        await db.ab_record_sent(chosen_variant)
        logger.info(f"Опенер отправлен {target.get('sender_name')} "
                    f"({sender_id}), вариант #{chosen_variant}")
        return "sent"

    async def _fill_invite_template(self, template: str, target: dict) -> str | None:
        """Подстановка плейсхолдеров в шаблон приглашения."""
        import ai_engine
        name_raw = (target.get("sender_name") or "").strip()
        first_name = name_raw.split()[0] if name_raw else ""
        if not (first_name and first_name.isalpha()):
            # Ник декоративный/латинский — AI ищет настоящее имя в нике и сообщениях
            ai_name = await ai_engine.extract_person_name(
                name_raw, target.get("last_message_text") or "")
            if ai_name:
                first_name = ai_name
        name = f", {first_name}" if first_name and first_name.isalpha() else ""
        greeting = f"Здравствуйте, {first_name}" if first_name and first_name.isalpha() else "Здравствуйте"

        topic = "актуальную для вас тему"
        if "{topic}" in template:
            extracted = await ai_engine.extract_forum_topic(
                target.get("last_message_text") or "")
            if extracted:
                topic = extracted

        try:
            return template.format(
                name=name, greeting=greeting, topic=topic,
                chat=target.get("chat_name") or "тематическом чате",
                date=FORUM_DATE, time=FORUM_TIME, venue=FORUM_VENUE,
                promo=FORUM_PROMO_CODE, discount=FORUM_DISCOUNT,
                url=FORUM_TICKET_URL, instagram=FORUM_INSTAGRAM,
            )
        except (KeyError, IndexError) as e:
            logger.error(f"Ошибка подстановки в шаблон приглашения: {e}")
            return None

    async def _process_cold_lead(self, event, cold_result: 'LeadResult', text: str):
        """Сохраняет холодного лид и уведомляет владельца."""
        sender = await event.get_sender()
        meta = _sender_metadata(sender, text)
        chat_entity = await event.get_chat()
        chat_name = getattr(chat_entity, "title", str(event.chat_id))
        sender_name = meta["sender_name"]
        sender_username = meta["sender_username"]
        lead_meta = {
            "source_message_id": event.message.id,
            "source_url": _source_url(chat_entity, event.chat_id, event.message.id),
            "sender_type": meta["sender_type"],
            "contact_username": meta["contact_username"],
            "contact_source": meta["contact_source"],
            "service": cold_result.service,
            "evidence": cold_result.evidence,
        }

        lead_id = await db.add_cold_lead(
            chat_id=event.chat_id,
            chat_name=chat_name,
            sender_id=meta["sender_id"] or 0,
            sender_name=sender_name,
            sender_username=sender_username,
            message_text=text,
            category=cold_result.category,
            business_type=cold_result.business_type,
            pain=cold_result.pain,
            hook=cold_result.hook,
            market_price=cold_result.market_price,
            market_deadline=cold_result.market_deadline,
            lead_score=cold_result.lead_score,
            **lead_meta,
        )
        logger.info(f"ХОЛОДНЫЙ ЛИД #{lead_id} [{cold_result.category}] от {sender_name}")

        if self.notification_callback:
            await self.notification_callback(
                lead_id=lead_id,
                chat_name=chat_name,
                sender_name=sender_name,
                sender_username=sender_username,
                message_text=text,
                category=cold_result.category,
                business_type=cold_result.business_type,
                pain=cold_result.pain,
                hook=cold_result.hook,
                market_price=cold_result.market_price,
                market_deadline=cold_result.market_deadline,
                lead_score=cold_result.lead_score,
                sender_id=meta["sender_id"],
                **lead_meta,
            )

    async def _process_private_message(self, event):
        """Обработка входящего ЛС — с дебаунсом для серии сообщений.

        Клиент может написать несколько сообщений подряд (как живой человек).
        Ждём self._dialog_debounce_seconds после последнего сообщения,
        затем объединяем все накопленные сообщения в один текст и отвечаем один раз.
        """
        sender_id = event.sender_id
        text = event.message.text or ""
        if not text.strip():
            return

        # Кэшируем input-entity отправителя — у UpdateShortMessage нет
        # access_hash, иначе ответ по голому id не резолвится
        if sender_id not in self._input_peers:
            try:
                peer = await event.get_input_sender()
                if peer is not None:
                    self._input_peers[sender_id] = peer
            except Exception:
                pass

        # Логируем сразу, но обработку откладываем
        dialog = await db.get_dialog_by_sender(sender_id)
        if not dialog:
            logger.debug(f"ЛС от {sender_id} без активного диалога: {text[:50]}...")
            return

        await db.log_message(0, sender_id, dialog["sender_name"], text,
                             event.message.id, "incoming")

        # Если диалог завершён — сразу владельцу, без дебаунса
        if dialog["stage"] in ("ENDED", "CLOSED", "HANDOFF") or dialog.get("do_not_contact"):
            logger.info(f"ЛС для завершённого диалога #{dialog['id']} оставлено владельцу")
            if dialog["stage"] == "HANDOFF" and self.notification_callback:
                await self.notification_callback(
                    lead_id=dialog["lead_id"], chat_name="ЛС",
                    sender_name=dialog["sender_name"], sender_username=dialog.get("sender_username"),
                    message_text=text, category="HUMAN_REQUIRED",
                    sender_id=dialog["sender_id"], dialog_id=dialog["id"],
                    reason="Клиент написал после передачи диалога",
                )
            return

        # Исходящие заблокированы — сообщение залогировано, автоответ не генерируем
        if self._paused:
            logger.info(f"ЛС от {sender_id} залогировано, автоответ пропущен (исходящие заблокированы)")
            return

        # Накапливаем сообщения и перезапускаем таймер
        self._pending_messages.setdefault(sender_id, []).append(text)

        # Отменяем предыдущий таймер если есть
        old_timer = self._dialog_timers.get(sender_id)
        if old_timer:
            old_timer.cancel()

        # Запускаем новый таймер — через N секунд вызовем _flush_pending_messages
        loop = asyncio.get_event_loop()
        timer = loop.call_later(
            self._dialog_debounce_seconds,
            lambda: asyncio.ensure_future(self._flush_pending_messages(sender_id))
        )
        self._dialog_timers[sender_id] = timer
        logger.debug(f"Дебаунс: накоплено {len(self._pending_messages[sender_id])} сообщений от {sender_id}, ждём {self._dialog_debounce_seconds}с")

    async def _flush_pending_messages(self, sender_id: int):
        """Объединяет накопленные сообщения и обрабатывает их как одно."""
        # Убираем таймер
        self._dialog_timers.pop(sender_id, None)

        # Достаём накопленные сообщения
        pending = self._pending_messages.pop(sender_id, [])
        if not pending:
            return

        # Объединяем в один текст (как живой человек — несколько фраз подряд)
        combined_text = "\n".join(pending)

        lock = self._dialog_locks.setdefault(sender_id, asyncio.Lock())
        async with lock:
            dialog = await db.get_dialog_by_sender(sender_id)
            if not dialog:
                return
            if dialog["stage"] in ("ENDED", "CLOSED", "HANDOFF") or dialog.get("do_not_contact"):
                return

            ab_variant = dialog.get("ab_variant", 0)
            if ab_variant is not None and ab_variant >= 0 and dialog["stage"] in ("INITIATING", "QUALIFYING"):
                messages = json.loads(dialog.get("ai_messages_json") or "[]")
                assistant_count = sum(1 for m in messages if m.get("role") == "assistant")
                if assistant_count <= 1:
                    await db.ab_record_reply(ab_variant)
                    logger.info(f"A/B: клиент ответил на вариант #{ab_variant} (диалог #{dialog['id']})")

            logger.info(f"Обработка серии из {len(pending)} сообщений от {sender_id}: {combined_text[:80]}...")
            await self._continue_dialog(dialog, combined_text)

    async def _handle_task_received_reply(self, dialog: dict, event, text: str):
        """Обработка ответа клиента на стадии TASK_RECEIVED (уточнение задачи)."""
        # Логируем
        await db.log_message(0, dialog["sender_id"], dialog["sender_name"],
                             text, event.message.id, "incoming")

        # Обновляем историю сообщений
        messages = json.loads(dialog.get("ai_messages_json") or "[]")
        messages.append({"role": "user", "content": text})

        # Уведомляем владельца о дополнении задачи
        if self.notification_callback:
            await self.notification_callback(
                lead_id=dialog["lead_id"],
                chat_name="ЛС",
                sender_name=dialog["sender_name"],
                sender_username=dialog.get("sender_username"),
                message_text=text,
                category="TASK_UPDATE",
                sender_id=dialog["sender_id"],
                dialog_id=dialog["id"],
            )

    async def _continue_dialog(self, dialog: dict, client_message: str):
        """Продолжение диалога структурированным контуром продаж."""
        import sales_engine

        messages = json.loads(dialog.get("ai_messages_json") or "[]")
        messages.append({"role": "user", "content": client_message})
        await db.update_dialog_messages(dialog["id"], json.dumps(messages, ensure_ascii=False))

        # Форумные диалоги — отдельный лёгкий контур, без sales engine
        if dialog.get("lead_source") == "forum_outreach":
            await self._continue_forum_dialog(dialog, messages, client_message)
            return

        try:
            analysis = await sales_engine.analyze_turn(dialog, messages, client_message)
        except Exception as e:
            logger.error(f"Ошибка анализа диалога #{dialog['id']}: {e}", exc_info=True)
            if self.notification_callback:
                await self.notification_callback(
                    lead_id=dialog["lead_id"], chat_name="ЛС",
                    sender_name=dialog["sender_name"], sender_username=dialog.get("sender_username"),
                    message_text=client_message, category="HUMAN_REQUIRED",
                    sender_id=dialog["sender_id"], dialog_id=dialog["id"],
                    reason="AI не смог надёжно проанализировать сообщение",
                )
            return

        updates, quote = sales_engine.merge_state(dialog, analysis)
        await db.update_dialog_sales_data(dialog["id"], **updates)
        dialog = {**dialog, **updates}
        services = json.loads(dialog.get("service_codes_json") or "[]")
        requirements = json.loads(dialog.get("requirements_json") or "[]")
        intent = analysis["intent"]

        if intent == "DO_NOT_CONTACT":
            await db.update_dialog_sales_data(dialog["id"], do_not_contact=1)
            await db.update_dialog_stage(dialog["id"], "ENDED")
            logger.info(f"Диалог #{dialog['id']} закрыт: запрет дальнейшего контакта")
            return

        if intent == "HARD_REFUSAL":
            await self._send_and_record(
                dialog, messages, "Понял, спасибо за прямой ответ. Больше не буду отвлекать.", "ENDED"
            )
            return

        if not sales_engine.is_supported_country(dialog.get("country")):
            await self._send_and_record(
                dialog, messages,
                "Спасибо, что уточнили. К сожалению, сейчас мы не работаем с вашим регионом. Всего доброго.",
                "ENDED",
            )
            return

        interested = (
            dialog.get("interest_level") == "HOT" or analysis.get("asks_price")
            or analysis.get("ready_to_work") or intent in {"ASKS_PAYMENT", "READY_TO_WORK"}
        )
        if interested and not dialog.get("country"):
            await db.update_dialog_sales_data(dialog["id"], country_asked=1)
            reply = await sales_engine.generate_reply(
                dialog, messages,
                "Клиент проявил предметный интерес. Уточни только страну, из которой он обращается, "
                "объяснив одним коротким оборотом, что это нужно для выбора доступных сервисов.",
            )
            if reply:
                await self._send_and_record(dialog, messages, reply, "QUALIFYING")
            else:
                await self._notify_human_required(dialog, client_message, "Не удалось сформировать вопрос о стране")
            return

        unresolved = quote.get("needs_owner", []) + quote.get("unknown", [])
        needs_price = analysis.get("asks_price") or analysis.get("task_clear") or analysis.get("ready_to_work")
        if unresolved and needs_price and not dialog.get("price"):
            description = ", ".join(unresolved)
            recommendation = await sales_engine.recommend_price(description, requirements)
            await db.update_dialog_sales_data(dialog["id"], awaiting_owner_price=1)
            await db.update_dialog_stage(dialog["id"], "AWAITING_OWNER_PRICE")
            brief = sales_engine.build_brief(dialog, services, requirements, quote)
            if self.notification_callback:
                await self.notification_callback(
                    lead_id=dialog["lead_id"], chat_name="ЛС",
                    sender_name=dialog["sender_name"], sender_username=dialog.get("sender_username"),
                    message_text=brief, category="PRICE_REQUIRED",
                    sender_id=dialog["sender_id"], dialog_id=dialog["id"],
                    recommendation=recommendation, service_description=description,
                    country=dialog.get("country"),
                )
            reply = await sales_engine.generate_reply(
                dialog, messages,
                "Цена этой услуги отсутствует в прайсе. Скажи, что уточнишь стоимость по описанному "
                "объёму и вернёшься с точной цифрой. Не называй цену и не задавай новый вопрос.",
            )
            if reply:
                await self._send_and_record(dialog, messages, reply, "AWAITING_OWNER_PRICE")
            else:
                await self._notify_human_required(dialog, client_message, "Не удалось сообщить об уточнении цены")
            return

        if dialog.get("stage") == "AWAITING_OWNER_PRICE" and not dialog.get("price"):
            return

        if ((analysis.get("asks_price") or (analysis.get("task_clear") and interested))
                and quote.get("complete") and not dialog.get("price")):
            price = sales_engine.format_quote(quote)
            await db.update_dialog_price(dialog["id"], price)
            dialog["price"] = price
            reply = await sales_engine.generate_reply(
                dialog, messages,
                f"Назови рассчитанную по прайсу стоимость {price}. Коротко уточни, что расчёт относится "
                "к уже описанному объёму, и задай один вопрос, подходит ли такой состав работ.",
            )
            if reply:
                await self._send_and_record(dialog, messages, reply, "NEGOTIATING")
            else:
                await self._notify_human_required(dialog, client_message, "Не удалось сообщить рассчитанную цену")
            return

        ready = bool(dialog.get("ready_to_work") or analysis.get("ready_to_work") or intent == "READY_TO_WORK")
        if ready and not dialog.get("company"):
            reply = await sales_engine.generate_reply(
                dialog, messages,
                "Клиент готов работать. Уточни, на кого зафиксировать договорённости: на него лично "
                "или на компанию; если компания — попроси название. Один вопрос.",
            )
            if reply:
                await self._send_and_record(dialog, messages, reply, "QUALIFYING")
            else:
                await self._notify_human_required(dialog, client_message, "Не удалось уточнить данные клиента")
            return

        if ready and services and requirements and dialog.get("country"):
            brief = await sales_engine.compose_brief(dialog, services, requirements, quote)
            await db.update_dialog_sales_data(
                dialog["id"], brief_text=brief, handoff_notified=1, awaiting_owner_price=0
            )
            await db.update_dialog_stage(dialog["id"], "HANDOFF")
            reply = await sales_engine.generate_reply(
                dialog, messages,
                "Подтверди, что основные вводные зафиксированы и дальше подключится разработчик. "
                "Если клиент спрашивал об оплате, скажи только, что доступна криптовалюта через Telegram. "
                "Не задавай вопросов и не обещай сроки.",
            )
            if reply:
                await self._send_and_record(dialog, messages, reply, "HANDOFF")
            if self.notification_callback:
                await self.notification_callback(
                    lead_id=dialog["lead_id"], chat_name="ЛС",
                    sender_name=dialog["sender_name"], sender_username=dialog.get("sender_username"),
                    message_text=brief, category="DEAL_READY",
                    sender_id=dialog["sender_id"], dialog_id=dialog["id"],
                    country=dialog.get("country"), brief=brief,
                )
            return

        objection_directives = {
            "OBJECTION_PRICE": (
                "Спокойно признай, что цена важна. Не оправдывайся и не давай скидку. "
                "Задай один вопрос: с каким бюджетом или вариантом клиент сравнивает."
            ),
            "OBJECTION_TRUST": (
                "Не спорь с сомнением. Уточни одним вопросом, какое подтверждение надёжности "
                "важнее клиенту: этапы, договорённости, демонстрация или связь с разработчиком."
            ),
            "OBJECTION_TIMING": (
                "Прими ограничение по срокам и уточни, к какой дате результат действительно нужен. "
                "Не создавай искусственную срочность."
            ),
            "THINKING": (
                "Не дави. Спроси одним коротким вопросом, какой момент клиент хочет обдумать, "
                "и предложи вернуться к разговору в удобное время."
            ),
        }
        directive = objection_directives.get(intent) or analysis.get("reply_goal") or (
            "Ответь по существу и задай один короткий вопрос, который уточняет задачу клиента. "
            "Не продавай раньше времени и не называй цену без расчёта."
        )
        if analysis.get("asks_payment"):
            directive = (
                "Скажи, что доступна оплата криптовалютой через Telegram и детали предоставит "
                "разработчик после согласования задачи. Затем задай один вопрос по недостающим требованиям."
            )
        reply = await sales_engine.generate_reply(dialog, messages, directive)
        if reply:
            next_stage = "WAITING_CLIENT" if intent in {"OBJECTION_TIMING", "THINKING"} else "QUALIFYING"
            await self._send_and_record(dialog, messages, reply, next_stage)
        else:
            await self._notify_human_required(dialog, client_message, "Не удалось сформировать безопасный ответ")

    async def _continue_forum_dialog(self, dialog: dict, messages: list[dict],
                                     client_message: str):
        """Диалог с приглашённой: стоп-слова/отказы — без AI, всё остальное —
        AI отвечает по смыслу сообщения и контексту переписки (без сценария)."""
        import ai_engine

        # Диалог закрыт (отказ/стоп-слова/закрытие) — молчим, не отвечаем
        if dialog.get("stage") == "ENDED":
            return

        # Автоответчик/бот-магазин: одно и то же сообщение приходит
        # повторно — закрываем без ответа, чтобы не кормить AI впустую
        normalized = re.sub(r"\s+", " ", client_message.strip().lower())
        if len(normalized) >= 15:
            repeats = sum(
                1 for m in messages
                if m.get("role") == "user"
                and re.sub(r"\s+", " ", str(m.get("content") or "").strip().lower())
                == normalized)
            if repeats >= 2:
                await db.update_dialog_sales_data(dialog["id"], do_not_contact=1)
                await db.update_dialog_stage(dialog["id"], "ENDED")
                logger.info(f"Форумный диалог #{dialog['id']}: повторяющиеся "
                            f"сообщения — похоже на автоответчик, завершён")
                return

        if re.search(r"(?i)(не\s*(надо|нужно|интересно|пишите|беспоко|звоните)|"
                     r"отстань|спам|стоп|отпишись|хватит)", client_message):
            await db.update_dialog_sales_data(dialog["id"], do_not_contact=1)
            await db.update_dialog_stage(dialog["id"], "ENDED")
            logger.info(f"Форумный диалог #{dialog['id']} закрыт по просьбе клиента")
            return

        # Отказ: первый — AI отрабатывает возражение, повторный — завершаем
        refusal_re = re.compile(
            r"(?i)(не\s*(пойду|приду|буду|хочу|смогу|получится|интересно)|"
            r"передумал|не\s*смогу\s*прийти|в\s*другой\s*раз|"
            r"нет\s*,?\s*спасибо|не\s*актуально)")
        farewell_re = re.compile(
            r"(?i)\b(до\s*свидания|прощай|всего\s*доброго|всего\s*хорошего)\b")
        if refusal_re.search(client_message):
            refusals = sum(
                1 for m in messages
                if m.get("role") == "user"
                and refusal_re.search(str(m.get("content") or "")))
            if refusals >= 2 or farewell_re.search(client_message):
                decline = FORUM_DECLINE_TEXT.format(promo=FORUM_PROMO_CODE)
                await self._send_and_record(dialog, messages, decline, "ENDED")
                await db.update_dialog_sales_data(dialog["id"], do_not_contact=1)
                await db.update_dialog_stage(dialog["id"], "ENDED")
                logger.info(f"Форумный диалог #{dialog['id']}: повторный отказ — завершён")
                return
            # Первый отказ — деликатная попытка отработать возражение через AI
            messages.append({
                "role": "system",
                "content": "Собеседница отказалась — это первый отказ. Сделай "
                           "одну деликатную попытку отработать возражение: "
                           "признай его, уточни причину вопросом и предложи "
                           "решение. Не дави и не прощайся."})
        elif farewell_re.search(client_message):
            decline = FORUM_DECLINE_TEXT.format(promo=FORUM_PROMO_CODE)
            await self._send_and_record(dialog, messages, decline, "ENDED")
            await db.update_dialog_sales_data(dialog["id"], do_not_contact=1)
            await db.update_dialog_stage(dialog["id"], "ENDED")
            logger.info(f"Форумный диалог #{dialog['id']}: прощание — завершён")
            return

        # Готова купить — шлём ссылку+промокод+атмосферу и уведомляем владельца.
        # Явный интент покупки: «готова» только жен. род с границей слова
        # («вы готовы к оплате» не считается), «давай» — только со ссылкой/билетом.
        if re.search(
                r"(?i)\b(готова\b|куплю|оплачу|оплатить|купить|беру\s+билет|"
                r"запиши(?:те|сь|усь)|зарегистриру\w+|приду\b|буду\s+участв|"
                r"иду\s+на\s+форум|хочу\s+(?:билет|попасть|прийти|купить)|"
                r"давай(?:те)?\s+(?:ссылку|билет|промокод|промо|регистраци\w+))",
                client_message):
            closing = FORUM_CLOSING_TEXT.format(
                url=FORUM_TICKET_URL, promo=FORUM_PROMO_CODE,
                discount=FORUM_DISCOUNT, instagram=FORUM_INSTAGRAM,
                program=FORUM_PROGRAM)
            await self._send_and_record(dialog, messages, closing, "FORUM_CHAT")
            await db.update_dialog_sales_data(dialog["id"], ready_to_work=1,
                                              interest_level="HOT")
            await self._notify_human_required(
                dialog, client_message,
                "Клиент готов купить билет на форум — отправлена ссылка и промокод")
            # Пуш заказчикам через отчётный бот (если настроен)
            try:
                import report_bot
                rbot = report_bot.get_bot()
                if rbot:
                    await report_bot.notify_hot_lead(rbot, dialog)
            except Exception as e:
                logger.warning(f"Отчётный бот: пуш о HOT-лиде не отправлен: {e}")
            return

        # AI отвечает по смыслу сообщения: интенты пишутся в заметку для контекста
        intents = self._forum_detect_intents(client_message)
        # Память диалога: фиксируем последний факт и интенты в истории
        messages.append({
            "role": "system",
            "content": f"last_user_fact={client_message[:150]}; intents={','.join(intents) or 'none'}"
        })
        reply, unresolved = await self._forum_compose_reply(
            dialog, messages, client_message, intents)
        await self._send_and_record(dialog, messages, reply, "FORUM_CHAT")
        if unresolved:
            await self._notify_human_required(
                dialog, client_message, "Нестандартный вопрос в форумном диалоге")

    def _forum_detect_intents(self, text: str) -> list[str]:
        """Все интенты сообщения (мульти-интент), в порядке приоритета шаблонов."""
        patterns = [
            ("price", r"(?i)(сколько\s*(стоит|это)|цена|стоимость|сколько\s*денег|прайс)"),
            ("speakers", r"(?i)(спикер|кто[^.?!]{0,25}(выступ|рассказ|будет)|ведущ)"),
            ("program", r"(?i)(программ|что\s*будет|расписание|расписан)"),
            ("venue", r"(?i)(где\s*(будет|проходит|находится)|где[?!]?\s*$|адрес|когда|"
                       r"во\s*сколько|время)"),
            ("expensive", r"(?i)(дорого|дорог|не\s*по\s*карману|бюджет)"),
            ("think", r"(?i)(подума|позже|потом|не\s*сейчас|вернусь\s*позже)"),
            ("friend", r"(?i)(с\s+(другом|подруг|мужем|знаком|коллег)|вдвоём|вдвоем|"
                       r"вместе|двоих|не\s*одна|не\s*одной)"),
        ]
        return [name for name, pat in patterns if re.search(pat, text)]

    def _forum_intent_text(self, intent: str) -> str | None:
        """Шаблонный ответ по одному интенту с подстановкой фактов форума.
        Возвращает None, если шаблоны отключены (чистый AI-режим)."""
        tpl = FORUM_REPLY_TEMPLATES.get(intent)
        if not tpl:
            return None
        return tpl.format(
            url=FORUM_TICKET_URL, promo=FORUM_PROMO_CODE, discount=FORUM_DISCOUNT,
            date=FORUM_DATE, time=FORUM_TIME, venue=FORUM_VENUE,
            speakers=FORUM_SPEAKERS, program=FORUM_PROGRAM,
            instagram=FORUM_INSTAGRAM)

    async def _forum_compose_reply(self, dialog: dict, messages: list[dict],
                                   client_message: str,
                                   intents: list[str]) -> tuple[str, bool]:
        """AI отвечает на все сообщения по фактам форума и контексту диалога.
        Шаблоны по интентам — только fallback при недоступном AI.
        Возвращает (текст, unresolved)."""
        import ai_engine
        ai_reply = await ai_engine.generate_forum_reply(
            messages, sender_name=dialog.get("sender_name") or "")
        if ai_reply:
            # Уведомляем владельца на нестандартный вопрос — для контроля качества
            return ai_reply, not intents
        # Шаблоны отключены (чистый AI-тест): AI недоступен —
        # уточнение + уведомление владельцу
        return ("Уточните, пожалуйста, что именно интересует — "
                "цена, программа, спикеры или место проведения?"), True

    async def wait_for_reply(self, sender_id: int, timeout: int = 600,
                             poll: int = 10) -> str | None:
        """Ждёт реальное входящее сообщение от собеседника (polling get_messages).
        Возвращает текст первого нового входящего сообщения после baseline
        или None по таймауту. Нужен для честного живого теста и потоков,
        где следующий шаг зависит от ответа собеседника."""
        from datetime import timezone
        baseline = datetime.now(timezone.utc)
        deadline = baseline.timestamp() + timeout
        seen: set[int] = set()
        # Фиксируем уже существующие сообщения, чтобы не поймать старые
        try:
            for m in await self.client.get_messages(sender_id, limit=10):
                if m:
                    seen.add(m.id)
        except Exception as e:
            logger.warning(f"wait_for_reply: не удалось прочитать историю: {e}")
        while time.time() < deadline:
            if self._paused:
                return None
            try:
                msgs = await self.client.get_messages(sender_id, limit=5)
            except Exception as e:
                logger.warning(f"wait_for_reply: get_messages упал: {e}")
                await asyncio.sleep(poll)
                continue
            for m in msgs:
                if (m and m.id not in seen and not m.out
                        and m.message and m.message.strip()):
                    return m.message.strip()
            await asyncio.sleep(poll)
        return None

    async def _forum_cooldown(self):
        """Рандомный кулдаун 5с-5мин перед исходящим сообщением форумного контура."""
        import random
        delay = random.uniform(FORUM_MSG_COOLDOWN_MIN, FORUM_MSG_COOLDOWN_MAX)
        logger.info(f"Кулдаун перед сообщением: {delay:.0f}с")
        await asyncio.sleep(delay)

    async def _send_and_record(self, dialog: dict, messages: list[dict], text: str,
                               stage: str) -> bool:
        import random
        if dialog.get("lead_source") == "forum_outreach":
            await self._forum_cooldown()
        # Пауза перед ответом — имитация того, что человек прочитал и подумал
        delay = random.uniform(DIALOG_REPLY_DELAY_MIN, DIALOG_REPLY_DELAY_MAX)
        logger.info(f"Пауза перед ответом {dialog.get('sender_id')}: {delay:.0f}с")
        await asyncio.sleep(delay)
        success = await self._send_message(dialog["sender_id"], text, fast=True)
        if not success:
            if not self._paused:
                await self._notify_human_required(dialog, text, "Сообщение клиенту не отправлено")
            return False
        messages.append({"role": "assistant", "content": text})
        await db.update_dialog_messages(dialog["id"], json.dumps(messages, ensure_ascii=False))
        await db.update_dialog_stage(dialog["id"], stage)
        return True

    async def _notify_human_required(self, dialog: dict, message_text: str, reason: str):
        if self.notification_callback:
            await self.notification_callback(
                lead_id=dialog["lead_id"], chat_name="ЛС",
                sender_name=dialog["sender_name"], sender_username=dialog.get("sender_username"),
                message_text=message_text, category="HUMAN_REQUIRED",
                sender_id=dialog["sender_id"], dialog_id=dialog["id"], reason=reason,
            )

    async def _anti_repeat_note(self) -> str:
        """Формирует блок с последними отправленными первыми сообщениями,
        чтобы AI не повторял одну и ту же структуру/фразы разным людям
        (иначе Telegram может расценить это как спам-рассылку и забанить аккаунт)."""
        recent = await db.get_recent_dialog_openers(limit=8)
        if not recent:
            return ""
        numbered = "\n".join(f"{i+1}. {m}" for i, m in enumerate(recent))
        return (
            "\n\n=== ПОСЛЕДНИЕ ПЕРВЫЕ СООБЩЕНИЯ, ОТПРАВЛЕННЫЕ ДРУГИМ ЛЮДЯМ ===\n"
            f"{numbered}\n\n"
            "ВАЖНО: не повторяй структуру, порядок слов и фразы из этих сообщений. "
            "Сформулируй своё сообщение совершенно иначе — другое начало, другой порядок "
            "мыслей, другие слова. Разные люди должны получать явно РАЗНЫЕ сообщения, "
            "иначе Telegram может посчитать это спам-рассылкой и заблокировать аккаунт."
        )

    async def _send_message(self, sender_id: int, text: str, fast: bool = False,
                            via_chat_id: int | None = None):
        """Отправка сообщения с анти-бан проверками.

        fast=True — пропуск долгой паузы перед действием (для пользовательских команд).
        via_chat_id — чат, через который резолвится юзер, если акк его не «видел»
        (нужно для целей, собранных другими сессиями на другом сервере).
        """
        if self._paused:
            logger.warning("Отправка заблокирована: исходящие выключены владельцем")
            return False
        if not await self.anti_ban.can_send():
            logger.warning("Отправка заблокирована анти-бан модулем")
            return False

        if not fast:
            await self.anti_ban.wait_before_action()

        try:
            entity = self._input_peers.get(sender_id)
            if entity is None:
                try:
                    entity = await self.client.get_input_entity(sender_id)
                except ValueError:
                    entity = None
            if entity is None and via_chat_id:
                entity = await self._resolve_user_via_chats(sender_id, via_chat_id)
            if entity is None:
                logger.error(f"Нет access_hash для {sender_id} — отправка пропущена")
                return False
            # Видимый статус «печатает…» на время имитации набора текста
            async with self.client.action(entity, 'typing'):
                await self.anti_ban.wait_typing(text)
                await self.client.send_message(entity, text)
            await self.anti_ban.record_action()
            await db.log_message(0, sender_id, "", text, 0, "outgoing")
            logger.info(f"Отправлено сообщение для {sender_id}: {text[:60]}...")
            return True
        except (PeerFloodError, FloodWaitError) as e:
            # Аккаунт-вайд ограничение — пробрасываем наверх, чтобы воркер
            # вернул цель в очередь, а не помечал её FAILED навсегда
            logger.error(f"Аккаунт ограничен Telegram ({type(e).__name__}): {e}")
            raise
        except Exception as e:
            logger.error(f"Ошибка отправки сообщения: {e}")
            return False

    async def _resolve_user_via_chats(self, sender_id: int, origin_chat_id):
        """Resuelve access_hash de un usuario que el acct aún no ha «visto».
        Orden: username directo → búsqueda de participantes por nombre →
        filtro from_user → escaneo del historial reciente. Telegram devuelve
        los objetos `user` junto a mensajes/participantes y Telethon los
        cachea en la sesión."""
        target = await db.get_outreach_target_by_sender(sender_id)
        username = (target or {}).get("sender_username")
        if username:
            try:
                return await self.client.get_entity(username)
            except Exception:
                pass
        raw_name = (target or {}).get("sender_name") or ""
        # NFKC convierte nombres decorativos («𝒜𝓇𝒾𝓃𝒶» → «Arina») en
        # algo buscable en la lista de participantes
        import unicodedata
        name_hints = [unicodedata.normalize("NFKC", raw_name).strip(), raw_name]
        name_hints = [h for i, h in enumerate(name_hints) if h and h not in name_hints[:i]]
        chat_ids = [origin_chat_id]
        chat_ids += [cid for cid in self._monitored_chat_ids if cid != origin_chat_id]
        for chat_id in chat_ids:
            chat_entity = await self._get_chat_entity(chat_id)
            if chat_entity is None:
                continue
            for hint in name_hints:
                try:
                    # Buscar en los participantes del chat por nombre —
                    # encuentra aunque no haya posteado recientemente
                    async for p in self.client.iter_participants(
                            chat_entity, search=hint):
                        if p.id == sender_id:
                            return await self.client.get_input_entity(sender_id)
                except Exception:
                    pass
            try:
                # El filtro from_user puede funcionar si Telegram resuelve el
                # id en el contexto del peer
                async for msg in self.client.iter_messages(
                        chat_entity, from_user=sender_id, limit=1):
                    try:
                        return await self.client.get_input_entity(sender_id)
                    except ValueError:
                        pass
                    break
            except Exception:
                pass
            try:
                # Escaneo cliente-side del historial reciente
                async for msg in self.client.iter_messages(chat_entity, limit=300):
                    if msg.sender_id == sender_id:
                        try:
                            return await self.client.get_input_entity(sender_id)
                        except ValueError:
                            sender = await msg.get_sender()
                            if sender:
                                return sender
            except Exception as e:
                logger.warning(f"Escaneo de {chat_id} para {sender_id}: {e}")
        return None

    async def _get_chat_entity(self, chat_id):
        """Resuelve la entity de un chat monitorizado tolerando los formatos
        de id que llegan de la BD (negativo, -100…, positivo bare)."""
        try:
            chat_id = int(chat_id)
        except (TypeError, ValueError):
            return None
        bare, ptype = utils.resolve_id(chat_id)
        for cand in (chat_id, ptype(bare), tl_types.PeerChannel(bare)):
            try:
                return await self.client.get_entity(cand)
            except Exception:
                continue
        return None

    async def start_dialog_with_lead(self, lead_id: int) -> bool:
        """Начало диалога с лидом: AI генерирует варианты первого сообщения (A/B), выбирает лучший и отправляет."""
        import ai_engine
        import random

        if self._paused:
            logger.warning("Диалог не начат: исходящие сообщения заблокированы")
            return False

        lead = await db.get_lead(lead_id)
        if not lead:
            logger.error(f"Лид #{lead_id} не найден")
            return False

        # Личный диалог возможен только с реальным пользователем, а не с каналом/анонимом
        if not lead.get("sender_id") or (lead.get("sender_type") and lead["sender_type"] != "user"):
            logger.warning(f"Лид #{lead_id}: нет прямого контакта (sender_type={lead.get('sender_type')}) — диалог не начат")
            return False

        # Проверяем, не писали ли уже этому пользователю
        if await db.has_seen_user(lead["sender_id"]):
            logger.warning(f"Уже писали пользователю {lead['sender_id']}")
            return False

        # Проверяем анти-бан
        if not await self.anti_ban.can_send():
            logger.warning("Невозможно начать диалог: анти-бан лимиты")
            return False

        # Заметка об анти-повторе — до создания диалога, чтобы не учитывать текущую пустую запись
        anti_repeat_note = await self._anti_repeat_note()

        # Создаём запись диалога
        dialog_id = await db.create_dialog(
            lead_id=lead_id,
            sender_id=lead["sender_id"],
            sender_name=lead["sender_name"],
            sender_username=lead.get("sender_username"),
            lead_source="regular",
        )
        await db.update_dialog_sales_data(
            dialog_id,
            client_name=lead["sender_name"],
            contact=f"@{lead['sender_username']}" if lead.get("sender_username") else None,
        )

        # Формируем контекст для первого сообщения
        context = (
            f"Вы увидели сообщение в чате \"{lead['chat_name']}\":\n"
            f"\"{lead['message_text']}\"\n\n"
            f"Напишите первое сообщение этому человеку. "
            f"Кратко представьтесь, упомяните что увидели его запрос, "
            f"предложите обсудить проект. Не более 2-3 предложений."
        )

        # A/B: генерируем 3 варианта, выбираем по весам
        variants = await ai_engine.generate_first_message_variants(
            context=context,
            anti_repeat_note=anti_repeat_note,
            num_variants=3,
        )

        if not variants:
            # Fallback на старый метод
            messages = [{"role": "user", "content": context + anti_repeat_note}]
            result = await ai_engine.generate_dialogue_response(
                messages_history=messages,
                stage="INITIATING",
            )
            if result is None:
                logger.error("Ошибка генерации первого сообщения (fallback)")
                return False
            first_message, new_stage = result
            chosen_variant = 0
        else:
            weights = await db.ab_get_weights(len(variants))
            chosen_variant = random.choices(range(len(variants)), weights=weights, k=1)[0]
            first_message = variants[chosen_variant]
            new_stage = "QUALIFYING"

        messages = [
            {"role": "user", "content": context + anti_repeat_note},
            {"role": "assistant", "content": first_message},
        ]

        await db.update_dialog_messages(dialog_id, json.dumps(messages, ensure_ascii=False))
        await db.update_dialog_stage(dialog_id, new_stage)
        await db.set_dialog_ab_variant(dialog_id, chosen_variant)
        await db.ab_record_sent(chosen_variant)

        # Отправляем
        success = await self._send_message(lead["sender_id"], first_message, fast=True)
        if success:
            await db.mark_user_seen(lead["sender_id"])
            await db.update_lead_status(lead_id, "DIALOG_STARTED")
            logger.info(f"Диалог #{dialog_id} начат с лидом #{lead_id} (A/B вариант #{chosen_variant})")
            return True
        return False

    async def start_dialog_with_cold_lead(self, lead_id: int) -> bool:
        """Начало диалога с холодным лидом: AI генерирует варианты первого сообщения (A/B), выбирает и отправляет."""
        import ai_engine
        import random

        if self._paused:
            logger.warning("Холодный диалог не начат: исходящие сообщения заблокированы")
            return False

        lead = await db.get_cold_lead(lead_id)
        if not lead:
            logger.error(f"Холодный лид #{lead_id} не найден")
            return False

        if not lead.get("sender_id") or (lead.get("sender_type") and lead["sender_type"] != "user"):
            logger.warning(f"Холодный лид #{lead_id}: нет прямого контакта (sender_type={lead.get('sender_type')}) — диалог не начат")
            return False

        if await db.has_seen_user(lead["sender_id"]):
            logger.warning(f"Уже писали пользователю {lead['sender_id']}")
            return False

        if not await self.anti_ban.can_send():
            logger.warning("Невозможно начать холодный диалог: анти-бан лимиты")
            return False

        anti_repeat_note = await self._anti_repeat_note()

        dialog_id = await db.create_dialog(
            lead_id=lead_id,
            sender_id=lead["sender_id"],
            sender_name=lead["sender_name"],
            sender_username=lead.get("sender_username"),
            lead_source="cold",
        )
        await db.update_dialog_sales_data(
            dialog_id,
            client_name=lead["sender_name"],
            contact=f"@{lead['sender_username']}" if lead.get("sender_username") else None,
        )

        context = (
            f"Вы увидели сообщение в чате \"{lead['chat_name']}\":\n"
            f"\"{lead['message_text']}\"\n\n"
            f"Это ХОЛОДНЫЙ лид: человек не искал разработчика напрямую, "
            f"но у него есть потребность.\n"
        )
        if lead.get("business_type"):
            context += f"Тип бизнеса: {lead['business_type']}\n"
        if lead.get("pain"):
            context += f"Его боль/потребность: {lead['pain']}\n"
        if lead.get("hook"):
            context += f"Рекомендуемый подход: {lead['hook']}\n"
        context += (
            "\nНапишите первое сообщение этому человеку. Он не просил услуги, поэтому ничего "
            "не продавайте и не перечисляйте свои возможности. Отреагируйте на конкретную боль "
            "одной короткой фразой и задайте один естественный диагностический вопрос. "
            "Не используйте слова «автоматизация», «решение», «проект», «предложение» и "
            "не пишите общие фразы вроде «я помогаю бизнесам». Не более 2 предложений. "
            "Пример нужного тона: «Да, с первичкой легко утонуть. А больше времени съедает "
            "перенос данных или их проверка?»"
        )

        # A/B: генерируем 3 варианта, выбираем по весам
        variants = await ai_engine.generate_first_message_variants(
            context=context,
            anti_repeat_note=anti_repeat_note,
            num_variants=3,
        )

        if not variants:
            messages = [{"role": "user", "content": context + anti_repeat_note}]
            result = await ai_engine.generate_dialogue_response(
                messages_history=messages,
                stage="INITIATING",
            )
            if result is None:
                logger.error("Ошибка генерации первого сообщения холодному лиду (fallback)")
                return False
            first_message, new_stage = result
            chosen_variant = 0
        else:
            weights = await db.ab_get_weights(len(variants))
            chosen_variant = random.choices(range(len(variants)), weights=weights, k=1)[0]
            first_message = variants[chosen_variant]
            new_stage = "QUALIFYING"

        messages = [
            {"role": "user", "content": context + anti_repeat_note},
            {"role": "assistant", "content": first_message},
        ]

        await db.update_dialog_messages(dialog_id, json.dumps(messages, ensure_ascii=False))
        await db.update_dialog_stage(dialog_id, new_stage)
        await db.set_dialog_ab_variant(dialog_id, chosen_variant)
        await db.ab_record_sent(chosen_variant)

        success = await self._send_message(lead["sender_id"], first_message, fast=True)
        if success:
            await db.mark_user_seen(lead["sender_id"])
            await db.update_cold_lead_status(lead_id, "DIALOG_STARTED")
            logger.info(f"Холодный диалог #{dialog_id} начат с лидом #{lead_id} (A/B вариант #{chosen_variant})")
            return True
        return False

    async def send_followup(self, dialog: dict) -> bool:
        """Отправка follow-up сообщения клиенту, который не ответил.
        Генерирует короткое ненавязчивое напоминание через AI.
        """
        import ai_engine

        if not await self.anti_ban.can_send():
            logger.warning(f"Follow-up диалог #{dialog['id']}: анти-бан лимиты")
            return False

        messages = json.loads(dialog.get("ai_messages_json") or "[]")
        followup_count = dialog.get("followup_count", 0)

        # Форумные диалоги — фиксированный текст напоминания про промокод.
        # Автоотправка отключена (FORUM_FOLLOWUP_ENABLED=0): пропускаем диалог.
        if dialog.get("lead_source") == "forum_outreach":
            if not FORUM_FOLLOWUP_ENABLED or not FORUM_FOLLOWUP_TEXT:
                await db.update_dialog_stage(dialog["id"], "ENDED")
                logger.info(f"Follow-up выключен — диалог #{dialog['id']} завершён без напоминания")
                return False
            followup_msg = FORUM_FOLLOWUP_TEXT.format(
                date=FORUM_DATE, promo=FORUM_PROMO_CODE, url=FORUM_TICKET_URL)
            messages.append({"role": "assistant", "content": followup_msg})
            await db.update_dialog_messages(dialog["id"], json.dumps(messages, ensure_ascii=False))
            await db.increment_followup(dialog["id"])
            await self._forum_cooldown()
            success = await self._send_message(dialog["sender_id"], followup_msg, fast=True)
            if success:
                logger.info(f"Forum follow-up #{followup_count + 1} для диалога #{dialog['id']}")
                return True
            return False

        context = (
            f"Клиент не ответил на ваше последнее сообщение. "
            f"Это {followup_count + 1}-е напоминание (не более {FOLLOWUP_MAX}). "
            f"Напишите короткое, ненавязчивое напоминание (1-2 предложения). "
            f"Будьте дружелюбны, без давления. Не повторяйте предыдущие сообщения. "
            f"Мягко напомните о себе и предложите обсудить, если ещё актуально."
        )
        messages.append({"role": "user", "content": context})

        result = await ai_engine.generate_dialogue_response(
            messages_history=messages,
            stage=dialog.get("stage", "INITIATING"),
        )

        if result is None:
            logger.error(f"Ошибка генерации follow-up для диалога #{dialog['id']}")
            return False

        followup_msg, new_stage = result
        messages.append({"role": "assistant", "content": followup_msg})

        await db.update_dialog_messages(dialog["id"], json.dumps(messages, ensure_ascii=False))
        await db.increment_followup(dialog["id"])

        success = await self._send_message(dialog["sender_id"], followup_msg, fast=True)
        if success:
            logger.info(f"Follow-up #{followup_count + 1} отправлен для диалога #{dialog['id']}")
            return True
        return False

    async def continue_dialog_with_price(self, dialog_ref: str, price: str) -> bool:
        """Продолжение диалога с ценой владельца по ID диалога или username."""
        import sales_engine

        dialog = await db.get_dialog_by_identifier(str(dialog_ref))
        if not dialog:
            logger.error(f"Диалог {dialog_ref} не найден")
            return False
        if dialog["stage"] in ("ENDED", "CLOSED", "HANDOFF") or dialog.get("do_not_contact"):
            logger.warning(f"Нельзя назначить цену завершённому диалогу #{dialog['id']}")
            return False

        await db.update_dialog_price(dialog["id"], price)
        await db.update_dialog_sales_data(dialog["id"], awaiting_owner_price=0)
        dialog["price"] = price
        dialog["awaiting_owner_price"] = 0
        messages = json.loads(dialog.get("ai_messages_json") or "[]")
        reply = await sales_engine.generate_reply(
            dialog, messages,
            f"Владелец подтвердил цену: {price}. Сообщи её клиенту без изменения и коротко "
            "объясни, что точный состав работ фиксируется в ТЗ. Задай один вопрос, подходит ли цена.",
        )
        if not reply:
            logger.error(f"Ошибка генерации ответа с ценой для диалога #{dialog['id']}")
            return False
        return await self._send_and_record(dialog, messages, reply, "NEGOTIATING")

    async def send_scheduled_message(self, chat_id: int, text: str):
        """Отправка запланированного сообщения в чат (для scheduler)."""
        if self._paused:
            logger.warning("Запланированная отправка отменена: исходящие заблокированы")
            return False
        if not await self.anti_ban.can_send():
            logger.warning("Запланированная отправка заблокирована анти-бан модулем")
            return False

        await self.anti_ban.wait_before_action()

        # Повторная проверка: пока ждали, другая задача могла исчерпать лимит
        if not await self.anti_ban.can_send():
            logger.warning("Лимит исчерпан во время ожидания, отправка отменена")
            return False

        await self.anti_ban.wait_typing(text)

        try:
            await self.client.send_message(chat_id, text)
            await self.anti_ban.record_action()
            await db.log_message(chat_id, 0, "scheduled", text, 0, "outgoing_scheduled")
            logger.info(f"Запланированное сообщение отправлено в чат {chat_id}")
            return True
        except Exception as e:
            logger.error(f"Ошибка запланированной отправки: {e}")
            return False

    async def join_chat(self, chat_link: str) -> int | None:
        """Вступление в чат по ссылке или username."""
        try:
            from telethon import utils as tl_utils
            await self.anti_ban.wait_before_action()
            entity = await self.client.get_entity(chat_link)
            await self.client.join_chat(entity)
            await self.anti_ban.record_action()
            # get_peer_id даёт маркированный ID (-100...), совпадающий с event.chat_id
            chat_id = tl_utils.get_peer_id(entity)
            chat_name = getattr(entity, "title", str(chat_id))
            logger.info(f"Вступил в чат: {chat_name} (ID: {chat_id})")
            return chat_id
        except Exception as e:
            logger.error(f"Ошибка вступления в чат {chat_link}: {e}")
            return None

    async def fetch_dialogs(self) -> list[dict]:
        """Список всех групп/каналов аккаунта (для интерфейса управления чатами).

        Dialog.id — уже маркированный ID, совместим с event.chat_id.
        """
        dialogs = []
        async for d in self.client.iter_dialogs():
            if d.is_group or d.is_channel:
                dialogs.append({"chat_id": d.id, "chat_name": d.title or str(d.id)})
        logger.info(f"Получено диалогов (группы/каналы): {len(dialogs)}")
        return dialogs

    @property
    def is_running(self) -> bool:
        return self._running
