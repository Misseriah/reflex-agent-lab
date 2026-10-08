PROMPT_VERSION = "bounded-control-v3"

JEV_INSTRUCTIONS = (
    "Choose the single best next action for this agent step using the observable state, "
    "policy and chronological action history. The criteria describe every available action. "
    "Select a tool when it advances the user's request. Choose a clarification action when "
    "essential user information is missing, a completion action when the task is finished, "
    "or escalate for open-ended reasoning. Do not repeat an already completed operation. "
    "Bound arguments are deterministic copies from observed state, not suggested answers. "
    "A null binding means generation is required, not that the action is unavailable."
)

LLM_INSTRUCTIONS = (
    "Select exactly one next action and supply all its arguments. Return only a JSON object "
    'with exactly the keys "action" (an action ID) and "arguments" (an object). '
    "Follow each action's JSON parameter schema; use no unknown keys. You may supply "
    "arguments independently of the bound_arguments template, using only available evidence. "
    "Do not invent IDs, tool results or completed operations. Tool errors in history "
    "are observations: recover or ask the user, and do not claim success from an error. "
    "Use the environment's completion and clarification actions according to their schemas; "
    "some bounded actions require no generated text. Only choose escalate when can_escalate is true. "
    "When selected_action is non-null, supply arguments for that action only, or escalate "
    "if it cannot be carried out. Do not replace it with a different action. "
    "Give the action, not an explanation of your internal reasoning."
)
