from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from xdsh.core.knowledge import KnowledgeIndexError, SessionKnowledgeStore
from xdsh.core.semantic import SemanticEmbeddingError


class KeywordEmbeddingProvider:
    identity = "test-semantic-v1"
    dimensions = 3

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        vectors: list[tuple[float, ...]] = []
        for text in texts:
            folded = text.casefold()
            if any(word in folded for word in ("car", "automobile", "vehicle")):
                vectors.append((1.0, 0.0, 0.0))
            elif any(word in folded for word in ("bill", "invoice", "payment")):
                vectors.append((0.0, 1.0, 0.0))
            else:
                vectors.append((0.0, 0.0, 1.0))
        return tuple(vectors)


class FailingEmbeddingProvider:
    identity = "test-semantic-unavailable"
    dimensions = 3

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        del texts
        raise SemanticEmbeddingError("synthetic outage")


def _record(
    store: SessionKnowledgeStore,
    *,
    chat_id: int,
    session_id: str,
    user: str,
    assistant: str,
) -> None:
    store.record_turn(
        chat_id=chat_id,
        session_id=session_id,
        created_at="2026-08-28T16:00:00Z",
        user_text=user,
        assistant_text=assistant,
        model="test-model",
    )


def test_fts_search_is_chat_and_session_scoped_with_cjk_and_redaction(
    tmp_path: Path,
) -> None:
    store = SessionKnowledgeStore(tmp_path / "state")
    _record(
        store,
        chat_id=42,
        session_id="session-a",
        user="分析 3090 斷線原因 TOKEN=super-secret-value",
        assistant="根因是核心鎖死, 建議保留崩潰證據。",
    )
    _record(
        store,
        chat_id=42,
        session_id="session-b",
        user="Compare bounded retry policy",
        assistant="Use a runner-neutral token and stall budget.",
    )
    _record(
        store,
        chat_id=99,
        session_id="session-a",
        user="3090 斷線是私人內容",
        assistant="This other chat must not be returned.",
    )

    current = store.search(
        chat_id=42,
        session_id="session-a",
        query="斷線原因",
    )
    across_sessions = store.search(chat_id=42, session_id=None, query="runner neutral")

    assert len(current) == 1
    assert current[0].session_id == "session-a"
    assert "super-secret-value" not in current[0].user_excerpt
    assert "[REDACTED]" in current[0].user_excerpt
    assert len(across_sessions) == 1
    assert across_sessions[0].session_id == "session-b"
    assert store.search(chat_id=99, session_id="session-a", query="核心鎖死") == ()
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.path.parent.stat().st_mode & 0o777 == 0o700


def test_retention_is_bounded_per_session_and_chat(tmp_path: Path) -> None:
    store = SessionKnowledgeStore(
        tmp_path / "state",
        max_turns_per_session=2,
        max_turns_per_chat=3,
    )
    for index in range(3):
        _record(
            store,
            chat_id=42,
            session_id="session-a",
            user=f"shared marker old-{index}",
            assistant=f"answer-{index}",
        )
    _record(
        store,
        chat_id=42,
        session_id="session-b",
        user="shared marker newest",
        assistant="newest-answer",
    )

    assert store.count(chat_id=42, session_id="session-a") == 2
    assert store.count(chat_id=42) == 3
    results = store.search(chat_id=42, session_id=None, query="shared marker")
    assert len(results) == 3
    assert all("old-0" not in result.user_excerpt for result in results)


def test_search_recovers_partial_matches_with_deterministic_relevance(tmp_path: Path) -> None:
    store = SessionKnowledgeStore(tmp_path / "state")
    _record(
        store,
        chat_id=42,
        session_id="session-a",
        user="alpha beta",
        assistant="exact candidate",
    )
    _record(
        store,
        chat_id=42,
        session_id="session-a",
        user="alpha beta only",
        assistant="partial candidate",
    )

    results = store.search(
        chat_id=42,
        session_id="session-a",
        query="alpha beta missing",
        limit=2,
    )

    assert [result.assistant_excerpt for result in results] == [
        "exact candidate",
        "partial candidate",
    ]
    assert (results[0].matched_terms, results[0].query_terms) == (2, 3)
    assert (results[1].matched_terms, results[1].query_terms) == (2, 3)


def test_search_partial_recall_handles_cjk_without_relaxing_scope(tmp_path: Path) -> None:
    store = SessionKnowledgeStore(tmp_path / "state")
    _record(
        store,
        chat_id=42,
        session_id="session-a",
        user="核心鎖死與重啟",
        assistant="保留崩潰證據",
    )

    results = store.search(
        chat_id=42,
        session_id="session-a",
        query="核心鎖死 不存在的詞",
    )

    assert len(results) == 1
    assert results[0].matched_terms >= 2
    assert results[0].query_terms >= results[0].matched_terms


def test_search_uses_bounded_typo_fallback_after_exact_candidates(
    tmp_path: Path,
) -> None:
    store = SessionKnowledgeStore(tmp_path / "state")
    _record(
        store,
        chat_id=42,
        session_id="session-a",
        user="configure context compaction",
        assistant="deployment guidance",
    )
    _record(
        store,
        chat_id=42,
        session_id="session-a",
        user="unrelated release note",
        assistant="keep this out of a typo-only search",
    )

    results = store.search(
        chat_id=42,
        session_id="session-a",
        query="context compctation",
    )

    assert len(results) == 1
    assert results[0].approximate is True
    assert results[0].matched_terms == 2
    assert "compaction" in results[0].user_excerpt


def test_optional_semantic_search_recovers_synonyms_without_cross_chat_leak(
    tmp_path: Path,
) -> None:
    store = SessionKnowledgeStore(
        tmp_path / "state",
        semantic_provider=KeywordEmbeddingProvider(),
    )
    _record(
        store,
        chat_id=42,
        session_id="session-a",
        user="Automobile engine maintenance",
        assistant="Inspect the cooling system before replacing parts.",
    )
    _record(
        store,
        chat_id=42,
        session_id="session-a",
        user="Invoice reconciliation",
        assistant="Compare the payment ledger.",
    )
    _record(
        store,
        chat_id=99,
        session_id="session-a",
        user="Private vehicle record",
        assistant="Must remain in the other chat.",
    )

    results = store.search(
        chat_id=42,
        session_id="session-a",
        query="fix car",
    )

    assert len(results) == 1
    assert "Automobile" in results[0].user_excerpt
    assert results[0].approximate is True
    assert results[0].semantic_score == pytest.approx(1.0)
    assert "Private" not in results[0].user_excerpt
    stats = store.stats(chat_id=42, session_id="session-a")
    assert stats.retrieval_mode == "fts5+bounded-fuzzy+semantic"
    assert stats.semantic_provider == "test-semantic-v1"
    assert stats.semantic_turns == 2


def test_semantic_outage_falls_back_to_durable_lexical_index(tmp_path: Path) -> None:
    store = SessionKnowledgeStore(
        tmp_path / "state",
        semantic_provider=FailingEmbeddingProvider(),
    )
    _record(
        store,
        chat_id=42,
        session_id="session-a",
        user="bounded retry policy",
        assistant="keep the partial answer",
    )

    results = store.search(
        chat_id=42,
        session_id="session-a",
        query="bounded retry",
    )

    assert len(results) == 1
    assert results[0].semantic_score is None
    assert store.stats(chat_id=42).semantic_turns == 0


def test_stats_are_content_free_bounded_and_scoped(tmp_path: Path) -> None:
    store = SessionKnowledgeStore(
        tmp_path / "state",
        max_turns_per_session=3,
        max_turns_per_chat=5,
    )
    _record(
        store,
        chat_id=42,
        session_id="session-a",
        user="first user line",
        assistant="first assistant line",
    )
    _record(
        store,
        chat_id=42,
        session_id="session-b",
        user="second user line",
        assistant="second assistant line",
    )

    all_stats = store.stats(chat_id=42)
    session_stats = store.stats(chat_id=42, session_id="session-a")
    empty_stats = store.stats(chat_id=99)

    assert all_stats.turns == 2
    assert all_stats.sessions == 2
    assert all_stats.stored_chars > 0
    assert all_stats.retrieval_mode == "fts5+bounded-fuzzy"
    assert all_stats.as_dict()["stored_chars"] == all_stats.stored_chars
    assert session_stats.turns == 1
    assert session_stats.sessions == 1
    assert empty_stats.index_present is True
    assert empty_stats.turns == 0


def test_stats_without_index_do_not_create_state(tmp_path: Path) -> None:
    store = SessionKnowledgeStore(tmp_path / "state")

    stats = store.stats(chat_id=42)

    assert stats.index_present is False
    assert not store.path.exists()


def test_search_rejects_unscoped_syntax_only_queries_and_unsafe_roots(
    tmp_path: Path,
) -> None:
    store = SessionKnowledgeStore(tmp_path / "state")

    with pytest.raises(KnowledgeIndexError, match="2-240"):
        store.search(chat_id=42, session_id=None, query="x")
    with pytest.raises(KnowledgeIndexError, match="token"):
        store.search(chat_id=42, session_id=None, query='"*')
    with pytest.raises(KnowledgeIndexError, match="chat ID"):
        store.search(chat_id=0, session_id=None, query="valid query")
    with pytest.raises(KnowledgeIndexError, match="不安全"):
        SessionKnowledgeStore(tmp_path / ".xclaude" / "state")


def test_symlinked_database_is_rejected(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    target = tmp_path / "outside.sqlite3"
    target.write_bytes(b"")
    (state / "knowledge.sqlite3").symlink_to(target)
    store = SessionKnowledgeStore(state)

    with pytest.raises(KnowledgeIndexError, match="路徑不安全"):
        _record(
            store,
            chat_id=42,
            session_id="session-a",
            user="valid query",
            assistant="valid answer",
        )
