"""Точка входа: запуск всех компонентов системы."""
import asyncio
import logging
import sys
import os
from aiohttp import web

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")

# Создаём директории
os.makedirs("data", exist_ok=True)
os.makedirs("logs", exist_ok=True)

import config
import database as db
from user_client import UserClient
from notification_bot import NotificationBot
from scheduler import MessageScheduler


async def main():
    """Главная асинхронная функция: инициализация и запуск."""
    # Проверка конфигурации
    errors = []
    if not config.TG_API_ID or not config.TG_API_HASH:
        errors.append("TG_API_ID и TG_API_HASH не настроены")
    if not config.NOTIF_BOT_TOKEN:
        errors.append("NOTIF_BOT_TOKEN не настроен")
    if not config.OWNER_TG_ID:
        errors.append("OWNER_TG_ID не настроен")
    # OmniRoute работает без ключа (ключ по умолчанию "omni"), так что проверяем только если провайдер не omniroute
    has_ai_key = (config.OMNIROUTE_API_KEY or config.GROQ_API_KEY or
                  config.OPENROUTER_API_KEY or config.OPENAI_API_KEY or
                  config.DEEPSEEK_API_KEY or config.GIGACHAT_API_KEY)
    if not has_ai_key:
        errors.append("Нужен хотя бы один AI провайдер. Бесплатно: OmniRoute (npm install -g omniroute) или Groq (console.groq.com)")

    if errors:
        logger.error("Ошибки конфигурации:")
        for e in errors:
            logger.error(f"  - {e}")
        logger.error("Скопируйте .env.example в .env и заполните настройки")
        sys.exit(1)

    # Инициализация БД
    logger.info("Инициализация базы данных...")
    await db.init_db()

    # Создание компонентов
    notif_bot = NotificationBot()

    async def notification_router(**kwargs):
        """Маршрутизатор уведомлений: обычные лиды -> notify_lead,
        холодные лиды -> notify_cold_lead. Переводит иностранные лиды на русский."""
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

    logger.info("✅ Система запущена! Ожидание лидов...")
    logger.info("Управление через бота уведомлений. Команда: /help")

    # Запуск HTTP API для синхронизации с GUI
    @web.middleware
    async def auth_middleware(request, handler):
        """Авторизация по токену: без правильного X-API-Token доступ запрещён."""
        if config.API_TOKEN:
            token = request.headers.get("X-API-Token", "")
            if token != config.API_TOKEN:
                return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    app = web.Application(middlewares=[auth_middleware])

    async def get_chats(request):
        """API: получить список чатов."""
        chats = await db.get_all_chats()
        return web.json_response({"chats": chats})

    async def api_update_chat(request):
        """API: обновить чат и сразу применить настройки (мониторинг + рассылки)."""
        data = await request.json()
        chat_id = data.get("chat_id")
        if not chat_id:
            return web.json_response({"error": "chat_id required"}, status=400)

        await db.update_chat(
            chat_id=chat_id,
            chat_name=data.get("chat_name"),
            is_monitored=data.get("is_monitored", False),
            is_broadcast=data.get("is_broadcast", False),
            chat_rules=data.get("chat_rules", ""),
            broadcast_times=data.get("broadcast_times", ""),
            message_text=data.get("message_text", ""),
            message_variants=data.get("message_variants"),
            schedule_cron=data.get("schedule_cron"),
            is_direct_promo=data.get("is_direct_promo", False),
            chat_niche=data.get("chat_niche", ""),
            broadcast_language=data.get("broadcast_language", "ru")
        )
        # Применяем изменения сразу, без перезапуска сервиса
        await user_client.reload_chats()
        await scheduler.reload_jobs()
        return web.json_response({"success": True})

    async def api_sync_dialogs(request):
        """API: обновить список чатов из Telegram-аккаунта."""
        dialogs = await user_client.fetch_dialogs()
        for d in dialogs:
            await db.upsert_dialog_chat(d["chat_id"], d["chat_name"])
        return web.json_response({"success": True, "count": len(dialogs)})

    async def api_preview_broadcast(request):
        """API: сгенерировать тестовое сообщение рассылки (без отправки)."""
        data = await request.json()
        chat_id = data.get("chat_id")
        if not chat_id:
            return web.json_response({"error": "chat_id required"}, status=400)
        result = await scheduler.preview_broadcast(chat_id)
        return web.json_response({"result": result})

    async def api_get_settings(request):
        """API: настройки (пол разработчика и др.)."""
        gender = await db.get_setting("developer_gender") or config.DEVELOPER_GENDER
        return web.json_response({"developer_gender": gender})

    async def api_set_settings(request):
        """API: сохранить настройки."""
        data = await request.json()
        gender = data.get("developer_gender")
        if gender in ("male", "female"):
            await db.set_setting("developer_gender", gender)
        return web.json_response({"success": True})

    async def api_status(request):
        """API: статус системы для GUI."""
        hourly = await db.get_hourly_actions_count()
        daily = await db.get_daily_actions_count()
        chats = await db.get_monitored_chats()
        return web.json_response({
            "running": user_client.is_running,
            "paused": user_client.is_paused,
            "monitored_chats": len(chats),
            "hourly_actions": hourly,
            "daily_actions": daily,
        })

    async def api_pause(request):
        """API: поставить парсинг на паузу (остановить обработку сообщений и рассылки)."""
        await user_client.pause()
        logger.info("Парсинг остановлен через API (GUI)")
        return web.json_response({"success": True, "paused": True})

    async def api_resume(request):
        """API: возобновить парсинг (запустить обработку сообщений и рассылки)."""
        await user_client.resume()
        logger.info("Парсинг запущен через API (GUI)")
        return web.json_response({"success": True, "paused": False})

    async def api_restart(request):
        """API: перезапустить сервис (ручной рестарт из GUI).

        systemd-run планирует `systemctl restart` через 3 секунды в отдельном
        transient-юните — это переживает смерть текущего процесса и даёт
        aiohttp время вернуть ответ. Если systemd-run недоступен — убиваем
        себя по SIGTERM, systemd с Restart=always поднимет сервис заново.
        Актуально при отвале Telethon-клиента (прокси/сеть) без авто-реконнекта.
        """
        import subprocess
        import signal
        logger.info("RESTART запрошен через API (GUI) — перезапуск через 3с")
        try:
            subprocess.Popen(
                ['systemd-run', '--on-active=3s', 'systemctl', 'restart', 'lead-hunter'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return web.json_response({"success": True, "message": "restart in 3s"})
        except FileNotFoundError:
            # systemd-run недоступен — убиваем процесс, systemd Restart=always поднимет
            loop = asyncio.get_event_loop()
            loop.call_later(1.0, lambda: os.kill(os.getpid(), signal.SIGTERM))
            return web.json_response({"success": True, "message": "restart via SIGTERM in 1s"})

    app.router.add_get('/api/chats', get_chats)
    app.router.add_post('/api/chats', api_update_chat)
    app.router.add_post('/api/sync_dialogs', api_sync_dialogs)
    app.router.add_post('/api/preview_broadcast', api_preview_broadcast)
    app.router.add_get('/api/settings', api_get_settings)
    app.router.add_post('/api/settings', api_set_settings)
    app.router.add_get('/api/status', api_status)
    app.router.add_post('/api/pause', api_pause)
    app.router.add_post('/api/resume', api_resume)
    app.router.add_post('/api/restart', api_restart)
    
    runner = web.AppRunner(app)
    await runner.setup()
    api_port = int(os.getenv("API_PORT", "8080"))
    site = web.TCPSite(runner, '0.0.0.0', api_port)
    await site.start()
    logger.info(f"HTTP API запущен на порту {api_port}")

    try:
        # Держим основной цикл
        await bot_task
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("Остановка системы...")
        await runner.cleanup()
        await scheduler.stop()
        await user_client.stop()
        await notif_bot.stop()
        logger.info("Система остановлена")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Прервано пользователем")
    except Exception as e:
        logger.error(f"Фатальная ошибка: {e}", exc_info=True)
        sys.exit(1)
