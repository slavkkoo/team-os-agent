#!/usr/bin/env python3
"""
Командный AI-агент («AI chief of staff») — Telegram-адаптер. Только stdlib.

DM: открыт всем; режим по allowlist TEAM_IDS: команда — полный (база знаний,
    код, GitLab), остальные — публичный (только база знаний, без shell).
Группы: отвечает на @упоминание или reply на своё сообщение.
Ответ: изолированный `claude -p` на каждый вопрос; знания — read-only клон
    knowledge/ (git pull раз в 30 мин), промпт — из prompts/*.md.
Поправки (M4): «ошибаешься…» реплаем на ответ → staged → владелец делает
    promote/reject → approved-поправки подмешиваются в промпт.
Лог: logs/interactions.jsonl (append-only).

Вся конфигурация — из .env (см. .env.example). Имена агента, команды и
продукта подставляются в промпты.
"""
import html
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

AGENT_HOME = os.path.abspath(os.path.expanduser(
    os.environ.get("AGENT_HOME") or "~/.local/share/team-agent"))
REPO = os.path.join(AGENT_HOME, "knowledge")
CODE_DIR = os.path.join(AGENT_HOME, "code")
PROMPT_DIR = os.path.join(AGENT_HOME, "prompts")
CONNECTORS_DIR = os.path.join(AGENT_HOME, "connectors")
DM_DIR = os.path.join(AGENT_HOME, "memory", "dm")
LEARN_DIR = os.path.join(AGENT_HOME, "memory", "learnings")
DIGESTS_DIR = os.path.join(AGENT_HOME, "memory", "digests")
UPLOAD_DIR = os.path.join(AGENT_HOME, "uploads")
LOG = os.path.join(AGENT_HOME, "logs", "interactions.jsonl")
RATE_FILE = os.path.join(AGENT_HOME, "memory", "ratelimit.json")
STATE_FILE = os.path.join(AGENT_HOME, "memory", "updates_state.json")

SEEN_MAX = 300             # дедуп последних обработанных сообщений
RESEED_TURNS = 6           # сколько реплик DM-истории подмешивать в follow-up
IDLE_WINDOW_S = 20 * 60    # история DM старше этого окна не подмешивается
JSONL_KEEP = 500           # сколько последних записей держим в буферах диалогов
KNOWLEDGE_PULL_S = 1800    # git pull базы знаний
_DOC_OK = ("image/", "application/pdf", "text/", "application/json", "application/xml")
_DOC_MAX = 10 * 1024 * 1024

_rate_lock = threading.Lock()
_log_lock = threading.Lock()
_followups = {}            # (chat_id, uid) -> expires_at
_fu_lock = threading.Lock()


# --- конфигурация ---

def env(key, default=""):
    v = os.environ.get(key, "").strip()
    return v if v else default


def env_int(key, default):
    try:
        return int(env(key, str(default)))
    except ValueError:
        return default


def load_env(path=None):
    """Читает AGENT_HOME/.env (KEY=VALUE, без source — значения не исполняются).
    Уже заданные переменные окружения не перекрываются."""
    path = path or os.path.join(AGENT_HOME, ".env")
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


def agent_name():
    return env("AGENT_NAME", "Ассистент")


def team_name():
    return env("TEAM_NAME", "команды")


def product_name():
    return env("PRODUCT_NAME", "продукт")


def owner_name():
    return env("OWNER_NAME")


def owner_ref():
    """« (руководитель продукта)» — уточнение после слова «владелец» в любом падеже."""
    return f" ({owner_name()})" if owner_name() else ""


def owner_id():
    return env("OWNER_ID")


def bot_username():
    return env("TG_BOT_USERNAME").lstrip("@")


def model_answer():
    return env("MODEL_ANSWER", "claude-opus-5-5")


def team_ids():
    return {s.strip() for s in env("TEAM_IDS").split(",") if s.strip()}


def allowed_groups():
    return {s.strip() for s in env("ALLOWED_GROUP_IDS").split(",") if s.strip()}


# --- Telegram ---

def api(method, **params):
    tok = os.environ["TG_BOT_TOKEN"]
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=70) as r:
        return json.load(r)


def fetch_attachment(msg):
    """Фото/документ из сообщения → локальный файл для Read. Без этого фото без
    подписи молча отбрасывались, а с подписью бот не видел картинку."""
    fid, ext = None, ".bin"
    if msg.get("photo"):
        fid, ext = msg["photo"][-1]["file_id"], ".jpg"          # самое крупное превью
    elif msg.get("document"):
        d = msg["document"]
        if (d.get("file_size") or 0) <= _DOC_MAX and (d.get("mime_type") or "").startswith(_DOC_OK):
            fid = d["file_id"]
            ext = os.path.splitext(d.get("file_name") or "")[1][:8] or ".bin"
    if not fid:
        return None
    try:
        info = api("getFile", file_id=fid)
        fpath = info["result"]["file_path"]
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        now = time.time()
        for f in os.listdir(UPLOAD_DIR):            # храним неделю
            fp = os.path.join(UPLOAD_DIR, f)
            if now - os.path.getmtime(fp) > 7 * 86400:
                os.remove(fp)
        local = os.path.join(UPLOAD_DIR, f"{msg['chat']['id']}_{msg['message_id']}{ext}")
        url = f"https://api.telegram.org/file/bot{os.environ['TG_BOT_TOKEN']}/{fpath}"
        with urllib.request.urlopen(url, timeout=60) as r, open(local, "wb") as out:
            out.write(r.read())
        return local
    except Exception as e:
        print(f"attachment error: {e}", flush=True)
        return None


def md_to_tg_html(text):
    """Ограниченный markdown модели -> Telegram HTML.
    Код прячем до экранирования, чтобы <?php и & в примерах не ломали разметку."""
    stash = []

    def hide(m, tag):
        stash.append((tag, m))
        return f"\x00{len(stash) - 1}\x00"

    text = re.sub(r"```[a-zA-Z0-9]*\n?(.*?)```", lambda m: hide(m.group(1), "pre"),
                  text, flags=re.S)
    text = re.sub(r"`([^`\n]+)`", lambda m: hide(m.group(1), "code"), text)
    text = html.escape(text, quote=False)
    text = re.sub(r"(?m)^#{1,6}\s+(.+)$", r"<b>\1</b>", text)          # заголовки -> жирная строка
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.S)    # **жирный**
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', text)

    def unhide(m):
        tag, s = stash[int(m.group(1))]
        esc = html.escape(s, quote=False)
        return f"<pre>{esc}</pre>" if tag == "pre" else f"<code>{esc}</code>"

    return re.sub("\x00(\\d+)\x00", unhide, text)


def smart_chunks(text, limit=3900):
    """Резать по границе строки, не посреди слова/тега."""
    out = []
    while len(text) > limit:
        cut = text.rfind("\n", limit // 2, limit)
        if cut == -1:
            cut = limit
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        out.append(text)
    return out


def send(chat_id, text, reply_to=None):
    for chunk in smart_chunks(text):
        params = {"chat_id": chat_id, "text": md_to_tg_html(chunk),
                  "parse_mode": "HTML", "disable_web_page_preview": "true"}
        if reply_to:
            params["reply_to_message_id"] = reply_to
            reply_to = None
        try:
            api("sendMessage", **params)
        except Exception:
            # битые сущности HTML -> фолбэк: тот же кусок плоским текстом
            params["text"] = chunk
            params.pop("parse_mode", None)
            api("sendMessage", **params)


def log_interaction(**rec):
    rec["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with _log_lock, open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# --- rate limiting: потолок, а не просто наблюдение ---

def rate_ok(uid, team=True):
    with _rate_lock:
        return _rate_ok(uid, team)


def _rate_ok(uid, team):
    today = time.strftime("%Y-%m-%d")
    data = {}
    if os.path.exists(RATE_FILE):
        try:
            data = json.load(open(RATE_FILE))
        except ValueError:
            data = {}
    if data.get("day") != today:
        data = {"day": today, "users": {}, "total": 0}
    if data["total"] >= env_int("GLOBAL_DAILY_LIMIT", 200):
        return False, data, "общий дневной лимит"
    per_user = env_int("PUBLIC_USER_DAILY_LIMIT", 20)
    if not team and per_user and data["users"].get(uid, 0) >= per_user:
        return False, data, "ваш дневной лимит"
    return True, data, ""


def rate_bump(data, uid):
    with _rate_lock:
        data["users"][uid] = data["users"].get(uid, 0) + 1
        data["total"] += 1
        os.makedirs(os.path.dirname(RATE_FILE), exist_ok=True)
        json.dump(data, open(RATE_FILE, "w"))


# --- память DM ---

def _trim_jsonl(path, keep=JSONL_KEEP):
    """Обрезает буфер до последних keep строк.

    Буферы диалогов — это plaintext-переписка на диске. Читаем из них максимум
    последние RESEED_TURNS записей, поэтому хранить всё с начала времён — значит
    копить персональные данные без причины. Переписываем с запасом в 20%.
    """
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= keep * 1.2:
            return
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(lines[-keep:])
        os.replace(tmp, path)
    except Exception:
        pass    # ротация не должна ронять обработку сообщения


def _append_jsonl(path, obj, keep=JSONL_KEEP):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    if keep:
        _trim_jsonl(path, keep)


def dm_recent(chat_id):
    path = os.path.join(DM_DIR, f"{chat_id}.jsonl")
    if not os.path.exists(path):
        return []
    now, turns = time.time(), []
    for line in open(path, encoding="utf-8"):
        try:
            t = json.loads(line)
        except ValueError:
            continue
        if now - t.get("ts", 0) <= IDLE_WINDOW_S:
            turns.append(t)
    return turns[-RESEED_TURNS:]


def followup_set(chat_id, uid):
    with _fu_lock:
        _followups[(chat_id, uid)] = time.time() + env_int("GROUP_FOLLOWUP_S", 300)


def followup_active(chat_id, uid):
    with _fu_lock:
        exp = _followups.get((chat_id, uid), 0)
        if exp < time.time():
            _followups.pop((chat_id, uid), None)
            return False
        return True


# --- код-репо (config/code-repos.txt) ---

def code_repos():
    """Строки `имя url ветка [ключевые,слова]`. Без ключевых слов репо подключается
    к каждому вопросу команды; с ними — только если в вопросе есть одно из слов
    (так большой фронтенд не попадает в контекст вопросов про бэкенд)."""
    path = env("CODE_REPOS", os.path.join(AGENT_HOME, "config", "code-repos.txt"))
    out = []
    if not os.path.exists(path):
        return out
    for line in open(path, encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 3 or "/" in parts[0] or parts[0].startswith("."):
            continue
        kws = [k.strip().lower() for k in (parts[3] if len(parts) > 3 else "").split(",") if k.strip()]
        out.append({"name": parts[0], "url": parts[1], "branch": parts[2], "keywords": kws})
    return out


def code_dirs(question, team=True):
    """Какие код-репо монтировать (--add-dir) под этот вопрос."""
    if not team:
        return []
    q = (question or "").lower()
    dirs = []
    for r in code_repos():
        path = os.path.join(CODE_DIR, r["name"])
        if not os.path.isdir(path):
            continue
        if not r["keywords"] or any(k in q for k in r["keywords"]):
            dirs.append(path)
    return dirs


_code_date = {"val": "", "at": 0.0}


def code_slice_date():
    """Дата последнего коммита в каждом клоне code/ — модель цитирует её, а не выдумывает."""
    if time.time() - _code_date["at"] > 3600:
        parts = []
        for r in code_repos():
            path = os.path.join(CODE_DIR, r["name"])
            try:
                out = subprocess.run(
                    ["git", "-C", path, "log", "-1", "--format=%cd", "--date=format:%d.%m.%Y"],
                    capture_output=True, text=True, timeout=10)
                parts.append(f"{r['name']} — {out.stdout.strip() or 'неизвестна'}")
            except Exception:
                parts.append(f"{r['name']} — неизвестна")
        _code_date["val"] = "; ".join(parts) or "неизвестна"
        _code_date["at"] = time.time()
    return _code_date["val"]


# --- M4: поправка тиммейта → staged → промоушен владельца → меняет ответы ---
LEARN_TRIGGERS = ("поправка", "не так", "неверно", "исправление", "👎", "wrong:",
                  "ошибаешься", "ты ошибся", "ты ошибаешься", "ты не прав", "на самом деле",
                  "это не так", "не верно")


def _is_correction(text):
    low = text.lower()
    if bot_username():
        low = low.replace(f"@{bot_username().lower()}", "")
    return low.strip().startswith(LEARN_TRIGGERS)


def learn_capture(who, surface, correction, bot_answer):
    """Поправка → staged-файл. Возвращает (slug, ok) — ok только после read-back."""
    os.makedirs(os.path.join(LEARN_DIR, "staged"), exist_ok=True)
    words = re.findall(r"[\w]+", correction.lower())
    tail = "-".join(words[1:5])[:40] or "bez-temy"
    slug = time.strftime("%Y-%m-%d") + "-" + tail
    path = os.path.join(LEARN_DIR, "staged", slug + ".md")
    n = 2
    while os.path.exists(path):
        path = os.path.join(LEARN_DIR, "staged", f"{slug}-{n}.md")
        n += 1
    slug = os.path.basename(path)[:-3]
    body = (f"---\nfrom: {who}\nsurface: {surface}\nts: "
            f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')}\nstatus: staged\n---\n\n"
            f"## Поправка\n{correction.strip()}\n\n"
            f"## Ответ бота, который поправили\n{(bot_answer or '(нет цитаты)').strip()[:800]}\n")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        with open(path, encoding="utf-8") as f:  # read-back: запись проверена чтением
            ok = correction.strip()[:50] in f.read()
    except Exception:
        return slug, False
    return slug, ok


def learn_report():
    lines = []
    for state in ("staged", "approved", "rejected"):
        d = os.path.join(LEARN_DIR, state)
        names = sorted(f[:-3] for f in os.listdir(d) if f.endswith(".md")) if os.path.isdir(d) else []
        lines.append(f"{state} ({len(names)}):")
        lines += [f"  {nm}" for nm in names] or ["  —"]
    lines.append("\nКоманды: promote <slug> | reject <slug>")
    return "\n".join(lines)


def learn_move(slug, dest):
    # slug приходит из текста сообщения («promote <slug>»): режем до имени файла,
    # иначе «promote ../../secret» увёл бы чтение и удаление за пределы learnings/.
    slug = os.path.basename(slug).removesuffix(".md")
    if not slug or slug.startswith("."):
        return False, "имя заметки пустое или начинается с точки"
    src = os.path.join(LEARN_DIR, "staged", slug + ".md")
    if not os.path.exists(src):
        return False, f"нет staged/{slug}.md (список: learnings)"
    os.makedirs(os.path.join(LEARN_DIR, dest), exist_ok=True)
    dst = os.path.join(LEARN_DIR, dest, slug + ".md")
    try:
        with open(src, encoding="utf-8") as f:
            body = f.read().replace("status: staged", f"status: {dest}", 1)
        with open(dst, "w", encoding="utf-8") as f:
            f.write(body)
        os.remove(src)
        return True, dst
    except Exception as e:
        return False, str(e)


def learnings_block():
    """Approved-поправки → блок промпта (применяются поверх базы знаний)."""
    d = os.path.join(LEARN_DIR, "approved")
    if not os.path.isdir(d):
        return ""
    parts, total = [], 0
    for name in sorted(os.listdir(d)):
        if not name.endswith(".md"):
            continue
        try:
            with open(os.path.join(d, name), encoding="utf-8") as f:
                t = f.read()
        except Exception:
            continue
        t = t.split("---", 2)[-1].strip()[:900]  # без frontmatter
        if total + len(t) > 4000:
            break
        parts.append(f"[{name[:-3]}]\n{t}")
        total += len(t)
    if not parts:
        return ""
    return ("\n\nПОПРАВКИ ОТ КОМАНДЫ (утверждены владельцем; применяй ПОВЕРХ базы знаний, "
            "при конфликте поправка главнее):\n" + "\n\n".join(parts) + "\n")


# --- промпт ---

_prompt_cache = {}


def _prompt_file(name):
    """Читает шаблон промпта, перечитывая файл только при изменении.

    Правила поведения живут в prompts/*.md, а не в коде: одну f-строку на десятки
    килобайт нельзя ни отревьюить в диффе, ни поправить, не задев соседнее
    правило. Кэш по mtime: деплой подхватывается без рестарта бота.
    """
    path = os.path.join(PROMPT_DIR, name)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return ""
    cached = _prompt_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    with open(path, encoding="utf-8") as f:
        text = f.read()
    _prompt_cache[path] = (mtime, text)
    return text


def _fill(text, **values):
    """Подставляет {{ПЛЕЙСХОЛДЕРЫ}} строго за ОДИН проход.

    Один проход принципиален: вопрос пользователя подставляется сюда же, и при
    последовательных заменах текст «{{LEARNINGS}}», написанный в сообщении,
    развернулся бы в реальный блок памяти.
    """
    return re.sub(r"\{\{(\w+)\}\}",
                  lambda m: values.get(m.group(1), m.group(0)), text)


def _common_values():
    return {"AGENT_NAME": agent_name(), "TEAM_NAME": team_name(),
            "PRODUCT_NAME": product_name(), "OWNER_REF": owner_ref(),
            "TODAY": time.strftime("%d.%m.%Y"), "TZ": env("TZ", "UTC"),
            "AGENT_HOME": AGENT_HOME, "REPO": REPO}


def _gitlab_tools():
    url, project = env("GITLAB_URL").rstrip("/"), env("GITLAB_PROJECT")
    if not url or not project:
        return ""
    issue_url = f"{url}/{project}/-/issues/<номер>" if not project.isdigit() else \
        f"{url}/<группа>/<проект>/-/issues/<номер> (полную ссылку бери из поля url в выводе)"
    return _fill(_prompt_file("tools-gitlab.md"), ISSUE_URL=issue_url, **_common_values())


def _code_tools(question):
    repos = code_repos()
    if not repos:
        return ""
    mounted = set(code_dirs(question, team=True))
    lines = []
    for r in repos:
        path = os.path.join(CODE_DIR, r["name"])
        if not os.path.isdir(path):
            state = "клона пока нет — недоступен"
        elif path in mounted:
            state = "ПОДКЛЮЧЁН к этому вопросу"
        else:
            state = ("НЕ подключён к этому вопросу (подключается только при словах: "
                     + ", ".join(r["keywords"]) + ") — не ссылайся на него и не гадай по нему")
        lines.append(f"- `{r['name']}` (ветка {r['branch']}, путь code/{r['name']}) — {state}")
    return _fill(_prompt_file("tools-code.md"), CODE_REPOS_LIST="\n".join(lines),
                 CODE_DATE=code_slice_date(), **_common_values())


# --- коннекторы (connectors/<имя>/, все выключены по умолчанию) ---
# Включённый коннектор добавляет свой фрагмент connectors/<имя>/prompt.md в инструменты
# команды. Выключенные не меняют промпт ни на байт. Публичный режим коннекторов не видит.

def connector_on(name):
    return env("CONNECTOR_" + name.upper().replace("-", "_") + "_ENABLED") == "1"


def _connector_prompt(name, **values):
    return _fill(_prompt_file(os.path.join(CONNECTORS_DIR, name, "prompt.md")), **values, **_common_values())


def _json_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _product_api_values():
    cfg = _json_file(env("PRODUCT_API_CONFIG", os.path.join(AGENT_HOME, "config", "product-api.json")))
    write = ""
    if env("PRODUCT_API_WRITE_ENABLED") == "1" and cfg.get("write"):
        write = "\n" + _fill(_prompt_file(os.path.join(CONNECTORS_DIR, "product-api", "prompt-write.md")),
                             PRODUCT_API_COLLECTIONS=", ".join(cfg["write"]), **_common_values()).rstrip("\n")
    return {"PRODUCT_API_GET": ", ".join(cfg.get("get_allowlist") or []) or "см. config/product-api.json",
            "PRODUCT_API_WRITE": write}


_mcp_mod = {}


def mcp_setup(team):
    """MCP-серверы для `claude -p` (только полный режим команды). → (аргументы, доп. запреты, инструменты).
    Проверка конфига и белого списка — connectors/mcp/mcp_check.py: пишущие инструменты
    без MCP_ALLOW_WRITE_TOOLS=1 отбрасываются, без разрешённых инструментов MCP не подключается."""
    cfg = env("MCP_CONFIG")
    if not team or not cfg:
        return [], [], []
    cfg = cfg if os.path.isabs(cfg) else os.path.join(AGENT_HOME, cfg)
    if not os.path.exists(cfg):
        print(f"WARN: MCP_CONFIG={cfg} не найден — MCP не подключён", file=sys.stderr, flush=True)
        return [], [], []
    if "m" not in _mcp_mod:
        import importlib.util
        spec = importlib.util.spec_from_file_location("mcp_check", os.path.join(CONNECTORS_DIR, "mcp", "mcp_check.py"))
        _mcp_mod["m"] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_mcp_mod["m"])
    m = _mcp_mod["m"]
    errs, _, servers = m.check_config(_json_file(cfg))
    tools, terrs, _ = m.check_tools([t.strip() for t in env("MCP_ALLOWED_TOOLS").split(",") if t.strip()],
                                    servers, env("MCP_ALLOW_WRITE_TOOLS") == "1")
    if errs or terrs:
        print("WARN: MCP: " + "; ".join(errs + terrs)[:500], file=sys.stderr, flush=True)
    if errs:
        return [], [], []
    deny = [t.strip() for t in env("MCP_DISALLOWED_TOOLS").split(",") if t.strip()]
    return m.bot_args(cfg, tools, deny), (deny if tools else []), tools


def _connector_tools():
    parts = []
    if connector_on("logs-metrics"):
        idx = ", ".join(s.strip() for s in env("LOGS_INDEX_ALLOWLIST").split(",") if s.strip())
        parts.append(_connector_prompt("logs-metrics", LOGS_INDEXES=idx or "(не заданы — LOGS_INDEX_ALLOWLIST)"))
    if connector_on("product-api"):
        parts.append(_connector_prompt("product-api", **_product_api_values()))
    if connector_on("prod-digest"):
        parts.append(_connector_prompt("prod-digest", PROD_DIGEST_LABEL=env("PROD_DIGEST_LABEL", "На проде")))
    tools = mcp_setup(True)[2]
    if tools:
        parts.append(_connector_prompt("mcp", MCP_TOOLS=", ".join(f"`{t}`" for t in tools)))
    return "".join(p if p.endswith("\n") else p + "\n" for p in parts if p)


def build_prompt(asker, question, context_block, in_group, team=True):
    brevity = ("В групповом чате отвечай особенно коротко: 2-5 предложений + источник."
               if in_group else
               "Отвечай кратко: 2-5 предложений + источник.")
    common = _common_values()
    # Вне команды: без shell-инструментов и внутренних систем.
    access_notice = "" if team else _fill(_prompt_file("access-public.md"), **common)
    team_tools = ""
    if team:
        team_tools = _fill(_prompt_file("team-tools.md"),
                           GITLAB_TOOLS=_gitlab_tools(), CODE_TOOLS=_code_tools(question),
                           CONNECTOR_TOOLS=_connector_tools(), **common)
    return _fill(_prompt_file("system.md"),
                 BREVITY=brevity, ASKER=asker, QUESTION=question,
                 ACCESS_NOTICE=access_notice, TEAM_TOOLS=team_tools,
                 LEARNINGS=learnings_block(), CONTEXT=context_block, **common)


# --- запуск модели ---

_last_stats = {}  # usage последнего run_agent (num_turns, токены, cost)


def qa_disallowed():
    """Запреты только для ответов в чате (не для duty: им нужны python3 и git для
    своих конвейеров). Ответ в чате читает чужие тексты (сообщения, вложения,
    задачи) — prompt-injection не должен получить исполнение кода, запись файлов
    и чтение чужих диалогов и дампов чатов."""
    h = "/" + AGENT_HOME  # Claude Code: абсолютный путь в правилах пишется с «//»
    rules = ["Edit", "Write", "NotebookEdit",
             "Bash(python3:*)", "Bash(python:*)", "Bash(bash:*)", "Bash(sh:*)", "Bash(git -C:*)"]
    for sub in ("memory/dm", "memory/learnings", "digest_run", "voc_run", "helpdesk_run", "eval_run",
                "tg", ".venv"):
        rules += [f"Read({h}/{sub}/**)", f"Grep({h}/{sub}/**)"]
    return rules


# секреты бота модели не нужны: bin/gl сам читает свой токен из .env
_SECRET_ENV = ("TG_BOT_TOKEN", "TG_API_ID", "TG_API_HASH", "GITLAB_TOKEN")
# переменные коннекторов: обёртки читают свои токены из .env сами; заодно отсекаем
# PRODUCT_API_WRITE_CONTEXT, унаследованный извне, — его ставит только run_agent
_SECRET_PREFIXES = ("LOGS_ES_", "METRICS_PROM_", "PRODUCT_API_", "HELPDESK_", "CLIENT_REPORTS_",
                    "GITHUB_", "EVAL_")


def run_agent(prompt, team=True, dirs=None, write_ok=False):
    """write_ok=True — только для лички команды: обёртка product-api разрешит запись
    (при PRODUCT_API_WRITE_ENABLED=1 и своих предохранителях)."""
    env_ = {k: v for k, v in os.environ.items()
            if k not in _SECRET_ENV and not k.startswith(_SECRET_PREFIXES)}
    if team and write_ok and connector_on("product-api"):
        env_["PRODUCT_API_WRITE_CONTEXT"] = "team-dm"
    mcp_args, mcp_deny, _ = mcp_setup(team)
    cfg = ".claude-config" if team else ".claude-config-public"
    env_["CLAUDE_CONFIG_DIR"] = os.path.join(AGENT_HOME, cfg)
    args = ["--add-dir", REPO]
    for cd in (dirs or []):
        args += ["--add-dir", cd]
    # Лимита turns нет — бэкстоп wall-clock таймаут: с кодом в контексте разбор
    # идёт дольше, простым вопросам — короткий таймаут (быстрый фейл при зависании).
    timeout_s = env_int("QUERY_TIMEOUT_CODE_S", 600) if dirs else env_int("QUERY_TIMEOUT_S", 300)
    model = model_answer()
    try:
        out = subprocess.run(
            ["claude", *args, "--model", model, "--output-format", "json", *mcp_args,
             "--disallowedTools", *qa_disallowed(), *mcp_deny, "-p", prompt],
            capture_output=True, text=True, timeout=timeout_s,
            # публичный режим — рабочая папка knowledge/: Claude Code сам разрешает
            # читать cwd, и при cwd=AGENT_HOME любой собеседник в личке мог бы
            # прочитать логи, бэкапы .env и дампы чатов.
            cwd=AGENT_HOME if team else REPO, env=env_,
        )
        raw = out.stdout.strip()
        try:
            d = json.loads(raw)
            u = d.get("usage") or {}
            _last_stats.clear()
            _last_stats.update({
                "turns": d.get("num_turns"),
                "tokens_in": u.get("input_tokens"),
                "tokens_out": u.get("output_tokens"),
                "cache_read": u.get("cache_read_input_tokens"),
                "cache_write": u.get("cache_creation_input_tokens"),
                "cost_usd": round(d.get("total_cost_usd") or 0, 4),
                "model": model,
                "mode": "team" if team else "public",
                "code_dirs": len(dirs or []),
            })
            text = (d.get("result") or "").strip()
            ok = out.returncode == 0 and not d.get("is_error") and bool(text)
            if ok:
                return text, True, out.stderr[-500:]
            # JSON валиден, но ответа нет — фиксируем ПОЧЕМУ, чтобы сбой был диагностируем
            why = (f"rc={out.returncode} subtype={d.get('subtype')} "
                   f"is_error={d.get('is_error')} stop={d.get('stop_reason')} "
                   f"turns={d.get('num_turns')} result={(d.get('result') or '')[:200]!r} "
                   f"stderr={out.stderr[-200:]!r}")
            return text, False, why
        except (ValueError, AttributeError):
            _last_stats.clear()
            why = f"non-json rc={out.returncode} stdout={raw[-300:]!r} stderr={out.stderr[-200:]!r}"
            return raw, out.returncode == 0 and bool(raw), why
    except subprocess.TimeoutExpired:
        _last_stats.clear()
        return "", False, f"timeout after {timeout_s}s (code_dirs={len(dirs or [])})"
    except OSError as e:
        _last_stats.clear()
        return "", False, f"не запустился claude: {e}"


# Историю диалога подмешиваем ТОЛЬКО когда вопрос похож на продолжение — иначе
# самодостаточный вопрос тянет нерелевантные прошлые ответы.
_FOLLOWUP_MARK = ("он ", "она ", "они ", "его ", "её ", "ее ", "это ", "этот", "эту",
                  "эти ", "там", "туда", "по нему", "по ней", "по этому", "тоже", "ещё",
                  "еще", "а что", "а как", "а если", "а почему", "а когда", "а где",
                  "а сколько", "предыдущ", "выше", "тот же", "та же", "то же",
                  "как ты сказал", "как выше", "вернись")
_OWN_ID_RE = re.compile(r"\d{4,}|[0-9a-f]{8}-[0-9a-f]{4}-", re.I)


def _needs_history(question):
    """Нужен ли фон прошлого диалога. По умолчанию НЕТ — включаем только для follow-up."""
    q = (question or "").strip().lower()
    if not q:
        return False
    if _OWN_ID_RE.search(q):          # свой идентификатор → вопрос самодостаточен
        return False
    if len(q.split()) <= 3:           # очень короткий вопрос без id → вероятно продолжение
        return True
    return any(m in q for m in _FOLLOWUP_MARK)


_last_alert = {}   # тип сбоя -> время последнего алерта (не спамить при серии)


def _alert_failure(asker, question, err):
    """Ответ не собрался — владельцу в личку сразу. Без этого упор в лимит
    подписки и таймауты проходят молча: тишина ≠ успех."""
    e = err or ""
    kind = ("лимит подписки" if "hit your limit" in e.lower() or "usage limit" in e.lower()
            else "таймаут" if "timeout" in e else "сбой")
    now = time.time()
    if now - _last_alert.get(kind, 0) < 900:   # один алерт на тип сбоя за 15 минут
        return
    _last_alert[kind] = now
    if not owner_id():
        return
    try:
        send(owner_id(), f"⚠️ {agent_name()} не ответил ({kind}): {asker} — «{question[:120]}»\n{e[:300]}")
    except Exception as ex:
        print(f"alert failed: {ex}", flush=True)


def _answered(asker, question, ctx, in_group, surface, team=True, write_ok=False):
    start = time.time()
    dirs = code_dirs(question, team)
    text, ok, err = run_agent(build_prompt(asker, question, ctx, in_group, team), team, dirs, write_ok=write_ok)
    latency = int(time.time() - start)
    if not ok or not text:
        log_interaction(surface=surface, requester=asker, latency_s=latency,
                        ok=False, question=question[:200], error=err)
        _alert_failure(asker, question, err)
        return "⚠️ Не смог собрать ответ (сбой на моей стороне). Владелец бота получил уведомление.", False
    log_interaction(surface=surface, requester=asker, latency_s=latency,
                    ok=True, question=question[:200], answer_len=len(text),
                    **{k: v for k, v in _last_stats.items() if v is not None})
    return text, True


def answer_dm(asker, chat_id, question, team, reply_ctx=""):
    # реплай на сообщение бота — ЯВНЫЙ сигнал «продолжаем»: тянем и цитату, и историю
    turns = dm_recent(chat_id) if (reply_ctx or _needs_history(question)) else []
    ctx = ""
    if reply_ctx:
        ctx += ("\n\nЭто РЕПЛАЙ пользователя на твой ПРОШЛЫЙ ответ — продолжай ту же нить, "
                "учитывай процитированное как прямой контекст текущего вопроса (не переспрашивай "
                "то, что уже в нём есть):\n«" + reply_ctx[:1500] + "»")
    if turns:
        lines = [f"[{t['role']}]: {t['text']}" for t in turns]
        ctx += ("\n\nФОН — недавние реплики ЭТОГО диалога (могут быть НЕ по теме текущего вопроса). "
                "Используй их ТОЛЬКО если текущий вопрос без них непонятен (явное продолжение). "
                "Если вопрос самодостаточен — ИГНОРИРУЙ фон: не смешивай темы и не тяни прошлые ответы в этот:\n"
                + "\n".join(lines))
    # запись через коннекторы (product-api) — только в личке команды, не в группах
    text, ok = _answered(asker, question, ctx, in_group=False, surface="dm", team=team, write_ok=team)
    if ok:
        path = os.path.join(DM_DIR, f"{chat_id}.jsonl")
        _append_jsonl(path, {"ts": time.time(), "role": "user", "text": question[:2000]})
        _append_jsonl(path, {"ts": time.time(), "role": "agent", "text": text[:2000]})
    return text


def answer_group(asker, question, addressed, team):
    ctx = ""
    if not addressed:
        ctx += ("\n\nВАЖНО: это, вероятно, продолжение твоего диалога с этим человеком (ты недавно "
                "ему отвечал), но тебя НЕ тегнули — реши сам, адресовано ли сообщение тебе. Отвечай, "
                "если это follow-up к твоему ответу или вопрос про продукт/процессы/задачи команды, "
                "на который у тебя есть ответ из источников. Если сообщение адресовано коллеге, "
                "оффтоп или ответа в источниках нет — выведи ровно NO_ANSWER и ничего больше.")
    text, ok = _answered(asker, question, ctx, in_group=True,
                         surface="mention" if addressed else "group-question", team=team)
    if not addressed and (not ok or "NO_ANSWER" in text[:40]):
        return ""
    return text


# --- обработка апдейтов ---

def _replied_to_bot(msg):
    u = bot_username()
    return bool(u) and ((msg.get("reply_to_message") or {}).get("from") or {}).get("username") == u


def _help_text(team):
    mode = ("полный режим: база знаний, код и задачи" if team
            else "публичный режим: только база знаний")
    return (f"Я {agent_name()}, ассистент команды {team_name()}. Отвечаю на вопросы про "
            f"{product_name()} с источником. У вас {mode}.\n\n"
            "Просто напишите вопрос. Ошибся — ответьте на мой ответ словами "
            "«ошибаешься, …» или «на самом деле …»: поправка уйдёт владельцу на проверку.")


def handle_update(msg):
    chat = msg.get("chat") or {}
    ctype = chat.get("type")
    user = msg.get("from") or {}
    uid = str(user.get("id"))
    asker = user.get("first_name") or uid
    text = (msg.get("text") or msg.get("caption") or "").strip()
    has_media = bool(msg.get("photo") or msg.get("document"))
    if user.get("is_bot") or (not text and not has_media):
        return
    team = uid in team_ids()
    mention = f"@{bot_username()}" if bot_username() else "\x00"

    if ctype in ("group", "supergroup") and allowed_groups() and str(chat.get("id")) not in allowed_groups():
        return
    if ctype == "private" and not team and env("PUBLIC_DM", "1") != "1":
        send(chat["id"], f"Я отвечаю только команде {team_name()}. Напишите владельцу бота{owner_ref()}, если нужен доступ.")
        return

    if has_media:
        if not team:
            # вложения разбираем только для команды: картинка — тоже вход для prompt-injection
            if ctype == "private" and not text:
                send(chat["id"], f"Скриншоты и файлы разбираю только для команды {team_name()} — опишите вопрос текстом.")
                return
        elif ctype == "private" or mention in text or _replied_to_bot(msg):
            att = fetch_attachment(msg)   # в группе — только когда обращаются к боту
            if att:
                text = (text or "Посмотри вложение и разберись, что на нём и в чём проблема.") + (
                    f"\n\n[Вложение пользователя: {att} — открой его инструментом Read "
                    "(изображение/файл), опирайся на то, что на нём видно]")
            elif not text:
                d = msg.get("document") or {}
                unsupported = d and not ((d.get("mime_type") or "").startswith(_DOC_OK)
                                         and (d.get("file_size") or 0) <= _DOC_MAX)
                send(chat["id"], "Этот файл не читаю — понимаю фото, PDF и текст/логи до 10 МБ. Опишите вопрос текстом."
                     if unsupported else "Не смог скачать вложение — пришлите, пожалуйста, ещё раз или опишите текстом.")
                return
        if not text:
            return

    if ctype == "private":
        low = text.lower().strip()
        if low in ("/start", "/help"):
            send(chat["id"], _help_text(team))
            return
        # M4: управление поправками — только владелец
        if owner_id() and uid == owner_id():
            if low in ("learnings", "поправки"):
                send(chat["id"], learn_report())
                return
            m = re.match(r"^(promote|reject)\s+(\S+)$", low)
            if m:
                dest = "approved" if m.group(1) == "promote" else "rejected"
                ok_mv, info = learn_move(m.group(2), dest)
                send(chat["id"], (f"✅ {m.group(2)} → {dest}. Действует со следующего ответа."
                                  if ok_mv else f"⚠️ Не вышло: {info}"))
                log_interaction(surface="learn", requester=asker, latency_s=0,
                                ok=ok_mv, question=text[:100])
                return
        # M4: поправка реплаем на ответ бота
        replied_bot = _replied_to_bot(msg)
        # поправка без reply тоже считается, если бот отвечал в этом диалоге ≤30 мин назад
        last_bot = ""
        if not replied_bot and _is_correction(text):
            recent = [t for t in dm_recent(chat["id"]) if t.get("role") == "agent"
                      and time.time() - t.get("ts", 0) <= 1800]
            last_bot = recent[-1].get("text", "") if recent else ""
        if (replied_bot or last_bot) and _is_correction(text):
            slug, ok_cap = learn_capture(asker, "dm", text,
                                         (msg.get("reply_to_message") or {}).get("text") or last_bot)
            send(chat["id"], (f"Записал поправку (staged: {slug}) — ждёт решения владельца."
                              if ok_cap else f"⚠️ Не смог записать поправку — напишите владельцу бота{owner_ref()}."))
            log_interaction(surface="learn", requester=asker, latency_s=0,
                            ok=ok_cap, question=text[:100])
            return
        ok, data, why = rate_ok(uid, team)
        if not ok:
            send(chat["id"], f"Притормозим: исчерпан {why} запросов на сегодня. Завтра продолжим.")
            log_interaction(surface="dm", requester=asker, latency_s=0, ok=False,
                            question=text[:100], error=f"rate:{why}")
            return
        try:
            api("sendChatAction", chat_id=chat["id"], action="typing")
        except Exception:
            pass
        rate_bump(data, uid)
        if not text.startswith("/"):
            try:
                send(chat["id"], "Принял вопрос, проверяю источники — отвечу через минуту-другую.")
            except Exception:
                pass
        reply_ctx = ((msg.get("reply_to_message") or {}).get("text") or "") if replied_bot else ""
        send(chat["id"], answer_dm(asker, chat["id"], text, team=team, reply_ctx=reply_ctx))
        return

    if ctype in ("group", "supergroup"):
        mentioned = mention in text
        replied = _replied_to_bot(msg)
        # M4: поправка реплаем на ответ бота — staged, не Q&A
        if replied and _is_correction(text):
            slug, ok_cap = learn_capture(asker, f"group:{chat['id']}", text,
                                         (msg.get("reply_to_message") or {}).get("text") or "")
            send(chat["id"], (f"Записал поправку (staged: {slug}) — ждёт решения владельца."
                              if ok_cap else f"⚠️ Не смог записать поправку — напишите владельцу бота{owner_ref()}."),
                 reply_to=msg.get("message_id"))
            log_interaction(surface="learn", requester=asker, latency_s=0,
                            ok=ok_cap, question=text[:100])
            return
        # follow-up без тега работает, только если у бота выключен privacy mode
        # (иначе Telegram не доставляет ему сообщения без упоминания/реплая)
        followup = followup_active(chat["id"], uid)
        if not (mentioned or replied or followup):
            return
        addressed = mentioned or replied  # follow-up без тега — с правом молчать
        question = text.replace(mention, "").strip() or "(вопрос в процитированном сообщении)"
        quoted = (msg.get("reply_to_message") or {})
        # текст процитированного может лежать в text ИЛИ в caption (медиа с подписью)
        q_text = quoted.get("text") or quoted.get("caption") or ""
        if q_text:
            q_author = (quoted.get("from") or {}).get("first_name") or "коллега"
            if replied:
                question += "\n\n(это ответ на моё сообщение: " + q_text[:500] + ")"
            else:
                question += "\n\n(вопрос задан реплаем на сообщение [" + q_author + "]: " + q_text[:800] + " — отвечай именно на него)"
        elif quoted and any(k in quoted for k in ("photo", "document", "sticker", "video", "voice", "audio", "animation")):
            # реплай на вложение без подписи — картинку чужого сообщения бот не скачивает
            question += ("\n\n(реплай на сообщение-вложение БЕЗ текста — там картинка/файл, "
                         "которые я прочитать не могу. НЕ проси прислать вопрос заново: "
                         "объясни, что вложение я не вижу, и попроси прислать текст ошибки, "
                         "идентификатор или суть проблемы словами)")
        ok, data, why = rate_ok(uid, team)
        if not ok:
            send(chat["id"], f"Притормозим: {why} исчерпан, продолжу завтра.", reply_to=msg.get("message_id"))
            return
        try:
            api("sendChatAction", chat_id=chat["id"], action="typing")
        except Exception:
            pass
        rate_bump(data, uid)
        # ack только при явном обращении: на follow-up без тега бот может решить
        # промолчать (NO_ANSWER) — «принял» перед молчанием слать нельзя
        if addressed:
            try:
                send(chat["id"], "Принял, проверяю источники.", reply_to=msg.get("message_id"))
            except Exception:
                pass
        reply_text = answer_group(asker, question, addressed, team)
        if reply_text:
            send(chat["id"], reply_text, reply_to=msg.get("message_id"))
            followup_set(chat["id"], uid)


def liveness_check(state={"last_alert_day": ""}):
    """Алерт, если свежайший дайджест старше суток + 2 ч: duty упала молча."""
    if not env("DIGEST_FOLDERS") or not owner_id():
        return
    today = time.strftime("%Y-%m-%d")
    if state["last_alert_day"] == today:
        return
    d = DIGESTS_DIR
    files = sorted(os.path.join(d, f) for f in os.listdir(d)) if os.path.isdir(d) else []
    newest = max((os.path.getmtime(f) for f in files), default=0)
    if newest and time.time() - newest > 26 * 3600:
        age_h = int((time.time() - newest) / 3600)
        state["last_alert_day"] = today
        try:
            send(owner_id(), f"⚠️ Liveness: последний дайджест {age_h} ч назад — duty не отработала. "
                             "Проверь: systemctl status team-agent-digest && journalctl -u team-agent-digest -n 20 "
                             "(ручной запуск: systemctl start team-agent-digest)")
        except Exception:
            pass


# --- состояние поллинга: offset + дедуп (после рестарта getUpdates(0) иначе
# вернул бы уже отвеченный вопрос — пользователь получил бы дубль) ---

def load_poll_state():
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
        return int(st.get("offset", 0)), list(st.get("seen", []))
    except Exception:
        return 0, []


def save_poll_state(offset, seen):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"offset": offset, "seen": seen[-SEEN_MAX:]}, f)
    os.replace(tmp, STATE_FILE)


_STOP = threading.Event()      # SIGTERM: перестать брать новые сообщения, дописать начатые
_workers = []                  # живые потоки ответов (для мягкой остановки)


def drain_s():
    return max(env_int("QUERY_TIMEOUT_CODE_S", 600), env_int("QUERY_TIMEOUT_S", 300)) + 40


def _on_term(signum, frame):
    if not any(t.is_alive() for t in _workers):
        # начатых ответов нет — выходим сразу, не дожидаясь long-poll (до 50 с)
        print("SIGTERM: начатых ответов нет — выход", flush=True)
        os._exit(0)
    print("SIGTERM: новые сообщения не беру, дописываю начатые ответы", flush=True)
    _STOP.set()


def check_config():
    missing = [k for k in ("TG_BOT_TOKEN", "TG_BOT_USERNAME", "OWNER_ID") if not env(k)]
    if missing:
        raise SystemExit(f"Не заданы обязательные переменные в {AGENT_HOME}/.env: {', '.join(missing)}")
    if not os.path.isdir(REPO):
        print(f"WARN: нет базы знаний {REPO} — склонируйте её (см. INSTALL.md)", flush=True)


def main():
    signal.signal(signal.SIGTERM, _on_term)
    load_env()
    check_config()
    offset, seen = load_poll_state()
    last_sync = 0.0
    last_liveness = 0.0
    print(f"{agent_name()} bot: polling started (offset={offset}, seen={len(seen)})", flush=True)
    while not _STOP.is_set():
        if time.time() - last_sync > KNOWLEDGE_PULL_S:
            try:
                subprocess.run(["git", "-C", REPO, "pull", "-q", "--ff-only"],
                               capture_output=True, timeout=120)
            except Exception as e:
                print(f"knowledge pull error: {e}", flush=True)
            last_sync = time.time()
        if time.time() - last_liveness > 3600:
            liveness_check()
            last_liveness = time.time()
        try:
            resp = api("getUpdates", offset=offset, timeout=50,
                       allowed_updates='["message"]')
        except Exception as e:
            # HTTP 409 = где-то запущен второй экземпляр с тем же токеном
            print(f"getUpdates error: {e}", flush=True)
            time.sleep(5)
            continue
        for upd in resp.get("result", []):
            offset = upd["update_id"] + 1
            msg = upd.get("message") or {}
            chat_id = (msg.get("chat") or {}).get("id")
            key = f"{chat_id}:{msg.get('message_id')}" if msg else f"u:{upd['update_id']}"
            if key in seen:
                print(f"skip duplicate update {key}", flush=True)
                continue
            seen.append(key)
            del seen[:-SEEN_MAX]

            def _work(m=msg):
                try:
                    handle_update(m)
                except Exception as e:
                    print(f"handle error: {e}", flush=True)

            t = threading.Thread(target=_work, daemon=True)
            t.start()
            _workers.append(t)
        _workers[:] = [t for t in _workers if t.is_alive()]
        if resp.get("result"):
            save_poll_state(offset, seen)
    # мягкая остановка (деплой/рестарт): без неё systemd убивал бы и начатые
    # ответы — вопрос пропадал без ответа. Ждём их, потом выходим.
    save_poll_state(offset, seen)
    alive = [t for t in _workers if t.is_alive()]
    print(f"остановка: жду {len(alive)} начатых ответов (до {drain_s()} с)", flush=True)
    deadline = time.time() + drain_s()
    for t in alive:
        t.join(max(0, deadline - time.time()))
    print("остановка: готово", flush=True)


if __name__ == "__main__":
    main()
