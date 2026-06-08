from __future__ import annotations

from typing import Any

from agentops_assessment.agent.planner import PlanStep
from agentops_assessment.agent.state import InMemoryRunStateStore, RunState, StepState
from agentops_assessment.agent.tools import ToolRegistry
from agentops_assessment.backend import database
from typing import TypedDict


BLACKLIST_KEYS = {"vendor_secret", "unit_cost_usd", "token", "credentials", "debug", "candidate_note"}


def _sanitize_dict(d: dict) -> dict:
    if not isinstance(d, dict):
        return d
    out: dict = {}
    for k, v in d.items():
        if k in BLACKLIST_KEYS:
            continue
        # avoid embedding large nested secrets — if nested dict, keep shallow sanitized
        if isinstance(v, dict):
            out[k] = _sanitize_dict(v)
        else:
            out[k] = v
    return out


class Executor:
    def __init__(
        self,
        registry: ToolRegistry,
        state_store: InMemoryRunStateStore | None = None,
    ) -> None:
        self.registry = registry
        self.state_store = state_store or InMemoryRunStateStore()

    def execute(
        self,
        run_id: str,
        plan: list[PlanStep],
        context: dict[str, Any],
    ) -> RunState:
        """执行计划并持久化步骤状态。

        TODO(candidate/P0): 实现可恢复的多步骤执行、工具入参渲染、
        步骤事件持久化、错误处理和最终业务结果汇总。
        """
        # initialize in-memory state
        state = RunState(
            run_id=run_id,
            status="running",
            steps=[StepState(step_id=step.id, tool_name=step.tool_name, status="pending") for step in plan],
        )
        self.state_store.save(state)

        # persist plan.created event
        with database.connect() as conn:
            database.init_db(conn)
            plan_payload = {
                "plan": [
                    {
                        "id": step.id,
                        "tool_name": step.tool_name,
                        "description": step.description,
                        "input_template": step.input_template,
                    }
                    for step in plan
                ]
            }
            database.insert_run_event(conn, run_id, "plan.created", plan_payload)

        # execute steps sequentially
        final_result: dict[str, Any] = {}
        total_token_cost = 0
        for idx, step in enumerate(plan):
            # render inputs: merge template with context (context keys override empties)
            inputs = dict(step.input_template or {})
            for k, v in context.items():
                if k not in inputs or inputs.get(k) in ("", None):
                    inputs[k] = v

            # ensure knowledge.search receives permissions
            if step.tool_name == "knowledge.search":
                inputs.setdefault("user_permissions", context.get("user_permissions", []))

            # call tool
            try:
                result = self.registry.call(step.tool_name, inputs)
                sanitized_input = _sanitize_dict(inputs)
                sanitized_output = _sanitize_dict(result if isinstance(result, dict) else {"result": result})

                # persist step state and run event
                with database.connect() as conn:
                    database.init_db(conn)
                    # insert step_states row
                    conn.execute(
                        """
                        INSERT INTO step_states (run_id, step_id, tool_name, status, attempt, input_json, output_json, started_at, finished_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            step.id,
                            step.tool_name,
                            "succeeded",
                            getattr(self.registry, "last_call_attempts", {}).get(step.tool_name, 1),
                            database.encode_json(sanitized_input),
                            database.encode_json(sanitized_output),
                            database.now_iso(),
                            database.now_iso(),
                        ),
                    )
                    # insert run event
                    database.insert_run_event(
                        conn,
                        run_id,
                        "tool.call",
                        {
                            "step_id": step.id,
                            "tool_name": step.tool_name,
                            "input_summary": sanitized_input,
                            "output_summary": sanitized_output,
                            "error": None,
                            "attempt": getattr(self.registry, "last_call_attempts", {}).get(step.tool_name, 1),
                            "retries": 0,
                            "token_cost": 0,
                        },
                        tool_name=step.tool_name,
                    )

                # update in-memory state
                state.steps[idx].status = "succeeded"
                state.steps[idx].output = sanitized_output

                # collect partial outputs for final result composition
                if step.tool_name == "bi.get_sales":
                    final_result["forecast_units_next_14d"] = result.get("forecast_units_next_14d") if isinstance(result, dict) else None
                if step.tool_name == "erp.get_inventory":
                    final_result["warehouse"] = result.get("warehouse") if isinstance(result, dict) else None
                    final_result["stock_gap"] = result.get("stock_gap") if isinstance(result, dict) else None
                if step.tool_name == "knowledge.search":
                    final_result.setdefault("citations", result.get("citations") if isinstance(result, dict) else [])
                if step.tool_name == "supplier.get_risk":
                    final_result.setdefault("supplier_risk", result)

            except Exception as exc:  # pragma: no cover - best-effort error capture
                err_msg = str(exc)
                with database.connect() as conn:
                    database.init_db(conn)
                    # record failed step
                    conn.execute(
                        """
                        INSERT INTO step_states (run_id, step_id, tool_name, status, attempt, input_json, output_json, error, started_at, finished_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            step.id,
                            step.tool_name,
                            "failed",
                            getattr(self.registry, "last_call_attempts", {}).get(step.tool_name, 1),
                            database.encode_json(_sanitize_dict(inputs)),
                            database.encode_json({}),
                            err_msg,
                            database.now_iso(),
                            database.now_iso(),
                        ),
                    )
                    database.insert_run_event(
                        conn,
                        run_id,
                        "tool.call",
                        {
                            "step_id": step.id,
                            "tool_name": step.tool_name,
                            "input_summary": _sanitize_dict(inputs),
                            "output_summary": {},
                            "error": {"message": err_msg},
                            "attempt": getattr(self.registry, "last_call_attempts", {}).get(step.tool_name, 1),
                            "retries": 0,
                            "token_cost": 0,
                        },
                        tool_name=step.tool_name,
                    )

                state.status = "failed"
                state.steps[idx].status = "failed"
                state.steps[idx].error = err_msg
                self.state_store.save(state)
                # update runs table
                with database.connect() as conn:
                    database.init_db(conn)
                    conn.execute(
                        "UPDATE runs SET status = ?, error = ?, finished_at = ? WHERE id = ?",
                        ("failed", err_msg, database.now_iso(), run_id),
                    )
                    conn.commit()
                return state

        # all steps done
        state.status = "completed"
        state.result = final_result
        self.state_store.save(state)

        # persist final run result
        with database.connect() as conn:
            database.init_db(conn)
            conn.execute(
                "UPDATE runs SET status = ?, result_json = ?, token_cost = ?, finished_at = ? WHERE id = ?",
                ("completed", database.encode_json(final_result), total_token_cost, database.now_iso(), run_id),
            )
            database.insert_run_event(conn, run_id, "run.finished", {"status": "completed", "token_cost": total_token_cost})
            conn.commit()

        return state
