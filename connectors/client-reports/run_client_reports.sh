#!/bin/bash
# Duty коннектора client-reports: ежедневные отчёты клиентам по config/client-reports.json.
# Без LLM: цифры считает источник, решение «слать или нет» — client_reports.py:
# любая метрика «н/д» или упавший источник → клиенту НИЧЕГО, владельцу алерт с черновиком.
# Запуск: team-agent-clientreports.timer. Проверка без отправки: run_client_reports.sh --dry-run
set -uo pipefail
DUTY_NAME="client-reports"
. "$(dirname "$0")/../../bin/lib/duty.sh"

[ "$(duty_env CONNECTOR_CLIENT_REPORTS_ENABLED)" = 1 ] || { echo "коннектор client-reports выключен — пропускаю"; exit 0; }
OUT=$(python3 "$V/connectors/client-reports/client_reports.py" run "$@" 2>&1)
rc=$?
echo "$OUT"
# rc=1 — есть неотправленные (алерт с черновиком владелец уже получил) или недоставленные:
# дублируем сводкой, чтобы «тишина» не выглядела как «всё ушло»
if [ $rc -ne 0 ]; then
  duty_alert "⚠️ $AGENT_NAME/$DUTY_NAME: не все отчёты ушли клиентам:
$(printf '%s' "$OUT" | tail -c 1500)"
  exit 1
fi
