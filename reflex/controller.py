from __future__ import annotations

from .types import Action, ActionError, Decision, ProviderError


class Controller:
    """Shared per-step policy. It has no access to task labels or the evaluator."""

    def __init__(self, config, store, tools, jev, strong, small):
        self.config, self.store, self.tools = config, store, tools
        self.jev, self.strong, self.small = jev, strong, small

    def choose(self, state, candidates):
        if self.config.mode not in {"hierarchical_reflex", "hierarchical_probe"}:
            return self.jev.choose(state, candidates), {}
        families = {}
        for candidate in candidates:
            family = candidate.get("family", candidate.get("effect", "actions"))
            families.setdefault(family, []).append(candidate)
        menu = []
        for name, members in families.items():
            descriptions = {c["family_description"] for c in members if c.get("family_description")}
            if len(descriptions) > 1:
                raise ValueError("Conflicting authored descriptions for one tool family")
            description = next(iter(descriptions), "Family containing these operations")
            menu.append({"id": name, "description": description + ": " + "; ".join(
                c["description"] for c in members), "members": members})
        first = self.jev.choose(state, menu)
        evidence = {"family_choice": first.choice, "family_confidence": first.confidence,
                    "family_probabilities": first.probabilities}
        if self.config.mode != "hierarchical_probe" and first.confidence < self.config.threshold:
            return first, {**evidence, "family_rejected": True}
        second = self.jev.choose(state, families[first.choice])
        # Both stages must pass. This is a declared reconstruction, not an author-specified joint score.
        return Decision(second.choice, min(first.confidence, second.confidence), second.probabilities), evidence

    def select(self, state, candidates):
        mode = self.config.mode
        if mode == "strong_only":
            return self.strong.act(state, candidates), {"source": "strong", "reason": "B0"}
        if mode == "small_only":
            return self.small.act(state, candidates), {"source": "small", "reason": "B1"}
        if mode == "cascade":
            action = self.small.act(state, candidates, can_escalate=True)
            if action.action == "escalate" and not action.arguments:
                return self.strong.act(state, candidates), {"source": "strong", "reason": "B3_self_escalation"}
            return action, {"source": "small", "reason": "B3"}
        if mode in {"matched_jev", "matched_small"}:
            return self.select_matched(state, candidates)
        if mode in {"decision_only", "hierarchical_probe"}:
            decision, hierarchy = self.choose(state, candidates)
            return Action(decision.choice, self.tools.bind(decision.choice, state) or {}), {
                "source": "jev", "reason": "ungated_probe", "choice": decision.choice,
                "confidence": decision.confidence, "probabilities": decision.probabilities, **hierarchy}
        gate = {"source": "strong", "threshold": self.config.threshold}
        try:
            decision, hierarchy = self.choose(state, candidates)
        except ProviderError as exc:
            gate.update(reason="jev_provider_error", error=str(exc))
        else:
            gate.update(choice=decision.choice, confidence=decision.confidence,
                        probabilities=decision.probabilities, **hierarchy)
            if decision.confidence < self.config.threshold:
                gate["reason"] = "low_confidence"
            else:
                arguments = self.tools.bind(decision.choice, state)
                if arguments is None:
                    gate["reason"] = "generation_or_reasoning_required"
                    if mode == "reflex_executor":
                        action = self.small.act(state, candidates, can_escalate=True,
                                                selected_action=decision.choice)
                        if action.action == decision.choice:
                            try:
                                self.tools.check(action, state)
                            except ActionError as exc:
                                gate.update(reason="executor_argument_error", executor_error=str(exc))
                            else:
                                return action, {**gate, "source": "small", "reason": "cheap_executor"}
                        elif action.action == "escalate" and not action.arguments:
                            gate["reason"] = "executor_escalation"
                        else:
                            gate["reason"] = "executor_selection_error"
                else:
                    proposed = Action(decision.choice, arguments)
                    try:
                        self.tools.check(proposed, state)
                    except ActionError as exc:
                        gate.update(reason="not_executable", error=str(exc), error_type=exc.category)
                    else:
                        return proposed, {**gate, "source": "jev", "reason": "accepted"}
        self.store.event(state.session_id, "fallback", {"step": state.steps, **gate})
        return self.strong.act(state, candidates), gate

    def select_matched(self, state, candidates):
        """Same executor and fallback; only the bounded choice provider changes."""
        selector = "jev" if self.config.mode == "matched_jev" else "small"
        gate = {"source": selector, "decision_provider": selector, "confidence_used": False,
                "confidence": None, "probabilities": None, "threshold": None}
        # Provider failures invalidate a matched run, never silently replace an arm.
        if selector == "jev":
            decision = self.jev.choose(state, candidates)
            choice = decision.choice
            gate.update(confidence=decision.confidence, probabilities=decision.probabilities)
        else:
            choice = self.small.choose(state, candidates)
        gate["choice"] = choice
        arguments = self.tools.bind(choice, state)
        if arguments is None:
            action = self.small.act(state, candidates, can_escalate=True, selected_action=choice)
            if action.action != choice:
                gate["reason"] = "executor_escalation" if action == Action("escalate", {}) else "executor_selection_error"
            else:
                try:
                    self.tools.check(action, state)
                except ActionError as exc:
                    gate.update(reason="executor_argument_error", executor_error=str(exc))
                else:
                    return action, {**gate, "source": "small", "reason": "cheap_executor"}
        else:
            action = Action(choice, arguments)
            try:
                self.tools.check(action, state)
            except ActionError as exc:
                gate.update(reason="not_executable", error=str(exc), error_type=exc.category)
            else:
                return action, {**gate, "reason": "accepted"}
        gate["source"] = "strong"
        self.store.event(state.session_id, "fallback", {"step": state.steps, **gate})
        return self.strong.act(state, candidates), gate
