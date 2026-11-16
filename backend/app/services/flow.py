"""查询编排业务规则：状态流转、字段校验、执行编排与失败回退都收在这里。

路由层不做业务判断：
- 流程状态机只能按 草稿->待校验->已启用->已停用 单向相邻流转，不能跳级；
- 执行引擎（app.services.orchestrator）负责依赖解析、调度、回退与合并；
- 执行成功后把执行顺序与字段清单写入编排步骤契约，供转换模块读取；
- 步骤顺序只允许归属团队调整，其他团队的修改请求直接拒绝。
"""
from __future__ import annotations

from typing import Any

from app.services.contract import OWNER_TEAM, contract_service
from app.services.orchestrator import (
    OrchestrationError,
    StateTransitionError,
    execute_flow,
    parse_plan,
)
from app.store import store

MODULE = "flow"
RUN_MODULE = "flow_run"
REQUIRED_FIELDS = ["流程编号", "流程名称", "步骤序列"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
# 只允许相邻状态单向流转；停用是终态，不再放开反向动作
STATUS_TRANSITIONS: dict[str, list[str]] = {
    "草稿": ["待校验"],
    "待校验": ["已启用", "草稿"],
    "已启用": ["已停用"],
    "已停用": [],
}
ACTION_RULES = {"提交校验": "待校验", "启用流程": "已启用", "停用流程": "已停用", "退回草稿": "草稿"}
NEGATIVE_ACTIONS = ["停用流程"]
# 只有已启用的编排才能执行，避免未校验的步骤序列被调度
EXECUTABLE_STATUS = "已启用"


class FlowService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("流程编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def find_by_flow_no(self, flow_no: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if row.get("流程编号") == flow_no:
                return row
        return None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        # 结构化配置与可选约束一并登记
        for optional in ("数据依赖", "失败回退", "超时秒数", "并行度", "结果合并", "归属团队"):
            if optional in values:
                entry[optional] = values.get(optional)
        entry.setdefault("归属团队", OWNER_TEAM)
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"编排流程 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于查询编排可执行范围"
        target = ACTION_RULES[action]
        current = str(entry.get("status"))
        # 状态只能按顺序单向流转，跳级直接拦下并说明允许的下一状态
        if target not in STATUS_TRANSITIONS.get(current, []):
            allowed = "、".join(STATUS_TRANSITIONS.get(current, [])) or "无（终态）"
            return None, f"流程状态不能从「{current}」跳到「{target}」，允许的下一状态：{allowed}"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"编排流程已{action}"

    # ---- 执行编排 ----------------------------------------------------------

    def execute(
        self,
        entry_id: int,
        *,
        team: str = OWNER_TEAM,
        fail_steps: list[str] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """按数据依赖执行编排：成功后落执行批次、更新合并结果、写编排步骤契约。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"编排流程 {entry_id} 不存在或已归档"
        if entry.get("status") != EXECUTABLE_STATUS:
            return None, f"仅「{EXECUTABLE_STATUS}」状态的编排可执行，当前为「{entry.get('status')}」"
        if team != OWNER_TEAM:
            return None, f"团队「{team}」无权执行编排，共享执行结果仅归属团队「{OWNER_TEAM}」可触发"

        try:
            # 先静态解析一次，配置不合法时在执行前给出可读错误，不产生半成品批次
            parse_plan(entry)
        except OrchestrationError as exc:
            return None, f"编排配置无法执行：{exc}"

        run_rows = store.rows(RUN_MODULE)
        run_no = max(
            (int(row.get("执行批次号", 0)) for row in run_rows if row.get("流程编号") == entry.get("流程编号")),
            default=0,
        ) + 1
        try:
            run_record = execute_flow(entry, fail_steps=set(fail_steps or []), run_no=run_no)
        except (OrchestrationError, StateTransitionError) as exc:
            return None, f"编排执行中断：{exc}"

        run_record["id"] = max((int(row.get("id", 0)) for row in run_rows), default=0) + 1
        run_record["归属团队"] = entry.get("归属团队", OWNER_TEAM)
        run_rows.append(run_record)

        if run_record["是否成功"]:
            # 合并结果回写编排记录，且与执行记录、契约保持同一套执行顺序
            entry["结果合并"] = {
                "执行批次号": run_no,
                "执行顺序": run_record["执行顺序"],
                "合并行数": run_record["合并结果"]["row_count"],
                "字段清单": run_record["合并结果"]["field_inventory"],
            }
            contract, message = contract_service.write_contract(run_record, team=team)
            run_record["契约写入"] = {"契约id": contract["id"] if contract else None, "说明": message}
            return run_record, f"编排执行成功，合并结果与编排步骤契约已按同一执行顺序写入：{message}"

        entry["abnormal"] = True
        return run_record, f"编排执行失败：{run_record.get('失败说明') or '存在未完成步骤'}（未写入契约）"

    def list_runs(self, entry_id: int) -> tuple[list[dict[str, Any]] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"编排流程 {entry_id} 不存在或已归档"
        rows = [
            row for row in store.rows(RUN_MODULE) if row.get("流程编号") == entry.get("流程编号")
        ]
        rows.sort(key=lambda row: int(row.get("执行批次号", 0)))
        return rows, ""

    def get_run(self, entry_id: int, run_no: int) -> dict[str, Any] | None:
        """按编排流程主键 + 执行批次号定位单条执行批次明细。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        for row in store.rows(RUN_MODULE):
            if row.get("流程编号") == entry.get("流程编号") and int(row.get("执行批次号", 0)) == run_no:
                return row
        return None

    # ---- 步骤顺序维护（仅归属团队） ----------------------------------------

    def reorder_steps(
        self, entry_id: int, ordered_step_ids: list[str], *, team: str
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"编排流程 {entry_id} 不存在或已归档"
        owner = str(entry.get("归属团队") or OWNER_TEAM)
        if team != owner:
            return None, f"团队「{team}」无权调整步骤顺序，仅归属团队「{owner}」可改动"
        if not isinstance(ordered_step_ids, list) or not ordered_step_ids:
            return None, "新步骤顺序必须是非空步骤编号数组"

        # 在原步骤序列对象上重排，保持步骤自身配置不变
        try:
            plan = parse_plan(entry)
        except OrchestrationError as exc:
            return None, f"当前编排配置无法解析，不能调整顺序：{exc}"
        current_ids = [step.step_id for step in plan.steps]
        if sorted(ordered_step_ids) != sorted(current_ids):
            return None, "步骤顺序调整不能新增、删除或遗漏步骤，只能重排现有步骤"

        raw_steps = entry["步骤序列"]
        if isinstance(raw_steps, str):
            import json

            raw_steps = json.loads(raw_steps)

        # 重排只能发生在同一拓扑波次内：不允许把下游步骤提到前置依赖之前，
        # 即便换序后仍是合法 DAG，也会破坏“按数据依赖先后读取数据产物”的口径。
        original_wave: dict[str, int] = {}
        for wave_index, wave in enumerate(plan.waves):
            for step_id in wave:
                original_wave[step_id] = wave_index
        new_position = {step_id: index for index, step_id in enumerate(ordered_step_ids)}
        for step_id in ordered_step_ids:
            for prereq in plan.dependencies[step_id]:
                if new_position[prereq] >= new_position[step_id]:
                    return None, (
                        f"步骤 {step_id} 不能重排到前置依赖 {prereq} 之前："
                        "跨拓扑波次调整会破坏数据依赖的读取顺序（仅同波次可互换）"
                    )

        step_index = {item["步骤编号"]: item for item in raw_steps}
        entry["步骤序列"] = [step_index[step_id] for step_id in ordered_step_ids]

        # 重排后重新校验依赖：出现循环或悬空依赖则拒绝，并恢复原顺序
        try:
            new_plan = parse_plan(entry)
        except OrchestrationError as exc:
            entry["步骤序列"] = [step_index[step_id] for step_id in current_ids]
            return None, f"新顺序破坏数据依赖（已回滚）：{exc}"
        # 波次归属必须保持不变，确保执行顺序的依赖分层没有被改写
        if [sorted(wave) for wave in new_plan.waves] != [
            sorted(wave) for wave in plan.waves
        ]:
            entry["步骤序列"] = [step_index[step_id] for step_id in current_ids]
            return None, "重排改变了拓扑波次归属（已回滚）：步骤不能跨数据依赖层级移动"
        return entry, "步骤顺序已更新，数据依赖与拓扑波次校验通过"


flow_service = FlowService()
