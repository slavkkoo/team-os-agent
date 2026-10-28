#!/bin/bash
# Duty коннектора prod-digest: ежемесячный черновик «что нового уехало на прод».
# Источник — задачи GitLab, у которых лейбл PROD_DIGEST_LABEL ПОСТАВЛЕН в прошлом месяце
# (prod_issues.py по resource_label_events; дата закрытия задачи тут врёт).
# Модель группирует пользовательские новинки в пост → ЧЕРНОВИК владельцу в личку, не в канал.
# Запуск: team-agent-proddigest.timer (первый понедельник месяца). Отладка: run_prod_digest.sh 2026-09-01 2026-10-01
set -uo pipefail
DUTY_NAME="prod-digest"
. "$(dirname "$0")/../../bin/lib/duty.sh"

[ "$(duty_env CONNECTOR_PROD_DIGEST_ENABLED)" = 1 ] || { echo "коннектор prod-digest выключен — пропускаю"; exit 0; }
RUN="$V/proddigest_run"
mkdir -p "$RUN"
TOOL="$V/connectors/prod-digest/prod_issues.py"

if [ $# -eq 2 ]; then SINCE="$1"; UNTIL="$2"; else read -r SINCE UNTIL < <(python3 "$TOOL" window); fi
MONTH="$SINCE — $UNTIL"

if ! python3 "$TOOL" "$SINCE" "$UNTIL" > "$RUN/issues.txt" 2> "$RUN/collect_err.log"; then
  duty_alert "⚠️ $AGENT_NAME/$DUTY_NAME: не собрал задачи ($MONTH): $(tail -c 300 "$RUN/collect_err.log")"
  exit 1
fi
if ! grep -q '^[0-9]' "$RUN/issues.txt"; then
  duty_send_owner "📦 Прод-дайджест $MONTH: задач с лейблом «$(duty_env PROD_DIGEST_LABEL "На проде")» за месяц нет (или лейбл не ставили)."
  exit 0
fi

PROMPT=$(duty_prompt "$V/connectors/prod-digest/duty-prompt.md" ISSUES="$RUN/issues.txt" MONTH="$MONTH") || {
  duty_alert "⚠️ $AGENT_NAME/$DUTY_NAME: не собрался промпт connectors/prod-digest/duty-prompt.md"; exit 1; }
cd "$V" || exit 1
duty_claude "$RUN/digest.txt" "$RUN/claude_err.log" "$MODEL_DUTY" 900 "$PROMPT" "Read(/$RUN/**)" --add-dir "$RUN"
rc=$?
[ "$rc" = 2 ] && exit 0
[ "$rc" != 0 ] && exit 1

duty_send_owner "📝 Черновик прод-дайджеста за $MONTH (задач на входе: $(grep -c '^[0-9]' "$RUN/issues.txt")). Проверь и опубликуй сам:

$(cat "$RUN/digest.txt")"
echo "prod-digest ok: $MONTH"
