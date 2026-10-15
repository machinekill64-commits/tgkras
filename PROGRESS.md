# LeadHunter — ход работ

## 2026-09-25 — исходное состояние (Astra)
- Базовый снапшот git, план из 6 пунктов. Astra начала работу, квота кончилась.

## 2026-09-25 — что сделано (Astra + Devin)

### Готово и проверено тестами (26/26 OK, все файлы компилируются)

**1. Детектор (lead_detector.py + config.py) — Astra + фиксы Devin**
- Новый фильтр: обязательная связка buyer-intent AND service-scope (`LEAD_BUYER_INTENT`, `LEAD_SERVICE_SCOPES`, `LEAD_HARD_EXCLUDES` в config.py).
- Жёсткие отсевы: кредиты/финансы/крипта, «темки»/«поднять денег»/доход-в-сутки, рекрутинг/подработка, самопиар исполнителей, казино/ставки.
- `интересно` standalone НЕ лид (broadcast-паттерны больше не создают лид сами по себе).
- Подстроки исправлены границами слов: `доставки`, `склад` (автоматизация), `реферальная` проходят.
- Scope включает devops: linux, vpn, docker, nginx, бэкап, deploy, настройка/администрирование сервера.
- `_extract_budget`: «Бюджет 50000»→50000, «от 1 до 20 млн» без слова бюджет→None, «за 5 дней»→None (срок, не бюджет), валюты/диапазоны.
- `_extract_task`: берёт строку с intent+scope, не случайную.
- Холодные: контекст+процесс+боль обязательны; добавлены ручная работа/эксель/бумага/on paper; отсев макро-новостей (ВВП, Росстат, инфляция, ЦБ).
- LeadResult: новые поля service, evidence.

**2. БД (database.py) — Astra**
- Миграции leads/cold_leads: source_message_id, source_url, sender_type, contact_username, contact_source, service, evidence.
- add_lead/add_cold_lead принимают **metadata; sender_id nullable в новой схеме (в старой БД NOT NULL → в коде пишем 0).

**3. user_client.py — Devin**
- `_sender_metadata(sender, text)`: sender=None не падает; sender_type user/bot/channel/unknown; alias usernames; контакт «пишите @…»/t.me из текста помечается contact_source='text'.
- `_source_url`: public → t.me/name/msgid; private supergroup → t.me/c/id/msgid; basic group → None.
- Метаданные + source_url пишутся в leads/cold_leads и передаются в notification_callback.
- **Персистентная пауза исходящих**: setting `outbound_paused` в БД; pause()/resume() пишут в БД; start() читает обратно.
- Блок исходящих = последний рубеж в `_send_message` и `send_scheduled_message`; гейт в start_dialog_with_(cold_)lead; sender_type!=user или sender_id=0 → диалог не начинается.
- Мониторинг НЕ блокируется паузой: on_new_message и owner-intervention работают; входящие ЛС логируются, автоответы пропускаются.
- `_send_and_record` при паузе не спамит owner'у human_required.

**4. notification_bot.py — Astra + Devin**
- `_lead_contact`: username → t.me кнопка; контакт из текста → отдельная кнопка; нет username → ID + tg:// fallback; ни того ни другого → честная надпись.
- `_keep_url_buttons`: после edit_text колбэков сохраняются URL-кнопки (контакт/источник). Применено ко всем колбэкам: close_, approve_chat_, reject_chat_, write_, ignore_, cold_write_, cold_ignore_, takeover_.
- `_can_write_client`: «Написать клиенту» только для sender_type user (или старых записей с sender_id).
- notify_lead/notify_cold_lead принимают: source_url, sender_type, contact_*, service, evidence, original_message_text, detected_language (фикс краша переведённых лидов).
- Кнопка «🔗 Исходное сообщение» при наличии source_url; вместо фикс-скора 70/100 показывается «🛠 Услуга» для новых лидов.
- Тексты кнопок паузы переписаны: «исходящие заблокированы, мониторинг идёт».

**5. trial_bot.py / cold_outreach.py / scheduler.py — Devin**
- trial router: HOT_COLD/WARM_COLD → notify_cold_lead.
- approve_and_send_cold_message: проверка outbound_paused из БД.
- scheduler уже проверял is_paused → теперь это именно блок исходящих (корректно).

### Тесты
- test_lead_detector.py: 11 тестов — весь обязательный набор кейсов (26 сообщений accept, ~40 reject, 20 кейсов бюджета, cold-гейты, контакты).
- Полный прогон `python -m unittest`: 26 тестов OK.

### НЕ сделано / осталось
- [ ] Выкладка на сервер `/opt/lead-hunter` (82.202.170.14): бэкап изменённых файлов → scp → `systemctl restart lead-hunter` → проверка active+Telethon+outbound_paused. ТОЛЬКО после «го» пользователя.
- [ ] Смена пароля root (утёк в чат) + SSH-ключи.
- [ ] Проверить на проде: уведомления со ссылкой-источником, sender=None не падает.

### Предупреждения
- AI в первичный отбор НЕ возвращать. Диалоги/рассылки не включать без подтверждения.
- Исторические лиды не трогать. Не коммитить/пушить. Секреты не выводить.
- `tg://user?id=` — fallback, может не открыться при приватности; основной путь — кнопка источника.


## 2026-09-29 — антиспам v2 + детектор языка + поиск чатов

- lead_detector.py: добавлены _seller_ad_patterns (самопиар: «если вам нужен», «помогу», «I provide», «DM me», «представляю агентство», «возьму на себя»), _hashtag_wall_pattern (5+ хэштегов подряд — рекламная карточка), USDT/gift-card/crypto-exchange в _spam_scam_patterns. Проверяются в L0 и в cold-пути.
- translator.py: detect_language переписан — кириллица по спецбуквам kk/uk (иначе ru), латиница по word-boundary словарям en/pt/id/es/tr/uz/az (неанглийскому нужно ≥2 маркеров). Раньше русский текст с «сайт/бот» ошибочно помечался казахским, английский — индонезийским.
- test_lead_detector.py: +4 боевых спам-кейса (лиды #395-#398).
- Новые скрипты на сервере: hunt_chats.py (поиск живых чатов по активности за 48ч, копия сессии), join_monitored.py (вступление+мониторинг). Найдено 21 живой чат из 49; 6 выбранных пользователем добавлены в мониторинг → 63 чата.
- Все тесты проходят, файлы задеплоены, сервис перезапущен.


## 2026-09-29 — семантическая квалификация лидов + перевод карточек (runtime-интеграция)

### Что сделано локально
- **user_client.py**: keyword detect_lead/detect_cold_lead заменены на `qualify_lead` из lead_qualification.py — единый финальный фильтр для main и trial. Новый семафор `self._lead_semaphore = asyncio.Semaphore(2)` (диалоговый `_ai_semaphore` не тронут). `WARM_COLD` → `_process_cold_lead` с вердиктом `cold_WARM_COLD`; при `review_unavailable` запись в спам-кэше откатывается для ретрая. Дедуп: sha256 вместо md5, убран `[:500]` из нормализации, проверка кэша теперь учитывает TTL.
- **lead_presentation.py (новый)**: `prepare_lead_notification(kwargs)` — копия kwargs; только категории HOT/WARM/HOT_COLD/WARM_COLD; русский возвращается без изменений; для остальных — перевод через `ai_engine._gigachat_chat` (модель = lead_qualification.REVIEW_MODEL, temperature=0.1, max_tokens=2200), чанки по 1500 символов с разрывом по пробелу/переводу, общий `asyncio.wait_for` 30с. Любая ошибка/пустой/неизменённый/некириллический чанк → оригинал + `translation_failed=True` (никакого частичного перевода). `clip_text` режет по UTF-16 code units.
- **main.py / trial_bot.py**: старый inline-блок перевода (original[:800]+translate→truncate 500) заменён на `kwargs = await prepare_lead_notification(kwargs)` в обоих роутерах. `has_ai_key` теперь включает `config.GIGACHAT_API_KEY` (в trial убрана ссылка на несуществующие GIGACHAT_CLIENT_ID/SECRET).
- **notification_bot.py**: `translation_failed` параметр в notify_lead/notify_cold_lead; общий `_lead_message_block` — перевод сверху + оригинал (400) с меткой языка, при сбое «Перевод временно недоступен» + полный оригинал (1400); задача/бюджет/срок и pain до текста сообщения; цитата `Основание: «…»` (≤350 UTF-16); заголовок холодной карточки «Возможная потребность #…», для WARM_COLD строка «Заказ на разработку пока не заявлен.»; итоговый текст клипается до 3500 UTF-16. Убрано логирование URL прокси с credentials → `Bot API: proxy configured`.
- **ai_engine.py**: `_gigachat_token_expires = float(result['expires_at']) / 1000 if result.get('expires_at') else time.time() + 1800` — раньше epoch-миллисекунды прибавлялись к time.time(), ломая кэш токена.
- **test_lead_detector.py**: +14 тестов — qualify_lead с моком GigaChat (chatter без вызова, DIRECT→HOT, PAIN→WARM_COLD/indirect_disabled, REJECT, malformed/invented-quote/unknown-service/missing-fields/TimeoutError → review_unavailable без keyword-fallback), интеграция UserClient через __new__ с моками db/event (reject без add_lead/нотификации, PAIN только в cold-путь, DIRECT сохраняет текст/evidence/source_url), prepare_lead_notification (перевод en→ru, ru без вызова, timeout→fallback, не-лид категории не переводятся), clip_text по UTF-16, рендер notify_lead/notify_cold_lead с кнопками и новыми формулировками.

### Доработки после ревью
- **ai_engine.py**: `_gigachat_chat` переименован в `_gigachat_chat_once` (тело без изменений) + обёртка `_gigachat_chat` с модульным `_gigachat_request_lock = asyncio.Lock()` — сериализация ВСЕХ вызовов GigaChat (review, перевод, диалоги) и ретрай 429 (до 3 попыток, backoff 2/4с); не-429 пробрасывается без ретрая.
- **notification_bot.py**: цитата `Основание:` перенесена ВЫШЕ блока сообщения в обеих карточках — не клипается концом текста.
- **lead_presentation.py**: `_split_chunks` больше не теряет разделительный пробел на границе чанков.
- **Тесты**: исправлен `test_english_lead_translated_and_original_kept` (side_effect по чанкам, проверка склейки и сохранения слов), роутер-тест на AST-парсинг main.py/trial_bot.py без импорта (trial_bot имеет top-level env/exit), +3 теста сериализации GigaChat (max concurrent=1, 429→retry→ok, 500 без ретрая; лок пересоздаётся в каждом тесте).

### Проверено
- `python -X utf8 -m py_compile lead_qualification.py lead_presentation.py user_client.py main.py trial_bot.py notification_bot.py ai_engine.py` — OK.
- `python -X utf8 -m unittest test_lead_detector -v` — **31/31 OK**.

### Доп. тесты под новый контракт lead_qualification (policy автор — lead)
- +3 кейса в QualifyLeadTests: DIRECT без optional budget/deadline ключей → принят, поля None; PAIN с service=null → WARM_COLD, service None; рекламный каталог замоканный как DIRECT development с evidence без глаголов запроса → `semantic_rejected` (гейт `_direct_request` в evidence).
- `python -X utf8 -m unittest test_lead_detector.QualifyLeadTests -v` — **8/8 OK**; py_compile повторно OK.
- +2 кейса под финальный контракт: evidence casefold-матчинг (мок 'Ищу' vs source 'ищу' → принят); PAIN без `_pain`-маркера в evidence → `semantic_rejected` («дайте советы по mini app»).
- `python -X utf8 -m unittest test_lead_detector.QualifyLeadTests -v` — **10/10 OK**. В файле определено 36 тестов; полный файл последний раз гонялся при 31, далее прогонялись только QualifyLeadTests.

### Живая проверка и развёртывание 2026-10-05
- **lead_qualification.py**: новый обязательный семантический шлюз после быстрого отбора кандидатов. DIRECT → HOT, конкретная собственная проблема бизнеса PAIN → WARM_COLD, остальные сообщения отклоняются. GigaChat получает исходный текст только кандидата. Нужны допустимая услуга, русское описание и проверяемая цитата; DIRECT дополнительно требует выражения запроса в цитате, PAIN — конкретного признака проблемы и контекста бизнеса. Неверные ответы/ошибки API не заменяются согласием старого regex-фильтра. Неуказанные бюджет/срок остаются пустыми.
- **check_lead_quality.py**: воспроизводимая живая проверка на зафиксированных текстах; не отправляет сообщения в Telegram и не меняет БД. Материалы в игнорируемой `data/`: `quality_cases_snapshot.json`, `quality_results_v3.json`, `quality_revalidated_v3.json`, `quality_results_final_focused.json`.
- С сервера прочитаны полные тексты 20 присланных пользователем лидов; исторические записи не изменялись. Дополнены 26 контрольными примерами (12 прямых заказов, 5 конкретных проблем бизнеса, 9 отрицательных).
- Первый запуск выявил HTTP 429 при параллельных запросах GigaChat; отдельная проба подтвердила причину. Общая очередь и ограниченные повторные попытки внедрены в ai_engine.
- После калибровки промпта и проверки цитат сохранённые ответы GigaChat на 46 текстов дают ожидаемый итог фильтра во всех 46 случаях. ВАЖНО: 3 отрицательных текста отклоняются как невалидный ответ модели (неизвестная услуга/выдуманная составная цитата/неизвестная категория), а не как корректное решение REJECT. Поэтому это не «100% точность модели».
- Дополнительный живой прогон 12 текстов: 7 новых рекламных сообщений из свежего потока + 5 проблемных повторных проверок. 9 корректных решений, 3 невалидных ответа безопасно подавлены; ложных уведомлений нет. Реальная проверка перевода en→ru прошла, оригинал сохранён. Перевод машинный, качество формулировок не гарантируется.
- Развёрнуты только 7 файлов: ai_engine.py, main.py, trial_bot.py, user_client.py, notification_bot.py, lead_qualification.py, lead_presentation.py.
- Резервная копия 5 заменённых файлов и манифест: `/opt/lead-hunter/backups/quality_20261005_073351`. Два новых модуля до выкладки отсутствовали. `_deploy_quality.py` проверил SHA-256 всех 7 файлов, компиляцию и наличие сохранённой блокировки исходящих; при сбое предусмотрен откат.
- Сервис перезапущен. API: running=true, paused=true, monitored_chats=63, hourly_actions=0, daily_actions=0. Загружено 0 плановых и 0 AI-рассылок. Новые вступления/диалоги не запускались.
- После старта в messages_log уже появились `prefilter_rejected` и `semantic_rejected`: новый путь реально обрабатывает входящие сообщения. На момент проверки ошибок review не было.
- Ограничения: набор проверок небольшой и целевой; это не оценка точности на всём потоке. При сбое модели кандидат остаётся в messages_log с review_unavailable, автоматической очереди повторного разбора пока нет. Уведомления содержат русский смысл, цитату и перевод первым, но длинные тексты ограничены размером Telegram; полный оригинал хранится в БД и доступен по кнопке источника.
