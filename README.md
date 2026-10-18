# Forum Outreach Krasnodar — Telegram-система приглашений на форум

Автоматизированный бот: находит активных участниц тематических чатов
Краснодара и приглашает их на форум «Современная женщина» — диалоги
ведёт AI-продавец (GigaChat), доводя до покупки билета.

## Возможности

- **Мониторинг Telegram-чатов** — читает сообщения через Telethon (user account)
- **Детекция лидов** — многоуровневый фильтр: L0 (спам/возраст) → L1 (ключевые слова) → AI (GigaChat)
- **Прямые и косвенные лиды** — ищет как прямые запросы, так и сигналы бизнес-болей
- **AI-диалоги** — GigaChat Pro проводит продажи автономно: квалификация, цена, ТЗ, handoff
- **AI-рассылки** — нативная реклама в чатах с учётом правил (WTB/WTS, хэштеги, язык)
- **8 языков рассылок** — русский, English, العربية, Português, Indonesia, Қазақша, O'zbekcha, Azərbaycan
- **Бесплатный переводчик** — MyMemory API + GigaChat fallback, без API-ключей
- **Уведомления** — бот присылает лиды с переводом на русский + подпись страны
- **Десктоп GUI** — управление чатами, настройками, парсингом (бруталистский дизайн)
- **Анти-бан** — лимиты, задержки, прогрев аккаунта
- **Прайс-каталог** — детерминированные цены на услуги

## Архитектура

```
Сервер (Linux)                    Десктоп (Windows)
┌─────────────────┐              ┌──────────────┐
│ Telethon        │              │  GUI (Tkinter)│
│ aiogram bot     │◄── HTTP ───►│  LeadHunter   │
│ Scheduler       │   API 8080   │  .exe         │
│ GigaChat AI     │              └──────────────┘
│ SQLite DB       │
│ Translator      │
└─────────────────┘
```

**Сервер** — вся логика: Telethon, AI, планировщик, БД, бот уведомлений
**Десктоп** — тонкий клиент: управляет настройками через HTTP API сервера

## Требования

### Сервер (Linux VPS)
- Ubuntu 22.04+ / Debian 12+
- Python 3.11+
- 1 GB RAM минимум
- Доступ к Telegram (прямой или через прокси)
- GigaChat API ключ (бесплатно для резидентов РФ)

### Десктоп (Windows)
- Windows 10/11
- Python 3.11+ (для запуска из исходников) ИЛИ готовый `LeadHunter.exe`

## Быстрый старт

### 1. Сервер

```bash
# Клонировать репозиторий
git clone https://github.com/machinekill64-commits/tgkras.git
cd tgkras

# Создать виртуальное окружение
python3 -m venv .venv
source .venv/bin/activate

# Установить зависимости
pip install -r requirements.txt

# Создать .env из примера
cp .env.example .env
nano .env  # заполнить все значения

# Инициализация Telegram-сессии
python -c "from user_client import UserClient; import asyncio; asyncio.run(UserClient().start_interactive())"

# Запуск
python main.py

# Или как systemd-сервис (см. DEPLOY_PROMPT.md)
```

### 2. Десктоп GUI

```bash
# Из исходников
pip install -r requirements.txt
python main_gui.py

# Или собрать exe
python build_exe.py
# Рядом с dist/LeadHunter.exe создать .env:
#   SERVER_URL=http://ВАШ_СЕРВЕР:8080
#   API_TOKEN=ВАШ_ТОКЕН
```

## Настройка .env

См. `.env.example` — все переменные с комментариями.

**Обязательные:**
- `TG_API_ID`, `TG_API_HASH`, `TG_PHONE` — получить на https://my.telegram.org
- `NOTIF_BOT_TOKEN` — создать бота через @BotFather
- `OWNER_TG_ID` — ваш Telegram ID (через @userinfobot)
- `GIGACHAT_API_KEY` — получить на https://developers.sber.ru/portal/products/gigachat
- `API_TOKEN` — любой случайный токен для защиты HTTP API

## Структура проекта

```
├── main.py              # Сервер: HTTP API + запуск всех компонентов
├── main_gui.py          # Десктоп GUI (тонкий клиент)
├── config.py            # AI-промпты, ключевые слова, настройки
├── ai_engine.py         # GigaChat: классификация, диалоги, рассылки
├── lead_detector.py     # Многоуровневый фильтр лидов (L0/L1/AI)
├── sales_engine.py      # Логика продаж: квалификация, цена, handoff
├── price_catalog.py     # Каталог услуг и цен
├── user_client.py       # Telethon: чтение чатов, отправка сообщений
├── notification_bot.py  # aiogram: уведомления владельцу
├── scheduler.py         # APScheduler: рассылки, follow-ups
├── database.py          # SQLite: схема, миграции, запросы
├── translator.py        # Бесплатный переводчик (MyMemory + GigaChat)
├── build_exe.py         # PyInstaller: сборка LeadHunter.exe
├── .env.example         # Шаблон конфигурации
└── requirements.txt     # Python-зависимости
```

## Лицензия

Приватный проект. Использование только с разрешения автора.
