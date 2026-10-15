"""Вступление в выбранные чаты + включение мониторинга лидов.

Запуск на сервере: .venv/bin/python join_monitored.py
Делает: join (лимит 5/день + fallback вручную на 6-й), mute, архив,
upsert в таблицу chats с is_monitored=1. Ничего не отправляет.
"""
import asyncio
import os
import shutil
import sys

os.chdir("/opt/lead-hunter")
sys.path.insert(0, "/opt/lead-hunter")

import config
import database as db
import chat_finder
from telethon import TelegramClient, connection, utils
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.account import UpdateNotifySettingsRequest
from telethon.tl.functions.folders import EditPeerFoldersRequest
from telethon.tl.types import InputPeerNotifySettings, InputFolderPeer

USERNAMES = [
    "JOBAlertBechlor",
    "hire_developers2",
    "freelance_vacancies_jobs",
    "bizneschats",
    "udafrii",
    "rabotavakansij",
]

SESSION_COPY = "/opt/lead-hunter/join_temp.session"


def build_client() -> TelegramClient:
    kwargs = {"connection_retries": 5, "retry_delay": 2}
    if config.TG_PROXY_TYPE and config.TG_PROXY_HOST and config.TG_PROXY_PORT:
        proxy = {
            "proxy_type": config.TG_PROXY_TYPE,
            "addr": config.TG_PROXY_HOST,
            "port": config.TG_PROXY_PORT,
        }
        if config.TG_PROXY_USERNAME:
            proxy["username"] = config.TG_PROXY_USERNAME
            proxy["password"] = config.TG_PROXY_PASSWORD
        kwargs["proxy"] = proxy
    elif config.TG_MTPROTO_HOST and config.TG_MTPROTO_PORT and config.TG_MTPROTO_SECRET:
        kwargs["connection"] = connection.ConnectionTcpMTProxyRandomizedIntermediate
        kwargs["proxy"] = (config.TG_MTPROTO_HOST, config.TG_MTPROTO_PORT, config.TG_MTPROTO_SECRET)
    return TelegramClient(SESSION_COPY, config.TG_API_ID, config.TG_API_HASH, **kwargs)


async def manual_join(client, username):
    """Вступление + mute + архив без проверки лимита (фолбэк)."""
    entity = await client.get_entity(username)
    await client(JoinChannelRequest(entity))
    await db.record_chat_join(username)
    await asyncio.sleep(2)
    await client(UpdateNotifySettingsRequest(
        peer=entity,
        settings=InputPeerNotifySettings(
            mute_until=2 ** 31 - 1, show_previews=False, silent=True),
    ))
    peer = await client.get_input_entity(entity)
    await client(EditPeerFoldersRequest(
        folder_peers=[InputFolderPeer(peer=peer, folder_id=1)]))
    return entity


async def main():
    shutil.copy(f"/opt/lead-hunter/{config.TG_SESSION_NAME}.session", SESSION_COPY)
    client = build_client()
    await client.connect()
    if not await client.is_user_authorized():
        print("ERROR: сессия не авторизована", flush=True)
        return

    my_ids = set()
    async for d in client.iter_dialogs():
        if d.entity:
            my_ids.add(d.entity.id)
    print(f"[info] аккаунт состоит в {len(my_ids)} диалогах", flush=True)

    for u in USERNAMES:
        print(f"--- @{u}", flush=True)
        try:
            entity = await client.get_entity(u)
        except Exception as e:
            print(f"  [FAIL] не резолвится: {e}", flush=True)
            continue

        title = getattr(entity, "title", u)
        chat_id = utils.get_peer_id(entity)

        if entity.id in my_ids:
            print(f"  [skip] уже состоим — просто включаю мониторинг", flush=True)
        else:
            ok = await chat_finder.join_and_organize_chat(
                client, {"username": u}, folder="freelance")
            if not ok:
                print("  [info] join_and_organize вернул False (лимит?) — вступаю вручную", flush=True)
                try:
                    await manual_join(client, u)
                    print("  [ok] вступил вручную + mute + архив", flush=True)
                except Exception as e:
                    print(f"  [FAIL] вступление: {e}", flush=True)
                    continue
            else:
                print("  [ok] вступил + mute + архив + папка", flush=True)

        await db.upsert_dialog_chat(chat_id, title)
        await db.update_chat(chat_id=chat_id, chat_name=title,
                             is_monitored=True, is_broadcast=False)
        print(f"  [db] chat_id={chat_id} is_monitored=1", flush=True)
        await asyncio.sleep(3)

    chats = await db.get_monitored_chats()
    print(f"\nИТОГ: мониторится чатов в БД: {len(chats)}", flush=True)
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
