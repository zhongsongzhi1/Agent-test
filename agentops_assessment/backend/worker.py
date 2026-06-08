from __future__ import annotations

from agentops_assessment.backend import database
from agentops_assessment.agent.planner import Planner
from agentops_assessment.agent.tools import ToolRegistry
from agentops_assessment.agent.executor import Executor


def execute_run(run_id: str) -> None:
    """后台执行入口：Planner -> Executor 流程骨架。

    该实现读取任务、将 run 标记为 running，创建 plan，持久化 plan.created 事件，
    并调用 Executor 执行计划。最终更新 run 状态与审计。
    """
    with database.connect() as conn:
        database.init_db(conn)
        now = database.now_iso()
        # mark running
        conn.execute("UPDATE runs SET status = ?, started_at = ? WHERE id = ?", ("running", now, run_id))
        database.insert_run_event(conn, run_id, "run.started", {"message": "worker started"})

        # load task
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            database.insert_run_event(conn, run_id, "run.finished", {"status": "failed", "message": "run not found"})
            return
        task_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (row["task_id"],)).fetchone()
        if not task_row:
            conn.execute("UPDATE runs SET status = ?, error = ?, finished_at = ? WHERE id = ?", ("failed", "task not found", database.now_iso(), run_id))
            conn.commit()
            return

        prompt = task_row["prompt"]
        # build context for planner/executor
        user_row = conn.execute("SELECT * FROM users WHERE id = ?", (row["requested_by"],)).fetchone()
        user = None
        if user_row:
            user = {
                "id": user_row["id"],
                "name": user_row["name"],
                "permissions": database.decode_json(user_row["permissions_json"], []),
            }

    # create planner, registry, executor
    planner = Planner()
    registry = ToolRegistry.with_default_clients()
    executor = Executor(registry)

    # create plan
    plan = planner.create_plan(prompt, context={"user": user})

    # execute plan
    # prepare executor context: include sku, prompt, user_permissions
    context = {"prompt": prompt, "user_permissions": user.get("permissions", []) if user else []}
    state = executor.execute(run_id, plan, context)

    # executor already updates runs; ensure final audit
    with database.connect() as conn:
        database.init_db(conn)
        database.insert_audit_log(conn, actor_id=user["id"] if user else "system", action="run.finished", resource=run_id, payload={"status": state.status})
