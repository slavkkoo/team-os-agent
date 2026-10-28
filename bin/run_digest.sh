#!/bin/bash
# Duty: дневной дайджест рабочих Telegram-папок (DIGEST_FOLDERS).
# Конвейер: read_chat.py за 24 ч → split по папкам → изолированный claude -p
# (по субагенту на папку) → memory/digests/YYYY-MM-DD.md → отправка.
# Маршрутизация: секция «🔴 Требуется от владельца» — ТОЛЬКО владельцу в личку;
# остальное (🟡/⚪) — в командный канал DIGEST_CHAT_ID. ⚠️-алерты — владельцу.
# Запуск: team-agent-digest.timer. Отладка: run_digest.sh --reuse-dump (не перечитывать Telegram).
set -uo pipefail
DUTY_NAME="дайджест"
. "$(dirname "$0")/lib/duty.sh"

RUN="$V/digest_run"
DIGESTS="$V/memory/digests"
PY="$V/.venv/bin/python3"
FOLDERS="$(duty_env DIGEST_FOLDERS)"
CHANNEL_ID="$(duty_env DIGEST_CHAT_ID "$OWNER_ID")"
mkdir -p "$RUN" "$DIGESTS"

[ -z "$FOLDERS" ] && { echo "DIGEST_FOLDERS не задан — дайджест выключен"; exit 0; }

if [ "${1:-}" = "--reuse-dump" ] && [ -s "$RUN/dump.txt" ]; then
  rm -f "$RUN"/part_*.txt "$RUN/digest.txt"
else
  rm -f "$RUN"/part_*.txt "$RUN/dump.txt" "$RUN/digest.txt"
  if ! "$PY" "$V/bin/read_chat.py" folders "$FOLDERS" 24h > "$RUN/dump.txt" 2> "$RUN/read_err.log"; then
    duty_alert "⚠️ $AGENT_NAME: чтение чатов для дайджеста упало — $(tail -c 300 "$RUN/read_err.log")"
    exit 1
  fi
fi

# по файлу на папку (граница — строка «## Folder: <имя>»)
awk -v dir="$RUN" '/^## Folder:/{n++} n{print > (dir "/part_" n ".txt")}' "$RUN/dump.txt"
if ! ls "$RUN"/part_*.txt > /dev/null 2>&1 || ! grep -q '^=== ' "$RUN"/part_*.txt; then
  duty_send_owner "📋 Дайджест за $(date +%d.%m.%Y): тишина за сутки — сообщений в рабочих чатах нет."
  printf 'Тишина за сутки\n' > "$DIGESTS/$(date +%Y-%m-%d).md"   # liveness бота видит, что duty отработала
  exit 0
fi
NPARTS=$(ls "$RUN"/part_*.txt | wc -l | tr -d ' ')
PARTS=$(for f in "$RUN"/part_*.txt; do
  printf -- '- %s — папка «%s»\n' "$f" "$(head -1 "$f" | sed 's/^## Folder: //')"
done)

PROMPT=$(duty_prompt duty-digest PARTS="$PARTS" NPARTS="$NPARTS" DATE="$(date +%d.%m.%Y)") || {
  duty_alert "⚠️ $AGENT_NAME/$DUTY_NAME: не собрался промпт prompts/duty-digest.md"; exit 1; }

cd "$V" || exit 1
# Модель задана явно: без --model duty молча ехал бы на дефолте CLI, и смена
# дефолта поменяла бы прод. Таймаут 30 мин — обычный прогон укладывается в ~5.
duty_claude "$RUN/digest.txt" "$RUN/claude_err.log" "$MODEL_DUTY" 1800 "$PROMPT" || exit 1

DIGEST=$(cat "$RUN/digest.txt")

# Модель иногда добавляет преамбулу-рассуждение перед дайджестом — режем всё до
# строки с 📋; если заголовка нет вовсе, шлём как есть.
CLEAN=$(printf '%s\n' "$DIGEST" | awk '/^📋/{f=1} f')
[ -n "$CLEAN" ] && DIGEST="$CLEAN"

# Персист ПОЛНОГО дайджеста: это память «что я пропустил» и вход brain-sync
printf '%s\n' "$DIGEST" > "$DIGESTS/$(date +%Y-%m-%d).md"

# Резка по секциям: 🔴 (от строки с 🔴 до строки с 🟡) — владельцу в личку;
# остальное (заголовок 📋 + 🟡 + ⚪) — в командный канал.
HEADER=$(printf '%s\n' "$DIGEST" | head -1)
RED=$(printf '%s\n' "$DIGEST" | awk '/🔴/{f=1} /🟡/{f=0} f')
REST=$(printf '%s\n' "$DIGEST" | awk '/🔴/{f=1} /🟡/{f=0} !f')

if [ "$CHANNEL_ID" = "$OWNER_ID" ]; then
  duty_send_owner "$DIGEST"         # канала нет — всё владельцу одним сообщением
  echo "digest ok: $DIGESTS/$(date +%Y-%m-%d).md (-> owner)"
  exit 0
fi

# Личка: только если в 🔴 есть содержимое (не «нет»)
if [ -n "$RED" ] && ! printf '%s' "$RED" | head -1 | grep -qiE ': *(нет|—|пусто)\s*$'; then
  duty_send_owner "$HEADER
$RED"
fi

# Канал: всё без 🔴 (если после резки что-то осталось кроме заголовка)
if [ "$(printf '%s\n' "$REST" | grep -vc '^\s*$')" -gt 1 ]; then
  duty_send_to "$CHANNEL_ID" "$REST"
else
  duty_send_to "$CHANNEL_ID" "$DIGEST"   # резка не сработала (нет маркеров) — шлём целиком
fi
echo "digest ok: $DIGESTS/$(date +%Y-%m-%d).md (red->owner, rest->channel)"
