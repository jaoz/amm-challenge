#!/usr/bin/env python3
"""Run amm-match for a strategy and append result metadata to a CSV file."""

from __future__ import annotations

import argparse
import csv
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


EDGE_LINE_RE = re.compile(
    r"^(?P<strategy_name>.+?)\s+Edge:\s+(?P<edge>-?\d+(?:\.\d+)?)\s*$"
)
OLD_HEADER = ["start_ts", "end_ts", "strategy_name", "edge"]
NEW_HEADER = ["start_ts", "end_ts", "strategy_path", "strategy_name", "edge"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run `amm-match run` and log start/end time, strategy name, and edge to CSV."
    )
    parser.add_argument(
        "strategy",
        nargs="?",
        default="Strat/my_strategy.sol",
        help="Path to strategy .sol file (default: Strat/my_strategy.sol)",
    )
    parser.add_argument(
        "--csv",
        default="match_runs.csv",
        help="Output CSV path (default: match_runs.csv)",
    )
    parser.add_argument(
        "--simulations",
        type=int,
        default=None,
        help="Optional simulation count passed through to `amm-match run`.",
    )
    return parser.parse_args()


def extract_edge_line(stdout: str) -> tuple[str, float]:
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise ValueError("Command produced no output.")

    last_line = lines[-1]
    match = EDGE_LINE_RE.match(last_line)
    if not match:
        raise ValueError(
            f"Could not parse last output line as '<strategy> Edge: <value>': {last_line!r}"
        )

    strategy_name = match.group("strategy_name")
    edge = float(match.group("edge"))
    return strategy_name, edge


def _read_header(csv_path: Path) -> list[str] | None:
    if not csv_path.exists():
        return None
    with csv_path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        try:
            return next(reader)
        except StopIteration:
            return None


def append_csv_row(
    csv_path: Path,
    start_ts: str,
    end_ts: str,
    strategy_path: str,
    strategy_name: str,
    edge: float,
) -> None:
    header = _read_header(csv_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    with csv_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if header is None:
            writer.writerow(NEW_HEADER)
            writer.writerow([start_ts, end_ts, strategy_path, strategy_name, f"{edge:.2f}"])
            return

        if header == NEW_HEADER:
            writer.writerow([start_ts, end_ts, strategy_path, strategy_name, f"{edge:.2f}"])
            return

        if header == OLD_HEADER:
            # Backward-compatible append for existing CSVs.
            name_with_path = f"{strategy_name} [{strategy_path}]"
            writer.writerow([start_ts, end_ts, name_with_path, f"{edge:.2f}"])
            return

        # Unknown header shape; append new-format row to preserve data.
        writer.writerow([start_ts, end_ts, strategy_path, strategy_name, f"{edge:.2f}"])


def main() -> int:
    args = parse_args()
    amm_match = shutil.which("amm-match")
    if amm_match:
        command = [amm_match, "run", args.strategy]
    else:
        command = [sys.executable, "-m", "amm_competition.cli", "run", args.strategy]
    if args.simulations is not None:
        command.extend(["--simulations", str(args.simulations)])

    start_ts = datetime.now(timezone.utc).isoformat()
    result = subprocess.run(command, capture_output=True, text=True)
    end_ts = datetime.now(timezone.utc).isoformat()

    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)

    if result.returncode != 0:
        print(
            f"\namm-match failed with exit code {result.returncode}; CSV not updated.",
            file=sys.stderr,
        )
        return result.returncode

    try:
        strategy_name, edge = extract_edge_line(result.stdout)
    except ValueError as exc:
        print(f"\nFailed to parse result: {exc}", file=sys.stderr)
        return 1

    csv_path = Path(args.csv)
    append_csv_row(csv_path, start_ts, end_ts, args.strategy, strategy_name, edge)
    print(f"\nLogged run to {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
