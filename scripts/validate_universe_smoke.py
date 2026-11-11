"""Read-only live sample of common equities, a fund, debt and an ETF."""

import argparse
import json
import tempfile
from pathlib import Path

import pandas as pd

from src.data.security_profiles import SecurityProfileResolver
from src.data.universe_fetcher import USStockUniverseFetcher

SAMPLE = {"TSM", "V", "BRK.B", "PLTR", "MU", "QQQ", "BDJ", "TVE"}
EXPECTED_EQUITIES = {"TSM", "V", "BRK-B", "PLTR", "MU"}


def validate(output: Path) -> None:
    """Save audit evidence and fail if the sampled security types are misclassified."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        resolver = SecurityProfileResolver(root / "profiles", max_lookups=10)
        fetcher = USStockUniverseFetcher(root / "universe", profile_resolver=resolver)
        frames = [fetcher._fetch_nasdaq_listed(), fetcher._fetch_other_listed()]
        if any(frame.empty for frame in frames):
            raise ValueError("Live exchange directory unavailable")
        directory_rows = pd.concat(frames, ignore_index=True)
        sample = directory_rows[directory_rows["symbol"].isin(SAMPLE)]
        missing = SAMPLE - set(sample["symbol"])
        selected = set(fetcher._filter_stocks(sample)["symbol"])
        audit = {
            **fetcher.audit,
            "scope": "Eight-symbol integration sample, not a full-market scan",
            "missing_sample_symbols": sorted(missing),
            "expected_equities": sorted(EXPECTED_EQUITIES),
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(audit, indent=2, allow_nan=False), encoding="utf-8"
        )
        if missing or selected != EXPECTED_EQUITIES:
            raise ValueError(
                f"Live security classification mismatch; missing={sorted(missing)}, "
                f"selected={sorted(selected)}; inspect {output}"
            )
        print(f"Live universe sample passed: {sorted(selected)}; QQQ/BDJ/TVE excluded")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("data/smoke/universe_smoke.json")
    )
    args = parser.parse_args()
    validate(args.output)


if __name__ == "__main__":
    main()
