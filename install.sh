#!/bin/bash
# install.sh — идемпотентная установка team-os-agent на Debian 12/13 (запускать от root).
#
# Репо должно лежать прямо в AGENT_HOME (deploy.sh потом делает git pull там же):
#   sudo -u teamagent git clone <url> /home/teamagent/.local/share/team-agent
#   cd /home/teamagent/.local/share/team-agent && sudo ./install.sh
#
# Что делает (повторный запуск безопасен — ничего не затирает, кроме сгенерированного):
#   1. создаёт пользователя AGENT_USER (если нет) и каталоги рантайма;
#   2. кладёт .env из .env.example и пустой ~/.claude-token.env (только если их нет);
#   3. рендерит .claude-config*/settings.json из config/*/settings.json.in;
#   4. создаёт venv с Telethon для чтения Telegram-папок;
#   5. рендерит systemd-юниты из deploy/systemd/*.in и connectors/*/systemd/*.in в SYSTEMD_DIR,
#      хост-скрипты из deploy/host/ в SBIN_DIR (подстановка @AGENT_USER@, @AGENT_HOME@, @TZ@ … через sed);
#      юниты коннекторов рендерятся всегда, но включаются только вручную (см. конец вывода);
#   6. daemon-reload. Юниты НЕ включает — печатает команды следующих шагов.
#
# Флаги:
#   --dry-run        только показать, что будет сделано (root не нужен)
#   --ssh-hardening  положить deploy/host/99-hardening.conf в sshd_config.d (вход только по ключу)
#   --no-venv        не создавать venv (Telegram-папки читать не будете)
# Переопределяемые переменные окружения: AGENT_USER, AGENT_USER_HOME, AGENT_HOME,
#   SYSTEMD_DIR, SBIN_DIR, SSHD_DIR, TZ.
set -euo pipefail

DRY=0; SSH_HARDEN=0; VENV=1
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    --ssh-hardening) SSH_HARDEN=1 ;;
    --no-venv) VENV=0 ;;
    -h|--help) sed -n '2,23p' "$0"; exit 0 ;;
    *) echo "неизвестный флаг: $a (см. --help)" >&2; exit 2 ;;
  esac
done

SRC="$(cd "$(dirname "$0")" && pwd)"
AGENT_USER="${AGENT_USER:-teamagent}"
if [ -z "${AGENT_USER_HOME:-}" ]; then
  AGENT_USER_HOME="$(getent passwd "$AGENT_USER" 2>/dev/null | cut -d: -f6 || true)"
  AGENT_USER_HOME="${AGENT_USER_HOME:-/home/$AGENT_USER}"
fi
AGENT_HOME="${AGENT_HOME:-$AGENT_USER_HOME/.local/share/team-agent}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
SBIN_DIR="${SBIN_DIR:-/usr/local/sbin}"
SSHD_DIR="${SSHD_DIR:-/etc/ssh/sshd_config.d}"

say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
run()  { if [ "$DRY" = 1 ]; then printf '  [dry-run] %s\n' "$*"; else "$@"; fi; }
as_user() { if [ "$DRY" = 1 ]; then printf '  [dry-run] (от %s) %s\n' "$AGENT_USER" "$*"; else runuser -u "$AGENT_USER" -- "$@"; fi; }

env_get() {   # env_get KEY — из $AGENT_HOME/.env (без source)
  [ -f "$AGENT_HOME/.env" ] || return 0
  grep -m1 "^$1=" "$AGENT_HOME/.env" 2>/dev/null | cut -d= -f2- | sed -e 's/^["'\'']//' -e 's/["'\'']$//' || true
}

if [ -z "${TZ:-}" ]; then
  TZ="$(env_get TZ)"
  [ -z "$TZ" ] && command -v timedatectl >/dev/null 2>&1 && TZ="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
  TZ="${TZ:-UTC}"
fi

sed_escape() { printf '%s' "$1" | sed -e 's/[|&\\]/\\&/g'; }
render() {    # render SRC DST MODE
  local src="$1" dst="$2" mode="$3"
  if [ "$DRY" = 1 ]; then printf '  [dry-run] render %s -> %s (mode %s)\n' "${src#"$SRC"/}" "$dst" "$mode"; return; fi
  local tmp; tmp="$(mktemp)"
  sed -e "s|@AGENT_USER@|$(sed_escape "$AGENT_USER")|g" \
      -e "s|@AGENT_USER_HOME@|$(sed_escape "$AGENT_USER_HOME")|g" \
      -e "s|@AGENT_HOME@|$(sed_escape "$AGENT_HOME")|g" \
      -e "s|@SBIN_DIR@|$(sed_escape "$SBIN_DIR")|g" \
      -e "s|@TZ@|$(sed_escape "$TZ")|g" "$src" > "$tmp"
  if grep -q '@[A-Z_]*@' "$tmp"; then
    echo "ОШИБКА: в $dst остались незаполненные плейсхолдеры: $(grep -o '@[A-Z_]*@' "$tmp" | sort -u | tr '\n' ' ')" >&2
    rm -f "$tmp"; exit 1
  fi
  install -m "$mode" "$tmp" "$dst"; rm -f "$tmp"
}

say "team-os-agent install $( [ "$DRY" = 1 ] && echo '(DRY-RUN)')"
say "  AGENT_USER=$AGENT_USER  AGENT_USER_HOME=$AGENT_USER_HOME"
say "  AGENT_HOME=$AGENT_HOME  TZ=$TZ"
say "  SYSTEMD_DIR=$SYSTEMD_DIR  SBIN_DIR=$SBIN_DIR"

if [ "$DRY" = 0 ]; then
  [ "$(id -u)" = 0 ] || { echo "запускайте от root (sudo ./install.sh) или с --dry-run" >&2; exit 1; }
  if [ "$SRC" != "$AGENT_HOME" ]; then
    echo "ОШИБКА: репо лежит в $SRC, а AGENT_HOME=$AGENT_HOME." >&2
    echo "Клонируйте репо прямо в AGENT_HOME (deploy.sh обновляет код там же) или задайте AGENT_HOME=$SRC." >&2
    exit 1
  fi
  for b in python3 git curl; do command -v "$b" >/dev/null || { echo "нет $b: apt install -y python3 python3-venv git curl" >&2; exit 1; }; done
  command -v claude >/dev/null || [ -x "$AGENT_USER_HOME/.local/bin/claude" ] \
    || say "ВНИМАНИЕ: не нашёл claude (Claude Code CLI) — установите до запуска бота, см. INSTALL.md"
elif [ "$SRC" != "$AGENT_HOME" ]; then
  say "  (dry-run: репо в $SRC, при настоящей установке оно должно лежать в AGENT_HOME)"
fi

step "1. Пользователь и каталоги"
if getent passwd "$AGENT_USER" >/dev/null 2>&1; then
  say "  пользователь $AGENT_USER есть"
else
  run useradd --create-home --home-dir "$AGENT_USER_HOME" --shell /bin/bash "$AGENT_USER"
fi
for d in "$AGENT_HOME" "$AGENT_HOME/memory" "$AGENT_HOME/memory/dm" "$AGENT_HOME/memory/digests" \
         "$AGENT_HOME/memory/voc" "$AGENT_HOME/memory/learnings/staged" "$AGENT_HOME/memory/learnings/approved" \
         "$AGENT_HOME/memory/learnings/rejected" "$AGENT_HOME/logs" "$AGENT_HOME/uploads" "$AGENT_HOME/code" \
         "$AGENT_HOME/tg" "$AGENT_HOME/config" "$AGENT_HOME/.claude-config" "$AGENT_HOME/.claude-config-public"; do
  [ -d "$d" ] && [ "$DRY" = 1 ] && continue
  as_user mkdir -p "$d"
done
run chmod 700 "$AGENT_HOME/memory" "$AGENT_HOME/tg" "$AGENT_HOME/logs" "$AGENT_HOME/uploads"

step "2. Секреты и конфиг (не перезаписываются)"
if [ -f "$AGENT_HOME/.env" ]; then
  say "  $AGENT_HOME/.env есть"
else
  run install -m 600 -o "$AGENT_USER" -g "$AGENT_USER" "$SRC/.env.example" "$AGENT_HOME/.env"
  say "  → заполните $AGENT_HOME/.env"
fi
if [ -f "$AGENT_USER_HOME/.claude-token.env" ]; then
  say "  $AGENT_USER_HOME/.claude-token.env есть"
else
  if [ "$DRY" = 1 ]; then
    say "  [dry-run] create $AGENT_USER_HOME/.claude-token.env (CLAUDE_CODE_OAUTH_TOKEN=, mode 600)"
  else
    printf '# claude setup-token → вставьте токен подписки\nCLAUDE_CODE_OAUTH_TOKEN=\n' > "$AGENT_USER_HOME/.claude-token.env"
    chown "$AGENT_USER:$AGENT_USER" "$AGENT_USER_HOME/.claude-token.env"; chmod 600 "$AGENT_USER_HOME/.claude-token.env"
  fi
  say "  → вставьте токен из «claude setup-token» в $AGENT_USER_HOME/.claude-token.env"
fi
[ -f "$AGENT_HOME/config/code-repos.txt" ] || say "  (код не подключён: cp config/code-repos.txt.example config/code-repos.txt и поправьте — необязательно)"

step "3. Настройки Claude Code (изоляция режимов: команда / публичный)"
render "$SRC/config/claude-config/settings.json.in" "$AGENT_HOME/.claude-config/settings.json" 644
render "$SRC/config/claude-config-public/settings.json.in" "$AGENT_HOME/.claude-config-public/settings.json" 644
run chown "$AGENT_USER:$AGENT_USER" "$AGENT_HOME/.claude-config/settings.json" "$AGENT_HOME/.claude-config-public/settings.json"

step "4. venv с Telethon (чтение Telegram-папок для дайджеста и VoC)"
if [ "$VENV" = 0 ]; then
  say "  пропущено (--no-venv)"
elif [ -x "$AGENT_HOME/.venv/bin/python3" ] && [ "$DRY" = 0 ] && "$AGENT_HOME/.venv/bin/python3" -c 'import telethon' 2>/dev/null; then
  say "  venv с telethon уже есть"
else
  as_user python3 -m venv "$AGENT_HOME/.venv"
  as_user "$AGENT_HOME/.venv/bin/pip" install --quiet --upgrade pip telethon
fi

step "5. systemd-юниты и хост-скрипты"
for f in "$SRC"/deploy/systemd/*.in; do
  name="$(basename "$f" .in)"
  render "$f" "$SYSTEMD_DIR/$name" 644
done
# коннекторы: юниты рендерятся всегда (идемпотентно), таймеры сами не включаются
for f in "$SRC"/connectors/*/systemd/*.in; do
  [ -e "$f" ] || continue
  render "$f" "$SYSTEMD_DIR/$(basename "$f" .in)" 644
done
for f in teamos-notify teamos-unit-failed teamos-watchdog.sh teamos-backup.sh; do
  render "$SRC/deploy/host/$f" "$SBIN_DIR/$f" 755
done
if [ "$SSH_HARDEN" = 1 ]; then
  run install -m 644 "$SRC/deploy/host/99-hardening.conf" "$SSHD_DIR/99-hardening.conf"
  if [ "$DRY" = 0 ]; then
    if sshd -t; then say "  sshd_config валиден; применить: systemctl reload ssh (проверьте вход по ключу в ОТДЕЛЬНОЙ сессии)"
    else rm -f "$SSHD_DIR/99-hardening.conf"; echo "sshd -t не прошёл — hardening убран" >&2; exit 1; fi
  fi
else
  say "  SSH-харденинг не трогал (флаг --ssh-hardening, см. INSTALL.md)"
fi
if command -v systemctl >/dev/null 2>&1 && [ "$SYSTEMD_DIR" = /etc/systemd/system ]; then
  run systemctl daemon-reload
fi

step "6. Проверки конфигурации"
for k in TG_BOT_TOKEN TG_BOT_USERNAME OWNER_ID TEAM_IDS AGENT_NAME TEAM_NAME; do
  [ -n "$(env_get "$k")" ] || say "  ✗ $k не задан в .env"
done
[ -d "$AGENT_HOME/knowledge/.git" ] && say "  ✓ knowledge/ склонирован" || say "  ✗ knowledge/ не склонирован (шаг «База знаний» в INSTALL.md)"
[ -s "$AGENT_USER_HOME/.claude-token.env" ] && grep -q '^CLAUDE_CODE_OAUTH_TOKEN=.' "$AGENT_USER_HOME/.claude-token.env" 2>/dev/null \
  && say "  ✓ токен Claude есть" || say "  ✗ токен Claude не вставлен"

step "7. Коннекторы (все выключены по умолчанию, см. connectors/README.md)"
CONN_TIMERS=""
conn_cfg() {   # conn_cfg ИМЯ ФАЙЛ — включён ли и есть ли его конфиг
  [ -f "$AGENT_HOME/config/$2" ] || say "    ✗ нет config/$2 (cp config/${2%.json}.example.json config/$2)"
}
for c in logs-metrics product-api helpdesk client-reports prod-digest eval github; do
  flag="CONNECTOR_$(printf '%s' "$c" | tr 'a-z-' 'A-Z_')_ENABLED"
  [ "$(env_get "$flag")" = 1 ] || continue
  py=$(ls "$SRC/connectors/$c"/*.py 2>/dev/null | head -1)
  if [ -n "$py" ] && st=$(python3 "$py" --selftest 2>&1 | tail -1); then say "  ✓ $c включён: $st"; else say "  ✗ $c включён, selftest не прошёл: $st"; fi
  case "$c" in
    product-api) conn_cfg "$c" product-api.json
                 if [ "$(env_get PRODUCT_API_WRITE_ENABLED)" = 1 ]; then CONN_TIMERS="$CONN_TIMERS team-agent-productapi-cleanup.timer"; fi ;;
    helpdesk) conn_cfg "$c" helpdesk.json; CONN_TIMERS="$CONN_TIMERS team-agent-ticketlearn.timer" ;;
    client-reports) conn_cfg "$c" client-reports.json; CONN_TIMERS="$CONN_TIMERS team-agent-clientreports.timer" ;;
    prod-digest) CONN_TIMERS="$CONN_TIMERS team-agent-proddigest.timer" ;;
    eval) CONN_TIMERS="$CONN_TIMERS team-agent-eval.timer" ;;
  esac
done
if [ -n "$(env_get MCP_CONFIG)" ]; then
  if [ "$DRY" = 1 ]; then say "  [dry-run] MCP: проверка python3 connectors/mcp/mcp_check.py check"
  else (cd "$AGENT_HOME" && AGENT_HOME="$AGENT_HOME" python3 "$SRC/connectors/mcp/mcp_check.py" check 2>&1 | sed 's/^/  MCP: /') || true; fi
fi
if [ -z "$CONN_TIMERS" ]; then say "  включённых коннекторов с таймерами нет"; fi

DUTIES="digest voc codesync stalefacts brainsync decisions questionlearn mergereview"
TIMERS=""; for d in $DUTIES; do TIMERS="$TIMERS team-agent-$d.timer"; done
cat <<EOF

Готово$( [ "$DRY" = 1 ] && echo ' (dry-run: ничего не изменено)'). Дальше:
  1. Заполните $AGENT_HOME/.env и $AGENT_USER_HOME/.claude-token.env (chmod 600).
  2. Склонируйте базу знаний в $AGENT_HOME/knowledge (INSTALL.md, шаг «База знаний»).
  3. Проверьте ответ без Telegram:
       sudo -u $AGENT_USER $AGENT_HOME/bin/ask.sh "Что ты знаешь о продукте?"
  4. Запустите бота и служебные таймеры:
       systemctl enable --now team-agent.service teamos-watchdog.timer teamos-backup.timer
  5. Включите нужные duty (каждая работает и по отдельности):
       systemctl enable --now$TIMERS
  6. Логи:  journalctl -u team-agent -f      Таймеры:  systemctl list-timers 'team-agent-*'
EOF
if [ -n "$CONN_TIMERS" ]; then
  say "  7. Таймеры включённых коннекторов (сначала прогоните вручную: systemctl start <имя>.service):"
  say "       systemctl enable --now$CONN_TIMERS"
fi
