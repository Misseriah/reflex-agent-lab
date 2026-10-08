from pathlib import Path

from .types import json_text, strict_json


def add_commands(commands):
    risk = commands.add_parser("risk", help="Paired states, finite-rule risk calibration and locked-policy evaluation")
    sub = risk.add_subparsers(dest="risk_command", required=True)
    pair = sub.add_parser("pair")
    pair.add_argument("--plan", type=Path, required=True)
    pair.add_argument("--partition", choices=("dev", "calibration"), default="calibration")
    run = sub.add_parser("run")
    run.add_argument("--policy", type=Path, required=True)
    run.add_argument("--arm", choices=("strong", "policy"), required=True)
    for p in (pair, run):
        p.add_argument("--suite", type=Path, required=True)
        p.add_argument("--split-manifest", type=Path, required=True)
        p.add_argument("--output", type=Path)
        p.add_argument("--execute", action="store_true")
        p.add_argument("--vectors", type=Path)
        p.add_argument("--certification", type=Path)
        p.add_argument("--base-suite", type=Path)
    calibrate = sub.add_parser("calibrate")
    calibrate.add_argument("paired_run", type=Path)
    calibrate.add_argument("--output", type=Path, required=True)
    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("baseline", type=Path)
    evaluate.add_argument("treatment", type=Path)
    evaluate.add_argument("--policy", type=Path, required=True)
    evaluate.add_argument("--output", type=Path)


def execute(args, config):
    from .experiments import load_tasks
    from .risk_experiments import calibrate, collect_pairs, evaluate_policy, run_policy
    if args.risk_command in {"pair", "run"}:
        if bool(args.certification) != bool(args.base_suite):
            raise ValueError("Supply both --certification and --base-suite")
        evidence = {"base_tasks": load_tasks(args.base_suite), "certification": strict_json(args.certification.read_text())} if args.certification else None
        options = {"split_manifest": args.split_manifest, "execute": args.execute,
                   "embeddings": strict_json(args.vectors.read_text()) if args.vectors else None,
                   "bfcl_evidence": evidence}
        if args.risk_command == "pair":
            result = collect_pairs(config, strict_json(args.plan.read_text()), args.suite, args.output,
                                   partition=args.partition, **options)
        else:
            result = run_policy(config, args.policy, args.suite, args.output, arm=args.arm, **options)
    elif args.risk_command == "calibrate":
        result = calibrate(args.paired_run, args.output)
    else:
        result = evaluate_policy(args.baseline, args.treatment, args.policy)
        if args.output:
            with args.output.open("x") as stream:
                stream.write(json_text(result) + "\n")
    print(json_text(result))
    return 3 if result.get("ready") is False or result.get("complete") is False else 0
