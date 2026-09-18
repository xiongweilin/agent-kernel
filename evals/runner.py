"""在 eval cases 上跑一个候选输出集，只判断确定性属性。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from evals.scorers import score_case


def load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default="evals/cases")
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--expect-failures", action="store_true")
    args = parser.parse_args()

    cases: list[dict] = []
    for path in sorted(Path(args.cases).glob("*.jsonl")):
        cases.extend(load_jsonl(path))

    outputs = {row["id"]: row.get("output") for row in load_jsonl(Path(args.candidate))}

    failures: list[str] = []
    for case in cases:
        problems = score_case(case, outputs.get(case["id"]))
        print(f"{case['id']:>16}  {'ok' if not problems else 'violation'}")
        for problem in problems:
            print(f"                  - {problem}")
        if problems:
            failures.append(case["id"])

    print(f"cases={len(cases)} violations={len(failures)}")

    if args.expect_failures:
        if failures:
            print("expected violations were found")
            return 0
        print("expected violations but the candidate passed: the scorer is vacuous")
        return 1

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
