"""整组启效预检与血缘批次提交。

多条衍生规则一次启效前，先把变动后的依赖关系形成候选血缘边数据，
与已确认图合并做全图环检测，血缘维护员在"维持批次提交语义"和
"尽早向无关下游交付"之间取得平衡：

- 批次整体提交、按边准入：只有通过全图检查的边才进入确认结果，
  无关下游（独立边）不必陪环边一起等待；
- 环上的边始终不能启效，判为环的边记录实际闭合路径和原规则编号，
  供拆分后重试与事后追溯；
- 已确认边才算已生效关系，待确认（自动推导）/ 环形告警 / 已废弃都不算；
- 调度员只能按已确认边领取下游重算，不能用尚未启效的候选关系开工，
  同一下游特征的领取幂等，不重复登记。
"""
from __future__ import annotations

import datetime
from typing import Any

from app.store import store

BATCH_TABLE = "lineage_batch"        # 预检批次
CANDIDATE_TABLE = "lineage_candidate"  # 候选血缘边
CLAIM_TABLE = "recompute_claim"      # 下游重算领取登记

CONFIRMED = "已确认"    # feature_line 中唯一算"已生效关系"的状态
CYCLE = "环形告警"
AUTO = "自动推导"      # 待确认

VERDICT_PASS = "通过"
VERDICT_CYCLE = "环阻断"
VERDICT_EXISTS = "已存在"
VERDICT_DUP = "重复边"


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _next_no(rows: list[dict[str, Any]], field: str, prefix: str) -> str:
    best = 0
    for row in rows:
        text = str(row.get(field, ""))
        if text.startswith(prefix) and text[len(prefix):].isdigit():
            best = max(best, int(text[len(prefix):]))
    return f"{prefix}{best + 1:04d}"


def confirmed_edges() -> list[tuple[str, str]]:
    """已生效关系：仅已确认边；待确认 / 环形告警 / 已废弃一律不算。"""
    edges: list[tuple[str, str]] = []
    for row in store.rows("feature_line"):
        if row.get("status") != CONFIRMED:
            continue
        up = str(row.get("上游特征", "")).strip()
        down = str(row.get("下游特征", "")).strip()
        if up and down:
            edges.append((up, down))
    return edges


def _find_path(adj: dict[str, list[str]], start: str, goal: str) -> list[str] | None:
    """BFS 找 start ⇒ goal 的一条路径（含首尾）；不存在返回 None。"""
    if start == goal:
        return [start]
    queue: list[tuple[str, list[str]]] = [(start, [start])]
    seen = {start}
    while queue:
        node, path = queue.pop(0)
        for nxt in adj.get(node, []):
            if nxt in seen:
                continue
            if nxt == goal:
                return [*path, goal]
            seen.add(nxt)
            queue.append((nxt, [*path, nxt]))
    return None


class LineageBatchService:
    """整组启效预检、批次提交确认、下游重算领取的领域服务。"""

    # ---------------------------------------------------------- 整组预检
    def precheck(
        self, edges: list[dict[str, str]], remark: str | None = None
    ) -> tuple[dict[str, Any] | None, list[str]]:
        """把一批规则变动形成候选血缘边，叠加已确认图做全图环检测。"""
        cleaned: list[dict[str, str]] = []
        errors: list[str] = []
        for index, raw in enumerate(edges, 1):
            rule_no = str(raw.get("rule_no", "")).strip()
            up = str(raw.get("upstream", "")).strip()
            down = str(raw.get("downstream", "")).strip()
            if not (rule_no and up and down):
                errors.append(f"第{index}条缺少 规则编号/上游特征/下游特征")
                continue
            cleaned.append({"rule_no": rule_no, "upstream": up, "downstream": down})
        if errors:
            return None, errors
        if not cleaned:
            return None, ["批次至少包含一条候选边"]

        base = confirmed_edges()
        base_set = set(base)
        # 全图 = 已确认边 + 本批全部候选边：整组叠加后才做环检测，
        # 单条各自合法、合起来成环的情况正是在这里暴露。
        adj: dict[str, list[str]] = {}
        for u, v in base + [(c["upstream"], c["downstream"]) for c in cleaned]:
            adj.setdefault(u, []).append(v)

        verdicts: list[dict[str, str]] = []
        seen_in_batch: set[tuple[str, str]] = set()
        for cand in cleaned:
            u, v = cand["upstream"], cand["downstream"]
            if (u, v) in seen_in_batch:
                verdicts.append({**cand, "verdict": VERDICT_DUP, "cycle_path": ""})
                continue
            seen_in_batch.add((u, v))
            if (u, v) in base_set:
                verdicts.append({**cand, "verdict": VERDICT_EXISTS, "cycle_path": ""})
                continue
            back = _find_path(adj, v, u)
            if back is not None:
                # 闭合路径 = 本边 u→v 接上全图中 v⇝u 的实际路径
                cycle_path = "→".join([u, *back])
                verdicts.append({**cand, "verdict": VERDICT_CYCLE, "cycle_path": cycle_path})
            else:
                verdicts.append({**cand, "verdict": VERDICT_PASS, "cycle_path": ""})

        batch_no = _next_no(store.rows(BATCH_TABLE), "批次号", "BATCH-")
        batch = {
            "id": _next_id(store.rows(BATCH_TABLE)),
            "批次号": batch_no,
            "边数": len(verdicts),
            "通过数": sum(1 for v in verdicts if v["verdict"] == VERDICT_PASS),
            "环阻断数": sum(1 for v in verdicts if v["verdict"] == VERDICT_CYCLE),
            "status": "待提交",
            "备注": remark or "",
            "预检时间": _now(),
            "提交时间": "",
        }
        store.rows(BATCH_TABLE).append(batch)

        cand_rows = store.rows(CANDIDATE_TABLE)
        stored: list[dict[str, Any]] = []
        for verdict in verdicts:
            row = {
                "id": _next_id(cand_rows),
                "批次号": batch_no,
                "规则编号": verdict["rule_no"],
                "上游特征": verdict["upstream"],
                "下游特征": verdict["downstream"],
                "预检结论": verdict["verdict"],
                "闭合路径": verdict["cycle_path"],
                "确认状态": "待确认",
                "链路编号": "",
            }
            cand_rows.append(row)
            stored.append(row)
        return {"batch": batch, "candidates": stored}, []

    # ---------------------------------------------------------- 提交确认
    def commit(self, batch_no: str) -> tuple[dict[str, Any] | None, str]:
        """特征血缘读取候选血缘边数据并提交确认。

        只有通过全图检查的边进入确认结果；环边写入环形告警档案，
        保留闭合路径和原规则编号。重复提交幂等，直接返回原结果。
        """
        batch = self._find_batch(batch_no)
        if batch is None:
            return None, f"批次 {batch_no} 不存在"
        cands = [c for c in store.rows(CANDIDATE_TABLE) if c["批次号"] == batch_no]
        if batch["status"] == "已提交":
            return {"batch": batch, "candidates": cands, "lines": [], "idempotent": True}, \
                f"批次 {batch_no} 已提交过，返回原确认结果，不重复写入"

        # 提交前按最新已确认图复检"通过"边：预检之后图可能已被别的批次改变，
        # 只允许在提交点仍能通过全图检查的边进入确认结果。
        pass_cands = [c for c in cands if c["预检结论"] == VERDICT_PASS]
        adj: dict[str, list[str]] = {}
        for u, v in confirmed_edges() + [(c["上游特征"], c["下游特征"]) for c in pass_cands]:
            adj.setdefault(u, []).append(v)
        for cand in pass_cands:
            back = _find_path(adj, cand["下游特征"], cand["上游特征"])
            if back is not None:
                cand["预检结论"] = VERDICT_CYCLE
                cand["闭合路径"] = "→".join([cand["上游特征"], *back])
                cand["复检改判"] = True

        line_rows = store.rows("feature_line")
        written: list[dict[str, Any]] = []
        for cand in cands:
            verdict = cand["预检结论"]
            u, d = cand["上游特征"], cand["下游特征"]
            rule_no = cand["规则编号"]
            if verdict == VERDICT_PASS:
                line = self._reuse_auto_line(u, d)
                if line is None:
                    line = {
                        "id": _next_id(line_rows),
                        "链路编号": _next_no(line_rows, "链路编号", "FEAT-"),
                        "转换函数": "",
                    }
                    line_rows.append(line)
                line.update({
                    "上游特征": u, "下游特征": d,
                    "依赖深度": "1",
                    "影响评估": f"批次{batch_no}全图检查通过",
                    "链路状态": "生效",
                    "最后更新": _now(),
                    "status": CONFIRMED, "pending": False, "abnormal": False,
                    "来源批次": batch_no, "来源规则": rule_no,
                })
                cand["确认状态"] = CONFIRMED
                cand["链路编号"] = str(line["链路编号"])
                written.append(line)
                self._activate_rule(rule_no, batch_no)
            elif verdict == VERDICT_CYCLE:
                # 环上的边始终不能启效：只留环告档案，绝不进入确认结果
                line = {
                    "id": _next_id(line_rows),
                    "链路编号": _next_no(line_rows, "链路编号", "FEAT-"),
                    "上游特征": u, "下游特征": d,
                    "转换函数": "",
                    "依赖深度": "-",
                    "影响评估": f"闭合路径 {cand['闭合路径']}",
                    "链路状态": "环阻断",
                    "最后更新": _now(),
                    "status": CYCLE, "pending": False, "abnormal": True,
                    "来源批次": batch_no, "来源规则": rule_no,
                    "闭合路径": cand["闭合路径"],
                }
                line_rows.append(line)
                cand["确认状态"] = CYCLE
                cand["链路编号"] = str(line["链路编号"])
                written.append(line)
                self._mark_rule_blocked(rule_no, batch_no)
            else:
                cand["确认状态"] = "未确认"

        batch["status"] = "已提交"
        batch["提交时间"] = _now()
        batch["通过数"] = sum(1 for c in cands if c["预检结论"] == VERDICT_PASS)
        batch["环阻断数"] = sum(1 for c in cands if c["预检结论"] == VERDICT_CYCLE)
        return {"batch": batch, "candidates": cands, "lines": written,
                "idempotent": False}, f"批次 {batch_no} 提交完成"

    def _reuse_auto_line(self, up: str, down: str) -> dict[str, Any] | None:
        """同向待确认（自动推导）行允许转正复用；环告/已废弃档案不复用，
        修正后重试会产生新的已确认链路，原档案保留供追溯。"""
        for row in store.rows("feature_line"):
            if row.get("status") == AUTO and \
                    str(row.get("上游特征", "")).strip() == up and \
                    str(row.get("下游特征", "")).strip() == down:
                return row
        return None

    def _activate_rule(self, rule_no: str, batch_no: str) -> None:
        """通过边的原规则联动启效（仅草稿态规则，避免覆盖停用/异常）。"""
        for row in store.rows("derive_rule"):
            if str(row.get("规则编号", "")) == rule_no:
                row["最近预检"] = f"{batch_no}:通过"
                if row.get("status") == "草稿":
                    row["status"] = "生效中"
                    row["pending"] = True

    def _mark_rule_blocked(self, rule_no: str, batch_no: str) -> None:
        """环边的原规则只记录阻断原因，保持原状态，绝不在此启效。"""
        for row in store.rows("derive_rule"):
            if str(row.get("规则编号", "")) == rule_no:
                row["最近预检"] = f"{batch_no}:环阻断"

    # ---------------------------------------------------------- 领取重算
    def claim_recompute(
        self, downstream: str, operator: str = "调度员"
    ) -> tuple[dict[str, Any] | None, str]:
        """调度员按已确认边领取下游重算；候选关系不能开工，领取幂等。"""
        target = downstream.strip()
        if not target:
            return None, "下游特征不能为空"

        claims = store.rows(CLAIM_TABLE)
        for existing in claims:
            if existing["下游特征"] == target and existing["status"] == "已领取":
                return {"result": "already_claimed", "claim": existing,
                        "claims_for_target": 1}, \
                    f"{target} 的重算此前已领取（登记#{existing['id']}），不重复登记"

        confirmed = [
            row for row in store.rows("feature_line")
            if str(row.get("下游特征", "")).strip() == target
            and row.get("status") == CONFIRMED
        ]
        if confirmed:
            line = confirmed[0]
            claim = {
                "id": _next_id(claims),
                "下游特征": target,
                "来源链路": line["链路编号"],
                "来源批次": line.get("来源批次", ""),
                "领取人": operator,
                "status": "已领取",
                "领取时间": _now(),
            }
            claims.append(claim)
            return {"result": "claimed", "claim": claim, "claims_for_target": 1}, \
                f"{target} 重算领取成功，依据已确认链路 {line['链路编号']}"

        # 未发布：区分原因，环阻断要给出闭合路径和原规则编号
        cycles = [
            row for row in store.rows("feature_line")
            if str(row.get("下游特征", "")).strip() == target
            and row.get("status") == CYCLE
        ]
        if cycles:
            reason = {
                "类型": "环路阻断",
                "说明": "本批未发布：所在批次存在环路，环上的边始终不能启效",
                "闭合路径": [str(row.get("闭合路径", "")) for row in cycles],
                "规则编号": [str(row.get("来源规则", "")) for row in cycles],
                "批次号": [str(row.get("来源批次", "")) for row in cycles],
            }
            return {"result": "blocked", "reason": reason, "claims_for_target": 0}, \
                f"{target} 本批未发布（环路阻断），不能开工"

        pending = [
            row for row in store.rows(CANDIDATE_TABLE)
            if row["下游特征"] == target and row["确认状态"] == "待确认"
        ]
        if pending:
            reason = {
                "类型": "候选未提交",
                "说明": "候选血缘边尚未提交确认，不能用尚未启效的候选关系开工",
                "批次号": sorted({str(row["批次号"]) for row in pending}),
            }
            return {"result": "blocked", "reason": reason, "claims_for_target": 0}, \
                f"{target} 仅有候选关系，尚未启效，不能开工"

        return {"result": "blocked",
                "reason": {"类型": "无已确认边", "说明": "不存在指向该下游的已确认血缘边"},
                "claims_for_target": 0}, f"{target} 无已确认血缘边，不能开工"

    # ---------------------------------------------------------- 查询
    def _find_batch(self, batch_no: str) -> dict[str, Any] | None:
        for row in store.rows(BATCH_TABLE):
            if row["批次号"] == batch_no:
                return row
        return None

    def batch_detail(self, batch_no: str) -> dict[str, Any] | None:
        batch = self._find_batch(batch_no)
        if batch is None:
            return None
        cands = [c for c in store.rows(CANDIDATE_TABLE) if c["批次号"] == batch_no]
        return {"batch": batch, "candidates": cands}

    def list_batches(self) -> list[dict[str, Any]]:
        return list(store.rows(BATCH_TABLE))

    def list_claims(self) -> list[dict[str, Any]]:
        return list(store.rows(CLAIM_TABLE))

    def cycle_traces(self) -> list[dict[str, Any]]:
        """环路追溯：所有环形告警档案，按原规则编号可回溯。"""
        traces = []
        for row in store.rows("feature_line"):
            if row.get("status") == CYCLE:
                traces.append({
                    "链路编号": row.get("链路编号", ""),
                    "上游特征": row.get("上游特征", ""),
                    "下游特征": row.get("下游特征", ""),
                    "闭合路径": row.get("闭合路径", ""),
                    "来源规则": row.get("来源规则", ""),
                    "来源批次": row.get("来源批次", ""),
                    "最后更新": row.get("最后更新", ""),
                })
        return traces


lineage_batch_service = LineageBatchService()
