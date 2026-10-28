#!/bin/bash
# Duty коннектора product-api: удалить свои тестовые объекты старше PRODUCT_API_CLEANUP_HOURS.
# Правило «создал — удали» страхуется таймером: модель могла забыть, ответ мог оборваться.
# Запуск: team-agent-productapi-cleanup.timer (включается вручную, см. connectors/README.md).
set -uo pipefail
DUTY_NAME="product-api-cleanup"
. "$(dirname "$0")/../../bin/lib/duty.sh"

[ "$(duty_env CONNECTOR_PRODUCT_API_ENABLED)" = 1 ] || { echo "коннектор product-api выключен — пропускаю"; exit 0; }
[ "$(duty_env PRODUCT_API_WRITE_ENABLED)" = 1 ] || { echo "запись product-api выключена — чистить нечего"; exit 0; }

OUT=$(PRODUCT_API_WRITE_CONTEXT=duty "$V/connectors/product-api/product_api.py" --write cleanup 2>&1)
rc=$?
echo "$OUT"
# владельцу — только если что-то удалили или что-то не удалилось: «чисто» не шумит
if [ $rc -ne 0 ] || ! printf '%s' "$OUT" | grep -q '^чисто'; then
  duty_send_owner "🧹 $AGENT_NAME/product-api: автоочистка тестовых объектов (rc=$rc):
$(printf '%s' "$OUT" | head -c 3000)"
fi
[ $rc -eq 0 ] || exit 1
