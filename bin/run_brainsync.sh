#!/bin/bash
# Duty: еженедельный brain-sync базы знаний (регламент — skill update-knowledge
# в репо знаний, если он есть). Запуск: team-agent-brainsync.timer (после codesync).
#
# Схема: канонический клон knowledge/ закрыт для записи (deny в настройках
# бота), поэтому claude правит одноразовую рабочую копию в sync_run/, а этот
# скрипт детерминированно делает ветку, коммит и push. Прямых пушей в main
# нет — только ветка brain-sync/YYYY-MM-DD и ссылка на PR владельцу.
set -uo pipefail
DUTY_NAME="brain-sync"
. "$(dirname "$0")/lib/duty.sh"

SYNC="$V/sync_run"
WORK="$SYNC/knowledge"
BRANCH="brain-sync/$(date +%Y-%m-%d)"
SKILL_PATH="$(duty_env BRAINSYNC_SKILL brain/skills/update-knowledge/SKILL.md)"
mkdir -p "$SYNC"

# 1. Свежий канонический клон → чистая рабочая копия на новой ветке.
if ! duty_workcopy "$WORK" "$BRANCH" > "$SYNC/pull.log" 2>&1; then
  duty_alert "⚠️ $AGENT_NAME: brain-sync не стартовал — не обновил/не склонировал knowledge: $(tail -c 200 "$SYNC/pull.log")"
  exit 1
fi
rm -f "$SYNC/pr_body.md"

REPOS=$(duty_code_repos | tr '\n' ' ')
PROMPT=$(duty_prompt duty-brainsync WORK="$WORK" SYNC="$SYNC" SKILL_PATH="$SKILL_PATH" \
          CODE_REPOS="${REPOS:-(код не подключён)}") || {
  duty_alert "⚠️ $AGENT_NAME/$DUTY_NAME: не собрался промпт prompts/duty-brainsync.md"; exit 1; }

# 2. claude правит рабочую копию.
cd "$V" || exit 1
duty_claude "$SYNC/claude_out.log" "$SYNC/claude_err.log" "$MODEL_DUTY" 1800 "$PROMPT" \
  "Read,Grep,Glob,Bash(git log:*),Bash(git -C:*),Bash(python3:*),Bash($V/bin/gl:*),Write(/$SYNC/**),Edit(/$SYNC/**)" \
  --add-dir "$WORK" --add-dir "$V/code" || exit 1

# 3. Детерминированные git-шаги.
if [ ! -s "$SYNC/pr_body.md" ]; then
  duty_alert "⚠️ $AGENT_NAME: brain-sync — claude отработал, но pr_body.md пуст. Смотри $SYNC/claude_out.log"
  exit 1
fi
if grep -q '^NO_CHANGES' "$SYNC/pr_body.md" || [ -z "$(git -C "$WORK" status --porcelain)" ]; then
  duty_send_owner "🧠 Brain-sync за неделю: изменений нет ($(head -c 300 "$SYNC/pr_body.md"))."
  exit 0
fi

# brain/ — то, что бот читает первым, поэтому тоже только через ревью
if URL=$(learn_to_branch "$WORK" "$BRANCH" "brain-sync $(date +%Y-%m-%d)" "$SYNC/pr_body.md" brain/ decisions.md); then
  LEARN_OUTSIDE=$(cat "$WORK/.git/learn_outside" 2>/dev/null)
  duty_send_owner "🧠 Brain-sync: снапшот недели ждёт ревью — открой PR: $URL${LEARN_OUTSIDE:+
⚠️ Правки вне brain/ и decisions.md НЕ взяты: $LEARN_OUTSIDE}
$(head -c 900 "$SYNC/pr_body.md")"
else
  rc=$?
  if [ "$rc" = 2 ]; then
    duty_send_owner "🧠 Brain-sync: изменений в brain/ и decisions.md нет."
  else
    duty_alert "⚠️ Brain-sync: push ветки $BRANCH не прошёл (deploy key без права записи?): $(tail -c 200 "$WORK/.git/learn_push.log" 2>/dev/null)"
    exit 1
  fi
fi
