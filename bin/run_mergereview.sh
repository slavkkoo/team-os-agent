#!/bin/bash
# Duty: ревью веток конвейеров знаний (brain-sync/*, learn-questions/* и т.п.)
# второй моделью перед тем, как человек их смёржит. Запуск: team-agent-mergereview.timer.
#
# Предохранители (нарушение любого = НЕ мёржим, отчёт владельцу):
#   1. Дифф трогает ТОЛЬКО разрешённые пути (MERGEREVIEW_ALLOWED_RE) — детерминированно.
#   2. Ревью делает модель MODEL_REVIEW со строгим JSON-вердиктом merge_ok.
#   3. Тот же коммит ветки повторно не ревьюится (иначе одна и та же ветка
#      съедает токены каждый день).
#   4. Авто-мёрж ВЫКЛЮЧЕН по умолчанию: конвейеры не должны попадать в main без
#      человека. MERGEREVIEW_AUTOMERGE=1 включает мёрж одобренного (merge --no-ff,
#      проверка конфликтов и того, что main реально изменился).
#   5. Отчёт владельцу всегда: что одобрено/смёржено и почему нет.
set -uo pipefail
DUTY_NAME="merge-review"
. "$(dirname "$0")/lib/duty.sh"

RUN="$V/mergereview_run"
WORK="$RUN/knowledge"
STATE="$RUN/reviewed_heads"          # строки «<sha> <verdict> <дата> <ветка>»
BASE="$(duty_env KNOWLEDGE_BRANCH main)"
PREFIXES="$(duty_env MERGEREVIEW_PREFIXES "brain-sync/ learn-questions/")"
ALLOWED_RE="$(duty_env MERGEREVIEW_ALLOWED_RE '^(brain|product)/|^decisions\.md$')"
AUTOMERGE="$(duty_env MERGEREVIEW_AUTOMERGE 0)"
MODEL_REVIEW="$(duty_env MODEL_REVIEW "$(duty_env MODEL_ANSWER claude-opus-5-5)")"
MAX_BRANCHES=3
mkdir -p "$RUN"; touch "$STATE"

ORIGIN=$(git -C "$K" remote get-url origin 2>/dev/null) || { duty_alert "⚠️ $AGENT_NAME/$DUTY_NAME: нет knowledge/ или ремоута origin"; exit 1; }
rm -rf "$WORK"
if ! git clone --quiet "$ORIGIN" "$WORK" > "$RUN/clone.log" 2>&1; then
  duty_alert "⚠️ $AGENT_NAME/$DUTY_NAME: не склонировал knowledge: $(tail -c 200 "$RUN/clone.log")"
  exit 1
fi
git -C "$WORK" checkout --quiet "$BASE" || { duty_alert "⚠️ $AGENT_NAME/$DUTY_NAME: нет ветки $BASE"; exit 1; }

CODE_LIST=""
for r in $(duty_code_repos); do CODE_LIST="${CODE_LIST:+$CODE_LIST, }$V/code/$r"; done

n=0
for prefix in $PREFIXES; do
  for ref in $(git -C "$WORK" for-each-ref --format='%(refname:short)' "refs/remotes/origin/${prefix}*"); do
    [ "$n" -ge "$MAX_BRANCHES" ] && break 2
    BR="${ref#origin/}"
    HEAD_SHA=$(git -C "$WORK" rev-parse "$ref")
    grep -q "^$HEAD_SHA " "$STATE" && continue                      # уже ревьюили этот коммит
    AHEAD=$(git -C "$WORK" rev-list --count "$BASE..$ref")
    [ "$AHEAD" = 0 ] && continue                                     # уже влита
    n=$((n + 1))
    URL=$(duty_compare_url "$BR")

    # Предохранитель 1: только разрешённые пути
    CHANGED=$(git -C "$WORK" diff --name-only "$BASE...$ref")
    BAD=$(printf '%s\n' "$CHANGED" | grep -vE "$ALLOWED_RE" | grep -v '^$' || true)
    if [ -n "$BAD" ]; then
      echo "$HEAD_SHA blocked $(date +%F) $BR" >> "$STATE"
      duty_send_owner "🛑 Ревью $BR: ветка меняет файлы вне разрешённых путей — не ревьюю и не мёржу:
$BAD
Разберись вручную: $URL"
      continue
    fi

    DIFF="$RUN/diff_$(echo "$BR" | tr '/' '_').txt"
    git -C "$WORK" diff "$BASE...$ref" > "$DIFF"
    PROMPT=$(duty_prompt duty-mergereview BRANCH="$BR" BASE="$BASE" DIFF="$DIFF" WORK="$WORK" \
              CODE_DIRS="${CODE_LIST:-(код не подключён)}") || {
      duty_alert "⚠️ $AGENT_NAME/$DUTY_NAME: не собрался промпт prompts/duty-mergereview.md"; exit 1; }

    cd "$V" || exit 1
    duty_claude "$RUN/verdict_raw.txt" "$RUN/claude_err.log" "$MODEL_REVIEW" 600 "$PROMPT" \
      "Read,Grep,Glob,Bash(git log:*)"
    rc=$?
    [ "$rc" = 2 ] && exit 0          # лимит подписки: ветка подождёт следующего прогона
    [ "$rc" != 0 ] && exit 1

    VERDICT=$(python3 - "$RUN/verdict_raw.txt" <<'PY'
import json, sys
raw = open(sys.argv[1], encoding="utf-8").read()
try:
    j = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
except Exception:
    j = {}
ok = "yes" if j.get("merge_ok") is True else "no"
summary = str(j.get("summary", "вердикт не распознан"))[:500].replace("\n", " ")
issues = "\n".join("- " + str(x) for x in (j.get("issues") or []))[:1500]
print(ok)
print(summary)
print(issues)
PY
)
    MERGE_OK=$(printf '%s\n' "$VERDICT" | sed -n 1p)
    SUMMARY=$(printf '%s\n' "$VERDICT" | sed -n 2p)
    ISSUES=$(printf '%s\n' "$VERDICT" | sed -n '3,$p')

    if [ "$MERGE_OK" != "yes" ]; then
      echo "$HEAD_SHA rejected $(date +%F) $BR" >> "$STATE"
      duty_send_owner "🟡 Ревью $BR ($AHEAD коммит.): НЕ пропускаю.
Вердикт: $SUMMARY
$ISSUES
Ветка не тронута: $URL"
      continue
    fi

    if [ "$AUTOMERGE" != "1" ]; then
      echo "$HEAD_SHA approved $(date +%F) $BR" >> "$STATE"
      duty_send_owner "🟢 Ревью $BR ($AHEAD коммит.): замечаний по сути нет — можно мёржить PR: $URL
Вердикт: $SUMMARY${ISSUES:+
Мелочи: $ISSUES}"
      continue
    fi

    # Авто-мёрж: результат проверяем — мёрж мог упасть на конфликте, а «успешный»
    # push неизменённого main выглядел бы как влитые знания.
    if ! git -C "$WORK" -c user.name="$AGENT_NAME (review)" -c user.email="team-agent@localhost" \
         merge --no-ff --quiet -m "merge $BR (ревью $MODEL_REVIEW одобрил): $SUMMARY" "$ref" > "$RUN/merge.log" 2>&1; then
      CONFLICTS=$(git -C "$WORK" diff --name-only --diff-filter=U | head -10 | tr '\n' ' ')
      git -C "$WORK" merge --abort 2>/dev/null || true
      echo "$HEAD_SHA conflict $(date +%F) $BR" >> "$STATE"
      duty_send_owner "🛑 $BR: ревью одобрило, но мёрж в $BASE упал на КОНФЛИКТЕ ($CONFLICTS). Ничего не влито: $URL"
      continue
    fi
    if [ "$(git -C "$WORK" rev-parse HEAD)" = "$(git -C "$WORK" rev-parse "origin/$BASE")" ]; then
      duty_send_owner "🛑 $BR: после мёржа $BASE не изменился — ничего не влито."
      continue
    fi
    if ! git -C "$WORK" push --quiet origin "$BASE" > "$RUN/push.log" 2>&1; then
      git -C "$WORK" reset --quiet --hard "origin/$BASE"
      duty_alert "⚠️ $BR: ревью одобрило, но push $BASE упал (защищённая ветка / deploy key без записи?): $(tail -c 200 "$RUN/push.log")"
      continue
    fi
    echo "$HEAD_SHA merged $(date +%F) $BR" >> "$STATE"
    git -C "$K" pull --ff-only --quiet 2>/dev/null || true   # канон бота сразу свежий
    duty_send_owner "✅ $BR смёржена в $BASE после ревью ($AHEAD коммит.).
Вердикт: $SUMMARY${ISSUES:+
Замечания: $ISSUES}
Откат при необходимости: git revert -m 1 <merge-коммит>."
  done
done
echo "merge-review done: reviewed=$n"
