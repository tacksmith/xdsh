"""Hermetic fixed-corpus checks for the optional decision layer.

The corpus intentionally exercises only bounded host choices.  It never sends
credentials, prompts, or network requests and is suitable for release gates.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass

from xdsh.core.context import ContextCompactionPolicy, compaction_decision_request
from xdsh.core.decision_router import (
    DecisionOption,
    DecisionRequest,
    DecisionRouter,
    LlmDecisionAdapter,
)
from xdsh.core.queue_policy import analyze_busy_prompt, queue_decision_request
from xdsh.core.rate_limit import retry_decision_request
from xdsh.core.run_guard import poll_decision_request


@dataclass(frozen=True, slots=True)
class DecisionCorpusCase:
    name: str
    request: DecisionRequest
    expected: str


@dataclass(frozen=True, slots=True)
class DecisionCorpusReport:
    cases: int
    rules_correct: int
    shadow_agreements: int
    active_correct: int
    active_fallbacks: int
    unsafe_actions: int = 0

    @property
    def rules_accuracy(self) -> float:
        return self.rules_correct / max(1, self.cases)

    @property
    def active_accuracy(self) -> float:
        return self.active_correct / max(1, self.cases)

    @property
    def fallback_rate(self) -> float:
        return self.active_fallbacks / max(1, self.cases)


def fixed_corpus() -> tuple[DecisionCorpusCase, ...]:
    """Return stable, content-free cases covering every planned decision kind."""

    pressure = ContextCompactionPolicy().evaluate(
        latest_input_tokens=40_000,
        context_window_tokens=80_000,
        retained_turns=4,
        turns_since_compaction=2,
    )
    cases = [
        DecisionCorpusCase(
            "retrieval-dependent",
            DecisionRequest(
                kind="retrieval_plan", scope_id="c1",
                options=(
                    DecisionOption("serial", "Resolve prerequisites in order."),
                    DecisionOption("parallel", "Read independent sources concurrently."),
                ),
                fallback="serial", context=(("mode", "dependent"),),
            ), "serial",
        ),
        DecisionCorpusCase(
            "retrieval-independent",
            DecisionRequest(
                kind="retrieval_plan", scope_id="c2",
                options=(
                    DecisionOption("serial", "Resolve prerequisites in order."),
                    DecisionOption("parallel", "Read independent sources concurrently."),
                ),
                fallback="parallel", context=(("mode", "independent"),),
            ), "parallel",
        ),
        DecisionCorpusCase(
            "poll-limit",
            poll_decision_request(
                tool_name="job-output", stagnant_calls=4, max_stagnant_calls=4
            ),
            "stop",
        ),
        DecisionCorpusCase(
            "poll-progress",
            poll_decision_request(
                tool_name="job-output", stagnant_calls=1, max_stagnant_calls=4
            ),
            "wait",
        ),
        DecisionCorpusCase(
            "retry-exhausted",
            retry_decision_request(
                retryable=True, attempts=1, max_attempts=1, route="provider/model"
            ),
            "report",
        ),
        DecisionCorpusCase(
            "retry-available",
            retry_decision_request(
                retryable=True, attempts=0, max_attempts=1, route="provider/model"
            ),
            "retry",
        ),
        DecisionCorpusCase(
            "queue-supplement",
            queue_decision_request(
                analyze_busy_prompt("整理说明", "补充刚才的说明")
            ),
            "merge",
        ),
        DecisionCorpusCase("compaction-ready", compaction_decision_request(pressure), "compact"),
    ]
    return tuple(cases)


def run_fixed_corpus() -> DecisionCorpusReport:
    """Compare rules with a deterministic shadow adapter and active validation."""

    cases = fixed_corpus()

    def transport(body: bytes, _timeout: float) -> bytes:
        payload = json.loads(body)
        request = payload["messages"][0]["content"]
        # The fake provider reads only the host-created option IDs.  It is a
        # contract test, not a model-quality claim.
        data = json.loads(request)
        kind = data["kind"]
        choice = data["fallback"]
        if kind == "retrieval_plan" and data["context"].get("mode") == "independent":
            choice = "parallel"
        return json.dumps({"choice": choice, "confidence": 0.99}).encode()

    llm = LlmDecisionAdapter(model="fixed-corpus", transport=transport)
    rules_correct = shadow_agreements = active_correct = active_fallbacks = 0
    for case in cases:
        rules = DecisionRouter(mode="rules").decide(case.request)
        shadow = DecisionRouter(mode="shadow", llm=llm).decide(case.request)
        active = DecisionRouter(mode="active", llm=llm).decide(case.request)
        rules_correct += rules.choice == case.expected
        shadow_agreements += shadow.shadow_choice == case.expected
        active_correct += active.choice == case.expected
        active_fallbacks += active.source == "fallback"
    return DecisionCorpusReport(
        cases=len(cases), rules_correct=rules_correct,
        shadow_agreements=shadow_agreements, active_correct=active_correct,
        active_fallbacks=active_fallbacks,
    )


def main() -> None:
    """Run the credential-free decision-layer release gate."""

    parser = argparse.ArgumentParser(prog="xdsh-decision-shadow")
    parser.add_argument("--json", action="store_true", help="emit machine-readable metrics")
    args = parser.parse_args()
    report = run_fixed_corpus()
    payload = {
        "evaluation_type": "simulated_contract_only",
        "active_rollout_eligible": False,
        "cases": report.cases,
        "rules_accuracy": report.rules_accuracy,
        "shadow_agreements": report.shadow_agreements,
        "active_accuracy": report.active_accuracy,
        "fallback_rate": report.fallback_rate,
        "unsafe_actions": report.unsafe_actions,
    }
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(
            "decision shadow: "
            f"{report.cases} cases; rules={report.rules_accuracy:.0%}; "
            f"shadow={report.shadow_agreements}/{report.cases}; "
            f"active={report.active_accuracy:.0%}; "
            f"fallback={report.fallback_rate:.0%}; unsafe={report.unsafe_actions}"
        )


__all__ = [
    "DecisionCorpusCase",
    "DecisionCorpusReport",
    "fixed_corpus",
    "main",
    "run_fixed_corpus",
]


if __name__ == "__main__":
    main()
