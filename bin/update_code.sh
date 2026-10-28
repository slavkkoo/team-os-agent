#!/bin/bash
# codesync: обновление read-only клонов кода по config/code-repos.txt
# (строки «имя url ветка [ключевые,слова]»). Нет клона — клонирует.
# Запуск: team-agent-codesync.timer (раз в неделю, до stalefacts и brain-sync).
# Неудача — алерт владельцу; обновление — короткий отчёт.
set -uo pipefail
DUTY_NAME="codesync"
. "$(dirname "$0")/lib/duty.sh"

LIST="$(duty_env CODE_REPOS "$V/config/code-repos.txt")"
[ -f "$LIST" ] || { echo "нет $LIST — код не подключён, синхронизировать нечего"; exit 0; }
mkdir -p "$V/code"

# Креды к HTTPS-ремоутам GitLab — через GIT_ASKPASS, а не через URL или
# -c credential.helper: так токен не попадает в командную строку git (виден в
# `ps` любому пользователю машины) и в журнал при падении. Для ssh-URL не нужен.
GITLAB_TOKEN="$(duty_env GITLAB_TOKEN)"
export GITLAB_TOKEN
ASKPASS=$(mktemp "${TMPDIR:-/tmp}/team-agent-askpass.XXXXXX")
chmod 700 "$ASKPASS"
cat > "$ASKPASS" <<'ASK'
#!/bin/bash
case "$1" in
  *[Uu]sername*) echo "oauth2" ;;
  *) printf '%s\n' "$GITLAB_TOKEN" ;;
esac
ASK
trap 'rm -f "$ASKPASS"' EXIT INT TERM
export GIT_ASKPASS="$ASKPASS"
export GIT_TERMINAL_PROMPT=0   # не зависать на интерактивном промпте в unattended

FAILED=""
UPDATED=""
while read -r NAME URL BRANCH _; do
  [ -z "${NAME:-}" ] && continue
  case "$NAME" in \#*|*/*|.*) continue ;; esac
  [ -z "${BRANCH:-}" ] && { echo "skip $NAME: нет ветки в строке"; continue; }
  CODE="$V/code/$NAME"
  if [ ! -d "$CODE/.git" ]; then
    if OUT=$(git clone --quiet --branch "$BRANCH" "$URL" "$CODE" 2>&1); then
      UPDATED="${UPDATED:+$UPDATED; }$NAME склонирован ($(git -C "$CODE" log -1 --format='%h %cd' --date=short))"
    else
      FAILED="${FAILED:+$FAILED, }$NAME"; echo "clone failed [$NAME]: $OUT"
    fi
    continue
  fi
  CUR=$(git -C "$CODE" rev-parse --abbrev-ref HEAD)
  if [ "$CUR" != "$BRANCH" ]; then
    git -C "$CODE" fetch --quiet origin "$BRANCH" && git -C "$CODE" checkout --quiet -B "$BRANCH" "origin/$BRANCH" \
      || { FAILED="${FAILED:+$FAILED, }$NAME"; echo "checkout $BRANCH failed [$NAME]"; continue; }
  fi
  if ! OUT=$(git -C "$CODE" pull --ff-only 2>&1); then
    FAILED="${FAILED:+$FAILED, }$NAME"
    echo "pull failed [$NAME]: $OUT"
    continue
  fi
  NEW=$(git -C "$CODE" log -1 --format="%h %cd" --date=short)
  echo "synced [$NAME]: $NEW"
  echo "$OUT" | grep -q "Already up to date" || UPDATED="${UPDATED:+$UPDATED; }$NAME -> $NEW"
done < <(sed -e 's/#.*//' "$LIST")

if [ -n "$FAILED" ]; then
  duty_alert "⚠️ $AGENT_NAME: не смог обновить код: $FAILED (сеть/VPN/токен?). Проверь и запусти: systemctl start team-agent-codesync"
  exit 1
fi
if [ -n "$UPDATED" ]; then
  duty_send_owner "✅ $AGENT_NAME: код обновлён — $UPDATED. Если drift-чекер базы знаний покраснеет — модель предметной области ждёт правок."
fi
echo "codesync done"
