from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime, timezone

from agentops_assessment.backend import database


def build_dashboard(conn: sqlite3.Connection) -> dict:
    task_count = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
    run_count = conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
    failed_count = conn.execute(
        "SELECT COUNT(*) AS c FROM runs WHERE status = 'failed'"
    ).fetchone()["c"]
    completed_count = conn.execute(
        "SELECT COUNT(*) AS c FROM runs WHERE status = 'completed'"
    ).fetchone()["c"]
    token_cost = conn.execute("SELECT COALESCE(SUM(token_cost), 0) AS c FROM runs").fetchone()[
        "c"
    ]
    events = conn.execute("SELECT tool_name FROM run_events WHERE tool_name IS NOT NULL").fetchall()
    tool_counts = Counter(row["tool_name"] for row in events)

    # 计算平均耗时
    avg_run_seconds = 0.0
    completed_runs = conn.execute(
        """
        SELECT started_at, finished_at
        FROM runs
        WHERE status = 'completed' AND started_at IS NOT NULL AND finished_at IS NOT NULL
        """
    ).fetchall()
    if completed_runs:
        total_seconds = 0.0
        for row in completed_runs:
            try:
                started = datetime.fromisoformat(row["started_at"].replace("Z", "+00:00"))
                finished = datetime.fromisoformat(row["finished_at"].replace("Z", "+00:00"))
                total_seconds += (finished - started).total_seconds()
            except (ValueError, TypeError):
                continue
        avg_run_seconds = total_seconds / len(completed_runs) if completed_runs else 0.0

    # 最近失败（最近 10 次失败）
    recent_failures = []
    failure_rows = conn.execute(
        """
        SELECT id, task_id, error, finished_at
        FROM runs
        WHERE status = 'failed'
        ORDER BY finished_at DESC
        LIMIT 10
        """
    ).fetchall()
    for row in failure_rows:
        recent_failures.append({
            "run_id": row["id"],
            "task_id": row["task_id"],
            "error": row["error"],
            "finished_at": row["finished_at"],
        })

    # 按工具计算 token 成本
    per_tool_token_costs = {}
    tool_cost_rows = conn.execute(
        """
        SELECT tool_name, SUM(token_cost) AS total_cost
        FROM tool_call_costs
        GROUP BY tool_name
        """
    ).fetchall()
    for row in tool_cost_rows:
        per_tool_token_costs[row["tool_name"]] = row["total_cost"]

    # 队列健康度（当前 queued/running 状态）
    queued_count = conn.execute("SELECT COUNT(*) AS c FROM runs WHERE status = 'queued'").fetchone()["c"]
    running_count = conn.execute("SELECT COUNT(*) AS c FROM runs WHERE status = 'running'").fetchone()["c"]
    queue_health = {
        "queued": queued_count,
        "running": running_count,
        "health_score": max(0, 100 - (queued_count * 5)) if (queued_count + running_count) > 0 else 100,
    }

    return {
        "task_count": task_count,
        "run_count": run_count,
        "completed_count": completed_count,
        "failed_count": failed_count,
        "failure_rate": failed_count / run_count if run_count else 0,
        "token_cost": token_cost,
        "tool_call_counts": dict(tool_counts),
        "average_run_seconds": avg_run_seconds,
        "recent_failures": recent_failures,
        "per_tool_token_costs": per_tool_token_costs,
        "queue_health": queue_health,
        "generated_at": database.now_iso(),
    }