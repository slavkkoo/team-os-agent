#!/bin/bash
# Duty: pending-решения к встрече + поправки из чата, ждущие promote/reject.
# Без claude: чистый grep по decisions.md — детерминированно и бесплатно.
# Запуск: team-agent-decisions.timer (поставьте за час до вашей еженедельной встречи).
set -uo pipefail
DUTY_NAME="decisions"
. "$(dirname "$0")/lib/duty.sh"

DECISIONS="$K/$(duty_env DECISIONS_FILE decisions.md)"
MEETING="$(duty_env DECISIONS_MEETING "еженедельной встрече")"
# строка статуса; по умолчанию ловит «Вердикт…: pending», «**Статус:** proposed», «Status: pending»
PENDING_RE="$(duty_env DECISIONS_PENDING_RE '(Вердикт|Статус|Status|Verdict)[^:]*:[ *]*(pending|proposed)')"

# Свежий decisions.md; если pull не прошёл — работаем по локальной копии, но говорим об этом.
STALE=""
if ! git -C "$K" pull --ff-only > /dev/null 2>&1; then
  STALE=" (git pull не прошёл — список по локальной копии)"
fi

if [ -f "$DECISIONS" ]; then
  # Нерешённое — это эпизоды под заголовком «### …» с РЕАЛЬНЫМ статусом pending/proposed:
  #   - строки внутри ```-блоков пропускаем (там образцы формата);
  #   - заголовки-шаблоны («D-NN — <…>», «YYYY-MM-DD — <…>») не считаются эпизодами;
  #   - перечень вариантов («proposed | ratified», «pending | agree | disagree») — образец
  #     формата, а не статус: исключаем строку, где pending/proposed стоит рядом с «|».
  # Принципы (### D-…) и эпизоды Decision log выводим раздельно; каждый заголовок — один раз.
  # Без интервалов {0,2} — mawk их не знает.
  PENDING=$(awk -v re="$PENDING_RE" '
    /^[ \t]*```/ { fence = !fence; next }
    fence { next }
    /^###? / { title=$0; sub(/^#+ /, "", title); tmpl = (title ~ /D-NN|YYYY-MM-DD|<[^>]*>/); next }
    tmpl || title == "" || done[title] { next }
    $0 ~ re && $0 !~ /(pending|proposed)[ .*`]*[|]/ && $0 !~ /[|][ *`]*(pending|proposed)/ {
      done[title] = 1
      if (title ~ /^D-[0-9]/) pr[++np] = title; else ep[++ne] = title
    }
    END {
      if (np) { printf "Принципы ждут ратификации (proposed): %d\n", np; for (i = 1; i <= np; i++) printf "%d. %s\n", i, pr[i] }
      if (ne) { printf "Эпизоды ждут вердикта agree/disagree: %d\n", ne; for (i = 1; i <= ne; i++) printf "%d. %s\n", i, ep[i] }
    }
  ' "$DECISIONS")
  N_PENDING=$(printf '%s\n' "$PENDING" | grep -c '^[0-9]' || true)
  PENDING=$(printf '%s' "$PENDING" | head -c 3300)
  if [ -z "$PENDING" ]; then
    duty_send_owner "🗂 К ${MEETING}: нерешённых принципов и эпизодов в $(basename "$DECISIONS") нет.${STALE}"
  else
    duty_send_owner "🗂 К ${MEETING} — ждут решения: ${N_PENDING}
${PENDING}
Файл: $(basename "$DECISIONS").${STALE}"
  fi
else
  echo "нет $DECISIONS — пропускаю решения"
fi

# Поправки людей из чата (M4): staged без напоминания висят месяцами
STAGED_DIR="$V/memory/learnings/staged"
if ls "$STAGED_DIR"/*.md >/dev/null 2>&1; then
  LIST=$(for f in "$STAGED_DIR"/*.md; do
    slug=$(basename "$f" .md)
    who=$(grep -m1 '^from:' "$f" | cut -d' ' -f2-)
    what=$(awk '/^## Поправка/{getline; print; exit}' "$f" | cut -c1-110)
    printf '▪️ %s (%s): %s\n   → promote %s / reject %s\n' "$slug" "$who" "$what" "$slug" "$slug"
  done | head -c 3300)
  duty_send_owner "📝 Поправки из чата ждут решения ($(ls "$STAGED_DIR"/*.md | wc -l | tr -d ' ')). Ответь боту в личку командой promote <slug> или reject <slug>:
$LIST"
fi
echo "decisions reminder sent ($(date +%F))"
