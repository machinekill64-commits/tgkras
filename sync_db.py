"""Двусторонняя синхронизация bot_acc1.db между серверами (cron).

Работает по pull-модели: каждый сервер периодически:
1. делает снапшот БД пира (sqlite backup через SSH) и скачивает его;
2. мержит снапшот пира В СВОЮ базу.

Правила мержа коммутативны и монотонны — две базы сходятся к объединению.
Ключи: outreach_targets/dialogs/leads по sender_id, chats/found_chats по
chat_id, остальные по PK/уникальному полю. id (AUTOINCREMENT) не мержится —
у каждой базы свои, конфликты решаются по бизнес-ключу.

Настройка: sync_peer.json рядом со скриптом:
{"host": "83.166.245.23", "user": "root", "password": "...",
 "remote_dir": "/opt/lead-hunter"}
По умолчанию local DB = data/bot_acc1.db (как DB_PATH в config).
Cron: */15 * * * * /opt/lead-hunter/.venv/bin/python /opt/lead-hunter/sync_db.py >> /opt/lead-hunter/logs/sync.log 2>&1
"""
import json
import os
import sqlite3
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PEER_CONF = os.path.join(BASE_DIR, "sync_peer.json")
LOCAL_DB = os.path.join(BASE_DIR, "data", "bot_acc1.db")

# --- правила мержа по таблицам --------------------------------------------
# key: бизнес-ключ строки; rank: статусная колонка с рангом терминальности;
# max/min/or/longer: колонки, мержимые соответствующим правилом;
# latest: колонка-время, чьё большей значение определяет "свежую" строку
# (остальные её поля побеждают, если нет rank).
TERMINAL_RANK = {"NEW": 0, "CLAIMED": 1}
DEFAULT_RANK = 2  # SENT/REJECTED/SKIPPED*/FAILED — терминальные

MERGE = {
    "outreach_targets": {
        "key": ["sender_id"], "rank": "status",
        "max": ["msg_count"], "min": ["first_seen_at"],
        "longer": ["sample_texts", "last_message_text"],
    },
    "dialogs": {
        "key": ["sender_id"], "latest": "updated_at",
        "max": ["followup_count"],
        "longer": ["ai_messages_json", "brief_text"],
        "or": ["do_not_contact", "handoff_notified", "ready_to_work",
               "awaiting_owner_price", "country_asked"],
    },
    "leads": {"key": ["sender_id"], "insert_only": True},
    "cold_leads": {"key": ["sender_id"], "insert_only": True},
    "chats": {
        "key": ["chat_id"],
        "or": ["is_monitored", "is_harvest", "is_direct_promo", "is_broadcast"],
        "max": ["last_read_message_id"],
    },
    "found_chats": {"key": ["chat_id"], "insert_only": True},
    "users_seen": {"key": ["sender_id"], "min": ["first_contact_at"]},
    "settings": {"key": ["key"], "longer": ["value"]},
    "anti_ban_stats": {"key": ["date", "hour"], "max": ["actions_count"]},
    "ab_message_stats": {"key": ["variant"],
                         "max": ["sent_count", "replied_count"]},
    "chat_join_log": {"key": ["chat_username", "joined_at"],
                      "insert_only": True},
    "gis_companies": {"key": ["name", "phone"], "insert_only": True},
}


def _rank(status: str) -> int:
    return TERMINAL_RANK.get(status or "NEW", DEFAULT_RANK)


def _cols(db, table):
    return [r[1] for r in db.execute(f"PRAGMA table_info({table})")]


def _merge_row(local_row: dict, peer_row: dict, spec: dict,
               cols: list) -> dict:
    """Возвращает объединённую строку (без id)."""
    merged = {}
    rank_col = spec.get("rank")
    latest_col = spec.get("latest")

    if rank_col:
        lw, pw = _rank(local_row.get(rank_col)), _rank(peer_row.get(rank_col))
        winner, loser = (peer_row, local_row) if pw > lw else (local_row, peer_row)
    elif latest_col:
        lt = local_row.get(latest_col) or ""
        pt = peer_row.get(latest_col) or ""
        winner, loser = (peer_row, local_row) if pt > lt else (local_row, peer_row)
    else:
        winner, loser = local_row, peer_row

    for c in cols:
        if c == "id":
            continue
        lv, pv = local_row.get(c), peer_row.get(c)
        if c in spec.get("max", []):
            merged[c] = max(lv or 0, pv or 0)
        elif c in spec.get("min", []):
            vals = [v for v in (lv, pv) if v]
            merged[c] = min(vals) if vals else lv or pv
        elif c in spec.get("or", []):
            merged[c] = 1 if (lv or pv) else (lv or pv)
        elif c in spec.get("longer", []):
            merged[c] = lv if len(str(lv or "")) >= len(str(pv or "")) else pv
        elif c == rank_col:
            merged[c] = winner[c]
        elif c == latest_col:
            merged[c] = max(str(lv or ""), str(pv or ""))
        else:
            # поле победителя; если пустое — берём из проигравшего
            merged[c] = winner.get(c)
            if merged[c] in (None, ""):
                merged[c] = loser.get(c)
    return merged


def merge_db(local_path: str, peer_path: str) -> dict:
    """Мержит peer БД в local. Возвращает счётчики изменений."""
    stats = {"inserted": 0, "updated": 0}
    local = sqlite3.connect(local_path, timeout=30)
    local.execute("PRAGMA busy_timeout=30000")
    peer = sqlite3.connect(peer_path, timeout=30)
    local.row_factory = sqlite3.Row
    peer.row_factory = sqlite3.Row

    for table, spec in MERGE.items():
        try:
            peer_cols = _cols(peer, table)
            local_cols = _cols(local, table)
        except sqlite3.OperationalError:
            continue
        if not peer_cols or not local_cols:
            continue
        cols = [c for c in peer_cols if c in local_cols]
        keys = [k for k in spec["key"] if k in cols]
        if not keys:
            continue

        where = " AND ".join(f"{k} = ?" for k in keys)
        for row in peer.execute(f"SELECT * FROM {table}"):
            r = dict(row)
            key = tuple(r[k] for k in keys)
            cur = local.execute(
                f"SELECT * FROM {table} WHERE {where}", key).fetchone()

            if cur is None:
                ins_cols = [c for c in cols if c != "id"]
                local.execute(
                    f"INSERT INTO {table} ({','.join(ins_cols)}) "
                    f"VALUES ({','.join('?' * len(ins_cols))})",
                    [r.get(c) for c in ins_cols])
                stats["inserted"] += 1
                continue

            if spec.get("insert_only"):
                continue

            merged = _merge_row(dict(cur), r, spec, cols)
            if any((merged.get(c) or "") != (dict(cur).get(c) or "")
                   for c in cols if c != "id"):
                set_cols = [c for c in cols if c != "id"]
                local.execute(
                    f"UPDATE {table} SET "
                    f"{','.join(f'{c} = ?' for c in set_cols)} "
                    f"WHERE {where}",
                    [merged.get(c) for c in set_cols] + list(key))
                stats["updated"] += 1
    local.commit()
    local.close()
    peer.close()
    return stats


def fetch_peer_snapshot(conf: dict, dst: str) -> bool:
    """SSH на пир: снапшот его БД + SFTP-скачивание."""
    import paramiko
    remote_db = f"{conf['remote_dir']}/data/bot_acc1.db"
    remote_snap = f"{conf['remote_dir']}/data/.sync_snapshot.db"
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(conf["host"], username=conf["user"],
              password=conf["password"], timeout=20)
    try:
        cmd = (f"python3 -c \"import sqlite3;"
               f"sqlite3.connect('{remote_db}').backup("
               f"sqlite3.connect('{remote_snap}'))\"")
        _, o, e = c.exec_command(cmd, timeout=120)
        err = e.read().decode(errors="replace").strip()
        if err:
            print(f"[peer] snapshot error: {err}", flush=True)
            return False
        sftp = c.open_sftp()
        sftp.get(remote_snap, dst)
        sftp.remove(remote_snap)
        sftp.close()
        return True
    finally:
        c.close()


def main():
    if not os.path.exists(PEER_CONF):
        print(f"Нет {PEER_CONF} — синк пропущен", flush=True)
        return
    conf = json.load(open(PEER_CONF, encoding="utf-8"))
    peer_db = os.path.join(BASE_DIR, "data", ".peer_snapshot.db")

    for attempt in range(3):
        try:
            if not fetch_peer_snapshot(conf, peer_db):
                print("[peer] не удалось скачать снапшот", flush=True)
                return
            stats = merge_db(LOCAL_DB, peer_db)
            print(f"[sync] merge peer -> local: "
                  f"+{stats['inserted']} новых, ~{stats['updated']} обновлено",
                  flush=True)
            return
        except sqlite3.OperationalError as e:
            print(f"[sync] попытка {attempt + 1}: {e}", flush=True)
            time.sleep(10 * (attempt + 1))
        finally:
            if os.path.exists(peer_db):
                try:
                    os.remove(peer_db)
                except OSError:
                    pass


if __name__ == "__main__":
    main()
