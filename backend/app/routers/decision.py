"""决策建议推送接口：维护决策建议，覆盖推送建议、标记采纳、驳回建议等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.decision import DecisionService
from app.services.decision_brief import decision_brief_service

router = APIRouter(prefix="/api/decision", tags=["决策建议推送"])

service = DecisionService()

LIST_FIELDS = ["建议编号", "分析主题", "决策选项", "收益评估", "风险评估", "推荐方案", "生成时间", "建议状态"]
STATUSES = ["待审核", "已推送", "已采纳", "已驳回"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按建议编号检索"),
    status: str | None = Query(default=None, description="待审核、已推送、已采纳、已驳回"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按建议编号与状态过滤决策建议推送列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


# ---------------------------------------------------------------- 预警分级推送
@router.get("/briefs")
def list_briefs(
    keyword: str | None = Query(default=None, description="按建议编号或分析主题检索"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """已按窗口版本生成的决策建议；每条都带引用数据集版本与引用窗口清单。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = decision_brief_service.list_briefs(keyword=keyword, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/briefs", response_model=ActionResult)
def generate_brief(payload: EntryPayload) -> ActionResult:
    """从异常预警分级数据集生成建议；没有预警时不生成“零风险”结论。"""
    brief, message = decision_brief_service.generate(payload.values)
    if brief is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=brief)


@router.get("/briefs/check")
def check_briefs(
    brief_id: str | None = Query(default=None, description="只核对指定建议编号"),
) -> dict[str, Any]:
    """核对建议引用是否仍有效：版本变化、级别变化、预警已更正都会列出。"""
    return decision_brief_service.check(brief_id)


@router.post("/briefs/{brief_id}/actions", response_model=ActionResult)
def run_brief_action(brief_id: str, payload: EntryPayload) -> ActionResult:
    """推送前核对、标记已更正引用；推送被拦下时说明原因而不是静默沿用旧结论。"""
    action = str(payload.values.get("action") or "").strip()
    if action == "核对推送":
        entry, message = decision_brief_service.push(brief_id)
    elif action == "标记已更正引用":
        entry, message = decision_brief_service.mark_corrected_usage(brief_id, payload.values)
    else:
        return ActionResult(
            ok=False,
            message=f"动作「{action}」不属于决策建议推送可执行范围",
        )
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/briefs/{brief_id}")
def get_brief(brief_id: str) -> dict[str, Any]:
    """读取建议明细：收益/风险输入逐项列出关联预警与来源观测编号。"""
    brief = decision_brief_service.get_brief(brief_id)
    if brief is None:
        raise HTTPException(status_code=404, detail=f"决策建议 {brief_id} 不存在或已归档")
    return brief


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条决策建议明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"决策建议 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条决策建议，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="决策建议已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条决策建议执行推送建议、标记采纳、驳回建议；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出决策建议推送清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "decision", "total": total, "items": items}
