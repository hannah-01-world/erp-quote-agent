"""견적 비교 에이전트.

LLM은 숫자를 직접 계산하지 않는다. 아래 3개 도구(function)를 호출해서
Python이 계산한 결과를 받고, 구매 담당자가 읽을 추천 의견만 쓴다.

실행:
  python agent.py 7000000101            # Claude API 사용 (ANTHROPIC_API_KEY 필요)
  python agent.py 7000000101 --offline  # API 없이 도구만 순서대로 실행
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import sap_client
import scoring

MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
OUTPUT_DIR = Path(__file__).parent / "output"

SYSTEM_PROMPT = """너는 제조기업 구매팀을 돕는 견적 비교 도우미다.
규칙:
1. 가격, 점수, 비율 같은 숫자는 절대 직접 계산하지 말고 반드시 도구 결과에 있는 값만 인용한다.
2. 순서: get_quotations로 견적을 받고, score_quotations로 점수를 받고, check_price_anomaly로 리스크를 확인한다.
3. 검증에서 제외된 견적이 있으면 제외 사유를 먼저 알린다.
   sensitivity 결과에서 1위가 바뀌는 경우가 있으면 "점수만으로 결정하기 어렵다"고 분명히 쓰고, 어떤 가중치에 따라 갈리는지 알린다.
4. 최종 답변은 한국어로, 구매 담당자가 결재 올리기 전에 읽는 메모 형식으로 쓴다:
   추천 업체와 근거 / 2순위와의 차이 / 확인이 필요한 리스크 / 담당자가 직접 판단해야 할 것
5. 최종 선정은 사람이 한다. 너는 추천만 한다."""

TOOLS = [
    {
        "name": "get_quotations",
        "description": "RFQ 번호로 ERP에 등록된 공급업체 견적을 가져오고 데이터 정합성 검증 결과(비교 가능 견적, 제외 견적과 사유)를 함께 돌려준다.",
        "input_schema": {
            "type": "object",
            "properties": {"rfq_id": {"type": "string", "description": "견적요청(RFQ) 번호"}},
            "required": ["rfq_id"],
        },
    },
    {
        "name": "score_quotations",
        "description": "검증을 통과한 견적을 가격, 실효 납기(견적 납기+과거 평균 지연), 수량(MOQ), 조건(할인-배송비) 4개 항목으로 점수화해 가중 합산한 순위를 돌려준다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "rfq_id": {"type": "string"},
                "weights": {
                    "type": "object",
                    "description": "선택. price, lead_time, quantity, condition 가중치(합 1). 없으면 기본값 사용.",
                    "properties": {k: {"type": "number"} for k in scoring.DEFAULT_WEIGHTS},
                },
            },
            "required": ["rfq_id"],
        },
    },
    {
        "name": "check_price_anomaly",
        "description": "견적 단가를 과거 구매 단가와 비교해 20% 넘게 오른 견적을 표시하고, 업체별 과거 납기 지연 이력을 돌려준다.",
        "input_schema": {
            "type": "object",
            "properties": {"rfq_id": {"type": "string"}},
            "required": ["rfq_id"],
        },
    },
]


def run_tool(name: str, args: dict, audit: dict) -> dict:
    """LLM이 고른 도구를 실제로 실행한다. 모든 호출은 감사 로그에 남긴다."""
    quotes = sap_client.get_quotations(args["rfq_id"])
    fx = sap_client.get_fx_rates()
    valid, excluded = scoring.validate(quotes, fx)  # 외화 견적은 환율표 기준으로 원화 환산

    if name == "get_quotations":
        result = {
            "rfq_id": args["rfq_id"],
            "valid_quotations": [
                {k: q[k] for k in ("SupplierName", "Material", "NetPriceAmount", "DocumentCurrency",
                                    "DeliveryLeadTimeInDays", "MinimumOrderQuantity",
                                    "DiscountPercent", "ShippingCostAmount")}
                | ({"fx_note": q["FxNote"]} if "FxNote" in q else {})
                for q in valid
            ],
            "fx_base_date": fx.get("base_date"),
            "excluded": excluded,
        }
    elif name == "score_quotations":
        weights = args.get("weights") or scoring.DEFAULT_WEIGHTS
        history = sap_client.get_purchase_history(valid[0]["Material"]) if valid else []
        result = {
            "weights": weights,
            "lead_time_basis": "견적 납기 + 해당 업체 과거 평균 지연일 (거래 이력 없으면 견적 납기 그대로)",
            "ranking": scoring.score(valid, weights, history),
            "sensitivity": scoring.sensitivity(valid, history, weights),
        }
    elif name == "check_price_anomaly":
        material = valid[0]["Material"] if valid else ""
        history = sap_client.get_purchase_history(material)
        result = {"threshold_pct": scoring.ANOMALY_THRESHOLD * 100,
                  "findings": scoring.check_anomalies(valid, history)}
    else:
        result = {"error": f"알 수 없는 도구: {name}"}

    audit["tool_calls"].append({"tool": name, "input": args, "output": result})
    return result


def run_with_claude(rfq_id: str, audit: dict) -> str:
    import anthropic

    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": f"RFQ {rfq_id} 견적을 비교해서 추천 의견을 써줘."}]

    for _ in range(8):  # 도구 호출이 끝없이 반복되지 않도록 상한
        resp = client.messages.create(
            model=MODEL, max_tokens=1500, system=SYSTEM_PROMPT, tools=TOOLS, messages=messages
        )
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content if b.type == "text")

        tool_results = []
        for block in resp.content:
            if block.type == "tool_use":
                print(f"  [도구 호출] {block.name}({json.dumps(block.input, ensure_ascii=False)})")
                out = run_tool(block.name, block.input, audit)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(out, ensure_ascii=False),
                })
        messages.append({"role": "user", "content": tool_results})

    return "도구 호출 횟수 상한에 도달해 중단했습니다."


def run_offline(rfq_id: str, audit: dict) -> str:
    """API 키 없이 같은 도구를 고정 순서로 실행하고 결과를 표로 보여준다."""
    q = run_tool("get_quotations", {"rfq_id": rfq_id}, audit)
    s = run_tool("score_quotations", {"rfq_id": rfq_id}, audit)
    a = run_tool("check_price_anomaly", {"rfq_id": rfq_id}, audit)

    lines = [f"RFQ {rfq_id} 비교 결과 (오프라인 모드, LLM 미사용)", ""]
    for e in q["excluded"]:
        note = f" (원화 환산: {e['fx_note']})" if "fx_note" in e else ""
        lines.append(f"제외: {e['supplier']} - {', '.join(e['reasons'])}{note}")
    for v in q["valid_quotations"]:
        if "fx_note" in v:
            lines.append(f"원화 환산: {v['SupplierName']} {v['fx_note']}")
    lines.append("")
    lines.append(f"{'순위':<4}{'업체':<10}{'총점':>6}{'가격':>7}{'납기':>7}{'수량':>7}{'조건':>7}")
    for r in s["ranking"]:
        sc = r["scores"]
        lines.append(f"{r['rank']:<4}{r['supplier']:<10}{r['total']:>6}{sc['price']:>7}"
                     f"{sc['lead_time']:>7}{sc['quantity']:>7}{sc['condition']:>7}")
    sens = s["sensitivity"]
    lines.append("")
    lines.append(f"민감도: 가중치를 0.05씩 옮긴 {sens['scenarios_tried']}개 경우 중 "
                 f"{sens['scenarios_where_top_changes']}개에서 1위가 바뀜")
    for ex in sens["examples"][:2]:
        lines.append(f"  예) {ex['moved']} 이면 1위 {ex['new_top']}")
    lines.append("")
    for f in a["findings"]:
        flag = " [단가 이상]" if f.get("price_flag") else ""
        delay = (f", 과거 {f['past_orders']}건 평균 지연 {f['avg_delay_days']}일"
                 if f["past_orders"] else ", 거래 이력 없음")
        lines.append(f"{f['supplier']}: 기준 대비 {f.get('change_pct', '-')}%{flag}{delay}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("rfq_id")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()

    audit = {
        "rfq_id": args.rfq_id,
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "offline" if args.offline else f"claude:{MODEL}",
        "tool_calls": [],
    }

    if not args.offline and not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY가 없어 오프라인 모드로 실행합니다.\n", file=sys.stderr)
        args.offline = True
        audit["mode"] = "offline"

    answer = run_offline(args.rfq_id, audit) if args.offline else run_with_claude(args.rfq_id, audit)
    audit["final_answer"] = answer

    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / f"audit_{args.rfq_id}_{datetime.now():%Y%m%d_%H%M%S}.json"
    path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")

    print(answer)
    print(f"\n감사 로그 저장: {path}")


if __name__ == "__main__":
    main()
