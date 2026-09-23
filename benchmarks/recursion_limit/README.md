# Recursion-limit benchmark

This benchmark measures the IDE cost of honoring the crate-level `recursion_limit`. It covers the two cases requested in the recursion-limit discussion:

- code that needs a deeper recursion limit, using deep autoderef and deliberate solver recursion;
- erroneous code whose inference work continues until the recursion limit, using `Foo: Deref<Target = Foo>`.

The benchmark uses `ide::AnalysisHost` directly. For every workload, limit, operation, and sample, it creates a fresh host for the cold measurement and then edits the function body for an incremental measurement. The operations are semantic highlighting, dot completion, type inlay hints, and full diagnostics.

Only the completion fixture contains a `$0` marker; other operations run on the same workload without an unrelated incomplete expression. After the incremental edit, the completion position is shifted by the inserted text so it continues to point at the original dot-completion location. The inlay-hints benchmark enables type and chaining hints while leaving other hint categories disabled.

## Run

From the repository root:

```console
python3 benchmarks/recursion_limit/run.py --samples 20
```

The default limits are `8,12,16,20,24,32,40,50,64,80,96,128,160,192,256,384,512,768,1024`. To use fewer samples while iterating:

```console
python3 benchmarks/recursion_limit/run.py --samples 3 --output target/recursion-limit-quick
```

On Windows PowerShell, run the same command; the script sets the environment variables for the Rust benchmark process itself.

## Outputs

The output directory contains:

- `raw.csv` with every measured sample;
- `summary.csv` with median, mean, p95, minimum, and maximum timings;
- `report.md` with the machine metadata and slowdown ratios.

The benchmark is intentionally gated behind `RUN_SLOW_BENCHES=1` in the Rust test. The Python driver sets it automatically.

## Interpretation

Use a release build and the same machine for comparisons. Absolute timings are machine-dependent; the useful signals are the per-limit measurements, the incremental-versus-cold gap, and the change around the limit that a workload actually needs. The self-deref and recursive-variable workloads are deliberate worst cases for malformed or highly recursive code; the deep-reference workload is correct code that requires autoderef beyond the old fixed 20-step cap.
