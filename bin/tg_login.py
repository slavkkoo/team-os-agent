#!/usr/bin/env python3
"""tg_login — создать Telegram-сессию для чтения папок (read_chat.py). Запускать ИНТЕРАКТИВНО
от пользователя агента:
  sudo -u teamagent -i
  ~/.local/share/team-agent/.venv/bin/python3 ~/.local/share/team-agent/bin/tg_login.py

Нужны TG_API_ID / TG_API_HASH (my.telegram.org → API development tools) и TG_SESSION
в .env. Показывает, КУДА Telegram отправил код (приложение / почта / SMS), даёт
запросить повторно. Код и облачный пароль вводятся здесь же и никуда не пересылаются.
Новая сессия проверяется чтением диалогов и только потом заменяет старую
(старая → <TG_SESSION>.session.old-<дата>).
"""
import asyncio
import getpass
import os
import shutil
import time

from telethon import TelegramClient, errors, functions
from telethon.tl import types

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_HOME = os.path.abspath(os.environ.get("AGENT_HOME") or os.path.join(HERE, ".."))
for line in open(os.path.join(AGENT_HOME, ".env"), encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("\"'"))
old = os.environ["TG_SESSION"]
new = old + "_new"
os.makedirs(os.path.dirname(old) or ".", mode=0o700, exist_ok=True)

WHERE = {
    types.auth.SentCodeTypeApp: "в приложение Telegram — чат «Telegram» на любом вашем устройстве, где вы залогинены",
    types.auth.SentCodeTypeSms: "SMS на номер",
    types.auth.SentCodeTypeCall: "звонком",
    types.auth.SentCodeTypeFlashCall: "flash-звонком (код — последние цифры номера)",
    types.auth.SentCodeTypeEmailCode: "на почту, привязанную ко входу в Telegram",
    types.auth.SentCodeTypeFragmentSms: "SMS через Fragment",
}


def describe(sent):
    t = sent.type
    name = WHERE.get(type(t), type(t).__name__)
    extra = ""
    if isinstance(t, types.auth.SentCodeTypeEmailCode):
        extra = f" ({getattr(t, 'email_pattern', '')})"
    if isinstance(t, getattr(types.auth, "SentCodeTypeSetUpEmailRequired", ())):
        name = "Telegram требует сначала привязать почту для входа (Настройки → Конфиденциальность → Почта для входа)"
    nxt = type(sent.next_type).__name__ if sent.next_type else "нет"
    return f"Код отправлен: {name}{extra}. Длина кода: {getattr(t, 'length', '?')}. Повторно можно: {nxt}"


async def main():
    phone = os.environ.get("TG_PHONE") or input("Телефон (в международном формате, с +): ")
    agent = os.environ.get("AGENT_NAME") or "team-agent"
    c = TelegramClient(new, int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"],
                       device_model=f"{agent} (VPS, read-only reader)", system_version="Debian",
                       app_version="team-agent-1")
    await c.connect()
    if not await c.is_user_authorized():
        sent = await c.send_code_request(phone)
        print(describe(sent))
        while True:
            code = input("Код (или r — запросить повторно другим способом): ").strip()
            if code.lower() == "r":
                sent = await c(functions.auth.ResendCodeRequest(phone, sent.phone_code_hash))
                print(describe(sent))
                continue
            try:
                await c.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
                break
            except errors.SessionPasswordNeededError:
                await c.sign_in(password=getpass.getpass("Облачный пароль Telegram: "))
                break
            except errors.PhoneCodeInvalidError:
                print("Неверный код, попробуйте ещё раз")
            except errors.PhoneCodeExpiredError:
                print("Код истёк — введите r, чтобы запросить новый")
    me = await c.get_me()
    n = 0
    async for _ in c.iter_dialogs(limit=5):
        n += 1
    await c.disconnect()
    print(f"OK: вошли как {me.first_name}, диалогов прочитано: {n}")
    stamp = time.strftime("%Y%m%d-%H%M")
    if os.path.exists(old + ".session"):
        shutil.move(old + ".session", f"{old}.session.old-{stamp}")
    shutil.move(new + ".session", old + ".session")
    os.chmod(old + ".session", 0o600)
    print(f"Новая сессия установлена: {old}.session. В Telegram → Настройки → Устройства появится «{agent} (VPS…)».")
    print("Проверка: .venv/bin/python3 bin/read_chat.py list")


asyncio.run(main())
