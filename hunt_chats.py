"""Разовый поиск живых чатов с потенциальными клиентами.

Запуск на сервере: .venv/bin/python hunt_chats.py
- ищет публичные группы по RU+EN ключевым словам (contacts.search + searchGlobal)
- исключает чаты, где аккаунт уже состоит, и уже мониторируемые
- проверяет активность: последние сообщения, сообщений за 48ч, сигналы лидов
- только чтение: ничего не отправляет и никуда не вступает
"""
import asyncio
import os
import re
import shutil
import sys
from datetime import datetime, timezone, timedelta

os.chdir("/opt/lead-hunter")
sys.path.insert(0, "/opt/lead-hunter")

import config
import database as db
from telethon import TelegramClient, connection
from telethon.tl.functions.contacts import SearchRequest as ContactsSearchRequest
from telethon.tl.functions.messages import SearchGlobalRequest
from telethon.tl.types import InputMessagesFilterEmpty, InputPeerEmpty

SESSION_COPY = "/opt/lead-hunter/hunt_temp.session"
MIN_MEMBERS = 300
MAX_CANDIDATES_TO_SCAN = 70
SAMPLE_MESSAGES = 30
ACTIVITY_WINDOW_H = 48
MIN_MSGS_IN_WINDOW = 8

KEYWORDS = [
    # RU
    "ищу разработчика", "нужен сайт", "заказ на разработку",
    "ищу программиста", "разработка сайта", "телеграм бот на заказ",
    "фриланс заказы", "заказы на разработку", "бизнес чат",
    "предприниматели чат", "нетворкинг предприниматели", "стартап чат",
    "автоматизация бизнеса", "нужен парсер", "ищу веб разработчика",
    # EN
    "looking for developer", "hire developer", "freelance jobs",
    "need a website", "web development", "startup founders",
    "entrepreneurs chat", "telegram bot development",
    "mobile app development", "business chat",
]

JUNK = re.compile(
    r"(?i)(казино|ставк|букмекер|порно|18\+|xxx|секс|наркот|знакомств|dating|"
    r"pump|airdrop|промокод|розыгрыш|крипт|трейдинг|форекс|darknet|даркнет|"
    r"кардинг|вбив|залив|обнал|логи|фальш|подделк|документы купить|"
    r"наращивание|отзывы накрутка|музыка|фильмы|аниме|игры|турики|мотивация|"
    r"психолог|эзотерик|гороскоп|ставим|лайки|подписчики|раскрутка)"
)

LEAD_SIGNAL = re.compile(
    r"(?i)(ищу|нужен|нужна|нужно|требуется|кто может|кто сделает|заказать|"
    r"бюджет|тз\b|looking for|need|hire|budget)"
)


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


async def search_candidates(client) -> dict:
    found = {}
    for kw in KEYWORDS:
        chats = []
        try:
            r = await client(ContactsSearchRequest(q=kw, limit=100))
            chats.extend(r.chats)
        except Exception as e:
            print(f"[warn] contacts.search '{kw}': {e}", flush=True)
        try:
            r = await client(SearchGlobalRequest(
                q=kw, filter=InputMessagesFilterEmpty(), min_date=0, max_date=0,
                offset_rate=0, offset_peer=InputPeerEmpty(), offset_id=0, limit=100))
            existing = {c.id for c in chats}
            chats.extend(c for c in r.chats if c.id not in existing)
        except Exception as e:
            print(f"[warn] searchGlobal '{kw}': {e}", flush=True)

        for c in chats:
            username = getattr(c, "username", None)
            if not username or c.id in found:
                continue
            # Только живые группы (мегагруппы/базовые), не каналы-односторонние
            is_megagroup = getattr(c, "megagroup", False)
            is_broadcast = getattr(c, "broadcast", False) and not is_megagroup
            if is_broadcast:
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
        print(f"[search] '{kw}' -> всего кандидатов: {len(found)}", flush=True)
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
    lead_hits = sum(1 for m in in_window if m.text and LEAD_SIGNAL.search(m.text))
    service_hits = sum(
        1 for m in in_window
        if m.text and re.search(
            r"(?i)(сайт|бот|приложен|автоматизац|парсер|скрипт|crm|интеграц|api|сервер|linux|docker|website|app|bot|automation)",
            m.text))
    return {
        "last_age_h": round((now - last).total_seconds() / 3600, 1),
        "msgs_48h": len(in_window),
        "lead_hits": lead_hits,
        "service_hits": service_hits,
    }


async def main():
    shutil.copy(f"/opt/lead-hunter/{config.TG_SESSION_NAME}.session", SESSION_COPY)
    client = build_client()
    await client.connect()
    if not await client.is_user_authorized():
        print("ERROR: копия сессии не авторизована")
        return

    my_ids = set()
    async for d in client.iter_dialogs():
        if d.entity:
            my_ids.add(d.entity.id)
    monitored = {c["chat_id"] for c in await db.get_monitored_chats()}
    monitored_unmarked = {int(str(cid)[4:]) for cid in monitored if str(cid).startswith("-100")}
    print(f"[info] состоит в диалогах: {len(my_ids)}, мониторится: {len(monitored)}", flush=True)

    candidates = await search_candidates(client)
    fresh = [
        c for c in candidates.values()
        if c["id"] not in my_ids and c["id"] not in monitored_unmarked
    ]
    fresh.sort(key=lambda c: -c["members"])
    fresh = fresh[:MAX_CANDIDATES_TO_SCAN]
    print(f"[info] новых кандидатов для проверки активности: {len(fresh)}", flush=True)

    results = []
    for c in fresh:
        act = await check_activity(client, c["username"])
        if not act or "err" in act:
            print(f"  [skip] @{c['username']}: {act.get('err') if act else '?'}", flush=True)
            await asyncio.sleep(1)
            continue
        c.update(act)
        flag = "HOT" if act["last_age_h"] <= 24 and act["msgs_48h"] >= MIN_MSGS_IN_WINDOW else "cold"
        print(
            f"  [{flag}] @{c['username']} | {c['title'][:45]} | {c['members']} уч. | "
            f"{act['msgs_48h']} сообщ./48ч | последнее {act['last_age_h']}ч назад | "
            f"лидов-сигналов {act['lead_hits']} | IT-сигналов {act['service_hits']}",
            flush=True)
        if flag == "HOT":
            results.append(c)
        await asyncio.sleep(1.5)

    results.sort(key=lambda c: (-c["msgs_48h"], -c["lead_hits"]))
    print("\n===== ИТОГ: ЖИВЫЕ ЧАТЫ (активность за 48ч) =====", flush=True)
    for c in results:
        print(
            f"https://t.me/{c['username']} | {c['title'][:55]} | {c['members']} уч. | "
            f"{c['msgs_48h']} сообщ./48ч | последнее {c['last_age_h']}ч | "
            f"сигналов {c['lead_hits']}/IT {c['service_hits']} | kw: {c['kw']}",
            flush=True)
    print(f"\nВсего живых: {len(results)} из {len(fresh)} проверенных", flush=True)
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
