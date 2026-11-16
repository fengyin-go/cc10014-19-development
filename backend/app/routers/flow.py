"""查询编排接口：维护编排流程，并提供按数据依赖执行、执行记录复核、
编排步骤契约读取与归属团队步骤顺序维护。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.contract import OWNER_TEAM, contract_service
from app.services.flow import flow_service

router = APIRouter(prefix="/api/flow", tags=["查询编排"])

LIST_FIELDS = ["流程编号", "流程名称", "步骤序列", "数据依赖", "失败回退", "超时秒数", "并行度", "结果合并"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


@router.get("/contracts/list", response_model=dict)
def list_contracts(flow_no: str | None = Query(default=None, description="按流程编号过滤")) -> dict[str, Any]:
    """读取编排步骤契约（只读）：转换模块从这里拿同一套执行顺序与字段清单。

    注意：该静态路径必须声明在 /{entry_id} 之前，否则会被路径参数吞掉。
    """
    items = contract_service.list_contracts(flow_no)
    return {"module": "flow_contract", "total": len(items), "items": items, "readonly": True}


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按流程编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按流程编号与状态过滤查询编排列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = flow_service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条编排流程明细；不存在时给出可读的错误说明。"""
    entry = flow_service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"编排流程 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条编排流程，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = flow_service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="编排流程已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条编排流程执行提交校验、启用流程、停用流程；状态只能单向相邻流转。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = flow_service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/execute", response_model=ActionResult)
def execute_flow(entry_id: int, payload: EntryPayload | None = None) -> ActionResult:
    """按数据依赖执行编排：解析依赖 -> 拓扑调度（受并行度约束）-> 失败回退 ->
    合并结果 -> 一致性闸门 -> 写入编排步骤契约。

    可选入参：
    - team: 调用团队，默认归属团队；非归属团队会被拒绝；
    - fail_steps: 注入首次执行失败的步骤编号，用于失败回退复测。
    """
    values = payload.values if payload else {}
    team = str(values.get("team") or OWNER_TEAM).strip()
    fail_steps = values.get("fail_steps") or []
    if not isinstance(fail_steps, list):
        return ActionResult(ok=False, message="fail_steps 必须是步骤编号数组")
    run_record, message = flow_service.execute(entry_id, team=team, fail_steps=[str(x) for x in fail_steps])
    if run_record is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=bool(run_record.get("是否成功")), message=message, entry=run_record)


@router.get("/{entry_id}/runs", response_model=dict)
def list_runs(entry_id: int) -> dict[str, Any]:
    """列出一条编排的全部执行批次，执行顺序与步骤记录可逐项复核。"""
    rows, message = flow_service.list_runs(entry_id)
    if rows is None:
        raise HTTPException(status_code=404, detail=message)
    return {"流程主键": entry_id, "total": len(rows), "items": rows}


@router.get("/{entry_id}/runs/{run_no}", response_model=dict)
def get_run(entry_id: int, run_no: int) -> dict[str, Any]:
    """读取单个执行批次明细：事件时间线、失败回退记录、合并数据集与一致性闸门。"""
    run_record = flow_service.get_run(entry_id, run_no)
    if run_record is None:
        raise HTTPException(status_code=404, detail=f"编排 {entry_id} 的执行批次 {run_no} 不存在")
    return run_record


@router.post("/{entry_id}/steps/reorder", response_model=ActionResult)
def reorder_steps(entry_id: int, payload: EntryPayload) -> ActionResult:
    """调整步骤顺序：仅归属团队可操作；重排后重新校验数据依赖，破坏依赖会回滚。"""
    team = str(payload.values.get("team") or "").strip()
    ordered = payload.values.get("ordered_step_ids") or []
    entry, message = flow_service.reorder_steps(entry_id, ordered, team=team)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出查询编排清单：返回当前过滤条件下的全量数据。"""
    items, total = flow_service.list_entries(page=1, size=10000)
    return {"module": "flow", "total": total, "items": items}
