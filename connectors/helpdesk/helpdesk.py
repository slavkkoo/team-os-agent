#!/usr/bin/env python3
"""helpdesk — выгрузка закрытых тикетов хелпдеска в пары «вопрос клиента / ответ оператора».

Usage:
  helpdesk.py fetch --out <каталог>_run/pairs.json [--days 7] [--offset 0]
      окно [сегодня - offset - days, сегодня - offset); offset — прошлые недели
  helpdesk.py --selftest      без сети, на фикстурах (оба адаптера)

Адаптеры (config/helpdesk.json, пример — config/helpdesk.example.json):
  zendesk  — Zendesk API v2: search (status>=solved за окно) + comments тикета;
             клиент = автор, совпадающий с requester_id; внутренние заметки (public=false) пропускаются;
  generic  — любой REST: пути, параметры и имена полей задаются маппингом в конфиге.
Авторизация: HELPDESK_AUTH=basic (HELPDESK_USER + HELPDESK_TOKEN; для Zendesk
HELPDESK_USER=<email>/token) | bearer (HELPDESK_TOKEN) | header (HELPDESK_AUTH_HEADER + HELPDESK_TOKEN).

Только чтение (GET). Сырые пары пишутся ТОЛЬКО в каталог *_run/ (он в .gitignore и закрыт
от чтения моделью в чате); email, телефоны и токены маскируются уже здесь.
Включение: CONNECTOR_HELPDESK_ENABLED=1 + HELPDESK_URL.
"""
import base64
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import connector_common as cc  # noqa: E402

NAME = "helpdesk"
_transport = cc.http
_sleep = time.sleep

_ID_RE = re.compile(r"\b\d{6,}\b|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}", re.I)
_ASK_RE = re.compile(r"провер|посмотр|почему.*(ошибк|упал|не созда|не приход|завис|не работ)|падает|"
                     r"check|look into|why.*(fail|error|missing|not)", re.I)


def needs_logs(q):
    """Разбор конкретного случая (есть идентификатор + просьба проверить) — не переносимое знание."""
    return bool(_ID_RE.search(q) and _ASK_RE.search(q))


def load_config(p=None):
    p = p or cc.env("HELPDESK_CONFIG", cc.path("config", "helpdesk.json"))
    if not os.path.exists(p):
        sys.exit(f"Нет конфига {p}: cp config/helpdesk.example.json config/helpdesk.json и поправьте")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def auth_headers():
    kind, tok = cc.env("HELPDESK_AUTH", "basic"), cc.env("HELPDESK_TOKEN")
    if not tok:
        return {}
    if kind == "bearer":
        return {"Authorization": "Bearer " + tok}
    if kind == "header":
        return {cc.env("HELPDESK_AUTH_HEADER", "X-Api-Key"): tok}
    raw = f"{cc.env('HELPDESK_USER')}:{tok}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


def api(path, params=None, retries=3):
    base = cc.env("HELPDESK_URL").rstrip("/")
    if not base:
        sys.exit("HELPDESK_URL не задан в .env")
    url = base + path + (("?" + urllib.parse.urlencode(params)) if params else "")
    status, text = _transport("GET", url, auth_headers(), None, 40)
    if status == 429 and retries > 0:
        _sleep((4 - retries) * 2)
        return api(path, params, retries - 1)
    if status >= 300:
        raise RuntimeError(f"хелпдеск ответил HTTP {status} на {path} — проверьте права токена и маппинг")
    return json.loads(text)


def _fmt(template, **kw):
    return template.format(**kw) if isinstance(template, str) else template


def strip_html(s):
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s or "")
    s = re.sub(r"(?i)<br\s*/?>|</p>", "\n", s)
    return html.unescape(re.sub(r"<[^>]+>", " ", s))


def list_tickets(ad, since, until):
    lst = ad["list"]
    per_page = int((lst.get("params") or {}).get("per_page") or (lst.get("params") or {}).get("limit") or 100)
    cap = cc.env_int("HELPDESK_MAX_TICKETS", 300)
    out = []
    for page in range(1, int(lst.get("max_pages", 20)) + 1):
        params = {k: _fmt(v, since=since, until=until, page=page,
                          since_ts=_epoch(since), until_ts=_epoch(until))
                  for k, v in (lst.get("params") or {}).items()}
        resp = api(_fmt(lst["path"], since=since, until=until, page=page), params)
        items = cc.dig(resp, lst.get("items", "")) or []
        out += items
        if len(items) < per_page or len(out) >= cap:
            break
        _sleep(0.15)
    return out[:cap]


def _epoch(day):
    return str(int(dt.datetime.fromisoformat(day).replace(tzinfo=dt.timezone.utc).timestamp()))


def ticket_pair(ad, t, noise_re):
    tm, mm = ad["ticket"], ad["messages"]
    if any(cc.dig(t, k) for k in tm.get("skip_if_true") or []):
        return None
    subject = str(cc.dig(t, tm.get("subject", "subject")) or "")
    if noise_re and noise_re.search(subject):
        return None
    tid = cc.dig(t, tm.get("id", "id"))
    requester = cc.dig(t, tm.get("requester_id", "")) if tm.get("requester_id") else None
    resp = api(_fmt(mm["path"], id=tid))
    msgs = cc.dig(resp, mm.get("items", "")) or []
    dialog = []
    for m in msgs:
        if mm.get("public") and cc.dig(m, mm["public"]) is False:
            continue                         # внутренняя заметка оператора
        if mm.get("internal") and cc.dig(m, mm["internal"]):
            continue
        body = cc.dig(m, mm.get("body", "body"))
        if not body and mm.get("body_html"):
            body = strip_html(cc.dig(m, mm["body_html"]) or "")
        body = re.sub(r"[ \t]+", " ", str(body or "")).strip()
        if not body:
            continue
        if mm.get("role"):
            staff = cc.dig(m, mm["role"]) in (mm.get("staff_values") or [])
        else:
            staff = requester is not None and cc.dig(m, mm.get("author_id", "author_id")) != requester
        dialog.append((str(cc.dig(m, mm.get("created_at", "created_at")) or ""), staff, body))
    dialog.sort(key=lambda x: x[0])
    first_sup = next((i for i, d in enumerate(dialog) if d[1]), -1)
    if first_sup == -1:
        return None                          # без ответа оператора учиться нечему
    seen, q_parts = set(), []
    for part in [subject] + [d[2] for d in dialog[:first_sup] if not d[1]]:
        if part and part not in seen:
            seen.add(part)
            q_parts.append(part)
    question = "\n".join(q_parts).strip()
    answer = "\n---\n".join(d[2] for d in dialog if d[1])
    if not question:
        return None
    question, answer = cc.mask_text(question[:3000]), cc.mask_text(answer[:3000])
    return {"ticket_id": str(tid), "created_at": cc.dig(t, tm.get("created_at", "created_at")),
            "question": question, "answer": answer, "needs_logs": needs_logs(question)}


def fetch(cfg, days, offset):
    name = cfg.get("adapter", "generic")
    ad = (cfg.get("adapters") or {}).get(name)
    if not ad:
        sys.exit(f"адаптер {name!r} не описан в config.adapters")
    hi = dt.date.today() - dt.timedelta(days=offset)
    lo = hi - dt.timedelta(days=days)
    noise = re.compile(cfg["noise_subject_re"], re.I) if cfg.get("noise_subject_re") else None
    pairs, skipped, errors = [], 0, 0
    for t in list_tickets(ad, lo.isoformat(), hi.isoformat()):
        try:
            p = ticket_pair(ad, t, noise)
        except RuntimeError:
            errors += 1
            continue
        if p:
            pairs.append(p)
        else:
            skipped += 1
        _sleep(0.15)
    return {"adapter": name, "days": days, "offset": offset, "window": f"{lo} — {hi}",
            "count": len(pairs), "skipped": skipped, "errors": errors, "pairs": pairs}


# --- selftest ---

def selftest():
    import tempfile
    t = cc.Selftest(NAME)
    here = os.path.dirname(os.path.abspath(__file__))
    cfg = json.load(open(os.path.join(here, "..", "..", "config", "helpdesk.example.json"), encoding="utf-8"))
    os.environ.update({"HELPDESK_URL": "https://acme.zendesk.example.com", "HELPDESK_AUTH": "basic",
                       "HELPDESK_USER": "agent@example.com/token", "HELPDESK_TOKEN": "x"})
    global _transport, _sleep
    _sleep = lambda s: None  # noqa: E731

    zd_search = {"results": [
        {"id": 501, "subject": "Как перенести запись клиента?", "requester_id": 9, "created_at": "2026-10-20T10:00:00Z"},
        {"id": 502, "subject": "Без ответа оператора", "requester_id": 9, "created_at": "2026-10-21T10:00:00Z"}]}
    zd_comments = {
        501: {"comments": [
            {"author_id": 9, "public": True, "plain_body": "Клиент anna@example.com просит перенести на завтра",
             "created_at": "2026-10-20T10:00:00Z"},
            {"author_id": 77, "public": False, "plain_body": "внутренняя заметка: позвонить +44 20 7946 0958",
             "created_at": "2026-10-20T10:05:00Z"},
            {"author_id": 77, "public": True, "plain_body": "Перенос — в карточке записи, кнопка «Перенести», не позже чем за 2 часа.",
             "created_at": "2026-10-20T10:10:00Z"}]},
        502: {"comments": [{"author_id": 9, "public": True, "plain_body": "алло?", "created_at": "2026-10-21T10:00:00Z"}]}}
    calls = []

    def zd(method, url, headers, body, timeout):
        calls.append(url)
        t.check(method == "GET", "только GET")
        if "/search.json" in url:
            return 200, json.dumps(zd_search)
        tid = int(url.split("/tickets/")[1].split("/")[0])
        return 200, json.dumps(zd_comments[tid])

    _transport = zd
    cfg["adapter"] = "zendesk"
    res = fetch(cfg, 7, 0)
    t.eq(res["count"], 1, "zendesk: одна пара (тикет без ответа отброшен)")
    p = res["pairs"][0]
    t.check(p["question"].startswith("Как перенести запись клиента?\nКлиент"), "вопрос = тема + реплики клиента до ответа")
    t.check("***@***" in p["question"] and "anna@" not in p["question"], "email в вопросе замаскирован")
    t.check("внутренняя заметка" not in p["answer"], "внутренние заметки не попадают в ответ")
    t.check(p["answer"].startswith("Перенос — в карточке"), "ответ оператора")
    t.eq(p["needs_logs"], False, "общий вопрос — не разбор случая")
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(calls[0]).query)
    t.check("status>=solved" in q["query"][0] and "updated>=" in q["query"][0], "zendesk: окно и статус в search")
    t.check(auth_headers()["Authorization"].startswith("Basic "), "zendesk: basic email/token")

    page1 = {"data": [{"id": "a1", "subject": "Не приходит SMS о записи", "requester": {"id": "c1"}, "created_at": "2026-10-22"},
                      {"id": "a2", "subject": "Автоответ: принято", "requester": {"id": "c1"}, "created_at": "2026-10-22"}]}
    page2 = {"data": [{"id": "a3", "subject": "спам", "spam": True, "requester": {"id": "c2"}, "created_at": "2026-10-23"}]}
    msgs = {"a1": {"data": [
        {"author": {"id": "c1", "type": "client"}, "text": "", "html": "<p>Проверьте запись 12345678, почему не пришло SMS?</p>",
         "created_at": "1"},
        {"author": {"id": "s1", "type": "staff"}, "text": "SMS уходит за 24 часа, если у клиента указан телефон.", "created_at": "2"},
        {"author": {"id": "s1", "type": "staff"}, "text": "служебное", "is_note": True, "created_at": "3"}]}}

    def gen(method, url, headers, body, timeout):
        if "/messages" in url:
            return 200, json.dumps(msgs[url.split("/tickets/")[1].split("/")[0]])
        page = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("page", ["1"])[0]
        return 200, json.dumps(page1 if page == "1" else page2)

    _transport = gen
    cfg["adapter"] = "generic"
    cfg["adapters"]["generic"]["list"]["params"]["limit"] = "2"
    res = fetch(cfg, 7, 7)
    t.eq(res["count"], 1, "generic: одна пара (автоответ и спам отброшены)")
    p = res["pairs"][0]
    t.check("<p>" not in p["question"] and "12345678" in p["question"], "generic: html-тело очищено")
    t.eq(p["needs_logs"], True, "id + «проверьте» = разбор случая")
    t.check("служебное" not in p["answer"], "generic: заметка (is_note) отброшена")
    t.check(res["skipped"] == 2, "generic: две страницы, отброшено 2")
    lo, hi = res["window"].split(" — ")
    t.eq((dt.date.fromisoformat(hi) - dt.date.fromisoformat(lo)).days, 7, "окно 7 дней со сдвигом")

    tmp = tempfile.mkdtemp()
    try:
        cc.require_run_dir(os.path.join(tmp, "pairs.json"))
        t.check(False, "вывод вне *_run должен быть отклонён")
    except SystemExit:
        t.check(True, "вывод вне *_run отклонён")
    t.check(cc.require_run_dir(os.path.join(tmp, "helpdesk_run", "pairs.json")).endswith("pairs.json"), "вывод в *_run")
    t.report()


def main():
    a = sys.argv[1:]
    if a[:1] == ["--selftest"]:
        selftest()
        return
    cc.load_env()
    cc.require_enabled(NAME)
    if a[:1] != ["fetch"] or "--out" not in a:
        sys.exit(__doc__)

    def opt(k, d):
        return a[a.index(k) + 1] if k in a and a.index(k) + 1 < len(a) else d
    out = cc.require_run_dir(opt("--out", ""))
    try:
        res = fetch(load_config(), int(opt("--days", 7)), int(opt("--offset", 0)))
    except cc.NetError as e:
        sys.exit(str(e))
    except RuntimeError as e:
        sys.exit(str(e))
    with open(out, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False)
    os.chmod(out, 0o600)
    print(f"{res['window']}: пар {res['count']}, отброшено {res['skipped']}, ошибок {res['errors']} → {out}")


if __name__ == "__main__":
    main()
