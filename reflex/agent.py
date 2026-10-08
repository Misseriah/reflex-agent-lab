from __future__ import annotations

import hashlib
import time
from pathlib import Path
from uuid import uuid4

from . import __version__
from .config import Config
from .controller import Controller
from .prompts import PROMPT_VERSION
from .providers import ChatClient, HttpTransport, JevClient
from .storage import SEED_KNOWLEDGE, SEED_ORDERS, SEED_VERSION, Store
from .tools import POLICY, ToolRegistry
from .types import Action, ActionError, ConfigError, ProviderError, State, digest


def manifest(config: Config) -> dict:
    source = hashlib.sha256()
    for path in sorted(Path(__file__).parent.rglob("*.py")):
        source.update(str(path.relative_to(Path(__file__).parent)).encode())
        source.update(path.read_bytes())
    return {"implementation": __version__, "source_sha256": source.hexdigest(),
            "prompt_version": PROMPT_VERSION, "seed_version": SEED_VERSION,
            "seed_sha256": digest([SEED_ORDERS, SEED_KNOWLEDGE]),
            "config": config.public(), "policy_sha256": digest(POLICY)}


class Agent:
    def __init__(self, config: Config, store: Store):
        self.config = config
        self.store = store
        self.tools = ToolRegistry(store)
        transport = HttpTransport(config, store)
        self.jev = JevClient(transport)
        self.strong = ChatClient(transport, "strong")
        self.small = ChatClient(transport, "small")

    def start(self, request: str, customer_id: str = "C-100", scopes: list[str] | None = None,
              bindings: dict[str, str] | None = None) -> State:
        if self.config.mode in {"decision_only", "hierarchical_probe"}:
            raise ConfigError("Single-decision probe modes require the experiment runner")
        self.config.require_ready()
        if not request.strip():
            raise ValueError("User request is empty")
        if not self.store.db.execute("SELECT 1 FROM customers WHERE id=?", (customer_id,)).fetchone():
            raise ValueError("Unknown local customer; run init first")
        scopes = scopes or []
        if set(scopes) - {"refund"}:
            raise ValueError("Unknown application scopes")
        self._validate_bindings(bindings or {})
        state = State(uuid4().hex, customer_id, scopes, self.config.mode, self.config.threshold,
                      manifest(self.config), messages=[{"role": "user", "content": request}],
                      bindings=dict(bindings or {}))
        with self.store.transaction():
            self.store.save(state)
            self.store.event(state.session_id, "started", {"manifest": state.manifest})
        return state

    @staticmethod
    def _validate_bindings(bindings):
        if set(bindings) - {"order_id", "ticket_id", "query", "note_body"}:
            raise ValueError("Unknown bindings; allowed: order_id, ticket_id, query, note_body")
        if any(not isinstance(v, str) or not v.strip() or len(v) > 4000 for v in bindings.values()):
            raise ValueError("Bindings must be nonempty strings up to 4000 characters")

    def resume(self, session_id: str, reply: str, bindings: dict[str, str] | None = None) -> State:
        self.config.require_ready()
        state = self.store.load(session_id)
        self._check_manifest(state)
        if state.status != "waiting_for_user":
            raise ValueError("Only a session waiting for user input can accept a reply")
        if not reply.strip():
            raise ValueError("Reply is empty")
        self._validate_bindings(bindings or {})
        # A reply may correct the order or ticket. Historical observations remain
        # visible, but stale bindings must not become this turn's tool arguments.
        state.bindings = dict(bindings or {})
        state.messages.append({"role": "user", "content": reply})
        state.status = "running"
        state.error = None
        with self.store.transaction():
            self.store.save(state)
            self.store.event(state.session_id, "user_reply", {"message": reply, "bindings": bindings or {}})
        return state

    def _check_manifest(self, state: State):
        if state.manifest != manifest(self.config):
            raise ConfigError("Session code/config differs from its manifest; start a new session")

    def _select(self, state: State, candidates: list[dict]) -> tuple[Action, dict]:
        return Controller(self.config, self.store, self.tools, self.jev,
                          self.strong, self.small).select(state, candidates)

    def run(self, state: State) -> State:
        self.config.require_ready()
        self._check_manifest(state)
        if state.status != "running":
            raise ValueError(f"Session is {state.status}; it is not runnable")
        began = time.monotonic()
        try:
            while state.steps < self.config.max_steps:
                state.steps += 1
                candidates = self.tools.candidates(state)
                action, gate = self._select(state, candidates)
                step = {"step": state.steps, "action": action.action, "arguments": action.arguments,
                        "gate": gate, "candidate_ids": [c["id"] for c in candidates]}
                # Tool writes, receipts, observations and checkpoint commit together.
                with self.store.transaction():
                    try:
                        self.tools.check(action, state)
                        if action.action in {"finish", "ask_clarification"}:
                            message = action.arguments["message"]
                            state.messages.append({"role": "assistant", "content": message})
                            state.status = "completed" if action.action == "finish" else "waiting_for_user"
                            result = {"ok": True, "data": {"message": message}}
                        elif action.action == "escalate":
                            raise ActionError("tool_selection_error", "No further escalation is available")
                        else:
                            result = self.tools.execute(action, state, f"{state.session_id}:{state.steps}")
                    except ActionError as exc:
                        result = {"ok": False, "error_type": exc.category, "message": str(exc)}
                    step["result"] = result
                    state.history.append(step)
                    self.tools.observe(state, action.action, result)
                    self.store.event(state.session_id, "step", step)
                    self.store.save(state)
                if state.status != "running":
                    return state
            state.status = "step_limit"
            state.error = "Maximum episode steps reached"
            self.store.save(state)
            return state
        except (ProviderError, ConfigError) as exc:
            state.status = "provider_error"
            state.error = str(exc)
            self.store.save(state)
            self.store.event(state.session_id, "provider_error", {"step": state.steps, "message": str(exc)})
            return state
        finally:
            self.store.event(state.session_id, "run_end", {"status": state.status,
                             "wall_time_ms": round((time.monotonic() - began) * 1000, 3)})
