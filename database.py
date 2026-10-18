"""SQLite база данных: схема и асинхронные операции."""
import aiosqlite
from datetime import datetime
from pathlib import Path
from config import DB_PATH


SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY,
    chat_name TEXT NOT NULL,
    is_monitored INTEGER DEFAULT 1,
    last_read_message_id INTEGER DEFAULT 0,
    added_at TEXT DEFAULT (datetime('now')),
    schedule_cron TEXT,
    message_text TEXT,
    message_variants TEXT
);

CREATE TABLE IF NOT EXISTS messages_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    sender_id INTEGER,
    sender_name TEXT,
    message_text TEXT,
    message_id INTEGER NOT NULL,
    direction TEXT NOT NULL,
    filter_verdict TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    chat_name TEXT,
    sender_id INTEGER NOT NULL,
    sender_name TEXT,
    sender_username TEXT,
    message_text TEXT,
    category TEXT NOT NULL,
    task TEXT,
    budget TEXT,
    deadline TEXT,
    market_price TEXT,
    market_deadline TEXT,
    lead_score INTEGER DEFAULT 0,
    source_message_id INTEGER,
    source_url TEXT,
    sender_type TEXT,
    contact_username TEXT,
    contact_source TEXT,
    service TEXT,
    evidence TEXT,
    status TEXT DEFAULT 'NEW',
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (chat_id) REFERENCES chats(chat_id)
);

CREATE TABLE IF NOT EXISTS dialogs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL,
    sender_id INTEGER,
    sender_name TEXT,
    sender_username TEXT,
    lead_source TEXT DEFAULT 'regular',
    stage TEXT DEFAULT 'INITIATING',
    ai_messages_json TEXT DEFAULT '[]',
    price TEXT,
    followup_count INTEGER DEFAULT 0,
    last_followup_at TEXT,
    ab_variant INTEGER DEFAULT 0,
    country TEXT,
    client_name TEXT,
    company TEXT,
    contact TEXT,
    service_codes_json TEXT DEFAULT '[]',
    requirements_json TEXT DEFAULT '[]',
    brief_text TEXT,
    sales_intent TEXT,
    interest_level TEXT DEFAULT 'COLD',
    awaiting_owner_price INTEGER DEFAULT 0,
    country_asked INTEGER DEFAULT 0,
    handoff_notified INTEGER DEFAULT 0,
    ready_to_work INTEGER DEFAULT 0,
    do_not_contact INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (lead_id) REFERENCES leads(id)
);

CREATE TABLE IF NOT EXISTS ab_message_stats (
    variant INTEGER PRIMARY KEY,
    sent_count INTEGER DEFAULT 0,
    replied_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS users_seen (
    sender_id INTEGER PRIMARY KEY,
    first_contact_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS anti_ban_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    hour INTEGER NOT NULL,
    actions_count INTEGER DEFAULT 0,
    UNIQUE(date, hour)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS cold_leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    chat_name TEXT,
    sender_id INTEGER NOT NULL,
    sender_name TEXT,
    sender_username TEXT,
    message_text TEXT,
    category TEXT NOT NULL,
    business_type TEXT,
    pain TEXT,
    hook TEXT,
    market_price TEXT,
    market_deadline TEXT,
    lead_score INTEGER DEFAULT 0,
    source_message_id INTEGER,
    source_url TEXT,
    sender_type TEXT,
    contact_username TEXT,
    contact_source TEXT,
    service TEXT,
    evidence TEXT,
    status TEXT DEFAULT 'NEW',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS gis_companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT,
    address TEXT,
    category TEXT,
    city TEXT,
    has_website INTEGER DEFAULT 0,
    telegram_id INTEGER,
    telegram_username TEXT,
    status TEXT DEFAULT 'NEW',
    generated_message TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS cold_outreach_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    target_id INTEGER NOT NULL,
    target_name TEXT,
    message_text TEXT,
    status TEXT,
    sent_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS found_chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    title TEXT,
    username TEXT,
    participants_count INTEGER DEFAULT 0,
    is_channel INTEGER DEFAULT 0,
    is_group INTEGER DEFAULT 0,
    search_keyword TEXT,
    found_date TEXT DEFAULT (date('now')),
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(chat_id)
);

CREATE TABLE IF NOT EXISTS outreach_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    chat_name TEXT,
    sender_id INTEGER NOT NULL,
    sender_name TEXT,
    sender_username TEXT,
    last_message_text TEXT,
    msg_count INTEGER DEFAULT 1,
    sample_texts TEXT,
    first_seen_at TEXT DEFAULT (datetime('now')),
    last_seen_at TEXT DEFAULT (datetime('now')),
    status TEXT DEFAULT 'NEW',
    sent_at TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(sender_id)
);
"""


async def init_db():
    """Инициализация БД: создание директории, таблиц и миграции."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.executescript(SCHEMA)
        await _migrate_chats(db)
        await _migrate_messages_log(db)
        await _migrate_leads(db)
        await _migrate_cold_leads(db)
        await _migrate_dialogs(db)
        await _migrate_outreach_targets(db)
        await db.commit()


async def _migrate_chats(db):
    """Добавляет новые колонки в chats, если их ещё нет."""
    async with db.execute("PRAGMA table_info(chats)") as cursor:
        columns = {row[1] for row in await cursor.fetchall()}
    if "is_broadcast" not in columns:
        await db.execute("ALTER TABLE chats ADD COLUMN is_broadcast INTEGER DEFAULT 0")
    if "chat_rules" not in columns:
        await db.execute("ALTER TABLE chats ADD COLUMN chat_rules TEXT")
    if "broadcast_times" not in columns:
        await db.execute("ALTER TABLE chats ADD COLUMN broadcast_times TEXT")
    if "is_direct_promo" not in columns:
        await db.execute("ALTER TABLE chats ADD COLUMN is_direct_promo INTEGER DEFAULT 0")
    if "chat_niche" not in columns:
        await db.execute("ALTER TABLE chats ADD COLUMN chat_niche TEXT")
    if "broadcast_language" not in columns:
        await db.execute("ALTER TABLE chats ADD COLUMN broadcast_language TEXT DEFAULT 'ru'")
    if "is_harvest" not in columns:
        await db.execute("ALTER TABLE chats ADD COLUMN is_harvest INTEGER DEFAULT 0")


async def _migrate_messages_log(db):
    """Добавляет новые колонки в messages_log, если их ещё нет."""
    async with db.execute("PRAGMA table_info(messages_log)") as cursor:
        columns = {row[1] for row in await cursor.fetchall()}
    if "filter_verdict" not in columns:
        await db.execute("ALTER TABLE messages_log ADD COLUMN filter_verdict TEXT")


async def _migrate_leads(db):
    """Добавляет новые колонки в leads, если их ещё нет."""
    async with db.execute("PRAGMA table_info(leads)") as cursor:
        columns = {row[1] for row in await cursor.fetchall()}
    if "market_price" not in columns:
        await db.execute("ALTER TABLE leads ADD COLUMN market_price TEXT")
    if "market_deadline" not in columns:
        await db.execute("ALTER TABLE leads ADD COLUMN market_deadline TEXT")
    additions = {"lead_score": "INTEGER DEFAULT 0", "source_message_id": "INTEGER",
                 "source_url": "TEXT", "sender_type": "TEXT", "contact_username": "TEXT",
                 "contact_source": "TEXT", "service": "TEXT", "evidence": "TEXT"}
    for name, definition in additions.items():
        if name not in columns:
            await db.execute(f"ALTER TABLE leads ADD COLUMN {name} {definition}")


async def _migrate_cold_leads(db):
    """Добавляет новые колонки в cold_leads, если их ещё нет."""
    async with db.execute("PRAGMA table_info(cold_leads)") as cursor:
        columns = {row[1] for row in await cursor.fetchall()}
    if "market_price" not in columns:
        await db.execute("ALTER TABLE cold_leads ADD COLUMN market_price TEXT")
    if "market_deadline" not in columns:
        await db.execute("ALTER TABLE cold_leads ADD COLUMN market_deadline TEXT")
    additions = {"lead_score": "INTEGER DEFAULT 0", "source_message_id": "INTEGER",
                 "source_url": "TEXT", "sender_type": "TEXT", "contact_username": "TEXT",
                 "contact_source": "TEXT", "service": "TEXT", "evidence": "TEXT"}
    for name, definition in additions.items():
        if name not in columns:
            await db.execute(f"ALTER TABLE cold_leads ADD COLUMN {name} {definition}")


async def _migrate_outreach_targets(db):
    """Добавляет новые колонки в outreach_targets, если их ещё нет."""
    async with db.execute("PRAGMA table_info(outreach_targets)") as cursor:
        columns = {row[1] for row in await cursor.fetchall()}
    for name, definition in (("msg_count", "INTEGER DEFAULT 1"),
                             ("sample_texts", "TEXT"),
                             ("first_seen_at", "TEXT"),
                             ("claimed_by", "TEXT")):
        if name not in columns:
            await db.execute(f"ALTER TABLE outreach_targets ADD COLUMN {name} {definition}")


async def _migrate_dialogs(db):
    """Добавляет новые колонки в dialogs, если их ещё нет."""
    async with db.execute("PRAGMA table_info(dialogs)") as cursor:
        columns = {row[1] for row in await cursor.fetchall()}
    if "followup_count" not in columns:
        await db.execute("ALTER TABLE dialogs ADD COLUMN followup_count INTEGER DEFAULT 0")
    if "last_followup_at" not in columns:
        await db.execute("ALTER TABLE dialogs ADD COLUMN last_followup_at TEXT")
    if "ab_variant" not in columns:
        await db.execute("ALTER TABLE dialogs ADD COLUMN ab_variant INTEGER DEFAULT 0")
    additions = {
        "lead_source": "TEXT DEFAULT 'regular'",
        "country": "TEXT",
        "client_name": "TEXT",
        "company": "TEXT",
        "contact": "TEXT",
        "service_codes_json": "TEXT DEFAULT '[]'",
        "requirements_json": "TEXT DEFAULT '[]'",
        "brief_text": "TEXT",
        "sales_intent": "TEXT",
        "interest_level": "TEXT DEFAULT 'COLD'",
        "awaiting_owner_price": "INTEGER DEFAULT 0",
        "country_asked": "INTEGER DEFAULT 0",
        "handoff_notified": "INTEGER DEFAULT 0",
        "ready_to_work": "INTEGER DEFAULT 0",
        "do_not_contact": "INTEGER DEFAULT 0",
    }
    for name, definition in additions.items():
        if name not in columns:
            await db.execute(f"ALTER TABLE dialogs ADD COLUMN {name} {definition}")


# === Chats ===

async def add_chat(chat_id: int, chat_name: str, schedule_cron: str = None,
                   message_text: str = None, message_variants: list[str] = None):
    import json
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR IGNORE INTO chats (chat_id, chat_name, schedule_cron, message_text, message_variants)
               VALUES (?, ?, ?, ?, ?)""",
            (chat_id, chat_name, schedule_cron, message_text,
             json.dumps(message_variants, ensure_ascii=False) if message_variants else None)
        )
        await db.commit()


async def update_chat_schedule(chat_id: int, schedule_cron: str,
                               message_text: str, message_variants: list[str] = None):
    import json
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE chats SET schedule_cron = ?, message_text = ?, message_variants = ?
               WHERE chat_id = ?""",
            (schedule_cron, message_text,
             json.dumps(message_variants, ensure_ascii=False) if message_variants else None,
             chat_id)
        )
        await db.commit()


async def get_monitored_chats() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM chats WHERE is_monitored = 1") as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_all_chats() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM chats ORDER BY chat_name") as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def upsert_dialog_chat(chat_id: int, chat_name: str):
    """Синхронизация чата из списка диалогов Telegram.

    Новые чаты вставляются с is_monitored=0 (default в схеме = 1 —
    иначе синк всех диалогов включил бы мониторинг везде).
    У существующих обновляется только имя.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO chats (chat_id, chat_name, is_monitored)
               VALUES (?, ?, 0)
               ON CONFLICT(chat_id) DO UPDATE SET chat_name = excluded.chat_name""",
            (chat_id, chat_name)
        )
        await db.commit()


async def update_chat_flags(chat_id: int, is_monitored: int, is_broadcast: int,
                            chat_rules: str | None, broadcast_times: str | None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE chats SET is_monitored = ?, is_broadcast = ?,
               chat_rules = ?, broadcast_times = ? WHERE chat_id = ?""",
            (is_monitored, is_broadcast, chat_rules, broadcast_times, chat_id)
        )
        await db.commit()


async def update_chat(chat_id: int, chat_name: str = None, is_monitored: bool = None,
                      is_broadcast: bool = None, chat_rules: str = None,
                      broadcast_times: str = None, message_text: str = None,
                      message_variants: list[str] = None, schedule_cron: str = None,
                      is_direct_promo: bool = None, chat_niche: str = None,
                      broadcast_language: str = None, is_harvest: bool = None):
    """Полное обновление чата (для API синхронизации с GUI)."""
    import json
    async with aiosqlite.connect(DB_PATH) as db:
        updates = []
        params = []

        if chat_name is not None:
            updates.append("chat_name = ?")
            params.append(chat_name)
        if is_monitored is not None:
            updates.append("is_monitored = ?")
            params.append(1 if is_monitored else 0)
        if is_broadcast is not None:
            updates.append("is_broadcast = ?")
            params.append(1 if is_broadcast else 0)
        if chat_rules is not None:
            updates.append("chat_rules = ?")
            params.append(chat_rules)
        if broadcast_times is not None:
            updates.append("broadcast_times = ?")
            params.append(broadcast_times)
        if message_text is not None:
            updates.append("message_text = ?")
            params.append(message_text)
        if message_variants is not None:
            updates.append("message_variants = ?")
            params.append(json.dumps(message_variants, ensure_ascii=False))
        if schedule_cron is not None:
            updates.append("schedule_cron = ?")
            params.append(schedule_cron)
        if is_direct_promo is not None:
            updates.append("is_direct_promo = ?")
            params.append(1 if is_direct_promo else 0)
        if chat_niche is not None:
            updates.append("chat_niche = ?")
            params.append(chat_niche)
        if broadcast_language is not None:
            updates.append("broadcast_language = ?")
            params.append(broadcast_language)
        if is_harvest is not None:
            updates.append("is_harvest = ?")
            params.append(1 if is_harvest else 0)
        
        if updates:
            params.append(chat_id)
            query = f"UPDATE chats SET {', '.join(updates)} WHERE chat_id = ?"
            await db.execute(query, params)
            await db.commit()


async def get_harvest_chat_ids() -> set[int]:
    """ID чатов, из которых собираем активных участников для рассылки приглашений."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT chat_id FROM chats WHERE is_harvest = 1") as cursor:
            return {row[0] for row in await cursor.fetchall()}


async def upsert_outreach_target(chat_id: int, chat_name: str, sender_id: int,
                                 sender_name: str, sender_username: str | None,
                                 message_text: str):
    """Запоминает активного участника чата как кандидата на приглашение.
    Дедуп по sender_id: повторное сообщение обновляет last_seen, статус не трогает.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO outreach_targets
               (chat_id, chat_name, sender_id, sender_name, sender_username,
                last_message_text, sample_texts)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(sender_id) DO UPDATE SET
                   last_seen_at = datetime('now'),
                   msg_count = outreach_targets.msg_count + 1,
                   last_message_text = excluded.last_message_text,
                   sample_texts = substr(
                       COALESCE(outreach_targets.sample_texts, '') || char(10) || excluded.sample_texts,
                       -800),
                   chat_id = excluded.chat_id,
                   chat_name = excluded.chat_name,
                   sender_name = excluded.sender_name,
                   sender_username = COALESCE(excluded.sender_username, outreach_targets.sender_username)
               WHERE outreach_targets.status = 'NEW'""",
            (chat_id, chat_name, sender_id, sender_name, sender_username,
             (message_text or "")[:300], (message_text or "")[:200]),
        )
        await db.commit()


async def get_pending_outreach_targets(limit: int = 10,
                                       min_messages: int = 3,
                                       recency_days: int = 3) -> list[dict]:
    """Кандидаты, прошедшие скоринг: >= min_messages сообщений
    и последняя активность не старше recency_days."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM outreach_targets
               WHERE status = 'NEW'
                 AND msg_count >= ?
                 AND datetime(last_seen_at) >= datetime('now', ?)
               ORDER BY last_seen_at DESC LIMIT ?""",
            (min_messages, f"-{recency_days} days", limit),
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def claim_outreach_target(target_id: int, claimer: str) -> bool:
    """Атомарный захват кандидата перед отправкой (мульти-аккаунт).
    True — цель захвачена этим процессом, False — уже занята другим."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """UPDATE outreach_targets
               SET status = 'CLAIMED', claimed_by = ?
               WHERE id = ? AND status = 'NEW'""",
            (claimer, target_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def release_outreach_target(target_id: int):
    """Вернуть захваченного кандидата в NEW (если отправка не началась)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE outreach_targets SET status = 'NEW' "
            "WHERE id = ? AND status = 'CLAIMED'",
            (target_id,),
        )
        await db.commit()


async def mark_outreach_target(target_id: int, status: str):
    """SENT / FAILED / SKIPPED — фиксация результата отправки."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE outreach_targets SET status = ?, sent_at = datetime('now') WHERE id = ?",
            (status, target_id),
        )
        await db.commit()


async def get_outreach_stats() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT status, COUNT(*) FROM outreach_targets GROUP BY status"
        ) as cursor:
            return {row[0]: row[1] for row in await cursor.fetchall()}


async def get_forum_report() -> dict:
    """Сводка кампании для отчётного бота заказчиков:
    найдено кандидатов, отправлено приглашений, ответили, уговорены."""
    from config import REPORT_EXCLUDE_SENDER_IDS
    excl = ",".join(str(x) for x in REPORT_EXCLUDE_SENDER_IDS) or "0"
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        stats = {}
        async with db.execute(
            "SELECT status, COUNT(*) c FROM outreach_targets GROUP BY status"
        ) as cur:
            stats["targets"] = {r["status"]: r["c"] for r in await cur.fetchall()}
        async with db.execute(
            f"""SELECT COUNT(*) c FROM dialogs
                WHERE lead_source = 'forum_outreach' AND stage != 'FORUM_OPENER'
                  AND sender_id NOT IN ({excl})"""
        ) as cur:
            stats["replied"] = (await cur.fetchone())["c"]
        async with db.execute(
            f"""SELECT COUNT(*) c FROM dialogs
                WHERE lead_source = 'forum_outreach' AND interest_level = 'HOT'
                  AND sender_id NOT IN ({excl})"""
        ) as cur:
            stats["hot"] = (await cur.fetchone())["c"]
        async with db.execute(
            f"""SELECT COUNT(*) c FROM dialogs
                WHERE lead_source = 'forum_outreach' AND stage = 'ENDED'
                  AND sender_id NOT IN ({excl})"""
        ) as cur:
            stats["ended"] = (await cur.fetchone())["c"]
        return stats


async def get_forum_leads(hot_only: bool = False, limit: int = 15) -> list[dict]:
    """Форумные диалоги для отчёта: имя, контакт, стадия, интерес."""
    from config import REPORT_EXCLUDE_SENDER_IDS
    excl = ",".join(str(x) for x in REPORT_EXCLUDE_SENDER_IDS) or "0"
    where = ("WHERE lead_source = 'forum_outreach' "
             f"AND sender_id NOT IN ({excl})")
    if hot_only:
        where += " AND interest_level = 'HOT'"
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"""SELECT sender_id, sender_name, sender_username, stage,
                       interest_level, ready_to_work, updated_at
                FROM dialogs {where}
                ORDER BY updated_at DESC LIMIT ?""",
            (limit,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_broadcast_chats() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM chats WHERE is_broadcast = 1
               AND broadcast_times IS NOT NULL AND broadcast_times != ''"""
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_chat(chat_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM chats WHERE chat_id = ?", (chat_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_recent_broadcasts(chat_id: int, limit: int = 20) -> list[str]:
    """Последние отправленные рассылки в чат (для неповторяемости текстов)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT message_text FROM messages_log
               WHERE chat_id = ? AND direction = 'outgoing_scheduled'
               ORDER BY id DESC LIMIT ?""",
            (chat_id, limit)
        ) as cursor:
            rows = await cursor.fetchall()
            return [r[0] for r in rows if r[0]]


async def get_recent_incoming_messages(chat_id: int, limit: int = 15) -> list[str]:
    """Последние входящие сообщения из чата — для определения ниши AI-рассылкой."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT message_text FROM messages_log
               WHERE chat_id = ? AND direction = 'incoming'
               ORDER BY id DESC LIMIT ?""",
            (chat_id, limit)
        ) as cursor:
            rows = await cursor.fetchall()
            return [r[0] for r in rows if r[0]]


async def get_scheduled_chats() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM chats WHERE schedule_cron IS NOT NULL AND is_monitored = 1"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def update_last_read_message(chat_id: int, message_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE chats SET last_read_message_id = ? WHERE chat_id = ?",
            (message_id, chat_id)
        )
        await db.commit()


async def get_last_read_message(chat_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT last_read_message_id FROM chats WHERE chat_id = ?", (chat_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


async def add_cold_lead(chat_id: int, chat_name: str, sender_id: int | None,
                        sender_name: str, sender_username: str, message_text: str,
                        category: str, business_type: str = None, pain: str = None,
                        hook: str = None, market_price: str = None,
                        market_deadline: str = None, lead_score: int = 0, **metadata) -> int:
    columns = ["chat_id", "chat_name", "sender_id", "sender_name", "sender_username", "message_text", "category", "business_type", "pain", "hook", "market_price", "market_deadline", "lead_score"]
    values = [chat_id, chat_name, sender_id, sender_name, sender_username, message_text, category, business_type, pain, hook, market_price, market_deadline, lead_score]
    for name in ("source_message_id", "source_url", "sender_type", "contact_username", "contact_source", "service", "evidence"):
        if name in metadata:
            columns.append(name); values.append(metadata[name])
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            f"INSERT INTO cold_leads ({', '.join(columns)}) VALUES ({', '.join('?' for _ in values)})", values
        )
        await db.commit()
        return cursor.lastrowid


async def get_cold_lead(lead_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM cold_leads WHERE id = ?", (lead_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def update_cold_lead_status(lead_id: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE cold_leads SET status = ? WHERE id = ?", (status, lead_id)
        )
        await db.commit()


async def import_gis_companies(companies: list[dict]):
    """Импорт компаний из 2GIS. Ожидает список словарей с name, phone, address, category, city."""
    async with aiosqlite.connect(DB_PATH) as db:
        for c in companies:
            await db.execute(
                """INSERT OR IGNORE INTO gis_companies
                   (name, phone, address, category, city, has_website)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (c.get("name"), c.get("phone"), c.get("address"),
                 c.get("category"), c.get("city"), int(c.get("has_website", 0)))
            )
        await db.commit()


async def get_pending_gis_companies(limit: int = 10) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM gis_companies WHERE status = 'NEW' LIMIT ?", (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def update_gis_company_status(company_id: int, status: str,
                                    telegram_id: int = None, telegram_username: str = None,
                                    generated_message: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE gis_companies SET status = ?, telegram_id = ?,
               telegram_username = ?, generated_message = ? WHERE id = ?""",
            (status, telegram_id, telegram_username, generated_message, company_id)
        )
        await db.commit()


async def log_cold_outreach(type: str, target_id: int, target_name: str,
                            message_text: str, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO cold_outreach_log (type, target_id, target_name, message_text, status)
               VALUES (?, ?, ?, ?, ?)""",
            (type, target_id, target_name, message_text, status)
        )
        await db.commit()


async def get_cold_outreach_count_today() -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM cold_outreach_log WHERE DATE(sent_at) = ?",
            (today,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


# === Chat Join Tracking ===

async def record_chat_join(chat_username: str):
    """Записывает вступление в чат для учёта дневного лимита."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS chat_join_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_username TEXT,
                joined_at TEXT DEFAULT (datetime('now'))
            )"""
        )
        await db.execute(
            "INSERT INTO chat_join_log (chat_username) VALUES (?)",
            (chat_username,)
        )
        await db.commit()


async def get_chat_joins_today() -> int:
    """Возвращает количество вступлений в чаты за сегодня."""
    today = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS chat_join_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_username TEXT,
                joined_at TEXT DEFAULT (datetime('now'))
            )"""
        )
        async with db.execute(
            "SELECT COUNT(*) FROM chat_join_log WHERE DATE(joined_at) = ?",
            (today,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


# === Leads ===

async def has_recent_lead_from_sender(sender_id: int, hours: int = 24) -> bool:
    """Проверяет, есть ли уже лид от этого отправителя за последние hours часов."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT 1 FROM leads
               WHERE sender_id = ?
                 AND datetime(created_at) > datetime('now', ?)
               LIMIT 1""",
            (sender_id, f"-{hours} hours")
        ) as cursor:
            return await cursor.fetchone() is not None

async def add_lead(chat_id: int, chat_name: str, sender_id: int | None,
                   sender_name: str, sender_username: str, message_text: str,
                   category: str, task: str = None, budget: str = None,
                   deadline: str = None, market_price: str = None,
                   market_deadline: str = None, lead_score: int = 0, **metadata) -> int:
    columns = ["chat_id", "chat_name", "sender_id", "sender_name", "sender_username", "message_text", "category", "task", "budget", "deadline", "market_price", "market_deadline", "lead_score"]
    values = [chat_id, chat_name, sender_id, sender_name, sender_username, message_text, category, task, budget, deadline, market_price, market_deadline, lead_score]
    for name in ("source_message_id", "source_url", "sender_type", "contact_username", "contact_source", "service", "evidence"):
        if name in metadata:
            columns.append(name); values.append(metadata[name])
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            f"INSERT INTO leads ({', '.join(columns)}) VALUES ({', '.join('?' for _ in values)})", values
        )
        await db.commit()
        return cursor.lastrowid


async def update_lead_status(lead_id: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE leads SET status = ? WHERE id = ?", (status, lead_id)
        )
        await db.commit()


async def get_lead(lead_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


# === Dialogs ===

async def create_dialog(lead_id: int, sender_id: int, sender_name: str,
                        sender_username: str, lead_source: str = "regular") -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO dialogs (lead_id, sender_id, sender_name, sender_username, lead_source)
               VALUES (?, ?, ?, ?, ?)""",
            (lead_id, sender_id, sender_name, sender_username, lead_source)
        )
        await db.commit()
        return cursor.lastrowid


async def get_dialog_by_lead(lead_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM dialogs WHERE lead_id = ?", (lead_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_dialog_by_sender(sender_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM dialogs WHERE sender_id = ? ORDER BY id DESC LIMIT 1",
            (sender_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def update_dialog_stage(dialog_id: int, stage: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE dialogs SET stage = ?, updated_at = datetime('now') WHERE id = ?",
            (stage, dialog_id)
        )
        await db.commit()


async def update_dialog_messages(dialog_id: int, messages_json: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE dialogs SET ai_messages_json = ?, updated_at = datetime('now') WHERE id = ?",
            (messages_json, dialog_id)
        )
        await db.commit()


async def update_dialog_price(dialog_id: int, price: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE dialogs SET price = ?, updated_at = datetime('now') WHERE id = ?",
            (price, dialog_id)
        )
        await db.commit()


async def get_dialog_by_id(dialog_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM dialogs WHERE id = ?", (dialog_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_dialog_by_identifier(identifier: str) -> dict | None:
    value = identifier.strip().lstrip("@")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if value.isdigit():
            query = "SELECT * FROM dialogs WHERE id = ?"
            params = (int(value),)
        else:
            query = "SELECT * FROM dialogs WHERE lower(sender_username) = lower(?) ORDER BY id DESC LIMIT 1"
            params = (value,)
        async with db.execute(query, params) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def update_dialog_sales_data(dialog_id: int, **fields):
    allowed = {
        "country", "client_name", "company", "contact", "service_codes_json",
        "requirements_json", "brief_text", "sales_intent", "interest_level",
        "awaiting_owner_price", "country_asked", "handoff_notified", "ready_to_work",
        "do_not_contact",
    }
    updates = [(name, value) for name, value in fields.items() if name in allowed]
    if not updates:
        return
    assignments = ", ".join(f"{name} = ?" for name, _ in updates)
    values = [value for _, value in updates]
    values.append(dialog_id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE dialogs SET {assignments}, updated_at = datetime('now') WHERE id = ?",
            values,
        )
        await db.commit()


async def get_stale_dialogs(hours_threshold: int = 24, max_followups: int = 2) -> list[dict]:
    """Возвращает диалоги, где клиент не отвечал дольше hours_threshold часов
    и ещё не превышен лимит follow-up сообщений.
    Стадии: INITIATING (бот написал, клиент молчит) и QUALIFYING/NEGOTIATING (был диалог, клиент замолчал).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM dialogs
               WHERE stage IN ('INITIATING', 'QUALIFYING', 'NEGOTIATING', 'FORUM_CHAT')
                 AND followup_count < ?
                 AND datetime(updated_at) < datetime('now', ?)
               ORDER BY updated_at ASC""",
            (max_followups, f"-{hours_threshold} hours")
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def increment_followup(dialog_id: int):
    """Увеличивает счётчик follow-up и обновляет last_followup_at."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE dialogs
               SET followup_count = followup_count + 1,
                   last_followup_at = datetime('now'),
                   updated_at = datetime('now')
               WHERE id = ?""",
            (dialog_id,)
        )
        await db.commit()


async def get_active_dialogs() -> list[dict]:
    """Возвращает все активные диалоги (не CLOSED/ENDED) с информацией о лиде."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT d.*, l.category as lead_category, l.task, l.budget,
                      l.deadline, l.lead_score, l.status as lead_status
               FROM dialogs d
               LEFT JOIN leads l ON d.lead_id = l.id
               WHERE d.stage NOT IN ('CLOSED', 'ENDED')
               ORDER BY d.updated_at DESC""",
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def close_dialog(dialog_id: int):
    """Закрывает диалог (помечает как CLOSED)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE dialogs SET stage = 'CLOSED', updated_at = datetime('now') WHERE id = ?",
            (dialog_id,)
        )
        await db.commit()


# === A/B Testing ===

async def ab_record_sent(variant: int):
    """Увеличивает счётчик отправленных сообщений для варианта."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO ab_message_stats (variant, sent_count, replied_count)
               VALUES (?, 1, 0)
               ON CONFLICT(variant) DO UPDATE SET sent_count = sent_count + 1""",
            (variant,)
        )
        await db.commit()


async def ab_record_reply(variant: int):
    """Увеличивает счётчик ответов для варианта."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE ab_message_stats SET replied_count = replied_count + 1 WHERE variant = ?",
            (variant,)
        )
        await db.commit()


async def ab_get_stats() -> list[dict]:
    """Возвращает статистику по всем вариантам."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT variant, sent_count, replied_count FROM ab_message_stats ORDER BY variant"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def ab_get_weights(num_variants: int, min_samples: int = 10) -> list[float]:
    """Возвращает веса для выбора вариантов на основе конверсии.
    Пока данных мало (< min_samples) — равные веса (чистый A/B тест).
    Когда данных достаточно — веса пропорциональны конверсии (эксплуатация).
    """
    stats = await ab_get_stats()
    if not stats:
        return [1.0 / num_variants] * num_variants

    total_sent = sum(s["sent_count"] for s in stats)
    if total_sent < min_samples:
        return [1.0 / num_variants] * num_variants

    weights = []
    for i in range(num_variants):
        stat = next((s for s in stats if s["variant"] == i), None)
        if stat and stat["sent_count"] > 0:
            rate = stat["replied_count"] / stat["sent_count"]
            weights.append(max(rate, 0.05))
        else:
            weights.append(0.05)

    total = sum(weights)
    return [w / total for w in weights]


async def set_dialog_ab_variant(dialog_id: int, variant: int):
    """Сохраняет номер A/B варианта для диалога."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE dialogs SET ab_variant = ? WHERE id = ?",
            (variant, dialog_id)
        )
        await db.commit()


# === Users seen ===

async def has_seen_user(sender_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM users_seen WHERE sender_id = ?", (sender_id,)
        ) as cursor:
            return await cursor.fetchone() is not None


async def mark_user_seen(sender_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users_seen (sender_id) VALUES (?)", (sender_id,)
        )
        await db.commit()


# === Messages log ===

async def log_message(chat_id: int, sender_id: int, sender_name: str,
                      message_text: str, message_id: int, direction: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO messages_log (chat_id, sender_id, sender_name, message_text, message_id, direction)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (chat_id, sender_id, sender_name, message_text, message_id, direction)
        )
        await db.commit()


async def get_recent_dialog_openers(limit: int = 8) -> list[str]:
    """Последние первые сообщения, отправленные разным лидам при старте диалога.
    Нужно, чтобы AI не повторял одну и ту же структуру/фразы разным людям
    (иначе Telegram может забанить аккаунт за похожие рассылки)."""
    import json
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT ai_messages_json FROM dialogs ORDER BY id DESC LIMIT ?",
            (limit,)
        ) as cursor:
            rows = await cursor.fetchall()
    openers = []
    for (messages_json,) in rows:
        try:
            messages = json.loads(messages_json or "[]")
        except (json.JSONDecodeError, TypeError):
            continue
        for m in messages:
            if m.get("role") == "assistant":
                openers.append(m["content"])
                break
    return openers


async def update_message_verdict(chat_id: int, message_id: int, verdict: str):
    """Записывает вердикт фильтрации (level0_filtered/ai_error/lead_HOT/cold_...) для диагностики."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE messages_log SET filter_verdict = ?
               WHERE chat_id = ? AND message_id = ? AND direction = 'incoming'""",
            (verdict, chat_id, message_id)
        )
        await db.commit()


async def get_last_messages_per_monitored_chat() -> list[dict]:
    """Последнее входящее сообщение по каждому отслеживаемому чату — для диагностики
    (проверить, что бот реально читает чат и как классифицировал последнее сообщение)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT c.chat_id, c.chat_name, ml.message_text, ml.sender_name,
                   ml.filter_verdict, ml.created_at
            FROM chats c
            LEFT JOIN messages_log ml ON ml.id = (
                SELECT id FROM messages_log
                WHERE chat_id = c.chat_id AND direction = 'incoming'
                ORDER BY id DESC LIMIT 1
            )
            WHERE c.is_monitored = 1
            ORDER BY c.chat_name
            """
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


# === Anti-ban stats ===

async def record_action():
    now = datetime.now()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO anti_ban_stats (date, hour, actions_count)
               VALUES (?, ?, 1)
               ON CONFLICT(date, hour)
               DO UPDATE SET actions_count = actions_count + 1""",
            (now.strftime("%Y-%m-%d"), now.hour)
        )
        await db.commit()


async def get_hourly_actions_count() -> int:
    now = datetime.now()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT actions_count FROM anti_ban_stats WHERE date = ? AND hour = ?",
            (now.strftime("%Y-%m-%d"), now.hour)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


async def get_daily_actions_count() -> int:
    now = datetime.now()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COALESCE(SUM(actions_count), 0) FROM anti_ban_stats WHERE date = ?",
            (now.strftime("%Y-%m-%d"),)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


# === Settings ===

async def get_setting(key: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value)
        )
        await db.commit()


# === Found Chats (поиск без AI) ===

async def add_found_chat(chat_id: int, title: str, username: str,
                         participants_count: int, is_channel: bool,
                         is_group: bool, search_keyword: str = None) -> bool:
    """Добавляет найденный чат в БД. Возвращает True если добавлен, False если дубликат."""
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                """INSERT OR IGNORE INTO found_chats
                   (chat_id, title, username, participants_count, is_channel, is_group, search_keyword)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (chat_id, title, username, participants_count,
                 1 if is_channel else 0, 1 if is_group else 0, search_keyword)
            )
            await db.commit()
            cursor = await db.execute("SELECT changes()")
            changes = (await cursor.fetchone())[0]
            return changes > 0
        except Exception:
            return False


async def is_chat_already_found(chat_id: int) -> bool:
    """Проверяет, есть ли чат уже в found_chats."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM found_chats WHERE chat_id = ?", (chat_id,)
        ) as cursor:
            return await cursor.fetchone() is not None


async def get_found_chats_today() -> list[dict]:
    """Возвращает все чаты, найденные сегодня."""
    today = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM found_chats WHERE found_date = ? ORDER BY participants_count DESC""",
            (today,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def get_found_chats_count_today() -> int:
    """Возвращает количество чатов, найденных сегодня."""
    today = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM found_chats WHERE found_date = ?", (today,)
        ) as cursor:
            return (await cursor.fetchone())[0]


async def cleanup_old_found_chats(days: int = 3) -> int:
    """Удаляет найденные чаты старше указанного количества дней. Возвращает количество удалённых."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM found_chats WHERE found_date < date('now', ?)",
            (f"-{days} days",)
        )
        await db.commit()
        return cursor.rowcount


async def get_all_found_chat_ids() -> set[int]:
    """Возвращает множество всех chat_id из found_chats (для фильтрации дубликатов при поиске)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT chat_id FROM found_chats") as cursor:
            rows = await cursor.fetchall()
            return {row[0] for row in rows}
            return {row[0] for row in rows}
