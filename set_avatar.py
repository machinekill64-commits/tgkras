"""Установка аватарки аккаунту. Используется из add_account.py и отдельно.

Отдельно: python set_avatar.py <session_name> <image_path>
Прокси берётся из accN.env (по TG_SESSION_NAME внутри файла).
"""
import asyncio
import os
import sys

from telethon import TelegramClient, functions

import config


def proxy_from_env(env_path: str) -> dict:
    """Читает TG_PROXY_* из accN.env."""
    vals = {}
    if not os.path.exists(env_path):
        return {}
    for line in open(env_path, encoding="utf-8"):
        if "=" in line:
            k, v = line.strip().split("=", 1)
            vals[k] = v.strip().strip('"')
    host = vals.get("TG_PROXY_HOST")
    port = vals.get("TG_PROXY_PORT")
    if not host or not port:
        return {}
    proxy = {"proxy_type": "socks5" if vals.get("TG_PROXY_TYPE") == "socks5" else "http",
             "addr": host, "port": int(port)}
    if vals.get("TG_PROXY_USERNAME"):
        proxy["username"] = vals["TG_PROXY_USERNAME"]
        proxy["password"] = vals.get("TG_PROXY_PASSWORD", "")
    return proxy


async def set_avatar(session_name: str, image_path: str, proxy: dict | None = None):
    num = session_name.replace("session_acc", "")
    if proxy is None:
        proxy = proxy_from_env(f"acc{num}.env")
    kwargs = {"connection_retries": 5, "retry_delay": 2}
    if proxy:
        kwargs["proxy"] = proxy
    client = TelegramClient(session_name, config.TG_API_ID,
                            config.TG_API_HASH, **kwargs)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            print(f"{session_name}: НЕ АВТОРИЗОВАН")
            return False
        file = await client.upload_file(image_path)
        await client(functions.photos.UploadProfilePhotoRequest(file=file))
        print(f"{session_name}: аватарка {image_path} установлена")
        return True
    except Exception as e:
        print(f"{session_name}: {type(e).__name__}: {e}")
        return False
    finally:
        await client.disconnect()


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    ok = asyncio.run(set_avatar(sys.argv[1], sys.argv[2]))
    sys.exit(0 if ok else 1)
