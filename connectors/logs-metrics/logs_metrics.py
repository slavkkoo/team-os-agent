#!/usr/bin/env python3
"""logs_metrics — read-only логи (Elasticsearch/OpenSearch) и метрики (Prometheus) для агента.

Usage:
  logs_metrics.py health
  logs_metrics.py search "<запрос>" [--index <индекс>] [--window 1h] [--size 50]
      поиск логов за окно (simple_query_string: слова через AND, "фраза в кавычках")
  logs_metrics.py trace <trace_id> [--index <индекс>] [--window 24h]
      все события одного запроса по полю LOGS_TRACE_FIELD, по времени, сжато
  logs_metrics.py count "<запрос>" [--index <индекс>] [--window 24h]
  logs_metrics.py prom "<PromQL>"                          мгновенное значение
  logs_metrics.py prom-range "<PromQL>" [--window 6h] [--step 5m]   сводка по ряду
  logs_metrics.py --selftest                                без сети, на фикстурах

Границы (в обёртке, не в промпте):
  - только чтение: _search/_count к ES и /api/v1/query* к Prometheus, ничего больше;
  - индексы — только из LOGS_INDEX_ALLOWLIST (запрос «*», «_all», чужой префикс — отказ);
  - окно не больше LOGS_MAX_WINDOW (по умолчанию 7d), выдача не больше 200 документов;
  - email, телефоны, токены и ключи маскируются здесь, ДОПОЛНИТЕЛЬНО к маскированию
    на стороне сервиса (свой прокси с серверной санитизацией — лучший вариант);
  - вывод сжат и обрезан (CONNECTOR_MAX_OUT).
Включение: CONNECTOR_LOGS_METRICS_ENABLED=1 + LOGS_ES_URL / METRICS_PROM_URL в .env.
"""
import base64
import fnmatch
import json
import os
import re
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import connector_common as cc  # noqa: E402

NAME = "logs-metrics"
MAX_SIZE = 200
MSG_LIMIT = 400


# --- конфигурация и проверки ---

def parse_window(w, max_w=None):
    """«15m» / «6h» / «7d» → секунды; окно шире LOGS_MAX_WINDOW урезается."""
    m = re.fullmatch(r"(\d{1,4})([mhd])", (w or "").strip())
    if not m:
        sys.exit(f"Окно {w!r}: нужен формат 15m / 6h / 7d")
    sec = int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400}[m.group(2)]
    lim = parse_window(max_w, None)[0] if max_w else None
    if lim and sec > lim:
        return lim, True
    return sec, False


def es_window(sec):
    return f"now-{sec // 60}m" if sec % 3600 else f"now-{sec // 3600}h"


def check_index(index, allow):
    """Индекс разрешён, только если он ДОСЛОВНО подпадает под шаблон allowlist.
    Запрос «acme-*» при allowlist «acme-app-*» не пройдёт: fnmatch сравнивает
    запрошенное имя как строку, звёздочка в запросе не расширяет доступ."""
    if not allow:
        return False, "LOGS_INDEX_ALLOWLIST пуст — задайте разрешённые индексы в .env"
    for part in index.split(","):
        p = part.strip()
        if not p or p in ("*", "_all") or p.startswith(("-", ".", "_")) or "/" in p or ".." in p:
            return False, f"индекс {p!r} запрещён"
        if not any(fnmatch.fnmatchcase(p, pat) for pat in allow):
            return False, f"индекс {p!r} вне LOGS_INDEX_ALLOWLIST ({', '.join(allow)})"
    return True, ""


def es_headers():
    h = {}
    if cc.env("LOGS_ES_API_KEY"):
        h["Authorization"] = "ApiKey " + cc.env("LOGS_ES_API_KEY")
    elif cc.env("LOGS_ES_USER"):
        raw = f"{cc.env('LOGS_ES_USER')}:{cc.env('LOGS_ES_PASSWORD')}".encode()
        h["Authorization"] = "Basic " + base64.b64encode(raw).decode()
    return h


def fields():
    return {"time": cc.env("LOGS_TIME_FIELD", "@timestamp"),
            "msg": cc.env("LOGS_MESSAGE_FIELD", "message"),
            "level": cc.env("LOGS_LEVEL_FIELD", "level"),
            "service": cc.env("LOGS_SERVICE_FIELD", "service"),
            "trace": cc.env("LOGS_TRACE_FIELD", "trace_id")}


def build_search(query, sec, size, f, trace=None):
    flt = [{"range": {f["time"]: {"gte": es_window(sec)}}}]
    if trace:
        flt.append({"term": {f["trace"]: trace}})
    must = [{"simple_query_string": {"query": query, "default_operator": "and"}}] if query else []
    return {"size": min(max(size, 1), MAX_SIZE),
            "sort": [{f["time"]: {"order": "asc" if trace else "desc"}}],
            "track_total_hits": True,
            "_source": [f["time"], f["msg"], f["level"], f["service"], f["trace"]],
            "query": {"bool": {"filter": flt, "must": must}}}


# --- форматирование ---

def _flat(src, dotted):
    v = cc.dig(src, dotted)
    if v is None and dotted in src:
        v = src[dotted]           # поле с точкой в имени («log.level»), а не вложенное
    return v


def compact_hits(resp, f, header):
    hits = (resp.get("hits") or {}).get("hits") or []
    total = (resp.get("hits") or {}).get("total")
    total = total.get("value") if isinstance(total, dict) else total
    lines = [f"{header}: показано {len(hits)} из {total if total is not None else '?'}"]
    if not hits:
        lines.append("0 совпадений в этом окне. Это НЕ значит, что события не было: расширь "
                     "--window или проверь запрос/индекс, прежде чем делать вывод.")
    for h in hits:
        s = h.get("_source") or {}
        ts = str(_flat(s, f["time"]) or "?")
        msg = re.sub(r"\s+", " ", str(_flat(s, f["msg"]) or "")).strip()
        if len(msg) > MSG_LIMIT:
            msg = msg[:MSG_LIMIT] + f" …(+{len(msg) - MSG_LIMIT})"
        lvl = _flat(s, f["level"]) or "-"
        svc = _flat(s, f["service"]) or "-"
        lines.append(f"{ts} {lvl} {svc}: {msg}")
    lines.append("Время — как в индексе (обычно UTC).")
    return "\n".join(lines)


def _labels(metric):
    name = metric.get("__name__", "")
    rest = ",".join(f'{k}="{v}"' for k, v in sorted(metric.items()) if k != "__name__")
    return f"{name}{{{rest}}}" if rest else (name or "{}")


def compact_prom(resp, max_series=30):
    if resp.get("status") != "success":
        return f"Prometheus: {resp.get('errorType', 'error')}: {resp.get('error', resp)}"
    data = resp.get("data") or {}
    rtype, res = data.get("resultType"), data.get("result") or []
    if rtype == "scalar":
        return f"scalar = {res[1] if len(res) > 1 else res}"
    lines = [f"{rtype}: рядов {len(res)}" + (f" (показаны первые {max_series})" if len(res) > max_series else "")]
    if not res:
        lines.append("Пусто: метрики с такими метками нет в окне — проверь имя и метки, это не «ноль».")
    for s in res[:max_series]:
        lbl = _labels(s.get("metric") or {})
        if rtype == "vector":
            lines.append(f"{lbl} = {(s.get('value') or [None, '?'])[1]}")
        else:
            vals = []
            for _, v in s.get("values") or []:
                try:
                    vals.append(float(v))
                except (TypeError, ValueError):
                    pass
            if vals:
                lines.append(f"{lbl}: точек {len(vals)}, первое {vals[0]:g}, последнее {vals[-1]:g}, "
                             f"min {min(vals):g}, max {max(vals):g}")
            else:
                lines.append(f"{lbl}: нет числовых точек")
    return "\n".join(lines)


# --- команды ---

def _opts(args, **defaults):
    pos, opts = [], dict(defaults)
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--") and a[2:] in opts and i + 1 < len(args):
            opts[a[2:]] = args[i + 1]
            i += 2
            continue
        pos.append(a)
        i += 1
    return pos, opts


def es_call(index, endpoint, body):
    base = cc.env("LOGS_ES_URL").rstrip("/")
    if not base:
        sys.exit("LOGS_ES_URL не задан в .env — логи не подключены")
    url = f"{base}/{urllib.parse.quote(index, safe=',*')}/{endpoint}"
    try:
        status, text = cc.http("POST", url, es_headers(), body, timeout=60)
    except cc.NetError as e:
        sys.exit(str(e))
    if status >= 300:
        sys.exit(f"Elasticsearch ответил HTTP {status} (сервис доступен — проблема в запросе или правах): "
                 + cc.mask_text(text[:400]))
    return cc.parse_json(text) or {}


def prom_call(path, params):
    base = cc.env("METRICS_PROM_URL").rstrip("/")
    if not base:
        sys.exit("METRICS_PROM_URL не задан в .env — метрики не подключены")
    h = {"Authorization": "Bearer " + cc.env("METRICS_PROM_TOKEN")} if cc.env("METRICS_PROM_TOKEN") else {}
    try:
        status, text = cc.http("GET", f"{base}{path}?{urllib.parse.urlencode(params)}", h, timeout=60)
    except cc.NetError as e:
        sys.exit(str(e))
    d = cc.parse_json(text)
    if d is None:
        sys.exit(f"Prometheus ответил HTTP {status}, не JSON: " + cc.mask_text(text[:300]))
    return d


def cmd_logs(kind, args):
    allow = cc.env_list("LOGS_INDEX_ALLOWLIST")
    pos, o = _opts(args, index=(allow[0] if allow else ""), window=("24h" if kind != "search" else "1h"), size="50")
    ok, why = check_index(o["index"], allow)
    if not ok:
        sys.exit("Отказ: " + why)
    sec, clipped = parse_window(o["window"], cc.env("LOGS_MAX_WINDOW", "7d"))
    f = fields()
    note = f" (окно урезано до LOGS_MAX_WINDOW={cc.env('LOGS_MAX_WINDOW', '7d')})" if clipped else ""
    if kind == "trace":
        if not pos or not re.fullmatch(r"[A-Za-z0-9._:-]{6,128}", pos[0]):
            sys.exit("usage: logs_metrics.py trace <trace_id> — id из букв/цифр/-_.:")
        body = build_search("", sec, MAX_SIZE, f, trace=pos[0])
        resp = es_call(o["index"], "_search", body)
        cc.out(compact_hits(resp, f, f"трейс {pos[0]}, {o['index']}, окно {es_window(sec)}{note}"))
        return
    if not pos:
        sys.exit(f"usage: logs_metrics.py {kind} \"<запрос>\"")
    body = build_search(" ".join(pos), sec, int(o["size"]) if o["size"].isdigit() else 50, f)
    if kind == "count":
        body = {"query": body["query"]}
        resp = es_call(o["index"], "_count", body)
        cc.out(f"count = {resp.get('count', '?')} ({o['index']}, окно {es_window(sec)}{note})")
        return
    resp = es_call(o["index"], "_search", body)
    cc.out(compact_hits(resp, f, f"«{' '.join(pos)}», {o['index']}, окно {es_window(sec)}{note}"))


def cmd_prom(kind, args):
    pos, o = _opts(args, window="6h", step="5m")
    if not pos:
        sys.exit(f"usage: logs_metrics.py {kind} \"<PromQL>\"")
    q = " ".join(pos)
    if kind == "prom":
        cc.out(compact_prom(prom_call("/api/v1/query", {"query": q})))
        return
    sec, _ = parse_window(o["window"], cc.env("LOGS_MAX_WINDOW", "7d"))
    step, _ = parse_window(o["step"])
    end = int(time.time())
    if sec // step > 11000:
        sys.exit("Слишком много точек: увеличь --step или сузь --window")
    cc.out(compact_prom(prom_call("/api/v1/query_range",
                                  {"query": q, "start": end - sec, "end": end, "step": step})))


def cmd_health():
    res = []
    if cc.env("LOGS_ES_URL"):
        try:
            st, _ = cc.http("GET", cc.env("LOGS_ES_URL").rstrip("/") + "/", es_headers(), timeout=15)
            res.append(f"logs: HTTP {st}")
        except cc.NetError as e:
            res.append(f"logs: {e}")
    if cc.env("METRICS_PROM_URL"):
        try:
            st, _ = cc.http("GET", cc.env("METRICS_PROM_URL").rstrip("/") + "/-/healthy", timeout=15)
            res.append(f"metrics: HTTP {st}")
        except cc.NetError as e:
            res.append(f"metrics: {e}")
    print("\n".join(res) or "Ни LOGS_ES_URL, ни METRICS_PROM_URL не заданы")


# --- selftest ---

def selftest():
    t = cc.Selftest(NAME)
    allow = ["acme-app-*", "acme-nginx-*"]
    t.check(check_index("acme-app-*", allow)[0], "разрешённый шаблон")
    t.check(check_index("acme-app-2026.10.28", allow)[0], "конкретный индекс под шаблоном")
    t.check(check_index("acme-app-*,acme-nginx-*", allow)[0], "список разрешённых")
    for bad in ("*", "_all", "acme-*", "billing-*", ".security", "acme-app-*,billing", "-acme-app-x", "../x"):
        t.check(not check_index(bad, allow)[0], f"запрещённый индекс {bad}")
    t.check(not check_index("acme-app-x", [])[0], "пустой allowlist — отказ")
    t.eq(parse_window("90m", "7d"), (5400, False), "окно 90m")
    t.eq(parse_window("30d", "7d"), (604800, True), "окно урезано до 7d")
    t.eq(es_window(5400), "now-90m", "es окно в минутах")
    t.eq(es_window(7200), "now-2h", "es окно в часах")

    f = {"time": "@timestamp", "msg": "message", "level": "log.level", "service": "service", "trace": "trace_id"}
    body = build_search("booking failed", 3600, 9999, f)
    t.eq(body["size"], MAX_SIZE, "size ограничен")
    t.eq(body["query"]["bool"]["filter"][0]["range"]["@timestamp"]["gte"], "now-1h", "фильтр по окну")
    tb = build_search("", 3600, 10, f, trace="abc-123")
    t.eq(tb["query"]["bool"]["filter"][1], {"term": {"trace_id": "abc-123"}}, "трейс по полю")
    t.eq(tb["sort"][0]["@timestamp"]["order"], "asc", "трейс по возрастанию времени")

    secret = "token=" + "s3cr" + "etvalue42"
    es_fixture = {"hits": {"total": {"value": 2}, "hits": [
        {"_source": {"@timestamp": "2026-10-28T09:12:01.120Z", "log.level": "ERROR", "service": "booking-api",
                     "message": "POST /v1/bookings 409 slot taken; client anna@example.com +44 20 7946 0958 " + secret}},
        {"_source": {"@timestamp": "2026-10-28T09:12:01.300Z", "log.level": "INFO", "service": "booking-api",
                     "message": "x" * 1000}}]}}
    txt = cc.mask_text(compact_hits(es_fixture, f, "fixture"))
    t.check("показано 2 из 2" in txt, "шапка с total")
    t.check("anna@example.com" not in txt and "***@***" in txt, "email замаскирован")
    t.check("7946" not in txt and "+***" in txt, "телефон замаскирован")
    t.check("etvalue42" not in txt, "токен в сообщении замаскирован")
    t.check("ERROR booking-api: POST /v1/bookings 409" in txt, "поле с точкой в имени (log.level)")
    t.check("…(+600)" in txt, "длинное сообщение обрезано")
    empty = compact_hits({"hits": {"total": {"value": 0}, "hits": []}}, f, "empty")
    t.check("НЕ значит, что события не было" in empty, "подсказка про пустое окно")

    vec = {"status": "success", "data": {"resultType": "vector", "result": [
        {"metric": {"__name__": "http_requests_total", "code": "500", "service": "booking-api"},
         "value": [1700, "17"]}]}}
    t.eq(compact_prom(vec).splitlines()[1], 'http_requests_total{code="500",service="booking-api"} = 17', "vector")
    mat = {"status": "success", "data": {"resultType": "matrix", "result": [
        {"metric": {"service": "booking-api"}, "values": [[1, "1"], [2, "5"], [3, "2"]]}]}}
    t.check("точек 3, первое 1, последнее 2, min 1, max 5" in compact_prom(mat), "matrix сводкой")
    t.check("Пусто" in compact_prom({"status": "success", "data": {"resultType": "vector", "result": []}}),
            "пустой vector ≠ ноль")
    t.check("bad_data" in compact_prom({"status": "error", "errorType": "bad_data", "error": "parse error"}),
            "ошибка PromQL как есть")

    os.environ["LOGS_ES_API_KEY"] = "QUJD" + "REVG"
    t.eq(es_headers(), {"Authorization": "ApiKey QUJDREVG"}, "заголовок ApiKey")
    t.report()


def main():
    a = sys.argv[1:]
    if a[:1] == ["--selftest"]:
        selftest()
        return
    cc.load_env()
    cc.require_enabled(NAME)
    if not a:
        sys.exit(__doc__)
    cmd, rest = a[0], a[1:]
    if cmd == "health":
        cmd_health()
    elif cmd in ("search", "trace", "count"):
        cmd_logs(cmd, rest)
    elif cmd in ("prom", "prom-range"):
        cmd_prom(cmd, rest)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
