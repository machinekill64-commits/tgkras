"""Онбординг нового Telegram-аккаунта: env + сессия + деплой на сервер.

Использование (интерактивно — спросит недостающее):
    python add_account.py --num 3

Или всё сразу:
    python add_account.py --num 3 --phone +79990001122 --name Марина ^
        --proxy "http://user:pass@host:port" --deploy

Сессия: если рядом лежит готовая session_accN.session — используется она,
иначе запускается авторизация по номеру (код из Telegram + 2FA при нужде).
--deploy заливает accN.env + session_accN.session на сервер и запускает
systemd-юнит lead-hunter@N.
"""
import argparse
import asyncio
import os
import sys
from urllib.parse import urlparse

import config
from telethon import TelegramClient
from telethon import connection
from telethon.errors import SessionPasswordNeededError

DCS = [
    (1, "149.154.175.53", 443),
    (2, "149.154.167.51", 443),
    (3, "149.154.175.100", 443),
    (4, "149.154.167.91", 443),
    (5, "91.108.56.130", 443),
]

REMOTE_DIR = "/opt/lead-hunter"
UNIT_TEMPLATE_LOCAL = os.path.join("deploy", "lead-hunter@.service")
UNIT_TEMPLATE_REMOTE = "/etc/systemd/system/lead-hunter@.service"


def parse_proxy(raw: str) -> dict:
    """'http://user:pass@host:port' | 'socks5://host:port' | 'host:port' | ''."""
    raw = (raw or "").strip()
    if not raw:
        return {}
    if "://" not in raw:
        raw = "http://" + raw
    u = urlparse(raw)
    proxy = {
        "proxy_type": "socks5" if u.scheme.startswith("socks") else "http",
        "addr": u.hostname,
        "port": u.port,
    }
    if u.username:
        proxy["username"] = u.username
        proxy["password"] = u.password or ""
    return proxy


def write_env_file(num: int, proxy: dict, persona: str) -> str:
    lines = [
        f"TG_SESSION_NAME=session_acc{num}",
        f"TG_PROXY_TYPE={proxy.get('proxy_type', 'http') if proxy else ''}",
        f"TG_PROXY_HOST={proxy.get('addr', '')}",
        f"TG_PROXY_PORT={proxy.get('port', '') or ''}",
        f"TG_PROXY_USERNAME={proxy.get('username', '')}",
        f"TG_PROXY_PASSWORD={proxy.get('password', '')}",
        "TG_MTPROTO_ENABLED=false",
        f"BOT_PERSONA_NAME={persona}",
    ]
    path = f"acc{num}.env"
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def build_client(session_name: str, proxy: dict) -> TelegramClient:
    kwargs = {"connection_retries": 5, "retry_delay": 2}
    if proxy:
        kwargs["proxy"] = proxy
    return TelegramClient(session_name, config.TG_API_ID, config.TG_API_HASH, **kwargs)


def _hash_file(session_name: str) -> str:
    return f".phone_code_hash_{session_name}"


async def authorize(phone: str, session_name: str, proxy: dict,
                    code: str = "", password: str = ""):
    """Вход по SMS/2FA. code='' → запрос кода, сохранение hash, выход.
    code — вход по коду из SMS (hash берётся из сохранённого файла).
    password — пароль 2FA. Возвращает user, 'WAIT_CODE', 'NEED_2FA' или None."""
    client = build_client(session_name, proxy)
    await client.connect()
    try:
        if await client.is_user_authorized():
            return await client.get_me()

        if code:
            phash = ""
            if os.path.exists(_hash_file(session_name)):
                phash = open(_hash_file(session_name)).read().strip()
            try:
                await client.sign_in(phone, code, phone_code_hash=phash or None)
            except SessionPasswordNeededError:
                if password:
                    await client.sign_in(password=password)
                else:
                    return "NEED_2FA"
            if os.path.exists(_hash_file(session_name)):
                os.remove(_hash_file(session_name))
            return await client.get_me()

        sent = await client.send_code_request(phone)
        with open(_hash_file(session_name), "w") as f:
            f.write(sent.phone_code_hash)
        print(f"Код отправлен на {phone} ({sent.type}). "
              "Пришли его — дальше --code КОД [--password ПАРОЛЬ].")
        return "WAIT_CODE"
    finally:
        await client.disconnect()


async def authorize_authkey(hex_key: str, session_name: str, proxy: dict):
    """Вход по готовому auth_key (512 hex) — перебор DC."""
    from telethon.crypto import AuthKey
    key = AuthKey(bytes.fromhex(hex_key))
    session_file = session_name + ".session"
    for dc_id, addr, port in DCS:
        if os.path.exists(session_file):
            os.remove(session_file)
        client = build_client(session_name, proxy)
        client.session.set_dc(dc_id, addr, port)
        client.session.auth_key = key
        client.session.save()
        try:
            await client.connect()
            if await client.is_user_authorized():
                me = await client.get_me()
                await client.disconnect()
                return me
            print(f"  dc{dc_id}: ключ не зарегистрирован")
        except Exception as e:
            print(f"  dc{dc_id}: {type(e).__name__}: {e}")
        try:
            await client.disconnect()
        except Exception:
            pass
    return None


def ssh_connect():
    """SSH-подключение: креды из env, иначе из ssh_exec.py."""
    import paramiko
    host = os.getenv("SSH_HOST")
    user = os.getenv("SSH_USER", "root")
    pw = os.getenv("SSH_PASS")
    if not host or not pw:
        try:
            import ssh_exec
            host = host or ssh_exec.HOST
            user = os.getenv("SSH_USER") or ssh_exec.USER
            pw = pw or ssh_exec.PASS
        except Exception:
            pass
    if not host or not pw:
        raise RuntimeError("Нет SSH-кредов: задай SSH_HOST/SSH_PASS "
                           "или обнови ssh_exec.py")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, username=user, password=pw, timeout=15)
    return client


def deploy(num: int, session_name: str) -> bool:
    """Заливка accN.env + session + юнит на сервер, запуск воркера."""
    env_path = f"acc{num}.env"
    session_file = session_name + ".session"
    client = ssh_connect()
    try:
        sftp = client.open_sftp()
        sftp.put(env_path, f"{REMOTE_DIR}/{env_path}")
        sftp.put(session_file, f"{REMOTE_DIR}/{session_file}")
        sftp.put(UNIT_TEMPLATE_LOCAL, UNIT_TEMPLATE_REMOTE)
        sftp.close()
        for cmd in [
            f"chmod 600 {REMOTE_DIR}/{env_path}",
            "systemctl daemon-reload",
            f"systemctl enable --now lead-hunter@{num}",
            f"systemctl status lead-hunter@{num} --no-pager -l | head -15",
        ]:
            _, stdout, stderr = client.exec_command(cmd)
            out = stdout.read().decode(errors="replace").strip()
            err = stderr.read().decode(errors="replace").strip()
            if out:
                print(out)
            if err:
                print(f"[stderr] {err}")
        return True
    finally:
        client.close()


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val or default


async def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--num", type=int, required=True, help="Номер аккаунта (3, 4, ...)")
    p.add_argument("--phone", help="Номер телефона аккаунта")
    p.add_argument("--name", help="Имя персоны в диалогах (как в профиле акка)")
    p.add_argument("--proxy", help="Прокси: http://user:pass@host:port, "
                                 "socks5://... или host:port. Пусто = без прокси")
    p.add_argument("--code", help="Код из SMS (после первого запуска)")
    p.add_argument("--password", help="Пароль 2FA облака Telegram")
    p.add_argument("--authkey", help="Готовый auth_key (512 hex) вместо SMS")
    p.add_argument("--skip-auth", action="store_true",
                   help="Не авторизовывать — только env (+деплой готовой сессии)")
    p.add_argument("--deploy", action="store_true",
                   help="Залить на сервер и запустить systemd-юнит")
    p.add_argument("--no-deploy", action="store_true",
                   help="Только локальные файлы, на сервер не ходить")
    args = p.parse_args()

    num = args.num
    session_name = f"session_acc{num}"
    print(f"=== Онбординг аккаунта #{num} ({session_name}) ===\n")

    # 1. Данные
    need_phone = not (args.authkey or args.skip_auth
                      or os.path.exists(f"{session_name}.session"))
    phone = args.phone or (ask("Телефон аккаунта (+7...)") if need_phone else "")
    proxy_raw = args.proxy if args.proxy is not None else ask(
        "Прокси (http://user:pass@host:port или пусто)")
    proxy = parse_proxy(proxy_raw)
    persona = args.name or ask("Имя персоны (как зовут профиль)", "Елена")

    # 2. env-файл
    env_path = write_env_file(num, proxy, persona)
    print(f"\n[1/3] Создан {env_path} (session={session_name}, "
          f"proxy={proxy.get('addr') or 'нет'}, персона={persona})")

    # 3. Сессия
    session_file = session_name + ".session"
    me = None
    if args.skip_auth:
        print(f"[2/3] Авторизация пропущена (--skip-auth)")
        if not os.path.exists(session_file):
            print(f"      ВНИМАНИЕ: {session_file} отсутствует — без него "
                  "воркер не запустится. Положи файл или убери --skip-auth.")
    elif args.authkey:
        print("[2/3] Вход по auth_key...")
        me = await authorize_authkey(args.authkey, session_name, proxy)
    elif args.code or args.password:
        print("[2/3] Вход по коду/паролю...")
        me = await authorize(phone, session_name, proxy,
                             code=args.code or "",
                             password=args.password or "")
    elif os.path.exists(session_file):
        print(f"[2/3] Используется существующая сессия {session_file}")
        client = build_client(session_name, proxy)
        await client.connect()
        me = await client.get_me() if await client.is_user_authorized() else None
        await client.disconnect()
    else:
        print("[2/3] Авторизация в Telegram...")
        me = await authorize(phone, session_name, proxy,
                             code=args.code or "",
                             password=args.password or "")

    if me == "WAIT_CODE":
        sys.exit(0)
    if me == "NEED_2FA":
        print("      На аккаунте 2FA — перезапусти с --password ПАРОЛЬ")
        sys.exit(1)
    if not args.skip_auth:
        if not me:
            print("\nСЕССИЯ НЕ ПОЛУЧЕНА — проверь телефон/код/прокси и запусти снова.")
            sys.exit(1)
        full_name = f"{me.first_name} {me.last_name or ''}".strip()
        print(f"      Авторизован: {full_name} @{me.username} id={me.id}")
        if persona == "Елена" and me.first_name and me.first_name != "Елена":
            print(f"      ВНИМАНИЕ: профиль называется «{me.first_name}», а персона "
                  f"«{persona}». Перезапусти с --name {me.first_name} или поправь {env_path}")
        # Аватарка: если рядом лежит avatar_accN.{jpg,jpeg,png,webp} — ставим
        for ext in ("jpg", "jpeg", "png", "webp"):
            avatar = f"avatar_acc{num}.{ext}"
            if os.path.exists(avatar):
                try:
                    from set_avatar import set_avatar as _set_avatar
                    await _set_avatar(session_name, avatar, proxy)
                except Exception as e:
                    print(f"      Аватарка не установлена: {e}")
                break

    # 4. Деплой
    do_deploy = args.deploy
    if not args.deploy and not args.no_deploy:
        do_deploy = ask("Задеплоить на сервер? (y/n)", "n").lower() in ("y", "yes", "д", "да")
    if do_deploy:
        print(f"[3/3] Деплой lead-hunter@{num} на сервер...")
        try:
            deploy(num, session_name)
            print(f"\nГОТОВО: воркер lead-hunter@{num} запущен на сервере.")
            print(f"Логи: ssh ... 'journalctl -u lead-hunter@{num} -f'")
        except Exception as e:
            print(f"\nДеплой не удался: {type(e).__name__}: {e}")
            print("Вручную: скопируй accN.env и session_accN.session в "
                  f"{REMOTE_DIR}, юнит {UNIT_TEMPLATE_LOCAL} → "
                  f"{UNIT_TEMPLATE_REMOTE}, затем:")
            print("  systemctl daemon-reload && systemctl enable --now "
                  f"lead-hunter@{num}")
    else:
        print(f"\n[3/3] Без деплоя. Файлы готовы: {env_path}, {session_file}")
        print(f"Для сервера: закинь оба в {REMOTE_DIR}/, юнит — "
              f"{UNIT_TEMPLATE_REMOTE},")
        print(f"затем systemctl daemon-reload && systemctl enable --now "
              f"lead-hunter@{num}")


if __name__ == "__main__":
    asyncio.run(main())
