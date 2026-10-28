#!/bin/bash
# Watchdog Team OS (teamos-watchdog.timer, каждые 10 мин): то, что иначе ломается молча.
#  - живость бота (team-agent.service)
#  - диск
#  - свежесть локального бэкапа (и, если настроено, офсайт-копии)
#  - доп. проверки из .env: WATCHDOG_TCP_CHECKS («имя=host:port …», например VPN до
#    внутреннего GitLab) и WATCHDOG_HTTP_CHECKS («имя=URL …»)
# Алертит на СМЕНУ состояния (упало / восстановилось), а не каждые 10 минут,
# и только после двух провалов подряд — чтобы не будить из-за одного сбоя сети.
# Шаблон: @AGENT_HOME@/@SBIN_DIR@ подставляет install.sh.
set -uo pipefail
ENV_FILE="@AGENT_HOME@/.env"
BACKUP_DIR=/var/backups/teamos
S=/var/lib/teamos-watchdog; mkdir -p "$S"
notify() { @SBIN_DIR@/teamos-notify "$1"; }
get() { grep -m1 "^$1=" "$ENV_FILE" 2>/dev/null | cut -d= -f2-; }

check() {   # имя  0|1(ok)  текст-проблемы
  local name="$1" ok="$2" msg="$3" key f
  key=$(printf '%s' "$name" | tr -c 'A-Za-z0-9._-' '_')
  f="$S/$key.down"
  if [ "$ok" = 1 ]; then
    if [ -f "$f" ]; then
      since=$(cat "$f"); rm -f "$f"
      notify "✅ Восстановилось: $name (лежало с $since)"
    fi
    rm -f "$S/$key.strike"
  else
    if [ ! -f "$S/$key.strike" ]; then date '+%d.%m %H:%M %Z' > "$S/$key.strike"; return; fi
    if [ ! -f "$f" ]; then
      cp "$S/$key.strike" "$f"
      notify "⚠️ $name: $msg (с $(cat "$f"))"
    fi
  fi
}

# 1. бот
for u in team-agent.service; do
  check "$u" "$(systemctl is-active --quiet "$u" && echo 1 || echo 0)" "сервис не active ($(systemctl is-active "$u")). journalctl -u $u -n 50"
done

# 2. диск
lim=$(get WATCHDOG_DISK_PCT); lim=${lim:-85}
use=$(df --output=pcent / | tail -1 | tr -dc 0-9)
check "диск" "$([ "$use" -lt "$lim" ] && echo 1 || echo 0)" "занято ${use}% корневого раздела"

# 3. локальный бэкап: последний архив моложе 36 ч
b="$BACKUP_DIR/latest.tar.gz"
age=999999; [ -e "$b" ] && age=$(( $(date +%s) - $(stat -L -c %Y "$b") ))
check "бэкап" "$([ "$age" -lt 129600 ] && echo 1 || echo 0)" "свежего архива нет больше 36 ч — systemctl status teamos-backup"

# 4. офсайт-копия: машина, которая забирает бэкап, после успешного pull трогает маркер
off=$(get WATCHDOG_OFFSITE_MAX_H)
if [ -n "$off" ]; then
  m="$BACKUP_DIR/.last_offsite_pull"
  age=999999; [ -f "$m" ] && age=$(( $(date +%s) - $(stat -c %Y "$m") ))
  check "офсайт-бэкап" "$([ "$age" -lt $(( off * 3600 )) ] && echo 1 || echo 0)" "бэкап не забирали больше ${off} ч"
fi

# 5. доп. сеть: TCP (VPN/внутренние сервисы) и HTTP
for item in $(get WATCHDOG_TCP_CHECKS); do
  name="${item%%=*}"; hp="${item#*=}"; host="${hp%:*}"; port="${hp##*:}"
  ok=1; timeout 6 bash -c "</dev/tcp/$host/$port" 2>/dev/null || ok=0
  check "$name" "$ok" "нет TCP-связи с $hp"
done
for item in $(get WATCHDOG_HTTP_CHECKS); do
  name="${item%%=*}"; url="${item#*=}"
  code=$(curl -s -o /dev/null -m 15 -w '%{http_code}' "$url" || true)
  check "$name" "$(case "$code" in 2??|3??) echo 1;; *) echo 0;; esac)" "HTTP $code от $url"
done
