#!/bin/bash
# Бэкап невосстановимого состояния: память агента, секреты, Telegram-сессия, конфиги хоста.
# Код и база знаний НЕ бэкапятся — они в git. Хранится KEEP дней в /var/backups/teamos
# (chmod 600: внутри секреты). Офсайт-копию забирайте с другой машины (см. INSTALL.md):
# бэкап на том же диске не спасает от потери сервера.
# Шаблон: @AGENT_HOME@/@AGENT_USER_HOME@ подставляет install.sh.
set -uo pipefail
DEST=/var/backups/teamos
KEEP=14
STAMP=$(date +%Y%m%d)
OUT="$DEST/teamos-$STAMP.tar.gz"
V="@AGENT_HOME@"
mkdir -p "$DEST"; chmod 700 "$DEST"

ITEMS=()
for p in \
  "$V/memory" "$V/.env" "$V/logs/interactions.jsonl" "$V/tg" "$V/config" \
  "@AGENT_USER_HOME@/.claude-token.env" "@AGENT_USER_HOME@/.ssh" \
  /etc/systemd/system/team-agent* /etc/systemd/system/teamos-* /etc/systemd/system/notify-failure@.service \
  /etc/ssh/sshd_config.d /usr/local/sbin/teamos-* ; do
  [ -e "$p" ] && ITEMS+=("$p")
done
[ ${#ITEMS[@]} -eq 0 ] && { echo "BACKUP: нечего бэкапить"; exit 1; }

tar --warning=no-file-changed \
    --exclude='*/.venv' --exclude='*/__pycache__' \
    -czf "$OUT.tmp" "${ITEMS[@]}" 2>/dev/null
RC=$?
# tar rc=1 = «файлы менялись при чтении» (живой бот пишет память) — не фатально
if [ $RC -gt 1 ] || [ ! -s "$OUT.tmp" ]; then
  rm -f "$OUT.tmp"
  echo "BACKUP FAILED rc=$RC"
  exit 1
fi
mv "$OUT.tmp" "$OUT"
chmod 600 "$OUT"
# проверка целостности архива — «архив создан» ≠ «архив читается»
if ! gzip -t "$OUT" 2>/dev/null; then echo "BACKUP CORRUPT: $OUT"; exit 1; fi
ln -sfn "$OUT" "$DEST/latest.tar.gz"
find "$DEST" -name 'teamos-*.tar.gz' -mtime +$KEEP -delete
echo "BACKUP OK: $OUT ($(du -h "$OUT" | cut -f1)), files=$(tar tzf "$OUT" | wc -l)"
