"""Session data extraction functions for OpenCode insights."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from .types import AggregatedStats, SessionMeta

_KNOWN_TABLES = (
    "session",
    "session_v2",
    "message",
    "session_message",
    "part",
    "todo",
    "project",
)

_FILE_TOOLS = frozenset({"read", "edit", "write", "patch", "apply_patch", "multiedit"})


def _connect(db_path: Path | str) -> sqlite3.Connection:
    """Open a read-only SQLite connection."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _inspect_schema(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """Return mapping of present table names to their column name sets."""
    present = {
        r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    schema: dict[str, set[str]] = {}
    for table in _KNOWN_TABLES:
        if table in present:
            cols = {c[1] for c in conn.execute(f'PRAGMA table_info("{table}")')}
            schema[table] = cols
    return schema


def _norm_ts_sql(expr: str) -> str:
    """Normalize epoch seconds or milliseconds SQL expression to milliseconds."""
    return (
        f"CASE WHEN {expr} IS NULL THEN NULL "
        f"WHEN {expr} > 1000000000000 THEN {expr} "
        f"ELSE {expr} * 1000 END"
    )


def _sessions_sql(schema: dict[str, set[str]]) -> str:
    """Build unified session SELECT across session_v2 and legacy session tables."""
    parts: list[str] = []
    has_v2 = "session_v2" in schema

    if has_v2:
        cols = schema["session_v2"]
        title = "title" if "title" in cols else "NULL"
        parent = "parent_id" if "parent_id" in cols else "NULL"
        project = "project_id" if "project_id" in cols else "NULL"
        t_ms = _norm_ts_sql("time_created") if "time_created" in cols else "NULL"
        parts.append(
            f"SELECT id, {title} AS title, {parent} AS parent_id, "
            f"{project} AS project_id, {t_ms} AS time_created_ms FROM session_v2"
        )

    if "session" in schema:
        cols = schema["session"]
        title = "title" if "title" in cols else "NULL"
        parent = "parent_id" if "parent_id" in cols else "NULL"
        project = "project_id" if "project_id" in cols else "NULL"
        t_ms = _norm_ts_sql("time_created") if "time_created" in cols else "NULL"
        dedup = "WHERE id NOT IN (SELECT id FROM session_v2)" if has_v2 else ""
        parts.append(
            f"SELECT id, {title} AS title, {parent} AS parent_id, "
            f"{project} AS project_id, {t_ms} AS time_created_ms FROM session {dedup}"
        )

    if not parts:
        return (
            "SELECT NULL AS id, NULL AS title, NULL AS parent_id, "
            "NULL AS project_id, NULL AS time_created_ms WHERE 1 = 0"
        )
    return " UNION ALL ".join(parts)


def _messages_sql(schema: dict[str, set[str]]) -> str:
    """Build normalized message SELECT across v2 session_message and v1 message."""
    parts: list[str] = []
    has_v2 = "session_message" in schema

    if has_v2:
        cols = schema["session_message"]
        ts_raw = (
            "COALESCE(time_created, json_extract(data, '$.time.created'))"
            if "time_created" in cols
            else "json_extract(data, '$.time.created')"
        )
        seq_expr = "seq" if "seq" in cols else "0"
        parts.append(f"""
            SELECT
                id,
                session_id,
                type AS role,
                {_norm_ts_sql(ts_raw)} AS time_created_ms,
                {seq_expr} AS seq,
                json_extract(data, '$.agent') AS agent,
                COALESCE(
                    json_extract(data, '$.model.id'),
                    json_extract(data, '$.modelID')
                ) AS model,
                COALESCE(
                    json_extract(data, '$.model.providerID'),
                    json_extract(data, '$.providerID')
                ) AS provider,
                COALESCE(json_extract(data, '$.tokens.input'), 0) AS input_tokens,
                COALESCE(json_extract(data, '$.tokens.output'), 0) AS output_tokens,
                COALESCE(json_extract(data, '$.tokens.reasoning'), 0) AS reasoning_tokens,
                (COALESCE(json_extract(data, '$.tokens.input'), 0) +
                 COALESCE(json_extract(data, '$.tokens.output'), 0) +
                 COALESCE(json_extract(data, '$.tokens.reasoning'), 0)) AS total_tokens,
                COALESCE(json_extract(data, '$.cost'), 0) AS cost,
                data,
                'v2' AS schema_ver
            FROM session_message
        """)

    if "message" in schema:
        cols = schema["message"]
        ts_raw = (
            "COALESCE(json_extract(data, '$.time.created'), time_created)"
            if "time_created" in cols
            else "json_extract(data, '$.time.created')"
        )
        dedup = "WHERE id NOT IN (SELECT id FROM session_message)" if has_v2 else ""
        parts.append(f"""
            SELECT
                id,
                session_id,
                json_extract(data, '$.role') AS role,
                {_norm_ts_sql(ts_raw)} AS time_created_ms,
                0 AS seq,
                json_extract(data, '$.agent') AS agent,
                json_extract(data, '$.modelID') AS model,
                json_extract(data, '$.providerID') AS provider,
                COALESCE(json_extract(data, '$.tokens.input'), 0) AS input_tokens,
                COALESCE(json_extract(data, '$.tokens.output'), 0) AS output_tokens,
                COALESCE(json_extract(data, '$.tokens.reasoning'), 0) AS reasoning_tokens,
                COALESCE(
                    json_extract(data, '$.tokens.total'),
                    COALESCE(json_extract(data, '$.tokens.input'), 0) +
                    COALESCE(json_extract(data, '$.tokens.output'), 0) +
                    COALESCE(json_extract(data, '$.tokens.reasoning'), 0)
                ) AS total_tokens,
                COALESCE(json_extract(data, '$.cost'), 0) AS cost,
                data,
                'v1' AS schema_ver
            FROM message
            {dedup}
        """)

    if not parts:
        return """
            SELECT
                NULL AS id, NULL AS session_id, NULL AS role,
                NULL AS time_created_ms, 0 AS seq, NULL AS agent,
                NULL AS model, NULL AS provider,
                0 AS input_tokens, 0 AS output_tokens, 0 AS reasoning_tokens,
                0 AS total_tokens, 0.0 AS cost, NULL AS data, 'none' AS schema_ver
            WHERE 1 = 0
        """
    return " UNION ALL ".join(parts)


def _extract_tool_name(item: dict[str, Any]) -> str | None:
    """Extract tool name from either v2 content item ('name') or v1 part ('tool')."""
    name = item.get("name")
    if isinstance(name, str) and name:
        return name
    tool = item.get("tool")
    if isinstance(tool, dict):
        tname = tool.get("name")
        return tname if isinstance(tname, str) and tname else None
    if isinstance(tool, str) and tool:
        return tool
    return None


def _extract_tool_filepath(item: dict[str, Any]) -> str | None:
    """Extract target file path from a tool call's state.input payload."""
    state = item.get("state")
    if not isinstance(state, dict):
        return None
    inp = state.get("input")
    if not isinstance(inp, dict):
        return None
    fp = inp.get("filePath") or inp.get("path")
    return fp if isinstance(fp, str) and fp else None


def _time_params(
    since: datetime | None,
    until: datetime | None = None,
    *,
    col: str = "m.time_created_ms",
) -> tuple[str, list[Any]]:
    """Return WHERE clause fragments for message time filtering."""
    params: list[Any] = []
    clauses: list[str] = []
    if since:
        clauses.append(f"{col} >= ?")
        params.append(int(since.timestamp() * 1000))
    if until:
        clauses.append(f"{col} < ?")
        params.append(int(until.timestamp() * 1000))
    where = ("AND " + " AND ".join(clauses)) if clauses else ""
    return where, params


def _session_time_params(
    since: datetime | None,
    until: datetime | None = None,
    *,
    col: str = "s.time_created_ms",
) -> tuple[str, list[Any]]:
    """Return WHERE clause fragments for session time filtering."""
    params: list[Any] = []
    clauses: list[str] = []
    if since:
        clauses.append(f"{col} >= ?")
        params.append(int(since.timestamp() * 1000))
    if until:
        clauses.append(f"{col} < ?")
        params.append(int(until.timestamp() * 1000))
    where = ("AND " + " AND ".join(clauses)) if clauses else ""
    return where, params


# ── T5: Session Filtering & Metadata ─────────────────────────


def filter_sessions(
    db_path: Path | str,
    since: datetime | None,
    until: datetime | None = None,
) -> list[str]:
    """Return root session IDs with sufficient activity in the time range."""
    time_clause, params = _time_params(since, until, col="m.time_created_ms")
    conn = _connect(db_path)
    try:
        schema = _inspect_schema(conn)
        sessions_sql = _sessions_sql(schema)
        messages_sql = _messages_sql(schema)
        sql = f"""
            SELECT s.id,
                COUNT(CASE WHEN m.role = 'user' THEN 1 END) AS user_count,
                MAX(m.time_created_ms) - MIN(m.time_created_ms) AS duration_ms
            FROM ({sessions_sql}) s
            LEFT JOIN ({messages_sql}) m ON m.session_id = s.id
            WHERE s.parent_id IS NULL
            {time_clause}
            GROUP BY s.id
            HAVING user_count >= 2 AND duration_ms >= 60000
        """
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [r["id"] for r in rows]


def extract_session_meta(
    db_path: Path | str,
    session_id: str,
) -> SessionMeta:
    """Populate SessionMeta from DB for a single session."""
    conn = _connect(db_path)
    try:
        schema = _inspect_schema(conn)
        sessions_sql = _sessions_sql(schema)
        messages_sql = _messages_sql(schema)

        # 1. Message-level stats
        row = conn.execute(
            f"""
            SELECT
                s.title, s.parent_id,
                MIN(m.time_created_ms) AS start_ms,
                MAX(m.time_created_ms) AS end_ms,
                COUNT(CASE WHEN m.role = 'user' THEN 1 END) AS user_count,
                COUNT(CASE WHEN m.role = 'assistant' THEN 1 END) AS asst_count,
                COALESCE(SUM(CASE WHEN m.role = 'assistant' THEN m.input_tokens END), 0)
                    AS input_tok,
                COALESCE(SUM(CASE WHEN m.role = 'assistant' THEN m.output_tokens END), 0)
                    AS output_tok,
                COALESCE(SUM(CASE WHEN m.role = 'assistant' THEN m.total_tokens END), 0)
                    AS total_tok,
                COALESCE(SUM(m.cost), 0) AS total_cost
            FROM ({sessions_sql}) s
            LEFT JOIN ({messages_sql}) m ON m.session_id = s.id
            WHERE s.id = ?
            GROUP BY s.id
            """,
            [session_id],
        ).fetchone()

        if row is None:
            return SessionMeta(id=session_id, title=session_id)

        # 2. Agent/model counts
        am_rows = conn.execute(
            f"""
            SELECT agent, model
            FROM ({messages_sql}) m
            WHERE session_id = ?
              AND role = 'assistant'
            """,
            [session_id],
        ).fetchall()

        agent_counts: dict[str, int] = {}
        model_counts: dict[str, int] = {}
        for amr in am_rows:
            ag = amr["agent"]
            md = amr["model"]
            if ag:
                agent_counts[ag] = agent_counts.get(ag, 0) + 1
            if md:
                model_counts[md] = model_counts.get(md, 0) + 1

        # 3 & 4. Tool counts + language detection (v2 embedded content + non-overlapping v1 parts)
        tool_counts: dict[str, int] = {}
        languages: dict[str, int] = {}

        def _record_tool(item: dict[str, Any]) -> None:
            tname = _extract_tool_name(item)
            if not tname:
                return
            tool_counts[tname] = tool_counts.get(tname, 0) + 1
            if tname in _FILE_TOOLS:
                fp = _extract_tool_filepath(item)
                if fp:
                    ext = Path(fp).suffix.lstrip(".")
                    if ext:
                        languages[ext] = languages.get(ext, 0) + 1

        if "session_message" in schema:
            v2_rows = conn.execute(
                "SELECT data FROM session_message WHERE session_id = ? AND type = 'assistant'",
                [session_id],
            ).fetchall()
            for r in v2_rows:
                try:
                    d = json.loads(r["data"])
                except (json.JSONDecodeError, TypeError):
                    continue
                for item in d.get("content") or []:
                    if isinstance(item, dict) and item.get("type") == "tool":
                        _record_tool(item)

        if "part" in schema and "message" in schema:
            dedup = (
                "AND p.message_id NOT IN (SELECT id FROM session_message)"
                if "session_message" in schema
                else ""
            )
            part_rows = conn.execute(
                f"""
                SELECT p.data
                FROM part p
                JOIN message m ON p.message_id = m.id
                WHERE m.session_id = ?
                {dedup}
                """,
                [session_id],
            ).fetchall()
            for r in part_rows:
                try:
                    d = json.loads(r["data"])
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(d, dict) and d.get("type") == "tool":
                    _record_tool(d)

        # 5. Project path (table may not exist in all DBs)
        project_path: str | None = None
        if "project" in schema:
            try:
                proj_row = conn.execute(
                    f"""
                    SELECT p.worktree
                    FROM project p
                    JOIN ({sessions_sql}) s ON s.project_id = p.id
                    WHERE s.id = ?
                    """,
                    [session_id],
                ).fetchone()
                if proj_row:
                    project_path = proj_row["worktree"]
            except sqlite3.OperationalError:
                pass

    finally:
        conn.close()

    start_ms = row["start_ms"] or 0
    end_ms = row["end_ms"] or 0
    dur = end_ms - start_ms if start_ms and end_ms else 0

    return SessionMeta(
        id=session_id,
        title=row["title"] or session_id,
        project_path=project_path,
        parent_id=row["parent_id"],
        duration_minutes=dur / 60_000,
        user_msg_count=int(row["user_count"]),
        assistant_msg_count=int(row["asst_count"]),
        input_tokens=int(row["input_tok"]),
        output_tokens=int(row["output_tok"]),
        total_tokens=int(row["total_tok"]),
        cost=float(row["total_cost"]),
        tool_counts=tool_counts,
        languages=languages,
        agent_counts=agent_counts,
        model_counts=model_counts,
        start_time=int(start_ms),
        end_time=int(end_ms),
    )


# ── T6: Transcript Reconstruction ────────────────────────────


def reconstruct_transcript(
    db_path: Path | str,
    session_id: str,
    max_chars: int = 30000,
) -> str:
    """Flatten session parts and v2 messages into a chronological transcript."""
    conn = _connect(db_path)
    try:
        schema = _inspect_schema(conn)
        entries: list[tuple[int, int, str, int, str]] = []

        if "session_message" in schema:
            cols = schema["session_message"]
            seq_expr = "seq" if "seq" in cols else "0"
            ts_raw = (
                "COALESCE(time_created, json_extract(data, '$.time.created'))"
                if "time_created" in cols
                else "json_extract(data, '$.time.created')"
            )
            v2_rows = conn.execute(
                f"""
                SELECT id, type, {seq_expr} AS seq, {_norm_ts_sql(ts_raw)} AS time_ms, data
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
                mtype = r["type"]

                if mtype == "user":
                    text = d.get("text", "")
                    if text:
                        entries.append((t_ms, seq, mid, 0, f"[user]: {text}"))
                    for idx, item in enumerate(d.get("content") or [], start=1):
                        if (
                            isinstance(item, dict)
                            and item.get("type") == "text"
                            and item.get("text")
                        ):
                            entries.append((t_ms, seq, mid, idx, f"[user]: {item['text']}"))
                elif mtype == "assistant":
                    for idx, item in enumerate(d.get("content") or []):
                        if not isinstance(item, dict):
                            continue
                        ptype = item.get("type", "")
                        if ptype == "text" and item.get("text"):
                            entries.append((t_ms, seq, mid, idx, f"[assistant]: {item['text']}"))
                        elif ptype == "tool":
                            tool = _extract_tool_name(item) or "unknown"
                            state = item.get("state") or {}
                            status = (
                                state.get("status", "unknown")
                                if isinstance(state, dict)
                                else "unknown"
                            )
                            entries.append(
                                (t_ms, seq, mid, idx, f"[assistant]: Used {tool} ({status})")
                            )
                        elif ptype == "reasoning" and item.get("text"):
                            text = item["text"][:200]
                            entries.append(
                                (t_ms, seq, mid, idx, f"[assistant]: (reasoning) {text}")
                            )

        if "part" in schema and "message" in schema:
            p_cols = schema["part"]
            p_ts = _norm_ts_sql("p.time_created") if "time_created" in p_cols else "0"
            dedup = (
                "AND p.message_id NOT IN (SELECT id FROM session_message)"
                if "session_message" in schema
                else ""
            )
            rows = conn.execute(
                f"""
                SELECT p.id, {p_ts} AS time_ms, p.data, m.data AS msg_data
                FROM part p
                JOIN message m ON p.message_id = m.id
                WHERE m.session_id = ?
                {dedup}
                ORDER BY time_ms ASC, p.id ASC
                """,
                [session_id],
            ).fetchall()

            for r in rows:
                try:
                    part = json.loads(r["data"])
                    msg = json.loads(r["msg_data"])
                except (json.JSONDecodeError, TypeError):
                    continue
                t_ms = int(r["time_ms"] or 0)
                pid = str(r["id"])
                ptype = part.get("type", "")
                if ptype == "text" and part.get("text"):
                    role = msg.get("role", "unknown")
                    entries.append((t_ms, 0, pid, 0, f"[{role}]: {part['text']}"))
                elif ptype == "tool":
                    tool = _extract_tool_name(part) or "unknown"
                    state = part.get("state") or {}
                    status = (
                        state.get("status", "unknown") if isinstance(state, dict) else "unknown"
                    )
                    entries.append((t_ms, 0, pid, 0, f"[assistant]: Used {tool} ({status})"))
                elif ptype == "reasoning" and part.get("text"):
                    text = part["text"][:200]
                    entries.append((t_ms, 0, pid, 0, f"[assistant]: (reasoning) {text}"))
    finally:
        conn.close()

    entries.sort(key=lambda x: (x[0], x[1], x[2], x[3]))
    transcript = "\n".join(x[4] for x in entries)

    if len(transcript) > max_chars:
        header = f"[TRUNCATED — showing last {max_chars} chars]\n"
        transcript = header + transcript[-max_chars:]

    return transcript


# ── T7: Statistics Extraction ─────────────────────────────────


def extract_agent_stats(
    db_path: Path | str,
    since: datetime | None,
    until: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """Agent-level aggregated statistics."""
    time_clause, params = _time_params(since, until, col="m.time_created_ms")
    conn = _connect(db_path)
    try:
        schema = _inspect_schema(conn)
        messages_sql = _messages_sql(schema)
        sql = f"""
            SELECT
                m.agent,
                m.model,
                COUNT(*) AS calls,
                COALESCE(SUM(m.total_tokens), 0) AS tokens,
                COALESCE(SUM(m.cost), 0) AS cost
            FROM ({messages_sql}) m
            WHERE m.role = 'assistant'
              AND (m.schema_ver = 'v1' OR m.total_tokens > 0)
            {time_clause}
            GROUP BY m.agent, m.model
        """
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    result: dict[str, dict[str, Any]] = {}
    for r in rows:
        agent = r["agent"]
        if not agent:
            continue
        if agent not in result:
            result[agent] = {
                "calls": 0,
                "tokens": 0,
                "cost": 0.0,
                "models_used": [],
            }
        result[agent]["calls"] += r["calls"]
        result[agent]["tokens"] += r["tokens"]
        result[agent]["cost"] += r["cost"]
        model = r["model"]
        if model and model not in result[agent]["models_used"]:
            result[agent]["models_used"].append(model)
    return result


def extract_model_stats(
    db_path: Path | str,
    since: datetime | None,
    until: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """Model-level aggregated statistics."""
    time_clause, params = _time_params(since, until, col="m.time_created_ms")
    conn = _connect(db_path)
    try:
        schema = _inspect_schema(conn)
        messages_sql = _messages_sql(schema)
        sql = f"""
            SELECT
                m.model,
                m.agent,
                COUNT(*) AS calls,
                COALESCE(SUM(m.total_tokens), 0) AS tokens,
                COALESCE(SUM(m.cost), 0) AS cost
            FROM ({messages_sql}) m
            WHERE m.role = 'assistant'
              AND (m.schema_ver = 'v1' OR m.total_tokens > 0)
            {time_clause}
            GROUP BY m.model, m.agent
        """
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    result: dict[str, dict[str, Any]] = {}
    for r in rows:
        model = r["model"]
        if not model:
            continue
        if model not in result:
            result[model] = {
                "calls": 0,
                "tokens": 0,
                "cost": 0.0,
                "agents_using": [],
            }
        result[model]["calls"] += r["calls"]
        result[model]["tokens"] += r["tokens"]
        result[model]["cost"] += r["cost"]
        agent = r["agent"]
        if agent and agent not in result[model]["agents_using"]:
            result[model]["agents_using"].append(agent)
    return result


def extract_tool_stats(
    db_path: Path | str,
    since: datetime | None,
    until: datetime | None = None,
) -> dict[str, dict[str, int]]:
    """Tool completion/error counts across v2 messages and v1 parts."""
    result: dict[str, dict[str, int]] = {}

    def _record(tool: str | None, status: str | None, cnt: int = 1) -> None:
        if not tool:
            return
        if tool not in result:
            result[tool] = {"completed": 0, "errors": 0, "total": 0}
        result[tool]["total"] += cnt
        if status == "error":
            result[tool]["errors"] += cnt
        else:
            result[tool]["completed"] += cnt

    conn = _connect(db_path)
    try:
        schema = _inspect_schema(conn)

        if "session_message" in schema:
            cols = schema["session_message"]
            ts_raw = (
                "COALESCE(time_created, json_extract(data, '$.time.created'))"
                if "time_created" in cols
                else "json_extract(data, '$.time.created')"
            )
            t_col = _norm_ts_sql(ts_raw)
            time_clause, params = _time_params(since, until, col=t_col)
            v2_rows = conn.execute(
                f"""
                SELECT data
                FROM session_message
                WHERE type = 'assistant'
                {time_clause}
                """,
                params,
            ).fetchall()
            for r in v2_rows:
                try:
                    d = json.loads(r["data"])
                except (json.JSONDecodeError, TypeError):
                    continue
                for item in d.get("content") or []:
                    if isinstance(item, dict) and item.get("type") == "tool":
                        state = item.get("state")
                        status = state.get("status") if isinstance(state, dict) else None
                        _record(_extract_tool_name(item), status)

        if "part" in schema and "message" in schema:
            m_cols = schema["message"]
            m_ts_raw = (
                "COALESCE(json_extract(m.data, '$.time.created'), m.time_created)"
                if "time_created" in m_cols
                else "json_extract(m.data, '$.time.created')"
            )
            m_col = _norm_ts_sql(m_ts_raw)
            time_clause, params = _time_params(since, until, col=m_col)
            dedup = (
                "AND p.message_id NOT IN (SELECT id FROM session_message)"
                if "session_message" in schema
                else ""
            )
            part_rows = conn.execute(
                f"""
                SELECT p.data
                FROM part p
                JOIN message m ON p.message_id = m.id
                WHERE json_extract(p.data, '$.type') = 'tool'
                {dedup}
                {time_clause}
                """,
                params,
            ).fetchall()
            for r in part_rows:
                try:
                    d = json.loads(r["data"])
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(d, dict):
                    state = d.get("state")
                    status = state.get("status") if isinstance(state, dict) else None
                    _record(_extract_tool_name(d), status)
    finally:
        conn.close()

    return result


def extract_todo_stats(
    db_path: Path | str,
    since: datetime | None,
    until: datetime | None = None,
) -> dict[str, int]:
    """Todo status counts joined with unified sessions for time filtering."""
    conn = _connect(db_path)
    try:
        schema = _inspect_schema(conn)
        if "todo" not in schema or not ("session" in schema or "session_v2" in schema):
            return {}
        sessions_sql = _sessions_sql(schema)
        time_clause, params = _session_time_params(since, until, col="s.time_created_ms")
        sql = f"""
            SELECT t.status, COUNT(*) AS cnt
            FROM todo t
            JOIN ({sessions_sql}) s ON t.session_id = s.id
            WHERE 1=1
            {time_clause}
            GROUP BY t.status
        """
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    return {r["status"]: r["cnt"] for r in rows if r["status"]}


def extract_delegation_stats(
    db_path: Path | str,
    since: datetime | None,
    until: datetime | None = None,
) -> dict[str, Any]:
    """Session hierarchy statistics across unified v1/v2 sessions."""
    time_clause, params = _session_time_params(since, until, col="s.time_created_ms")
    conn = _connect(db_path)
    try:
        schema = _inspect_schema(conn)
        sessions_sql = _sessions_sql(schema)
        sql = f"""
            SELECT s.id, s.parent_id
            FROM ({sessions_sql}) s
            WHERE 1=1
            {time_clause}
        """
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    root_count = 0
    sub_count = 0
    children: dict[str, list[str]] = {}
    parent_map: dict[str, str | None] = {}

    for r in rows:
        sid = r["id"]
        pid = r["parent_id"]
        parent_map[sid] = pid
        if pid is None:
            root_count += 1
        else:
            sub_count += 1
            children.setdefault(pid, []).append(sid)

    # Compute depths via parent traversal with cycle guard
    depths: dict[str, int] = {}
    for sid in parent_map:
        depth = 0
        current: str | None = sid
        seen: set[str] = set()
        while (
            current is not None
            and current not in seen
            and parent_map.get(current) is not None
        ):
            seen.add(current)
            depth += 1
            current = parent_map[current]
        depths[sid] = depth

    max_depth = max(depths.values()) if depths else 0
    all_depths = list(depths.values())
    avg_depth = sum(all_depths) / len(all_depths) if all_depths else 0.0

    sub_types: dict[str, int] = {k: len(v) for k, v in children.items()}

    return {
        "root_sessions": root_count,
        "sub_sessions": sub_count,
        "sub_types": sub_types,
        "max_depth": max_depth,
        "avg_depth": avg_depth,
    }


def extract_project_stats(
    db_path: Path | str,
    since: datetime | None,
    until: datetime | None = None,
) -> dict[str, int]:
    """Project path to session count mapping."""
    conn = _connect(db_path)
    try:
        schema = _inspect_schema(conn)
        if "project" not in schema or not ("session" in schema or "session_v2" in schema):
            return {}
        sessions_sql = _sessions_sql(schema)
        time_clause, params = _session_time_params(since, until, col="s.time_created_ms")
        sql = f"""
            SELECT p.worktree AS project_path, COUNT(s.id) AS cnt
            FROM ({sessions_sql}) s
            JOIN project p ON s.project_id = p.id
            WHERE 1=1
            {time_clause}
            GROUP BY p.worktree
        """
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    return {r["project_path"]: r["cnt"] for r in rows if r["project_path"]}


def aggregate_all(
    db_path: Path | str,
    session_ids: list[str],
    since: datetime | None,
    until: datetime | None = None,
) -> AggregatedStats:
    """Build AggregatedStats from extraction functions."""
    agent_stats = extract_agent_stats(db_path, since, until)
    model_stats = extract_model_stats(db_path, since, until)
    tool_stats = extract_tool_stats(db_path, since, until)

    time_clause, params = _time_params(since, until, col="m.time_created_ms")
    conn = _connect(db_path)
    try:
        schema = _inspect_schema(conn)
        messages_sql = _messages_sql(schema)
        sql = f"""
            SELECT
                MIN(m.time_created_ms) AS min_time,
                MAX(m.time_created_ms) AS max_time,
                COUNT(*) AS total_messages,
                COALESCE(SUM(m.cost), 0) AS total_cost
            FROM ({messages_sql}) m
            WHERE m.role IN ('user', 'assistant')
            {time_clause}
        """
        row = conn.execute(sql, params).fetchone()
    finally:
        conn.close()

    min_time = int(row["min_time"] or 0)
    max_time = int(row["max_time"] or 0)
    total_messages = row["total_messages"] or 0
    total_cost = float(row["total_cost"] or 0.0)

    top_tools = sorted(
        [(name, d["total"]) for name, d in tool_stats.items()],
        key=lambda x: x[1],
        reverse=True,
    )[:10]
    top_agents = sorted(
        [(name, d["calls"]) for name, d in agent_stats.items()],
        key=lambda x: x[1],
        reverse=True,
    )[:10]
    top_models = sorted(
        [(name, d["calls"]) for name, d in model_stats.items()],
        key=lambda x: x[1],
        reverse=True,
    )[:10]

    return AggregatedStats(
        total_sessions=len(session_ids),
        analyzed_sessions=0,
        date_range=(min_time, max_time),
        total_messages=total_messages,
        total_cost=total_cost,
        top_tools=top_tools,
        top_agents=top_agents,
        top_models=top_models,
    )
