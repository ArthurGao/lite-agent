"""Which facts a long-task run kept (design §15, masking on long runs), scored afterwards from the
answers the run recorded (`Metrics.answer`): for the audits, how many of the truly delayed
reports the answer names and gives the true weight of; for compare-en, whether it gives the first
and the last report's true weight. Done rate alone hides this: on qwen3:8b both configs fail every
task, one by forgetting and one by miscounting. Not a test and not in CI.

    uv run python scripts/long_eval_facts.py eval/lite-eval/runs/agent-long-ollama/<run> ...
"""

import json
import re
import sys
from pathlib import Path

from lite_agent.evals.archive import REPORTS, Report, chain
from lite_agent.evals.tasks import says


def facts(case: str, answer: str) -> str:
    if case == "compare-en":
        first, last = REPORTS[chain("T")[0]], REPORTS[chain("T")[-1]]
        return (
            f"first weight {first.weight}: {says(answer, *_kg(first))}; "
            f"last weight {last.weight}: {says(answer, *_kg(last))}"
        )
    series = "R" if case == "audit-en" else "Q"
    delayed = {r for r in chain(series) if REPORTS[r].status == "delayed"}
    named = set(re.findall(rf"\b{series}\d\d\b", answer))
    weighed = {r for r in delayed if says(answer, *_kg(REPORTS[r]))}
    return (
        f"delayed named {len(named & delayed)}/{len(delayed)}, wrongly named "
        f"{len(named - delayed)}, true weights given {len(weighed)}/{len(delayed)}"
    )


def _kg(report: Report) -> tuple[str, str]:
    return str(report.weight), f"{report.weight}kg"


def main(runs: list[str]) -> None:
    for run in runs:
        print("==", Path(run).name)
        for line in Path(run, "outputs.jsonl").read_text().splitlines():
            out = json.loads(line)
            print(f"  {out['case_id']}: {facts(out['case_id'], out['value']['answer'])}")


if __name__ == "__main__":
    main(sys.argv[1:])
