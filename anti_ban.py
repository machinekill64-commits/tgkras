"""Модуль защиты от бана: задержки, лимиты, прогрев аккаунта."""
import asyncio
import random
import logging
from datetime import datetime, timedelta
import database as db
from config import (ANTI_BAN_MIN_DELAY, ANTI_BAN_MAX_DELAY,
                    ANTI_BAN_HOURLY_LIMIT, ANTI_BAN_DAILY_LIMIT,
                    COLD_OUTREACH_DAILY_LIMIT, COLD_OUTREACH_HOURLY_LIMIT,
                    WARMUP_DAYS, TG_SESSION_NAME)

logger = logging.getLogger(__name__)


class AntiBan:
    """Контролирует частоту действий аккаунта для предотвращения бана."""

    def __init__(self):
        self.warmup_start = datetime.now()
        self._is_warmup_done = False

    async def init_warmup(self):
        """Проверяет, был ли прогрев уже завершён ранее."""
        done = await db.get_setting("warmup_done")
        if done == "1":
            self._is_warmup_done = True
            logger.info("Прогрев аккаунта уже завершён ранее")
        else:
            logger.info(f"Прогрев аккаунта активен: {WARMUP_DAYS} дней только чтение")

    @property
    def is_warmup(self) -> bool:
        if self._is_warmup_done:
            return False
        elapsed = datetime.now() - self.warmup_start
        if elapsed >= timedelta(days=WARMUP_DAYS):
            self._is_warmup_done = True
            return False
        return True

    async def can_send(self) -> bool:
        """Проверяет, можно ли отправлять сообщение (лимиты + прогрев)."""
        if self.is_warmup:
            logger.warning("Прогрев аккаунта: отправка сообщений заблокирована")
            return False

        hourly = await db.get_hourly_actions_count()
        if hourly >= ANTI_BAN_HOURLY_LIMIT:
            logger.warning(f"Достигнут часовой лимит: {hourly}/{ANTI_BAN_HOURLY_LIMIT}")
            return False

        daily = await db.get_daily_actions_count()
        if daily >= ANTI_BAN_DAILY_LIMIT:
            logger.warning(f"Достигнут дневной лимит: {daily}/{ANTI_BAN_DAILY_LIMIT}")
            return False

        return True

    async def wait_before_action(self):
        """Случайная задержка перед действием (имитация человека)."""
        delay = random.uniform(ANTI_BAN_MIN_DELAY, ANTI_BAN_MAX_DELAY)
        jitter = random.uniform(-5, 15)
        total = max(5, delay + jitter)
        logger.info(f"Ожидание {total:.0f}с перед действием (анти-бан)")
        await asyncio.sleep(total)

    async def record_action(self):
        """Фиксирует действие в БД для учёта лимитов."""
        await db.record_action()

    async def can_send_cold(self) -> bool:
        """Проверяет лимиты холодного обхода ТЕКУЩЕГО аккаунта
        (type в cold_outreach_log = TG_SESSION_NAME, лимиты по-аккаунтные)."""
        today = datetime.now().strftime("%Y-%m-%d")
        hour = datetime.now().hour
        async with db.aiosqlite.connect(db.DB_PATH) as conn:
            async with conn.execute(
                "SELECT COUNT(*) FROM cold_outreach_log WHERE type = ? AND DATE(sent_at) = ?",
                (TG_SESSION_NAME, today)
            ) as cursor:
                daily = (await cursor.fetchone())[0]
            async with conn.execute(
                "SELECT COUNT(*) FROM cold_outreach_log WHERE type = ? AND DATE(sent_at) = ? AND strftime('%H', sent_at) = ?",
                (TG_SESSION_NAME, today, f"{hour:02d}")
            ) as cursor:
                hourly = (await cursor.fetchone())[0]
        if daily >= COLD_OUTREACH_DAILY_LIMIT:
            logger.warning(f"Дневной лимит холодного обхода: {daily}/{COLD_OUTREACH_DAILY_LIMIT}")
            return False
        if hourly >= COLD_OUTREACH_HOURLY_LIMIT:
            logger.warning(f"Часовой лимит холодного обхода: {hourly}/{COLD_OUTREACH_HOURLY_LIMIT}")
            return False
        return True

    async def record_cold_action(self, target_id: int, target_name: str, message_text: str, status: str = "sent"):
        """Фиксирует отправку холодного сообщения."""
        await db.log_cold_outreach(TG_SESSION_NAME, target_id, target_name, message_text, status)

    async def wait_typing(self, text: str):
        """Имитирует набор текста перед отправкой (задержка пропорциональна длине)."""
        chars = len(text)
        typing_time = min(max(chars * 0.12, 6), 35)
        typing_time += random.uniform(1.0, 4.0)
        logger.info(f"Имитация набора текста: {typing_time:.1f}с")
        await asyncio.sleep(typing_time)

    async def random_activity_pause(self):
        """Случайная пауза между разными типами действий."""
        pause = random.uniform(10, 60)
        await asyncio.sleep(pause)

    async def mark_warmup_done(self):
        """Принудительно завершить прогрев (через бот-команду)."""
        self._is_warmup_done = True
        await db.set_setting("warmup_done", "1")
        logger.info("Прогрев аккаунта завершён вручную")
