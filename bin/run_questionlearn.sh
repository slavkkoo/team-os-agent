#!/bin/bash
# Duty: LEARN-FROM-QUESTIONS — раз в неделю смотрит свои же ответы в
# logs/interactions.jsonl, находит вопросы, на которые ушло много шагов
# (turns >= QUESTIONLEARN_MIN_TURNS, не разовые инциденты), и предлагает
# переиспользуемые заметки в базу знаний — чтобы в следующий раз отвечать за 1-2 шага.
# Два потока: верифицированное кодом → ветка learn-questions/YYYY-MM-DD + ссылка на PR;
# противоречия «база говорит X, код — Y» → отчёт владельцу, в базу не идут.
# Запуск: team-agent-questionlearn.timer. Ручной прогон: скрипт без аргументов.
set -uo pipefail
DUTY_NAME="learn-questions"
. "$(dirname "$0")/lib/duty.sh"

RUN="$V/questionlearn_run"
WORK="$RUN/knowledge"
STATE="$V/memory/questionlearn_state.json"
LOG="$V/logs/interactions.jsonl"
CAND="$RUN/candidates.txt"
MIN_TURNS="$(duty_env QUESTIONLEARN_MIN_TURNS 10)"
LEARN_PATHS="$(duty_env LEARN_PATHS "product/ brain/product/")"
mkdir -p "$RUN" "$V/memory"
[ -f "$LOG" ] || { echo "нет $LOG — учить нечего"; exit 0; }

# 1. Кандидаты: ok, turns >= MIN_TURNS, не разовый инцидент (без идентификаторов).
#    stdout питона — максимальный ts лога (новое состояние).
MAX_TS=$(python3 - "$LOG" "$STATE" "$CAND" "$MIN_TURNS" <<'PY'
import json, re, sys
log, state_path, out, min_turns = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
try:
    last_ts = json.load(open(state_path)).get("last_ts", "")
except Exception:
    last_ts = ""

# признаки разового инцидента (не переносимо в базу) — отсекаем
ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}|\b\d{6,}\b", re.I)
ONE_OFF = ("по логам", "trace", "трейс", "почему упал", "не создался", "у клиента",
           "у пользователя", "сегодня", "вчера", "диагност")

seen, cands, max_ts = set(), [], last_ts
for line in open(log, encoding="utf-8"):
    try:
        d = json.loads(line)
    except Exception:
        continue
    ts = d.get("ts", "")
    if ts > max_ts:
        max_ts = ts
    if ts <= last_ts:            # только новое с прошлого прогона
        continue
    t = d.get("turns")
    if not d.get("ok") or not isinstance(t, int) or t < min_turns:
        continue
    q = (d.get("question") or "").strip()
    if len(q) < 15:
        continue
    ql = q.lower()
    if ID_RE.search(q) or any(k in ql for k in ONE_OFF):
        continue
    key = ql[:45]
    if key in seen:
        continue
    seen.add(key)
    cands.append({"turns": t, "question": q[:400]})

cands.sort(key=lambda x: -x["turns"])
cands = cands[:12]              # кап на прогон — не заваливать ревью
with open(out, "w", encoding="utf-8") as f:
    for c in cands:
        f.write(f"[turns={c['turns']}] {c['question']}\n\n")
print(max_ts)
PY
) || { duty_alert "⚠️ $AGENT_NAME/$DUTY_NAME: разбор interactions.jsonl упал"; exit 1; }

save_state() { python3 -c 'import json,sys; json.dump({"last_ts": sys.argv[2]}, open(sys.argv[1], "w"))' "$STATE" "$MAX_TS"; }

NUM=$(grep -c '^\[turns=' "$CAND" 2>/dev/null || true)
if [ "${NUM:-0}" -eq 0 ]; then
  save_state   # двигаем состояние и без кандидатов — чтобы не перебирать старое
  duty_send_owner "🎓 Learn-from-questions: новых тяжёлых вопросов (≥$MIN_TURNS шагов) за неделю нет — учить нечего."
  exit 0
fi

# 2. Свежая рабочая копия knowledge + ветка.
BRANCH="learn-questions/$(date +%Y-%m-%d)"
if ! duty_workcopy "$WORK" "$BRANCH" > "$RUN/pull.log" 2>&1; then
  duty_alert "⚠️ $AGENT_NAME learn-questions: не обновил/не склонировал knowledge: $(tail -c 200 "$RUN/pull.log")"
  exit 1
fi
rm -f "$RUN/contradictions.md" "$RUN/pr_body.md"

ADD_DIRS=(--add-dir "$WORK")
CODE_LIST=""
for r in $(duty_code_repos); do
  ADD_DIRS+=(--add-dir "$V/code/$r"); CODE_LIST="${CODE_LIST:+$CODE_LIST, }$V/code/$r"
done

PROMPT=$(duty_prompt duty-questionlearn CAND="$CAND" CANDIDATES="@$CAND" WORK="$WORK" RUN="$RUN" \
          MIN_TURNS="$MIN_TURNS" LEARN_PATHS="$LEARN_PATHS" CODE_DIRS="${CODE_LIST:-(код не подключён — сверяй с базой и документацией, без кода факты в поток 1 не пиши)}") || {
  duty_alert "⚠️ $AGENT_NAME/$DUTY_NAME: не собрался промпт prompts/duty-questionlearn.md"; exit 1; }

# 3. Claude разбирает кандидатов. Лимит turns высокий — глубокая сверка с кодом
# идёт через субагентов; реальный бэкстоп — таймаут (unattended без таймаута опасен).
# Edit покрывает и создание файлов.
cd "$V" || exit 1
duty_claude "$RUN/claude_out.log" "$RUN/claude_err.log" "$MODEL_DUTY" 1200 "$PROMPT" \
  "Read,Grep,Glob,Bash(git log:*),Bash(git -C:*),Bash(ls:*),Edit(/$RUN/**),Write(/$RUN/**)" \
  "${ADD_DIRS[@]}" --max-turns 150
rc=$?
# Лимит подписки — не поломка, а «окно кончилось»: выходим ДО сдвига состояния,
# чтобы те же кандидаты разобрались в следующий прогон, а не потерялись.
[ "$rc" = 2 ] && exit 0
[ "$rc" != 0 ] && exit 1

# 4. Два потока.
save_state
CONTRA="$RUN/contradictions.md"
HAS_VERIFIED=0; [ -n "$(git -C "$WORK" status --porcelain)" ] && HAS_VERIFIED=1
HAS_CONTRA=0;   [ -s "$CONTRA" ] && ! grep -qx 'NONE' "$CONTRA" && HAS_CONTRA=1

# 4a. ВЕРИФИЦИРОВАНО → ветка + ссылка на PR (ревью человека), не прямо в main.
if [ "$HAS_VERIFIED" = "1" ]; then
  # shellcheck disable=SC2086
  if URL=$(learn_to_branch "$WORK" "$BRANCH" "learn-questions $(date +%Y-%m-%d): факты из тяжёлых вопросов" \
           "$RUN/pr_body.md" $LEARN_PATHS); then
    LEARN_OUTSIDE=$(cat "$WORK/.git/learn_outside" 2>/dev/null)
    duty_send_owner "🎓 Learn-questions: новые факты ждут ревью — открой PR: $URL${LEARN_OUTSIDE:+
⚠️ Правки вне $LEARN_PATHS НЕ взяты: $LEARN_OUTSIDE}
$(tail -c 900 "$RUN/pr_body.md" 2>/dev/null)"
  else
    rc=$?
    if [ "$rc" = 2 ]; then
      duty_send_owner "🎓 Learn-questions: модель правила только файлы вне $LEARN_PATHS — НЕ взяты: $(cat "$WORK/.git/learn_outside" 2>/dev/null)"
    else
      duty_alert "⚠️ Learn-questions: push ветки $BRANCH не прошёл (deploy key без права записи?): $(tail -c 200 "$WORK/.git/learn_push.log" 2>/dev/null)"
    fi
  fi
fi

# 4b. ПРОТИВОРЕЧИЯ → отчёт владельцу (НЕ вливается, ждёт вердикта).
if [ "$HAS_CONTRA" = "1" ]; then
  N_CONTRA=$(grep -c '^### Противоречие' "$CONTRA")
  duty_send_owner "⚠️ Learn-questions: найдено противоречий база↔код: $N_CONTRA — нужен вердикт (в базу НЕ вливал):

$(cat "$CONTRA")"
fi

if [ "$HAS_VERIFIED" = "0" ] && [ "$HAS_CONTRA" = "0" ]; then
  duty_send_owner "🎓 Learn-questions: разобрал $NUM вопросов — ни новых верифицированных фактов, ни противоречий."
fi
echo "learn-questions done (verified=$HAS_VERIFIED, contradictions=$HAS_CONTRA)"
