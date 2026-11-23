"""端到端验证：整组启效预检 → 批次提交 → 调度领取 的完整场景。

场景（对应需求验收步骤）：
  0. 已确认图先有 A→B；另布两条干扰边：待确认 E→D、已废弃 C→A，
     验证"待确认或已废弃边别算成已生效关系"。
  1. 一次提交 B→C、C→A、D→E（前两条与已确认图成环，D→E 给独立下游供数）。
  2. 提交后立即尝试领取 E 的重算：已确认可领取；领取 C 则本批未发布且有环路原因，
     C 不得借环路提交。
  3. 更正 C→A（改为 C→D）后再次确认并领取：E 的重算不重复登记，
     环路能按两个原规则编号追溯。

运行：backend/.venv/bin/python scripts/e2e_lineage_batch.py
脚本会自己在 8123 端口起一套服务，跑完自动关闭。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8123"
BACKEND_DIR = Path(__file__).resolve().parent.parent

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASSED if cond else FAILED).append(name)
    mark = "PASS" if cond else "FAIL"
    suffix = f" | {detail}" if detail else ""
    print(f"  [{mark}] {name}{suffix}")


def req(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        payload = exc.read() or b"{}"
        return {"_http_error": exc.code, **json.loads(payload)}


def feature_lines() -> list[dict]:
    return req("GET", "/api/feature_line?size=200")["items"]


def derive_rules() -> list[dict]:
    return req("GET", "/api/derive_rule?size=200")["items"]


def line_of(up: str, down: str, status: str | None = None) -> list[dict]:
    rows = [r for r in feature_lines()
            if r.get("上游特征") == up and r.get("下游特征") == down]
    if status:
        rows = [r for r in rows if r.get("status") == status]
    return rows


def rule_status(rule_no: str) -> str:
    for row in derive_rules():
        if row.get("规则编号") == rule_no:
            return str(row.get("status"))
    return "<缺失>"


def main() -> int:
    # ------------------------------------------------ 步骤0：布景
    print("步骤0：先有 A→B（已确认），并布干扰边（待确认 E→D、已废弃 C→A）")
    r = req("POST", "/api/feature_line",
            {"values": {"链路编号": "FEAT-A-B", "上游特征": "A", "下游特征": "B"}})
    assert r["ok"], r
    ab_id = r["entry"]["id"]
    r = req("POST", f"/api/feature_line/{ab_id}/actions", {"action": "确认链路"})
    check("A→B 已确认", r["ok"] and r["entry"]["status"] == "已确认")

    req("POST", "/api/feature_line",
        {"values": {"链路编号": "FEAT-E-D", "上游特征": "E", "下游特征": "D"}})  # 保持待确认
    r = req("POST", "/api/feature_line",
            {"values": {"链路编号": "FEAT-C-A", "上游特征": "C", "下游特征": "A"}})
    ca_old = r["entry"]["id"]
    req("POST", f"/api/feature_line/{ca_old}/actions", {"action": "废弃链路"})  # 已废弃

    for no, up, down in [("R-BC", "B", "C"), ("R-CA", "C", "A"), ("R-DE", "D", "E")]:
        r = req("POST", "/api/derive_rule",
                {"values": {"规则编号": no, "输入特征": up, "衍生函数": "f", "输出特征": down}})
        assert r["ok"], r
    check("三条衍生规则登记为草稿",
          all(rule_status(no) == "草稿" for no in ("R-BC", "R-CA", "R-DE")))

    # ------------------------------------------------ 步骤1：整组预检
    print("步骤1：整组启效预检 [B→C, C→A, D→E]，形成候选血缘边")
    r = req("POST", "/api/lineage/batches/precheck", {
        "edges": [
            {"rule_no": "R-BC", "upstream": "B", "downstream": "C"},
            {"rule_no": "R-CA", "upstream": "C", "downstream": "A"},
            {"rule_no": "R-DE", "upstream": "D", "downstream": "E"},
        ],
        "remark": "一次启效多条衍生规则",
    })
    check("预检成功", r["ok"], r.get("message", ""))
    batch_no = r["batch"]["批次号"]
    verdict = {c["规则编号"]: c for c in r["candidates"]}
    check("B→C 判为环阻断", verdict["R-BC"]["预检结论"] == "环阻断",
          verdict["R-BC"]["闭合路径"])
    check("B→C 闭合路径为实际闭环 B→C→A→B",
          verdict["R-BC"]["闭合路径"] == "B→C→A→B")
    check("C→A 判为环阻断", verdict["R-CA"]["预检结论"] == "环阻断",
          verdict["R-CA"]["闭合路径"])
    check("C→A 闭合路径为实际闭环 C→A→B→C",
          verdict["R-CA"]["闭合路径"] == "C→A→B→C")
    check("D→E 通过（独立下游；待确认 E→D 干扰边未被算成已生效关系）",
          verdict["R-DE"]["预检结论"] == "通过")
    check("候选边均为待确认（未启效）",
          all(c["确认状态"] == "待确认" for c in r["candidates"]))

    # ------------------------------------------------ 步骤2：提交确认
    print("步骤2：特征血缘读取候选血缘边并提交确认")
    r = req("POST", f"/api/lineage/batches/{batch_no}/commit")
    check("批次提交成功", r["ok"], r.get("message", ""))
    check("D→E 进入确认结果", len(line_of("D", "E", "已确认")) == 1)
    check("B→C 未进入确认结果（环上的边始终不能启效）",
          len(line_of("B", "C", "已确认")) == 0
          and len(line_of("B", "C", "环形告警")) == 1)
    check("C→A 未进入确认结果（C 不得借环路提交）",
          len(line_of("C", "A", "已确认")) == 0
          and len(line_of("C", "A", "环形告警")) == 1)
    check("环边档案保留原规则编号",
          {l.get("来源规则") for l in feature_lines() if l["status"] == "环形告警"
           and l.get("来源批次") == batch_no} == {"R-BC", "R-CA"})
    check("通过边对应规则已启效（R-DE 生效中）", rule_status("R-DE") == "生效中")
    check("环边对应规则保持未启效（R-BC/R-CA 仍草稿）",
          rule_status("R-BC") == "草稿" and rule_status("R-CA") == "草稿")

    lines_before = len(feature_lines())
    r = req("POST", f"/api/lineage/batches/{batch_no}/commit")
    check("重复提交幂等（不重复写入血缘）",
          r["ok"] and r["idempotent"] and len(feature_lines()) == lines_before)

    # ------------------------------------------------ 步骤3：提交后立即领取
    print("步骤3：提交后立即尝试领取 E / C 的重算")
    r = req("POST", "/api/lineage/recompute/claim", {"downstream": "E"})
    check("E 已确认可领取", r["ok"] and r["result"] == "claimed", r.get("message", ""))
    r = req("POST", "/api/lineage/recompute/claim", {"downstream": "C"})
    check("C 本批未发布且有环路原因",
          not r["ok"] and r["result"] == "blocked"
          and r["reason"]["类型"] == "环路阻断",
          json.dumps(r.get("reason", {}), ensure_ascii=False))
    check("C 的阻断原因带闭合路径和原规则编号",
          any("C→A→B→C" in p or "B→C→A→B" in p for p in r["reason"]["闭合路径"])
          and "R-BC" in r["reason"]["规则编号"])
    check("C 仍未借环路提交（C→A 无已确认边）",
          len(line_of("C", "A", "已确认")) == 0)

    # ------------------------------------------------ 步骤4：更正后重试
    print("步骤4：更正 C→A 为 C→D（沿用原规则编号），B→C 拆分后随批重试")
    r = req("POST", "/api/lineage/batches/precheck", {
        "edges": [
            {"rule_no": "R-BC", "upstream": "B", "downstream": "C"},
            {"rule_no": "R-CA", "upstream": "C", "downstream": "D"},
        ],
        "remark": "更正 C→A 后拆分重试",
    })
    batch2 = r["batch"]["批次号"]
    verdict2 = {c["规则编号"]: c for c in r["candidates"]}
    check("环解除后 B→C 重试通过", verdict2["R-BC"]["预检结论"] == "通过")
    check("更正后的 C→D 通过", verdict2["R-CA"]["预检结论"] == "通过")
    r = req("POST", f"/api/lineage/batches/{batch2}/commit")
    check("第二批提交成功", r["ok"], r.get("message", ""))
    check("B→C、C→D 进入确认结果",
          len(line_of("B", "C", "已确认")) == 1 and len(line_of("C", "D", "已确认")) == 1)

    # ------------------------------------------------ 步骤5：再次领取
    print("步骤5：再次确认后领取，验证幂等与追溯")
    r = req("POST", "/api/lineage/recompute/claim", {"downstream": "E"})
    check("E 的重算不重复登记", r["result"] == "already_claimed", r.get("message", ""))
    e_claims = [c for c in req("GET", "/api/lineage/recompute/claims")["items"]
                if c["下游特征"] == "E"]
    check("E 只有一条领取登记", len(e_claims) == 1)
    r = req("POST", "/api/lineage/recompute/claim", {"downstream": "C"})
    check("C 现在可按已确认边 B→C 领取", r["ok"] and r["result"] == "claimed")

    traces = req("GET", "/api/lineage/cycles")["items"]
    traced_rules = {t["来源规则"] for t in traces if t["来源批次"] == batch_no}
    check("环路能按两个原规则编号追溯", traced_rules == {"R-BC", "R-CA"},
          json.dumps(traces, ensure_ascii=False))
    check("追溯记录保留实际闭合路径",
          all(t["闭合路径"] for t in traces if t["来源批次"] == batch_no))

    # ------------------------------------------------ 汇总
    total = len(PASSED) + len(FAILED)
    print(f"\n结果：{len(PASSED)}/{total} 通过")
    if FAILED:
        print("未通过项：")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    return 0


def wait_ready(proc: subprocess.Popen, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("服务进程提前退出")
        try:
            with urllib.request.urlopen(BASE + "/api/health", timeout=1) as resp:
                if resp.status == 200:
                    return
        except OSError:
            time.sleep(0.3)
    raise RuntimeError("服务启动超时")


if __name__ == "__main__":
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", "8123"],
        cwd=BACKEND_DIR,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_ready(server)
        sys.exit(main())
    finally:
        server.terminate()
        server.wait(timeout=10)
