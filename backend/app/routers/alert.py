"""异常预警中心接口。

保留原有“预警记录”维护接口（列表/登记/动作/导出），同时新增窗口预警链路：
观测接入 -> 规则版本 -> 窗口聚合 -> 分级数据集 -> 逐项追溯 -> 更正审计。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.alert import AlertService, alert_service

router = APIRouter(prefix="/api/alert", tags=["异常预警中心"])

# 窗口计算历史与原来的“预警记录”表格是两套数据；这里保留原服务实例给记录接口。
service = AlertService()

LIST_FIELDS = ["记录编号", "预警类型", "预警级别", "触发条件", "影响范围", "建议措施", "发送渠道", "处理状态"]
STATUSES = ["未处理", "已确认", "已处理", "已关闭"]


# ---------------------------------------------------------------- 原有预警记录
@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按记录编号检索"),
    status: str | None = Query(default=None, description="未处理、已确认、已处理、已关闭"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按记录编号与状态过滤异常预警中心列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


# ---------------------------------------------------------------- 窗口分级数据集
@router.get("/windows")
def list_windows(
    keyword: str | None = Query(default=None, description="按指标/维度/窗口编号检索"),
    level: str | None = Query(default=None, description="按窗口最高预警级别过滤"),
) -> dict[str, Any]:
    """窗口分级结果：每条窗口保留成员、逐条实际阈值、连续段和待补齐缺口。"""
    return alert_service.list_windows(keyword=keyword, level=level)


@router.get("/windows/{window_id}")
def get_window(window_id: str) -> dict[str, Any]:
    """读取单个窗口的完整判定依据与关联预警；不存在时返回可读错误。"""
    window = alert_service.get_window(window_id)
    if window is None:
        raise HTTPException(status_code=404, detail=f"窗口 {window_id} 不存在或已归档")
    return window


@router.get("/dataset")
def get_dataset() -> dict[str, Any]:
    """异常预警分级数据集：供决策建议推送按版本引用。"""
    return alert_service.dataset()


@router.get("/alerts")
def list_window_alerts(
    status: str | None = Query(default=None, description="待处置、已确认、已处理、已关闭"),
    level: str | None = Query(default=None, description="一般、较重、严重等"),
) -> dict[str, Any]:
    """窗口预警明细：同一窗口命中多个条件时逐条返回，各自保留判定依据。"""
    rows = alert_service.alerts(status=status, level=level)
    return {
        "数据集版本": alert_service.dataset()["数据集版本"],
        "total": len(rows),
        "items": rows,
    }


@router.post("/alerts/{alert_id}/actions", response_model=ActionResult)
def run_window_alert_action(alert_id: str, payload: EntryPayload) -> ActionResult:
    """确认/处理/关闭窗口预警；已更正预警不能被处置动作复活。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = alert_service.update_alert_status(alert_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/recompute")
def recompute() -> dict[str, Any]:
    """手动重算；失败时旧数据集整体保留，不会出现新旧参半。"""
    try:
        return alert_service.recompute()
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error))


# ---------------------------------------------------------------- 观测
@router.get("/observations")
def list_observations(
    keyword: str | None = Query(default=None, description="按观测编号检索"),
) -> dict[str, Any]:
    """观测明细：包含已更正的历史版本，重复次数单独统计。"""
    rows = alert_service.list_observations(observation_no=keyword)
    dataset = alert_service.dataset()
    return {
        "数据集版本": dataset["数据集版本"],
        "total": len(rows),
        "items": rows,
        "重复观测忽略数": dataset["重复观测忽略数"],
    }


@router.post("/observations", response_model=ActionResult)
def ingest_observation(payload: EntryPayload) -> ActionResult:
    """登记观测：重复只生效一次；内容变化的按迟到更正留痕。"""
    try:
        result = alert_service.ingest(payload.values)
    except ValueError as error:
        return ActionResult(ok=False, message=str(error))
    entry = {
        key: value
        for key, value in result.items()
        if key not in {"ok", "说明"}
    }
    return ActionResult(ok=True, message=result["说明"], entry=entry)


# ---------------------------------------------------------------- 规则
@router.get("/rules")
def list_rules() -> dict[str, Any]:
    """阈值规则版本：展示生效区间、适用范围、连续次数与分级标准。"""
    rows = alert_service.list_rules()
    return {"total": len(rows), "items": rows}


@router.post("/rules", response_model=ActionResult)
def create_rule(payload: EntryPayload) -> ActionResult:
    """登记新阈值版本；版本号重复或规则不合法时说明原因。"""
    rule, message = alert_service.add_rule(payload.values)
    if rule is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=rule)


# ---------------------------------------------------------------- 更正审计
@router.get("/corrections")
def list_corrections() -> dict[str, Any]:
    """迟到更正审计：原预警是否被更正、依据是什么、影响了哪些窗口。"""
    rows = alert_service.corrections()
    return {"total": len(rows), "items": rows}


# ---------------------------------------------------------------- 原有单条记录
@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条预警记录明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"预警记录 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条预警记录，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="预警记录已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条预警记录执行确认预警、执行处理、关闭预警；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出异常预警中心清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "alert", "total": total, "items": items}
