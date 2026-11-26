"""SQLite query layer for OpenCode's database."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ._opencode_cli import get_db_path


def _default_db_path() -> Path:
    """Resolve the OpenCode database path per platform."""
    return get_db_path()


@dataclass
class TokenStats:
    input: int = 0
    output: int = 0
    reasoning: int = 0
    cache_read: int = 0
    cache_write: int = 0
    total: int = 0


@dataclass
class UsageRow:
    """A single aggregated usage row."""

    label: str
    calls: int = 0
    tokens: TokenStats = field(default_factory=TokenStats)
    cost: float = 0.0
    detail: str | None = None


@dataclass
class SessionMeta:
    """Metadata about a user session."""

    session_id: str
    title: str
    parent_id: str | None
    start_time: datetime
    duration_minutes: float
    message_count: int
    user_message_count: int
    total_tokens: int
    total_cost: float
    agents: list[str]
    models: list[str]
    tool_counts: dict[str, int]
    tool_errors: int


class OpenCodeDB:
    """Read-only access to the OpenCode SQLite database."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.path = Path(db_path) if db_path else _default_db_path()
        if not self.path.exists():
            raise FileNotFoundError(
                f"OpenCode database not found at {self.path}\nSet OPENCODE_DB env var to override."
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def _detect_tables(self, conn) -> set[str]:
        """Return the set of user table names in the database."""
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return {r["name"] for r in rows}

    def _source_query(self, conn) -> str:
        """Return normalized assistant SQL SELECT across v1, v2, or mixed schemas."""
        tables = self._detect_tables(conn)
        has_v1 = "message" in tables
        has_v2 = "session_message" in tables

        parts: list[str] = []

        if has_v2:
            parts.append("""
                SELECT
                    id,
                    session_id,
                    CASE
                        WHEN time_created > 1000000000000 THEN time_created
                        ELSE time_created * 1000
                    END AS time_created_ms,
                    json_extract(data, '$.agent') AS agent,
                    json_extract(data, '$.model.id') AS model,
                    json_extract(data, '$.model.providerID') AS provider,
                    COALESCE(json_extract(data, '$.tokens.input'), 0) AS input_tokens,
                    COALESCE(json_extract(data, '$.tokens.output'), 0) AS output_tokens,
                    COALESCE(json_extract(data, '$.tokens.reasoning'), 0) AS reasoning_tokens,
                    COALESCE(json_extract(data, '$.tokens.cache.read'), 0) AS cache_read,
                    COALESCE(json_extract(data, '$.tokens.cache.write'), 0) AS cache_write,
                    (COALESCE(json_extract(data, '$.tokens.input'), 0) +
                     COALESCE(json_extract(data, '$.tokens.output'), 0) +
                     COALESCE(json_extract(data, '$.tokens.reasoning'), 0)) AS total_tokens,
                    COALESCE(json_extract(data, '$.cost'), 0) AS cost
                FROM session_message
                WHERE type = 'assistant'
                  AND (COALESCE(json_extract(data, '$.tokens.input'), 0) +
                       COALESCE(json_extract(data, '$.tokens.output'), 0) +
                       COALESCE(json_extract(data, '$.tokens.reasoning'), 0)) > 0
            """)

        if has_v1:
            dedup_clause = "AND id NOT IN (SELECT id FROM session_message)" if has_v2 else ""
            parts.append(f"""
                SELECT
                    id,
                    session_id,
                    json_extract(data, '$.time.created') AS time_created_ms,
                    json_extract(data, '$.agent') AS agent,
                    json_extract(data, '$.modelID') AS model,
                    json_extract(data, '$.providerID') AS provider,
                    COALESCE(json_extract(data, '$.tokens.input'), 0) AS input_tokens,
                    COALESCE(json_extract(data, '$.tokens.output'), 0) AS output_tokens,
                    COALESCE(json_extract(data, '$.tokens.reasoning'), 0) AS reasoning_tokens,
                    COALESCE(json_extract(data, '$.tokens.cache.read'), 0) AS cache_read,
                    COALESCE(json_extract(data, '$.tokens.cache.write'), 0) AS cache_write,
                    COALESCE(json_extract(data, '$.tokens.total'), 0) AS total_tokens,
                    COALESCE(json_extract(data, '$.cost'), 0) AS cost
                FROM message
                WHERE json_extract(data, '$.role') = 'assistant'
                  AND json_extract(data, '$.tokens.total') IS NOT NULL
                  {dedup_clause}
            """)

        if not parts:
            return """
                SELECT
                    NULL AS id,
                    NULL AS session_id,
                    0 AS time_created_ms,
                    NULL AS agent,
                    NULL AS model,
                    NULL AS provider,
                    0 AS input_tokens,
                    0 AS output_tokens,
                    0 AS reasoning_tokens,
                    0 AS cache_read,
                    0 AS cache_write,
                    0 AS total_tokens,
                    0.0 AS cost
                WHERE 1 = 0
            """

        return " UNION ALL ".join(parts)

    def _sessions_query(self, conn) -> str:
        """Return the SQL SELECT for normalized session (id, title)."""
        tables = self._detect_tables(conn)
        has_v1 = "session" in tables
        has_v2 = "session_v2" in tables

        if has_v2 and has_v1:
            return """
                SELECT id, title FROM session_v2
                UNION
                SELECT id, title FROM session WHERE id NOT IN (SELECT id FROM session_v2)
            """
        if has_v2:
            return "SELECT id, title FROM session_v2"
        if has_v1:
            return "SELECT id, title FROM session"
        return "SELECT NULL AS id, NULL AS title WHERE 1 = 0"

    # ── query helpers ─────────────────────────────────────────────

    def _time_filter(
        self,
        since: datetime | None,
        until: datetime | None = None,
        *,
        col: str = "src.time_created_ms",
    ) -> tuple[str, list[Any]]:
        """Return WHERE clause fragments and params for time filtering."""
        clauses: list[str] = []
        params: list[Any] = []
        if since is not None:
            ts_ms = int(since.timestamp() * 1000)
            clauses.append(f"AND {col} >= ?")
            params.append(ts_ms)
        if until is not None:
            ts_ms = int(until.timestamp() * 1000)
            clauses.append(f"AND {col} < ?")
            params.append(ts_ms)
        return " ".join(clauses), params

    def _base_query(
        self,
        group_expr: str,
        since: datetime | None = None,
        until: datetime | None = None,
        order: str = "total_tokens DESC",
        limit: int | None = None,
    ) -> list[UsageRow]:
        conn = self._connect()
        try:
            source_sql = self._source_query(conn)
            time_clause, params = self._time_filter(since, until, col="src.time_created_ms")

            sql = f"""
                SELECT
                    {group_expr} AS label,
                    COUNT(*)                                AS calls,
                    COALESCE(SUM(src.input_tokens),     0) AS input_tokens,
                    COALESCE(SUM(src.output_tokens),    0) AS output_tokens,
                    COALESCE(SUM(src.reasoning_tokens), 0) AS reasoning_tokens,
                    COALESCE(SUM(src.cache_read),       0) AS cache_read,
                    COALESCE(SUM(src.cache_write),      0) AS cache_write,
                    COALESCE(SUM(src.total_tokens),     0) AS total_tokens,
                    COALESCE(SUM(src.cost),             0) AS cost
                FROM ({source_sql}) src
                WHERE 1 = 1
                  {time_clause}
                GROUP BY label
                ORDER BY {order}
            """
            if limit:
                sql += " LIMIT ?"
                params.append(limit)

            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()

        return [
            UsageRow(
                label=r["label"] or "(unknown)",
                calls=r["calls"],
                tokens=TokenStats(
                    input=r["input_tokens"],
                    output=r["output_tokens"],
                    reasoning=r["reasoning_tokens"],
                    cache_read=r["cache_read"],
                    cache_write=r["cache_write"],
                    total=r["total_tokens"],
                ),
                cost=r["cost"],
            )
            for r in rows
        ]

    # ── public API ────────────────────────────────────────────────

    def daily(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> list[UsageRow]:
        return self._base_query(
            group_expr=("date(src.time_created_ms / 1000, 'unixepoch', 'localtime')"),
            since=since,
            until=until,
            order="label DESC",
            limit=limit,
        )

    def by_model(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> list[UsageRow]:
        return self._base_query(
            group_expr="src.model",
            since=since,
            until=until,
            limit=limit,
        )

    def by_agent(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> list[UsageRow]:
        """Group by agent x model, showing which model each agent uses."""
        conn = self._connect()
        try:
            source_sql = self._source_query(conn)
            time_clause, params = self._time_filter(since, until, col="src.time_created_ms")

            sql = f"""
                SELECT
                    src.agent                               AS agent,
                    src.model                               AS model,
                    COUNT(*)                                AS calls,
                    COALESCE(SUM(src.input_tokens),     0) AS input_tokens,
                    COALESCE(SUM(src.output_tokens),    0) AS output_tokens,
                    COALESCE(SUM(src.reasoning_tokens), 0) AS reasoning_tokens,
                    COALESCE(SUM(src.cache_read),       0) AS cache_read,
                    COALESCE(SUM(src.cache_write),      0) AS cache_write,
                    COALESCE(SUM(src.total_tokens),     0) AS total_tokens,
                    COALESCE(SUM(src.cost),             0) AS cost
                FROM ({source_sql}) src
                WHERE 1 = 1
                  {time_clause}
                GROUP BY src.agent, src.model
                ORDER BY agent, total_tokens DESC
            """
            if limit:
                sql += " LIMIT ?"
                params.append(limit)

            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()

        return [
            UsageRow(
                label=r["agent"] or "(unknown)",
                calls=r["calls"],
                tokens=TokenStats(
                    input=r["input_tokens"],
                    output=r["output_tokens"],
                    reasoning=r["reasoning_tokens"],
                    cache_read=r["cache_read"],
                    cache_write=r["cache_write"],
                    total=r["total_tokens"],
                ),
                cost=r["cost"],
                detail=r["model"],
            )
            for r in rows
        ]

    def by_provider(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> list[UsageRow]:
        return self._base_query(
            group_expr="src.provider",
            since=since,
            until=until,
            limit=limit,
        )

    def by_session(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> list[UsageRow]:
        """Group by session, using session title as label."""
        conn = self._connect()
        try:
            source_sql = self._source_query(conn)
            sessions_sql = self._sessions_query(conn)
            time_clause, params = self._time_filter(since, until, col="src.time_created_ms")

            sql = f"""
                SELECT
                    COALESCE(s.title, src.session_id)       AS label,
                    COUNT(*)                                AS calls,
                    COALESCE(SUM(src.input_tokens),     0) AS input_tokens,
                    COALESCE(SUM(src.output_tokens),    0) AS output_tokens,
                    COALESCE(SUM(src.reasoning_tokens), 0) AS reasoning_tokens,
                    COALESCE(SUM(src.cache_read),       0) AS cache_read,
                    COALESCE(SUM(src.cache_write),      0) AS cache_write,
                    COALESCE(SUM(src.total_tokens),     0) AS total_tokens,
                    COALESCE(SUM(src.cost),             0) AS cost
                FROM ({source_sql}) src
                LEFT JOIN ({sessions_sql}) s ON src.session_id = s.id
                WHERE 1 = 1
                  {time_clause}
                GROUP BY src.session_id
                ORDER BY total_tokens DESC
            """
            if limit:
                sql += " LIMIT ?"
                params.append(limit)

            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()

        return [
            UsageRow(
                label=r["label"] or "(untitled)",
                calls=r["calls"],
                tokens=TokenStats(
                    input=r["input_tokens"],
                    output=r["output_tokens"],
                    reasoning=r["reasoning_tokens"],
                    cache_read=r["cache_read"],
                    cache_write=r["cache_write"],
                    total=r["total_tokens"],
                ),
                cost=r["cost"],
            )
            for r in rows
        ]

    def totals(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> UsageRow:
        """Return a single aggregated row for the period."""
        rows = self._base_query(
            group_expr="'total'",
            since=since,
            until=until,
        )
        if rows:
            return rows[0]
        return UsageRow(label="total")

    def to_dicts(self, rows: list[UsageRow]) -> list[dict[str, Any]]:
        """Serialize rows for JSON output."""
        result = []
        for r in rows:
            d: dict[str, Any] = {
                "label": r.label,
                "calls": r.calls,
                "tokens": {
                    "input": r.tokens.input,
                    "output": r.tokens.output,
                    "reasoning": r.tokens.reasoning,
                    "cache_read": r.tokens.cache_read,
                    "cache_write": r.tokens.cache_write,
                    "total": r.tokens.total,
                },
                "cost": round(r.cost, 4),
            }
            if r.detail is not None:
                d["model"] = r.detail
            result.append(d)
        return result

    # ── insights queries ─────────────────────────────────────────

    def _table_cols(self, conn: sqlite3.Connection, table: str) -> set[str]:
        """Return column names for *table* (caller passes known table identifier)."""
        return {c[1] for c in conn.execute(f'PRAGMA table_info("{table}")')}

    def _sessions_meta_sql(self, conn: sqlite3.Connection) -> str:
        """Return unified session SELECT with (id, title, parent_id, time_updated)."""
        tables = self._detect_tables(conn)
        parts: list[str] = []
        has_v2 = "session_v2" in tables

        if has_v2:
            cols = self._table_cols(conn, "session_v2")
            title = "title" if "title" in cols else "NULL"
            parent = "parent_id" if "parent_id" in cols else "NULL"
            updated = "time_updated" if "time_updated" in cols else "0"
            parts.append(
                f"SELECT id, {title} AS title, {parent} AS parent_id, "
                f"{updated} AS time_updated FROM session_v2"
            )
        if "session" in tables:
            cols = self._table_cols(conn, "session")
            title = "title" if "title" in cols else "NULL"
            parent = "parent_id" if "parent_id" in cols else "NULL"
            updated = "time_updated" if "time_updated" in cols else "0"
            dedup = "WHERE id NOT IN (SELECT id FROM session_v2)" if has_v2 else ""
            parts.append(
                f"SELECT id, {title} AS title, {parent} AS parent_id, "
                f"{updated} AS time_updated FROM session {dedup}"
            )
        if not parts:
            return (
                "SELECT NULL AS id, NULL AS title, NULL AS parent_id, 0 AS time_updated WHERE 1 = 0"
            )
        return " UNION ALL ".join(parts)

    def session_meta(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> list[SessionMeta]:
        """Session metadata with aggregated token/cost stats across v1 and v2 schemas."""
        time_clause, params = self._time_filter(since, until, col="src.time_created_ms")

        conn = self._connect()
        try:
            sessions_sql = self._sessions_meta_sql(conn)
            source_sql = self._source_query(conn)
            sql = f"""
                SELECT
                    s.id AS session_id,
                    COALESCE(s.title, s.id) AS title,
                    s.parent_id,
                    MIN(src.time_created_ms) AS start_ms,
                    MAX(src.time_created_ms) AS end_ms,
                    COUNT(src.id) AS message_count,
                    COALESCE(SUM(src.total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(src.cost), 0) AS total_cost,
                    s.time_updated
                FROM ({sessions_sql}) s
                LEFT JOIN ({source_sql}) src ON src.session_id = s.id
                    {time_clause}
                GROUP BY s.id
                ORDER BY total_tokens DESC
            """
            if limit:
                sql += " LIMIT ?"
                params.append(limit)
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()

        result: list[SessionMeta] = []
        for r in rows:
            start_ms = r["start_ms"]
            end_ms = r["end_ms"]
            if start_ms is not None:
                start_time = datetime.fromtimestamp(start_ms / 1000).astimezone()
            else:
                start_time = datetime.min.replace(tzinfo=datetime.now().astimezone().tzinfo)
            if start_ms is not None and end_ms is not None:
                duration_minutes = (end_ms - start_ms) / 60000
            else:
                duration_minutes = 0.0
            result.append(
                SessionMeta(
                    session_id=r["session_id"],
                    title=r["title"] or r["session_id"],
                    parent_id=r["parent_id"],
                    start_time=start_time,
                    duration_minutes=duration_minutes,
                    message_count=r["message_count"],
                    user_message_count=0,
                    total_tokens=r["total_tokens"],
                    total_cost=r["total_cost"],
                    agents=[],
                    models=[],
                    tool_counts={},
                    tool_errors=0,
                )
            )
        return result

    def cache_efficiency(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> dict[str, float]:
        """Per-model cache read ratio: cache_read / (input + cache_read)."""
        time_clause, params = self._time_filter(since, until, col="src.time_created_ms")

        conn = self._connect()
        try:
            source_sql = self._source_query(conn)
            sql = f"""
                SELECT
                    src.model AS model,
                    COALESCE(SUM(src.cache_read), 0) AS cache_read,
                    COALESCE(SUM(src.input_tokens), 0) AS input_tokens
                FROM ({source_sql}) src
                WHERE 1 = 1
                  {time_clause}
                GROUP BY src.model
            """
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()

        result: dict[str, float] = {}
        for r in rows:
            model = r["model"]
            cache_read = r["cache_read"]
            input_tokens = r["input_tokens"]
            denominator = input_tokens + cache_read
            if model and denominator > 0:
                result[model] = cache_read / denominator
        return result

    def cost_per_1k_tokens(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> dict[str, float]:
        """Per-model cost per 1,000 tokens."""
        time_clause, params = self._time_filter(since, until, col="src.time_created_ms")

        conn = self._connect()
        try:
            source_sql = self._source_query(conn)
            sql = f"""
                SELECT
                    src.model AS model,
                    COALESCE(SUM(src.cost), 0) AS total_cost,
                    COALESCE(SUM(src.total_tokens), 0) AS total_tokens
                FROM ({source_sql}) src
                WHERE 1 = 1
                  {time_clause}
                GROUP BY src.model
            """
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()

        result: dict[str, float] = {}
        for r in rows:
            model = r["model"]
            total_tokens = r["total_tokens"]
            if model and total_tokens > 0:
                result[model] = r["total_cost"] / total_tokens * 1000
        return result

    def tool_error_rates(self) -> dict[str, float]:
        """Per-tool error rate across v2 session_message and v1 part tables."""
        totals: dict[str, int] = {}
        errors: dict[str, int] = {}

        def _record(name: str | None, status: str | None) -> None:
            if not name:
                return
            totals[name] = totals.get(name, 0) + 1
            if status == "error":
                errors[name] = errors.get(name, 0) + 1

        conn = self._connect()
        try:
            tables = self._detect_tables(conn)
            has_v2 = "session_message" in tables

            if has_v2:
                v2_rows = conn.execute(
                    "SELECT data FROM session_message WHERE type = 'assistant'"
                ).fetchall()
                for r in v2_rows:
                    try:
                        d = json.loads(r["data"])
                    except (json.JSONDecodeError, TypeError):
                        continue
                    for item in d.get("content") or []:
                        if isinstance(item, dict) and item.get("type") == "tool":
                            name = item.get("name")
                            if not name and isinstance(item.get("tool"), dict):
                                name = item["tool"].get("name")
                            state = item.get("state")
                            status = state.get("status") if isinstance(state, dict) else None
                            _record(name if isinstance(name, str) else None, status)

            if "part" in tables:
                dedup = "AND message_id NOT IN (SELECT id FROM session_message)" if has_v2 else ""
                part_rows = conn.execute(
                    f"""
                    SELECT data FROM part
                    WHERE json_extract(data, '$.type') = 'tool'
                    {dedup}
                    """
                ).fetchall()
                for r in part_rows:
                    try:
                        d = json.loads(r["data"])
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if not isinstance(d, dict):
                        continue
                    tool_val = d.get("tool")
                    name = tool_val.get("name") if isinstance(tool_val, dict) else tool_val
                    state = d.get("state")
                    status = state.get("status") if isinstance(state, dict) else None
                    _record(name if isinstance(name, str) else None, status)
        finally:
            conn.close()

        return {name: errors.get(name, 0) / cnt for name, cnt in totals.items() if cnt > 0}

    def agent_delegation(self) -> dict[str, list[str]]:
        """Map parent session agents to their sub-agents across v1 and v2."""
        conn = self._connect()
        try:
            sessions_sql = self._sessions_meta_sql(conn)
            source_sql = self._source_query(conn)
            sql = f"""
                SELECT
                    pm.agent AS parent_agent,
                    cm.agent AS child_agent
                FROM ({sessions_sql}) s
                JOIN ({source_sql}) cm ON cm.session_id = s.id
                    AND cm.agent IS NOT NULL
                JOIN ({source_sql}) pm ON pm.session_id = s.parent_id
                    AND pm.agent IS NOT NULL
                WHERE s.parent_id IS NOT NULL
                GROUP BY parent_agent, child_agent
            """
            rows = conn.execute(sql).fetchall()
        finally:
            conn.close()

        result: dict[str, list[str]] = {}
        for r in rows:
            parent = r["parent_agent"]
            child = r["child_agent"]
            if parent and child:
                result.setdefault(parent, []).append(child)
        return result

    def build_transcript(self, session_id: str, max_chars: int = 30000) -> str:
        """Flatten session parts and v2 messages into a text transcript for LLM analysis."""
        conn = self._connect()
        try:
            tables = self._detect_tables(conn)
            has_v2 = "session_message" in tables
            entries: list[tuple[int, int, str, int, str]] = []

            if has_v2:
                cols = self._table_cols(conn, "session_message")
                seq_expr = "seq" if "seq" in cols else "0"
                ts_expr = (
                    "CASE WHEN time_created > 1000000000000 THEN time_created "
                    "ELSE time_created * 1000 END"
                    if "time_created" in cols
                    else "0"
                )
                v2_rows = conn.execute(
                    f"""
                    SELECT id, type, {seq_expr} AS seq, {ts_expr} AS time_ms, data
                    FROM session_message
                    WHERE session_id = ?
                    """,
                    [session_id],
                ).fetchall()
                for r in v2_rows:
                    try:
                        d = json.loads(r["data"])
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if not isinstance(d, dict):
                        continue
                    t_ms = int(r["time_ms"] or 0)
                    seq = int(r["seq"] or 0)
                    mid = str(r["id"])
                    if r["type"] == "user":
                        text = str(d.get("text", ""))[:500]
                        if text:
                            entries.append((t_ms, seq, mid, 0, f"[text]: {text}"))
                    elif r["type"] == "assistant":
                        for idx, item in enumerate(d.get("content") or []):
                            if not isinstance(item, dict):
                                continue
                            ptype = item.get("type", "")
                            if ptype == "text":
                                text = str(item.get("text", ""))[:500]
                                if text:
                                    entries.append((t_ms, seq, mid, idx, f"[text]: {text}"))
                            elif ptype == "tool":
                                tool_name = item.get("name") or "unknown"
                                state_data = item.get("state", {})
                                if isinstance(state_data, dict):
                                    status = state_data.get("status", "unknown")
                                    tool_input = str(state_data.get("input", ""))[:200]
                                else:
                                    status = "unknown"
                                    tool_input = ""
                                entries.append(
                                    (
                                        t_ms,
                                        seq,
                                        mid,
                                        idx,
                                        f"[tool:{tool_name}] status:{status} input:{tool_input}",
                                    )
                                )
                            elif ptype == "reasoning":
                                text = str(item.get("text", ""))[:200]
                                if text:
                                    entries.append((t_ms, seq, mid, idx, f"[thinking]: {text}"))

            if "part" in tables:
                dedup = "AND message_id NOT IN (SELECT id FROM session_message)" if has_v2 else ""
                rows = conn.execute(
                    f"""
                    SELECT id, time_created, data FROM part
                    WHERE session_id = ?
                    {dedup}
                    ORDER BY time_created ASC
                    """,
                    [session_id],
                ).fetchall()
                for r in rows:
                    try:
                        d = json.loads(r["data"])
                    except (json.JSONDecodeError, TypeError):
                        continue
                    t_ms = int(r["time_created"] or 0)
                    if t_ms and t_ms <= 1_000_000_000_000:
                        t_ms *= 1000
                    pid = str(r["id"])
                    part_type = d.get("type", "")
                    if part_type == "text":
                        text = d.get("text", "")[:500]
                        entries.append((t_ms, 0, pid, 0, f"[text]: {text}"))
                    elif part_type == "tool":
                        tool_data = d.get("tool", {})
                        if isinstance(tool_data, dict):
                            tool_name = tool_data.get("name", "unknown")
                        else:
                            tool_name = str(tool_data)
                        state_data = d.get("state", {})
                        if isinstance(state_data, dict):
                            status = state_data.get("status", "unknown")
                            tool_input = str(state_data.get("input", ""))[:200]
                        else:
                            status = "unknown"
                            tool_input = ""
                        entries.append(
                            (
                                t_ms,
                                0,
                                pid,
                                0,
                                f"[tool:{tool_name}] status:{status} input:{tool_input}",
                            )
                        )
                    elif part_type == "reasoning":
                        text = d.get("text", "")[:200]
                        entries.append((t_ms, 0, pid, 0, f"[thinking]: {text}"))
        finally:
            conn.close()

        entries.sort(key=lambda x: (x[0], x[1], x[2], x[3]))
        transcript = "\n".join(x[4] for x in entries)

        if len(transcript) > max_chars:
            keep = int(max_chars * 0.4)
            transcript = transcript[:keep] + "\n[...truncated...]\n" + transcript[-keep:]

        return transcript

    def session_user_messages(self, session_id: str) -> list[str]:
        """Return user text messages for a session across v1 and v2 schemas."""
        conn = self._connect()
        try:
            tables = self._detect_tables(conn)
            has_v2 = "session_message" in tables
            entries: list[tuple[int, int, str, str]] = []

            if has_v2:
                cols = self._table_cols(conn, "session_message")
                seq_expr = "seq" if "seq" in cols else "0"
                ts_expr = (
                    "CASE WHEN time_created > 1000000000000 THEN time_created "
                    "ELSE time_created * 1000 END"
                    if "time_created" in cols
                    else "0"
                )
                v2_rows = conn.execute(
                    f"""
                    SELECT id, {seq_expr} AS seq, {ts_expr} AS time_ms,
                           json_extract(data, '$.text') AS text
                    FROM session_message
                    WHERE session_id = ? AND type = 'user'
                    """,
                    [session_id],
                ).fetchall()
                for r in v2_rows:
                    if r["text"]:
                        entries.append(
                            (
                                int(r["time_ms"] or 0),
                                int(r["seq"] or 0),
                                str(r["id"]),
                                r["text"],
                            )
                        )

            if "part" in tables:
                dedup = "AND message_id NOT IN (SELECT id FROM session_message)" if has_v2 else ""
                rows = conn.execute(
                    f"""
                    SELECT id, time_created, json_extract(data, '$.text') AS text FROM part
                    WHERE session_id = ? AND json_extract(data, '$.type') = 'text'
                    {dedup}
                    ORDER BY time_created ASC
                    """,
                    [session_id],
                ).fetchall()
                for r in rows:
                    if r["text"]:
                        t_ms = int(r["time_created"] or 0)
                        if t_ms and t_ms <= 1_000_000_000_000:
                            t_ms *= 1000
                        entries.append((t_ms, 0, str(r["id"]), r["text"]))
        finally:
            conn.close()

        entries.sort(key=lambda x: (x[0], x[1], x[2]))
        return [x[3] for x in entries]
