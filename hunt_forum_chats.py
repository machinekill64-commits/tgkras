"""Разовый поиск чатов с аудиторией форума «Современная женщина» (Краснодар).

Запуск: python hunt_forum_chats.py
- ищет публичные группы по CHAT_SEARCH_KEYWORDS (contacts.search + searchGlobal)
- только группы (megagroup), где можно писать — каналы-односторонние пропускаем
- исключает чаты, где аккаунт уже состоит, и уже найденные ранее (found_chats)
- проверяет активность: сообщений за 48ч, возраст последнего, сигналы ЦА форума
- только чтение: ничего не отправляет и никуда не вступает
"""
import asyncio
import os
import re
from datetime import datetime, timezone, timedelta

import config
import database as db
from telethon import TelegramClient
from telethon.tl.functions.contacts import SearchRequest as ContactsSearchRequest
from telethon.tl.functions.messages import SearchGlobalRequest
from telethon.tl.types import InputMessagesFilterEmpty, InputPeerEmpty

MIN_MEMBERS = 200
MAX_CANDIDATES_TO_SCAN = 60
SAMPLE_MESSAGES = 30
ACTIVITY_WINDOW_H = 48
MIN_MSGS_IN_WINDOW = 8

JUNK = re.compile(
    r"(?i)(казино|ставк|букмекер|порно|18\+|xxx|секс|наркот|знакомств|dating|"
    r"pump|airdrop|крипт|трейдинг|форекс|darknet|даркнет|кардинг|обнал|"
    r"документы купить|накрутка|музыка|фильмы|аниме|игры|гороскоп)"
)

# Сигналы целевой аудитории форума: женщины-предприниматели, бьюти, эксперты,
# мероприятия и нетворкинг в Краснодаре
TARGET_SIGNAL = re.compile(
    r"(?i)(женщин|девушк|мамы|мамочк|предпринимател|бизнес|нетворкинг|"
    r"мастер|косметолог|визажист|салон|бьюти|психолог|коуч|эксперт|"
    r"мероприят|форум|тренинг|встреч|краснодар|крд\b|самозанят|клиенты)"
)

# Признак локальности: Краснодар/Кубань в названии, username или сообщениях.
# Нужен именно краснодарский чат — федеральные (эксперты, WB) отсеиваем.
KRD_SIGNAL = re.compile(r"(?i)(краснодар|крд\b|krd\b|kuban|кубань|кубан|krasnodar)")


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
        from telethon import connection
        kwargs["connection"] = connection.ConnectionTcpMTProxyRandomizedIntermediate
        kwargs["proxy"] = (config.TG_MTPROTO_HOST, config.TG_MTPROTO_PORT, config.TG_MTPROTO_SECRET)
    return TelegramClient(config.TG_SESSION_NAME, config.TG_API_ID, config.TG_API_HASH, **kwargs)


async def search_candidates(client) -> dict:
    found = {}
    for kw in config.CHAT_SEARCH_KEYWORDS:
        chats = []
        try:
            r = await client(ContactsSearchRequest(q=kw, limit=100))
            chats.extend(r.chats)
        except Exception as e:
            print(f"[warn] contacts.search '{kw}': {e}", flush=True)
        try:
            r = await client(SearchGlobalRequest(
                q=kw, filter=InputMessagesFilterEmpty(), min_date=0, max_date=0,
                offset_rate=0, offset_peer=InputPeerEmpty(), offset_id=0, limit=50))
            existing = {c.id for c in chats}
            chats.extend(c for c in r.chats if c.id not in existing)
        except Exception as e:
            print(f"[warn] searchGlobal '{kw}': {e}", flush=True)

        for c in chats:
            username = getattr(c, "username", None)
            if not username or c.id in found:
                continue
            # Только живые группы — нам нужны чаты, где пишут люди
            if not getattr(c, "megagroup", False):
                continue
            title = c.title or ""
            if JUNK.search(title) or JUNK.search(username):
                continue
            members = getattr(c, "participants_count", 0) or 0
            if members < MIN_MEMBERS:
                continue
            found[c.id] = {
                "id": c.id, "title": title, "username": username,
                "members": members, "kw": kw,
            }
        print(f"[search] '{kw}' -> кандидатов: {len(found)}", flush=True)
        await asyncio.sleep(1.5)
    return found


async def check_activity(client, username: str) -> dict | None:
    try:
        entity = await client.get_entity(username)
    except Exception as e:
        return {"err": f"entity: {e}"}
    now = datetime.now(timezone.utc)
    msgs = []
    try:
        async for m in client.iter_messages(entity, limit=SAMPLE_MESSAGES):
            if m.date:
                msgs.append(m)
    except Exception as e:
        return {"err": f"messages: {e}"}
    if not msgs:
        return {"err": "no messages"}
    last = msgs[0].date
    in_window = [m for m in msgs if now - m.date <= timedelta(hours=ACTIVITY_WINDOW_H)]
    target_hits = sum(1 for m in in_window if m.text and TARGET_SIGNAL.search(m.text))
    krd_hits = sum(1 for m in msgs if m.text and KRD_SIGNAL.search(m.text))
    return {
        "last_age_h": round((now - last).total_seconds() / 3600, 1),
        "msgs_48h": len(in_window),
        "target_hits": target_hits,
        "krd_hits": krd_hits,
    }


async def main():
    await db.init_db()
    client = build_client()
    await client.connect()
    if not await client.is_user_authorized():
        print("ERROR: сессия не авторизована")
        return

    my_ids = set()
    async for d in client.iter_dialogs():
        if d.entity:
            my_ids.add(d.entity.id)
    print(f"[info] состоит в диалогах: {len(my_ids)}", flush=True)

    candidates = await search_candidates(client)
    fresh = [c for c in candidates.values() if c["id"] not in my_ids]
    fresh.sort(key=lambda c: -c["members"])
    fresh = fresh[:MAX_CANDIDATES_TO_SCAN]
    print(f"[info] новых кандидатов для проверки: {len(fresh)}", flush=True)

    results = []
    for c in fresh:
        act = await check_activity(client, c["username"])
        if not act or "err" in act:
            print(f"  [skip] @{c['username']}: {act.get('err') if act else '?'}", flush=True)
            await asyncio.sleep(1)
            continue
        c.update(act)
        # Локальность: Краснодар в названии/username ИЛИ упоминания в сообщениях
        is_local = bool(
            KRD_SIGNAL.search(c["title"]) or KRD_SIGNAL.search(c["username"])
            or act["krd_hits"] >= 2)
        hot = act["last_age_h"] <= 24 and act["msgs_48h"] >= MIN_MSGS_IN_WINDOW
        flag = "HOT" if hot else "cold"
        loc = "KRD" if is_local else "fed"
        print(
            f"  [{flag}/{loc}] @{c['username']} | {c['title'][:45]} | {c['members']} уч. | "
            f"{act['msgs_48h']} сообщ./48ч | ЦА {act['target_hits']} | КРД {act['krd_hits']}",
            flush=True)
        if hot and is_local:
            results.append(c)
        await asyncio.sleep(1.5)

    results.sort(key=lambda c: (-c["krd_hits"], -c["msgs_48h"]))
    print("\n===== ИТОГ: ЖИВЫЕ КРАСНОДАРСКИЕ ЧАТЫ =====", flush=True)
    for c in results:
        print(
            f"https://t.me/{c['username']} | {c['title'][:55]} | {c['members']} уч. | "
            f"{c['msgs_48h']} сообщ./48ч | ЦА {c['target_hits']} | КРД {c['krd_hits']} | kw: {c['kw']}",
            flush=True)
    print(f"\nВсего подходящих: {len(results)} из {len(fresh)} проверенных", flush=True)
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
