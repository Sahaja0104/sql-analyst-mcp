"""Measure how accurately an LLM turns English questions into SQL for this database.

How it works (no API key needed):
  1. `prompt`  writes a text file with ONLY the questions. You paste it into a new
               Claude Desktop chat (which has the sql-analyst tools) and save the
               JSON answer Claude replies with.
  2. `grade`   runs each answer through our validator and database, runs the
               hand-written "gold" query too, and compares the two result tables.

Run from the project root:
  python evals/run_eval.py prompt --difficulty easy
  python evals/run_eval.py grade evals/answers_easy.json evals/answers_medium.json evals/answers_hard.json --model "Claude Sonnet"

A generated query counts as CORRECT when it returns the same data as the gold query.
The SQL text and the column names may differ. Rules used when comparing:
  - same number of columns, in the same order
  - same rows (row order only matters for questions marked "ordered")
  - numbers are compared after rounding to 2 decimals
"""

import argparse
import datetime
import decimal
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from sql_analyst.db import QueryError, execute_select
from sql_analyst.validator import ValidationError, validate_query

EVAL_DIR = Path(__file__).resolve().parent
QUESTIONS_PATH = EVAL_DIR / "questions.json"
DIFFICULTIES = ["easy", "medium", "hard"]

PROMPT_TEMPLATE = """You are being evaluated on text-to-SQL accuracy. You have the "sql-analyst" tools, which give read-only access to a PostgreSQL database (a music store called Chinook).

For EACH question below:
1. Use get_schema if you need table or column names.
2. Write ONE PostgreSQL SELECT query that answers it. You may test it with run_query.
3. Return exactly the columns the question asks for, in the order the question lists them.

When you are done, reply with ONLY one JSON array inside a ```json code block, with one object per question and the final SQL you believe is correct:

[{{"id": "{first_id}", "sql": "SELECT ..."}}, {{"id": "{second_id}", "sql": "SELECT ..."}}]

Do not ask me questions, and do not add explanations outside the code block.

QUESTIONS
{questions}
"""


# --------------------------------------------------------------- loading ---
def load_questions() -> list[dict]:
    with open(QUESTIONS_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_answers(paths: list[str]) -> dict[str, str]:
    """Read one or more answer files. Later files override earlier ones for the same id."""
    answers: dict[str, str] = {}
    for path in paths:
        if not Path(path).exists():
            sys.exit(
                f"Answer file not found: {path}\n"
                "Paste the matching prompt file into Claude Desktop and save Claude's JSON reply under that name first."
            )
        text = Path(path).read_text(encoding="utf-8-sig")
        match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.S)  # Claude's code block
        candidate = match.group(1) if match else text[text.find("["): text.rfind("]") + 1]
        try:
            items = json.loads(candidate)
        except json.JSONDecodeError as exc:
            sys.exit(f"Could not read JSON from {path}: {exc}")
        for item in items:
            if isinstance(item, dict) and "id" in item and "sql" in item:
                answers[str(item["id"]).strip().upper()] = str(item["sql"])
    return answers


# ------------------------------------------------------------- comparing ---
def _norm(value):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, (float, decimal.Decimal)):
        return round(float(value), 2)
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return str(value)


def _normalized_rows(result) -> list[tuple]:
    return [tuple(_norm(v) for v in row) for row in result.rows]


def compare(gold, got, ordered: bool) -> tuple[bool, str]:
    """Return (is_correct, reason_if_not)."""
    if gold.truncated or got.truncated:
        return False, "result has more rows than the 100-row limit, so it cannot be compared"
    if len(gold.columns) != len(got.columns):
        return False, f"wrong number of columns (expected {len(gold.columns)}, got {len(got.columns)})"

    gold_rows, got_rows = _normalized_rows(gold), _normalized_rows(got)
    if not ordered:
        gold_rows, got_rows = sorted(gold_rows, key=repr), sorted(got_rows, key=repr)
    if gold_rows == got_rows:
        return True, ""

    if len(gold_rows) != len(got_rows):
        return False, f"wrong number of rows (expected {len(gold_rows)}, got {len(got_rows)})"
    for expected, actual in zip(gold_rows, got_rows):
        if expected != actual:
            order_hint = "" if ordered else " (rows compared in sorted order)"
            return False, f"values differ{order_hint}: expected {expected}, got {actual}"
    return False, "values differ"


def grade_one(question: dict, sql: str | None) -> dict:
    record = {
        "id": question["id"],
        "difficulty": question["difficulty"],
        "question": question["question"],
        "generated_sql": sql,
        "status": "",
        "reason": "",
    }
    if not sql or not sql.strip():
        record.update(status="missing", reason="no answer was provided")
        return record
    try:
        cleaned = validate_query(sql)
    except ValidationError as exc:
        record.update(status="rejected", reason=f"blocked by the validator: {exc}")
        return record
    try:
        got = execute_select(cleaned)
    except QueryError as exc:
        record.update(status="error", reason=str(exc))
        return record

    gold = execute_select(question["gold_sql"])
    ok, reason = compare(gold, got, question.get("ordered", False))
    record.update(status="pass" if ok else "fail", reason=reason)
    return record


# -------------------------------------------------------------- commands ---
def cmd_prompt(args) -> None:
    questions = load_questions()
    if args.difficulty != "all":
        questions = [q for q in questions if q["difficulty"] == args.difficulty]
    lines = "\n".join(f"{q['id']}: {q['question']}" for q in questions)
    text = PROMPT_TEMPLATE.format(
        first_id=questions[0]["id"], second_id=questions[1]["id"], questions=lines
    )
    out = Path(args.out) if args.out else EVAL_DIR / f"prompt_{args.difficulty}.txt"
    out.write_text(text, encoding="utf-8")
    print(f"Wrote {len(questions)} questions to {out}")
    print("Next: open a NEW Claude Desktop chat, paste the file's contents, and save Claude's")
    print(f"JSON reply as evals/answers_{args.difficulty}.json")


def cmd_grade(args) -> None:
    questions = load_questions()
    answers = load_answers(args.answers)
    unknown = sorted(set(answers) - {q["id"] for q in questions})
    if unknown:
        print(f"Warning: ignoring answers with unknown ids: {', '.join(unknown)}\n")

    results = [grade_one(q, answers.get(q["id"])) for q in questions]

    # ---- per-question table
    print(f"{'ID':<5}{'LEVEL':<8}{'RESULT':<10}DETAIL")
    for r in results:
        print(f"{r['id']:<5}{r['difficulty']:<8}{r['status'].upper():<10}{r['reason'][:90]}")

    # ---- summary
    by_level: dict[str, list[bool]] = defaultdict(list)
    for r in results:
        by_level[r["difficulty"]].append(r["status"] == "pass")
    total_pass = sum(r["status"] == "pass" for r in results)
    total = len(results)
    missing = sum(r["status"] == "missing" for r in results)

    print("\nACCURACY")
    print(f"  overall: {total_pass}/{total} = {100 * total_pass / total:.1f}%")
    for level in DIFFICULTIES:
        if level in by_level:
            hits = sum(by_level[level])
            print(f"  {level:<7}: {hits}/{len(by_level[level])} = {100 * hits / len(by_level[level]):.1f}%")
    if missing:
        print(f"\nNote: {missing} question(s) had no answer and count as wrong. "
              "For a final resume number, answer all questions.")

    # ---- failures
    failures = [r for r in results if r["status"] not in ("pass", "missing")]
    if failures:
        print("\nFAILURES (for your error analysis)")
        for r in failures:
            print(f"\n[{r['id']}] {r['question']}\n  status: {r['status']} - {r['reason']}\n  sql: {r['generated_sql']}")

    # ---- save results
    summary = {
        "model": args.model,
        "date": datetime.date.today().isoformat(),
        "overall": {"correct": total_pass, "total": total, "accuracy": round(total_pass / total, 4)},
        "by_difficulty": {
            lvl: {"correct": sum(v), "total": len(v), "accuracy": round(sum(v) / len(v), 4)}
            for lvl, v in by_level.items()
        },
        "results": results,
    }
    (EVAL_DIR / "results.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    (EVAL_DIR / "results.md").write_text(build_markdown(summary, failures), encoding="utf-8")
    print("\nSaved evals/results.json and evals/results.md (paste results.md into your README)")


def build_markdown(summary: dict, failures: list[dict]) -> str:
    o = summary["overall"]
    lines = [
        "## Evaluation results",
        "",
        f"Model: {summary['model']} | Date: {summary['date']} | "
        f"Metric: execution accuracy (the generated query must return the same data as the gold query)",
        "",
        "| Difficulty | Correct | Accuracy |",
        "|---|---|---|",
    ]
    for level in DIFFICULTIES:
        if level in summary["by_difficulty"]:
            d = summary["by_difficulty"][level]
            lines.append(f"| {level} | {d['correct']}/{d['total']} | {100 * d['accuracy']:.1f}% |")
    lines.append(f"| **overall** | **{o['correct']}/{o['total']}** | **{100 * o['accuracy']:.1f}%** |")
    if failures:
        lines += ["", "### Failures", ""]
        for r in failures:
            lines.append(f"- **{r['id']}** ({r['difficulty']}): {r['question']} Reason: {r['reason']}")
    return "\n".join(lines) + "\n"


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Text-to-SQL evaluation")
    sub = parser.add_subparsers(dest="command", required=True)

    p_prompt = sub.add_parser("prompt", help="write the questions to paste into Claude Desktop")
    p_prompt.add_argument("--difficulty", choices=DIFFICULTIES + ["all"], default="all")
    p_prompt.add_argument("--out", help="output file (default: evals/prompt_<difficulty>.txt)")
    p_prompt.set_defaults(func=cmd_prompt)

    p_grade = sub.add_parser("grade", help="grade saved answer files")
    p_grade.add_argument("answers", nargs="+", help="one or more JSON answer files")
    p_grade.add_argument("--model", default="Claude (via Claude Desktop)", help="label to record in the results")
    p_grade.set_defaults(func=cmd_grade)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()