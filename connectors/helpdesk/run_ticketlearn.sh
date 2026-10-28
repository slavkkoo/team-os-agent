#!/bin/bash
# Duty коннектора helpdesk: TICKET-LEARN — обучение на закрытых тикетах поддержки за неделю.
#   1. helpdesk.py fetch → helpdesk_run/pairs.json (пары вопрос клиента / ответ оператора);
#   2. модель сверяет ответы операторов с кодом и базой:
#      верифицированное → ветка ticket-learn/ДАТА + ссылка на PR (ревью человека, не в main);
#      «оператор сказал X, код показывает Y» → владельцу на вердикт, в базу не идёт;
#      отчёт о болях недели → HELPDESK_REPORT_CHAT_ID (пусто = владельцу);
#   3. id обработанных тикетов → memory/helpdesk-learned-ids.txt (eval берёт hold-out НЕ из них).
# Сырые тикеты живут только в helpdesk_run/ (.gitignore, закрыт от чтения в чате).
# Запуск: team-agent-ticketlearn.timer. Прошлая неделя вручную: run_ticketlearn.sh --offset 7
set -uo pipefail
DUTY_NAME="ticket-learn"
. "$(dirname "$0")/../../bin/lib/duty.sh"

[ "$(duty_env CONNECTOR_HELPDESK_ENABLED)" = 1 ] || { echo "коннектор helpdesk выключен — пропускаю"; exit 0; }
OFFSET=0; [ "${1:-}" = "--offset" ] && OFFSET="${2:-0}"
RUN="$V/helpdesk_run"
WORK="$RUN/knowledge"
REPORTS="$V/memory/helpdesk_reports"
LEARNED="$V/memory/helpdesk-learned-ids.txt"
LEARN_PATHS="$(duty_env HELPDESK_LEARN_PATHS "product/")"
mkdir -p "$RUN" "$REPORTS"
chmod 700 "$RUN"

if ! python3 "$V/connectors/helpdesk/helpdesk.py" fetch --days 7 --offset "$OFFSET" --out "$RUN/pairs.json" \
     > "$RUN/fetch.log" 2>&1; then
  duty_alert "⚠️ $AGENT_NAME/$DUTY_NAME: выгрузка тикетов упала: $(tail -c 300 "$RUN/fetch.log")"
  exit 1
fi
read -r CNT WINDOW < <(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["count"], d["window"])' "$RUN/pairs.json")
if [ "${CNT:-0}" = 0 ]; then
  duty_send_owner "🎫 Ticket-learn $WINDOW: закрытых тикетов с ответом оператора нет — учить нечего."
  exit 0
fi

BRANCH="ticket-learn/$(date +%Y-%m-%d)"
if ! duty_workcopy "$WORK" "$BRANCH" > "$RUN/pull.log" 2>&1; then
  duty_alert "⚠️ $AGENT_NAME/$DUTY_NAME: не обновил/не склонировал knowledge: $(tail -c 200 "$RUN/pull.log")"
  exit 1
fi
rm -f "$RUN/contradictions.md" "$RUN/pr_body.md" "$RUN/report.md"

ADD_DIRS=(--add-dir "$WORK")
CODE_LIST=""
for r in $(duty_code_repos); do
  ADD_DIRS+=(--add-dir "$V/code/$r"); CODE_LIST="${CODE_LIST:+$CODE_LIST, }$V/code/$r"
done
PREV=$(ls -1 "$REPORTS"/*.md 2>/dev/null | tail -1)
if [ -n "$PREV" ]; then PREV_LINE="Прошлый отчёт для динамики «было/стало»: $PREV — прочитай его."
else PREV_LINE="Прошлого отчёта нет — сравнений с прошлой неделей не пиши."; fi

PROMPT=$(duty_prompt "$V/connectors/helpdesk/duty-prompt.md" PAIRS="$RUN/pairs.json" WORK="$WORK" RUN="$RUN" \
          WINDOW="$WINDOW" LEARN_PATHS="$LEARN_PATHS" PREV_REPORT="$PREV_LINE" \
          CODE_DIRS="${CODE_LIST:-(код не подключён — без кода факты в поток 1 не пиши)}") || {
  duty_alert "⚠️ $AGENT_NAME/$DUTY_NAME: не собрался промпт connectors/helpdesk/duty-prompt.md"; exit 1; }

cd "$V" || exit 1
duty_claude "$RUN/claude_out.log" "$RUN/claude_err.log" "$MODEL_DUTY" 1800 "$PROMPT" \
  "Read,Grep,Glob,Bash(git log:*),Bash(git -C:*),Bash(ls:*),Edit(/$RUN/**),Write(/$RUN/**)" \
  "${ADD_DIRS[@]}" --max-turns 200
rc=$?
[ "$rc" = 2 ] && exit 0      # лимит подписки: та же неделя разберётся в следующий прогон
[ "$rc" != 0 ] && exit 1

CONTRA="$RUN/contradictions.md"
HAS_VERIFIED=0; [ -n "$(git -C "$WORK" status --porcelain)" ] && HAS_VERIFIED=1
HAS_CONTRA=0;   [ -s "$CONTRA" ] && ! grep -qx 'NONE' "$CONTRA" && HAS_CONTRA=1

if [ "$HAS_VERIFIED" = 1 ]; then
  # shellcheck disable=SC2086
  if URL=$(learn_to_branch "$WORK" "$BRANCH" "ticket-learn $WINDOW: факты из тикетов, сверенные с кодом ($CNT пар)" \
           "$RUN/pr_body.md" $LEARN_PATHS); then
    OUTSIDE=$(cat "$WORK/.git/learn_outside" 2>/dev/null)
    duty_send_owner "🎫 Ticket-learn $WINDOW: новые факты ждут ревью — открой PR: $URL${OUTSIDE:+
⚠️ Правки вне $LEARN_PATHS НЕ взяты: $OUTSIDE}
$(tail -c 900 "$RUN/pr_body.md" 2>/dev/null)"
  else
    rc=$?
    [ "$rc" = 2 ] || duty_alert "⚠️ Ticket-learn: push ветки $BRANCH не прошёл: $(tail -c 200 "$WORK/.git/learn_push.log" 2>/dev/null)"
  fi
fi

if [ "$HAS_CONTRA" = 1 ]; then
  duty_send_owner "⚠️ Ticket-learn $WINDOW: противоречий «ответ поддержки ↔ код»: $(grep -c '^### Противоречие' "$CONTRA") — нужен вердикт (в базу НЕ вливал):

$(cat "$CONTRA")"
fi

if [ -s "$RUN/report.md" ]; then
  cp "$RUN/report.md" "$REPORTS/$(date +%F).md"
  REPORT_CHAT="$(duty_env HELPDESK_REPORT_CHAT_ID "$OWNER_ID")"
  duty_send_to "$REPORT_CHAT" "$(cat "$RUN/report.md")"
fi
[ "$HAS_VERIFIED" = 0 ] && [ "$HAS_CONTRA" = 0 ] && \
  duty_send_owner "🎫 Ticket-learn $WINDOW ($CNT пар): ни новых верифицированных фактов, ни противоречий."

# id разобранных тикетов (без текста) — чтобы eval не брал hold-out из того, на чём учились
python3 -c 'import json,sys; [print(p["ticket_id"]) for p in json.load(open(sys.argv[1]))["pairs"]]' "$RUN/pairs.json" >> "$LEARNED"
sort -u -o "$LEARNED" "$LEARNED"
echo "ticket-learn done: $WINDOW (pairs=$CNT, verified=$HAS_VERIFIED, contradictions=$HAS_CONTRA)"
