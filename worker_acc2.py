"""Лёгкий воркер для дополнительных аккаунтов (acc2, acc3, ...).

Конфиг берётся целиком из env: TG_SESSION_NAME, TG_PROXY_*, BOT_PERSONA_NAME.
systemd: lead-hunter@N → /opt/lead-hunter/accN.env.
Локально: powershell -File start_acc2.ps1   (задаёт TG_SESSION_NAME + прокси)

Без бота владельца и HTTP API — только:
- слушатель сообщений (харвест кандидатов из is_harvest-чатов,
  ведение форумных диалогов в ЛС)
- цикл приглашений по outreach_targets через CLAIMED-дедуп
  (общий bot_acc1.db — не дублирует касания acc1)
"""
import asyncio
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("worker_acc2")

import database as db
from user_client import UserClient
from scheduler import MessageScheduler
from config import (FORUM_OUTREACH_ENABLED, OUTREACH_WORKER_INTERVAL_MIN,
                    TG_SESSION_NAME)


async def noop_notify(text: str):
    logger.info(f"[notify] {text}")


async def main():
    await db.init_db()

    user_client = UserClient(notification_callback=None)
    logger.info(f"Запуск воркера аккаунта {TG_SESSION_NAME} (Telethon)...")
    await user_client.start()

    scheduler = MessageScheduler(
        user_client=user_client,
        broadcast_notify_callback=noop_notify)

    if not FORUM_OUTREACH_ENABLED:
        logger.warning("FORUM_OUTREACH_ENABLED выключен — только слушатель")

    while True:
        try:
            if FORUM_OUTREACH_ENABLED:
                await scheduler._forum_outreach_worker()
        except Exception as e:
            logger.error(f"outreach worker error: {e}", exc_info=True)
        await asyncio.sleep(OUTREACH_WORKER_INTERVAL_MIN * 60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Остановлено пользователем")
