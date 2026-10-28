#!/usr/bin/env python3
"""github_pr — открыть PR из ветки конвейера (brain-sync, learn-questions, ticket-learn) на GitHub.

Usage:
  github_pr.py open --repo-dir <рабочая копия> --branch <ветка> --base <main> --title <заголовок> [--body-file <файл>]
      печатает URL PR (уже открытого для этой ветки — тоже); rc=1 — не вышло (обёртка даст compare-ссылку)
  github_pr.py --selftest     без сети, на фикстурах

Как создаёт:
  1. GITHUB_TOKEN есть → REST API: ищет открытый PR head=<owner>:<ветка>, нет — создаёт;
  2. токена нет, но есть `gh` (gh auth login от имени агента) → gh pr create;
  3. иначе rc=1, и bin/lib/duty.sh шлёт владельцу compare-ссылку, как без коннектора.
Репо: GITHUB_REPO=<owner>/<repo>, иначе — из origin рабочей копии (в т.ч. ssh-алиас
вида git@github-knowledge:<owner>/<repo>.git). GitHub Enterprise — GITHUB_API_URL.
Права токена: fine-grained PAT только на репо знаний, «Pull requests: Read and write»
(push веток делает deploy key, Contents не нужен).
Включение: CONNECTOR_GITHUB_ENABLED=1. Мёржит по-прежнему человек.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import connector_common as cc  # noqa: E402

NAME = "github"
_transport = cc.http
_run = subprocess.run


def parse_remote(url):
    """→ (host, owner/repo) или (None, None)."""
    url = (url or "").strip()
    m = (re.fullmatch(r"git@([^:]+):([^/]+/[^/]+?)(?:\.git)?/?", url)
         or re.fullmatch(r"ssh://git@([^/:]+)(?::\d+)?/([^/]+/[^/]+?)(?:\.git)?/?", url)
         or re.fullmatch(r"https?://(?:[^@/]+@)?([^/]+)/([^/]+/[^/]+?)(?:\.git)?/?", url))
    return (m.group(1), m.group(2)) if m else (None, None)


def resolve_repo(repo_dir):
    if cc.env("GITHUB_REPO"):
        return cc.env("GITHUB_REPO")
    r = _run(["git", "-C", repo_dir, "remote", "get-url", "origin"], capture_output=True, text=True, timeout=10)
    host, slug = parse_remote(r.stdout)
    if not slug or "github" not in (host or "") and not cc.env("GITHUB_API_URL"):
        return None
    return slug


def gh_api(method, path, body=None):
    base = cc.env("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    st, text = _transport(method, base + path, {"Authorization": "Bearer " + cc.env("GITHUB_TOKEN"),
                                                "Accept": "application/vnd.github+json",
                                                "X-GitHub-Api-Version": "2022-11-28"}, body, 30)
    return st, cc.parse_json(text)


def open_pr(repo, branch, base, title, body):
    if cc.env("GITHUB_TOKEN"):
        owner = repo.split("/")[0]
        q = urllib.parse.urlencode({"head": f"{owner}:{branch}", "state": "open"})
        st, data = gh_api("GET", f"/repos/{repo}/pulls?{q}")
        if st == 200 and isinstance(data, list) and data:
            return data[0].get("html_url")
        st, data = gh_api("POST", f"/repos/{repo}/pulls",
                          {"title": title[:250], "head": branch, "base": base, "body": body[:60000],
                           "maintainer_can_modify": False})
        if st == 201 and isinstance(data, dict):
            return data.get("html_url")
        msg = (data or {}).get("message") if isinstance(data, dict) else ""
        print(f"GitHub API: HTTP {st} {cc.mask_text(str(msg))[:200]}", file=sys.stderr)
        return None
    if shutil.which("gh"):
        r = _run(["gh", "pr", "create", "-R", repo, "--head", branch, "--base", base,
                  "--title", title[:250], "--body", body[:60000]], capture_output=True, text=True, timeout=60)
        m = re.search(r"https://\S+/pull/\d+", r.stdout + r.stderr)   # «already exists» тоже печатает URL
        if m:
            return m.group(0)
        print(f"gh pr create: rc={r.returncode} {r.stderr[-200:]}", file=sys.stderr)
        return None
    print("нет ни GITHUB_TOKEN, ни gh — PR не создан", file=sys.stderr)
    return None


def selftest():
    t = cc.Selftest(NAME)
    t.eq(parse_remote("git@github.com:acme/team-os-knowledge.git"), ("github.com", "acme/team-os-knowledge"), "ssh")
    t.eq(parse_remote("git@github-knowledge:acme/kb.git"), ("github-knowledge", "acme/kb"), "ssh-алиас из INSTALL")
    t.eq(parse_remote("ssh://git@github.com:22/acme/kb.git"), ("github.com", "acme/kb"), "ssh://")
    t.eq(parse_remote("https://x-token@github.com/acme/kb"), ("github.com", "acme/kb"), "https с кредами")
    t.eq(parse_remote("/srv/git/kb"), (None, None), "локальный путь")

    class R:
        def __init__(self, out):
            self.stdout, self.stderr, self.returncode = out, "", 0
    global _run, _transport
    _run = lambda argv, **kw: R("git@gitlab.example.com:acme/kb.git\n")  # noqa: E731
    os.environ.pop("GITHUB_REPO", None)
    os.environ.pop("GITHUB_API_URL", None)
    t.eq(resolve_repo("/tmp"), None, "не-GitHub ремоут → не наш случай")
    _run = lambda argv, **kw: R("git@github-knowledge:acme/kb.git\n")  # noqa: E731
    t.eq(resolve_repo("/tmp"), "acme/kb", "репо из origin")

    os.environ["GITHUB_TOKEN"] = "x"
    calls = []

    def api_new(method, url, headers, body, timeout):
        calls.append((method, url, body))
        t.check(headers["Authorization"] == "Bearer x", "токен в заголовке, не в URL")
        if method == "GET":
            return 200, "[]"
        return 201, json.dumps({"html_url": "https://github.com/acme/kb/pull/7"})

    _transport = api_new
    t.eq(open_pr("acme/kb", "brain-sync/2026-10-28", "main", "brain-sync", "тело"),
         "https://github.com/acme/kb/pull/7", "создан новый PR")
    t.check("head=acme%3Abrain-sync%2F2026-10-28" in calls[0][1], "поиск открытого PR по head")
    t.eq(calls[1][2]["base"], "main", "base в теле")

    _transport = lambda m, u, h, b, tm: (200, json.dumps([{"html_url": "https://github.com/acme/kb/pull/5"}]))  # noqa: E731
    t.eq(open_pr("acme/kb", "b", "main", "t", ""), "https://github.com/acme/kb/pull/5", "уже открытый PR")
    _transport = lambda m, u, h, b, tm: (200, "[]") if m == "GET" else (422, '{"message":"Validation Failed"}')  # noqa: E731
    t.eq(open_pr("acme/kb", "b", "main", "t", ""), None, "ошибка API → None (обёртка даст compare)")
    t.report()


def main():
    a = sys.argv[1:]
    if a[:1] == ["--selftest"]:
        selftest()
        return
    cc.load_env()
    cc.require_enabled(NAME)
    if a[:1] != ["open"]:
        sys.exit(__doc__)

    def opt(k, d=""):
        return a[a.index(k) + 1] if k in a and a.index(k) + 1 < len(a) else d
    repo = resolve_repo(opt("--repo-dir", "."))
    if not repo:
        sys.exit("репо не на GitHub (или задайте GITHUB_REPO=<owner>/<repo>)")
    body = ""
    if opt("--body-file") and os.path.exists(opt("--body-file")):
        with open(opt("--body-file"), encoding="utf-8") as f:
            body = f.read()
    try:
        url = open_pr(repo, opt("--branch"), opt("--base", "main"), opt("--title", opt("--branch")), body)
    except cc.NetError as e:
        sys.exit(str(e))
    if not url:
        sys.exit(1)
    print(url)


if __name__ == "__main__":
    main()
