#!/usr/bin/env python3
"""product_api — REST API своего продукта на ТЕСТОВОМ аккаунте: проверка вызовом, а не гаданием.

Usage:
  product_api.py get <путь>                         GET по allowlist (config: get_allowlist)
  product_api.py --write post <путь> '<json>'       создать тестовый объект (только коллекции из config.write)
  product_api.py --write put|patch <путь> '<json>'  изменить СВОЙ объект (из ledger)
  product_api.py --write delete <путь>              удалить СВОЙ объект (из ledger)
  product_api.py ledger                             свои открытые объекты (созданы и не удалены)
  product_api.py --write cleanup [--older-than-hours N]   удалить свои объекты старше N часов
  product_api.py --selftest                         без сети, на фикстурах

Чтение — всегда (если коннектор включён). Запись — только если ВСЁ сразу:
  1. PRODUCT_API_WRITE_ENABLED=1 в .env (отдельно от включения коннектора);
  2. флаг --write в команде;
  3. PRODUCT_API_WRITE_CONTEXT=team-dm (бот ставит его ТОЛЬКО для лички команды) или
     =duty (таймер автоочистки). В группах и публичном режиме записи нет;
  4. путь — коллекция из config.write; PUT/PATCH/DELETE — только объекты из своего ledger;
  5. тело проходит правила коллекции (require_in / max / force) и получает маркер теста;
  6. не исчерпан дневной лимит PRODUCT_API_WRITE_DAILY_LIMIT.
Каждая запись — строка в memory/product-api-ledger.jsonl. Таймер
team-agent-productapi-cleanup удаляет свои объекты старше PRODUCT_API_CLEANUP_HOURS.
Конфиг: config/product-api.json (пример — config/product-api.example.json).
"""
import json
import os
import posixpath
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import connector_common as cc  # noqa: E402

NAME = "product-api"
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
_transport = cc.http          # selftest подменяет на фейк


class Refused(Exception):
    """Предохранитель сработал — объясняем модели, что и почему нельзя."""


# --- конфиг и пути ---

def ledger_path():
    return cc.env("PRODUCT_API_LEDGER", cc.path("memory", "product-api-ledger.jsonl"))


def load_config(p=None):
    p = p or cc.env("PRODUCT_API_CONFIG", cc.path("config", "product-api.json"))
    if not os.path.exists(p):
        sys.exit(f"Нет конфига {p}: cp config/product-api.example.json config/product-api.json и поправьте")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def norm_path(raw):
    """«/v1/bookings/../admin» → отказ; возвращает (путь, query)."""
    u = urllib.parse.urlsplit(raw if raw.startswith("/") else "/" + raw)
    if u.scheme or u.netloc:
        raise Refused("нужен путь API, а не полный URL")
    p = urllib.parse.unquote(u.path)
    if ".." in p.split("/") or "//" in p or "\\" in p:
        raise Refused(f"путь {raw!r} с обходом каталогов")
    p = posixpath.normpath(p)
    return p, u.query


def get_allowed(p, cfg):
    for pref in cfg.get("get_allowlist") or []:
        if pref.endswith("/") and p.startswith(pref):
            return True
        if p == pref.rstrip("/") or p.startswith(pref.rstrip("/") + "/"):
            return True
    return False


def split_target(p, cfg):
    """/v1/bookings → (/v1/bookings, None, None); /v1/bookings/42 → (…, 42, None);
    /v1/bookings/42/cancel → (…, 42, cancel). Коллекция — ключ из config.write."""
    for coll in sorted(cfg.get("write") or {}, key=len, reverse=True):
        if p == coll:
            return coll, None, None
        if p.startswith(coll + "/"):
            rest = p[len(coll) + 1:].split("/")
            if len(rest) == 1:
                return coll, rest[0], None
            if len(rest) == 2:
                return coll, rest[0], rest[1]
    return None, None, None


# --- ledger ---

def ledger_read():
    p = ledger_path()
    if not os.path.exists(p):
        return []
    out = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def ledger_append(rec):
    rec.setdefault("epoch", time.time())
    rec.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    os.makedirs(os.path.dirname(ledger_path()), exist_ok=True)
    with open(ledger_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def open_objects():
    created, gone = {}, set()
    for r in ledger_read():
        key = (r.get("collection"), str(r.get("id")))
        if r.get("action") == "created":
            created[key] = r
        elif r.get("action") == "deleted":
            gone.add(key)
    return {k: v for k, v in created.items() if k not in gone}


def writes_today():
    day = time.strftime("%Y-%m-%d")
    return sum(1 for r in ledger_read() if r.get("action") in ("created", "updated", "deleted", "action")
               and time.strftime("%Y-%m-%d", time.localtime(r.get("epoch", 0))) == day)


# --- предохранители записи ---

def require_write(has_flag):
    if cc.env("PRODUCT_API_WRITE_ENABLED") != "1":
        raise Refused("запись выключена (PRODUCT_API_WRITE_ENABLED≠1) — доступно только чтение")
    if not has_flag:
        raise Refused("пишущая команда без --write")
    ctx = os.environ.get("PRODUCT_API_WRITE_CONTEXT", "")
    if ctx not in ("team-dm", "duty"):
        raise Refused("запись разрешена только из лички команды (и таймеру автоочистки); "
                      "в группах и для людей вне команды — только чтение")
    limit = cc.env_int("PRODUCT_API_WRITE_DAILY_LIMIT", 10)
    if writes_today() >= limit:
        raise Refused(f"дневной лимит записей ({limit}) исчерпан")


def apply_rules(body, rule):
    if not isinstance(body, dict):
        raise Refused("тело записи должно быть JSON-объектом")
    for dotted, allowed in (rule.get("require_in") or {}).items():
        if cc.dig(body, dotted) not in allowed:
            raise Refused(f"поле {dotted} должно быть одним из тестовых значений {allowed} "
                          "(реальным людям продукт шлёт уведомления)")
    for dotted, lim in (rule.get("max") or {}).items():
        v = cc.dig(body, dotted)
        if v is not None:
            try:
                if float(v) > float(lim):
                    raise Refused(f"поле {dotted}={v} больше предела {lim}")
            except (TypeError, ValueError):
                raise Refused(f"поле {dotted}={v!r} — не число") from None
    for dotted, val in (rule.get("force") or {}).items():
        cc.dig_set(body, dotted, val)
    mf = rule.get("marker_field")
    if mf:
        marker = cc.env("PRODUCT_API_TEST_MARKER", "AGENT-TEST")
        cur = cc.dig(body, mf) or ""
        if marker not in str(cur):
            cc.dig_set(body, mf, f"{marker} (автоудаление) {cur}".strip()[:200])
    return body


# --- вызовы ---

def call(method, p, query="", body=None):
    base = cc.env("PRODUCT_API_URL").rstrip("/")
    if not base:
        sys.exit("PRODUCT_API_URL не задан в .env")
    hdr = cc.env("PRODUCT_API_AUTH_HEADER", "Authorization")
    scheme = cc.env("PRODUCT_API_AUTH_SCHEME", "Bearer")      # none = голый токен (для X-Api-Key)
    tok = cc.env("PRODUCT_API_TOKEN")
    headers = {hdr: tok if scheme.lower() == "none" else f"{scheme} {tok}"} if tok else {}
    url = base + urllib.parse.quote(p) + (("?" + query) if query else "")
    status, text = _transport(method, url, headers, body, 45)
    return status, cc.parse_json(text) if text else {}, text


def show(status, obj, text):
    payload = obj if obj is not None else text
    s = json.dumps(cc.mask_obj(payload), ensure_ascii=False, indent=1) if not isinstance(payload, str) else payload
    prefix = "" if status < 300 else (f"HTTP {status} — API ДОСТУПЕН, это ответ на запрос (тело ошибки и есть "
                                      "диагноз; 404 на тестовом аккаунте = объект не этого аккаунта, а не «не существует»):\n")
    cc.out(prefix + s, cc.env_int("CONNECTOR_MAX_OUT", 6000))


def do_get(raw, cfg):
    p, q = norm_path(raw)
    if not get_allowed(p, cfg):
        raise Refused(f"путь {p} вне get_allowlist read-only обёртки")
    return call("GET", p, q)


def do_write(method, raw, body_raw, cfg, has_flag):
    method = method.upper()
    if method not in WRITE_METHODS:
        raise Refused(f"метод {method} не поддерживается")
    require_write(has_flag)
    p, q = norm_path(raw)
    coll, oid, action = split_target(p, cfg)
    if not coll:
        raise Refused(f"путь {p} не в списке тестовых коллекций config.write — писать туда нельзя")
    rule = cfg["write"][coll]
    if method not in [m.upper() for m in rule.get("methods", ["POST", "DELETE"])]:
        raise Refused(f"{method} для {coll} не разрешён правилами")
    mine = open_objects()
    if oid is not None and (coll, oid) not in mine:
        raise Refused(f"объект {coll}/{oid} не создан этой обёрткой (нет в ledger) — чужое не трогаем")
    if oid is None and method != "POST":
        raise Refused(f"{method} на всю коллекцию {coll} запрещён")
    if action is not None and (method != "POST" or action not in (rule.get("actions") or [])):
        raise Refused(f"действие {action!r} не разрешено правилами {coll}")
    body = None
    if method in ("POST", "PUT", "PATCH"):
        try:
            body = json.loads(body_raw or "{}")
        except ValueError as e:
            raise Refused(f"невалидный JSON тела: {e}") from None
        if action is None:
            body = apply_rules(body, rule)
    status, obj, text = call(method, p, q, body)
    if status < 300:
        if method == "POST" and oid is None:
            new_id = cc.dig(obj or {}, rule.get("id_field", "id"))
            if new_id is None:
                print("⚠️ Создано, но id в ответе не найден (id_field) — объект НЕ попал в ledger, "
                      "удалите его вручную и поправьте config.")
            else:
                ledger_append({"action": "created", "collection": coll, "id": str(new_id), "method": method})
                print(f"СОЗДАН тестовый объект {coll}/{new_id}. Удали после проверки: "
                      f"product_api.py --write delete {coll}/{new_id} (иначе удалит автоочистка).")
        elif method == "DELETE":
            ledger_append({"action": "deleted", "collection": coll, "id": oid, "method": method})
            print(f"Удалён свой тестовый объект {coll}/{oid}.")
        else:
            ledger_append({"action": "action" if action else "updated", "collection": coll, "id": oid,
                           "method": method, "op": action or ""})
    return status, obj, text


def do_cleanup(cfg, hours, has_flag):
    require_write(has_flag)
    cutoff = time.time() - hours * 3600
    old = {k: v for k, v in open_objects().items() if v.get("epoch", 0) <= cutoff}
    if not old:
        return ["чисто: своих объектов старше %g ч нет" % hours]
    lines = []
    for (coll, oid) in old:
        status, _, _ = call("DELETE", f"{coll}/{oid}")
        if status < 300 or status == 404:
            ledger_append({"action": "deleted", "collection": coll, "id": oid, "method": "DELETE",
                           "by": "cleanup", "status": status})
            lines.append(f"удалён {coll}/{oid}" + (" (уже не было, 404)" if status == 404 else ""))
        else:
            lines.append(f"НЕ удалился {coll}/{oid} (HTTP {status}) — нужен человек")
    return lines


# --- selftest ---

def selftest():
    import tempfile
    t = cc.Selftest(NAME)
    tmp = tempfile.mkdtemp()
    os.environ.update({"PRODUCT_API_LEDGER": os.path.join(tmp, "ledger.jsonl"),
                       "PRODUCT_API_URL": "https://sandbox-api.example.com", "PRODUCT_API_TOKEN": "x",
                       "PRODUCT_API_WRITE_DAILY_LIMIT": "3", "PRODUCT_API_TEST_MARKER": "AGENT-TEST"})
    for k in ("PRODUCT_API_WRITE_ENABLED", "PRODUCT_API_WRITE_CONTEXT"):
        os.environ.pop(k, None)
    cfg = {"get_allowlist": ["/v1/services", "/v1/bookings/"],
           "write": {"/v1/bookings": {"methods": ["POST", "DELETE"], "id_field": "data.id",
                                      "marker_field": "comment", "actions": ["cancel"],
                                      "require_in": {"client.phone": ["+15550000000"]},
                                      "max": {"prepayment": 0}, "force": {"notify_client": False}}}}
    calls = []
    seq = iter(range(100, 200))

    def fake(method, url, headers, body, timeout):
        calls.append((method, url, body))
        if method == "POST" and url.endswith("/v1/bookings"):
            return 201, json.dumps({"data": {"id": next(seq)}, "client": {"email": "eve@example.com"}})
        if method == "DELETE" and url.endswith("/404"):
            return 404, "{}"
        return 200, json.dumps({"ok": True})

    global _transport
    _transport = fake

    def refused(fn, what):
        try:
            fn()
            t.check(False, f"{what}: ждали отказ")
        except Refused:
            t.check(True, what)

    t.check(do_get("/v1/services?city=x", cfg)[0] == 200, "GET по allowlist")
    t.check(do_get("/v1/bookings/77", cfg)[0] == 200, "GET объекта по префиксу")
    refused(lambda: do_get("/v1/admin/users", cfg), "GET вне allowlist")
    refused(lambda: do_get("/v1/bookings/../admin", cfg), "обход каталогов")
    refused(lambda: do_get("https://evil.example.com/v1/services", cfg), "полный URL вместо пути")
    t.eq(get_allowed("/v1/servicesX", cfg), False, "префикс без границы сегмента")

    good = json.dumps({"client": {"phone": "+15550000000"}, "prepayment": 0, "comment": "проверка"})
    refused(lambda: do_write("post", "/v1/bookings", good, cfg, True), "запись выключена в .env")
    os.environ["PRODUCT_API_WRITE_ENABLED"] = "1"
    refused(lambda: do_write("post", "/v1/bookings", good, cfg, False), "без --write")
    refused(lambda: do_write("post", "/v1/bookings", good, cfg, True), "без контекста лички команды")
    os.environ["PRODUCT_API_WRITE_CONTEXT"] = "group"
    refused(lambda: do_write("post", "/v1/bookings", good, cfg, True), "контекст группы")
    os.environ["PRODUCT_API_WRITE_CONTEXT"] = "team-dm"
    refused(lambda: do_write("post", "/v1/bookings",
                             json.dumps({"client": {"phone": "+447700900123"}}), cfg, True), "реальный телефон")
    refused(lambda: do_write("post", "/v1/bookings",
                             json.dumps({"client": {"phone": "+15550000000"}, "prepayment": 500}), cfg, True), "предел суммы")
    refused(lambda: do_write("post", "/v1/payments", good, cfg, True), "коллекция вне config.write")
    refused(lambda: do_write("delete", "/v1/bookings", None, cfg, True), "DELETE на всю коллекцию")

    st, obj, _ = do_write("post", "/v1/bookings", good, cfg, True)
    sent = calls[-1][2]
    t.eq(st, 201, "создание прошло")
    t.check(sent["comment"].startswith("AGENT-TEST"), "маркер теста в теле")
    t.eq(sent["notify_client"], False, "force: уведомления клиенту выключены")
    t.check(("/v1/bookings", "100") in open_objects(), "объект в ledger")
    t.check("eve@example.com" not in json.dumps(cc.mask_obj(obj)), "PII в ответе маскируется")
    refused(lambda: do_write("delete", "/v1/bookings/999", None, cfg, True), "удаление чужого объекта")
    refused(lambda: do_write("post", "/v1/bookings/100/refund", "{}", cfg, True), "неразрешённое действие")
    t.eq(do_write("post", "/v1/bookings/100/cancel", "{}", cfg, True)[0], 200, "разрешённое действие над своим")
    t.eq(do_write("delete", "/v1/bookings/100", None, cfg, True)[0], 200, "удаление своего")
    t.check(("/v1/bookings", "100") not in open_objects(), "после удаления — не открыт")
    refused(lambda: do_write("post", "/v1/bookings", good, cfg, True), "дневной лимит (3 записи)")

    # cleanup: старый объект удаляется, свежий остаётся, 404 считается удалённым
    os.environ["PRODUCT_API_WRITE_DAILY_LIMIT"] = "100"
    now = time.time()
    ledger_append({"action": "created", "collection": "/v1/bookings", "id": "old1", "epoch": now - 30 * 3600})
    ledger_append({"action": "created", "collection": "/v1/bookings", "id": "404", "epoch": now - 30 * 3600})
    ledger_append({"action": "created", "collection": "/v1/bookings", "id": "fresh", "epoch": now - 600})
    os.environ["PRODUCT_API_WRITE_CONTEXT"] = "duty"
    lines = do_cleanup(cfg, 12, True)
    t.check(any("удалён /v1/bookings/old1" in x for x in lines), "cleanup удалил старый")
    t.check(any("404" in x for x in lines), "cleanup: 404 = уже удалён")
    t.check(("/v1/bookings", "fresh") in open_objects(), "cleanup не тронул свежий")
    t.check(all("fresh" not in c[1] for c in calls if c[0] == "DELETE"), "свежий не удалялся")
    t.report()


def main():
    a = sys.argv[1:]
    if a[:1] == ["--selftest"]:
        selftest()
        return
    # контекст записи — только из окружения процесса (его ставит бот или таймер), не из .env:
    # строка PRODUCT_API_WRITE_CONTEXT=team-dm в .env открыла бы запись из любого чата
    ctx = os.environ.get("PRODUCT_API_WRITE_CONTEXT")
    cc.load_env()
    if ctx is None:
        os.environ.pop("PRODUCT_API_WRITE_CONTEXT", None)
    cc.require_enabled(NAME)
    has_write = "--write" in a
    a = [x for x in a if x != "--write"]
    if not a:
        sys.exit(__doc__)
    cmd = a[0].lower()
    try:
        if cmd == "ledger":
            mine = open_objects()
            print(f"открытых своих объектов: {len(mine)}")
            for (coll, oid), r in mine.items():
                print(f"  {coll}/{oid} создан {r.get('ts')}")
            return
        cfg = load_config()
        if cmd == "get" and len(a) > 1:
            show(*do_get(a[1], cfg))
        elif cmd.upper() in WRITE_METHODS and len(a) > 1:
            show(*do_write(cmd, a[1], a[2] if len(a) > 2 else None, cfg, has_write))
        elif cmd == "cleanup":
            hours = cc.env_int("PRODUCT_API_CLEANUP_HOURS", 12)
            if "--older-than-hours" in a:
                hours = float(a[a.index("--older-than-hours") + 1])
            print("\n".join(do_cleanup(cfg, hours, has_write)))
        else:
            sys.exit(__doc__)
    except Refused as e:
        sys.exit(f"ОТКАЗ предохранителя: {e}. Скажи об этом спрашивающему как есть, не пытайся обойти.")
    except cc.NetError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
