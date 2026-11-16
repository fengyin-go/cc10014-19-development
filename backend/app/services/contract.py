"""编排步骤契约：查询编排执行完成后，把同一套执行顺序与字段清单写入契约，
响应转换模块从这里读取，保证两个业务链路看到的口径完全一致。

契约的写入方只有查询编排服务（归属团队），对外只暴露只读接口；
其他业务模块（含转换模块）不能改动步骤顺序与字段清单。
契约按版本追加，历史版本保留以便复核每次执行对应的口径快照。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

MODULE = "flow_contract"
# 唯一允许写入契约的归属团队；其他团队调用写入会被拒绝
OWNER_TEAM = "查询编排团队"


class ContractService:
    def list_contracts(self, flow_no: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(MODULE)
        if flow_no:
            rows = [row for row in rows if row.get("流程编号") == flow_no]
        return rows

    def latest_for_flow(self, flow_no: str) -> dict[str, Any] | None:
        rows = [row for row in self.list_contracts(flow_no)]
        if not rows:
            return None
        return max(rows, key=lambda row: int(row.get("契约版本", 0)))

    def get_contract(self, contract_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, contract_id)

    def write_contract(self, run_record: dict[str, Any], *, team: str) -> tuple[dict[str, Any] | None, str]:
        """只允许归属团队写入；契约内容直接取自执行批次记录，保证与执行顺序、
        字段清单、合并结果三者一致。"""
        if team != OWNER_TEAM:
            return None, f"团队「{team}」无权写入编排步骤契约，仅归属团队「{OWNER_TEAM}」可维护步骤顺序"
        if not run_record.get("是否成功"):
            return None, "执行批次未成功，禁止写入编排步骤契约，避免转换模块读到未完成的数据产物"

        merged = run_record.get("合并结果") or {}
        rows = store.rows(MODULE)
        flow_no = run_record["流程编号"]
        existing_versions = [
            int(row.get("契约版本", 0)) for row in rows if row.get("流程编号") == flow_no
        ]
        next_version = max(existing_versions, default=0) + 1
        contract = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "流程编号": flow_no,
            "契约版本": next_version,
            "来源执行批次": run_record.get("执行批次号"),
            "写入团队": OWNER_TEAM,
            "写入时间": datetime.now().isoformat(timespec="seconds"),
            "执行顺序": list(run_record.get("执行顺序", [])),
            "拓扑波次": [list(wave) for wave in run_record.get("拓扑波次", [])],
            "并行度": run_record.get("并行度"),
            "字段清单": [
                {
                    "字段": item["字段"],
                    "采纳来源步骤": item["采纳来源步骤"],
                    "全部来源步骤": item["全部来源步骤"],
                    "判定依据": item["判定依据"],
                }
                for item in merged.get("field_inventory", [])
            ],
            "口径优先级判定": list(merged.get("merge_decisions", [])),
            "合并行数": merged.get("row_count", 0),
            "只读": True,
            "status": "已生效",
            "pending": False,
            "abnormal": False,
        }
        rows.append(contract)
        return contract, "编排步骤契约已写入，转换模块可读取同一执行顺序与字段清单"

    def assert_read_only(self) -> str:
        """供路由层调用：任何对外的修改尝试都统一拒绝。"""
        return "编排步骤契约为共享执行结果，只读；步骤顺序仅归属团队可通过查询编排模块调整"


contract_service = ContractService()
