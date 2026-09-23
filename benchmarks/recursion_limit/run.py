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


DEFAULT_LIMITS = (20, 50, 80, 128, 256, 512, 1024)
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
        help="directory for raw CSV, fits, report, and SVG plots",
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


def fit_linear(xs: list[float], ys: list[float]) -> tuple[float, float, float] | None:
    if len(xs) < 2:
        return None
    x_mean = statistics.fmean(xs)
    y_mean = statistics.fmean(ys)
    denominator = sum((x - x_mean) ** 2 for x in xs)
    if denominator == 0:
        return None
    slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denominator
    intercept = y_mean - slope * x_mean
    return intercept, slope, r_squared(ys, [intercept + slope * x for x in xs])


def r_squared(actual: list[float], predicted: list[float]) -> float:
    mean = statistics.fmean(actual)
    residual = sum((a - p) ** 2 for a, p in zip(actual, predicted))
    total = sum((a - mean) ** 2 for a in actual)
    return 1.0 if total == 0 and residual == 0 else 1.0 - residual / total


def fit_group(points: list[tuple[int, float]]) -> dict[str, object]:
    xs = [float(limit) for limit, _ in points]
    ys = [max(nanos, 1.0) for _, nanos in points]

    log_x = [math.log(x) for x in xs]
    log_y = [math.log(y) for y in ys]
    power = fit_linear(log_x, log_y)
    power_result: dict[str, object] = {"model": "power", "a": 0.0, "b": 0.0, "r2": 0.0}
    if power is not None:
        intercept, slope, _ = power
        a = math.exp(intercept)
        b = slope
        predicted = [a * (x**b) for x in xs]
        power_result = {"model": "power", "a": a, "b": b, "r2": r_squared(ys, predicted)}

    exponential = fit_linear(xs, log_y)
    exponential_result: dict[str, object] = {
        "model": "exponential",
        "a": 0.0,
        "b": 0.0,
        "r2": 0.0,
    }
    if exponential is not None:
        intercept, slope, _ = exponential
        a = math.exp(intercept)
        b = slope
        predicted = [a * math.exp(b * x) for x in xs]
        exponential_result = {
            "model": "exponential",
            "a": a,
            "b": b,
            "r2": r_squared(ys, predicted),
        }

    linear = fit_linear(xs, ys)
    linear_result: dict[str, object] = {"model": "linear", "a": 0.0, "b": 0.0, "r2": 0.0}
    if linear is not None:
        intercept, slope, r2 = linear
        linear_result = {"model": "linear", "a": intercept, "b": slope, "r2": r2}

    return max((power_result, exponential_result, linear_result), key=lambda it: float(it["r2"]))


def fits(summary: list[dict[str, str]]) -> list[dict[str, str]]:
    grouped: dict[tuple[str, str, str], list[tuple[int, float]]] = defaultdict(list)
    for row in summary:
        grouped[(row["workload"], row["operation"], row["phase"])].append(
            (int(row["limit"]), float(row["median_ns"]))
        )

    result = []
    for (workload, operation, phase), points in sorted(grouped.items()):
        fit = fit_group(sorted(points))
        result.append(
            {
                "workload": workload,
                "operation": operation,
                "phase": phase,
                "model": str(fit["model"]),
                "a": f"{float(fit['a']):.6g}",
                "b": f"{float(fit['b']):.6g}",
                "r2": f"{float(fit['r2']):.6f}",
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


def escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def svg_text(x: float, y: float, text: str, size: int = 12, anchor: str = "start") -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-family="sans-serif" '
        f'font-size="{size}" text-anchor="{anchor}">{escape(text)}</text>'
    )


def write_scatter_svg(path: Path, rows: list[dict[str, str]], fitted: list[dict[str, str]], operation: str) -> None:
    operation_rows = [row for row in rows if row["operation"] == operation]
    operation_fits = [row for row in fitted if row["operation"] == operation]
    workloads = sorted({row["workload"] for row in operation_rows})
    phases = ["cold", "incremental"]
    colors = {"cold": "#0072B2", "incremental": "#D55E00"}
    width = 1320
    panel_height = 230
    top = 56
    left = 78
    right = 72
    bottom = 48
    height = max(width, top + panel_height * len(workloads) + bottom)
    plot_width = width - left - right
    panel_plot_height = panel_height - 54

    limits = sorted({int(row["limit"]) for row in operation_rows})
    x_min = min(math.log2(limit) for limit in limits)
    x_max = max(math.log2(limit) for limit in limits)
    if x_min == x_max:
        x_min -= 0.5
        x_max += 0.5

    def x_pos(limit: float) -> float:
        return left + (math.log2(limit) - x_min) / (x_max - x_min) * plot_width

    legend_x = width - 270
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        f'<rect width="{width}" height="{height}" fill="white"/>',
        svg_text(left, 24, f"Recursion-limit benchmark: {operation}", 20),
        svg_text(legend_x, 24, "cold", 13),
        f'<circle cx="{legend_x + 40}" cy="20" r="4" fill="{colors["cold"]}"/>',
        svg_text(legend_x + 52, 24, "incremental", 13),
        f'<circle cx="{legend_x + 132}" cy="20" r="4" fill="{colors["incremental"]}"/>',
    ]

    for panel_index, workload in enumerate(workloads):
        panel_rows = [row for row in operation_rows if row["workload"] == workload]
        values = [int(row["nanos"]) for row in panel_rows]
        y_min = math.log10(max(min(values), 1))
        y_max = math.log10(max(max(values), 10))
        if y_min == y_max:
            y_min -= 0.2
            y_max += 0.2
        else:
            padding = (y_max - y_min) * 0.08
            y_min -= padding
            y_max += padding

        panel_top = top + panel_index * panel_height
        plot_top = panel_top + 28
        plot_bottom = plot_top + panel_plot_height

        def y_pos(nanos: float) -> float:
            value = math.log10(max(nanos, 1))
            return plot_bottom - (value - y_min) / (y_max - y_min) * panel_plot_height

        parts.append(svg_text(left, panel_top + 8, workload, 15))
        parts.append(
            f'<rect x="{left}" y="{plot_top}" width="{plot_width}" height="{panel_plot_height}" '
            'fill="none" stroke="#D0D0D0"/>'
        )
        for tick in limits:
            x = x_pos(tick)
            parts.append(f'<line x1="{x:.1f}" y1="{plot_bottom}" x2="{x:.1f}" y2="{plot_bottom + 4}" stroke="#777"/>')
            parts.append(svg_text(x, plot_bottom + 18, str(tick), 10, "middle"))
        for exponent in range(math.floor(y_min), math.ceil(y_max) + 1):
            if exponent < y_min or exponent > y_max:
                continue
            y = y_pos(10**exponent)
            label = f"{10**exponent / 1_000_000:g}"
            parts.append(f'<line x1="{left - 4}" y1="{y:.1f}" x2="{left}" y2="{y:.1f}" stroke="#777"/>')
            parts.append(svg_text(left - 8, y + 4, label, 10, "end"))

        for phase in phases:
            phase_rows = [row for row in panel_rows if row["phase"] == phase]
            for row in phase_rows:
                parts.append(
                    f'<circle cx="{x_pos(int(row["limit"])):.1f}" cy="{y_pos(int(row["nanos"])):.1f}" '
                    f'r="2.3" fill="{colors[phase]}" fill-opacity="0.45"/>'
                )
            fit = next(
                (
                    row
                    for row in operation_fits
                    if row["workload"] == workload and row["phase"] == phase
                ),
                None,
            )
            if fit is None:
                continue
            points = []
            for step in range(80):
                x_value = x_min + (x_max - x_min) * step / 79
                limit = 2**x_value
                a = float(fit["a"])
                b = float(fit["b"])
                if fit["model"] == "power":
                    nanos = a * (limit**b)
                elif fit["model"] == "exponential":
                    nanos = a * math.exp(b * limit)
                else:
                    nanos = a + b * limit
                points.append(f"{x_pos(limit):.1f},{y_pos(nanos):.1f}")
            parts.append(
                f'<polyline points="{" ".join(points)}" fill="none" stroke="{colors[phase]}" '
                'stroke-width="1.6" stroke-dasharray="5 3"/>'
            )

    parts.append(svg_text(left, height - 12, "x: recursion limit (log2 spacing), y: milliseconds (log10 spacing)", 12))
    parts.append("</svg>")
    path.write_text("\n".join(parts))


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
    fitted: list[dict[str, str]],
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
        "## Best fits",
        "",
        "The script evaluates power (`t = a * limit^b`), exponential (`t = a * exp(b * limit)`), and linear models on median timings, then keeps the highest original-scale R².",
        "",
        "| workload | operation | phase | model | a | b | R² |",
        "|---|---|---|---|---:|---:|---:|",
    ]
    for row in fitted:
        lines.append(
            f"| {row['workload']} | {row['operation']} | {row['phase']} | {row['model']} | "
            f"{row['a']} | {row['b']} | {row['r2']} |"
        )

    lines += [
        "",
        "## Files",
        "",
        "- `raw.csv`: every measured sample.",
        "- `summary.csv`: per-case count, median, mean, p95, min, and max.",
        "- `fits.csv`: fitted model parameters.",
        "- `scatter-highlighting.svg`, `scatter-completion.svg`, `scatter-diagnostics.svg`: raw points plus fitted curves.",
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
    fitted = fits(summary)
    write_csv(output / "summary.csv", summary)
    write_csv(output / "fits.csv", fitted)
    for operation in ("highlighting", "completion", "diagnostics"):
        write_scatter_svg(output / f"scatter-{operation}.svg", rows, fitted, operation)
    write_report(output / "report.md", metadata, rows, summary, fitted)
    print(f"\nresults written to {output.resolve()}")


if __name__ == "__main__":
    main()
