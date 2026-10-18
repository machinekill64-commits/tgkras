# Промпт для развёртывания через Devin (GLM-5.2)

Скопируйте текст ниже (начиная с «## ПРОМПТ ДЛЯ DEVIN») и отправьте Devin в новом чате.
Devin развернёт систему на вашем сервере и соберёт десктоп-приложение.

---

## ПРОМПТ ДЛЯ DEVIN

Ты — DevOps-инженер. Разверни Telegram-систему приглашений на форум Forum Outreach Krasnodar (tgkras) на моём VDS-сервере с нуля, настрой прокси для обхода блокировок Telegram, и собери десктоп-приложение для Windows.

Действуй пошагово. На каждом шаге проверяй результат и исправляй ошибки прежде чем идти дальше.

### Что мне нужно подготовить заранее

Перед началом подготовь эти данные — Devin их спросит:

**1. Telegram API credentials** (получить на https://my.telegram.org → API development tools):
- `TG_API_ID` — число (например 12345678)
- `TG_API_HASH` — строка (например abc123def456...)
- `TG_PHONE` — номер телефона аккаунта в формате +79991234567

**2. Notification bot** (создать через @BotFather в Telegram):
- `NOTIF_BOT_TOKEN` — токен бота (например 7890123:ABCdefGHIjkl...)
- `OWNER_TG_ID` — твой Telegram ID (узнать через @userinfobot)

**3. GigaChat API** (бесплатно для резидентов РФ, регистрация на https://developers.sber.ru/portal/products/gigachat):
- `GIGACHAT_API_KEY` — base64-строка client_id:client_secret
- `GIGACHAT_SCOPE` = `GIGACHAT_API_PERS`

**4. Прокси для Telegram** (обязательно если сервер в РФ — Telegram заблокирован):
- Вариант A: MTProto прокси (host, port, secret)
- Вариант B: SOCKS5 прокси (host, port, username, password)
- Вариант C: HTTP прокси (host, port)
- Если сервер НЕ в РФ — прокси не нужен

**5. Прокси для Bot API** (aiogram — для бота уведомлений):
- MTProto НЕ работает с Bot API. Нужен SOCKS5 или HTTP прокси.
- Формат: `socks5://user:pass@host:1080` или `http://host:8080`

Спроси у меня все значения перед началом. НЕ придумывай их сам.

---

### Шаг 1. Подготовка сервера

Сервер: Linux VDS (Ubuntu 22.04+ или Debian 12+), минимум 1 vCPU / 1 ГБ RAM (рекомендуется 2 ГБ RAM).

```bash
# Обновить систему
sudo apt update && sudo apt upgrade -y

# Установить системные пакеты
sudo apt install -y python3 python3-venv python3-pip git curl

# Проверить версию Python (нужно 3.11+)
python3 --version
```

Если Python < 3.11 — установи новую версию:
```bash
sudo apt install -y software-properties-common
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3.12-dev
```

### Шаг 2. Клонирование проекта

```bash
cd /opt
sudo git clone https://github.com/machinekill64-commits/tgkras.git lead-hunter
cd /opt/lead-hunter
sudo chown -R $(whoami) /opt/lead-hunter
```

### Шаг 3. Виртуальное окружение и зависимости

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Проверь что все импорты работают:
```bash
python -c "import telethon, aiogram, aiohttp, aiosqlite, apscheduler; print('Все зависимости установлены')"
```

Если ошибка — установи недостающие пакеты по одному.

### Шаг 4. Настройка .env

```bash
cp .env.example .env
nano .env
```

Заполни ВСЕ обязательные поля. Вот пример заполненного .env:

```ini
# === Telegram User Account ===
TG_API_ID=12345678
TG_API_HASH=abc123def456ghi789
TG_PHONE=+79991234567
TG_SESSION_NAME=work_account

# === Прокси для Telethon (user client) ===
# ВАРИАНТ A: MTProto прокси (если есть)
TG_MTPROTO_ENABLED=true
TG_MTPROTO_HOST=xxx.xxx.xxx.xxx
TG_MTPROTO_PORT=8444
TG_MTPROTO_SECRET=dd1234567890abcdef1234567890abcdef

# ВАРИАНТ B: SOCKS5 прокси (если MTProto нет)
# TG_PROXY_TYPE=socks5
# TG_PROXY_HOST=xxx.xxx.xxx.xxx
# TG_PROXY_PORT=1080
# TG_PROXY_USERNAME=user
# TG_PROXY_PASSWORD=pass

# ВАРИАНТ C: без прокси (сервер НЕ в РФ)
# Оставь все прокси-поля пустыми

# === Прокси для Bot API (aiogram) ===
# MTProto НЕ работает с Bot API! Нужен SOCKS5 или HTTP
TG_BOT_PROXY=socks5://user:pass@xxx.xxx.xxx.xxx:1080
# Или без прокси (сервер НЕ в РФ):
# TG_BOT_PROXY=""

# === Notification Bot ===
NOTIF_BOT_TOKEN=7890123:ABCdefGHIjklMNOpqrSTUvwx
OWNER_TG_ID=123456789

# === GigaChat ===
GIGACHAT_API_KEY=base64_string_here
GIGACHAT_SCOPE=GIGACHAT_API_PERS

# === AI Settings ===
DIALOG_AI_PROVIDER=gigachat
DIALOG_MODEL=GigaChat-Pro
CLASSIFY_AI_PROVIDER=gigachat
CLASSIFY_MODEL=GigaChat-2

# === Anti-Ban ===
ANTI_BAN_MIN_DELAY=30
ANTI_BAN_MAX_DELAY=300
ANTI_BAN_HOURLY_LIMIT=10
ANTI_BAN_DAILY_LIMIT=37
WARMUP_DAYS=0
MAX_MESSAGE_AGE_DAYS=30

# === Database ===
DB_PATH=data/bot.db

# === Scheduler ===
SCHEDULER_TIMEZONE=Europe/Moscow

# === HTTP API ===
API_TOKEN=придумай_случайный_токен_32_символа

# === Developer ===
DEVELOPER_GENDER=male
```

**ВАЖНО про прокси:**
- Telethon (user client) поддерживает MTProto и SOCKS5
- aiogram (Bot API) поддерживает только SOCKS5 или HTTP, НЕ MTProto
- Если сервер в РФ — нужны прокси для ОБЕИХ частей
- Если сервер НЕ в РФ — оставь все прокси-поля пустыми

### Шаг 5. Настройка SWAP (если RAM < 2 ГБ)

Если на сервере 1 ГБ RAM — обязательно создай swap-файл:

```bash
# Проверить есть ли уже swap
swapon --show

# Если нет — создать 1 ГБ swap
sudo fallocate -l 1G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile

# Добавить в fstab для сохранения после перезагрузки
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# Проверить
free -h
```

### Шаг 6. Инициализация Telegram-сессии

Это самый важный шаг — авторизация Telegram-аккаунта.

```bash
cd /opt/lead-hunter
source .venv/bin/activate
python -c "
from user_client import UserClient
import asyncio
async def init():
    client = UserClient()
    await client.start_interactive()
asyncio.run(init())
"
```

Процесс:
1. Программа попросит ввести код подтверждения — Telegram пришлёт его в приложение
2. Введи код когда попросит
3. Если включена 2FA — введи пароль облака Telegram
4. Должно появиться «Успешная авторизация» или похожее

После успешной авторизации появится файл `work_account.session`:
```bash
ls -la *.session
# Должен быть файл work_account.session
```

**Если ошибка подключения:**
- `ConnectionError` / `TimeoutError` → прокси не работает, проверь `TG_PROXY_*` или `TG_MTPROTO_*`
- `AuthKeyError` → неверный API_ID/API_HASH
- `PhoneCodeInvalidError` → неверный код подтверждения

**Если прокси не работает — попробуй альтернативный:**
- MTProto не работает → переключи на SOCKS5 (заполни `TG_PROXY_*`, очисти `TG_MTPROTO_*`)
- SOCKS5 не работает → попробуй HTTP-прокси (`TG_PROXY_TYPE=http`)
- Ничего не работает → проверь что прокси вообще живёт: `curl -x socks5://host:port https://api.telegram.org`

### Шаг 7. Тестовый запуск

```bash
cd /opt/lead-hunter
source .venv/bin/activate
python main.py
```

Проверь в логах:
- `Telethon: ... proxy ...` или `Telethon: прямое подключение` — подключение Telegram
- `Bot API через прокси` или `Bot API: прямое подключение` — бот уведомлений
- `✅ Система запущена! Ожидание лидов...`
- `HTTP API запущен на порту 8080`

**Проверь HTTP API:**
```bash
curl http://localhost:8080/status
# Должен вернуть JSON со статусом
```

**Проверь бота уведомлений:**
- Открой Telegram, найди своего бота, отправь `/start`
- Бот должен ответить с меню

Если всё работает — останови процесс (`Ctrl+C`) и переходи к systemd.

**Частые ошибки и решения:**

| Ошибка | Причина | Решение |
|--------|---------|---------|
| `ConnectionError` Telethon | Прокси не работает | Проверь прокси, попробуй другой тип |
| `ChatNotFound` Bot API | Bot API не может подключиться | Заполни `TG_BOT_PROXY` (SOCKS5/HTTP) |
| `401 Unauthorized` GigaChat | Неверный API ключ | Проверь `GIGACHAT_API_KEY` |
| `403 Forbidden` GigaChat | Неверный scope | Проверь `GIGACHAT_SCOPE=GIGACHAT_API_PERS` |
| `Address already in use` 8080 | Порт занят | `lsof -i:8080` и убей процесс |
| `ModuleNotFoundError` | Не все пакеты установлены | `pip install -r requirements.txt` |

### Шаг 8. Настройка systemd-сервиса

Создай файл сервиса:
```bash
sudo tee /etc/systemd/system/lead-hunter.service > /dev/null << 'EOF'
[Unit]
Description=Lead Hunter — Telegram Lead Generation
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/lead-hunter
ExecStart=/opt/lead-hunter/.venv/bin/python /opt/lead-hunter/main.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
```

Активируй:
```bash
sudo systemctl daemon-reload
sudo systemctl enable lead-hunter
sudo systemctl start lead-hunter
sudo systemctl status lead-hunter
# Должно быть: active (running)
```

Проверь логи:
```bash
journalctl -u lead-hunter -f --no-pager
# Должны быть те же сообщения что и при ручном запуске
```

### Шаг 9. Настройка файрвола

Открой порт 8080 для HTTP API:

```bash
# Если используется ufw
sudo ufw allow 8080/tcp
sudo ufw allow ssh
sudo ufw enable
sudo ufw status

# Если используется iptables
sudo iptables -A INPUT -p tcp --dport 8080 -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 22 -j ACCEPT
```

Проверь доступность снаружи:
```bash
curl http://ВАШ_СЕРВЕР_IP:8080/status
```

### Шаг 10. Сборка десктоп-приложения (Windows)

На Windows-машине (твой компьютер):

```bash
git clone https://github.com/machinekill64-commits/tgkras.git
cd tgkras
pip install -r requirements.txt
python build_exe.py
```

Рядом с `dist/LeadHunter.exe` создай файл `.env`:
```
SERVER_URL=http://ВАШ_СЕРВЕР_IP:8080
API_TOKEN=ТОТ_ЖЕ_ТОКЕН_ЧТО_НА_СЕРВЕРЕ
```

Запусти `dist/LeadHunter.exe`. Программа подключится к серверу.
В строке статуса должно быть «РАБОТАЕТ | чатов: N».

### Шаг 11. Полная проверка

1. **Telegram бот**: открой своего бота, отправь `/start` — должно появиться меню
2. **GUI**: нажми «СИНХРОН» — загрузятся чаты из Telegram
3. **Выбери чат**: включи «ЧИТАТЬ», задай правила, нажми «СОХРАНИТЬ»
4. **Тест AI**: нажми «ТЕСТ» — AI сгенерирует тестовое сообщение
5. **Парсинг**: нажми «ЗАПУСК» — статус должен измениться на «РАБОТАЕТ»
6. **Прокси проверка**: в логах сервера должно быть `Telethon: ... proxy ...` (не «прямое подключение» если сервер в РФ)

### Настройка чатов в GUI

Для каждого чата:
- **ЧИТАТЬ** — включить поиск лидов в чате
- **РАССЫЛКА** — включить AI-рассылку
- **ПРАВИЛА ЧАТА** — вставить текст правил чата (или отметить «ПРАВИЛ НЕТ»)
- **ВРЕМЯ РАССЫЛКИ** — формат: `10:00, 19:30` (через запятую)
- **ТЕМАТИКА ЧАТА** — например «крипта P2P», «рестораны», «недвижимость»
- **ЯЗЫК РАССЫЛКИ** — выбрать язык чата:
  - Русский — для русскоязычных чатов
  - English — для англоязычных
  - العربية — для арабских чатов (Саудовская Аравия)
  - Português — для бразильских чатов
  - Indonesia — для индонезийских чатов
  - Қазақша / O'zbekcha / Azərbaycan — для СНГ
- **ПОЛ РАЗРАБОТЧИКА** — male или female (влияет на тексты AI)

### Где взять прокси для Telegram

**Бесплатные MTProto прокси:**
- https://t.me/mtproto (канал с бесплатными прокси)
- Поиск в Telegram: «mtproto proxy»
- Формат: host, port, secret (hex-строка)

**Платные SOCKS5 прокси:**
- proxy6.net — от 99 ₽/мес
- proxy-seller.ru — от 100 ₽/мес
- onlineproxy.ru — от 150 ₽/мес

**Бесплатные SOCKS5 (не стабильные):**
- https://free-proxy-list.net
- https://spys.one/proxys/socks/
- Не рекомендуются для продакшена

**Настройка в .env:**
- MTProto: `TG_MTPROTO_HOST`, `TG_MTPROTO_PORT`, `TG_MTPROTO_SECRET`
- SOCKS5: `TG_PROXY_TYPE=socks5`, `TG_PROXY_HOST`, `TG_PROXY_PORT`, `TG_PROXY_USERNAME`, `TG_PROXY_PASSWORD`
- Bot API: `TG_BOT_PROXY=socks5://user:pass@host:port`

### Важные замечания

1. **НЕ коммить `.env` в git** — там секреты. Файл уже в `.gitignore`
2. **НЕ удаляй `*.session` файлы** — это авторизация Telegram. Без них придётся заново входить
3. **GigaChat лимиты** — бесплатный тариф имеет ограничения. Если упрётесь — переключи на `GigaChat-Pro` или используй запасной провайдер (Groq, регистрация на console.groq.com)
4. **Анти-бан** — не включай рассылку сразу во всех чатах. Начни с 2-3, постепенно увеличивай
5. **Прокси для РФ обязателен** — без прокси Telegram не подключится с российского IP
6. **Переводчик** — MyMemory API бесплатный (5000 слов/день). При превышении — GigaChat fallback
7. **База данных** — SQLite, файл `data/bot.db`. Для бэкапа просто скопируй файл
8. **Бэкапы** — настрой регулярное копирование `data/bot.db` и `*.session`:
   ```bash
   # Пример cron-задачи на каждый день в 3 ночи
   0 3 * * * cp /opt/lead-hunter/data/bot.db /opt/lead-hunter/data/bot.db.bak
   ```

### Команды бота уведомлений

После запуска отправь боту `/start` — откроется меню:
- 🔥 Лиды — список найденных лидов
- 💬 Диалоги — активные AI-диалоги с клиентами
- 🔍 Поиск чатов — поиск новых чатов по ключевым словам
- ❄️ Холодный обход — поиск косвенных лидов
- 📈 Статистика — метрики за день/неделю
- ⚙️ Настройки — параметры системы
- ⏸ Остановить парсинг / ▶️ Запустить парсинг

### Траблшутинг

**Сервер не запускается:**
```bash
journalctl -u lead-hunter -n 50 --no-pager
```

**Telegram не подключается:**
```bash
# Проверь прокси
curl -x socks5://user:pass@host:port https://api.telegram.org
# Должен вернуть {"ok":true...}
```

**GigaChat не работает:**
```bash
cd /opt/lead-hunter
source .venv/bin/activate
python -c "
from ai_engine import _get_gigachat_token
import asyncio
print('Token:', asyncio.run(_get_gigachat_token())[:20], '...')
"
```

**HTTP API не доступен с Windows:**
```bash
# На сервере проверь
curl http://localhost:8080/status
# С Windows
curl http://ВАШ_СЕРВЕР_IP:8080/status
# Если не работает — проверь файрвол
sudo ufw status
```

**GUI не подключается:**
- Проверь `.env` рядом с `LeadHunter.exe` — `SERVER_URL` и `API_TOKEN`
- Проверь что сервер доступен: `ping ВАШ_СЕРВЕР_IP`
- Проверь что `API_TOKEN` в GUI совпадает с `API_TOKEN` на сервере

**Перезапуск после изменений:**
```bash
sudo systemctl restart lead-hunter
sudo systemctl status lead-hunter
```

**Обновление кода:**
```bash
cd /opt/lead-hunter
git pull origin main
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart lead-hunter
```

---

## КОНЕЦ ПРОМПТА

Скопируй всё выше (начиная с «## ПРОМПТ ДЛЯ DEVIN») и отправь Devin.
Devin попросит тебя ввести значения для .env — подготовь их заранее.
