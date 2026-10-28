#!/usr/bin/env python3
"""stale_facts — какие факты базы знаний могли устареть: код по их путям менялся
после даты среза, записанной в самом факте.

Формат факта, который ловится (так их пишет learn-questions):
  - **Заголовок факта** … код backend/src/booking/Slot.php:12-20, срез 2026-08-28
где `backend` — имя клона из config/code-repos.txt. Без «срез ДАТА» берётся самая
поздняя дата ГГГГ-ММ-ДД в строке. Строки без ссылки на код пропускаются.

Без такой проверки факты тихо расходятся с кодом: доки «как обещано» живут
месяцами после того, как код поменялся.
Запуск: team-agent-stalefacts.timer (после codesync). Пишет memory/stale_facts.md;
с --notify шлёт владельцу сводку.
Usage: stale_facts.py [--notify]
"""
import glob
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
V = os.path.abspath(os.environ.get("AGENT_HOME") or os.path.join(HERE, ".."))
K = os.path.join(V, "knowledge")
CODE = os.path.join(V, "code")
OUT = os.path.join(V, "memory", "stale_facts.md")
SLICE = re.compile(r"(?:срез|slice|as of)\s+(\d{4}-\d{2}-\d{2})", re.I)
DATE = re.compile(r"20\d\d-\d\d-\d\d")


def load_env():
    vals = {}
    p = os.path.join(V, ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip("\"'")
    return vals


def repos():
    if not os.path.isdir(CODE):
        return []
    return sorted(d for d in os.listdir(CODE) if os.path.isdir(os.path.join(CODE, d, ".git")))


def git_log(repo, path, since):
    r = subprocess.run(["git", "-C", os.path.join(CODE, repo), "log", f"--since={since}T23:59:59",
                        "--format=%h %cs %s", "--", path], capture_output=True, text=True, timeout=60)
    return [line for line in r.stdout.splitlines() if line.strip()]


def scan():
    rs = repos()
    if not rs:
        return 0, []
    ref = re.compile(r"(?<![\w/.-])(?P<repo>" + "|".join(map(re.escape, rs)) +
                     r")/(?P<path>[A-Za-z0-9_][A-Za-z0-9_/.\-]*?)(?::\d+(?:-\d+)?)?(?=[,;)\s`]|$)")
    files = sorted(glob.glob(os.path.join(K, "**", "*.md"), recursive=True))
    checked, stale = 0, []
    for f in files:
        if "/.git/" in f:
            continue
        for line in open(f, encoding="utf-8", errors="replace"):
            refs = list(ref.finditer(line))
            if not refs:
                continue
            m = SLICE.search(line)
            dates = DATE.findall(line)
            since = m.group(1) if m else (max(dates) if dates else None)
            if not since:
                continue
            checked += 1
            title = re.match(r"\s*- \*\*(.+?)\*\*", line)
            title = title.group(1) if title else line.strip()[:80]
            hits = []
            for r in refs:
                path = r.group("path").rstrip(".")
                commits = git_log(r.group("repo"), path, since)
                if commits:
                    hits.append((f"{r.group('repo')}/{path}", commits))
            if hits:
                stale.append((os.path.relpath(f, K), title, since, hits))
    return checked, stale


def main():
    checked, stale = scan()
    lines = [f"# Факты на перепроверку (код менялся после среза) — проверено {checked}, под подозрением {len(stale)}", ""]
    for rel, title, since, hits in stale:
        lines.append(f"- **{title}** ({rel}, срез {since})")
        for p, commits in hits:
            lines.append(f"  - {p}: {len(commits)} коммит(ов) после среза, последний: {commits[0]}")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines[:40]))
    if "--notify" in sys.argv and stale:
        e = load_env()
        tok, owner = e.get("TG_BOT_TOKEN", ""), e.get("OWNER_ID", "")
        if not tok or not owner:
            sys.exit("нет TG_BOT_TOKEN/OWNER_ID в .env — уведомление не отправлено")
        top = "\n".join(f"▪️ {t[:90]} — {rel} (срез {s}; {sum(len(c) for _, c in h)} изм.)"
                        for rel, t, s, h in stale[:15])
        text = (f"🔎 Факты базы, код по которым менялся после записи: {len(stale)} из {checked} проверяемых.\n"
                f"{top}\nПолный список: memory/stale_facts.md. Перепроверь или отдай в learn-questions.")
        data = urllib.parse.urlencode({"chat_id": owner, "text": text[:3900]}).encode()
        with urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data=data, timeout=20) as r:
            if not json.load(r).get("ok"):
                sys.exit("Telegram не принял сообщение")


if __name__ == "__main__":
    main()
