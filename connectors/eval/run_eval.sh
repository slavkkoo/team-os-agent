#!/bin/bash
# Duty коннектора eval: еженедельный eval агента на СВЕЖИХ вопросах.
# Источник вопросов (по приоритету): EVAL_SET (ручной набор jsonl) или, если включён helpdesk,
# свежая неделя тикетов (helpdesk.py fetch) — минус тикеты, на которых уже учился ticket-learn.
# Отчёт владельцу — с оговоркой: судья завышает «галлюцинации», спорные вердикты — на ручной ревью.
# Стоимость: EVAL_SIZE ответов моделью MODEL_ANSWER + столько же вызовов судьи.
# Запуск: team-agent-eval.timer (вс 04:00 — ДО ticket-learn, неделя ещё не выучена).
set -uo pipefail
DUTY_NAME="eval"
. "$(dirname "$0")/../../bin/lib/duty.sh"

[ "$(duty_env CONNECTOR_EVAL_ENABLED)" = 1 ] || { echo "коннектор eval выключен — пропускаю"; exit 0; }
RUN="$V/eval_run/$(date +%Y-%m-%d)"
mkdir -p "$RUN"; chmod 700 "$V/eval_run"
EV="$V/connectors/eval/eval.py"
SIZE="$(duty_env EVAL_SIZE 10)"
SET="$(duty_env EVAL_SET)"

if [ -n "$SET" ]; then
  SRC=(--set "$SET")
elif [ "$(duty_env CONNECTOR_HELPDESK_ENABLED)" = 1 ]; then
  python3 "$V/connectors/helpdesk/helpdesk.py" fetch --days 7 --out "$RUN/pairs.json" > "$RUN/fetch.log" 2>&1 || {
    duty_alert "⚠️ $AGENT_NAME/$DUTY_NAME: выгрузка свежих тикетов упала: $(tail -c 300 "$RUN/fetch.log")"; exit 1; }
  SRC=(--pairs "$RUN/pairs.json")
else
  echo "нет источника вопросов: задайте EVAL_SET или включите helpdesk"; exit 0
fi

python3 "$EV" sample --out "$RUN/sample.json" --size "$SIZE" "${SRC[@]}" > "$RUN/sample.log" 2>&1 || {
  duty_alert "⚠️ $AGENT_NAME/$DUTY_NAME: выборка не собралась: $(tail -c 300 "$RUN/sample.log")"; exit 1; }
N=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))))' "$RUN/sample.json")
[ "${N:-0}" = 0 ] && { duty_send_owner "🧪 Eval: свежих вопросов для выборки нет (все уже в обучении или слишком короткие)."; exit 0; }

timeout $(( N * 660 )) python3 "$EV" run --sample "$RUN/sample.json" --out "$RUN/answers.json" > "$RUN/runner.log" 2>&1 || {
  duty_alert "⚠️ $AGENT_NAME/$DUTY_NAME: прогон ответов упал или не уложился: $(tail -c 300 "$RUN/runner.log")"; exit 1; }
timeout $(( N * 260 )) python3 "$EV" judge --sample "$RUN/sample.json" --answers "$RUN/answers.json" --out "$RUN/verdicts.json" \
  > "$RUN/judge.log" 2>&1 || {
  duty_alert "⚠️ $AGENT_NAME/$DUTY_NAME: судья упал: $(tail -c 300 "$RUN/judge.log")"; exit 1; }

duty_send_owner "🧪 Еженедельный eval ($(date +%F)):
$(python3 "$EV" report --verdicts "$RUN/verdicts.json")
Файлы: ${RUN#"$V"/}/{sample,answers,verdicts}.json"
echo "eval ok: $N вопросов"
