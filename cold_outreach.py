"""Холодный обход: генерация сообщений, поиск Telegram-аккаунтов по телефону,
контроль лимитов и отправка (только после одобрения владельцем).
"""
import asyncio
import logging
import random
from datetime import datetime, timedelta

import database as db
from ai_engine import _get_client, CLASSIFY_AI_PROVIDER
from config import COLD_2GIS_MESSAGE_PROMPT

logger = logging.getLogger(__name__)
async def generate_cold_message(business_info: dict) -> str:
    """Генерирует персонализированное первое холодное сообщение."""
    client = _get_client(CLASSIFY_AI_PROVIDER)

    info_text = (
        f"Название: {business_info.get('name', '')}\n"
        f"Категория: {business_info.get('category', '')}\n"
        f"Город: {business_info.get('city', '')}\n"
        f"Адрес: {business_info.get('address', '')}\n"
    )
    if not business_info.get("has_website", 0):
        info_text += "Особенность: у компании нет сайта\n"

    prompt = COLD_2GIS_MESSAGE_PROMPT.format(business_info=info_text)

    response = await client.chat.completions.create(
        model="auto/claude-sonnet",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
        max_tokens=250,
    )
    return response.choices[0].message.content.strip()


async def find_telegram_by_phone(client, phone: str) -> tuple[int | None, str | None]:
    """Пытается найти Telegram-аккаунт по номеру телефона через ImportContacts.

    Возвращает (telegram_id, username) или (None, None).
    """
    try:
        from telethon.tl.functions.contacts import ImportContactsRequest
        from telethon.tl.types import InputPhoneContact

        contact = InputPhoneContact(
            client_id=random.randint(0, 2**31 - 1),
            phone=phone,
            first_name="Probe",
            last_name="",
        )
        result = await client(ImportContactsRequest([contact]))

        if result.imported:
            user = result.imported[0]
            return user.user_id, getattr(user, "username", None)

        if result.users:
            user = result.users[0]
            return user.id, getattr(user, "username", None)

        return None, None
    except Exception as e:
        logger.warning(f"Не удалось найти Telegram по телефону {phone}: {e}")
        return None, None


async def prepare_next_gis_company(telethon_client) -> dict | None:
    """Берёт следующую компанию из 2GIS, генерирует сообщение, ищет Telegram.
    Статус меняет на PENDING_REVIEW (ожидает вашего одобрения).
    """
    companies = await db.get_pending_gis_companies(limit=1)
    if not companies:
        return None

    company = companies[0]

    # Генерируем сообщение
    message = await generate_cold_message(company)

    # Ищем Telegram по телефону
    telegram_id, telegram_username = None, None
    if company.get("phone"):
        telegram_id, telegram_username = await find_telegram_by_phone(
            telethon_client, company["phone"]
        )

    await db.update_gis_company_status(
        company["id"],
        "PENDING_REVIEW",
        telegram_id=telegram_id,
        telegram_username=telegram_username,
        generated_message=message,
    )

    return {
        "id": company["id"],
        "name": company["name"],
        "phone": company.get("phone"),
        "telegram_id": telegram_id,
        "telegram_username": telegram_username,
        "generated_message": message,
    }


async def approve_and_send_cold_message(telethon_client, company_id: int, anti_ban) -> bool:
    """Отправляет одобренное холодное сообщение."""
    if (await db.get_setting("outbound_paused")) == "1":
        logger.warning("Холодная отправка заблокирована: исходящие выключены владельцем")
        return False
    if not await anti_ban.can_send_cold():
        logger.warning("Лимиты холодного обхода исчерпаны")
        return False

    company = None
    async with db.aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = db.aiosqlite.Row
        async with conn.execute("SELECT * FROM gis_companies WHERE id = ?", (company_id,)) as cursor:
            row = await cursor.fetchone()
            company = dict(row) if row else None

    if not company or not company.get("telegram_id"):
        logger.error(f"Компания #{company_id} не готова к отправке")
        return False

    message_text = company.get("generated_message")
    if not message_text:
        logger.error(f"У компании #{company_id} нет сгенерированного сообщения")
        return False

    # Анти-бан пауза
    await anti_ban.wait_before_action()
    await anti_ban.wait_typing(message_text)

    try:
        await telethon_client.send_message(company["telegram_id"], message_text)
        await anti_ban.record_cold_action(
            target_id=company["telegram_id"],
            target_name=company["name"],
            message_text=message_text,
            status="sent",
        )
        await db.update_gis_company_status(company_id, "SENT")
        await anti_ban.record_action()  # общий счётчик тоже
        logger.info(f"Холодное сообщение отправлено {company['name']}")
        return True
    except Exception as e:
        logger.error(f"Ошибка отправки холодного сообщения: {e}")
        await anti_ban.record_cold_action(
            target_id=company.get("telegram_id") or 0,
            target_name=company["name"],
            message_text=message_text,
            status=f"error: {e}",
        )
        return False
