#!/usr/bin/env python3
"""mcp_check — проверка конфига MCP-серверов и белого списка инструментов до того, как их увидит модель.

Usage:
  mcp_check.py check [--config <mcp.json>]   конфиг из MCP_CONFIG (или --config) + MCP_ALLOWED_TOOLS
  mcp_check.py args                          какие аргументы бот добавит к `claude -p`
  mcp_check.py --selftest                    без сети: пример конфига и фикстуры

Как подключается: бот (только полный режим команды, не публичный и не duty) добавляет
  --mcp-config <MCP_CONFIG> --strict-mcp-config --allowedTools <MCP_ALLOWED_TOOLS…>
и, если задано, MCP_DISALLOWED_TOOLS в --disallowedTools. В headless-режиме инструмент
вне --allowedTools модель вызвать не может — белый список и есть граница.
Инструменты с «пишущими» именами (create/update/delete/send/…) отклоняются, пока не
выставлен MCP_ALLOW_WRITE_TOOLS=1: агент читает чужие тексты, а prompt injection
в сообщении или задаче превращает write-инструмент в действие от вашего имени.
"""
import json
import os
import re
import stat
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import connector_common as cc  # noqa: E402

NAME = "mcp"
HERE = os.path.dirname(os.path.abspath(__file__))
NAME_RE = re.compile(r"[A-Za-z0-9_-]+")
WRITE_WORDS = {"create", "update", "delete", "remove", "write", "send", "post", "put", "patch", "merge",
               "close", "archive", "move", "upload", "comment", "edit", "set", "add", "insert", "publish",
               "invite", "share", "transfer", "run", "exec", "execute", "append", "replace", "trash"}
SECRETISH = re.compile(r"(?i)(token|secret|key|password|authorization)")


def is_write(tool):
    """По словам имени: get_dataset — чтение, create_issue / addComment — запись."""
    words = re.split(r"[_\-.]+|(?<=[a-z0-9])(?=[A-Z])", tool)
    return any(w.lower() in WRITE_WORDS for w in words if w)


def parse_tool(t):
    """mcp__<сервер>__<инструмент> → (сервер, инструмент); mcp__<сервер> → (сервер, None); иначе None."""
    if not t.startswith("mcp__"):
        return None
    srv, sep, tool = t[5:].partition("__")
    if not NAME_RE.fullmatch(srv or "-x-") or not srv or (sep and not NAME_RE.fullmatch(tool or "")):
        return None
    return srv, (tool if sep else None)


def config_path():
    p = cc.env("MCP_CONFIG")
    if p and not os.path.isabs(p):
        p = cc.path(p)
    return p


def check_config(cfg):
    errs, warns = [], []
    servers = cfg.get("mcpServers") if isinstance(cfg, dict) else None
    if not isinstance(servers, dict) or not servers:
        return ["нет объекта mcpServers"], warns, {}
    for name, s in servers.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            errs.append(f"{name}: имя сервера — только буквы/цифры/_/-")
        kind = s.get("type", "stdio")
        if kind == "stdio":
            if not s.get("command"):
                errs.append(f"{name}: stdio без command")
        elif kind in ("http", "sse"):
            url = s.get("url", "")
            if not url.startswith("https://") and not url.startswith("http://127.0.0.1") \
                    and not url.startswith("http://localhost"):
                errs.append(f"{name}: url должен быть https:// (или localhost)")
        else:
            errs.append(f"{name}: неизвестный type {kind!r}")
        for where in ("env", "headers"):
            for k, v in (s.get(where) or {}).items():
                if SECRETISH.search(k) and isinstance(v, str) and v and not v.startswith("<") \
                        and not v.startswith("${") and "<" not in v:
                    warns.append(f"{name}.{where}.{k}: похоже на живой секрет — держите файл вне git, chmod 600")
    return errs, warns, servers


def check_tools(tools, servers, allow_write):
    ok, errs, warns = [], [], []
    for t in tools:
        parsed = parse_tool(t)
        if not parsed:
            errs.append(f"{t}: формат mcp__<сервер>__<инструмент>")
            continue
        srv, tool = parsed
        if servers and srv not in servers:
            errs.append(f"{t}: сервера {srv} нет в конфиге")
            continue
        if tool is None:
            warns.append(f"{t}: разрешены ВСЕ инструменты сервера — лучше перечислить по одному")
            if not allow_write:
                errs.append(f"{t}: весь сервер без MCP_ALLOW_WRITE_TOOLS=1 нельзя (среди инструментов могут быть пишущие)")
                continue
        elif is_write(tool) and not allow_write:
            errs.append(f"{t}: похоже на пишущий инструмент — нужен явный MCP_ALLOW_WRITE_TOOLS=1")
            continue
        ok.append(t)
    return ok, errs, warns


def bot_args(cfg_path, tools, deny):
    if not cfg_path or not tools:
        return []
    return ["--mcp-config", cfg_path, "--strict-mcp-config", "--allowedTools", *tools]


def selftest():
    t = cc.Selftest(NAME)
    with open(os.path.join(HERE, "mcp.json.example"), encoding="utf-8") as f:
        example = json.load(f)
    errs, warns, servers = check_config(example)
    t.eq(errs, [], "пример конфига валиден")
    t.eq(warns, [], "в примере нет живых секретов")
    t.eq(sorted(servers), ["docs", "tracker"], "два сервера: stdio и http")
    t.eq({s.get("type") for s in servers.values()}, {"stdio", "http"}, "типы серверов")

    ok, e, w = check_tools(["mcp__tracker__get_issue", "mcp__docs__search"], servers, False)
    t.eq((ok, e), (["mcp__tracker__get_issue", "mcp__docs__search"], []), "read-инструменты проходят")
    ok, e, w = check_tools(["mcp__tracker__create_issue", "mcp__tracker__addComment"], servers, False)
    t.eq(ok, [], "пишущие без явного разрешения отклонены")
    t.eq((is_write("list_datasets"), is_write("get_asset"), is_write("sendMessage")), (False, False, True),
         "пишущие — по словам имени, не по подстроке")
    ok, e, w = check_tools(["mcp__tracker__create_issue"], servers, True)
    t.eq(ok, ["mcp__tracker__create_issue"], "пишущий с MCP_ALLOW_WRITE_TOOLS=1")
    ok, e, w = check_tools(["mcp__tracker"], servers, False)
    t.check(not ok and e, "весь сервер без разрешения — отказ")
    ok, e, w = check_tools(["mcp__ghost__read", "Bash(rm:*)", "mcp__docs__"], servers, False)
    t.eq(len(e), 3, "чужой сервер / не MCP / битый формат")

    live = {"mcpServers": {"x": {"type": "http", "url": "http://mcp.example.com",
                                 "headers": {"Authorization": "Bearer " + "abc" + "def123456"}}}}
    errs, warns, _ = check_config(live)
    t.check(any("https" in x for x in errs), "http без TLS — ошибка")
    t.check(any("живой секрет" in x for x in warns), "живой токен в конфиге — предупреждение")
    t.eq(check_config({"servers": {}})[0], ["нет объекта mcpServers"], "битый конфиг")

    t.eq(bot_args("/a/mcp.json", ["mcp__docs__search"], []),
         ["--mcp-config", "/a/mcp.json", "--strict-mcp-config", "--allowedTools", "mcp__docs__search"], "аргументы бота")
    t.eq(bot_args("/a/mcp.json", [], []), [], "без разрешённых инструментов MCP не подключается")
    t.report()


def main():
    a = sys.argv[1:]
    if a[:1] == ["--selftest"]:
        selftest()
        return
    cc.load_env()
    p = a[a.index("--config") + 1] if "--config" in a else config_path()
    if not p:
        sys.exit("MCP_CONFIG не задан в .env — MCP выключен")
    if not os.path.exists(p):
        sys.exit(f"нет файла {p}")
    with open(p, encoding="utf-8") as f:
        cfg = json.load(f)
    errs, warns, servers = check_config(cfg)
    ok, terrs, twarns = check_tools(cc.env_list("MCP_ALLOWED_TOOLS"), servers, cc.env("MCP_ALLOW_WRITE_TOOLS") == "1")
    mode = stat.S_IMODE(os.stat(p).st_mode)
    if mode & 0o077:
        warns.append(f"{p}: права {oct(mode)} — поставьте chmod 600 (в файле токены серверов)")
    if a[:1] == ["args"]:
        print(" ".join(bot_args(p, ok, cc.env_list("MCP_DISALLOWED_TOOLS"))) or "(MCP не подключится: нет разрешённых инструментов)")
        return
    for x in errs + terrs:
        print("✗ " + x)
    for x in warns + twarns:
        print("! " + x)
    print(f"серверы: {', '.join(servers) or '—'}; разрешено инструментов: {len(ok)}: {', '.join(ok) or '—'}")
    sys.exit(1 if errs or terrs or not ok else 0)


if __name__ == "__main__":
    main()
