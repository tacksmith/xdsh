from __future__ import annotations

from xdsh.core.execution_policy import (
    DIRECT_EXECUTION_POLICY,
    OPTIONAL_SKILL_ROUTING_POLICY,
    with_direct_execution_policy,
    with_optional_skill_routing_policy,
)


def test_policy_requires_bounded_planning_then_direct_execution() -> None:
    prompt = with_direct_execution_policy("Base operator instructions.")

    assert prompt.startswith("Base operator instructions.")
    assert "briefly identify the goal" in prompt
    assert "shortest safe path" in prompt
    assert "unnecessary abstractions" in prompt
    assert prompt.count("Execution policy:") == 1


def test_policy_is_idempotent_and_handles_empty_base() -> None:
    assert with_direct_execution_policy("") == DIRECT_EXECUTION_POLICY
    composed = with_direct_execution_policy("Base")
    assert with_direct_execution_policy(composed) == composed


def test_material_decisions_require_confirmation_not_silent_scope_reduction() -> None:
    assert (
        "Before changing scope, accuracy, methodology or authorization" in DIRECT_EXECUTION_POLICY
    )
    assert "2-3 concrete options" in DIRECT_EXECUTION_POLICY
    assert "wait for the user's choice" in DIRECT_EXECUTION_POLICY
    assert "Do not substitute static review for requested execution" in DIRECT_EXECUTION_POLICY
    assert "Respect already delegated choices" in DIRECT_EXECUTION_POLICY


def test_search_stops_without_evidence_gain_and_dates_match_events() -> None:
    assert "two batches without new decisive evidence" in DIRECT_EXECUTION_POLICY
    assert "Dates conflict only for the same event" in DIRECT_EXECUTION_POLICY
    assert "Scripts written are not results reproduced" in DIRECT_EXECUTION_POLICY


def test_conversation_only_recall_does_not_trigger_tools_or_durable_memory() -> None:
    assert "respond directly without tools or Skills" in DIRECT_EXECUTION_POLICY
    assert "In-session recall is not a durable-memory write" in DIRECT_EXECUTION_POLICY
    assert "explicit scope and approval" in DIRECT_EXECUTION_POLICY


def test_policy_defaults_to_low_token_parallelism_without_agent_fanout() -> None:
    policy = DIRECT_EXECUTION_POLICY

    assert "at most four non-overlapping queries" in policy
    assert "one batch-capable search call" in policy
    assert "declares them concurrency-safe" in policy
    assert "Keep writes, edits, installs, state changes" in policy
    assert "Do not create subagents solely for speed" in policy
    assert "Share an evidence budget" in policy
    assert len(policy) <= 4_500


def test_retrieval_and_intent_policy_has_evidence_and_authority_boundaries() -> None:
    policy = with_direct_execution_policy("")
    assert "gate each step on dependencies" in policy
    assert "authority and relevance" in policy
    assert "genuinely independent primary sources" in policy
    assert "are not proof of absence" in policy
    assert "Separate platform API availability" in policy
    assert "one brief optional next-step question" in policy
    assert "suggestions never authorize additional actions" in policy


def test_every_runner_prompt_prescribes_official_sources_before_other_sources() -> None:
    policy = with_direct_execution_policy("Custom operator instructions.")
    assert "Official sources first, then other sources." in policy
    assert "Verify ownership and freshness" in policy
    assert "within each stage" in policy
    assert len(DIRECT_EXECUTION_POLICY) <= 4_500


def test_todo_milestones_require_native_updates_not_final_only_prose() -> None:
    assert "via the native tool BEFORE the next step" in DIRECT_EXECUTION_POLICY
    assert "send the full list" in DIRECT_EXECUTION_POLICY
    assert "Never batch completions at the end" in DIRECT_EXECUTION_POLICY


def test_optional_skill_policy_is_bounded_idempotent_and_separate() -> None:
    prompt = with_optional_skill_routing_policy("Base operator instructions.")

    assert prompt.startswith("Base operator instructions.")
    assert "at most one primary procedural" in prompt
    assert "Never combine overlapping" in prompt
    assert prompt.count("Optional Skill routing policy:") == 1
    assert with_optional_skill_routing_policy(prompt) == prompt
    assert with_optional_skill_routing_policy("") == OPTIONAL_SKILL_ROUTING_POLICY
    assert "Optional Skill routing policy:" not in with_direct_execution_policy("Base")


def test_evidence_stopping_and_interaction_contract_is_delivered_without_answer_facts() -> None:
    policy = with_direct_execution_policy("")
    for clause in (
        "model_availability", "do not hunt for credentials", "partial/empty output",
        "field meanings", "reset windows", "current task", "each milestone",
        "changes the next action", "exact patch changelog",
    ):
        assert clause in policy
    assert "v24.21.0" not in policy and "deepseek-v4-flash" not in policy


def test_answer_style_is_compact_but_preserves_user_detail_and_evidence() -> None:
    policy = with_direct_execution_policy("Custom operator prompt")
    for clause in (
        "conclusion first", "short paragraphs", "blank lines", "flat list",
        "no decorative headings", "Do not add a word-count report",
        "Explicit detail/code/format requests override brevity",
        "never omit evidence, caveats or required steps",
        "<=200 Chinese characters or <=100 words; never pad",
        "process/tool-compliance narration",
    ):
        assert clause in policy
    assert len(policy) < 4_550
