"""Trial bot — вторая копия бота для тестового клиента (7 дней).

Запуск:
  1. Скопируйте trial.env.example в trial.env
  2. Заполните данные клиента
  3. Запустите: python trial_bot.py

Особенности:
  - Отдельная база данных (data/trial_bot.db)
  - Отдельный порт HTTP API (8081 по умолчанию)
  - Тот же прокси что у основного бота
  - Проверка срока триала — после истечения бот останавливается
  - Исходный код НЕ передаётся клиенту
"""
import asyncio
import logging
import sys
import os
from datetime import datetime, date
from dotenv import load_dotenv

# Загружаем trial.env ПЕРЕД всеми импортами
load_dotenv("trial.env")

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [TRIAL] [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("trial_bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("trial_bot")

# Создаём директории
os.makedirs("data", exist_ok=True)
os.makedirs("logs", exist_ok=True)

# Проверка срока триала
TRIAL_EXPIRES = os.getenv("TRIAL_EXPIRES", "")
if TRIAL_EXPIRES:
    try:
        expires = datetime.strptime(TRIAL_EXPIRES, "%Y-%m-%d").date()
        today = date.today()
        if today > expires:
            logger.error(f"❌ Триал истёк {TRIAL_EXPIRES}. Бот не запущен.")
            print(f"❌ Триал истёк {TRIAL_EXPIRES}. Обратитесь к владельцу для продления.")
            sys.exit(1)
        days_left = (expires - today).days
        logger.info(f"⏰ Триал активен до {TRIAL_EXPIRES}. Осталось {days_left} дней.")
        print(f"⏰ Триал активен до {TRIAL_EXPIRES}. Осталось {days_left} дней.")
    except ValueError:
        logger.warning(f"Неверный формат TRIAL_EXPIRES: {TRIAL_EXPIRES}")
else:
    logger.warning("TRIAL_EXPIRES не указан — триал без ограничения по времени")

# Теперь импортируем основные модули
import config
import database as db
from user_client import UserClient
from notification_bot import NotificationBot
from scheduler import MessageScheduler
from aiohttp import web


async def main():
    """Главная функция trial-бота."""
    # Проверка конфигурации
    errors = []
    if not config.TG_API_ID or not config.TG_API_HASH:
        errors.append("TG_API_ID и TG_API_HASH не настроены")
    if not config.NOTIF_BOT_TOKEN:
        errors.append("NOTIF_BOT_TOKEN не настроен")
    if not config.OWNER_TG_ID:
        errors.append("OWNER_TG_ID не настроен")
    has_ai_key = (config.OMNIROUTE_API_KEY or config.GROQ_API_KEY or
                 config.OPENROUTER_API_KEY or config.OPENAI_API_KEY or
                 config.DEEPSEEK_API_KEY or config.GIGACHAT_API_KEY)
    if not has_ai_key:
        errors.append("Нужен хотя бы один AI провайдер")

    if errors:
        for e in errors:
            logger.error(f"Конфигурация: {e}")
        print("❌ Ошибки конфигурации. Проверьте trial.env")
        sys.exit(1)

    logger.info("Запуск trial-бота...")

    # Инициализация базы данных
    await db.init_db()
    logger.info(f"База данных: {config.DB_PATH}")

    # Создание компонентов
    notif_bot = NotificationBot()

    async def notification_router(**kwargs):
        """Маршрутизатор уведомлений для trial."""
        from lead_presentation import prepare_lead_notification
        kwargs = await prepare_lead_notification(kwargs)
        if kwargs.get("category", "") in ("HOT_COLD", "WARM_COLD"):
            await notif_bot.notify_cold_lead(**kwargs)
        else:
            await notif_bot.notify_lead(**kwargs)

    user_client = UserClient(
        notification_callback=notification_router
    )
    notif_bot.user_client = user_client

    async def broadcast_notify(text: str):
        await notif_bot.bot.send_message(config.OWNER_TG_ID, text)

    scheduler = MessageScheduler(user_client=user_client,
                                 broadcast_notify_callback=broadcast_notify)
    notif_bot.scheduler = scheduler

    # Запуск
    logger.info("Запуск рабочего аккаунта (Telethon)...")
    await user_client.start()

    logger.info("Запуск планировщика...")
    await scheduler.start()

    logger.info("Запуск бота уведомлений...")
    bot_task = asyncio.create_task(notif_bot.start())

    logger.info("✅ Trial-бот запущен!")

    # HTTP API на отдельном порту
    @web.middleware
    async def auth_middleware(request, handler):
        if config.API_TOKEN:
            token = request.headers.get("X-API-Token", "")
            if token != config.API_TOKEN:
                return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    app = web.Application(middlewares=[auth_middleware])

    async def get_chats(request):
        chats = await db.get_all_chats()
        return web.json_response({"chats": chats})

    async def api_update_chat(request):
        data = await request.json()
        chat_id = data.get("chat_id")
        if not chat_id:
            return web.json_response({"error": "chat_id required"}, status=400)
        await db.update_chat(
            chat_id=chat_id,
            is_monitored=data.get("is_monitored"),
            is_broadcast=data.get("is_broadcast"),
            chat_rules=data.get("chat_rules"),
            broadcast_times=data.get("broadcast_times"),
            message_text=data.get("message_text"),
            chat_niche=data.get("chat_niche"),
            broadcast_language=data.get("broadcast_language"),
        )
        await user_client.reload_chats()
        await scheduler.reload_jobs()
        return web.json_response({"success": True})

    async def api_status(request):
        daily = await db.get_daily_actions_count()
        chats = await db.get_monitored_chats()
        return web.json_response({
            "running": user_client.is_running,
            "paused": user_client.is_paused,
            "daily_actions": daily,
            "monitored_chats": len(chats),
            "trial": True,
            "trial_expires": TRIAL_EXPIRES,
        })

    async def api_pause(request):
        await user_client.pause()
        return web.json_response({"success": True, "paused": True})

    async def api_resume(request):
        await user_client.resume()
        return web.json_response({"success": True, "paused": False})

    app.router.add_get('/api/chats', get_chats)
    app.router.add_post('/api/chats', api_update_chat)
    app.router.add_get('/api/status', api_status)
    app.router.add_post('/api/pause', api_pause)
    app.router.add_post('/api/resume', api_resume)

    runner = web.AppRunner(app)
    await runner.setup()
    api_port = int(os.getenv("API_PORT", "8081"))
    site = web.TCPSite(runner, '0.0.0.0', api_port)
    await site.start()
    logger.info(f"HTTP API запущен на порту {api_port}")

    try:
        await bot_task
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("Остановка trial-бота...")
        await runner.cleanup()
        await scheduler.stop()
        await user_client.stop()
        await notif_bot.stop()
        logger.info("Trial-бот остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Прервано пользователем")
    except Exception as e:
        logger.error(f"Фатальная ошибка: {e}", exc_info=True)
        sys.exit(1)
