#!/usr/bin/env python3
"""prod_issues — задачи GitLab, у которых лейбл «на проде» ПОСТАВЛЕН в окне [since, until).

Usage:
  prod_issues.py <since YYYY-MM-DD> <until YYYY-MM-DD>   задачи за окно
  prod_issues.py --prev-month                           за прошлый календарный месяц
  prod_issues.py window                                 напечатать окно прошлого месяца
  prod_issues.py --selftest                             без сети, на фикстурах

Дата — момент ПРОСТАНОВКИ лейбла (resource_label_events), а не закрытия задачи:
задачу закрывают когда угодно (до релиза, через месяц после), а «уехало на прод»
отмечается лейблом. Лейбл снимали и ставили снова — берётся последняя простановка.
Вывод: «YYYY-MM-DD #iid [другие лейблы] заголовок», свежие сверху.

Только чтение (GET). GITLAB_URL / GITLAB_TOKEN (read_api) / GITLAB_PROJECT — те же, что у bin/gl.
Лейбл — PROD_DIGEST_LABEL (по умолчанию «На проде»).
Включение: CONNECTOR_PROD_DIGEST_ENABLED=1.
"""
import datetime as dt
import json
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import connector_common as cc  # noqa: E402

NAME = "prod-digest"
_transport = cc.http


def prev_month(today=None):
    today = today or dt.date.today()
    first = today.replace(day=1)
    prev = (first - dt.timedelta(days=1)).replace(day=1)
    return prev.isoformat(), first.isoformat()


def api(path):
    base = cc.env("GITLAB_URL").rstrip("/")
    project = cc.env("GITLAB_PROJECT")
    if not base or not project or not cc.env("GITLAB_TOKEN"):
        sys.exit("GitLab не настроен: нужны GITLAB_URL, GITLAB_TOKEN, GITLAB_PROJECT в .env")
    url = f"{base}/api/v4/projects/{urllib.parse.quote(project, safe='')}{path}"
    status, text = _transport("GET", url, {"PRIVATE-TOKEN": cc.env("GITLAB_TOKEN")}, None, 30)
    if status >= 300:
        raise RuntimeError(f"GitLab HTTP {status} на {path.split('?')[0]} (GitLab доступен — права/проект)")
    return json.loads(text)


def collect(since, until, label):
    issues, page = [], 1
    while page < 50:
        q = urllib.parse.urlencode({"labels": label, "updated_after": f"{since}T00:00:00Z",
                                    "state": "all", "per_page": 100, "page": page})
        batch = api(f"/issues?{q}")
        issues += batch
        if len(batch) < 100:
            break
        page += 1
    out = []
    for i in issues:
        try:
            evs = api(f"/issues/{i['iid']}/resource_label_events?per_page=100")
        except RuntimeError:
            continue
        added = [e["created_at"][:10] for e in evs
                 if e.get("action") == "add" and (e.get("label") or {}).get("name") == label]
        if not added:
            continue
        day = max(added)
        if since <= day < until:
            others = ",".join(x for x in i.get("labels") or [] if x != label)
            out.append((day, i["iid"], others, (i.get("title") or "")[:120]))
    out.sort(reverse=True)
    return out


def fmt(rows):
    return "\n".join(f"{d} #{iid} [{lbl}] {title}" for d, iid, lbl, title in rows)


def selftest():
    t = cc.Selftest(NAME)
    t.eq(prev_month(dt.date(2026, 10, 28)), ("2026-09-01", "2026-10-01"), "окно прошлого месяца")
    t.eq(prev_month(dt.date(2026, 1, 5)), ("2025-12-01", "2026-01-01"), "окно через Новый год")
    os.environ.update({"GITLAB_URL": "https://gitlab.example.com", "GITLAB_TOKEN": "x", "GITLAB_PROJECT": "acme/backlog"})
    L = "На проде"
    issues = [
        {"iid": 11, "title": "Онлайн-оплата при записи", "labels": [L, "feature"]},
        {"iid": 12, "title": "Закрыта в сентябре, на прод в октябре", "labels": [L]},
        {"iid": 13, "title": "Сняли и поставили снова", "labels": [L, "bug"]},
        {"iid": 14, "title": "Лейбл ставили в августе", "labels": [L]},
    ]
    events = {
        11: [{"action": "add", "label": {"name": L}, "created_at": "2026-09-15T10:00:00Z"}],
        12: [{"action": "add", "label": {"name": L}, "created_at": "2026-10-02T09:00:00Z"}],
        13: [{"action": "add", "label": {"name": L}, "created_at": "2026-08-30T09:00:00Z"},
             {"action": "remove", "label": {"name": L}, "created_at": "2026-09-01T09:00:00Z"},
             {"action": "add", "label": {"name": L}, "created_at": "2026-09-20T09:00:00Z"}],
        14: [{"action": "add", "label": {"name": L}, "created_at": "2026-08-10T09:00:00Z"},
             {"action": "add", "label": {"name": "feature"}, "created_at": "2026-09-10T09:00:00Z"}],
    }
    seen = []

    def fake(method, url, headers, body, timeout):
        seen.append(url)
        t.check(headers.get("PRIVATE-TOKEN") == "x" and method == "GET", "только GET с токеном в заголовке")
        if "/resource_label_events" in url:
            iid = int(url.split("/issues/")[1].split("/")[0])
            return 200, json.dumps(events[iid])
        return 200, json.dumps(issues)

    global _transport
    _transport = fake
    rows = collect("2026-09-01", "2026-10-01", L)
    t.eq([r[1] for r in rows], [13, 11], "в окно попали по дате ПРОСТАНОВКИ лейбла")
    t.eq(rows[0][0], "2026-09-20", "повторная простановка — последняя дата")
    t.check("[feature] Онлайн-оплата" in fmt(rows), "другие лейблы без «на проде»")
    t.check(any("acme%2Fbacklog" in u for u in seen), "путь проекта экранирован")
    t.report()


def main():
    a = sys.argv[1:]
    if a[:1] == ["--selftest"]:
        selftest()
        return
    if a[:1] == ["window"]:
        print(*prev_month())
        return
    cc.load_env()
    cc.require_enabled(NAME)
    if a[:1] == ["--prev-month"]:
        since, until = prev_month()
    elif len(a) == 2:
        since, until = a
        for d in a:
            try:
                dt.date.fromisoformat(d)
            except ValueError:
                sys.exit(f"дата {d!r}: нужен формат YYYY-MM-DD")
    else:
        sys.exit(__doc__)
    label = cc.env("PROD_DIGEST_LABEL", "На проде")
    try:
        rows = collect(since, until, label)
    except cc.NetError as e:
        sys.exit(str(e))
    except RuntimeError as e:
        sys.exit(str(e))
    print(f"# лейбл «{label}» поставлен в [{since}, {until}): задач {len(rows)}")
    if rows:
        print(fmt(rows))


if __name__ == "__main__":
    main()
