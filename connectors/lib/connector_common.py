#!/usr/bin/env python3
"""Общие функции коннекторов team-os-agent. Только stdlib.

Каждый коннектор — отдельная обёртка в connectors/<имя>/, которая:
  - выключена по умолчанию (CONNECTOR_<ИМЯ>_ENABLED=1 в .env включает);
  - сама читает свои токены из .env (бот вычищает их из окружения модели);
  - маскирует персональные данные и секреты в выводе ДО того, как его увидит модель;
  - обрезает вывод (модели нужен сжатый ответ, а не тысяча строк);
  - имеет режим --selftest: без сети, на встроенных фикстурах.

Подключение из коннектора:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
    import connector_common as cc
"""
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_HOME = os.path.abspath(os.environ.get("AGENT_HOME") or os.path.join(HERE, "..", ".."))


# --- конфигурация ---

def load_env(path=None):
    """AGENT_HOME/.env построчно (без source). Уже заданные переменные не перекрываются."""
    path = path or os.path.join(AGENT_HOME, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                os.environ.setdefault(k.strip(), v)


def env(key, default=""):
    v = os.environ.get(key, "").strip()
    return v if v else default


def env_int(key, default):
    try:
        return int(env(key, str(default)))
    except ValueError:
        return default


def env_list(key, default=""):
    return [x.strip() for x in env(key, default).split(",") if x.strip()]


def flag_name(connector):
    return "CONNECTOR_" + connector.upper().replace("-", "_") + "_ENABLED"


def enabled(connector):
    return env(flag_name(connector)) == "1"


def require_enabled(connector):
    if not enabled(connector):
        sys.exit(f"Коннектор {connector} выключен ({flag_name(connector)}=1 в .env включает). "
                 "Скажи спрашивающему, что эта система к агенту не подключена.")


def path(*parts):
    return os.path.join(AGENT_HOME, *parts)


# --- маскирование ---

# Телефон — только с «+» и кодом страны: без этого маска съедает даты, id и суммы.
_PHONE = re.compile(r"(?<![\w+])\+\d{1,3}(?:[\s\-()]*\d){7,12}(?!\w)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_BEARER = re.compile(r"(?i)\b(bearer|basic|apikey|token)\s+[A-Za-z0-9._~+/=-]{8,}")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}")
_KV_SECRET = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|secret|password|passwd|"
    r"token|authorization|x-api-key)(\"?\s*[:=]\s*\"?)(?!(?:bearer|basic|apikey|token)\s)([^\s\"'&,;}]{4,})")
_PREFIXED = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|glpat-[A-Za-z0-9_-]{16,}|xox[abprs]-[A-Za-z0-9-]{10,}|"
    r"AKIA[0-9A-Z]{16}|(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{10,})")

DEFAULT_PII_KEYS = ("email,phone,phone_number,mobile,first_name,last_name,middle_name,full_name,"
                    "birth_date,birthday,address,passport,card_number,iban")


def mask_text(text):
    """Страховочная маска поверх серверной: email, телефоны, токены и ключи."""
    if not isinstance(text, str) or not text:
        return text
    text = _PREFIXED.sub("<secret>", text)
    text = _JWT.sub("<jwt>", text)
    text = _BEARER.sub(lambda m: m.group(1) + " ***", text)
    text = _KV_SECRET.sub(lambda m: m.group(1) + m.group(2) + "***", text)
    text = _EMAIL.sub("***@***", text)
    return _PHONE.sub("+***", text)


def pii_keys():
    return {k.lower() for k in env_list("CONNECTOR_PII_KEYS", DEFAULT_PII_KEYS)}


def mask_obj(obj, keys=None):
    """JSON-структура: значения PII-ключей → «***», строки — через mask_text."""
    keys = pii_keys() if keys is None else keys
    if isinstance(obj, dict):
        return {k: ("***" if str(k).lower() in keys and obj[k] not in (None, "") else mask_obj(v, keys))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask_obj(x, keys) for x in obj]
    if isinstance(obj, str):
        return mask_text(obj)
    return obj


def truncate(text, limit, hint="сузь окно или запрос"):
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…(обрезано: ещё {len(text) - limit} симв.; {hint})"


def out(text, limit=None, hint="сузь окно или запрос"):
    """Единая точка вывода модели: маска + обрезка."""
    limit = limit or env_int("CONNECTOR_MAX_OUT", 8000)
    print(truncate(mask_text(text), limit, hint))


# --- JSON-утилиты ---

def dig(obj, dotted, default=None):
    """dig({"a": {"b": [1, 2]}}, "a.b.1") → 2. Пустой путь — сам объект."""
    if not dotted:
        return obj
    cur = obj
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return default
    return cur


def dig_set(obj, dotted, value):
    cur = obj
    parts = dotted.split(".")
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


# --- HTTP ---

class NetError(Exception):
    """Сеть/VPN/DNS/таймаут — в отличие от HTTP-ошибки, система до нас не ответила."""


def http(method, url, headers=None, body=None, timeout=30):
    """→ (status, text). HTTP-ошибка — это ответ (status, тело); сеть — NetError."""
    data = None
    hdrs = {"Accept": "application/json"}
    hdrs.update(headers or {})
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            txt = e.read().decode("utf-8", "replace")
        except Exception:
            txt = ""
        return e.code, txt[:2000]
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        host = urllib.parse.urlparse(url).netloc
        raise NetError(f"{host} недоступен ({type(e).__name__}: {e}) — похоже на сеть/VPN/DNS") from None


def parse_json(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


# --- рантайм-каталоги ---

def require_run_dir(p):
    """Сырые выгрузки (тикеты, ответы) — только в каталог *_run/ (он в .gitignore и
    закрыт от чтения моделью в чате). Возвращает абсолютный путь."""
    ap = os.path.abspath(p)
    parts = os.path.dirname(ap).split(os.sep)
    if not any(x.endswith("_run") for x in parts if x):
        sys.exit(f"Отказ: {p} — сырые данные пишем только в каталог *_run/ (например {path('helpdesk_run')}/)")
    os.makedirs(os.path.dirname(ap), exist_ok=True)
    return ap


# --- selftest ---

class Selftest:
    """Мини-раннер проверок без зависимостей: check(cond, what) → в конце report()."""

    def __init__(self, name):
        self.name, self.n, self.fail = name, 0, []

    def check(self, cond, what):
        self.n += 1
        if not cond:
            self.fail.append(what)

    def eq(self, got, want, what):
        self.check(got == want, f"{what}: ждали {want!r}, получили {got!r}")

    def report(self):
        if self.fail:
            print(f"selftest {self.name}: FAIL {len(self.fail)} из {self.n}")
            for f in self.fail:
                print("  ✗ " + f)
            sys.exit(1)
        print(f"selftest {self.name}: OK ({self.n} проверок)")


if __name__ == "__main__":
    t = Selftest("connector_common")
    fake_gh = "gh" + "p_" + "A1b2C3d4" * 3
    t.eq(mask_text("mail me: a.b+c@acme.example.com"), "mail me: ***@***", "email")
    t.eq(mask_text("call +1 (555) 010-0199 now"), "call +*** now", "телефон с +")
    t.eq(mask_text("2026-10-28 12:00:05 id=1234567890"), "2026-10-28 12:00:05 id=1234567890", "даты и id не трогаем")
    t.eq(mask_text("Authorization: Bearer abcdefghijkl123"), "Authorization: Bearer ***", "bearer")
    t.eq(mask_text("token=" + fake_gh), "token=***", "kv-секрет")
    t.eq(mask_text("key " + fake_gh), "key <secret>", "префиксный токен")
    t.eq(mask_obj({"email": "x@y.io", "service": "Стрижка", "items": [{"phone": "+15550100199"}]}, {"email", "phone"}),
         {"email": "***", "service": "Стрижка", "items": [{"phone": "***"}]}, "mask_obj")
    t.eq(dig({"a": {"b": [1, {"c": 3}]}}, "a.b.1.c"), 3, "dig")
    t.check("обрезано" in truncate("x" * 50, 10), "truncate")
    t.report()
