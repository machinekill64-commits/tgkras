"""Авторизация второго аккаунта (session_acc2) в два шага.

Шаг 1: python auth_acc2.py request
    -> запрашивает код у Telegram, печатает phone_code_hash
Шаг 2: python auth_acc2.py signin <код> <phone_code_hash>
    -> входит по коду; при 2FA: python auth_acc2.py password <пароль>

После успеха создаётся session_acc2.session.
"""
import asyncio
import sys

import config
from telethon import TelegramClient
from telethon import connection

PHONE = "+12344044445"
SESSION = "session_acc2"


def build_client() -> TelegramClient:
    kwargs = {"connection_retries": 5, "retry_delay": 2}
    if config.TG_PROXY_TYPE and config.TG_PROXY_HOST and config.TG_PROXY_PORT:
        proxy = {
            "proxy_type": config.TG_PROXY_TYPE,
            "addr": config.TG_PROXY_HOST,
            "port": config.TG_PROXY_PORT,
        }
        if config.TG_PROXY_USERNAME:
            proxy["username"] = config.TG_PROXY_USERNAME
            proxy["password"] = config.TG_PROXY_PASSWORD
        kwargs["proxy"] = proxy
    elif config.TG_MTPROTO_HOST and config.TG_MTPROTO_PORT and config.TG_MTPROTO_SECRET:
        kwargs["connection"] = connection.ConnectionTcpMTProxyRandomizedIntermediate
        kwargs["proxy"] = (config.TG_MTPROTO_HOST, config.TG_MTPROTO_PORT,
                           config.TG_MTPROTO_SECRET)
    return TelegramClient(SESSION, config.TG_API_ID, config.TG_API_HASH, **kwargs)


async def request_code():
    client = build_client()
    await client.connect()
    try:
        sent = await client.send_code_request(PHONE)
        print(f"Код отправлен на {PHONE} (тип: {sent.type})")
        print(f"PHONE_CODE_HASH={sent.phone_code_hash}")
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
    await client.disconnect()


async def sign_in(code: str, phone_code_hash: str):
    client = build_client()
    await client.connect()
    try:
        await client.sign_in(PHONE, code, phone_code_hash=phone_code_hash)
    except Exception as e:
        name = type(e).__name__
        if "Password" in name or "password" in str(e).lower():
            print("NEED_2FA: включена двухфакторка — запустите "
                  "`python auth_acc2.py password <пароль>`")
        else:
            print(f"ERROR: {name}: {e}")
        await client.disconnect()
        return
    me = await client.get_me()
    print(f"OK: авторизован {me.first_name} {me.last_name or ''} "
          f"@{me.username} id={me.id}")
    await client.disconnect()


async def sign_in_password(password: str):
    client = build_client()
    await client.connect()
    try:
        await client.sign_in(password=password)
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
        await client.disconnect()
        return
    me = await client.get_me()
    print(f"OK: авторизован {me.first_name} {me.last_name or ''} "
          f"@{me.username} id={me.id}")
    await client.disconnect()


DCS = [
    (1, "149.154.175.53", 443),
    (2, "149.154.167.51", 443),
    (3, "149.154.175.100", 443),
    (4, "149.154.167.91", 443),
    (5, "91.108.56.130", 443),
]


async def sign_in_authkey(hex_key: str):
    """Создаёт session_acc2.session из готового auth_key (512 hex),
    перебирая DC — ключ валиден только на своём дата-центре."""
    import os
    from telethon.crypto import AuthKey

    key = AuthKey(bytes.fromhex(hex_key))
    session_file = SESSION + ".session"
    for dc_id, addr, port in DCS:
        if os.path.exists(session_file):
            os.remove(session_file)
        client = build_client()
        client.session.set_dc(dc_id, addr, port)
        client.session.auth_key = key
        client.session.save()
        try:
            await client.connect()
            if await client.is_user_authorized():
                me = await client.get_me()
                print(f"OK dc{dc_id}: {me.first_name} {me.last_name or ''} "
                      f"@{me.username} id={me.id}")
                await client.disconnect()
                return
            print(f"dc{dc_id}: ключ не зарегистрирован")
        except Exception as e:
            print(f"dc{dc_id}: {type(e).__name__}: {e}")
        try:
            await client.disconnect()
        except Exception:
            pass
    print("ERROR: auth_key не подошёл ни к одному DC")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "request":
        asyncio.run(request_code())
    elif mode == "signin" and len(sys.argv) > 3:
        asyncio.run(sign_in(sys.argv[2], sys.argv[3]))
    elif mode == "password" and len(sys.argv) > 2:
        asyncio.run(sign_in_password(sys.argv[2]))
    elif mode == "authkey" and len(sys.argv) > 2:
        asyncio.run(sign_in_authkey(sys.argv[2]))
    else:
        print(__doc__)
