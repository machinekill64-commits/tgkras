"""Планировщик рассылки сообщений по чатам по расписанию (cron)."""
import asyncio
import json
import random
import re
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telethon.errors import PeerFloodError, FloodWaitError
from config import (SCHEDULER_TIMEZONE, FOLLOWUP_ENABLED, FOLLOWUP_HOURS_THRESHOLD,
                    FOLLOWUP_MAX, FOLLOWUP_CHECK_INTERVAL_MIN, CHAT_SEARCH_CLEANUP_DAYS,
                    FORUM_OUTREACH_ENABLED, OUTREACH_WORKER_INTERVAL_MIN,
                    HARVEST_MIN_MESSAGES, HARVEST_RECENCY_DAYS, TG_SESSION_NAME,
                    OUTREACH_SERVER_SLOT, OUTREACH_SERVER_COUNT)
import database as db
import ai_engine

logger = logging.getLogger(__name__)

TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

RU_WEEKDAYS = ["понедельник", "вторник", "среда", "четверг",
               "пятница", "суббота", "воскресенье"]
RU_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня",
             "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def parse_broadcast_times(times_str: str) -> list[tuple[int, int]]:
    """Парсит строку '10:00, 19:30' в список (час, минута). Неверные — пропускает."""
    result = []
    for part in (times_str or "").split(","):
        m = TIME_RE.match(part.strip())
        if m:
            result.append((int(m.group(1)), int(m.group(2))))
    return result


def now_ru_str() -> str:
    """Текущие дата и время по-русски в TZ планировщика (для промпта AI)."""
    now = datetime.now(ZoneInfo(SCHEDULER_TIMEZONE))
    return (f"{RU_WEEKDAYS[now.weekday()]}, {now.day} {RU_MONTHS[now.month - 1]} "
            f"{now.year} года, {now.strftime('%H:%M')}")


class MessageScheduler:
    """Управление запланированными рассылками в чаты."""

    def __init__(self, user_client=None, broadcast_notify_callback=None):
        self.scheduler = AsyncIOScheduler(timezone=SCHEDULER_TIMEZONE)
        self.user_client = user_client
        self.broadcast_notify_callback = broadcast_notify_callback

    async def start(self):
        await self._load_jobs()
        self.scheduler.start()
        logger.info(f"Планировщик запущен (TZ: {SCHEDULER_TIMEZONE})")

        if FOLLOWUP_ENABLED:
            self.scheduler.add_job(
                self._check_followups,
                trigger="interval",
                minutes=FOLLOWUP_CHECK_INTERVAL_MIN,
                id="followup_checker",
                name="Follow-up проверка",
                misfire_grace_time=300,
                coalesce=True,
            )
            logger.info(f"Follow-up checker: каждые {FOLLOWUP_CHECK_INTERVAL_MIN} мин, "
                        f"порог {FOLLOWUP_HOURS_THRESHOLD}ч, макс {FOLLOWUP_MAX}")

        # Воркер приглашений на форум: берёт кандидатов из outreach_targets
        if FORUM_OUTREACH_ENABLED:
            self.scheduler.add_job(
                self._forum_outreach_worker,
                trigger="interval",
                minutes=OUTREACH_WORKER_INTERVAL_MIN,
                id="forum_outreach",
                name="Рассылка приглашений на форум",
                misfire_grace_time=300,
                coalesce=True,
            )
            logger.info(f"Forum outreach worker: каждые {OUTREACH_WORKER_INTERVAL_MIN} мин")

        # Ежедневная очистка старых найденных чатов (в 3:00 ночи)
        self.scheduler.add_job(
            self._cleanup_found_chats,
            trigger=CronTrigger(hour=3, minute=0),
            id="cleanup_found_chats",
            name=f"Очистка found_chats старше {CHAT_SEARCH_CLEANUP_DAYS} дней",
            misfire_grace_time=3600,
            coalesce=True,
        )
        logger.info(f"Cleanup found_chats: ежедневно в 03:00, удаление старше {CHAT_SEARCH_CLEANUP_DAYS} дней")

    async def stop(self):
        self.scheduler.shutdown(wait=False)
        logger.info("Планировщик остановлен")

    async def _load_jobs(self):
        """Загружает все запланированные рассылки из БД."""
        chats = await db.get_scheduled_chats()
        for chat in chats:
            self._add_job(chat)
        logger.info(f"Загружено запланированных рассылок: {len(chats)}")

        bcast_chats = await db.get_broadcast_chats()
        jobs_count = 0
        for chat in bcast_chats:
            jobs_count += self._add_broadcast_jobs(chat)
        logger.info(f"Загружено AI-рассылок: {len(bcast_chats)} чатов, {jobs_count} задач")

    def _add_job(self, chat: dict):
        """Добавляет cron-задачу для чата."""
        chat_id = chat["chat_id"]
        cron = chat.get("schedule_cron")
        if not cron:
            return

        try:
            trigger = CronTrigger.from_crontab(cron, timezone=SCHEDULER_TIMEZONE)
        except Exception as e:
            logger.error(f"Неверный cron для чата {chat_id}: {cron} — {e}")
            return

        job_id = f"chat_{chat_id}"

        # Удаляем старую задачу если есть
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)

        self.scheduler.add_job(
            self._send_scheduled,
            trigger=trigger,
            args=[chat_id],
            id=job_id,
            name=f"Рассылка: {chat.get('chat_name', chat_id)}",
            misfire_grace_time=300,
        )
        logger.info(f"Добавлена задача {job_id}: cron={cron}")

    async def _send_scheduled(self, chat_id: int):
        """Отправка запланированного сообщения в чат."""
        if not self.user_client:
            logger.error("User client не инициализирован для scheduler")
            return

        # Проверка паузы
        if self.user_client.is_paused:
            logger.info(f"Бот на паузе — рассылка для чата {chat_id} пропущена")
            return

        # Получаем настройки чата
        async with db.aiosqlite.connect(db.DB_PATH) as conn:
            conn.row_factory = db.aiosqlite.Row
            async with conn.execute(
                "SELECT * FROM chats WHERE chat_id = ?", (chat_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return
                chat = dict(row)

        # Выбираем текст: случайный вариант или основной
        text = chat.get("message_text", "")
        variants_json = chat.get("message_variants")
        if variants_json:
            try:
                variants = json.loads(variants_json)
                if variants:
                    text = random.choice(variants)
            except json.JSONDecodeError:
                pass

        if not text:
            logger.warning(f"Нет текста для рассылки в чат {chat_id}")
            return

        logger.info(f"Отправка запланированного сообщения в чат {chat.get('chat_name', chat_id)}")
        await self.user_client.send_scheduled_message(chat_id, text)

    def _add_broadcast_jobs(self, chat: dict) -> int:
        """Добавляет cron-задачи AI-рассылки для чата. Возвращает число задач."""
        chat_id = chat["chat_id"]
        times = parse_broadcast_times(chat.get("broadcast_times") or "")
        if not times:
            logger.warning(f"Чат {chat_id}: неверный формат времени рассылки "
                           f"'{chat.get('broadcast_times')}'")
            return 0

        count = 0
        for hour, minute in times:
            job_id = f"bcast_{chat_id}_{hour:02d}{minute:02d}"
            if self.scheduler.get_job(job_id):
                self.scheduler.remove_job(job_id)
            self.scheduler.add_job(
                self._send_ai_broadcast,
                trigger=CronTrigger(hour=hour, minute=minute,
                                    timezone=SCHEDULER_TIMEZONE, jitter=1500),
                args=[chat_id],
                id=job_id,
                name=f"AI-рассылка: {chat.get('chat_name', chat_id)} {hour:02d}:{minute:02d}",
                misfire_grace_time=900,
                coalesce=True,
            )
            count += 1
        return count

    async def _notify_owner(self, text: str):
        if self.broadcast_notify_callback:
            try:
                await self.broadcast_notify_callback(text)
            except Exception as e:
                logger.error(f"Ошибка уведомления владельца: {e}")

    async def _send_ai_broadcast(self, chat_id: int):
        """Генерация и отправка AI-рассылки в чат."""
        if not self.user_client:
            logger.error("User client не инициализирован для AI-рассылки")
            return

        # Проверка паузы
        if self.user_client.is_paused:
            logger.info(f"Бот на паузе — AI-рассылка для чата {chat_id} пропущена")
            return

        # Перечитываем чат: правила/флаги могли измениться после создания джоба
        chat = await db.get_chat(chat_id)
        if not chat or not chat.get("is_broadcast"):
            logger.info(f"AI-рассылка в чат {chat_id} отменена: чат выключен")
            return
        chat_name = chat.get("chat_name", str(chat_id))

        rules = (chat.get("chat_rules") or "").strip()
        is_direct = bool(chat.get("is_direct_promo"))
        if not is_direct and not rules:
            logger.warning(f"Чат {chat_name}: правила не настроены, рассылка не отправлена")
            await self._notify_owner(
                f"⚠️ Рассылка в чат «{chat_name}» НЕ отправлена:\n"
                f"правила чата не настроены. Откройте программу управления чатами "
                f"и впишите правила (или отметьте «Правил нет»)."
            )
            return

        recent = await db.get_recent_broadcasts(chat_id)
        recent_chat = await db.get_recent_incoming_messages(chat_id, limit=15)
        chat_niche = (chat.get("chat_niche") or "").strip()
        language = (chat.get("broadcast_language") or "ru").strip() or "ru"
        result = await ai_engine.generate_broadcast(rules, recent, now_ru_str(),
                                                     is_direct_promo=is_direct,
                                                     chat_name=chat_name,
                                                     recent_chat_messages=recent_chat,
                                                     chat_niche=chat_niche,
                                                     language=language)

        if result is None:
            await self._notify_owner(
                f"❌ Рассылка в чат «{chat_name}» не отправлена: ошибка AI-генерации."
            )
            return

        if result["skip"]:
            logger.info(f"AI пропустил рассылку в {chat_name}: {result['reason']}")
            await self._notify_owner(
                f"⏭️ Пропуск рассылки в «{chat_name}»:\n{result['reason']}"
            )
            return

        # Перевод рассылки на язык чата (если не русский)
        message_text = result["message"]
        if language and language != "ru":
            try:
                from translator import translate, get_language_label
                original_text = message_text
                message_text = await translate(message_text, "ru", language)
                logger.info(f"Рассылка переведена ru->{language}: {len(original_text)}->{len(message_text)} символов")
                result["message"] = message_text
                result["original_message"] = original_text
                result["translated"] = True
            except Exception as e:
                logger.error(f"Ошибка перевода рассылки ({language}): {e}")
                # Отправляем как есть — GigaChat мог написать на нужном языке

        success = await self.user_client.send_scheduled_message(chat_id, result["message"])
        if success:
            niche = result.get("chat_niche", "")
            entry = result.get("entry_point", "")
            niche_info = f"📊 Ниша: {niche}\n💡 Точка входа: {entry}\n\n" if niche else ""
            # Если был перевод — показываем оригинал и перевод
            if result.get("translated"):
                lang_label = get_language_label(language) if language else ""
                await self._notify_owner(
                    f"✅ Рассылка отправлена в «{chat_name}» [{lang_label}]:\n\n"
                    f"{niche_info}"
                    f"📝 Оригинал (ru):\n{result['original_message']}\n\n"
                    f"🌐 Перевод ({lang_label}):\n{result['message']}"
                )
            else:
                await self._notify_owner(
                    f"✅ Рассылка отправлена в «{chat_name}»:\n\n{niche_info}{result['message']}"
                )
        else:
            await self._notify_owner(
                f"❌ Рассылка в «{chat_name}» не отправлена: анти-бан лимиты или ошибка отправки."
            )

    async def _check_followups(self):
        """Проверяет stale-диалоги и отправляет follow-up сообщения."""
        if not self.user_client:
            return

        # Проверка паузы
        if self.user_client.is_paused:
            logger.info("Бот на паузе — follow-up проверка пропущена")
            return

        stale = await db.get_stale_dialogs(FOLLOWUP_HOURS_THRESHOLD, FOLLOWUP_MAX)
        if not stale:
            return

        logger.info(f"Follow-up: найдено {len(stale)} stale-диалогов")

        for dialog in stale:
            try:
                success = await self.user_client.send_followup(dialog)
                if success and self.broadcast_notify_callback:
                    await self.broadcast_notify_callback(
                        f"📨 Follow-up отправлен для диалога #{dialog['id']} "
                        f"с {dialog.get('sender_name', 'клиентом')} "
                        f"(напоминание #{dialog.get('followup_count', 0) + 1}/{FOLLOWUP_MAX})"
                    )
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"Follow-up ошибка для диалога #{dialog['id']}: {e}", exc_info=True)

    async def _forum_outreach_worker(self):
        """Периодически отправляет приглашения новым активным участникам чатов."""
        if not self.user_client:
            return
        if self.user_client.is_paused:
            logger.info("Бот на паузе — воркер приглашений пропущен")
            return
        if not await self.user_client.anti_ban.can_send_cold():
            return

        targets = await db.get_pending_outreach_targets(
            limit=5, min_messages=HARVEST_MIN_MESSAGES,
            recency_days=HARVEST_RECENCY_DAYS,
            slot=OUTREACH_SERVER_SLOT, server_count=OUTREACH_SERVER_COUNT)
        for target in targets:
            if not await self.user_client.anti_ban.can_send_cold():
                break
            try:
                # Мульти-аккаунт: атомарный захват цели до отправки —
                # второй аккаунт получит False и возьмёт следующего
                claimed = await db.claim_outreach_target(
                    target["id"], claimer=TG_SESSION_NAME)
                if not claimed:
                    continue
                # Квалификация: DIRECT/PAIN/WARM_COLD пишем, REJECT пропускаем
                category = await ai_engine.classify_forum_candidate(
                    target.get("sample_texts") or target.get("last_message_text") or "")
                if category == "REJECT":
                    await db.mark_outreach_target(target["id"], "REJECTED")
                    continue
                try:
                    status = await self.user_client.send_forum_invite(target)
                except (PeerFloodError, FloodWaitError) as e:
                    # Аккаунт в флуд-лимите — цель обратно в очередь,
                    # цикл останавливаем до следующего интервала
                    logger.error(
                        f"{type(e).__name__}: аккаунт ограничен — цель "
                        f"#{target['id']} возвращена в очередь, цикл остановлен")
                    await db.release_outreach_target(target["id"])
                    break
                if status == "sent":
                    await db.mark_outreach_target(target["id"], "SENT")
                    await self._notify_owner(
                        f"📩 Приглашение на форум отправлено: {target.get('sender_name')} "
                        f"(чат «{target.get('chat_name')}»)"
                    )
                elif status == "skipped_male":
                    await db.mark_outreach_target(target["id"], "SKIPPED_MALE")
                elif status == "skipped_city":
                    await db.mark_outreach_target(target["id"], "SKIPPED_CITY")
                elif status == "skipped":
                    await db.mark_outreach_target(target["id"], "SKIPPED")
                elif status == "limited":
                    # Лимит на отправку — возвращаем цель в очередь,
                    # её сможет взять любой аккаунт позже
                    await db.release_outreach_target(target["id"])
                    break
                else:
                    await db.mark_outreach_target(target["id"], "FAILED")
            except Exception as e:
                logger.error(f"Ошибка приглашения target #{target['id']}: {e}", exc_info=True)

    async def _cleanup_found_chats(self):
        """Ежедневная очистка найденных чатов старше CHAT_SEARCH_CLEANUP_DAYS дней."""
        try:
            deleted = await db.cleanup_old_found_chats(CHAT_SEARCH_CLEANUP_DAYS)
            if deleted > 0:
                logger.info(f"Очистка found_chats: удалено {deleted} записей старше {CHAT_SEARCH_CLEANUP_DAYS} дней")
        except Exception as e:
            logger.error(f"Ошибка очистки found_chats: {e}", exc_info=True)

    async def preview_broadcast(self, chat_id: int) -> dict | None:
        """Генерация текста рассылки БЕЗ отправки (кнопка «Тест» в GUI)."""
        chat = await db.get_chat(chat_id)
        if not chat:
            return None
        is_direct = bool(chat.get("is_direct_promo"))
        rules = (chat.get("chat_rules") or "").strip()
        if not is_direct and not rules:
            return {"skip": True, "reason": "Правила чата не настроены", "message": ""}
        recent = await db.get_recent_broadcasts(chat_id)
        recent_chat = await db.get_recent_incoming_messages(chat_id, limit=15)
        chat_name = chat.get("chat_name", str(chat_id))
        chat_niche = (chat.get("chat_niche") or "").strip()
        language = (chat.get("broadcast_language") or "ru").strip() or "ru"
        return await ai_engine.generate_broadcast(rules, recent, now_ru_str(),
                                                     is_direct_promo=is_direct,
                                                     chat_name=chat_name,
                                                     recent_chat_messages=recent_chat,
                                                     chat_niche=chat_niche,
                                                     language=language)

    async def reload_jobs(self):
        """Перезагрузка всех задач (после изменения расписаний)."""
        self.scheduler.remove_all_jobs()
        await self._load_jobs()
