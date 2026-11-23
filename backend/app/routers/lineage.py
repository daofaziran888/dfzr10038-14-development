"""整组启效预检与血缘批次提交接口。

独立于通用资源路由：批次预检、提交确认、下游重算领取和环路追溯。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.lineage_batch import lineage_batch_service

router = APIRouter(prefix="/api/lineage", tags=["血缘批次"])


class EdgeIn(BaseModel):
    rule_no: str = Field(description="原规则编号，供拆分后重试与追溯")
    upstream: str = Field(description="上游特征")
    downstream: str = Field(description="下游特征")


class PrecheckPayload(BaseModel):
    edges: list[EdgeIn]
    remark: str | None = None


class ClaimPayload(BaseModel):
    downstream: str
    operator: str = "调度员"


@router.post("/batches/precheck")
def precheck_batch(payload: PrecheckPayload) -> dict[str, Any]:
    """整组启效预检：把变动后的依赖关系形成候选血缘边并做全图环检测。"""
    result, errors = lineage_batch_service.precheck(
        [edge.model_dump() for edge in payload.edges], remark=payload.remark)
    if errors:
        return {"ok": False, "message": "；".join(errors)}
    batch, cands = result["batch"], result["candidates"]
    return {
        "ok": True,
        "message": (
            f"批次 {batch['批次号']} 预检完成：{batch['通过数']} 条通过、"
            f"{batch['环阻断数']} 条环阻断；通过边可提交确认，环边始终不能启效"
        ),
        **result,
    }


@router.post("/batches/{batch_no}/commit")
def commit_batch(batch_no: str) -> dict[str, Any]:
    """提交确认：只有通过全图检查的边进入确认结果，环边留环告档案。"""
    result, message = lineage_batch_service.commit(batch_no)
    if result is None:
        raise HTTPException(status_code=404, detail=message)
    return {"ok": True, "message": message, **result}


@router.get("/batches")
def list_batches() -> dict[str, Any]:
    return {"ok": True, "items": lineage_batch_service.list_batches()}


@router.get("/batches/{batch_no}")
def batch_detail(batch_no: str) -> dict[str, Any]:
    detail = lineage_batch_service.batch_detail(batch_no)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"批次 {batch_no} 不存在")
    return {"ok": True, **detail}


@router.post("/recompute/claim")
def claim_recompute(payload: ClaimPayload) -> dict[str, Any]:
    """调度员按已确认边领取下游重算；未发布时返回阻断原因。"""
    result, message = lineage_batch_service.claim_recompute(
        payload.downstream, operator=payload.operator)
    if result is None:
        return {"ok": False, "message": message}
    return {"ok": result["result"] != "blocked", "message": message, **result}


@router.get("/recompute/claims")
def list_claims() -> dict[str, Any]:
    return {"ok": True, "items": lineage_batch_service.list_claims()}


@router.get("/cycles")
def cycle_traces() -> dict[str, Any]:
    """环路追溯：判为环的边及其实际闭合路径、原规则编号。"""
    return {"ok": True, "items": lineage_batch_service.cycle_traces()}
