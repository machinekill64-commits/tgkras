"""Тест промпта generate_forum_reply на живом GigaChat.

Сценарии:
A) Имя неизвестно — AI должен спросить, как обращаться
B) Имя известно (Мария) — AI должен использовать его
C) «Подумаю» — AI должен давить срочностью (дата форума/дефицит)
"""
import asyncio
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import ai_engine


def opener(name=""):
    greet = f", {name}" if name else ""
    return [
        {"role": "system", "content": "Приглашение на форум отправлено организатором."},
        {"role": "assistant", "content":
            f"Здравствуйте{greet}! Увидела вас в чате «Бизнес-леди Краснодар». "
            "Чем вы занимаетесь? Приглашаю на наш форум для женщин 25 октября."},
    ]


async def main():
    results = []

    # A) имя неизвестно
    msgs = opener() + [{"role": "user", "content": "Здравствуйте. Работаю в салоне красоты мастером."}]
    reply_a = await ai_engine.generate_forum_reply(msgs, sender_name="")
    results.append(("A) имя неизвестно → ждём вопрос про имя", reply_a))

    # B) имя известно
    msgs = opener("Мария") + [{"role": "user", "content": "Здравствуйте! Занимаюсь своим небольшим магазином одежды."}]
    reply_b = await ai_engine.generate_forum_reply(msgs, sender_name="Мария")
    results.append(("B) имя известно (Мария) → ждём обращение по имени", reply_b))

    # C) «подумаю» — давим срочностью
    msgs = opener() + [
        {"role": "user", "content": "Я дизайнер интерьеров, фрилансю."},
        {"role": "assistant", "content": "Отлично, вам как раз про личный бренд и нетворкинг будет полезно. За полдня унесёте контакты и заряд на неделю."},
        {"role": "user", "content": "Звучит неплохо, но я подумаю, времени мало сейчас"},
    ]
    reply_c = await ai_engine.generate_forum_reply(msgs, sender_name="Ольга")
    results.append(("C) «подумаю» → ждём срочность/дату/дефицит", reply_c))

    with open("test_forum_result.txt", "w", encoding="utf-8") as f:
        for title, reply in results:
            f.write(f"### {title}\n{reply}\n\n")


asyncio.run(main())
