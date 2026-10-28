#!/usr/bin/env bash
# deploy.sh — выкатка из git на сервере, от root:
#   /home/teamagent/.local/share/team-agent/deploy.sh
# Тянет DEPLOY_BRANCH (main) из git, валидирует синтаксис и сборку промпта,
# мягко рестартит бота, проверяет здоровье и АВТОМАТИЧЕСКИ откатывается на прошлый
# коммит при сбое. Код на сервере руками не правим: всё, что не в git, этот
# скрипт затрёт (reset --hard) — и это намеренно (дрейф сервера от git = грабли).
set -uo pipefail

AGENT_USER="${AGENT_USER:-teamagent}"
REPO="${AGENT_HOME:-$(cd "$(dirname "$0")" && pwd)}"
BRANCH="${DEPLOY_BRANCH:-main}"
SVC=team-agent.service
G() { runuser -u "$AGENT_USER" -- git -C "$REPO" "$@"; }
U() { runuser -u "$AGENT_USER" -- env AGENT_HOME="$REPO" "$@"; }

[ "$(id -u)" = 0 ] || { echo "[deploy] запускайте от root"; exit 1; }

BEFORE=$(G rev-parse HEAD)
echo "[deploy] текущий:  $BEFORE"

if ! G fetch --quiet origin "$BRANCH"; then
  echo "[deploy] git fetch не удался (сеть/доступ) — деплой отменён, ничего не менял"
  exit 1
fi
G reset --hard --quiet "origin/$BRANCH"
AFTER=$(G rev-parse HEAD)
echo "[deploy] выкатываю: $AFTER"

rollback() {
  echo "[deploy] $1 — откат на $BEFORE"
  G reset --hard --quiet "$BEFORE"
}

# 1) валидация ДО рестарта: синтаксис python и bash
PYFILES=$(ls "$REPO"/bin/*.py "$REPO"/bin/lib/*.py "$REPO/bin/gl" "$REPO/bin/code-git" "$REPO"/connectors/*/*.py 2>/dev/null)
# shellcheck disable=SC2086
if ! U python3 -c 'import ast,sys; [ast.parse(open(f,encoding="utf-8").read(), f) for f in sys.argv[1:]]' $PYFILES; then
  rollback "СИНТАКС-ОШИБКА python"; exit 1
fi
for f in "$REPO"/bin/*.sh "$REPO"/bin/lib/*.sh "$REPO"/deploy/host/teamos-* "$REPO"/connectors/*/*.sh; do
  [ -e "$f" ] || continue
  bash -n "$f" || { rollback "СИНТАКС-ОШИБКА bash в $f"; exit 1; }
done

# 2) смоук: модуль бота импортируется, промпт собирается без дыр.
#    py_compile не ловит потерянные глобалы и сломанные шаблоны — импорт и вызов ловят.
if ! U python3 - "$REPO/bin/team_agent_bot.py" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("bot", sys.argv[1])
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)
bot.load_env()
for team in (True, False):
    p = bot.build_prompt("deploy", "проверка", "", in_group=False, team=team)
    assert p and "проверка" in p, "пустой промпт"
    assert "{{" not in p, "незаполненный плейсхолдер: " + p[p.index("{{"):p.index("{{") + 40]
bot.qa_disallowed(); bot.code_dirs("проверка"); bot.learnings_block()
print("smoke ok")
PY
then
  rollback "смоук сборки промпта не прошёл"; exit 1
fi

if [ "$BEFORE" != "$AFTER" ] && G diff --name-only "$BEFORE" "$AFTER" | grep -qE '^(deploy/|config/claude-config|connectors/[^/]+/systemd/)'; then
  echo "[deploy] ⚠️ изменились юниты/хост-скрипты/settings — после деплоя запустите ./install.sh"
fi

# 3) мягкий рестарт: бот дописывает начатые ответы (KillMode=mixed, TimeoutStopSec в юните)
MAINPID=$(systemctl show "$SVC" -p MainPID --value)
BUSY=0; [ "${MAINPID:-0}" != 0 ] && BUSY=$(pgrep -u "$AGENT_USER" -x claude -P "$MAINPID" | wc -l)
[ "$BUSY" -gt 0 ] && echo "[deploy] бот отвечает на $BUSY вопрос(ов) — жду, пока допишет"
systemctl restart "$SVC"
sleep 6

# 4) healthcheck: сервис active И нет краша/трейсбека в свежем журнале
UNHEALTHY=""
[ "$(systemctl is-active "$SVC")" != "active" ] && UNHEALTHY="сервис не active ($(systemctl is-active "$SVC"))"
if journalctl -u "$SVC" --since "8 seconds ago" --no-pager 2>/dev/null \
     | grep -qiE "traceback|critical|modulenotfound|unhandled|fatal|не заданы обязательные"; then
  UNHEALTHY="${UNHEALTHY:+$UNHEALTHY; }краш в журнале при старте"
fi

if [ -n "$UNHEALTHY" ]; then
  rollback "НЕЗДОРОВ ($UNHEALTHY)"
  systemctl restart "$SVC"
  sleep 4
  echo "[deploy] откат выполнен, сервис: $(systemctl is-active "$SVC")"
  exit 1
fi

echo "[deploy] OK — $SVC active на $AFTER"
