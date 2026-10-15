"""Require real financial/forecast evidence, not merely a successful process exit."""

import json
import sys
from pathlib import Path


def validate(path):
    report = json.loads(Path(path).read_text())
    if not report["enriched"]:
        raise ValueError("Live scan produced no Stage 1/2 candidates to enrich")
    financial = sum(r["components"]["quality"]["coverage"] > 0 for r in report["rows"])
    estimates = sum(
        r["research"]["metrics"].get("eps_fy1") is not None for r in report["rows"]
    )
    print(
        f"Enriched {report['enriched']}; financial evidence {financial}; forward EPS evidence {estimates}"
    )
    if not financial or not estimates:
        raise ValueError(
            "Live provider coverage is insufficient; inspect artifact statuses"
        )
    print(
        "Live data, scoring and report generation verified. Investment performance remains untested."
    )


if __name__ == "__main__":
    validate(sys.argv[1])
