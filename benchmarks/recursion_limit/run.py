#!/usr/bin/env python3
"""Run and summarize the rust-analyzer recursion-limit benchmark."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import statistics
import subprocess
from collections import defaultdict
from pathlib import Path


DEFAULT_LIMITS = (8, 12, 16, 20, 24, 32, 40, 50, 64, 80, 96, 128, 160, 192, 256, 384, 512, 768, 1024)
ROW_PREFIX = "RA_RECURSION_BENCH_ROW,"
META_PREFIX = "RA_RECURSION_BENCH_META,"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--limits", default=",".join(map(str, DEFAULT_LIMITS)))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("target/recursion-limit-benchmark"),
        help="directory for raw CSV, summary CSV, and report",
    )
    parser.add_argument(
        "--skip-run",
        action="store_true",
        help="reuse raw.csv in the output directory instead of running cargo test",
    )
    return parser.parse_args()


def run_benchmark(samples: int, limits: list[int], output: Path) -> tuple[dict[str, str], list[dict[str, str]]]:
    output.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["RUN_SLOW_BENCHES"] = "1"
    env["RA_RECURSION_BENCH_SAMPLES"] = str(samples)
    env["RA_RECURSION_BENCH_LIMITS"] = ",".join(map(str, limits))

    command = [
        "cargo",
        "test",
        "-p",
        "rust-analyzer",
        "--release",
        "--lib",
        "integrated_recursion_limit_benchmark",
        "--",
        "--nocapture",
    ]
    print("+ " + " ".join(command), flush=True)
    process = subprocess.Popen(
        command,
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None

    metadata: dict[str, str] = {}
    rows: list[dict[str, str]] = []
    for line in process.stdout:
        print(line, end="")
        line = line.strip()
        if line.startswith(META_PREFIX):
            raw_metadata = line[len(META_PREFIX) :]
            metadata["_raw"] = raw_metadata
            for item in raw_metadata.split(","):
                key, _, value = item.partition("=")
                metadata[key] = value
            limits_match = re.search(r"limits=\[(.*?)\]", raw_metadata)
            if limits_match:
                metadata["limits"] = limits_match.group(1).replace(" ", "")
        elif line.startswith(ROW_PREFIX):
            fields = line[len(ROW_PREFIX) :].split(",")
            if len(fields) != 7:
                raise RuntimeError(f"unexpected benchmark row: {line}")
            sample, limit, workload, operation, phase, nanos, result = fields
            rows.append(
                {
                    "sample": sample,
                    "limit": limit,
                    "workload": workload,
                    "operation": operation,
                    "phase": phase,
                    "nanos": nanos,
                    "result": result,
                }
            )

    return_code = process.wait()
    if return_code != 0:
        raise SystemExit(return_code)
    if not rows:
        raise SystemExit("benchmark produced no rows")
    write_raw_csv(output / "raw.csv", rows)
    (output / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    return metadata, rows


def write_raw_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fields = ["sample", "limit", "workload", "operation", "phase", "nanos", "result"]
    with path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def read_raw_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as file:
        return list(csv.DictReader(file))


def summarize(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    grouped: dict[tuple[str, str, str, int], list[int]] = defaultdict(list)
    for row in rows:
        grouped[(row["workload"], row["operation"], row["phase"], int(row["limit"]))].append(
            int(row["nanos"])
        )

    result = []
    for (workload, operation, phase, limit), samples in sorted(grouped.items()):
        samples.sort()
        p95 = samples[min(len(samples) - 1, math.ceil(len(samples) * 0.95) - 1)]
        result.append(
            {
                "workload": workload,
                "operation": operation,
                "phase": phase,
                "limit": str(limit),
                "count": str(len(samples)),
                "median_ns": f"{statistics.median(samples):.0f}",
                "mean_ns": f"{statistics.fmean(samples):.0f}",
                "p95_ns": str(p95),
                "min_ns": str(samples[0]),
                "max_ns": str(samples[-1]),
            }
        )
    return result


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    with path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


def key_ratio(summary: list[dict[str, str]], workload: str, operation: str, phase: str, numerator: int, denominator: int) -> str:
    values = {
        int(row["limit"]): float(row["median_ns"])
        for row in summary
        if row["workload"] == workload and row["operation"] == operation and row["phase"] == phase
    }
    if numerator not in values or denominator not in values or values[denominator] == 0:
        return "n/a"
    return f"{values[numerator] / values[denominator]:.2f}x"


def write_report(
    path: Path,
    metadata: dict[str, str],
    rows: list[dict[str, str]],
    summary: list[dict[str, str]],
) -> None:
    lines = [
        "# Recursion-limit benchmark report",
        "",
        f"- Platform: `{metadata.get('os', 'unknown')}/{metadata.get('arch', 'unknown')}`",
        f"- Samples per case: `{metadata.get('samples', 'unknown')}`",
        f"- Limits: `{metadata.get('limits', 'unknown').replace(';', ',')}`",
        f"- Raw rows: `{len(rows)}`",
        "",
        "Each cold sample uses a fresh `AnalysisHost`. Incremental samples apply an edit to the function body before measuring the same operation. Timing is end-to-end through the `ide::Analysis` API.",
        "",
        "`deep_ref_scaled` grows its input depth with the configured limit, so it measures the cost of the work that the higher limit unlocks. `deep_ref_fixed_256` keeps the input fixed and is the better workload for isolating the cost of moving the limit itself.",
        "",
        "## Ratios",
        "",
        "| workload | operation | phase | 128 / 20 | 1024 / 128 |",
        "|---|---|---:|---:|---:|",
    ]
    keys = sorted({(row["workload"], row["operation"], row["phase"]) for row in summary})
    for workload, operation, phase in keys:
        lines.append(
            f"| {workload} | {operation} | {phase} | "
            f"{key_ratio(summary, workload, operation, phase, 128, 20)} | "
            f"{key_ratio(summary, workload, operation, phase, 1024, 128)} |"
        )

    lines += [
        "",
        "## Files",
        "",
        "- `raw.csv`: every measured sample.",
        "- `summary.csv`: per-case count, median, mean, p95, min, and max.",
        "",
    ]
    path.write_text("\n".join(lines))


def main() -> None:
    args = parse_args()
    limits = [int(limit) for limit in args.limits.split(",") if limit.strip()]
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    if args.skip_run:
        rows = read_raw_csv(output / "raw.csv")
        metadata_path = output / "metadata.json"
        metadata = (
            json.loads(metadata_path.read_text())
            if metadata_path.exists()
            else {"samples": "reused", "limits": ",".join(map(str, limits))}
        )
    else:
        metadata, rows = run_benchmark(args.samples, limits, output)

    summary = summarize(rows)
    write_csv(output / "summary.csv", summary)
    write_report(output / "report.md", metadata, rows, summary)
    print(f"\nresults written to {output.resolve()}")


if __name__ == "__main__":
    main()
