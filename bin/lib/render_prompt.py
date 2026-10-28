#!/usr/bin/env python3
"""render_prompt — собирает промпт duty из prompts/<имя>.md.

Usage: render_prompt.py <имя-или-путь> [KEY=VALUE ...] [KEY=@файл ...]

Подстановка {{KEY}} — за ОДИН проход (как в боте): значения, в которых
встречается «{{...}}» (например, вопросы пользователей), не разворачиваются.
Общие значения (AGENT_NAME, TEAM_NAME, PRODUCT_NAME, OWNER_REF, TZ, AGENT_HOME,
REPO, TODAY, DATE_ISO) берутся из .env. Если в шаблоне остался плейсхолдер без
значения — выход с кодом 2: лучше упасть с алертом, чем отправить модели
промпт с дырой.
"""
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_HOME = os.path.abspath(os.environ.get("AGENT_HOME") or os.path.join(HERE, "..", ".."))
PH = re.compile(r"\{\{(\w+)\}\}")


def load_env():
    path = os.path.join(AGENT_HOME, ".env")
    vals = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                vals[k.strip()] = v
    return vals


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    name = sys.argv[1]
    path = name if os.path.sep in name else os.path.join(AGENT_HOME, "prompts", name)
    if not path.endswith(".md") and not os.path.exists(path):
        path += ".md"
    text = open(path, encoding="utf-8").read()

    e = load_env()
    g = lambda k, d: (os.environ.get(k) or e.get(k) or d)  # noqa: E731
    values = {
        "AGENT_NAME": g("AGENT_NAME", "Ассистент"),
        "TEAM_NAME": g("TEAM_NAME", "команды"),
        "PRODUCT_NAME": g("PRODUCT_NAME", "продукт"),
        "OWNER_REF": f" ({g('OWNER_NAME', '')})" if g("OWNER_NAME", "") else "",
        "TZ": g("TZ", "UTC"),
        "AGENT_HOME": AGENT_HOME,
        "REPO": os.path.join(AGENT_HOME, "knowledge"),
        "TODAY": time.strftime("%d.%m.%Y"),
        "DATE_ISO": time.strftime("%Y-%m-%d"),
    }
    for arg in sys.argv[2:]:
        if "=" not in arg:
            sys.exit(f"render_prompt: аргумент без '=': {arg}")
        k, v = arg.split("=", 1)
        if v.startswith("@"):
            v = open(v[1:], encoding="utf-8").read()
        values[k] = v

    missing = sorted({m for m in PH.findall(text) if m not in values})
    if missing:
        print(f"render_prompt: в {path} нет значений для: {', '.join(missing)}", file=sys.stderr)
        sys.exit(2)
    sys.stdout.write(PH.sub(lambda m: values[m.group(1)], text))


if __name__ == "__main__":
    main()
