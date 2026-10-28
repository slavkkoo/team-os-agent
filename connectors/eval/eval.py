#!/usr/bin/env python3
"""eval — харнесс качества ответов агента: свежие вопросы → ответы бота → LLM-судья → отчёт.

Usage:
  eval.py sample --out eval_run/sample.json [--size 10] (--pairs <pairs.json> | --set <eval-set.jsonl>)
      выборка: пары хелпдеска (ответ оператора = эталон) или ваш ручной набор {question, reference};
      тикеты из memory/helpdesk-learned-ids.txt (на них учился ticket-learn) — исключаются
  eval.py run --sample <sample.json> --out <answers.json>
      каждый вопрос — через ТОТ ЖЕ промпт и запуск, что у бота (полный режим команды, без записи);
      пишет после каждого ответа, перезапуск продолжает с места остановки
  eval.py judge --sample <sample.json> --answers <answers.json> --out <verdicts.json>
      судья — claude -p --model EVAL_MODEL_JUDGE с рубрикой connectors/eval/judge-rubric.md
  eval.py report --verdicts <verdicts.json>
  eval.py --selftest          без сети и без claude, на фикстурах

ЧЕСТНО ПРО СУДЬЮ: LLM-судья систематически ЗАВЫШАЕТ «галлюцинации» — верный по сути
ответ с деталями, которых нет в эталоне (короткий ответ оператора), он считает враньём.
Поэтому: каждый вердикт hallucination/wrong ревьюит человек; hold-out — из свежей недели,
а не из той, на которой учились; маленькая выборка каждую неделю лучше большой раз в квартал.
Включение: CONNECTOR_EVAL_ENABLED=1.
"""
import importlib.util
import json
import os
import re
import statistics
import subprocess
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import connector_common as cc  # noqa: E402

NAME = "eval"
HERE = os.path.dirname(os.path.abspath(__file__))
VERDICTS = ("correct", "partial", "honest_refusal", "wrong", "hallucination")
SAFE = ("correct", "partial", "honest_refusal")
CAVEAT = ("⚠️ Судья завышает «галлюцинации»: верный ответ с деталями сверх короткого эталона он "
          "считает выдумкой. Вердикты hallucination/wrong ниже — на ручной ревью, а не в метрику как есть.")


def _read_json(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _write_json(p, obj):
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def learned_ids(path=None):
    path = path or cc.path("memory", "helpdesk-learned-ids.txt")
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as f:
        return {x.strip() for x in f if x.strip()}


def make_sample(items, size, exclude):
    """Половина — разборы случаев (needs_logs), половина — общие вопросы; без обучающих тикетов."""
    ok = [p for p in items if len(p.get("question", "")) >= 25 and len(p.get("reference", "")) >= 45
          and str(p.get("id")) not in exclude]
    logs = [p for p in ok if p.get("needs_logs")][:size // 2]
    rest = [p for p in ok if not p.get("needs_logs")][:size - len(logs)]
    return [{"i": n + 1, **p} for n, p in enumerate(logs + rest)]


def items_from_pairs(path):
    return [{"id": str(p.get("ticket_id")), "question": p["question"][:2500],
             "reference": p.get("answer", "")[:2200], "needs_logs": bool(p.get("needs_logs"))}
            for p in _read_json(path).get("pairs", [])]


def items_from_set(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if line and not line.startswith("#"):
                d = json.loads(line)
                out.append({"id": str(d.get("id", f"set-{n}")), "question": d["question"],
                            "reference": d.get("reference", ""), "needs_logs": bool(d.get("needs_logs"))})
    return out


def load_bot():
    p = cc.path("bin", "team_agent_bot.py")
    spec = importlib.util.spec_from_file_location("team_agent_bot", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.load_env()
    return m


def run_answers(sample, out_path, bot):
    done = _read_json(out_path) if os.path.exists(out_path) else []
    have = {a["i"] for a in done}
    for s in sample:
        if s["i"] in have:
            continue
        t0 = time.time()
        try:
            q = s["question"]
            text, ok, err = bot.run_agent(bot.build_prompt("eval", q, "", in_group=False, team=True),
                                          team=True, dirs=bot.code_dirs(q, True))
        except Exception as e:  # прогон не должен падать целиком из-за одного вопроса
            text, ok, err = "", False, f"{type(e).__name__}: {e}"
        done.append({"i": s["i"], "id": s.get("id"), "answer": text, "ok": ok,
                     "latency_s": round(time.time() - t0), **({} if ok else {"error": (err or "")[-300:]})})
        _write_json(out_path, done)
        print(f"[{len(done)}/{len(sample)}] i={s['i']} {'ok' if ok else 'FAIL'}", flush=True)
    return done


def rubric():
    with open(os.path.join(HERE, "judge-rubric.md"), encoding="utf-8") as f:
        text = f.read()
    return text.replace("{{PRODUCT_NAME}}", cc.env("PRODUCT_NAME", "продукт"))


def extract_verdict(raw):
    try:
        j = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    except ValueError:
        return {"verdict": "judge_error", "raw": (raw or "")[:200]}
    if j.get("verdict") not in VERDICTS:
        j = {"verdict": "judge_error", "raw": json.dumps(j, ensure_ascii=False)[:200]}
    return j


def claude_judge(prompt):
    model = cc.env("EVAL_MODEL_JUDGE") or cc.env("MODEL_DUTY", "claude-sonnet-5-5")
    try:
        r = subprocess.run(["claude", "--model", model, "-p", prompt, "--disallowedTools",
                            "Bash", "Edit", "Write", "WebFetch", "WebSearch"],
                           capture_output=True, text=True, timeout=240)
        return r.stdout.strip()
    except (subprocess.TimeoutExpired, OSError) as e:
        return f"judge failed: {e}"


def judge(sample, answers, out_path, judge_fn=claude_judge):
    by_i = {s["i"]: s for s in sample}
    base = rubric()
    out = []
    for a in answers:
        s = by_i[a["i"]]
        prompt = (base + "\n\n=== ВОПРОС ===\n" + s["question"][:2500]
                  + "\n\n=== ЭТАЛОН (ответ оператора/команды) ===\n" + (s.get("reference") or "(нет)")[:2000]
                  + "\n\n=== ОТВЕТ АГЕНТА ===\n" + (a.get("answer") or "(пусто/ошибка)")[:2500])
        v = extract_verdict(judge_fn(prompt)) if a.get("ok") else {"verdict": "no_answer", "note": a.get("error", "")}
        out.append({"i": a["i"], "id": a.get("id"), "needs_logs": s.get("needs_logs"), **v})
        _write_json(out_path, out)
    return out


def summarize(verdicts):
    n = len(verdicts)
    if not n:
        return "Eval: вердиктов нет."
    c = Counter(v["verdict"] for v in verdicts)
    fm = [v["factual_match"] for v in verdicts if isinstance(v.get("factual_match"), (int, float))]
    safe = sum(c.get(k, 0) for k in SAFE)
    lines = [f"Eval: {n} вопросов · ✅ {c.get('correct', 0)} · 🟡 {c.get('partial', 0)} · "
             f"🤝 {c.get('honest_refusal', 0)} · ❌ {c.get('wrong', 0)} · 🔴 {c.get('hallucination', 0)}"
             + (f" · нет ответа {c['no_answer']}" if c.get("no_answer") else "")
             + (f" · ошибка судьи {c['judge_error']}" if c.get("judge_error") else ""),
             f"Безопасных (верно/частично/честный отказ): {round(100 * safe / n)}%"
             + (f" · factual_match {round(statistics.mean(fm), 1)}/10" if fm else "")]
    review = [v for v in verdicts if v["verdict"] in ("hallucination", "wrong")]
    if review:
        lines.append(CAVEAT)
        for v in review[:15]:
            lines.append(f"  i={v['i']} id={v.get('id')} {v['verdict']}: {str(v.get('note', ''))[:140]}")
    return "\n".join(lines)


# --- selftest ---

def selftest():
    import tempfile
    t = cc.Selftest(NAME)
    tmp = tempfile.mkdtemp()
    items = [{"id": str(k), "question": f"Как настроить напоминание о записи номер {k}?", "reference":
              "Напоминание настраивается в разделе Уведомления, по умолчанию за 24 часа до визита.",
              "needs_logs": k % 2 == 0} for k in range(1, 9)]
    items.append({"id": "short", "question": "алло", "reference": "x", "needs_logs": False})
    s = make_sample(items, 4, exclude={"1", "2"})
    t.eq(len(s), 4, "размер выборки")
    t.check(all(x["id"] not in ("1", "2", "short") for x in s), "обучающие и короткие исключены")
    t.eq(sum(1 for x in s if x["needs_logs"]), 2, "половина — разборы случаев")
    t.eq([x["i"] for x in s], [1, 2, 3, 4], "нумерация")

    setp = os.path.join(tmp, "set.jsonl")
    with open(setp, "w", encoding="utf-8") as f:
        f.write('# ручной набор\n{"question": "Можно ли записаться без телефона?", "reference": "Нет"}\n')
    t.eq(items_from_set(setp)[0]["id"], "set-2", "ручной набор jsonl")

    class FakeBot:
        calls = 0

        def build_prompt(self, asker, q, ctx, in_group=False, team=True):
            assert team and not in_group
            return "P:" + q

        def code_dirs(self, q, team):
            return []

        def run_agent(self, prompt, team=True, dirs=None):
            FakeBot.calls += 1
            if "номер 5" in prompt:
                raise RuntimeError("boom")
            return "Ответ: раздел Уведомления, за 24 часа; можно поменять на 2 часа.", True, ""

    ans_p = os.path.join(tmp, "answers.json")
    _write_json(ans_p, [{"i": 1, "id": s[0]["id"], "answer": "уже было", "ok": True}])
    answers = run_answers(s, ans_p, FakeBot())
    t.eq(FakeBot.calls, 3, "resume: уже отвеченный пропущен")
    t.check(any(not a["ok"] and "boom" in a.get("error", "") for a in answers), "сбой вопроса не роняет прогон")

    noisy = 'Вот оценка:\n```json\n{"verdict": "hallucination", "factual_match": 6, "hallucination": true, ' \
            '"note": "упомянул 2 часа, в эталоне нет"}\n```'
    t.eq(extract_verdict(noisy)["verdict"], "hallucination", "JSON из шумного вывода судьи")
    t.eq(extract_verdict("не JSON")["verdict"], "judge_error", "мусор → judge_error")
    t.eq(extract_verdict('{"verdict": "great"}')["verdict"], "judge_error", "неизвестный вердикт → judge_error")

    seq = iter([noisy, '{"verdict": "correct", "factual_match": 9}', '{"verdict": "partial", "factual_match": 5}'])
    seen = []
    ver = judge(s, answers, os.path.join(tmp, "v.json"), lambda p: seen.append(p) or next(seq))
    t.check("=== ЭТАЛОН" in seen[0] and "детали" in seen[0].lower(), "в промпте судьи рубрика и эталон")
    t.eq(len(seen), 3, "неотвеченные судье не отправляются")
    rep = summarize(ver)
    t.check("Eval: 4 вопросов" in rep and "🔴 1" in rep, "сводка по вердиктам")
    t.check("нет ответа 1" in rep, "сбой ответа виден в сводке")
    t.check("Безопасных (верно/частично/честный отказ): 50%" in rep, "доля безопасных")
    t.check(CAVEAT in rep, "оговорка про судью — в отчёте")
    t.report()


def main():
    a = sys.argv[1:]
    if a[:1] == ["--selftest"]:
        selftest()
        return
    cc.load_env()
    cc.require_enabled(NAME)
    if not a:
        sys.exit(__doc__)

    def opt(k, d=""):
        return a[a.index(k) + 1] if k in a and a.index(k) + 1 < len(a) else d
    cmd = a[0]
    if cmd == "sample":
        out = cc.require_run_dir(opt("--out"))
        if opt("--pairs"):
            items = items_from_pairs(opt("--pairs"))
        elif opt("--set"):
            items = items_from_set(opt("--set"))
        else:
            sys.exit("нужен --pairs <pairs.json> или --set <eval-set.jsonl>")
        sample = make_sample(items, int(opt("--size", cc.env("EVAL_SIZE", "10"))), learned_ids())
        _write_json(out, sample)
        print(f"sample: {len(sample)} → {out}")
    elif cmd == "run":
        run_answers(_read_json(opt("--sample")), cc.require_run_dir(opt("--out")), load_bot())
    elif cmd == "judge":
        judge(_read_json(opt("--sample")), _read_json(opt("--answers")), cc.require_run_dir(opt("--out")))
        print("judge done")
    elif cmd == "report":
        print(summarize(_read_json(opt("--verdicts"))))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
