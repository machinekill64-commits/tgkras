"""Симуляция лида: создаёт forum-диалог для тестового пользователя
и шлёт реальный опенер с рабочего аккаунта (через копию сессии).
Запуск на сервере: .venv/bin/python simulate_lead.py
"""
import asyncio
import json
import shutil
import os

import config
import database as db
from telethon import TelegramClient

TEST_SENDER_ID = 6561323821
TEST_USERNAME = "littlehugYB"
TEST_NAME = "Михаил Кудинов"
CHAT_NAME = "Бизнес Краснодар"

OPENER = (
    f"Здравствуйте, Михаил! Заметила вас в чате «{CHAT_NAME}». "
    "Интересно — у вас свой проект или работа в найме?"
)

TMP_SESSION = "/tmp/sim_acc1"


async def main():
    await db.init_db()

    # Создаём диалог как после реального send_forum_invite
    dialog_id = await db.create_dialog(
        lead_id=0, sender_id=TEST_SENDER_ID,
        sender_name=TEST_NAME, sender_username=TEST_USERNAME,
        lead_source="forum_outreach",
    )
    messages = [
        {"role": "system", "content": "Приглашение на форум отправлено организатором."},
        {"role": "assistant", "content": OPENER},
    ]
    await db.update_dialog_messages(dialog_id, json.dumps(messages, ensure_ascii=False))
    await db.update_dialog_stage(dialog_id, "FORUM_OPENER")
    print(f"DIALOG_CREATED id={dialog_id}")

    # Отправляем опенер через копию сессии (боевой файл залочен сервисом)
    shutil.copyfile(f"{config.TG_SESSION_NAME}.session", TMP_SESSION + ".session")
    proxy = {"proxy_type": "http", "addr": config.TG_PROXY_HOST,
             "port": config.TG_PROXY_PORT, "rdns": True}
    if config.TG_PROXY_USERNAME:
        proxy["username"] = config.TG_PROXY_USERNAME
        proxy["password"] = config.TG_PROXY_PASSWORD
    client = TelegramClient(TMP_SESSION, config.TG_API_ID, config.TG_API_HASH,
                            proxy=proxy)
    await client.connect()
    entity = await client.get_entity(TEST_USERNAME)
    msg = await client.send_message(entity, OPENER)
    print(f"SENT msg_id={msg.id}")
    await client.disconnect()
    os.remove(TMP_SESSION + ".session")
    print("DONE")


if __name__ == "__main__":
    asyncio.run(main())
