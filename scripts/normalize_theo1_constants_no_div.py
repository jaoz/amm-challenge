#!/usr/bin/env python3
"""Normalize early Theo1 constants from `WAD / d` or `N * BPS` to integers.

Only rewrites constant definitions in the first N lines (default 100).
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

WAD_INT = 10**18
BPS_INT = 10**14
CONST_DIV_RE = re.compile(r"^(\s*uint256\s+constant\s+[A-Z0-9_]+\s*=\s*)WAD\s*/\s*(\d+)(\s*;\s*)(.*)$")
CONST_BPS_RE = re.compile(r"^(\s*uint256\s+constant\s+[A-Z0-9_]+\s*=\s*)(\d+)\s*\*\s*BPS(\s*;\s*)(.*)$")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Replace `WAD / d` and `N * BPS` constants with integer values.")
    p.add_argument("--in-path", required=True, help="Input strategy file path.")
    p.add_argument("--out-path", default="", help="Output strategy file path (default: in-place).")
    p.add_argument("--line-limit", type=int, default=100, help="Only rewrite constants in the first N lines.")
    return p.parse_args()


def normalize_text(src: str, line_limit: int) -> tuple[str, int]:
    lines = src.splitlines()
    rewrites = 0
    max_i = min(line_limit, len(lines))
    for i in range(max_i):
        line = lines[i]
        m = CONST_DIV_RE.match(line)
        if m:
            prefix, d_raw, mid, suffix = m.groups()
            d = int(d_raw)
            if d <= 0:
                continue
            value = WAD_INT // d
            lines[i] = f"{prefix}{value}{mid}{suffix}"
            rewrites += 1
            continue

        m = CONST_BPS_RE.match(line)
        if m:
            prefix, n_raw, mid, suffix = m.groups()
            n = int(n_raw)
            value = n * BPS_INT
            lines[i] = f"{prefix}{value}{mid}{suffix}"
            rewrites += 1
    return "\n".join(lines) + ("\n" if src.endswith("\n") else ""), rewrites


def main() -> int:
    args = parse_args()
    in_path = Path(args.in_path)
    out_path = Path(args.out_path) if args.out_path else in_path
    src = in_path.read_text(encoding="utf-8")
    dst, rewrites = normalize_text(src, max(1, args.line_limit))
    out_path.write_text(dst, encoding="utf-8")
    print(f"rewrites={rewrites}")
    print(f"out={out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
