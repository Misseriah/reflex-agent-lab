from __future__ import annotations

import argparse
import fcntl
import sys
from dataclasses import replace
from pathlib import Path

from .agent import Agent
from .config import Config, MODES
from .billing import budget_status
from .evaluation import compare_runs, load_suite, run_suite
from .storage import Store
from .tools import ToolRegistry
from .types import ConfigError, json_text


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="REFLEX baseline agent with real provider adapters")
    p.add_argument("--config", type=Path, default=Path("config.toml"))
    p.add_argument("--db", type=Path, default=Path("runs/workspace.sqlite3"))
    p.add_argument("--mode", choices=MODES)
    p.add_argument("--threshold", type=float)
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Validate local configuration without contacting providers")
    commands.add_parser("init", help="Initialize the local support workspace (no model calls)")
    commands.add_parser("tools", help="Show registered tool/control schemas")
    run = commands.add_parser("run", help="Start a model-driven episode")
    run.add_argument("request")
    run.add_argument("--customer", default="C-100")
    run.add_argument("--allow-refunds", action="store_true")
    for sub in (run,):
        add_bindings(sub)
    chat = commands.add_parser("chat", help="Interactive session, including clarification replies")
    chat.add_argument("--customer", default="C-100")
    chat.add_argument("--allow-refunds", action="store_true")
    add_bindings(chat)
    resume = commands.add_parser("resume", help="Reply to an agent's clarification")
    resume.add_argument("session_id")
    resume.add_argument("reply")
    add_bindings(resume)
    retry = commands.add_parser("retry", help="Resume after a provider error or process interruption")
    retry.add_argument("session_id")
    show = commands.add_parser("show", help="Inspect a saved session and metrics")
    show.add_argument("session_id")
    export = commands.add_parser("export", help="Export raw calls, events and observations")
    export.add_argument("session_id")
    export.add_argument("--output", type=Path, required=True)
    evaluate = commands.add_parser("eval", help="Run the independently authored local acceptance suite")
    evaluate.add_argument("--suite", type=Path, default=Path("examples/acceptance.jsonl"))
    evaluate.add_argument("--output", type=Path)
    evaluate.add_argument("--validate-only", action="store_true")
    compare = commands.add_parser("compare", help="Compare complete, matched B0/R1 local runs")
    compare.add_argument("baseline", type=Path)
    compare.add_argument("reflex", type=Path)
    from .experiment_cli import add_commands
    add_commands(commands)
    return p


def add_bindings(p):
    p.add_argument("--order", help="Order ID supplied by the application/user, not a predicted answer")
    p.add_argument("--ticket")
    p.add_argument("--query")
    p.add_argument("--note-body")


def bindings(args):
    return {key: value for key, value in {
        "order_id": args.order, "ticket_id": args.ticket,
        "query": args.query, "note_body": args.note_body}.items() if value is not None}


def display(state, store):
    for item in state.history:
        gate = item["gate"]
        outcome = "ok" if item["result"]["ok"] else item["result"]["error_type"]
        print(f"[{item['step']}] {gate['source']} -> {item['action']} ({gate['reason']}; {outcome})")
    if state.messages[-1]["role"] == "assistant":
        print("\n" + state.messages[-1]["content"])
    print(f"\nsession={state.session_id} status={state.status}")
    print(json_text(store.metrics(state.session_id)))
    if state.error:
        print(state.error, file=sys.stderr)


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    store = None
    lock = None
    try:
        config = Config.load(args.config, args.mode, args.threshold)
        if args.command == "experiment":
            from .experiment_cli import execute
            return execute(args, config)
        if args.command == "doctor":
            problems = config.problems()
            print(json_text({"ready": not problems, "mode": config.mode, "problems": problems,
                             "live_api_tested": False, "required_providers": config.required_roles(),
                             "budget": budget_status(config)}))
            return 2 if problems else 0
        if args.command == "eval":
            if args.validate_only:
                tasks = load_suite(args.suite)
                print(json_text({"valid_tasks": len(tasks), "model_calls": 0, "paper_benchmark": False}))
                return 0
            if args.output is None:
                raise ValueError("eval requires --output for a new experiment directory")
            result = run_suite(config, args.suite, args.output)
            print(json_text(result))
            return 0 if result["complete"] else 3
        if args.command == "compare":
            print(json_text(compare_runs(args.baseline, args.reflex)))
            return 0
        if args.command in {"run", "chat"}:
            config.require_ready()
        args.db.parent.mkdir(parents=True, exist_ok=True)
        lock = Path(str(args.db) + ".lock").open("a")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store = Store(args.db)
        if args.command == "init":
            store.seed()
            print(f"Initialized local SQLite workspace: {args.db.resolve()}")
            print("Customers: C-100, C-200. Example orders: O-100, O-101, O-102, O-200. Model calls: 0.")
            return 0
        if args.command == "tools":
            print(json_text([{ "name": s.name, "description": s.description, "parameters": s.schema()}
                             for s in ToolRegistry(store).specs.values()]))
            return 0
        if args.command in {"show", "export"}:
            exported = store.export(args.session_id)
            if args.command == "show":
                display(store.load(args.session_id), store)
            else:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with args.output.open("x") as stream:
                    stream.write(json_text(exported) + "\n")
                print(str(args.output.resolve()))
            return 0
        if args.command in {"resume", "retry"}:
            saved = store.load(args.session_id)
            if args.mode is not None and args.mode != saved.mode:
                raise ValueError("Cannot change mode when resuming")
            if args.threshold is not None and args.threshold != saved.threshold:
                raise ValueError("Cannot change threshold when resuming")
            config = replace(config, mode=saved.mode, threshold=saved.threshold)
        agent = Agent(config, store)
        if args.command == "resume":
            state = agent.resume(args.session_id, args.reply, bindings(args))
        elif args.command == "retry":
            config.require_ready()
            agent._check_manifest(saved)
            if saved.status not in {"provider_error", "running"}:
                raise ValueError("retry requires an interrupted session or provider_error")
            saved.status, saved.error = "running", None
            state = saved
            store.save(state)
            store.event(state.session_id, "manual_retry", {})
        else:
            request = args.request if args.command == "run" else input("You: ")
            state = agent.start(request, args.customer, ["refund"] if args.allow_refunds else [], bindings(args))
        while True:
            state = agent.run(state)
            display(state, store)
            if args.command != "chat" or state.status != "waiting_for_user":
                break
            try:
                reply = input("\nYou (Ctrl-D to keep this session paused): ")
            except EOFError:
                break
            state = agent.resume(state.session_id, reply)
        return 0 if state.status in {"completed", "waiting_for_user"} else 3
    except BlockingIOError:
        print("ERROR: Another process is using this workspace; use a different --db.", file=sys.stderr)
        return 2
    except (ConfigError, ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted. Saved checkpoints remain available via show/retry.", file=sys.stderr)
        return 130
    finally:
        if store:
            store.close()
        if lock:
            lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
