"""Repeatable verification. Integration/live checks run only with explicit flags."""

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--integration", action="store_true", help="build Docker and verify disposable PostgreSQL"
    )
    parser.add_argument(
        "--live", action="store_true", help="run bounded public collection and paper cycle"
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = root / "data" / "verification" / uuid4().hex
    output.mkdir(parents=True)
    checks = [
        ("tests", [sys.executable, "-m", "pytest", "-q"]),
        ("lint", [sys.executable, "-m", "ruff", "check", "src", "tests", "scripts", "examples"]),
        (
            "format",
            [
                sys.executable,
                "-m",
                "ruff",
                "format",
                "src",
                "tests",
                "scripts",
                "examples",
                "--check",
            ],
        ),
    ]
    if args.integration:
        docker = shutil.which("docker")
        if not docker:
            raise RuntimeError("Docker executable unavailable")
        checks += [
            ("container_build", [docker, "compose", "build", "--quiet", "collector"]),
            ("postgres", [sys.executable, "scripts/verify_postgres.py"]),
        ]
    if args.live:
        checks += [
            (
                "public_data",
                [
                    sys.executable,
                    "-m",
                    "polyalpha.cli",
                    "--config",
                    "config/smoke.toml",
                    "--activity",
                ],
            ),
            (
                "paper",
                [
                    sys.executable,
                    "-m",
                    "polyalpha.paper",
                    "--config",
                    "config/smoke.toml",
                    "--cycles",
                    "1",
                    "--output-dir",
                    str(output / "paper"),
                ],
            ),
        ]
    results = []
    for name, command in checks:
        started = time.monotonic()
        with (output / f"{name}.log").open("x", encoding="utf-8") as log:
            try:
                result = subprocess.run(
                    command, cwd=root, stdout=log, stderr=subprocess.STDOUT, timeout=300
                )
                status = "passed" if result.returncode == 0 else "failed"
            except subprocess.TimeoutExpired:
                status = "timeout"
        row = dict(check=name, status=status, elapsed_seconds=round(time.monotonic() - started, 2))
        results.append(row)
        print(json.dumps(row), flush=True)
    report = dict(completed_at=datetime.now(UTC).isoformat(), checks=results)
    (output / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"verification_report": str(output / "summary.json")}))
    if any(r["status"] != "passed" for r in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
