from __future__ import annotations

import hashlib
import os
from copy import deepcopy
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from uuid import uuid4

from jsonschema import Draft202012Validator, ValidationError

from .agent import manifest
from .controller import Controller
from .environments import validate_schema
from .experiments import write_json
from .providers import ChatClient, HttpTransport, JevClient
from .storage import Store
from .types import ActionError, ProviderError, State, digest, json_text, strict_json

TAU_VERSION = "1.0.1"
TESTED_REVISION = "fc0055dc4e0a316c3f83133267fbd6faaa770992"
RESPOND = "__respond"


class NativeMenu:
    def __init__(self, functions):
        self.functions = {f["name"]: deepcopy(f) for f in functions}
        if len(self.functions) != len(functions) or RESPOND in self.functions or "escalate" in self.functions:
            raise ValueError("Duplicate/reserved native tool name")
        self.functions[RESPOND] = {"name": RESPOND, "description": "Send a message to the user, including clarification or a final response.",
            "parameters": {"type": "object", "properties": {"message": {"type": "string", "minLength": 1}},
                           "required": ["message"], "additionalProperties": False}}
        for f in self.functions.values():
            validate_schema(f["parameters"])

    def bind(self, name, state):
        schema = self.functions[name]["parameters"]
        return {} if Draft202012Validator(schema).is_valid({}) else None

    def candidates(self, state):
        return [{"id": f["name"], "description": f["description"], "parameters": f["parameters"],
                 "effect": "control" if f["name"] == RESPOND else "tool",
                 "bound_arguments": self.bind(f["name"], state)} for f in self.functions.values()]

    def check(self, action, state):
        if action.action not in self.functions:
            raise ActionError("tool_selection_error", "Unknown native function")
        try:
            Draft202012Validator(self.functions[action.action]["parameters"]).validate(action.arguments)
        except ValidationError as exc:
            raise ActionError("argument_error", exc.message) from exc


def create_agent_class():
    """Imports the optional, version-pinned native benchmark only when requested."""
    from tau2.agent.base_agent import HalfDuplexAgent, is_valid_agent_history_message
    from tau2.data_model.message import AssistantMessage, MultiToolMessage, ToolCall

    class ReflexTauAgent(HalfDuplexAgent):
        def __init__(self, tools, domain_policy, config, store):
            super().__init__(tools=tools, domain_policy=domain_policy)
            self.config, self.store = config, store
            self.menu = NativeMenu([t.openai_schema["function"] for t in tools])
            transport = HttpTransport(config, store)
            self.controller = Controller(config, store, self.menu, JevClient(transport),
                                         ChatClient(transport, "strong"), ChatClient(transport, "small"))
            self.session_id, self.control_errors, self.argument_errors = None, 0, 0
            self.provider_error = None

        @staticmethod
        def visible(message):
            # Never forward raw_data, task goals, user scenario instructions, or audio gold scripts.
            value = {"role": message.role, "content": message.content}
            calls = getattr(message, "tool_calls", None)
            if calls:
                value["tool_calls"] = [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in calls]
            if message.role == "tool":
                value.update(id=message.id, error=message.error)
            return value

        def get_init_state(self, message_history=None):
            state = State(uuid4().hex, "native_tau", [], self.config.mode, self.config.threshold,
                          manifest(self.config), policy=self.domain_policy)
            self.session_id = state.session_id
            for message in message_history or []:
                if is_valid_agent_history_message(message):
                    state.messages.append(self.visible(message))
            self.store.save(state)
            return state

        def generate_next_message(self, message, state):
            incoming = message.tool_messages if isinstance(message, MultiToolMessage) else [message]
            state.messages.extend(self.visible(m) for m in incoming)
            state.steps += 1
            before = len(self.store.calls(state.session_id))
            try:
                action, gate = self.controller.select(state, self.menu.candidates(state))
            except Exception as exc:
                self.provider_error = f"{type(exc).__name__}: {exc}"
                self.store.save(state)
                raise
            if gate.get("reason") == "jev_provider_error":
                self.provider_error = "Jev provider failure changed the controller path"
            try:
                self.menu.check(action, state)
            except ActionError as exc:
                self.control_errors += exc.category == "tool_selection_error"
                self.argument_errors += exc.category == "argument_error"
                self.store.event(state.session_id, "native_action_check", {"error_type": exc.category, "step": state.steps})
                if action.action == RESPOND:
                    raise
                # Native environment handles invalid tool calls and exposes their errors for recovery.
            calls = self.store.calls(state.session_id)[before:]
            costs = [c["payload"]["cost_usd"] for c in calls]
            cost = sum(costs) if all(c is not None for c in costs) else None
            if action.action == RESPOND:
                reply = AssistantMessage(role="assistant", content=action.arguments["message"], cost=cost)
            else:
                reply = AssistantMessage(role="assistant", tool_calls=[ToolCall(id=uuid4().hex,
                    name=action.action, arguments=action.arguments, requestor="assistant")], cost=cost)
            state.messages.append(self.visible(reply))
            state.history.append({"step": state.steps, "action": action.action, "arguments": action.arguments, "gate": gate})
            self.store.event(state.session_id, "step", state.history[-1])
            self.store.save(state)
            return reply, state

    return ReflexTauAgent


def run_tau_plan(config, plan_path, output, *, execute=False):
    try:
        installed = version("tau2")
    except PackageNotFoundError as exc:
        raise ValueError("Optional tau2==1.0.1 is not installed; see EXPERIMENTS.md") from exc
    if installed != TAU_VERSION:
        raise ValueError(f"Expected tau2 {TAU_VERSION}, found {installed}; do not mix benchmark versions")
    plan_path = Path(plan_path)
    plan = strict_json(plan_path.read_text())
    if set(plan) != {"data_dir", "task_split", "tasks", "user_model", "user_args", "repeats", "seed"}:
        raise ValueError("tau2 plan requires data_dir/task_split/tasks/user_model/user_args/repeats/seed")
    if not plan["tasks"] or type(plan["repeats"]) is not int or plan["repeats"] < 1 or type(plan["seed"]) is not int:
        raise ValueError("Invalid tau2 plan counts/seed")
    if not isinstance(plan["user_args"], dict) or any("key" in k.lower() or "token" == k.lower() for k in plan["user_args"]):
        raise ValueError("Keep simulator credentials in environment variables, not the frozen plan")
    data_dir = (plan_path.parent / plan["data_dir"]).resolve()
    if not data_dir.is_dir():
        raise ValueError("tau2 data_dir does not exist")
    os.environ["TAU2_DATA_DIR"] = str(data_dir)
    import tau2
    from tau2.data_model.simulation import TextRunConfig
    from tau2.registry import registry
    from tau2.runner import get_tasks, run_single_task
    from tau2.utils.utils import DATA_DIR
    if DATA_DIR.resolve() != data_dir:
        raise ValueError("tau2 was already imported with a different data directory; start a fresh process")
    selected = _select_tasks(plan, get_tasks)
    source = hashlib.sha256()
    package = Path(tau2.__file__).parent
    for file in sorted(package.rglob("*.py")):
        source.update(str(file.relative_to(package)).encode())
        source.update(file.read_bytes())
    data_hash = hashlib.sha256()
    for file in sorted(data_dir.rglob("*")):
        if file.is_file() and "simulations" not in file.relative_to(data_dir).parts:
            data_hash.update(str(file.relative_to(data_dir)).encode())
            data_hash.update(file.read_bytes())
    report = {"tau2_version": installed, "tested_interface_revision": TESTED_REVISION,
              "installed_source_sha256": source.hexdigest(), "data_sha256": data_hash.hexdigest(),
              "suite_sha256": digest([(d, t.model_dump(mode="json")) for d, t in selected]),
              "tasks": len(selected), "repeats": plan["repeats"], "configuration": config.public(),
              "problems": config.problems(), "executed": False, "paper_task_subset": False}
    if not isinstance(plan["user_model"], str) or not plan["user_model"].strip():
        report["problems"].append("User simulator model is not configured")
    if config.budget:
        report["problems"].append("The shared CNY pilot budget does not cover tau2's external user simulator; execution is blocked")
    if config.mode not in {"strong_only", "small_only", "cascade", "reflex_executor"}:
        raise ValueError("Native multi-turn arms use B0/B1/B3/reflex_executor, not main-experiment R1")
    if not execute:
        return report
    config.require_ready()
    if report["problems"]:
        raise ValueError("; ".join(report["problems"]))
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "manifest.json", {**manifest(config), **report, "plan": plan})
    agent_class = create_agent_class()
    rows = []
    for repeat in range(plan["repeats"]):
        for index, (domain, task) in enumerate(selected):
            case = output / f"r{repeat:03}-case{index:04}"
            case.mkdir()
            store, agents = Store(case / "agent.sqlite3"), []
            def factory(tools, domain_policy, **_ignored):
                # The native factory also offers task/user-scenario objects. Deliberately discard them.
                agent = agent_class(tools, domain_policy, config, store)
                agents.append(agent)
                return agent
            agent_name = "reflex_" + uuid4().hex
            registry.register_agent_factory(factory, agent_name)
            native_config = TextRunConfig(domain=domain, agent=agent_name,
                llm_agent="configured_by_reflex", llm_user=plan["user_model"],
                llm_args_user=plan["user_args"], max_steps=config.max_steps,
                enforce_communication_protocol=True)
            try:
                result = run_single_task(native_config, task, seed=plan["seed"] + repeat, auto_review=False)
                write_json(case / "native_result.json", result.model_dump(mode="json"))
                if len(agents) != 1 or result.reward_info is None:
                    raise ValueError("Native runner did not produce exactly one agent and a reward")
                agent = agents[0]
                from .native_audit import native_failure_audit
                native_audit = native_failure_audit(result, task)
                write_json(case / "native_task.json", task.model_dump(mode="json"))
                failure = _infrastructure_error(result.termination_reason, agent.provider_error)
                row = {"task_id": domain + ":" + task.id, "family": domain + ":" + task.id,
                       "case_dir": case.name,
                       "domain": domain, "repeat": repeat, "native_reward": result.reward_info.reward,
                       "success": None if failure else result.reward_info.reward == 1,
                       "infrastructure_error": failure,
                       "termination_reason": result.termination_reason,
                       "schema_control_errors": agent.control_errors, "schema_argument_errors": agent.argument_errors,
                       "native_audit": native_audit,
                       "native_evidence_sha256": digest({"task": task.model_dump(mode="json"), "result": result.model_dump(mode="json")}),
                       "agent_metrics": store.metrics(agent.session_id),
                       "user_simulator_cost_usd": _user_cost(result.messages)}
                write_json(case / "agent_calls.json", store.calls(agent.session_id))
            except Exception as exc:
                row = {"task_id": domain + ":" + task.id, "family": domain + ":" + task.id,
                       "repeat": repeat, "success": None,
                       "infrastructure_error": f"{type(exc).__name__}: {exc}"}
            finally:
                store.close()
            rows.append(row)
            (output / "results.jsonl").write_text("".join(json_text(r) + "\n" for r in rows))
            if row["success"] is None:
                incomplete = {**report, "executed": True, "complete": False, "results": rows}
                write_json(output / "summary.json", incomplete)
                return incomplete
    result = {**report, "executed": True, "complete": True,
              "success_rate": sum(r["success"] for r in rows) / len(rows), "results": rows}
    write_json(output / "summary.json", result)
    return result


def _select_tasks(plan, loader):
    groups, order = {}, []
    for item in plan["tasks"]:
        if set(item) != {"domain", "task_id"}:
            raise ValueError("Invalid native task selection")
        key = item["domain"], item["task_id"]
        if key in order:
            raise ValueError("Duplicate native task selection")
        order.append(key)
        groups.setdefault(item["domain"], []).append(item["task_id"])
    selected = {}
    # The official loader parses the entire domain before filtering task IDs.
    for domain, ids in groups.items():
        tasks = loader(domain, task_split_name=plan["task_split"], task_ids=ids)
        if len(tasks) != len(ids) or {t.id for t in tasks} != set(ids):
            raise ValueError("Native loader returned a different task selection")
        selected.update({(domain, t.id): t for t in tasks})
    return [(domain, selected[domain, task_id]) for domain, task_id in order]


def _infrastructure_error(termination_reason, provider_error):
    if provider_error:
        return provider_error
    # Native runners can return a zero reward instead of raising simulator/API errors.
    if termination_reason in {"user_error", "infrastructure_error", "unexpected_error",
                              "timeout", "context_window_exceeded"}:
        return f"Native run is not a valid controller outcome: {termination_reason}"
    return None


def _user_cost(messages):
    costs = [m.cost for m in messages if m.role == "user"]
    return sum(costs) if costs and all(c is not None for c in costs) else None
