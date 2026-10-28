#!/usr/bin/env python3
"""client_reports — ежедневный отчёт метрик конкретному клиенту в его чат.

Usage:
  client_reports.py run [--date YYYY-MM-DD] [--only <name>] [--dry-run]
      отчётный день — вчера (по TZ сервера); --dry-run печатает решения, ничего не шлёт
  client_reports.py --selftest     без сети, на фикстурах

Конфиг: config/client-reports.json (пример — config/client-reports.example.json):
клиент → chat_id → источник метрик → какие метрики показать. Источники:
  http_json — GET JSON-эндпоинта со сводкой за день;
  sql_http  — POST SQL-запроса в отчётный эндпоинт своего продукта (read-only роль БД!);
  csv       — локальный файл или URL выгрузки.

ГЛАВНОЕ ПРАВИЛО: нет данных — не отправлять. Если хоть одна метрика «н/д» или источник
упал, клиент НИЧЕГО не получает, владельцу уходит алерт с черновиком. (Урок прода: при
лежащем источнике клиентам несколько дней подряд уходили отчёты из одних «н/д».)
draft_only=true (по умолчанию) — отчёт идёт владельцу черновиком: первые недели сверяйте
цифры глазами, прежде чем открывать клиенту.
Включение: CONNECTOR_CLIENT_REPORTS_ENABLED=1.
"""
import csv
import datetime as dt
import io
import json
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import connector_common as cc  # noqa: E402

NAME = "client-reports"
ND = "н/д"
_transport = cc.http


class SourceError(Exception):
    pass


def load_config(p=None):
    p = p or cc.env("CLIENT_REPORTS_CONFIG", cc.path("config", "client-reports.json"))
    if not os.path.exists(p):
        sys.exit(f"Нет конфига {p}: cp config/client-reports.example.json config/client-reports.json и поправьте")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _auth(src):
    tok = cc.env(src.get("auth_env", "CLIENT_REPORTS_TOKEN"))
    return {"Authorization": "Bearer " + tok} if tok else {}


def _num(v):
    if v is None or v == "":
        return None
    try:
        f = float(str(v).replace(",", ".").replace(" ", ""))
        return int(f) if f.is_integer() else f
    except ValueError:
        return None


def fetch_values(src, client_id, day):
    """→ dict метрика → число (или None, если нет). Ошибка источника → SourceError."""
    kind = src.get("type")
    fmt = {"client_id": urllib.parse.quote(str(client_id)), "date": day}
    try:
        if kind == "http_json":
            st, text = _transport("GET", src["url"].format(**fmt), _auth(src), None, 40)
            if st >= 300:
                raise SourceError(f"HTTP {st}")
            data = cc.dig(cc.parse_json(text) or {}, src.get("values_path", ""))
            if not isinstance(data, dict):
                raise SourceError("ответ не объект метрик")
            return {k: _num(v) for k, v in data.items()}
        if kind == "sql_http":
            body = {"query": src["query"], "params": {"client_id": str(client_id), "date": day}}
            st, text = _transport("POST", src["url"], _auth(src), body, 60)
            if st >= 300:
                raise SourceError(f"HTTP {st}")
            d = cc.parse_json(text) or {}
            rows, cols = cc.dig(d, src.get("rows_path", "rows")), cc.dig(d, src.get("columns_path", "columns"))
            if not rows:
                return {}
            row = rows[0]
            if isinstance(row, dict):
                return {k: _num(v) for k, v in row.items()}
            return {c: _num(v) for c, v in zip(cols or [], row)}
        if kind == "csv":
            loc = src["path"]
            if loc.startswith(("http://", "https://")):
                st, text = _transport("GET", loc, _auth(src), None, 40)
                if st >= 300:
                    raise SourceError(f"HTTP {st}")
            else:
                with open(loc, encoding="utf-8") as f:
                    text = f.read()
            for row in csv.DictReader(io.StringIO(text), delimiter=src.get("delimiter", ",")):
                if row.get(src.get("date_column", "date")) == day and \
                        row.get(src.get("client_column", "client_id")) == str(client_id):
                    return {k: _num(v) for k, v in row.items()}
            return {}
    except cc.NetError as e:
        raise SourceError(str(e)) from None
    except OSError as e:
        raise SourceError(f"{type(e).__name__}: {e}") from None
    raise SourceError(f"неизвестный тип источника {kind!r}")


def pct(cur, prev, prev_day):
    if cur is None or prev is None:
        return ""
    if prev == 0:
        return f"  (накануне 0)"
    d = (cur - prev) * 100.0 / prev
    arrow = "▲" if d > 0 else ("▼" if d < 0 else "—")
    return f"  {arrow} {abs(round(d, 1)):g}% к {prev_day}"


def build(rep, cfg, day):
    """→ dict(action, text, missing, error). action: send | draft | alert."""
    src = (cfg.get("sources") or {}).get(rep.get("source"))
    metrics = rep.get("metrics") or []
    err, cur, prev = "", {}, {}
    d = dt.date.fromisoformat(day)
    prev_iso = (d - dt.timedelta(days=1)).isoformat()
    if not src:
        err = f"источник {rep.get('source')!r} не описан в config.sources"
    else:
        try:
            cur = fetch_values(src, rep.get("client_id"), day)
            if any(m.get("compare") for m in metrics):
                prev = fetch_values(src, rep.get("client_id"), prev_iso)
        except SourceError as e:
            err = str(e)
    lines = [f"📊 {rep.get('label') or rep['name']} — {d.strftime('%d.%m.%Y')}", ""]
    missing = []
    for m in metrics:
        v = cur.get(m["key"])
        if v is None:
            missing.append(m["key"])
        shown = ND if v is None else f"{v:g}" if isinstance(v, float) else str(v)
        extra = pct(v, prev.get(m["key"]), (d - dt.timedelta(days=1)).strftime("%d.%m")) if m.get("compare") else ""
        lines.append(f"▪️ {m.get('title', m['key'])}: {shown}{extra}")
    if rep.get("footer"):
        lines += ["", rep["footer"]]
    text = "\n".join(lines)
    chat = str(rep.get("chat_id") or "")
    draft_only = rep.get("draft_only", (cfg.get("defaults") or {}).get("draft_only", True))
    if err or missing or not metrics:
        action = "alert"
    elif draft_only or not chat or chat.startswith("<"):
        action = "draft"
    else:
        action = "send"
    return {"name": rep["name"], "chat_id": chat, "action": action, "text": text,
            "missing": missing, "error": err}


def tg_send(chat_id, text):
    """Отправка с проверкой ok:true у каждого куска: «ушло» = Telegram подтвердил."""
    tok = cc.env("TG_BOT_TOKEN")
    if not tok or not chat_id:
        return False
    for i in range(0, len(text), 3900):
        body = urllib.parse.urlencode({"chat_id": chat_id, "text": text[i:i + 3900],
                                       "disable_web_page_preview": "true"}).encode()
        try:
            st, resp = _transport("POST", f"https://api.telegram.org/bot{tok}/sendMessage",
                                  {"Content-Type": "application/x-www-form-urlencoded"}, body, 30)
        except cc.NetError:
            return False
        if st >= 300 or '"ok":true' not in resp.replace(" ", ""):
            return False
    return True


def route(res, owner, send=tg_send, dry=False):
    """Решение → отправка. Возвращает строку журнала и признак проблемы."""
    if res["action"] == "send":
        msg, to = res["text"], res["chat_id"]
    elif res["action"] == "draft":
        msg, to = f"📝 Черновик отчёта «{res['name']}» (клиенту не отправлен: draft_only/нет chat_id):\n\n{res['text']}", owner
    else:
        why = "; ".join(x for x in (f"ошибка источника: {res['error']}" if res["error"] else "",
                                    f"{ND}: {', '.join(res['missing'])}" if res["missing"] else "") if x)
        msg, to = (f"⚠️ client-reports: отчёт «{res['name']}» неполный ({why or 'нет метрик'}) — "
                   f"клиенту НЕ отправлен. Черновик:\n\n{res['text']}"), owner
    if dry:
        return f"[dry-run] {res['name']}: {res['action']} → {to}", res["action"] == "alert"
    ok = send(to, msg)
    problem = res["action"] == "alert" or not ok
    return f"{res['name']}: {res['action']} → {to} ({'доставлено' if ok else 'НЕ доставлено'})", problem


# --- selftest ---

def selftest():
    import tempfile
    t = cc.Selftest(NAME)
    here = os.path.dirname(os.path.abspath(__file__))
    cfg = json.load(open(os.path.join(here, "..", "..", "config", "client-reports.example.json"), encoding="utf-8"))
    day = "2026-10-27"
    data = {("c-001", "2026-10-27"): {"bookings_created": 44, "bookings_cancelled": 3, "no_show": 2},
            ("c-001", "2026-10-26"): {"bookings_created": 40, "bookings_cancelled": 4, "no_show": 0}}
    calls = []
    global _transport

    def fake(method, url, headers, body, timeout):
        calls.append((method, url, body))
        if "metrics.example.com" in url:
            q = urllib.parse.urlsplit(url)
            cid, date = q.path.split("/")[-2], urllib.parse.parse_qs(q.query)["date"][0]
            if cid == "down":
                raise cc.NetError("metrics.example.com недоступен (timeout)")
            return 200, json.dumps({"data": data.get((cid, date), {})})
        if "reports/query" in url:
            return 200, json.dumps({"columns": ["bookings_created", "revenue"], "rows": [[12, "1500.5"]]})
        if "api.telegram.org" in url:
            return 200, '{"ok":true,"result":{}}'
        return 404, ""

    _transport = fake
    rep = dict(cfg["reports"][0], draft_only=False, chat_id="-1000")
    r = build(rep, cfg, day)
    t.eq(r["action"], "send", "все метрики есть, draft_only=false → клиенту")
    t.check("▪️ Создано записей: 44  ▲ 10% к 26.10" in r["text"], "рост в %")
    t.check("▪️ Отменено: 3  ▼ 25% к 26.10" in r["text"], "падение в %")
    t.check("Неявки: 2  (накануне 0)" in r["text"], "накануне 0 — без деления на ноль")

    r = build(dict(rep, client_id="c-404"), cfg, day)
    t.eq(r["action"], "alert", "нет данных за день → алерт, не клиенту")
    t.check(ND in r["text"] and set(r["missing"]) == {"bookings_created", "bookings_cancelled", "no_show"}, "все н/д")
    r = build(dict(rep, client_id="down"), cfg, day)
    t.eq(r["action"], "alert", "источник недоступен → алерт")
    t.check("недоступен" in r["error"], "причина в алерте")
    part = dict(rep, metrics=rep["metrics"] + [{"key": "revenue", "title": "Выручка"}])
    r = build(part, cfg, day)
    t.eq((r["action"], r["missing"]), ("alert", ["revenue"]), "одна метрика н/д → весь отчёт не уходит")
    t.eq(build(dict(rep, draft_only=True), cfg, day)["action"], "draft", "draft_only → черновик владельцу")
    t.eq(build(dict(rep, chat_id="<chat_id клиента>"), cfg, day)["action"], "draft", "плейсхолдер chat_id → черновик")

    sql = dict(cfg["reports"][1], draft_only=False, chat_id="-1001")
    r = build(sql, cfg, day)
    t.eq(r["action"], "send", "sql_http: строка по колонкам")
    t.check("Выручка: 1500.5" in r["text"], "sql_http: число из строки")
    t.check(calls[-1][2]["params"] == {"client_id": "c-002", "date": day}, "sql_http: параметры, а не подстановка в SQL")

    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "daily.csv")
    with open(p, "w", encoding="utf-8") as f:
        f.write("date,client_id,bookings_created,no_show\n2026-10-27,c-003,7,1\n2026-10-27,c-009,99,9\n")
    cfg["sources"]["export-csv"]["path"] = p
    r = build(dict(cfg["reports"][2], draft_only=False, chat_id="-1002"), cfg, day)
    t.check(r["action"] == "send" and "Создано записей: 7" in r["text"], "csv: строка нужного клиента и дня")

    sent = []
    fake_send = lambda to, msg: sent.append((to, msg)) or True  # noqa: E731
    line, prob = route(build(dict(rep, client_id="c-404"), cfg, day), "OWNER", fake_send)
    t.check(prob and sent[-1][0] == "OWNER" and "НЕ отправлен" in sent[-1][1], "алерт уходит владельцу, не клиенту")
    line, prob = route(build(rep, cfg, day), "OWNER", fake_send)
    t.check(not prob and sent[-1][0] == "-1000", "полный отчёт → чат клиента")
    os.environ["TG_BOT_TOKEN"] = "x"
    t.check(tg_send("-1000", "a" * 5000), "tg_send: два куска, оба ok:true")
    t.report()


def main():
    a = sys.argv[1:]
    if a[:1] == ["--selftest"]:
        selftest()
        return
    cc.load_env()
    cc.require_enabled(NAME)
    if a[:1] != ["run"]:
        sys.exit(__doc__)

    def opt(k):
        return a[a.index(k) + 1] if k in a and a.index(k) + 1 < len(a) else ""
    day = opt("--date") or (dt.date.today() - dt.timedelta(days=1)).isoformat()
    cfg = load_config()
    owner = cc.env("OWNER_ID")
    problems = 0
    for rep in cfg.get("reports") or []:
        if opt("--only") and rep.get("name") != opt("--only"):
            continue
        line, prob = route(build(rep, cfg, day), owner, dry="--dry-run" in a)
        problems += prob
        print(line)
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
