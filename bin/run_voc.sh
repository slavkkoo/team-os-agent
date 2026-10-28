#!/bin/bash
# Duty: VoC — «голос клиентов» за неделю (папки VOC_FOLDERS).
# Конвейер: read_chat.py за 168 ч → split по чатам кусками ~200 КБ → изолированный
# claude -p (по субагенту на кусок) → memory/voc/YYYY-MM-DD.md → командный канал.
# Запуск: team-agent-voc.timer. Отладка: run_voc.sh --reuse-dump.
set -uo pipefail
DUTY_NAME="VoC"
. "$(dirname "$0")/lib/duty.sh"

RUN="$V/voc_run"
VOC="$V/memory/voc"
PY="$V/.venv/bin/python3"
FOLDERS="$(duty_env VOC_FOLDERS)"
CHANNEL_ID="$(duty_env DIGEST_CHAT_ID "$OWNER_ID")"
mkdir -p "$RUN" "$VOC"

[ -z "$FOLDERS" ] && { echo "VOC_FOLDERS не задан — VoC выключен"; exit 0; }

if [ "${1:-}" = "--reuse-dump" ] && [ -s "$RUN/dump.txt" ]; then
  rm -f "$RUN"/part_*.txt "$RUN/voc.txt"
else
  rm -f "$RUN"/part_*.txt "$RUN/dump.txt" "$RUN/voc.txt"
  if ! "$PY" "$V/bin/read_chat.py" folders "$FOLDERS" 168h > "$RUN/dump.txt" 2> "$RUN/read_err.log"; then
    duty_alert "⚠️ $AGENT_NAME: чтение чатов для VoC упало — $(tail -c 300 "$RUN/read_err.log")"
    exit 1
  fi
fi

# Сплит по границам чатов/папок, куски ~200 КБ — по субагенту на кусок.
awk -v maxb=200000 -v dir="$RUN" '
  /^## Folder:/ || /^=== /{ if (bytes >= maxb) { n++; bytes = 0 } }
  { print >> (dir "/part_" (n+1) ".txt"); bytes += length($0) + 1 }
' "$RUN/dump.txt"
if ! grep -q '^=== ' "$RUN"/part_*.txt 2>/dev/null; then
  duty_send_to "$CHANNEL_ID" "🗣 VoC: за неделю сообщений в клиентских чатах не было."
  exit 0
fi
NPARTS=$(ls "$RUN"/part_*.txt | wc -l | tr -d ' ')

WEEK="$(date -d '7 days ago' +%d.%m)–$(date +%d.%m.%Y)"   # GNU date (Debian)
PROMPT=$(duty_prompt duty-voc RUN="$RUN" NPARTS="$NPARTS" FOLDERS="$FOLDERS" WEEK="$WEEK") || {
  duty_alert "⚠️ $AGENT_NAME/$DUTY_NAME: не собрался промпт prompts/duty-voc.md"; exit 1; }

cd "$V" || exit 1
duty_claude "$RUN/voc.txt" "$RUN/claude_err.log" "$MODEL_DUTY" 1800 "$PROMPT" || exit 1

REPORT=$(cat "$RUN/voc.txt")
# Режем возможную преамбулу модели до заголовка.
CLEAN=$(printf '%s\n' "$REPORT" | awk '/^🗣/{f=1} f')
[ -n "$CLEAN" ] && REPORT="$CLEAN"

printf '%s\n' "$REPORT" > "$VOC/$(date +%Y-%m-%d).md"
duty_send_to "$CHANNEL_ID" "$REPORT"
echo "voc ok: $VOC/$(date +%Y-%m-%d).md -> $CHANNEL_ID"
