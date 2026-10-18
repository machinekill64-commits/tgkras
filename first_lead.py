"""Вступление в топ-краснодарские чаты + поиск и касание первого лида.

Запуск: python first_lead.py
1. Вступает в приоритетные чаты (лимит CHAT_JOIN_DAILY_LIMIT/день)
2. Регистрирует их в chats с is_harvest=1
3. Бэкфилл: сканирует последние сообщения → outreach_targets
4. Скорит кандидатов (гендер + город через AI)
5. Отправляет приглашение первому подходящему лиду
"""
import asyncio
import os
import re

import database as db
import ai_engine
from user_client import UserClient
from chat_finder import join_and_organize_chat

# Приоритет: лучшие локальные чаты из hunt_forum_chats2.log
CHATS_TO_JOIN = [
    "biznes_krasnodar1",    # ЦА 19/19 — лучший
    "bizneskrasnodar0",     # самый крупный
    "buskrasnodar",         # самый живой
    "mkopora23",            # предприниматели КРД
    "krasnodar_busines",    # предприниматели КРД
    "krasnodar_biz",        # запасной, если лимит/ошибка
]

BACKFILL_MESSAGES = 150
MIN_TEXT_LEN = 15

# Сигналы ЦА форума для скоринга бэкфилл-кандидатов
LEAD_SIGNAL = re.compile(
    r"(?i)(ищу клиент|предпринимател|свой бизнес|самозанят|салон|мастер|"
    r"косметолог|визажист|маникюр|бьюти|психолог|коуч|эксперт|"
    r"личный бренд|нетворкинг|мероприят|форум|записывайтесь|услуги)"
)
# Мусорные отправители: реклама вакансий/услуг массовая, боты
SPAM_SIGNAL = re.compile(
    r"(?i)(требуется|ваканси|заработок|удалённ|график|опыт не обязателен|"
    r"пишите в лс|подробности в лс|накрутка|подписчики)"
)


async def backfill_candidates(uc: UserClient, entity, chat_id_db: int, title: str):
    """Сканирует историю чата и сохраняет активных участников в outreach_targets."""
    added = 0
    async for m in uc.client.iter_messages(entity, limit=BACKFILL_MESSAGES):
        text = (m.text or "").strip()
        if len(text) < MIN_TEXT_LEN:
            continue
        sender = await m.get_sender()
        if not sender or getattr(sender, "bot", False):
            continue
        sid = getattr(sender, "id", None)
        if not sid or sid == uc._me_id:
            continue
        name = " ".join(filter(None, [
            getattr(sender, "first_name", "") or "",
            getattr(sender, "last_name", "") or ""])).strip()
        username = getattr(sender, "username", None)
        await db.upsert_outreach_target(
            chat_id=chat_id_db, chat_name=title,
            sender_id=sid, sender_name=name or username or str(sid),
            sender_username=username, message_text=text)
        added += 1
    return added


async def score_candidate(target: dict) -> tuple[int, str]:
    """Эвристика + AI-проверки. Возвращает (score, причина)."""
    text = (target.get("sample_texts") or target.get("last_message_text") or "")
    name = target.get("sender_name") or ""
    score = 0
    lead_hits = len(LEAD_SIGNAL.findall(text))
    spam_hits = len(SPAM_SIGNAL.findall(text))
    score += lead_hits * 2 - spam_hits * 3 + min(target.get("msg_count", 1), 5)
    if target.get("sender_username"):
        score += 1
    if spam_hits >= 3:
        return -100, "spam"
    gender = await ai_engine.detect_person_gender(name, text)
    if gender == "male":
        return -50, "male"
    city = await ai_engine.detect_person_city(text)
    if city == "other":
        return -50, "other_city"
    if gender == "female":
        score += 5
    if city == "krasnodar":
        score += 3
    return score, f"gender={gender} city={city} hits={lead_hits}"


async def main():
    await db.init_db()
    uc = UserClient()
    await uc.client.connect()
    me = await uc.client.get_me()
    uc._me_id = me.id
    print(f"[me] {me.first_name} {me.last_name or ''} @{me.username}", flush=True)

    # 1. Вступление (лимит 5/день внутри join_and_organize_chat)
    joined = []
    for username in CHATS_TO_JOIN:
        ok = await join_and_organize_chat(
            uc.client, {"username": username}, folder="business")
        print(f"[join] @{username} -> {'OK' if ok else 'FAIL/LIMIT'}", flush=True)
        if ok:
            joined.append(username)
        if len(joined) >= 5:
            break

    if not joined:
        print("[warn] ни один чат не вступлен — лимит или ошибки", flush=True)
        await uc.client.disconnect()
        return

    # 2. Регистрация is_harvest + бэкфилл кандидатов
    for username in joined:
        try:
            entity = await uc.client.get_entity(username)
            chat_id_db = int(f"-100{entity.id}")
            await db.add_chat(chat_id_db, entity.title)
            await db.update_chat(chat_id_db, is_harvest=True)
            n = await backfill_candidates(uc, entity, chat_id_db, entity.title)
            print(f"[harvest] @{username} ({chat_id_db}) -> {n} кандидатов", flush=True)
        except Exception as e:
            print(f"[err] @{username}: {e}", flush=True)
        await asyncio.sleep(2)

    # 3. Скоринг кандидатов
    targets = await db.get_pending_outreach_targets(
        limit=30, min_messages=1, recency_days=30)
    print(f"[score] кандидатов в очереди: {len(targets)}", flush=True)

    best = None
    best_score = -999
    for t in targets:
        score, reason = await score_candidate(t)
        print(f"  {score:4d} | {t['sender_name'][:30]:30s} | @{t.get('sender_username') or '-'} | {reason}", flush=True)
        if score > best_score:
            best_score = score
            best = t
        await asyncio.sleep(0.5)

    if not best or best_score <= 0:
        print("[result] подходящего лида нет", flush=True)
        await uc.client.disconnect()
        return

    print(f"\n[LEAD] {best['sender_name']} @{best.get('sender_username')} "
          f"score={best_score} из чата {best['chat_name']}", flush=True)
    print(f"  sample: {(best.get('last_message_text') or '')[:150]}", flush=True)

    # 4. Отправка приглашения (с атомарным захватом — мульти-аккаунт)
    claimed = await db.claim_outreach_target(best["id"], claimer="first_lead")
    if not claimed:
        print("[invite] цель уже захвачена другим аккаунтом — пропуск", flush=True)
        await uc.client.disconnect()
        return
    status = await uc.send_forum_invite(best)
    print(f"[invite] -> {status}", flush=True)
    if status == "sent":
        await db.mark_outreach_target(best["id"], "SENT")
    elif status == "skipped_male":
        await db.mark_outreach_target(best["id"], "SKIPPED_MALE")
    elif status == "skipped_city":
        await db.mark_outreach_target(best["id"], "SKIPPED_CITY")
    elif status == "skipped":
        await db.mark_outreach_target(best["id"], "SKIPPED")
    else:
        await db.mark_outreach_target(best["id"], "FAILED")

    await uc.client.disconnect()
    print("DONE", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
