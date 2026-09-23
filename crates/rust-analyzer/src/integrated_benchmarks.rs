//! Fully integrated benchmarks for rust-analyzer, which load real cargo
//! projects.
//!
//! The benchmark here is used to debug specific performance regressions. If you
//! notice that, eg, completion is slow in some specific case, you can  modify
//! code here exercise this specific completion, and thus have a fast
//! edit/compile/test cycle.
//!
//! Note that "rust-analyzer: Run" action does not allow running a single test
//! in release mode in VS Code. There's however "rust-analyzer: Copy Run Command Line"
//! which you can use to paste the command in terminal and add `--release` manually.

use std::{env, hint::black_box, time::Instant};

use hir::ChangeWithProcMacros;
use ide::{
    AnalysisHost, CallableSnippets, CompletionConfig, CompletionFieldsToResolve, DiagnosticsConfig,
    FilePosition, RaFixtureConfig, TextSize,
};
use ide_db::{
    SnippetCap,
    imports::insert_use::{ImportGranularity, InsertUseConfig},
};
use project_model::CargoConfig;
use test_fixture::ChangeFixture;
use test_utils::project_root;
use vfs::{AbsPathBuf, VfsPath};

use load_cargo::{LoadCargoConfig, ProcMacroServerChoice, load_workspace_at};

#[track_caller]
fn file_id(vfs: &vfs::Vfs, path: &VfsPath) -> vfs::FileId {
    match vfs.file_id(path) {
        Some((file_id, vfs::FileExcluded::No)) => file_id,
        None | Some((_, vfs::FileExcluded::Yes)) => panic!("can't find virtual file for {path}"),
    }
}

#[test]
fn integrated_highlighting_benchmark() {
    if std::env::var("RUN_SLOW_BENCHES").is_err() {
        return;
    }

    // Load rust-analyzer itself.
    let workspace_to_load = project_root();
    let file = "./crates/rust-analyzer/src/config.rs";

    let cargo_config = CargoConfig {
        sysroot: Some(project_model::RustLibSource::Discover),
        all_targets: true,
        set_test: true,
        ..CargoConfig::default()
    };
    let load_cargo_config = LoadCargoConfig {
        load_out_dirs_from_check: true,
        with_proc_macro_server: ProcMacroServerChoice::Sysroot,
        prefill_caches: false,
        num_worker_threads: 1,
        proc_macro_processes: 1,
    };

    let (db, vfs, _proc_macro) = {
        let _it = stdx::timeit("workspace loading");
        load_workspace_at(
            workspace_to_load.as_std_path(),
            &cargo_config,
            &load_cargo_config,
            &|_| {},
        )
        .unwrap()
    };
    let mut host = AnalysisHost::with_database(db);

    let file_id = {
        let file = workspace_to_load.join(file);
        let path = VfsPath::from(AbsPathBuf::assert(file));
        file_id(&vfs, &path)
    };

    {
        let _it = stdx::timeit("initial");
        let analysis = host.analysis();
        analysis.highlight_as_html(file_id, false).unwrap();
    }

    {
        let _it = stdx::timeit("change");
        let mut text = host.analysis().file_text(file_id).unwrap().to_string();
        text = text.replace(
            "self.data.cargo_buildScripts_rebuildOnSave",
            "self. data. cargo_buildScripts_rebuildOnSave",
        );
        let mut change = ChangeWithProcMacros::default();
        change.change_file(file_id, Some(text));
        host.apply_change(change);
    }

    let _g = crate::tracing::hprof::init("*>10");

    {
        let _it = stdx::timeit("after change");
        let _span = profile::cpu_span();
        let analysis = host.analysis();
        analysis.highlight_as_html(file_id, false).unwrap();
    }
}

#[test]
fn integrated_completion_benchmark() {
    if std::env::var("RUN_SLOW_BENCHES").is_err() {
        return;
    }

    // Load rust-analyzer itself.
    let workspace_to_load = project_root();
    let file = "./crates/hir/src/lib.rs";

    let cargo_config = CargoConfig {
        sysroot: Some(project_model::RustLibSource::Discover),
        all_targets: true,
        set_test: true,
        ..CargoConfig::default()
    };
    let load_cargo_config = LoadCargoConfig {
        load_out_dirs_from_check: true,
        with_proc_macro_server: ProcMacroServerChoice::Sysroot,
        prefill_caches: true,
        num_worker_threads: 1,
        proc_macro_processes: 1,
    };

    let (db, vfs, _proc_macro) = {
        let _it = stdx::timeit("workspace loading");
        load_workspace_at(
            workspace_to_load.as_std_path(),
            &cargo_config,
            &load_cargo_config,
            &|_| {},
        )
        .unwrap()
    };
    let mut host = AnalysisHost::with_database(db);

    let file_id = {
        let file = workspace_to_load.join(file);
        let path = VfsPath::from(AbsPathBuf::assert(file));
        file_id(&vfs, &path)
    };

    // kick off parsing and index population

    let completion_offset = {
        let _it = stdx::timeit("change");
        let mut text = host.analysis().file_text(file_id).unwrap().to_string();
        let completion_offset =
            patch(&mut text, "db.struct_signature(self.id)", "sel;\ndb.struct_signature(self.id)")
                + "sel".len();
        let mut change = ChangeWithProcMacros::default();
        change.change_file(file_id, Some(text));
        host.apply_change(change);
        completion_offset
    };

    {
        let _span = profile::cpu_span();
        let analysis = host.analysis();
        let config = completion_config();
        let position =
            FilePosition { file_id, offset: TextSize::try_from(completion_offset).unwrap() };
        analysis.completions(&config, position, None).unwrap();
    }

    let _g = crate::tracing::hprof::init("*>10");

    let completion_offset = {
        let _it = stdx::timeit("change");
        let mut text = host.analysis().file_text(file_id).unwrap().to_string();
        let completion_offset = patch(
            &mut text,
            "sel;\ndb.struct_signature(self.id)",
            ";sel;\ndb.struct_signature(self.id)",
        ) + ";sel".len();
        let mut change = ChangeWithProcMacros::default();
        change.change_file(file_id, Some(text));
        host.apply_change(change);
        completion_offset
    };

    {
        let _p = tracing::info_span!("unqualified path completion").entered();
        let _span = profile::cpu_span();
        let analysis = host.analysis();
        let config = completion_config();
        let position =
            FilePosition { file_id, offset: TextSize::try_from(completion_offset).unwrap() };
        analysis.completions(&config, position, None).unwrap();
    }

    let completion_offset = {
        let _it = stdx::timeit("change");
        let mut text = host.analysis().file_text(file_id).unwrap().to_string();
        let completion_offset = patch(
            &mut text,
            "sel;\ndb.struct_signature(self.id)",
            "self.;\ndb.struct_signature(self.id)",
        ) + "self.".len();
        let mut change = ChangeWithProcMacros::default();
        change.change_file(file_id, Some(text));
        host.apply_change(change);
        completion_offset
    };

    {
        let _p = tracing::info_span!("dot completion").entered();
        let _span = profile::cpu_span();
        let analysis = host.analysis();
        let config = completion_config();
        let position =
            FilePosition { file_id, offset: TextSize::try_from(completion_offset).unwrap() };
        analysis.completions(&config, position, None).unwrap();
    }
}

#[test]
fn integrated_diagnostics_benchmark() {
    if std::env::var("RUN_SLOW_BENCHES").is_err() {
        return;
    }

    // Load rust-analyzer itself.
    let workspace_to_load = project_root();
    let file = "./crates/hir/src/lib.rs";

    let cargo_config = CargoConfig {
        sysroot: Some(project_model::RustLibSource::Discover),
        all_targets: true,
        set_test: true,
        ..CargoConfig::default()
    };
    let load_cargo_config = LoadCargoConfig {
        load_out_dirs_from_check: true,
        with_proc_macro_server: ProcMacroServerChoice::Sysroot,
        prefill_caches: true,
        num_worker_threads: 1,
        proc_macro_processes: 1,
    };

    let (db, vfs, _proc_macro) = {
        let _it = stdx::timeit("workspace loading");
        load_workspace_at(
            workspace_to_load.as_std_path(),
            &cargo_config,
            &load_cargo_config,
            &|_| {},
        )
        .unwrap()
    };
    let mut host = AnalysisHost::with_database(db);

    let file_id = {
        let file = workspace_to_load.join(file);
        let path = VfsPath::from(AbsPathBuf::assert(file));
        file_id(&vfs, &path)
    };

    let diagnostics_config = diagnostics_config();
    host.analysis()
        .full_diagnostics(&diagnostics_config, ide::AssistResolveStrategy::None, file_id)
        .unwrap();

    let _g = crate::tracing::hprof::init("*");

    {
        let _it = stdx::timeit("change");
        let mut text = host.analysis().file_text(file_id).unwrap().to_string();
        patch(&mut text, "db.struct_signature(self.id)", "();\ndb.struct_signature(self.id)");
        let mut change = ChangeWithProcMacros::default();
        change.change_file(file_id, Some(text));
        host.apply_change(change);
    };

    {
        let _p = tracing::info_span!("diagnostics").entered();
        let _span = profile::cpu_span();
        host.analysis()
            .full_diagnostics(&diagnostics_config, ide::AssistResolveStrategy::None, file_id)
            .unwrap();
    }
}

fn patch(what: &mut String, from: &str, to: &str) -> usize {
    let idx = what.find(from).unwrap();
    *what = what.replacen(from, to, 1);
    idx
}

fn diagnostics_config() -> DiagnosticsConfig {
    DiagnosticsConfig {
        enabled: false,
        proc_macros_enabled: true,
        proc_attr_macros_enabled: true,
        disable_experimental: true,
        disabled: Default::default(),
        expr_fill_default: Default::default(),
        style_lints: false,
        snippet_cap: SnippetCap::new(true),
        insert_use: InsertUseConfig {
            granularity: ImportGranularity::Crate,
            enforce_granularity: false,
            prefix_kind: hir::PrefixKind::ByCrate,
            group: true,
            skip_glob_imports: true,
        },
        prefer_no_std: false,
        prefer_prelude: false,
        prefer_absolute: false,
        term_search_fuel: 400,
        show_rename_conflicts: true,
    }
}

fn completion_config() -> CompletionConfig<'static> {
    CompletionConfig {
        enable_postfix_completions: true,
        enable_imports_on_the_fly: true,
        enable_self_on_the_fly: true,
        enable_private_editable: true,
        enable_term_search: true,
        term_search_fuel: 200,
        full_function_signatures: false,
        callable: Some(CallableSnippets::FillArguments),
        snippet_cap: SnippetCap::new(true),
        insert_use: InsertUseConfig {
            granularity: ImportGranularity::Crate,
            prefix_kind: hir::PrefixKind::ByCrate,
            enforce_granularity: true,
            group: true,
            skip_glob_imports: true,
        },
        prefer_no_std: false,
        prefer_prelude: true,
        prefer_absolute: false,
        snippets: Vec::new(),
        limit: None,
        add_colons_to_module: true,
        add_semicolon_to_unit: true,
        fields_to_resolve: CompletionFieldsToResolve::empty(),
        exclude_flyimport: vec![],
        exclude_traits: &[],
        enable_auto_await: true,
        enable_auto_iter: true,
        ra_fixture: RaFixtureConfig::default(),
    }
}

fn inlay_hints_config() -> ide::InlayHintsConfig<'static> {
    ide::InlayHintsConfig {
        render_colons: true,
        type_hints: true,
        type_hints_placement: ide::TypeHintsPlacement::Inline,
        sized_bound: false,
        discriminant_hints: ide::DiscriminantHints::Never,
        parameter_hints: false,
        parameter_hints_for_missing_arguments: false,
        generic_parameter_hints: ide::GenericParameterHints {
            type_hints: false,
            lifetime_hints: false,
            const_hints: false,
        },
        chaining_hints: true,
        adjustment_hints: ide::AdjustmentHints::Never,
        adjustment_hints_disable_reborrows: false,
        adjustment_hints_mode: ide::AdjustmentHintsMode::Prefix,
        adjustment_hints_hide_outside_unsafe: false,
        closure_return_type_hints: ide::ClosureReturnTypeHints::Never,
        closure_capture_hints: false,
        binding_mode_hints: false,
        implicit_drop_hints: false,
        implied_dyn_trait_hints: false,
        lifetime_elision_hints: ide::LifetimeElisionHints::Never,
        param_names_for_lifetime_elision_hints: false,
        hide_inferred_type_hints: false,
        hide_named_constructor_hints: false,
        hide_closure_initialization_hints: false,
        hide_closure_parameter_hints: false,
        range_exclusive_hints: false,
        closure_style: hir::ClosureStyle::ImplFn,
        max_length: None,
        closing_brace_hints_min_lines: None,
        fields_to_resolve: ide::InlayFieldsToResolve::empty(),
        ra_fixture: RaFixtureConfig::default(),
    }
}

#[derive(Clone, Copy, Debug)]
enum RecursionBenchmarkWorkload {
    SelfDerefError,
    RecursiveVars,
    DeepRefScaled,
    DeepRefFixed256,
}

impl RecursionBenchmarkWorkload {
    const ALL: [RecursionBenchmarkWorkload; 4] = [
        RecursionBenchmarkWorkload::SelfDerefError,
        RecursionBenchmarkWorkload::RecursiveVars,
        RecursionBenchmarkWorkload::DeepRefScaled,
        RecursionBenchmarkWorkload::DeepRefFixed256,
    ];

    fn name(self) -> &'static str {
        match self {
            RecursionBenchmarkWorkload::SelfDerefError => "self_deref_error",
            RecursionBenchmarkWorkload::RecursiveVars => "recursive_vars",
            RecursionBenchmarkWorkload::DeepRefScaled => "deep_ref_scaled",
            RecursionBenchmarkWorkload::DeepRefFixed256 => "deep_ref_fixed_256",
        }
    }

    fn fixture(self, recursion_limit: usize, include_completion_marker: bool) -> String {
        match self {
            RecursionBenchmarkWorkload::SelfDerefError => {
                let tail = if include_completion_marker {
                    "let value = Foo;\n    Foo.nonexistent_method();\n    Foo.$0"
                } else {
                    "let value = Foo;\n    Foo.nonexistent_method();"
                };
                format!(
                    r#"//- minicore: deref
#![recursion_limit = "{recursion_limit}"]
struct Foo;
impl core::ops::Deref for Foo {{
    type Target = Foo;
    fn deref(&self) -> &Foo {{ self }}
}}
fn bar() {{
    {tail}
}}
"#
                )
            }
            RecursionBenchmarkWorkload::RecursiveVars => {
                let tail = if include_completion_marker {
                    "y.nonexistent_method();\n    y.$0"
                } else {
                    "y.nonexistent_method();"
                };
                format!(
                    r#"#![recursion_limit = "{recursion_limit}"]
fn test() {{
    let y = unknown;
    [y, &y];
    {tail}
}}
"#
                )
            }
            RecursionBenchmarkWorkload::DeepRefScaled
            | RecursionBenchmarkWorkload::DeepRefFixed256 => {
                let depth = match self {
                    RecursionBenchmarkWorkload::DeepRefScaled => recursion_limit,
                    RecursionBenchmarkWorkload::DeepRefFixed256 => 256,
                    _ => unreachable!(),
                };
                let references = "&".repeat(depth);
                let tail = if include_completion_marker {
                    "value.leaf();\n    value.$0"
                } else {
                    "value.leaf();"
                };
                format!(
                    r#"#![recursion_limit = "{recursion_limit}"]
struct Leaf;
impl Leaf {{
    fn leaf(&self) {{}}
}}
fn test() {{
    let value = {references}Leaf;
    {tail}
}}
"#
                )
            }
        }
    }

    fn mutate(self, text: &mut String) -> TextSize {
        let anchor = match self {
            RecursionBenchmarkWorkload::SelfDerefError => "fn bar() {",
            RecursionBenchmarkWorkload::RecursiveVars
            | RecursionBenchmarkWorkload::DeepRefScaled
            | RecursionBenchmarkWorkload::DeepRefFixed256 => "fn test() {",
        };
        const INSERTION: &str = "\n    let _ = 0;";
        let replacement = format!("{anchor}{INSERTION}");
        assert!(text.contains(anchor), "missing mutation anchor for {}", self.name());
        *text = text.replacen(anchor, &replacement, 1);
        TextSize::from(INSERTION.len() as u32)
    }
}

#[derive(Clone, Copy, Debug)]
enum RecursionBenchmarkOperation {
    Highlighting,
    Completion,
    InlayHints,
    Diagnostics,
}

impl RecursionBenchmarkOperation {
    const ALL: [RecursionBenchmarkOperation; 4] = [
        RecursionBenchmarkOperation::Highlighting,
        RecursionBenchmarkOperation::Completion,
        RecursionBenchmarkOperation::InlayHints,
        RecursionBenchmarkOperation::Diagnostics,
    ];

    fn name(self) -> &'static str {
        match self {
            RecursionBenchmarkOperation::Highlighting => "highlighting",
            RecursionBenchmarkOperation::Completion => "completion",
            RecursionBenchmarkOperation::InlayHints => "inlay_hints",
            RecursionBenchmarkOperation::Diagnostics => "diagnostics",
        }
    }
}

fn run_recursion_benchmark_operation(
    operation: RecursionBenchmarkOperation,
    analysis: &ide::Analysis,
    file_id: vfs::FileId,
    position: Option<FilePosition>,
    completion_config: &CompletionConfig<'_>,
    inlay_hints_config: &ide::InlayHintsConfig<'_>,
    diagnostics_config: &DiagnosticsConfig,
) -> usize {
    let result = match operation {
        RecursionBenchmarkOperation::Highlighting => {
            analysis.highlight_as_html(file_id, false).unwrap().len()
        }
        RecursionBenchmarkOperation::Completion => analysis
            .completions(
                completion_config,
                position.expect("completion benchmark requires a $0 marker"),
                None,
            )
            .unwrap()
            .map_or(0, |completions| completions.len()),
        RecursionBenchmarkOperation::InlayHints => {
            analysis.inlay_hints(inlay_hints_config, file_id, None).unwrap().len()
        }
        RecursionBenchmarkOperation::Diagnostics => analysis
            .full_diagnostics(diagnostics_config, ide::AssistResolveStrategy::None, file_id)
            .unwrap()
            .len(),
    };
    black_box(result)
}

fn recursion_benchmark_host(
    workload: RecursionBenchmarkWorkload,
    limit: usize,
    include_completion_marker: bool,
) -> (AnalysisHost, vfs::FileId, Option<FilePosition>) {
    let fixture = workload.fixture(limit, include_completion_marker);
    let mut host = AnalysisHost::default();
    host.raw_database_mut().enable_proc_attr_macros();
    let change_fixture = ChangeFixture::parse(&fixture);
    let file_id = change_fixture.files[0].file_id();
    let position = change_fixture.file_position.map(|(file_id, offset)| FilePosition {
        file_id: file_id.file_id(),
        offset: offset.expect_offset(),
    });
    host.apply_change(change_fixture.change);
    (host, file_id, position)
}

#[test]
#[allow(clippy::print_stdout)]
fn integrated_recursion_limit_benchmark() {
    if env::var("RUN_SLOW_BENCHES").is_err() {
        return;
    }

    let samples = env::var("RA_RECURSION_BENCH_SAMPLES")
        .ok()
        .and_then(|it| it.parse::<usize>().ok())
        .unwrap_or(10);
    let limits = env::var("RA_RECURSION_BENCH_LIMITS")
        .ok()
        .map(|it| {
            it.split(',')
                .map(|limit| limit.trim().parse::<usize>().expect("invalid recursion limit"))
                .collect::<Vec<_>>()
        })
        .unwrap_or_else(|| {
            vec![8, 12, 16, 20, 24, 32, 40, 50, 64, 80, 96, 128, 160, 192, 256, 384, 512, 768, 1024]
        });

    std::thread::Builder::new()
        .name("recursion-limit-benchmark".to_owned())
        .stack_size(stdx::thread::DEFAULT_STACK_SIZE)
        .spawn(move || {
            let limits_display =
                limits.iter().map(|limit| limit.to_string()).collect::<Vec<_>>().join(";");
            println!(
                "RA_RECURSION_BENCH_META,os={},arch={},samples={},limits={}",
                env::consts::OS,
                env::consts::ARCH,
                samples,
                limits_display
            );
            let completion_config = completion_config();
            let inlay_hints_config = inlay_hints_config();
            let diagnostics_config = diagnostics_config();
            for workload in RecursionBenchmarkWorkload::ALL {
                for limit in &limits {
                    for sample in 0..samples {
                        for operation in RecursionBenchmarkOperation::ALL {
                            let include_completion_marker =
                                matches!(operation, RecursionBenchmarkOperation::Completion);
                            let (mut host, file_id, mut position) = recursion_benchmark_host(
                                workload,
                                *limit,
                                include_completion_marker,
                            );
                            let (elapsed, result) = {
                                let analysis = host.analysis();
                                let elapsed = Instant::now();
                                let result = run_recursion_benchmark_operation(
                                    operation,
                                    &analysis,
                                    file_id,
                                    position,
                                    &completion_config,
                                    &inlay_hints_config,
                                    &diagnostics_config,
                                );
                                (elapsed.elapsed(), result)
                            };
                            println!(
                                "RA_RECURSION_BENCH_ROW,{sample},{limit},{},{},cold,{},{}",
                                workload.name(),
                                operation.name(),
                                elapsed.as_nanos(),
                                result
                            );

                            let mut text = host.analysis().file_text(file_id).unwrap().to_string();
                            let shift = workload.mutate(&mut text);
                            if let Some(position) = &mut position {
                                position.offset += shift;
                            }
                            let mut change = ChangeWithProcMacros::default();
                            change.change_file(file_id, Some(text));
                            host.apply_change(change);

                            let (elapsed, result) = {
                                let analysis = host.analysis();
                                let elapsed = Instant::now();
                                let result = run_recursion_benchmark_operation(
                                    operation,
                                    &analysis,
                                    file_id,
                                    position,
                                    &completion_config,
                                    &inlay_hints_config,
                                    &diagnostics_config,
                                );
                                (elapsed.elapsed(), result)
                            };
                            println!(
                                "RA_RECURSION_BENCH_ROW,{sample},{limit},{},{},incremental,{},{}",
                                workload.name(),
                                operation.name(),
                                elapsed.as_nanos(),
                                result
                            );
                        }
                    }
                }
            }
        })
        .expect("failed to spawn recursion limit benchmark thread")
        .join()
        .expect("recursion limit benchmark thread panicked");
}
