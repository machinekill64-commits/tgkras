"""Вступление аккаунта в harvest-чаты пачкой + регистрация + бэкфилл лидов.

Запуск на сервере:
    python join_harvest.py --num 3 --chats krasnodar_chati,chat_krasnodar1

Грузит accN.env поверх .env (для acc1 env-файла нет — берётся .env).
Для каждого чата: вступает (mute+archive), is_harvest=1, бэкфиллит
последние сообщения в outreach_targets. Между вступлениями пауза 60-90с —
новым аккаунтам нельзя вступать пачкой без разрывов.
ВАЖНО: сессия занята работающим воркером — сервис должен быть остановлен.
"""
import argparse
import asyncio
import random
import sys

from dotenv import load_dotenv


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--num", type=int, required=True, help="Номер аккаунта")
    p.add_argument("--chats", required=True,
                   help="Username чатов через запятую (без @)")
    p.add_argument("--join-delay", type=int, default=75,
                   help="Пауза между вступлениями (сек, ±30%%)")
    p.add_argument("--max-joins", type=int, default=5,
                   help="Макс. вступлений за запуск (антибан)")
    return p.parse_args()


async def main():
    args = parse_args()
    # accN.env приоритетнее .env — dotenv не перезаписывает существующие
    import os
    if os.path.exists(f"acc{args.num}.env"):
        load_dotenv(f"acc{args.num}.env")
    load_dotenv(".env")

    import config
    import database as db
    from user_client import UserClient
    from telethon.tl.functions.channels import JoinChannelRequest
    from telethon.tl.functions.account import UpdateNotifySettingsRequest
    from telethon.tl.functions.folders import EditPeerFoldersRequest
    from telethon.tl.types import (InputPeerNotifySettings, InputFolderPeer)
    from first_lead import backfill_candidates

    usernames = [u.strip().lstrip("@") for u in args.chats.split(",") if u.strip()]
    print(f"[join_harvest] acc{args.num} ({config.TG_SESSION_NAME}): "
          f"{len(usernames)} чатов, лимит {args.max_joins}", flush=True)

    await db.init_db()
    uc = UserClient()
    await uc.client.connect()
    if not await uc.client.is_user_authorized():
        print("[err] сессия не авторизована", flush=True)
        return
    me = await uc.client.get_me()
    uc._me_id = me.id
    print(f"[me] {me.first_name} {me.last_name or ''} @{me.username}", flush=True)

    joined = 0
    for username in usernames:
        if joined >= args.max_joins:
            print(f"[limit] достигнут лимит {args.max_joins} — остальные завтра",
                  flush=True)
            break
        try:
            entity = await uc.client.get_entity(username)
            await uc.client(JoinChannelRequest(entity))
            await db.record_chat_join(username)
            joined += 1
            print(f"[join] @{username} OK ({joined})", flush=True)
        except Exception as e:
            print(f"[join] @{username} FAIL: {type(e).__name__}: {e}", flush=True)
            continue
        try:
            await uc.client(UpdateNotifySettingsRequest(
                peer=entity,
                settings=InputPeerNotifySettings(
                    mute_until=2 ** 31 - 1, show_previews=False, silent=True)))
            peer = await uc.client.get_input_entity(entity)
            await uc.client(EditPeerFoldersRequest(
                folder_peers=[InputFolderPeer(peer=peer, folder_id=1)]))
        except Exception as e:
            print(f"[warn] mute/archive @{username}: {e}", flush=True)

        chat_id_db = int(f"-100{entity.id}")
        try:
            await db.add_chat(chat_id_db, entity.title or username)
            await db.update_chat(chat_id_db, is_harvest=True)
            n = await backfill_candidates(uc, entity, chat_id_db,
                                          entity.title or username)
            print(f"[harvest] @{username} -> {n} кандидатов", flush=True)
        except Exception as e:
            print(f"[err] harvest @{username}: {e}", flush=True)

        delay = args.join_delay * random.uniform(0.7, 1.3)
        print(f"[wait] {delay:.0f}с до следующего вступления...", flush=True)
        await asyncio.sleep(delay)

    await uc.client.disconnect()
    print(f"[done] acc{args.num}: вступлено {joined}/{len(usernames)}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
