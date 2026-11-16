"""响应转换接口：维护转换模板，并只读消费查询编排写入的编排步骤契约。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.contract import contract_service
from app.services.transform import TransformService

router = APIRouter(prefix="/api/transform", tags=["响应转换"])

service = TransformService()

LIST_FIELDS = ["模板编号", "关联服务", "输入字段", "输出字段", "格式转换", "默认填充", "数组展开", "嵌套打平"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按模板编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按模板编号与状态过滤响应转换列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/contracts/summary", response_model=dict)
def read_flow_contracts(flow_no: str | None = Query(default=None, description="按流程编号过滤")) -> dict[str, Any]:
    """转换模块只读消费编排步骤契约：读取与查询编排完全相同的执行顺序与字段清单。

    转换模块不持有契约写权限，任何修改尝试都会得到只读提示。
    """
    items = contract_service.list_contracts(flow_no)
    return {
        "module": "flow_contract",
        "readonly": True,
        "total": len(items),
        "items": [
            {
                "流程编号": item.get("流程编号"),
                "契约版本": item.get("契约版本"),
                "来源执行批次": item.get("来源执行批次"),
                "执行顺序": item.get("执行顺序"),
                "拓扑波次": item.get("拓扑波次"),
                "并行度": item.get("并行度"),
                "字段清单": item.get("字段清单"),
                "口径优先级判定": item.get("口径优先级判定"),
                "合并行数": item.get("合并行数"),
                "写入团队": item.get("写入团队"),
                "写入时间": item.get("写入时间"),
            }
            for item in items
        ],
    }


@router.post("/contracts/reject-write", response_model=ActionResult)
def reject_contract_write() -> ActionResult:
    """显式声明：转换模块不能改编排步骤契约，共享执行结果只读。"""
    return ActionResult(ok=False, message=contract_service.assert_read_only())


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条转换模板明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"转换模板 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条转换模板，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="转换模板已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条转换模板执行提交校验、启用模板、停用模板；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出响应转换清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "transform", "total": total, "items": items}
