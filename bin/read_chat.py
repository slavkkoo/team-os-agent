#!/usr/bin/env python3
"""read_chat — READ-ONLY дамп Telegram-папок (dialog filters) за последние N часов.

Запускать из venv с Telethon: $AGENT_HOME/.venv/bin/python3 bin/read_chat.py ...
Сессия — личный аккаунт (TG_SESSION), её создаёт bin/tg_login.py. Бот так не
умеет: Bot API не видит папки и историю чатов, поэтому нужен user-аккаунт.

Usage:
  read_chat.py folders "Папка A,Папка B" 24h   # несколько папок (через запятую)
  read_chat.py folder "Папка A" 168h           # одна папка
  read_chat.py list                            # какие папки есть в аккаунте

Вывод (его режут duty):
  ## Folder: <имя>
  === <название чата> ===
  [дд.мм ЧЧ:ММ] Имя: текст
Ничего не пишет и не отмечает прочитанным.
"""
import asyncio
import datetime as dt
import os
import sys

from telethon import TelegramClient, functions, types

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_HOME = os.path.abspath(os.environ.get("AGENT_HOME") or os.path.join(HERE, ".."))
MAX_PER_CHAT = int(os.environ.get("READ_CHAT_MAX_PER_CHAT", "400"))


def load_env():
    path = os.path.join(AGENT_HOME, ".env")
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            os.environ.setdefault(k.strip(), v)


def parse_hours(s):
    s = s.strip().lower()
    if s.endswith("h"):
        return int(s[:-1])
    if s.endswith("d"):
        return int(s[:-1]) * 24
    return int(s)


def title_of(f):
    t = getattr(f, "title", "")
    return getattr(t, "text", t) or ""   # в новых слоях API title — TextWithEntities


def chat_name(e):
    if getattr(e, "title", None):
        return e.title
    name = " ".join(x for x in [getattr(e, "first_name", ""), getattr(e, "last_name", "")] if x)
    return name or str(getattr(e, "id", "?"))


async def folder_filters(client):
    res = await client(functions.messages.GetDialogFiltersRequest())
    filters = getattr(res, "filters", res)   # старые версии Telethon отдают список
    return [f for f in filters if isinstance(f, (types.DialogFilter, getattr(types, "DialogFilterChatlist", types.DialogFilter)))]


async def dump_folder(client, f, since):
    print(f"## Folder: {title_of(f)}")
    peers = list(getattr(f, "pinned_peers", []) or []) + list(getattr(f, "include_peers", []) or [])
    seen = set()
    for p in peers:
        try:
            e = await client.get_entity(p)
        except Exception as ex:
            print(f"(не открыл чат: {type(ex).__name__})", file=sys.stderr)
            continue
        if e.id in seen:
            continue
        seen.add(e.id)
        lines = []
        async for m in client.iter_messages(e, limit=MAX_PER_CHAT):
            mdate = m.date if m.date.tzinfo else m.date.replace(tzinfo=dt.timezone.utc)
            if mdate < since:
                break
            if m.action is not None:
                continue
            who = "?"
            try:
                s = await m.get_sender()
                who = chat_name(s) if s else chat_name(e)
            except Exception:
                pass
            text = (m.message or "(вложение/медиа)").replace("\n", " ")[:1500]
            lines.append(f"[{mdate.astimezone().strftime('%d.%m %H:%M')}] {who}: {text}")
        if lines:
            print(f"=== {chat_name(e)} ===")
            for line in reversed(lines):
                print(line)
            print()
    if getattr(f, "groups", False) or getattr(f, "broadcasts", False) or getattr(f, "contacts", False):
        print("(в папке есть правила-категории «все группы/каналы/контакты» — читаются только чаты, "
              "добавленные в папку явно)", file=sys.stderr)


async def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    load_env()
    client = TelegramClient(os.environ["TG_SESSION"], int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"])
    await client.connect()
    if not await client.is_user_authorized():
        sys.exit("Telegram-сессия не авторизована — запустите bin/tg_login.py (см. INSTALL.md)")
    try:
        filters = await folder_filters(client)
        if sys.argv[1] == "list":
            for f in filters:
                print(title_of(f))
            return
        if sys.argv[1] not in ("folder", "folders") or len(sys.argv) < 4:
            sys.exit(__doc__)
        names = [sys.argv[2]] if sys.argv[1] == "folder" else [n.strip() for n in sys.argv[2].split(",") if n.strip()]
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=parse_hours(sys.argv[3]))
        by_name = {title_of(f): f for f in filters}
        missing = [n for n in names if n not in by_name]
        if missing:
            sys.exit(f"Нет папок: {', '.join(missing)}. Есть: {', '.join(by_name) or '—'}")
        for n in names:
            await dump_folder(client, by_name[n], since)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
