from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentops_assessment.agent.fake_llm import FakeLLM


@dataclass(frozen=True)
class PlanStep:
    id: str
    tool_name: str
    description: str
    input_template: dict[str, Any] = field(default_factory=dict)


class Planner:
    def __init__(self, llm: FakeLLM | None = None) -> None:
        self.llm = llm or FakeLLM()

    def create_plan(self, prompt: str, context: dict[str, Any] | None = None) -> list[PlanStep]:
        """为业务请求创建多步骤工具计划。

        简单确定性实现：从提示中提取 `sku`、`warehouse` 与 `intent`（分析或创建审批），
        然后返回稳定的工具链：erp.get_inventory -> bi.get_sales -> knowledge.search -> supplier.get_risk -> (可选) oa.create_approval_draft。
        """
        text = (prompt or "").strip()
        lower = text.lower()

        import re

        sku_match = re.search(r"\bsku[-_]?([A-Za-z0-9]+)\b", lower, re.IGNORECASE)
        sku = sku_match.group(0).upper() if sku_match else None

        wh_match = re.search(r"\bwh[-_A-Za-z0-9]+\b", text)
        warehouse = wh_match.group(0) if wh_match else None

        intent = "analysis"
        approval_keywords = ["审批", "审批建议", "创建审批", "create approval", "approval", "补货审批", "补货"]
        if any(k in text for k in approval_keywords) or any(k in lower for k in approval_keywords):
            intent = "create_approval"

        plan: list[PlanStep] = []

        plan.append(
            PlanStep(
                id="erp.get_inventory",
                tool_name="erp.get_inventory",
                description="查询 ERP 中指定 SKU 的库存与仓库信息。",
                input_template={"sku": sku or "", "warehouse": warehouse or ""},
            )
        )

        plan.append(
            PlanStep(
                id="bi.get_sales",
                tool_name="bi.get_sales",
                description="查询 BI 中的历史销量与未来预测（14 天）。",
                input_template={"sku": sku or "", "days": 14},
            )
        )

        plan.append(
            PlanStep(
                id="knowledge.search",
                tool_name="knowledge.search",
                description="在知识库中检索与库存/补货相关的规则与引用。",
                input_template={"query": prompt, "top_k": 3},
            )
        )

        plan.append(
            PlanStep(
                id="supplier.get_risk",
                tool_name="supplier.get_risk",
                description="查询供应商风险摘要以辅助补货决策。",
                input_template={"sku": sku or ""},
            )
        )

        if intent == "create_approval":
            plan.append(
                PlanStep(
                    id="oa.create_approval_draft",
                    tool_name="oa.create_approval_draft",
                    description="在 OA 中创建补货审批草稿（受权限约束）。",
                    input_template={"sku": sku or "", "warehouse": warehouse or "", "reason": prompt},
                )
            )

        return plan
