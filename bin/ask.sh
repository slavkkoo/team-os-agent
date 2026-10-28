#!/bin/bash
# ask.sh — задать агенту вопрос из терминала (тест без Telegram), в полном режиме команды.
# Usage: bin/ask.sh <вопрос>         (кто спрашивает: env ASKER, по умолчанию «cli»)
#        PUBLIC=1 bin/ask.sh <вопрос> — проверить публичный режим (как для чужого в личке)
#
# Промпт, модель, подключаемые код-репо и запреты берутся ИЗ БОТА
# (team_agent_bot.py), а не из собственной копии: своя копия промпта в CLI
# неизбежно отстаёт от прода, и тест начинает проверять не то, что видят люди.
set -uo pipefail

V="${AGENT_HOME:-$(cd "$(dirname "$0")/.." && pwd)}"
export AGENT_HOME="$V"
REPO="$V/knowledge"
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
[ -f "$HOME/.claude-token.env" ] && { set -a; . "$HOME/.claude-token.env"; set +a; }
TZ_ENV=$(grep -m1 '^TZ=' "$V/.env" 2>/dev/null | cut -d= -f2-); [ -n "$TZ_ENV" ] && export TZ="$TZ_ENV"   # время в логе — в поясе команды, как у бота

Q="$*"
[ -z "$Q" ] && { echo "usage: ask.sh <вопрос>"; exit 1; }
ASKER="${ASKER:-cli}"
TEAM=1; [ "${PUBLIC:-0}" = "1" ] && TEAM=0

if [ "$TEAM" = 1 ]; then
  export CLAUDE_CONFIG_DIR="$V/.claude-config"; CWD="$V"
else
  export CLAUDE_CONFIG_DIR="$V/.claude-config-public"; CWD="$REPO"
fi

PROMPT_FILE=$(mktemp "${TMPDIR:-/tmp}/team-agent-ask.XXXXXX")
trap 'rm -f "$PROMPT_FILE"' EXIT INT TERM

# Бот отдаёт: модель, таймаут, запреты, доп. аргументы коннекторов (MCP), список --add-dir
# (по строке на каталог); промпт — в файл
# (чтобы не упереться в лимит длины аргументов и не светить вопрос в ps).
META=$(python3 - "$ASKER" "$Q" "$PROMPT_FILE" "$TEAM" "$V/bin/team_agent_bot.py" <<'PY'
import importlib.util, sys
asker, question, out_path, team, path = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "1", sys.argv[5]
spec = importlib.util.spec_from_file_location("bot", path)
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)
bot.load_env()
dirs = bot.code_dirs(question, team)
with open(out_path, "w", encoding="utf-8") as f:
    f.write(bot.build_prompt(asker, question, "", in_group=False, team=team))
print(bot.model_answer())
print(bot.env_int("QUERY_TIMEOUT_CODE_S", 600) if dirs else bot.env_int("QUERY_TIMEOUT_S", 300))
mcp_args, mcp_deny, _ = bot.mcp_setup(team)
print("\x1f".join(bot.qa_disallowed() + mcp_deny))
print("\x1f".join(mcp_args))
for d in dirs:
    print(d)
PY
) || { echo "ask.sh: не смог собрать промпт из team_agent_bot.py" >&2; exit 1; }

MODEL=$(printf '%s\n' "$META" | sed -n 1p)
TMO=$(printf '%s\n' "$META" | sed -n 2p)
IFS=$'\x1f' read -r -a DISALLOWED <<< "$(printf '%s\n' "$META" | sed -n 3p)"
EXTRA=(); EXTRA_LINE="$(printf '%s\n' "$META" | sed -n 4p)"
[ -n "$EXTRA_LINE" ] && IFS=$'\x1f' read -r -a EXTRA <<< "$EXTRA_LINE"
ADD_DIRS=(--add-dir "$REPO")
while IFS= read -r d; do [ -n "$d" ] && ADD_DIRS+=(--add-dir "$d"); done < <(printf '%s\n' "$META" | sed -n '5,$p')

[ -s "$PROMPT_FILE" ] || { echo "ask.sh: пустой промпт (нет prompts/system.md?)" >&2; exit 1; }

mkdir -p "$V/logs"
START=$(date +%s)
cd "$CWD" || exit 1
OUT=$(timeout "$TMO" claude "${ADD_DIRS[@]}" --model "$MODEL" ${EXTRA[@]+"${EXTRA[@]}"} \
  --disallowedTools "${DISALLOWED[@]}" -p "$(cat "$PROMPT_FILE")" 2>"$V/logs/last_err.log")
RC=$?
LATENCY=$(( $(date +%s) - START ))

if [ $RC -ne 0 ] || [ -z "$OUT" ]; then
  echo "ask.sh: claude вернул rc=$RC. Хвост: $(tail -c 300 "$V/logs/last_err.log")" >&2
  [ $RC -eq 0 ] && RC=1
else
  printf '%s\n' "$OUT"
fi

OUT="$OUT" MODEL="$MODEL" python3 - "$RC" "$LATENCY" "$ASKER" "$Q" "$V/logs/interactions.jsonl" "$TEAM" <<'PY'
import json, os, sys, datetime
rc, lat, asker, q, log, team = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6]
rec = {
    "ts": datetime.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"),
    "surface": "cli", "requester": asker, "latency_s": lat,
    "ok": rc == 0, "question": q[:200], "model": os.environ.get("MODEL", ""),
    "mode": "team" if team == "1" else "public",
    "answer_len": len(os.environ.get("OUT", "")),
}
with open(log, "a", encoding="utf-8") as f:
    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
PY
exit $RC
