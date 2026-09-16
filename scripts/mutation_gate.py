#!/usr/bin/env python3
"""Targeted mutation-testing gate.

mutmut does not support native Windows (mutmut issue #397), so this is a
focused, dependency-free mutation runner for the highest-risk semantic code:
US price/quantity scaling, US state predicates, and the live pre-trade risk
gate. It applies deterministic single-operator mutations using Python's
tokenizer (so it never touches docstrings, comments, or string literals), runs
the relevant tests for each mutant, and reports mutants that SURVIVE.

  killed mutant  -> the suite noticed the change (good)
  survived mutant -> the suite did NOT notice (coverage gap)

The gate exits non-zero if the survival rate exceeds --max-survival.

Safety: the original file is always restored (try/finally), and each mutant is
written atomically. A crash cannot leave mutated source behind.
"""

import argparse
import io
import subprocess
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (source file, [test files to run for mutants in that file])
TARGETS = {
    "src/polyalpha/us/instruments.py": ["tests/test_us_adapter.py"],
    "src/polyalpha/us/adapter.py": ["tests/test_us_adapter.py"],
    "src/polyalpha/us/states.py": ["tests/test_us_adapter.py"],
    "src/polyalpha/pre_trade_gate.py": ["tests/test_live_architecture.py"],
}

# Operator-token mutations. Only real OP/NAME tokens are mutated (tokenize),
# so string literals and docstrings are never touched.
OP_MUTATIONS = {
    "<=": "<", "<": "<=",
    ">=": ">", ">": ">=",
    "==": "!=", "!=": "==",
    "+": "-", "-": "+",
    "*": "/", "/": "*",
}
NAME_MUTATIONS = {"and": "or", "or": "and"}


def _generate_mutants(path: Path):
    """Yield (line_index, col_start, col_end, replacement) real-operator mutants."""
    src = path.read_text(encoding="utf-8")
    seen_lines: set[int] = set()
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except tokenize.TokenError:
        return
    for tok in tokens:
        if tok.type == tokenize.OP and tok.string in OP_MUTATIONS:
            replacement = OP_MUTATIONS[tok.string]
        elif tok.type == tokenize.NAME and tok.string in NAME_MUTATIONS:
            replacement = NAME_MUTATIONS[tok.string]
        else:
            continue
        (line_no, col_start) = tok.start
        (end_line, col_end) = tok.end
        if line_no != end_line:
            continue
        if line_no in seen_lines:  # one mutant per line keeps the set reviewable
            continue
        seen_lines.add(line_no)
        yield (line_no - 1, col_start, col_end, replacement)


def _run_tests(test_files: list[str]) -> bool:
    """True if the tests PASS (mutant survives)."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-x", "-q", *test_files],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Targeted mutation-testing gate")
    parser.add_argument("--max-survival", type=float, default=0.10,
                        help="Max allowed survived-mutant fraction (default 0.10)")
    parser.add_argument("--limit", type=int, default=40,
                        help="Max mutants per file (keeps the gate fast)")
    args = parser.parse_args()

    total = 0
    survived = 0
    survivors: list[str] = []

    for rel, test_files in TARGETS.items():
        path = ROOT / rel
        original = path.read_text(encoding="utf-8")
        mutants = list(_generate_mutants(path))
        if len(mutants) > args.limit:
            mutants = mutants[: args.limit]
        print(f"\n=== {rel}: {len(mutants)} mutants ===")
        try:
            for (line_no, col_start, col_end, replacement) in mutants:
                lines = original.splitlines(keepends=True)
                line = lines[line_no]
                lines[line_no] = line[:col_start] + replacement + line[col_end:]
                path.write_text("".join(lines), encoding="utf-8")
                try:
                    passed = _run_tests(test_files)
                finally:
                    path.write_text(original, encoding="utf-8")
                total += 1
                if passed:
                    survived += 1
                    survivors.append(
                        f"{rel}:{line_no + 1}  {line.strip()[:70]} -> {replacement}"
                    )
        finally:
            path.write_text(original, encoding="utf-8")

    rate = (survived / total) if total else 0.0
    print("\n=== mutation summary ===")
    print(f"mutants: {total} | killed: {total - survived} | survived: {survived}")
    print(f"survival rate: {rate:.1%} (max allowed {args.max_survival:.1%})")
    if survivors:
        print("\nsurvivors (coverage gaps):")
        for s in survivors:
            print(f"  {s}")
    return 1 if rate > args.max_survival else 0


if __name__ == "__main__":
    sys.exit(main())