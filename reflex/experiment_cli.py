from __future__ import annotations

from pathlib import Path


def add_commands(commands):
    p = commands.add_parser("experiment", help="Frozen research suites, execution, replay and paired statistics")
    sub = p.add_subparsers(dest="experiment_command", required=True)
    from .risk_cli import add_commands as add_risk_commands
    add_risk_commands(sub)
    live = sub.add_parser("prepare-bfcl-live", help="Import pinned public BFCL Live data with leakage groups")
    live.add_argument("--raw", type=Path, required=True)
    live.add_argument("--output", type=Path, required=True)
    dojo = sub.add_parser("agentdojo", help="Native public safety stress tests, not risk calibration")
    dojo_sub = dojo.add_subparsers(dest="dojo_command", required=True)
    dojo_prepare = dojo_sub.add_parser("prepare")
    dojo_prepare.add_argument("--output", type=Path, required=True)
    dojo_run = dojo_sub.add_parser("run")
    dojo_run.add_argument("--data", type=Path, required=True)
    dojo_run.add_argument("--plan", type=Path, required=True)
    dojo_run.add_argument("--output", type=Path)
    dojo_run.add_argument("--execute", action="store_true")
    dojo_compare = dojo_sub.add_parser("compare", help="Interleaved Jev and generative baselines; preflight unless --execute")
    dojo_compare.add_argument("--data", type=Path, required=True)
    dojo_compare.add_argument("--plan", type=Path, required=True)
    dojo_compare.add_argument("--output", type=Path)
    dojo_compare.add_argument("--execute", action="store_true")
    generate = sub.add_parser("generate")
    generate.add_argument("kind", choices=("controlled", "rf5c", "intervention"))
    generate.add_argument("--output", type=Path, required=True)
    generate.add_argument("--seed", type=int, default=17)
    validate = sub.add_parser("validate")
    validate.add_argument("suite", type=Path)
    split = sub.add_parser("split")
    split.add_argument("suite", type=Path)
    split.add_argument("--output", type=Path, required=True)
    split.add_argument("--seed", type=int, default=17)
    split.add_argument("--ratios", type=float, nargs=3, default=(.6, .2, .2), metavar=("DEV", "CAL", "TEST"))
    split.add_argument("--template-key", help="Required metadata template field; template_id is always respected when present")
    split_audit = sub.add_parser("audit-split")
    split_audit.add_argument("manifest", type=Path)
    run = sub.add_parser("run")
    run.add_argument("--suite", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--repeats", type=int, default=1)
    run.add_argument("--vectors", type=Path)
    run.add_argument("--certification", type=Path)
    run.add_argument("--base-suite", type=Path)
    run.add_argument("--split-manifest", type=Path)
    run.add_argument("--partition", choices=("dev", "calibration", "test"))
    plan = sub.add_parser("matrix")
    plan.add_argument("plan", type=Path)
    plan.add_argument("--output", type=Path)
    plan.add_argument("--execute", action="store_true")
    replay = sub.add_parser("replay")
    replay.add_argument("case_dir", type=Path)
    replay.add_argument("--revised-task", type=Path)
    compare = sub.add_parser("compare")
    compare.add_argument("baseline", type=Path)
    compare.add_argument("treatment", type=Path)
    compare.add_argument("--resamples", type=int, default=10000)
    compare.add_argument("--seed", type=int, default=0)
    analyze = sub.add_parser("analyze")
    analyze.add_argument("kind", choices=("factorial", "intervention", "bfcl"))
    analyze.add_argument("results", type=Path)
    analyze.add_argument("--resamples", type=int, default=10000)
    analyze.add_argument("--seed", type=int, default=20260921)
    importer = sub.add_parser("import-bfcl")
    importer.add_argument("--questions", type=Path, required=True)
    importer.add_argument("--answers", type=Path)
    importer.add_argument("--irrelevant", action="store_true")
    importer.add_argument("--revision", required=True)
    importer.add_argument("--output", type=Path, required=True)
    expand = sub.add_parser("expand-bfcl")
    expand.add_argument("suite", type=Path)
    expand.add_argument("--certification", type=Path, required=True)
    expand.add_argument("--output", type=Path, required=True)
    expand.add_argument("--seed", type=int, default=17)
    review = sub.add_parser("review-bfcl")
    review.add_argument("suite", type=Path)
    review.add_argument("--pool", type=Path, nargs="+", required=True)
    review.add_argument("--output", type=Path, required=True)
    review.add_argument("--seed", type=int, default=17)
    tau = sub.add_parser("tau2")
    tau.add_argument("--plan", type=Path, required=True)
    tau.add_argument("--output", type=Path)
    tau.add_argument("--execute", action="store_true")
    tau_compare = sub.add_parser("compare-tau2")
    tau_compare.add_argument("baseline", type=Path)
    tau_compare.add_argument("treatment", type=Path)
    tau_compare.add_argument("--resamples", type=int, default=10000)
    embed = sub.add_parser("encode")
    embed.add_argument("suite", type=Path)
    embed.add_argument("--model-path", type=Path, required=True)
    embed.add_argument("--revision", required=True)
    embed.add_argument("--output", type=Path, required=True)
    similarity = sub.add_parser("embedding-audit")
    similarity.add_argument("suite", type=Path)
    similarity.add_argument("vectors", type=Path)
    match = sub.add_parser("match-intervention")
    match.add_argument("suite", type=Path)
    match.add_argument("--model-path", type=Path, required=True)
    match.add_argument("--revision", required=True)
    match.add_argument("--output", type=Path, required=True)
    prices = sub.add_parser("reprice")
    prices.add_argument("run_dir", type=Path)
    prices.add_argument("snapshots", type=Path)
    audit = sub.add_parser("audit-suite")
    audit.add_argument("profile", choices=("rf5c", "intervention", "bfcl-cardinality", "bfcl-relevance"))
    audit.add_argument("suite", type=Path)
    audit.add_argument("--vectors", type=Path)
    audit.add_argument("--certification", type=Path)
    audit.add_argument("--base-suite", type=Path)
    prepare = sub.add_parser("prepare-bfcl")
    prepare.add_argument("--positive", type=Path, nargs="+", required=True)
    prepare.add_argument("--negative", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--seed", type=int, default=17)
    report = sub.add_parser("report")
    report.add_argument("kind", choices=("pair", "cross-family", "repeat", "hierarchy", "native"))
    report.add_argument("first", type=Path)
    report.add_argument("second", type=Path, nargs="?")
    report.add_argument("--reviews", type=Path)
    report.add_argument("--first-repeat", type=int, default=0)
    report.add_argument("--second-repeat", type=int, default=1)
    report.add_argument("--resamples", type=int, default=10000)
    report.add_argument("--seed", type=int, default=20260921)


def execute(args, config):
    from .datasets import audit_decisions, bfcl_import, controlled_suite, decision_suite, expand_bfcl, write_suite
    from .experiments import compare_experiments, load_tasks, matrix, replay_case, run_experiment
    from .analyses import bfcl_results, compare_tau, embedding_audit, encode_candidates, reprice
    from .statistics import factorial_contrasts, matched_intervention
    from .types import json_text, strict_json
    command = args.experiment_command
    if command == "prepare-bfcl-live":
        from .public_data import prepare_live
        print(json_text(prepare_live(args.raw, args.output)))
        return 0
    if command == "agentdojo":
        from .agentdojo_adapter import prepare_native, run_native
        from .agentdojo_comparison import run_comparison
        if args.dojo_command == "prepare":
            report = prepare_native(args.output)
        else:
            runner = run_comparison if args.dojo_command == "compare" else run_native
            report = runner(config, args.data, args.plan, args.output, execute=args.execute)
        print(json_text(report))
        return 0 if report.get("complete", report.get("ready", not report.get("failures"))) else 3
    if command == "risk":
        from .risk_cli import execute as execute_risk
        return execute_risk(args, config)
    bfcl_evidence = None
    if command in {"run", "audit-suite"}:
        if bool(args.certification) != bool(args.base_suite):
            raise ValueError("Supply both --certification and --base-suite")
        if args.certification:
            bfcl_evidence = {"certification": strict_json(args.certification.read_text()), "base_tasks": load_tasks(args.base_suite)}
    if command == "generate":
        tasks = controlled_suite(args.seed) if args.kind == "controlled" else decision_suite(
            seed=args.seed, intervention=args.kind == "intervention")
        if args.kind != "controlled":
            audit_decisions(tasks)
        result = write_suite(args.output, tasks)
    elif command == "validate":
        tasks = load_tasks(args.suite)
        result = {"tasks": len(tasks), "kind": tasks[0]["kind"], "model_calls": 0,
                  "provenance": sorted({t["provenance"] for t in tasks})}
        if all("candidate_distances" in t.get("metadata", {}) for t in tasks):
            result["audit"] = audit_decisions(tasks)
    elif command == "split":
        from .splits import create_split
        result = create_split(args.suite, args.output, seed=args.seed, ratios=args.ratios, template_key=args.template_key)
    elif command == "audit-split":
        from .splits import audit_split
        result = audit_split(args.manifest)
    elif command == "run":
        result = run_experiment(config, args.suite, args.output, repeats=args.repeats,
                                embeddings=strict_json(args.vectors.read_text()) if args.vectors else None, bfcl_evidence=bfcl_evidence,
                                split_manifest=args.split_manifest, partition=args.partition)
    elif command == "matrix":
        if args.execute and args.output is None:
            raise ValueError("Executing a matrix requires --output")
        result = matrix(args.plan, args.output, execute=args.execute)
    elif command == "replay":
        result = replay_case(args.case_dir, revised_task=strict_json(args.revised_task.read_text()) if args.revised_task else None)
    elif command == "compare":
        result = compare_experiments(args.baseline, args.treatment, resamples=args.resamples, seed=args.seed)
    elif command == "analyze":
        rows = [strict_json(line) for line in args.results.read_text().splitlines() if line.strip()]
        analyze = {"factorial": factorial_contrasts, "intervention": matched_intervention, "bfcl": bfcl_results}[args.kind]
        result = analyze(rows, resamples=args.resamples, seed=args.seed)
    elif command == "import-bfcl":
        result = write_suite(args.output, bfcl_import(args.questions, args.answers, source_revision=args.revision, irrelevant=args.irrelevant))
    elif command == "expand-bfcl":
        result = write_suite(args.output, expand_bfcl(load_tasks(args.suite), strict_json(args.certification.read_text()), seed=args.seed))
    elif command == "review-bfcl":
        from .datasets import bfcl_review_packet
        from .experiments import write_json
        cert, pending = bfcl_review_packet(load_tasks(args.suite), [t for file in args.pool for t in load_tasks(file)], seed=args.seed)
        args.output.mkdir(parents=True, exist_ok=False)
        write_json(args.output / "certification.json", cert)
        write_json(args.output / "pending.json", pending)
        result = {"proposed_pairs": len(pending["pending_pairs"]), "approved_pairs": 0,
                  "ready": False, "reason": "Task-specific semantic review required; proposals are not certifications"}
    elif command == "tau2":
        from .tau_adapter import run_tau_plan
        if args.execute and args.output is None:
            raise ValueError("Executing tau2 requires --output")
        result = run_tau_plan(config, args.plan, args.output, execute=args.execute)
    elif command == "compare-tau2":
        result = compare_tau(args.baseline, args.treatment, resamples=args.resamples)
    elif command == "encode":
        vectors = encode_candidates(load_tasks(args.suite), model_path=args.model_path, revision=args.revision)
        with args.output.open("x") as stream:
            stream.write(json_text(vectors) + "\n")
        result = {"vectors": len(vectors["vectors"]), "output": str(args.output.resolve())}
    elif command == "embedding-audit":
        result = embedding_audit(load_tasks(args.suite), strict_json(args.vectors.read_text()))
    elif command == "match-intervention":
        from .matching import match_intervention
        from .experiments import write_json
        if args.output.exists():
            raise ValueError("Matching output must be a new directory")
        tasks, vectors, report, pool = match_intervention(load_tasks(args.suite), model_path=args.model_path, revision=args.revision)
        args.output.mkdir(parents=True, exist_ok=False)
        result = write_suite(args.output / "suite.jsonl", tasks)
        for name, value in (("vectors.json", vectors), ("matching.json", report), ("pool-vectors.json", pool)):
            write_json(args.output / name, value)
        result["max_absolute_pair_difference"] = max(p["absolute_difference"] for p in report["pairs"])
    elif command == "reprice":
        result = reprice(args.run_dir, strict_json(args.snapshots.read_text()))
    elif command == "audit-suite":
        from .suite_audit import suite_audit
        result = suite_audit(load_tasks(args.suite), args.profile,
                             strict_json(args.vectors.read_text()) if args.vectors else None, bfcl_evidence=bfcl_evidence)
    elif command == "prepare-bfcl":
        from .datasets import prepare_bfcl
        routing, relevance = prepare_bfcl([task for file in args.positive for task in load_tasks(file)],
                                         load_tasks(args.negative), seed=args.seed)
        args.output.mkdir(parents=True, exist_ok=False)
        result = {"routing": write_suite(args.output / "routing-base.jsonl", routing),
                  "relevance": write_suite(args.output / "relevance.jsonl", relevance), "author_subset": False}
    elif command == "report":
        from .reports import pair_report, cross_family_report, repeat_report, hierarchy_report
        from .native_audit import audit_native_run
        if args.kind in {"pair", "hierarchy"} and args.second is None:
            raise ValueError("Pair/hierarchy reports require two complete runs")
        if args.kind == "pair":
            result = pair_report(args.first, args.second, resamples=args.resamples, seed=args.seed)
        elif args.kind == "cross-family":
            result = cross_family_report(args.first, resamples=args.resamples, seed=args.seed)
        elif args.kind == "hierarchy":
            result = hierarchy_report(args.first, args.second, resamples=args.resamples, seed=args.seed)
        elif args.kind == "repeat":
            result = repeat_report(args.first, args.second, first_repeat=args.first_repeat, second_repeat=args.second_repeat)
        else:
            result = audit_native_run(args.first, args.reviews)
    else:
        raise ValueError("Unknown experiment command")
    print(json_text(result))
    return 3 if result.get("complete") is False or result.get("ready") is False else 0
