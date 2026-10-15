import asyncio
import json
import unittest
import httpx
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import ai_engine
import lead_presentation
import user_client as user_client_module
from lead_detector import (LeadResult, detect_cold_lead, detect_lead,
                           _extract_budget, _extract_task)
from lead_qualification import qualify_lead
from notification_bot import NotificationBot, _lead_contact


class LeadDetectorQualityTests(unittest.IsolatedAsyncioTestCase):
    async def test_required_intent_and_service_scope(self):
        messages = [
            "Нужно приложение для доставки продуктов",
            "Ищу программиста для автоматизации учёта на складе",
            "Нужна доработка сайта: реферальная система",
            "Нужна помощь с настройкой linux-сервера",
            "Нужна помощь с настройкой vpn",
            "Нужна помощь с настройкой docker",
            "Нужна помощь с настройкой nginx",
            "Нужна помощь с администрированием сервера",
            "Нужно сервер настроить для компании",
            "Нужен сайт", "Нужен бот", "Need VPN",
            "Ищу исполнителя: нужен лендинг для магазина",
            "Кто может сделать веб-приложение для заказов?",
            "Кто сделает мобильное приложение?",
            "Требуется разработать приложение ios/android",
            "Нужно заказать телеграм-бот для клиентов",
            "Есть ТЗ на чат-бот для записи",
            "Бюджет 50000 на интеграцию crm и API",
            "Нужен парсер, скрипт для сбора данных",
            "Нужна доработка 1с/bitrix",
            "Ищу специалиста DevOps: бэкапы, deploy",
            "Наша компания: нужен сайт для клиентов",
            "Вакансия: ищу разработчика под задачу",
            "Looking for a developer to build a website",
            "Need a mobile app for food delivery",
            "Hire a programmer for API integration",
        ]
        for text in messages:
            with self.subTest(text=text):
                result = await detect_lead(text, datetime.now(timezone.utc))
                self.assertTrue(result.is_lead)
                self.assertTrue(result.service)
                self.assertTrue(result.evidence)

    async def test_required_rejections(self):
        messages = [
            "Кредит для физ. и юр. лиц",
            "Есть тема на сегодня, кому интересно — в лс",
            "Темка с доходом 20.000, интересно?",
            "Темка поднять денег 15.000, интересно?",
            "новости ВВП", "интересно", "Интересно?",
            "интересно, расскажите подробнее о сайте",
            "Автоматизирую ваш бизнес", "Я технический специалист",
            "Нужен сайт, разработаю вам сайт. Портфолио, пишите в лс",
            "Делаю сайты: бюджет от 50000. Отзывы, пишите в лс",
            "Настрою вам CRM, нужна автоматизация? Пишите в лс",
            "Ищу подработку: сайт на заказ", "Нужен сайт: работа на дому",
            "Вакансии: нужен разработчик сайта в штат",
            "Нужен программист без опыта, пишите в личные",
            "Нужен сайт, доход 5000 в сутки, кому интересно пишите в лс",
            "Нужен бот, есть тема поднять денег",
            "Статья о том, как работает автоматизация склада",
            "Обсуждаем сайт, новый выпуск Docker и Nginx",
            "Нужен курьер для доставки продуктов", "Ищу кладовщика",
            "Нужен сервер для игры", "Нужен велосипед",
            "Ищу разработчика",  # Нет ни услуги, ни конкретной проектной задачи.
            "Нужен бот для казино и ставки на спорт",
            # Самопиар продавцов (боевые примеры лидов #395-#397)
            "🚀 Need a Website, App or AI Solution? I provide custom development services "
            "starting from ₹5,999. Websites & Web Apps, Mobile Apps, E-commerce, SaaS, "
            "Bots & AI Bots, APIs & Integrations. Feel free to DM me to discuss.",
            "#помогу #seo #сеопродвижение #разработка #созданиесайтов Привет! "
            "Меня зовут Максим, представляю digital-агентство GracieDigital. "
            "Если вам нужен сайт или продвижение в интернете — помогу с любой задачей.",
            "#помогу #техническийспециалист #техспец #чатбот #геткурс Всем привет! "
            "Меня зовут Миша. Помощь технического специалиста является неотъемлемой частью "
            "проекта. Моя задача — сделать так, чтобы вы могли сосредоточиться на росте "
            "бизнеса, а я возьму на себя всю техническую часть.",
            # Крипто/USDT-спам (боевой пример лида #398)
            "**We need a lot of USDT** 1usdt=135inr-145inr Minimum: 100usdt "
            "Flipkart E-Gift Card. An account can be used for 5 to 7 days. Mixed funds.",
        ]
        messages += [f"Нужен сайт: {word}" for word in (
            "кредит", "заём", "займ", "ипотека", "инвестиции", "крипто",
            "биткоин", "вклад", "стейкинг")]
        for text in messages:
            with self.subTest(text=text):
                result = await detect_lead(text, datetime.now(timezone.utc))
                self.assertFalse(result.is_lead)

    def test_budget_formats_and_duration(self):
        cases = [
            ("Бюджет 50000", "50000"), ("Цена: 50 000 руб", "50000 руб"),
            ("Бюджет от 1 до 20 млн", "от 1 до 20 млн"),
            ("от 1 до 20 млн", None),
            ("от 1 до 20 млн руб", "от 1 до 20 млн руб"),
            ("Бюджет 1–20 млн", "1–20 млн"),
            ("Бюджет 1 млн — 20 млн ₽", "1 млн — 20 млн ₽"),
            ("Бюджет 50-80 тыс руб", "50-80 тыс руб"),
            ("Бюджет 1,5 млн ₽", "1,5 млн ₽"),
            ("Нужен сайт за 5 дней", None),
            ("Срок 50000, за 5 дней", None), ("за 19000", None),
            ("Бюджет обсудим за 5 дней", None),
            ("Бюджет 5 дней", None),
            ("Нужно за 5 дней, бюджет 50000", "50000"),
            ("Сделать сайт за $800", "$800"),
            ("Стоимость €1200", "€1200"), ("Сделать за 800$", "800$"),
            ("Оплата 900€", "900€"), ("Оплата 50000₽", "50000₽"),
            ("Оплата 800 usd", "800 usd"), ("Оплата 800 EUR", "800 EUR"),
            ("Оплата 19 000 рублей", "19000 рублей"),
            ("Бюджет: договорной", "договорной"),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(_extract_budget(text), expected)

    def test_task_selects_request_line(self):
        text = "Наш канал об IT\nОбсуждаем новости\nНужна интеграция CRM и API\nКонтакт: @manager"
        self.assertEqual(_extract_task(text), "Нужна интеграция CRM и API")
        self.assertEqual(_extract_task("Новости\nНужна настройка linux-сервера"),
                         "Нужна настройка linux-сервера")
        self.assertIsNone(_extract_task("Реклама сайта\nинтересно"))

    async def test_cold_business_process_required(self):
        for text in ["У нас заказы ведём вручную, теряем клиентов",
                     "У меня учёт на складе в экселе, много ручной работы",
                     "Наша компания: нет сайта, клиенты не могут нас найти",
                     "Our company processes orders on paper"]:
            with self.subTest(text=text):
                self.assertTrue((await detect_cold_lead(text, datetime.now(timezone.utc))).is_lead)
        for text in ["Устал каждый день вручную заполнять документы",
                     "У меня нет сил, не хватает времени", "Нужен сайт для бизнеса",
                     "Новости: у нас рост ВВП, много клиентов",
                     "У нас подработка без опыта, все заявки вручную",
                     "У нас кредит, нет сайта", "У нас открыли кафе",
                     "Наша компания предлагает сайт, отзывы и портфолио"]:
            with self.subTest(text=text):
                self.assertFalse((await detect_cold_lead(text, datetime.now(timezone.utc))).is_lead)

    async def test_old_messages_and_bots_rejected(self):
        text = "Нужен сайт интернет-магазина"
        for detector in (detect_lead, detect_cold_lead):
            self.assertFalse((await detector(text, datetime.now(timezone.utc), True)).is_lead)
            self.assertFalse((await detector(text, datetime.now(timezone.utc) - timedelta(days=365))).is_lead)

    async def test_real_it_requests_pass_and_extract_details(self):
        cases = [
            ("Ищу разработчика 1С-Битрикс. Нужно сделать интеграцию платежной системы Platega и Heleket. Бюджет 800$.", "800$"),
            ("Нужно создать сайт интернет-магазина на Яндекс Кит. Цена договорная.", "договорная"),
        ]
        for text, budget in cases:
            with self.subTest(text=text):
                result = await detect_lead(text, datetime.now(timezone.utc))
                self.assertTrue(result.is_lead)
                self.assertEqual(result.category, "HOT")
                self.assertEqual(result.budget, budget)
                self.assertIsNotNone(result.task)

    async def test_generic_chat_and_recruiting_spam_are_rejected(self):
        messages = [
            "Привет, есть мяу нет компаний, но хотелось бы с кем то отдохнуть, если интересно пиши в ЛС",
            "Интeресуeшься IT-сферoй (крипто, мaйнинг, прог. технoлогии, хaкинг)? Узнаешь о высокоперспективном проекте. Цель — сформировать свою команду.",
            "Есть рабочий вариант с нормальной оплатой, можно попробовать в свободное время. Расскажу, как делать 20.000 в сутки.",
            "Есть несложное дело, плачу 10к день. Интересно — пиши.",
            "АВТОМАТИЗИРУЮ ВАШ БИЗНЕС. Устали от ручной работы и таблиц? Разработаю систему. Стек: Python, JavaScript.",
            "Динамика ВВП: сползание к нулю. Росстат опубликовал оценку ВВП за первое полугодие.",
            "Кому интересно отпишите пожалуйста",
            "Ищу разработчика для продажи наркотиков и закладок.",
        ]
        for text in messages:
            with self.subTest(text=text):
                result = await detect_lead(text, datetime.now(timezone.utc))
                self.assertFalse(result.is_lead)

    async def test_service_ad_is_rejected(self):
        text = (
            "Привет! Делаю инфографику и CTR-слайды для карточек товара на WB/Ozon.\n"
            "100+ заказов уже выполнено. Портфолио и отзывы — в канале."
        )
        result = await detect_lead(text, datetime.now(timezone.utc))
        self.assertFalse(result.is_lead)

    async def test_cold_lead_requires_business_context(self):
        real_business_pain = "У нас нет сайта, все заявки ведем вручную и теряем клиентов."
        generic_news = "Росстат опубликовал оценку ВВП: рост экономики замедлился."
        developer_ad = "Автоматизирую ваш бизнес. Устали от ручной работы? Разработаю систему."

        cold = await detect_cold_lead(real_business_pain, datetime.now(timezone.utc))
        news = await detect_cold_lead(generic_news, datetime.now(timezone.utc))
        ad = await detect_cold_lead(developer_ad, datetime.now(timezone.utc))

        self.assertTrue(cold.is_lead)
        self.assertFalse(news.is_lead)
        self.assertFalse(ad.is_lead)

    def test_contact_includes_username_or_profile_fallback(self):
        username_text, username_button = _lead_contact("@contact_me", 12345)
        id_text, id_button = _lead_contact(None, 12345)

        self.assertIn("@contact_me", username_text)
        self.assertEqual(username_button.url, "https://t.me/contact_me")
        self.assertIn("12345", id_text)
        self.assertEqual(id_button.url, "tg://user?id=12345")


def _review_json(decision, service=None, summary="Заказ на разработку сайта",
                 evidence="Нужен сайт для магазина", reason="Причина",
                 budget=None, deadline=None):
    return json.dumps({
        "decision": decision, "service": service, "summary_ru": summary,
        "evidence": evidence, "reason_ru": reason,
        "budget_quote": budget, "deadline_quote": deadline,
    })


def _gigachat_mock(content=None, side_effect=None):
    mock = AsyncMock(side_effect=side_effect)
    if side_effect is None:
        mock.return_value = content
    return patch.object(ai_engine, "_gigachat_chat", mock), mock


class QualifyLeadTests(unittest.IsolatedAsyncioTestCase):
    """Семантическая проверка кандидатов через GigaChat (моки, без сети)."""

    async def test_irrelevant_chatter_never_calls_review(self):
        patcher, mock = _gigachat_mock(_review_json("REJECT"))
        with patcher:
            result = await qualify_lead("Хорошего вечера всем, до завтра",
                                        datetime.now(timezone.utc))
        self.assertFalse(result.is_lead)
        mock.assert_not_called()

    async def test_direct_with_verbatim_evidence_is_hot(self):
        text = "Нужен сайт для магазина, бюджет обсудим"
        patcher, mock = _gigachat_mock(_review_json(
            "DIRECT", service="website", evidence="Нужен сайт для магазина"))
        with patcher:
            result = await qualify_lead(text, datetime.now(timezone.utc))
        self.assertTrue(result.is_lead)
        self.assertEqual(result.category, "HOT")
        self.assertEqual(result.service, "website")
        self.assertEqual(result.evidence, "Нужен сайт для магазина")
        mock.assert_called_once()

    async def test_pain_is_warm_cold_and_indirect_toggle(self):
        text = "У нас в магазине все заказы ведём вручную, теряем заявки и клиентов"
        patcher, _ = _gigachat_mock(_review_json(
            "PAIN", service="automation",
            summary="Заявки теряются при ручном учёте",
            evidence="все заказы ведём вручную, теряем заявки"))
        with patcher:
            result = await qualify_lead(text, datetime.now(timezone.utc))
            self.assertTrue(result.is_lead)
            self.assertEqual(result.category, "WARM_COLD")
            disabled = await qualify_lead(text, datetime.now(timezone.utc),
                                          include_indirect=False)
        self.assertFalse(disabled.is_lead)
        self.assertEqual(disabled.reason, "indirect_disabled")

    async def test_explicit_reject_is_not_a_lead(self):
        text = "Нужен сайт для магазина, бюджет обсудим"
        patcher, _ = _gigachat_mock(_review_json("REJECT", evidence=""))
        with patcher:
            result = await qualify_lead(text, datetime.now(timezone.utc))
        self.assertFalse(result.is_lead)
        self.assertEqual(result.reason, "semantic_rejected")

    async def test_bad_review_output_fails_closed_without_keyword_fallback(self):
        text = "Нужен сайт для магазина, бюджет обсудим"
        cases = [
            "это не json вообще",                                          # malformed JSON
            _review_json("DIRECT", service="website",
                         evidence="фраза которой не было в сообщении"),    # invented quote
            _review_json("DIRECT", service="quantum_computing"),           # unknown service
            json.dumps({"decision": "DIRECT"}),                            # missing fields
        ]
        for content in cases:
            patcher, _ = _gigachat_mock(content)
            with patcher, self.subTest(content=content[:40]):
                result = await qualify_lead(text, datetime.now(timezone.utc))
                self.assertFalse(result.is_lead)
                self.assertEqual(result.reason, "review_unavailable")
        patcher, _ = _gigachat_mock(side_effect=asyncio.TimeoutError())
        with patcher:
            result = await qualify_lead(text, datetime.now(timezone.utc))
            self.assertFalse(result.is_lead)
            self.assertEqual(result.reason, "review_unavailable")

    async def test_direct_without_optional_detail_fields_accepted(self):
        text = "Нужен сайт для магазина"
        payload = {"decision": "DIRECT", "service": "website",
                   "summary_ru": "Заказ сайта для магазина",
                   "evidence": "Нужен сайт для магазина",
                   "reason_ru": "Прямой запрос на разработку"}
        patcher, _ = _gigachat_mock(json.dumps(payload))
        with patcher:
            result = await qualify_lead(text, datetime.now(timezone.utc))
        self.assertTrue(result.is_lead)
        self.assertEqual(result.category, "HOT")
        self.assertIsNone(result.budget)
        self.assertIsNone(result.deadline)

    async def test_pain_with_null_service_accepted_as_warm_cold(self):
        text = "У нас заказы ведём вручную, теряем клиентов"
        payload = {"decision": "PAIN", "service": None,
                   "summary_ru": "Ручной учёт приводит к потере клиентов",
                   "evidence": text, "reason_ru": "Собственная боль автора"}
        patcher, _ = _gigachat_mock(json.dumps(payload))
        with patcher:
            result = await qualify_lead(text, datetime.now(timezone.utc))
        self.assertTrue(result.is_lead)
        self.assertEqual(result.category, "WARM_COLD")
        self.assertIsNone(result.service)

    async def test_ad_catalog_direct_without_request_verbs_rejected(self):
        text = "Разработка под ключ: сайты, боты, API. Опишите задачу и бюджет"
        payload = {"decision": "DIRECT", "service": "development",
                   "summary_ru": "Каталог услуг разработки",
                   "evidence": "Разработка под ключ: сайты, боты, API.",
                   "reason_ru": "Модель приняла рекламу за заказ"}
        patcher, _ = _gigachat_mock(json.dumps(payload))
        with patcher:
            result = await qualify_lead(text, datetime.now(timezone.utc))
        self.assertFalse(result.is_lead)
        self.assertEqual(result.reason, "semantic_rejected")

    async def test_evidence_case_difference_still_accepted(self):
        text = ("Вакансия: ищу разработчика под задачу. Нужно перенести наш сайт "
                "с Tilda на WordPress, разовый проект.")
        payload = {"decision": "DIRECT", "service": "website",
                   "summary_ru": "Перенос сайта",
                   "evidence": ("Ищу разработчика под задачу. Нужно перенести "
                                "наш сайт с Tilda на WordPress"),
                   "reason_ru": "Разовый заказ на перенос сайта"}
        patcher, _ = _gigachat_mock(json.dumps(payload))
        with patcher:
            result = await qualify_lead(text, datetime.now(timezone.utc))
        self.assertTrue(result.is_lead)
        self.assertEqual(result.category, "HOT")
        self.assertEqual(result.service, "website")

    async def test_pain_without_pain_marker_rejected(self):
        text = ("Я запустил mini app. Нужны советы, что исправить перед "
                "запуском. Буду рад помощи.")
        payload = {"decision": "PAIN", "service": "development",
                   "summary_ru": "Советы по приложению",
                   "evidence": text, "reason_ru": "Похоже на потребность"}
        patcher, _ = _gigachat_mock(json.dumps(payload))
        with patcher:
            result = await qualify_lead(text, datetime.now(timezone.utc))
        self.assertFalse(result.is_lead)
        self.assertEqual(result.reason, "semantic_rejected")


def _fake_event(text):
    sender = SimpleNamespace(id=555, first_name="Author", username="author",
                             usernames=None, bot=False)
    chat = SimpleNamespace(title="TestChat", username="testchat",
                           usernames=None)
    message = SimpleNamespace(id=10, text=text,
                              date=datetime.now(timezone.utc))
    event = SimpleNamespace(chat_id=-1001234, message=message,
                            get_sender=AsyncMock(return_value=sender),
                            get_chat=AsyncMock(return_value=chat))
    return event


def _user_client_stub():
    client = user_client_module.UserClient.__new__(user_client_module.UserClient)
    client._lead_semaphore = asyncio.Semaphore(2)
    client._spam_cache = {}
    client._spam_cache_ttl = 86400
    client.notification_callback = AsyncMock()
    return client


class UserClientQualificationTests(unittest.IsolatedAsyncioTestCase):
    """Интеграция qualify_lead в _process_chat_message (всё внешнее замокано)."""

    def _db_mock(self):
        db = MagicMock()
        db.log_message = AsyncMock()
        db.update_message_verdict = AsyncMock()
        db.has_recent_lead_from_sender = AsyncMock(return_value=False)
        db.add_lead = AsyncMock(return_value=42)
        db.add_cold_lead = AsyncMock(return_value=43)
        return db

    async def test_rejected_candidate_saves_nothing_and_notifies_nothing(self):
        client = _user_client_stub()
        db = self._db_mock()
        qualifier = AsyncMock(return_value=LeadResult(False, reason="semantic_rejected"))
        with patch.object(user_client_module, "db", db), \
             patch.object(user_client_module, "qualify_lead", qualifier):
            await client._process_chat_message(_fake_event("Нужен сайт для магазина"))
        qualifier.assert_called_once()
        db.add_lead.assert_not_called()
        db.add_cold_lead.assert_not_called()
        client.notification_callback.assert_not_called()
        verdict = db.update_message_verdict.call_args[0][2]
        self.assertEqual(verdict, "semantic_rejected")

    async def test_pain_result_goes_only_to_cold_path(self):
        client = _user_client_stub()
        client._process_cold_lead = AsyncMock()
        db = self._db_mock()
        pain_result = LeadResult(True, category="WARM_COLD",
                                 pain="Заявки теряются", service="automation",
                                 evidence="все заказы ведём вручную")
        qualifier = AsyncMock(return_value=pain_result)
        with patch.object(user_client_module, "db", db), \
             patch.object(user_client_module, "qualify_lead", qualifier):
            text = "У нас в магазине все заказы ведём вручную, теряем заявки"
            await client._process_chat_message(_fake_event(text))
        db.add_lead.assert_not_called()
        client._process_cold_lead.assert_called_once()
        self.assertEqual(db.update_message_verdict.call_args[0][2], "cold_WARM_COLD")
        client.notification_callback.assert_not_called()

    async def test_direct_lead_preserves_original_text_and_source(self):
        client = _user_client_stub()
        db = self._db_mock()
        text = "Нужен сайт для магазина, бюджет обсудим"
        direct = LeadResult(True, category="HOT", task="Сайт для магазина",
                            service="website",
                            evidence="Нужен сайт для магазина")
        qualifier = AsyncMock(return_value=direct)
        with patch.object(user_client_module, "db", db), \
             patch.object(user_client_module, "qualify_lead", qualifier):
            await client._process_chat_message(_fake_event(text))
        qualifier.assert_called_once()
        db.add_lead.assert_called_once()
        lead_kwargs = db.add_lead.call_args.kwargs
        self.assertEqual(lead_kwargs["message_text"], text)
        self.assertEqual(lead_kwargs["evidence"], "Нужен сайт для магазина")
        self.assertEqual(lead_kwargs["source_url"],
                         "https://t.me/testchat/10")
        client.notification_callback.assert_called_once()
        notify_kwargs = client.notification_callback.call_args.kwargs
        self.assertEqual(notify_kwargs["message_text"], text)
        self.assertEqual(notify_kwargs["category"], "HOT")


class LeadPresentationTests(unittest.IsolatedAsyncioTestCase):
    """prepare_lead_notification: перевод карточки лида на русский."""

    async def test_english_lead_translated_and_original_kept(self):
        original = ("I need a website for my shop and a mobile app for orders. "
                    "Please contact me with your price and timeline. ") * 15
        self.assertGreater(len(original), 1500)
        chunks = lead_presentation._split_chunks(original)
        self.assertGreater(len(chunks), 1)
        translations = [f"Переведённая часть {i} заказа на русском языке"
                        for i in range(len(chunks))]
        patcher, mock = _gigachat_mock(side_effect=translations)
        kwargs = {"category": "HOT", "message_text": original}
        with patcher:
            result = await lead_presentation.prepare_lead_notification(kwargs)
        self.assertEqual(result["message_text"], "\n".join(translations))
        self.assertEqual(result["original_message_text"], original)
        self.assertEqual(result["detected_language"], "en")
        self.assertFalse(result["translation_failed"])
        self.assertEqual(kwargs["message_text"], original)  # вход не мутирован
        self.assertEqual(mock.call_count, len(chunks))
        sent_chunks = [c.args[1][1]["content"] for c in mock.call_args_list]
        self.assertEqual("".join(sent_chunks).split(), original.split())

    async def test_russian_lead_returned_unchanged_without_ai(self):
        kwargs = {"category": "HOT",
                  "message_text": "Нужен сайт для интернет-магазина"}
        patcher, mock = _gigachat_mock("irrelevant")
        with patcher:
            result = await lead_presentation.prepare_lead_notification(kwargs)
        self.assertEqual(result["message_text"], kwargs["message_text"])
        self.assertNotIn("original_message_text", result)
        mock.assert_not_called()

    async def test_translation_timeout_falls_back_to_original(self):
        original = "I need a website for my shop, please contact me for details."

        async def slow(*a, **kw):
            await asyncio.sleep(60)

        kwargs = {"category": "HOT", "message_text": original}
        with patch.object(ai_engine, "_gigachat_chat", AsyncMock(side_effect=slow)), \
             patch.object(lead_presentation, "_TIMEOUT", 0.05):
            result = await lead_presentation.prepare_lead_notification(kwargs)
        self.assertEqual(result["message_text"], original)
        self.assertEqual(result["original_message_text"], original)
        self.assertTrue(result["translation_failed"])

    async def test_non_lead_categories_never_translated(self):
        kwargs = {"category": "HUMAN_REQUIRED",
                  "message_text": "Please check this dialog manually"}
        patcher, mock = _gigachat_mock("irrelevant")
        with patcher:
            result = await lead_presentation.prepare_lead_notification(kwargs)
        self.assertEqual(result["message_text"], kwargs["message_text"])
        mock.assert_not_called()

    def test_clip_text_counts_utf16_units(self):
        text = "a" * 100 + "😀" * 100  # эмодзи = 2 UTF-16 единицы
        clipped = lead_presentation.clip_text(text, 150)
        units = len(clipped.encode("utf-16-le")) // 2
        self.assertLessEqual(units, 150)
        self.assertTrue(clipped.endswith("…"))
        self.assertEqual(lead_presentation.clip_text("short", 10), "short")

    def test_routers_use_prepare_lead_notification(self):
        import ast
        from pathlib import Path
        for filename in ("main.py", "trial_bot.py"):
            source = Path(__file__).with_name(filename).read_text(encoding="utf-8")
            tree = ast.parse(source)
            router = next(
                node for node in ast.walk(tree)
                if isinstance(node, ast.AsyncFunctionDef)
                and node.name == "notification_router")
            calls = {
                node.func.id for node in ast.walk(router)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)}
            self.assertIn("prepare_lead_notification", calls,
                          f"{filename}: notification_router не вызывает "
                          f"prepare_lead_notification")


class NotificationRenderingTests(unittest.IsolatedAsyncioTestCase):
    """Рендер карточки лида: перевод сверху, кнопки контакта/источника."""

    def _bot_stub(self):
        bot = NotificationBot.__new__(NotificationBot)
        bot.bot = AsyncMock()
        return bot

    async def test_translated_lead_shows_russian_first_and_keeps_buttons(self):
        bot = self._bot_stub()
        original = ("I need a website for my shop with payment integration. "
                    "Contact me please. ") * 20
        translation = "Мне нужен сайт для магазина с интеграцией оплаты."
        await bot.notify_lead(
            lead_id=7, chat_name="Chat", sender_name="Author",
            sender_username="author", message_text=translation,
            category="HOT", task="Сайт с оплатой", service="website",
            evidence="I need a website for my shop",
            original_message_text=original, detected_language="en",
            translation_failed=False, sender_id=555,
            source_url="https://t.me/chat/10",
        )
        args, kwargs = bot.bot.send_message.call_args
        text = args[1]
        self.assertIn("Перевод на русский", text)
        self.assertLess(text.index("Мне нужен сайт"), text.index("Оригинал"))
        self.assertIn("🎯 Задача: Сайт с оплатой", text)
        self.assertIn("Основание:", text)
        self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 3500)
        urls = [b.url for row in kwargs["reply_markup"].inline_keyboard
                for b in row if getattr(b, "url", None)]
        self.assertIn("https://t.me/author", urls)
        self.assertIn("https://t.me/chat/10", urls)

    async def test_failed_translation_shows_fallback_and_original(self):
        bot = self._bot_stub()
        original = "I need a website for my shop, contact me please."
        await bot.notify_lead(
            lead_id=8, chat_name="Chat", sender_name="Author",
            sender_username=None, message_text=original, category="HOT",
            original_message_text=original, detected_language="en",
            translation_failed=True, sender_id=555,
        )
        text = bot.bot.send_message.call_args[0][1]
        self.assertIn("Перевод временно недоступен", text)
        self.assertIn(original, text)

    async def test_warm_cold_marks_potential_need_not_order(self):
        bot = self._bot_stub()
        await bot.notify_cold_lead(
            lead_id=9, chat_name="Chat", sender_name="Author",
            sender_username=None, message_text="Все заявки ведём вручную",
            category="WARM_COLD", pain="Заявки теряются при ручном учёте",
            sender_id=555,
        )
        text = bot.bot.send_message.call_args[0][1]
        self.assertIn("Возможная потребность #9", text)
        self.assertIn("Заказ на разработку пока не заявлен", text)
        self.assertLess(text.index("Потребность"), text.index("Сообщение"))


def _http_status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.test/chat")
    return httpx.HTTPStatusError("err", request=request,
                                 response=httpx.Response(code, request=request))


class GigaChatSerializationTests(unittest.IsolatedAsyncioTestCase):
    """_gigachat_chat: сериализация через лок + ретрай на 429."""

    def _fresh_lock(self):
        return patch.object(ai_engine, "_gigachat_request_lock", asyncio.Lock())

    async def test_concurrent_calls_are_serialized(self):
        active = 0
        max_active = 0

        async def once(model, messages, **kwargs):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return "ok"

        with self._fresh_lock(), \
             patch.object(ai_engine, "_gigachat_chat_once",
                          AsyncMock(side_effect=once)):
            results = await asyncio.gather(
                ai_engine._gigachat_chat("m", []),
                ai_engine._gigachat_chat("m", []))
        self.assertEqual(results, ["ok", "ok"])
        self.assertEqual(max_active, 1)

    async def test_429_retried_then_success(self):
        once = AsyncMock(side_effect=[_http_status_error(429), "ok"])
        with self._fresh_lock(), \
             patch.object(ai_engine, "_gigachat_chat_once", once), \
             patch.object(ai_engine.asyncio, "sleep", AsyncMock()) as sleep_mock:
            result = await ai_engine._gigachat_chat("m", [])
        self.assertEqual(result, "ok")
        self.assertEqual(once.call_count, 2)
        sleep_mock.assert_called_once_with(2)

    async def test_non_429_propagates_without_retry(self):
        once = AsyncMock(side_effect=_http_status_error(500))
        with self._fresh_lock(), \
             patch.object(ai_engine, "_gigachat_chat_once", once), \
             patch.object(ai_engine.asyncio, "sleep", AsyncMock()):
            with self.assertRaises(httpx.HTTPStatusError):
                await ai_engine._gigachat_chat("m", [])
        self.assertEqual(once.call_count, 1)


if __name__ == "__main__":
    unittest.main()
