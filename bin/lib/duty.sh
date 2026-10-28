#!/bin/bash
# Общая библиотека duty. Подключение в начале run_*.sh:
#   . "$(dirname "$0")/lib/duty.sh"
#
# Зачем она: когда каждый duty сам склеивает PATH, читает .env, шлёт в Telegram
# и зовёт claude -p по-своему, половина вызовов оказывается без таймаута
# (зависший claude вешает duty навсегда), часть не проверяет код возврата —
# обрезанный ответ уходит в командный канал как нормальный дайджест, а упор
# в лимит подписки выглядит как «просто пустой день». Тишина ≠ успех.

V="${AGENT_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
K="$V/knowledge"
export AGENT_HOME="$V"
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
export CLAUDE_CONFIG_DIR="$V/.claude-config"

# duty_env KEY [default] — значение из .env (присваиванием, а не source: значение
# со спецсимволами при source выполнилось бы как команда; и без экспорта, чтобы
# секреты не утекали в дочерние процессы).
duty_env() {
  local key="$1" def="${2:-}" val
  val=$(grep -m1 "^${key}=" "$V/.env" 2>/dev/null | cut -d= -f2-)
  val="${val%\"}"; val="${val#\"}"; val="${val%\'}"; val="${val#\'}"
  printf '%s' "${val:-$def}"
}

OWNER_ID="$(duty_env OWNER_ID)"
AGENT_NAME="$(duty_env AGENT_NAME "Ассистент")"
MODEL_DUTY="$(duty_env MODEL_DUTY claude-sonnet-5-5)"
DUTY_NAME="${DUTY_NAME:-duty}"
TZ_ENV="$(duty_env TZ)"; [ -n "$TZ_ENV" ] && export TZ="$TZ_ENV"

# duty_send_to CHAT TEXT — отправка с нарезкой по лимиту Telegram (4096).
# Токен передаётся curl через конфиг на stdin, а не аргументом: иначе он виден в `ps`.
duty_send_to() {
  local cid="$1" text="$2" token chunk
  token="$(duty_env TG_BOT_TOKEN)"
  [ -z "$cid" ] || [ -z "$token" ] && { echo "duty_send_to: нет chat_id или TG_BOT_TOKEN — сообщение не отправлено" >&2; return 1; }
  while [ -n "$text" ]; do
    chunk="${text:0:3900}"; text="${text:3900}"
    printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$token" \
      | curl -s -m 30 -K - --data-urlencode "chat_id=${cid}" --data-urlencode "text=${chunk}" \
             --data-urlencode "disable_web_page_preview=true" > /dev/null
  done
}

# duty_send_owner TEXT — владельцу в личку (отчёты, ссылки на PR).
duty_send_owner() { duty_send_to "$OWNER_ID" "$1"; }

# duty_alert TEXT — техническая тревога владельцу в личку.
duty_alert() { duty_send_to "$OWNER_ID" "$1"; }

# duty_prompt ИМЯ [KEY=VALUE ...] — промпт из prompts/ИМЯ.md (см. bin/lib/render_prompt.py).
duty_prompt() { python3 "$V/bin/lib/render_prompt.py" "$@"; }

# duty_claude OUT ERR MODEL TIMEOUT PROMPT [ALLOWED_TOOLS] [EXTRA_ARGS...]
#
# Единственный способ звать claude -p из duty. Гарантирует, что прогон не может
# ни повиснуть навсегда, ни тихо отдать мусор дальше по конвейеру.
# Коды возврата: 0 ok · 1 ошибка/пусто · 2 упёрлись в лимит подписки · 3 таймаут.
# Вызывающий сам решает, что делать с 2 (обычно: не сдвигать состояние и ждать
# следующего прогона), но алерт владельцу библиотека уже отправила.
duty_claude() {
  local out="$1" err="$2" model="$3" tmo="$4" prompt="$5" tools="${6:-}"
  shift 6 2>/dev/null || shift $#
  local rc

  if [ -z "$prompt" ]; then
    duty_alert "⚠️ $AGENT_NAME/$DUTY_NAME: пустой промпт (шаблон не собрался) — прогон отменён."
    return 1
  fi
  if [ -n "$tools" ]; then
    timeout "$tmo" claude --model "$model" -p "$prompt" --allowedTools "$tools" "$@" \
      > "$out" 2> "$err"
  else
    timeout "$tmo" claude --model "$model" -p "$prompt" "$@" > "$out" 2> "$err"
  fi
  rc=$?

  # Лимит подписки проверяем ПЕРВЫМ: он может прийти и с ненулевым кодом, и с
  # нулевым (текстом в ответе) — иначе диагностируется как «claude упал».
  if grep -qiE "session limit|usage limit|rate limit|limit reached|hit your limit" "$out" "$err" 2>/dev/null; then
    duty_alert "⏸ $AGENT_NAME/$DUTY_NAME: упёрлись в лимит подписки — прогон отложен до следующего запуска."
    return 2
  fi
  if [ $rc -eq 124 ]; then
    duty_alert "⚠️ $AGENT_NAME/$DUTY_NAME: claude -p не уложился в ${tmo}s и был прерван. Результат НЕ отправлен."
    return 3
  fi
  if [ $rc -ne 0 ]; then
    duty_alert "⚠️ $AGENT_NAME/$DUTY_NAME: claude -p упал (rc=$rc). Хвост: $(tail -c 300 "$err" 2>/dev/null)"
    return 1
  fi
  if [ ! -s "$out" ]; then
    duty_alert "⚠️ $AGENT_NAME/$DUTY_NAME: пустой ответ claude. Хвост: $(tail -c 300 "$err" 2>/dev/null)"
    return 1
  fi
  return 0
}

# duty_code_repos — имена клонов из config/code-repos.txt, которые есть на диске.
duty_code_repos() {
  local f; f="$(duty_env CODE_REPOS "$V/config/code-repos.txt")"
  [ -f "$f" ] || return 0
  sed -e 's/#.*//' "$f" | awk 'NF>=3 {print $1}' | while read -r r; do
    [ -d "$V/code/$r/.git" ] && echo "$r"
  done
}

# duty_workcopy WORK BRANCH — свежий канонический клон knowledge/ → одноразовая
# рабочая копия на новой ветке. Канонический клон закрыт для записи (deny в
# настройках), поэтому claude правит только копию, а git-шаги делает скрипт.
duty_workcopy() {
  local work="$1" br="$2" origin
  git -C "$K" pull --ff-only --quiet || return 1
  origin=$(git -C "$K" remote get-url origin) || return 1
  rm -rf "$work"
  git clone --quiet "$K" "$work" || return 1
  git -C "$work" remote set-url origin "$origin"
  git -C "$work" checkout --quiet -b "$br"
}

# duty_compare_url BRANCH — ссылка «открыть PR» для GitHub/GitLab-ремоута knowledge.
duty_compare_url() {
  local br="$1" base url web hp
  base="$(duty_env KNOWLEDGE_BRANCH main)"
  url=$(git -C "$K" remote get-url origin 2>/dev/null)
  case "$url" in
    git@*:*) hp="${url#git@}"; web="https://${hp%%:*}/${hp#*:}" ;;
    ssh://git@*) hp="${url#ssh://git@}"; web="https://${hp%%[:/]*}/${hp#*/}" ;;
    https://*) web="${url#https://*@}"; [ "$web" = "$url" ] || web="https://$web" ;;
    *) echo "ветка $br (ремоут $url — откройте PR вручную)"; return ;;
  esac
  web="${web%.git}"
  case "$web" in
    *github.com*) echo "$web/compare/${base}...${br}?expand=1" ;;
    *) echo "$web/-/merge_requests/new?merge_request[source_branch]=${br}&merge_request[target_branch]=${base}" ;;
  esac
}

# duty_open_pr WORK BRANCH TITLE BODY_FILE — открыть PR на GitHub (коннектор github).
# Выключен (CONNECTOR_GITHUB_ENABLED≠1) или не вышло — rc=1, и вызывающий даёт compare-ссылку.
# stdout: URL PR. Причина неудачи — в $WORK/.git/pr_open.log.
duty_open_pr() {
  local work="$1" br="$2" title="$3" body="$4"
  [ "$(duty_env CONNECTOR_GITHUB_ENABLED)" = 1 ] || return 1
  python3 "$V/connectors/github/github_pr.py" open --repo-dir "$work" --branch "$br" \
    --base "$(duty_env KNOWLEDGE_BRANCH main)" --title "$title" --body-file "$body" 2> "$work/.git/pr_open.log"
}

# learn_to_branch WORK BRANCH TITLE BODY_FILE PATH...
# Новые знания — ТОЛЬКО через ветку и ревью человека: конвейеры, пушащие прямо
# в main, быстро наполняют канон фактами, которые уже разошлись с кодом.
# Коммитим лишь разрешённые пути (`git add -A` утащил бы всё подряд).
# stdout: ссылка на PR (коннектор github) или на создание PR; $WORK/.git/learn_outside — изменения вне разрешённых путей (не взяты).
# rc: 0 — ветка запушена, 2 — нечего коммитить, 1 — push не прошёл.
learn_to_branch() {
  local work="$1" br="$2" title="$3" body="$4"; shift 4
  git -C "$work" checkout --quiet -B "$br"
  local p
  # по одному пути: несуществующий путь иначе роняет весь `git add` и не берётся ничего
  for p in "$@"; do git -C "$work" add -- "$p" 2>/dev/null; done
  git -C "$work" status --porcelain | grep -v '^[AMRD] ' | cut -c4- | head -10 | tr '\n' ' ' > "$work/.git/learn_outside"
  git -C "$work" diff --cached --quiet && return 2
  { printf '%s\n\n' "$title"; head -c 60000 "$body" 2>/dev/null; } > "$work/.git/LEARN_MSG"
  git -C "$work" -c user.name="$AGENT_NAME (bot)" -c user.email="team-agent@localhost" \
    commit --quiet -F "$work/.git/LEARN_MSG" || return 1
  git -C "$work" push --quiet -u origin "$br" > "$work/.git/learn_push.log" 2>&1 || return 1
  duty_open_pr "$work" "$br" "$title" "$body" || duty_compare_url "$br"
}
