"""Chat-scoped full-text index for bounded, redacted successful turns."""

from __future__ import annotations

import math
import os
import re
import sqlite3
import struct
from contextlib import closing
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from xdsh.core.product_control import redact_sensitive_text
from xdsh.core.semantic import (
    EmbeddingProvider,
    SemanticEmbeddingError,
    normalize_embedding_vector,
)

_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$")
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}$")
_LATIN_TERM = re.compile(r"[a-z0-9_]{2,}")
_CJK_SPAN = re.compile(r"[\u3400-\u9fff]+")
_MAX_STORED_CHARS = 12_000
_MAX_QUERY_CHARS = 240
_MAX_TIMESTAMP_CHARS = 48
_MAX_RESULTS = 20
_MAX_CANDIDATES = 80
_MAX_FUZZY_CANDIDATES = 80
_MAX_FUZZY_COMPARISONS_PER_TERM = 64
_MAX_TERM_CHARS = 64
_FUZZY_TERM_THRESHOLD = 0.78
_MAX_SEMANTIC_TEXT_CHARS = 12_000


class KnowledgeIndexError(RuntimeError):
    """The search index or a scoped query is invalid."""


@dataclass(frozen=True, slots=True)
class SessionSearchResult:
    """One bounded match with enough provenance for a transport to label it."""

    session_id: str
    created_at: str
    model: str
    user_excerpt: str
    assistant_excerpt: str
    matched_terms: int = 0
    query_terms: int = 0
    approximate: bool = False
    semantic_score: float | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeIndexStats:
    """Bounded, content-free health data for the session knowledge index."""

    chat_id: int
    session_id: str | None
    turns: int
    sessions: int
    stored_chars: int
    index_present: bool
    max_turns_per_session: int
    max_turns_per_chat: int
    semantic_provider: str | None = None
    semantic_turns: int = 0

    @property
    def retrieval_mode(self) -> str:
        """Describe the local, dependency-free retrieval stages."""

        if self.semantic_provider is not None:
            return "fts5+bounded-fuzzy+semantic"
        return "fts5+bounded-fuzzy"

    def as_dict(self) -> dict[str, object]:
        return {
            "chat_id": self.chat_id,
            "session_id": self.session_id,
            "turns": self.turns,
            "sessions": self.sessions,
            "stored_chars": self.stored_chars,
            "index_present": self.index_present,
            "max_turns_per_session": self.max_turns_per_session,
            "max_turns_per_chat": self.max_turns_per_chat,
            "semantic_provider": self.semantic_provider,
            "semantic_turns": self.semantic_turns,
            "retrieval_mode": self.retrieval_mode,
        }


class SessionKnowledgeStore:
    """Own FTS persistence, redaction, retention, and authorization scoping."""

    def __init__(
        self,
        state_root: Path,
        *,
        max_turns_per_session: int = 1_000,
        max_turns_per_chat: int = 5_000,
        semantic_provider: EmbeddingProvider | None = None,
        semantic_candidate_limit: int = 256,
        semantic_score_threshold: float = 0.62,
    ) -> None:
        root = state_root.expanduser()
        if (
            root == Path(root.anchor)
            or root.is_symlink()
            or ".xclaude" in root.parts
            or ".dsh" in root.parts
        ):
            raise KnowledgeIndexError("知識索引根目錄不安全")
        if not 1 <= max_turns_per_session <= 10_000:
            raise ValueError("per-session turn limit must be between 1 and 10000")
        if not max_turns_per_session <= max_turns_per_chat <= 50_000:
            raise ValueError("per-chat turn limit must include at least one session")
        if not 20 <= semantic_candidate_limit <= 1_000:
            raise ValueError("semantic candidate limit must be between 20 and 1000")
        if (
            not isinstance(semantic_score_threshold, int | float)
            or isinstance(semantic_score_threshold, bool)
            or not math.isfinite(semantic_score_threshold)
            or not 0.2 <= semantic_score_threshold <= 0.95
        ):
            raise ValueError("semantic score threshold must be between 0.2 and 0.95")
        if semantic_provider is not None:
            if _MODEL_ID.fullmatch(semantic_provider.identity) is None:
                raise ValueError("semantic provider identity is invalid")
            if not 1 <= semantic_provider.dimensions <= 8_192:
                raise ValueError("semantic provider dimensions are invalid")
        self.root = root.resolve()
        self.path = self.root / "knowledge.sqlite3"
        self.max_turns_per_session = max_turns_per_session
        self.max_turns_per_chat = max_turns_per_chat
        self.semantic_provider = semantic_provider
        self.semantic_candidate_limit = semantic_candidate_limit
        self.semantic_score_threshold = semantic_score_threshold

    def record_turn(
        self,
        *,
        chat_id: int,
        session_id: str,
        created_at: str,
        user_text: str,
        assistant_text: str,
        model: str,
    ) -> None:
        """Index one successful exchange after redaction and bounded retention."""

        _validate_chat_id(chat_id)
        normalized_session = _validate_session_id(session_id)
        normalized_model = _validate_model(model)
        timestamp = created_at.strip()
        if not 1 <= len(timestamp) <= _MAX_TIMESTAMP_CHARS or any(
            character in timestamp for character in "\r\n\x00"
        ):
            raise KnowledgeIndexError("知識索引時間欄位無效")
        stored_user = _stored_text(user_text)
        stored_assistant = _stored_text(assistant_text)
        if not stored_assistant:
            raise KnowledgeIndexError("成功回合缺少可索引答案")
        search_terms = _search_terms(f"{stored_user}\n{stored_assistant}")
        if not search_terms:
            return
        semantic_vector = self._embed(_semantic_text(stored_user, stored_assistant))
        semantic_payload: bytes | None = None
        if semantic_vector is not None:
            try:
                semantic_payload = _pack_vector(semantic_vector)
            except SemanticEmbeddingError:
                semantic_payload = None
        with closing(self._connect(create=True)) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    INSERT INTO turns (
                        chat_id, session_id, created_at, model,
                        user_text, assistant_text, search_terms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chat_id,
                        normalized_session,
                        timestamp,
                        normalized_model,
                        stored_user,
                        stored_assistant,
                        search_terms,
                    ),
                )
                if semantic_payload is not None and self.semantic_provider is not None:
                    connection.execute(
                        """
                        INSERT INTO semantic_vectors (
                            turn_id, provider_id, dimensions, vector
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            cursor.lastrowid,
                            self.semantic_provider.identity,
                            self.semantic_provider.dimensions,
                            semantic_payload,
                        ),
                    )
                self._trim(connection, chat_id, normalized_session)
        os.chmod(self.path, 0o600)

    def search(
        self,
        *,
        chat_id: int,
        query: str,
        session_id: str | None,
        limit: int = 8,
    ) -> tuple[SessionSearchResult, ...]:
        """Search only one authorized chat and optionally one product session."""

        _validate_chat_id(chat_id)
        scoped_session = (
            _validate_session_id(session_id) if session_id is not None else None
        )
        if not 1 <= limit <= _MAX_RESULTS:
            raise ValueError(f"search limit must be between 1 and {_MAX_RESULTS}")
        normalized_query = " ".join(redact_sensitive_text(query).split())
        if not 2 <= len(normalized_query) <= _MAX_QUERY_CHARS:
            raise KnowledgeIndexError("搜尋詞必須是 2-240 個字元")
        terms = _query_terms(normalized_query)
        if not terms:
            raise KnowledgeIndexError("搜尋詞沒有可用的文字 token")
        if not self.path.is_file():
            return ()
        query_vector = self._embed(normalized_query)
        approximate_ids: set[int] = set()
        semantic_scores: dict[int, float] = {}
        with closing(self._connect(create=False)) as connection:
            strict_rows = _fetch_rows(
                connection,
                match=" AND ".join(f'"{term}"' for term in terms),
                chat_id=chat_id,
                session_id=scoped_session,
                limit=limit,
            )
            rows = list(strict_rows)
            # A strict AND query is precise but becomes brittle for natural
            # language, especially when a single term is paraphrased or a CJK
            # bigram is absent.  Only when precision did not fill the caller's
            # bound do we fetch a small OR candidate set and rank by term
            # coverage.  Scope, redaction, and a hard candidate cap remain the
            # same; no embedding service or unbounded prompt retrieval is used.
            if len(rows) < limit and len(terms) > 1:
                partial_rows = _fetch_rows(
                    connection,
                    match=" OR ".join(f'"{term}"' for term in terms),
                    chat_id=chat_id,
                    session_id=scoped_session,
                    limit=_MAX_CANDIDATES,
                )
                seen_ids = {int(str(row[0])) for row in rows}
                rows.extend(
                    row
                    for row in partial_rows
                    if int(str(row[0])) not in seen_ids
                    and _matched_term_count(str(row[6]), terms)
                    >= _minimum_partial_matches(terms)
                )
            if len(rows) < limit:
                fuzzy_rows = _fetch_recent_rows(
                    connection,
                    chat_id=chat_id,
                    session_id=scoped_session,
                    limit=_MAX_FUZZY_CANDIDATES,
                )
                seen_ids = {int(str(row[0])) for row in rows}
                for row in fuzzy_rows:
                    row_id = int(str(row[0]))
                    if row_id in seen_ids:
                        continue
                    matched, _score = _fuzzy_match_stats(str(row[6]), terms)
                    if matched < _minimum_fuzzy_matches(terms):
                        continue
                    rows.append(row)
                    approximate_ids.add(row_id)
                    seen_ids.add(row_id)
            lexical_ids = {int(str(row[0])) for row in rows}
            if query_vector is not None and self.semantic_provider is not None:
                semantic_rows = _fetch_semantic_rows(
                    connection,
                    chat_id=chat_id,
                    session_id=scoped_session,
                    provider_id=self.semantic_provider.identity,
                    dimensions=self.semantic_provider.dimensions,
                    limit=self.semantic_candidate_limit,
                )
                seen_ids = set(lexical_ids)
                for row in semantic_rows:
                    row_id = int(str(row[0]))
                    payload = row[8]
                    if not isinstance(payload, bytes):
                        continue
                    try:
                        candidate = _unpack_vector(
                            payload,
                            dimensions=self.semantic_provider.dimensions,
                        )
                    except KnowledgeIndexError:
                        continue
                    score = _cosine_similarity(query_vector, candidate)
                    if score < self.semantic_score_threshold:
                        continue
                    semantic_scores[row_id] = score
                    if row_id not in seen_ids:
                        rows.append(row[:8])
                        approximate_ids.add(row_id)
                        seen_ids.add(row_id)
        ranked = sorted(
            rows,
            key=lambda row: _relevance_key(
                row,
                query=normalized_query,
                terms=terms,
                approximate=int(str(row[0])) in approximate_ids,
                semantic_score=semantic_scores.get(int(str(row[0])), 0.0),
            ),
        )[:limit]
        return tuple(
            SessionSearchResult(
                session_id=str(row[1]),
                created_at=str(row[2]),
                model=str(row[3]),
                user_excerpt=_excerpt(str(row[4]), normalized_query, terms),
                assistant_excerpt=_excerpt(str(row[5]), normalized_query, terms),
                matched_terms=(
                    _fuzzy_match_stats(str(row[6]), terms)[0]
                    if int(str(row[0])) in approximate_ids
                    else _matched_term_count(str(row[6]), terms)
                ),
                query_terms=len(terms),
                approximate=int(str(row[0])) in approximate_ids,
                semantic_score=semantic_scores.get(int(str(row[0])), None),
            )
            for row in ranked
        )

    def count(self, *, chat_id: int, session_id: str | None = None) -> int:
        _validate_chat_id(chat_id)
        scoped_session = (
            _validate_session_id(session_id) if session_id is not None else None
        )
        if not self.path.is_file():
            return 0
        query = "SELECT COUNT(*) FROM turns WHERE chat_id = ?"
        parameters: tuple[object, ...] = (chat_id,)
        if scoped_session is not None:
            query += " AND session_id = ?"
            parameters = (chat_id, scoped_session)
        with closing(self._connect(create=False)) as connection:
            value = connection.execute(query, parameters).fetchone()
        return int(value[0]) if value is not None else 0

    def stats(
        self,
        *,
        chat_id: int,
        session_id: str | None = None,
    ) -> KnowledgeIndexStats:
        """Return bounded index metrics without loading stored turn content."""

        _validate_chat_id(chat_id)
        scoped_session = (
            _validate_session_id(session_id) if session_id is not None else None
        )
        empty = KnowledgeIndexStats(
            chat_id=chat_id,
            session_id=scoped_session,
            turns=0,
            sessions=0,
            stored_chars=0,
            index_present=False,
            max_turns_per_session=self.max_turns_per_session,
            max_turns_per_chat=self.max_turns_per_chat,
            semantic_provider=(
                self.semantic_provider.identity if self.semantic_provider is not None else None
            ),
        )
        if not self.path.is_file():
            return empty
        query = (
            "SELECT COUNT(*), COUNT(DISTINCT session_id), "
            "COALESCE(SUM(length(user_text) + length(assistant_text)), 0) "
            "FROM turns WHERE chat_id = ?"
        )
        parameters: tuple[object, ...] = (chat_id,)
        if scoped_session is not None:
            query += " AND session_id = ?"
            parameters = (chat_id, scoped_session)
        try:
            with closing(self._connect(create=False)) as connection:
                value = connection.execute(query, parameters).fetchone()
        except sqlite3.Error as error:
            raise KnowledgeIndexError("SQLite FTS5 知識索引不可用") from error
        if value is None:
            return empty
        semantic_turns = 0
        if self.semantic_provider is not None:
            semantic_query = (
                "SELECT COUNT(*) FROM semantic_vectors "
                "JOIN turns ON turns.id = semantic_vectors.turn_id "
                "WHERE turns.chat_id = ? AND semantic_vectors.provider_id = ? "
                "AND semantic_vectors.dimensions = ?"
            )
            semantic_parameters: list[object] = [
                chat_id,
                self.semantic_provider.identity,
                self.semantic_provider.dimensions,
            ]
            if scoped_session is not None:
                semantic_query += " AND turns.session_id = ?"
                semantic_parameters.append(scoped_session)
            with closing(self._connect(create=False)) as connection:
                semantic_value = connection.execute(
                    semantic_query,
                    semantic_parameters,
                ).fetchone()
            semantic_turns = int(semantic_value[0]) if semantic_value is not None else 0
        return KnowledgeIndexStats(
            chat_id=chat_id,
            session_id=scoped_session,
            turns=max(0, int(value[0])),
            sessions=max(0, int(value[1])),
            stored_chars=max(0, int(value[2])),
            index_present=True,
            max_turns_per_session=self.max_turns_per_session,
            max_turns_per_chat=self.max_turns_per_chat,
            semantic_provider=(
                self.semantic_provider.identity if self.semantic_provider is not None else None
            ),
            semantic_turns=max(0, semantic_turns),
        )

    def _embed(self, text: str) -> tuple[float, ...] | None:
        if self.semantic_provider is None:
            return None
        try:
            batch = self.semantic_provider.embed((text,))
            if len(batch) != 1:
                raise SemanticEmbeddingError("embedding provider returned the wrong batch size")
            return normalize_embedding_vector(
                batch[0],
                dimensions=self.semantic_provider.dimensions,
            )
        except SemanticEmbeddingError:
            # Semantic retrieval is explicitly additive.  A local embedding
            # outage must never make the durable lexical memory unavailable.
            return None

    def _trim(
        self,
        connection: sqlite3.Connection,
        chat_id: int,
        session_id: str,
    ) -> None:
        connection.execute(
            """
            DELETE FROM turns WHERE id IN (
                SELECT id FROM turns
                WHERE chat_id = ? AND session_id = ?
                ORDER BY id DESC LIMIT -1 OFFSET ?
            )
            """,
            (chat_id, session_id, self.max_turns_per_session),
        )
        connection.execute(
            """
            DELETE FROM turns WHERE id IN (
                SELECT id FROM turns
                WHERE chat_id = ?
                ORDER BY id DESC LIMIT -1 OFFSET ?
            )
            """,
            (chat_id, self.max_turns_per_chat),
        )

    def _connect(self, *, create: bool) -> sqlite3.Connection:
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise KnowledgeIndexError("知識索引路徑不安全")
        if create:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.root.is_symlink() or not self.root.is_dir():
                raise KnowledgeIndexError("知識索引根目錄不安全")
            self.root.chmod(0o700)
        try:
            connection = sqlite3.connect(self.path, timeout=5.0)
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    session_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    model TEXT NOT NULL,
                    user_text TEXT NOT NULL,
                    assistant_text TEXT NOT NULL,
                    search_terms TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS turns_chat_session_id
                    ON turns(chat_id, session_id, id);
                CREATE TABLE IF NOT EXISTS semantic_vectors (
                    turn_id INTEGER PRIMARY KEY,
                    provider_id TEXT NOT NULL,
                    dimensions INTEGER NOT NULL,
                    vector BLOB NOT NULL,
                    FOREIGN KEY(turn_id) REFERENCES turns(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS semantic_vectors_provider
                    ON semantic_vectors(provider_id, dimensions, turn_id);
                CREATE VIRTUAL TABLE IF NOT EXISTS turn_fts USING fts5(
                    search_terms,
                    content='turns',
                    content_rowid='id',
                    tokenize='unicode61 remove_diacritics 2'
                );
                CREATE TRIGGER IF NOT EXISTS turns_after_insert AFTER INSERT ON turns BEGIN
                    INSERT INTO turn_fts(rowid, search_terms)
                    VALUES (new.id, new.search_terms);
                END;
                CREATE TRIGGER IF NOT EXISTS turns_after_delete AFTER DELETE ON turns BEGIN
                    INSERT INTO turn_fts(turn_fts, rowid, search_terms)
                    VALUES ('delete', old.id, old.search_terms);
                END;
                CREATE TRIGGER IF NOT EXISTS turns_semantic_after_delete
                AFTER DELETE ON turns BEGIN
                    DELETE FROM semantic_vectors WHERE turn_id = old.id;
                END;
                """
            )
        except sqlite3.Error as error:
            raise KnowledgeIndexError("SQLite FTS5 知識索引不可用") from error
        if create:
            os.chmod(self.path, 0o600)
        return connection


def _validate_chat_id(chat_id: int) -> None:
    if chat_id == 0 or isinstance(chat_id, bool):
        raise KnowledgeIndexError("Telegram chat ID 無效")


def _validate_session_id(session_id: str) -> str:
    normalized = session_id.strip()
    if _SESSION_ID.fullmatch(normalized) is None:
        raise KnowledgeIndexError("搜尋會話 ID 無效")
    return normalized


def _validate_model(model: str) -> str:
    normalized = model.strip()
    if _MODEL_ID.fullmatch(normalized) is None:
        raise KnowledgeIndexError("搜尋模型 ID 無效")
    return normalized


def _stored_text(text: str) -> str:
    normalized = redact_sensitive_text(text).strip()
    if len(normalized) <= _MAX_STORED_CHARS:
        return normalized
    return normalized[: _MAX_STORED_CHARS - 1] + "…"


def _term_tuple(text: str) -> tuple[str, ...]:
    terms = {term[:_MAX_TERM_CHARS] for term in _LATIN_TERM.findall(text.casefold())}
    for span in _CJK_SPAN.findall(text):
        terms.update(span[index : index + 2] for index in range(len(span) - 1))
    return tuple(sorted(terms))


def _search_terms(text: str) -> str:
    return " ".join(_term_tuple(text))


def _query_terms(query: str) -> tuple[str, ...]:
    return _term_tuple(query)[:24]


def _fetch_rows(
    connection: sqlite3.Connection,
    *,
    match: str,
    chat_id: int,
    session_id: str | None,
    limit: int,
) -> list[tuple[object, ...]]:
    """Fetch a bounded FTS candidate set for one authorized scope."""

    parameters: list[object] = [match, chat_id]
    session_clause = ""
    if session_id is not None:
        session_clause = " AND turns.session_id = ?"
        parameters.append(session_id)
    parameters.append(limit)
    return connection.execute(
        f"""
        SELECT turns.id, turns.session_id, turns.created_at, turns.model,
               turns.user_text, turns.assistant_text, turns.search_terms,
               bm25(turn_fts)
        FROM turn_fts
        JOIN turns ON turns.id = turn_fts.rowid
        WHERE turn_fts MATCH ? AND turns.chat_id = ?{session_clause}
        ORDER BY bm25(turn_fts), turns.id DESC
        LIMIT ?
        """,
        parameters,
    ).fetchall()


def _fetch_recent_rows(
    connection: sqlite3.Connection,
    *,
    chat_id: int,
    session_id: str | None,
    limit: int,
) -> list[tuple[object, ...]]:
    """Read a small recent window for typo-tolerant local ranking."""

    parameters: list[object] = [chat_id]
    session_clause = ""
    if session_id is not None:
        session_clause = " AND session_id = ?"
        parameters.append(session_id)
    parameters.append(limit)
    return connection.execute(
        f"""
        SELECT id, session_id, created_at, model,
               user_text, assistant_text, search_terms,
               0.0
        FROM turns
        WHERE chat_id = ?{session_clause}
        ORDER BY id DESC
        LIMIT ?
        """,
        parameters,
    ).fetchall()


def _fetch_semantic_rows(
    connection: sqlite3.Connection,
    *,
    chat_id: int,
    session_id: str | None,
    provider_id: str,
    dimensions: int,
    limit: int,
) -> list[tuple[object, ...]]:
    """Read a bounded vector window from the already-authorized chat scope."""

    parameters: list[object] = [chat_id, provider_id, dimensions]
    session_clause = ""
    if session_id is not None:
        session_clause = " AND turns.session_id = ?"
        parameters.append(session_id)
    parameters.append(limit)
    return connection.execute(
        f"""
        SELECT turns.id, turns.session_id, turns.created_at, turns.model,
               turns.user_text, turns.assistant_text, turns.search_terms,
               0.0, semantic_vectors.vector
        FROM semantic_vectors
        JOIN turns ON turns.id = semantic_vectors.turn_id
        WHERE turns.chat_id = ?
          AND semantic_vectors.provider_id = ?
          AND semantic_vectors.dimensions = ?{session_clause}
        ORDER BY turns.id DESC
        LIMIT ?
        """,
        parameters,
    ).fetchall()


def _matched_term_count(indexed_terms: str, terms: tuple[str, ...]) -> int:
    available = set(indexed_terms.split())
    return sum(term in available for term in terms)


def _fuzzy_match_stats(indexed_terms: str, terms: tuple[str, ...]) -> tuple[int, float]:
    """Return approximate token coverage using a bounded stdlib matcher."""

    available = set(indexed_terms.split())
    if not available or not terms:
        return 0, 0.0
    matched = 0
    score = 0.0
    for term in terms:
        if term in available:
            matched += 1
            score += 1.0
            continue
        candidates = sorted(
            (
                candidate
                for candidate in available
                if len(candidate) >= 3 and candidate[0] == term[0]
            ),
            key=lambda candidate: (abs(len(term) - len(candidate)), candidate),
        )[:_MAX_FUZZY_COMPARISONS_PER_TERM]
        best = max(
            (_term_similarity(term, candidate) for candidate in candidates),
            default=0.0,
        )
        if best >= _FUZZY_TERM_THRESHOLD:
            matched += 1
            score += best
    return matched, score


def _term_similarity(left: str, right: str) -> float:
    if left == right:
        return 1.0
    # Short CJK bigrams and one-character fragments are intentionally not
    # guessed: exact FTS matching already handles them and fuzzy matching one
    # shared character would create noisy cross-turn results.
    if len(left) < 3 or len(right) < 3:
        return 0.0
    if left[0] != right[0] and not (left.startswith(right) or right.startswith(left)):
        return 0.0
    if abs(len(left) - len(right)) > max(3, min(len(left), len(right)) // 3):
        return 0.0
    return SequenceMatcher(None, left, right, autojunk=False).ratio()


def _minimum_partial_matches(terms: tuple[str, ...]) -> int:
    """Keep OR recall useful without turning a multi-word query into ``OR`` noise."""

    # A single-term query already uses the precise FTS path.  For a natural
    # multi-term query, require two independently indexed terms before adding
    # a partial candidate; this prevents a common word such as ``marker`` from
    # leaking unrelated turns into a scoped search while still recovering
    # paraphrased/partially indexed entries.
    return 2 if len(terms) > 1 else 1


def _minimum_fuzzy_matches(terms: tuple[str, ...]) -> int:
    """Require enough independent near-matches to keep fuzzy recall precise."""

    if len(terms) == 1:
        return 1
    return max(2, (len(terms) + 1) // 2)


def _relevance_key(
    row: tuple[object, ...],
    *,
    query: str,
    terms: tuple[str, ...],
    approximate: bool = False,
    semantic_score: float = 0.0,
) -> tuple[int, float, int, float, float, float, int]:
    """Prefer coverage and exact phrases, then FTS quality and recency."""

    row_id = int(str(row[0]))
    indexed_terms = str(row[6])
    matched = _matched_term_count(indexed_terms, terms)
    fuzzy_matched, fuzzy_score = _fuzzy_match_stats(indexed_terms, terms)
    haystack = f"{row[4]}\n{row[5]}".casefold()
    phrase = int(query.casefold() in haystack)
    try:
        score = float(str(row[7]))
    except (TypeError, ValueError):
        score = 0.0
    if not math.isfinite(score):
        score = 0.0
    coverage = fuzzy_matched if approximate else matched
    coverage_ratio = coverage / max(1, len(terms))
    bounded_semantic = semantic_score if math.isfinite(semantic_score) else 0.0
    hybrid = max(coverage_ratio, bounded_semantic)
    return (
        -phrase,
        -hybrid,
        -coverage,
        -bounded_semantic,
        -fuzzy_score,
        score,
        -row_id,
    )


def _semantic_text(user_text: str, assistant_text: str) -> str:
    combined = f"User: {user_text}\nAssistant: {assistant_text}".strip()
    return combined[:_MAX_SEMANTIC_TEXT_CHARS]


def _pack_vector(vector: tuple[float, ...]) -> bytes:
    try:
        return struct.pack(f"<{len(vector)}f", *vector)
    except (OverflowError, struct.error) as error:
        raise SemanticEmbeddingError("embedding vector cannot be stored safely") from error


def _unpack_vector(payload: bytes, *, dimensions: int) -> tuple[float, ...]:
    if not isinstance(payload, bytes) or len(payload) != dimensions * 4:
        raise KnowledgeIndexError("語義向量索引無效")
    try:
        values = struct.unpack(f"<{dimensions}f", payload)
    except struct.error as error:
        raise KnowledgeIndexError("語義向量索引無效") from error
    try:
        return normalize_embedding_vector(values, dimensions=dimensions)
    except SemanticEmbeddingError as error:
        raise KnowledgeIndexError("語義向量索引無效") from error


def _cosine_similarity(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    if len(left) != len(right):
        return 0.0
    score = sum(first * second for first, second in zip(left, right, strict=True))
    if not math.isfinite(score):
        return 0.0
    return max(-1.0, min(1.0, score))


def _excerpt(
    text: str,
    query: str,
    terms: tuple[str, ...],
    *,
    limit: int = 360,
) -> str:
    flattened = " ".join(text.split())
    if len(flattened) <= limit:
        return flattened
    folded = flattened.casefold()
    positions = [folded.find(query.casefold())]
    positions.extend(folded.find(term.casefold()) for term in terms)
    start_match = min((position for position in positions if position >= 0), default=0)
    start = max(0, start_match - limit // 3)
    end = min(len(flattened), start + limit)
    prefix = "…" if start else ""
    suffix = "…" if end < len(flattened) else ""
    return f"{prefix}{flattened[start:end]}{suffix}"
