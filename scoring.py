"""견적 검증, 점수 계산, 이상 탐지. LLM 없이 Python으로만 계산한다.

같은 입력이면 언제 돌려도 같은 결과가 나와야 하고,
구매 결정은 나중에 감사(audit) 대상이 되므로 계산 근거를 숫자로 남긴다.
"""

from statistics import mean

# 항목별 기본 가중치. 합이 1이어야 한다. 근거 있는 값은 아니고, ERP 연구회 실습 때 가격을 가장 중시했던 것을 따른 임시값
DEFAULT_WEIGHTS = {"price": 0.40, "lead_time": 0.30, "quantity": 0.15, "condition": 0.15}
ANOMALY_THRESHOLD = 0.20  # 직전 구매 단가보다 20% 넘게 비싸면 플래그
BASE_CURRENCY = "KRW"     # 모든 금액을 이 통화로 맞춘 뒤 비교한다


# ---------- 1. 데이터 정합성 검증 ----------

def _is_blank(value) -> bool:
    """값이 비어 있는지. JSON에서 ""나 None으로 들어온 경우를 모두 잡는다."""
    return value is None or str(value).strip() == ""


def validate(quotes: list[dict], fx: dict | None = None) -> tuple[list[dict], list[dict]]:
    """비교에 쓸 수 있는 견적과 제외할 견적을 나눈다. 제외 사유를 남긴다.

    fx(환율표)가 있으면 외화 견적을 원화로 바꿔서 비교 대상에 넣는다.
    환율표에 없는 통화만 제외한다.
    """
    rates = (fx or {}).get("rates_to_krw", {})
    base_date = (fx or {}).get("base_date", "")
    valid, excluded = [], []
    for q in quotes:
        q = dict(q)  # 원본 견적을 직접 고치지 않도록 복사본으로 작업
        reasons = []

        # 필수 값이 비어 있으면 점수를 낼 수 없으므로 제외
        for field in ("NetPriceAmount", "DeliveryLeadTimeInDays", "MinimumOrderQuantity", "ShippingCostAmount"):
            if _is_blank(q.get(field)):
                reasons.append(f"{field} 값 누락")

        # 단위가 다르면(예: BOX와 EA) 단가를 그대로 비교할 수 없음
        if q.get("OrderQuantityUnit") != "EA":
            reasons.append(f"단위 불일치({q.get('OrderQuantityUnit')})")

        # 통화 처리: 원화면 그대로, 환율표에 있으면 환산, 없으면 제외
        cur = q.get("DocumentCurrency")
        if cur != BASE_CURRENCY:
            if cur in rates:
                rate = float(rates[cur])
                orig_price = q.get("NetPriceAmount")
                if not _is_blank(orig_price):
                    q["NetPriceAmount"] = str(round(float(orig_price) * rate, 2))
                if not _is_blank(q.get("ShippingCostAmount")):
                    q["ShippingCostAmount"] = str(round(float(q["ShippingCostAmount"]) * rate, 2))
                q["DocumentCurrency"] = BASE_CURRENCY
                # 나중에 "이 금액은 어디서 나왔냐"에 답할 수 있게 환산 근거를 견적에 붙여 둔다
                q["FxNote"] = f"{orig_price} {cur} × {rate:,.0f} (기준일 {base_date})"
            else:
                reasons.append(f"통화 불일치({cur}), 환율표에 없는 통화")

        if reasons:
            excluded.append({"supplier": q["SupplierName"], "reasons": reasons,
                             **({"fx_note": q["FxNote"]} if "FxNote" in q else {})})
        else:
            valid.append(q)
    return valid, excluded


# ---------- 2. 항목별 점수 (0~100) ----------

def _ratio_to_best(values: dict[str, float], lower_is_better: bool = True) -> dict[str, float]:
    """가장 좋은 값을 100점으로 두고 나머지를 비율로 환산한다.

    가격, 납기처럼 낮을수록 좋은 값은 최저값 ÷ 내 값 × 100.
    예) 최저가 58,000원, HY 61,000원 → 58000 / 61000 × 100 = 95.1점
    """
    if lower_is_better:
        best = min(values.values())
        return {k: round(best / v * 100, 1) if v else 100.0 for k, v in values.items()}
    best = max(values.values())
    return {k: round(v / best * 100, 1) if best else 100.0 for k, v in values.items()}


def _avg_delay(supplier: str, history: list[dict]) -> float:
    """해당 업체의 과거 납품 지연일 평균. (실제 납기 - 약속 납기)의 평균, 이력 없으면 0"""
    delays = [int(h["ActualDays"]) - int(h["PromisedDays"]) for h in history if h["Supplier"] == supplier]
    return mean(delays) if delays else 0.0


def score(quotes: list[dict], weights: dict | None = None, history: list[dict] | None = None) -> list[dict]:
    """history를 주면 견적 납기에 과거 평균 지연일을 더한 '실효 납기'로 납기 점수를 낸다."""
    w = weights or DEFAULT_WEIGHTS
    # 가중치 합이 1이 아니면 총점이 100점 기준에서 벗어나므로 계산 자체를 막는다
    if abs(sum(w.values()) - 1.0) > 1e-6:
        raise ValueError(f"가중치 합이 1이 아닙니다: {sum(w.values())}")
    history = history or []

    # 업체 이름을 열쇠로 하는 사전들. 예: price = {"HG Tech": 58000.0, "HY Tech": 61000.0, ...}
    qty = {q["SupplierName"]: float(q["RequestedQuantity"]) for q in quotes}           # 요청 수량
    price = {q["SupplierName"]: float(q["NetPriceAmount"]) for q in quotes}            # 단가 (JSON엔 글자라 float으로 변환)
    quoted_lead = {q["SupplierName"]: float(q["DeliveryLeadTimeInDays"]) for q in quotes}  # 견적서 납기
    delay = {q["SupplierName"]: _avg_delay(q["Supplier"], history) for q in quotes}    # 과거 평균 지연. HY 5일, HG 0.5일
    lead = {k: quoted_lead[k] + delay[k] for k in quoted_lead}                         # 실효 납기. HY 5+5=10일
    moq = {q["SupplierName"]: float(q["MinimumOrderQuantity"]) for q in quotes}        # 최소주문수량

    # 조건: 할인으로 아끼는 금액에서 배송비를 뺀 순이익(원). 클수록 좋다.
    # 예) HG: 58,000 × 100개 × 2% - 배송비 30,000 = 86,000원
    cond_won = {
        q["SupplierName"]: price[q["SupplierName"]] * qty[q["SupplierName"]]
        * float(q["DiscountPercent"]) / 100 - float(q["ShippingCostAmount"])
        for q in quotes
    }
    # 조건 금액은 마이너스(CH -50,000)가 나올 수 있어서 비율 방식을 못 쓴다.
    # 그래서 최저를 0점, 최고를 100점으로 놓고 그 사이에 비례 배치한다.
    lo, hi = min(cond_won.values()), max(cond_won.values())
    cond_score = {k: 100.0 if hi == lo else round((v - lo) / (hi - lo) * 100, 1) for k, v in cond_won.items()}

    # 수량: 최소주문수량이 요청 수량 이하면 100점, 넘으면 요청 ÷ MOQ로 감점
    # 예) CH는 100개 사려는데 최소 200개 → 100 / 200 × 100 = 50점
    qty_score = {k: 100.0 if moq[k] <= qty[k] else round(qty[k] / moq[k] * 100, 1) for k in qty}

    price_score = _ratio_to_best(price)   # 가격: 낮을수록 좋음
    lead_score = _ratio_to_best(lead)     # 실효 납기: 짧을수록 좋음

    rows = []
    for name in price:
        parts = {
            "price": price_score[name],
            "lead_time": lead_score[name],
            "quantity": qty_score[name],
            "condition": cond_score[name],
        }
        # 총점 = 항목 점수 × 가중치의 합. HG: 100×0.4 + 53.3×0.3 + 100×0.15 + 58.4×0.15 = 79.75 → 79.8
        # 한계: 항목 점수를 먼저 소수 첫째 자리로 반올림한 뒤 곱하므로, 점수 차가 아주 작을 땐 반올림 영향이 섞인다
        total = round(sum(parts[k] * w[k] for k in w), 1)
        rows.append({
            "supplier": name,
            "total": total,
            "scores": parts,
            "raw": {  # 점수의 근거가 된 원래 숫자. 감사 로그에 같이 남는다
                "unit_price": price[name],
                "quoted_lead_days": quoted_lead[name],
                "avg_past_delay_days": round(delay[name], 1),
                "effective_lead_days": round(lead[name], 1),
                "moq": moq[name],
                "requested_qty": qty[name],
                "condition_net_won": round(cond_won[name]),
            },
        })
    rows.sort(key=lambda r: r["total"], reverse=True)  # 총점 높은 순으로 정렬
    for i, r in enumerate(rows, start=1):
        r["rank"] = i
    return rows


def sensitivity(quotes: list[dict], history: list[dict] | None = None,
                weights: dict | None = None, step: float = 0.05) -> dict:
    """가중치 하나에서 step만큼 떼어 다른 항목에 옮겨보며 1위가 바뀌는지 확인한다.

    항목이 4개라 '뺄 항목(src) × 더할 항목(dst)' 조합은 4 × 3 = 12가지.
    12번 중 1위가 바뀌는 경우가 많으면, 순위가 데이터보다 가중치 설정에 좌우되고 있다는 뜻이다.
    """
    base_w = weights or DEFAULT_WEIGHTS
    base_top = score(quotes, base_w, history)[0]["supplier"]  # 기본 가중치일 때 1위
    flips, tried = [], 0
    for src in base_w:          # 가중치를 뺄 항목
        for dst in base_w:      # 가중치를 더할 항목
            if src == dst or base_w[src] < step:  # 같은 항목이거나 뺄 만큼 남아 있지 않으면 건너뜀
                continue
            w = dict(base_w)
            w[src] = round(w[src] - step, 4)
            w[dst] = round(w[dst] + step, 4)
            tried += 1
            top = score(quotes, w, history)[0]["supplier"]
            if top != base_top:
                flips.append({"moved": f"{src} -{step} → {dst} +{step}", "new_top": top})
    return {"base_top": base_top, "scenarios_tried": tried,
            "scenarios_where_top_changes": len(flips), "examples": flips[:4]}


# ---------- 3. 원가 이상 탐지 + 납기 이력 ----------

def check_anomalies(quotes: list[dict], history: list[dict]) -> list[dict]:
    """공급업체별로 직전 구매 단가 대비 상승률과 과거 납기 지연을 계산한다."""
    findings = []
    all_last_prices = [float(h["NetPriceAmount"]) for h in history]
    for q in quotes:
        # 이 업체의 과거 구매오더를 날짜순으로 정렬. 마지막이 가장 최근
        mine = sorted(
            [h for h in history if h["Supplier"] == q["Supplier"]],
            key=lambda h: h["PurchaseOrderDate"],
        )
        quoted = float(q["NetPriceAmount"])
        # 비교 기준 단가: 거래 이력이 있으면 그 업체 직전 단가, 없으면 같은 자재 전체 평균
        if mine:
            ref, ref_basis = float(mine[-1]["NetPriceAmount"]), "해당 업체 직전 구매 단가"
        elif all_last_prices:
            ref, ref_basis = mean(all_last_prices), "거래 이력 없음, 같은 자재 전체 평균 구매 단가"
        else:
            ref, ref_basis = None, "비교 기준 없음"

        item = {"supplier": q["SupplierName"], "quoted_price": quoted, "reference_basis": ref_basis}
        if ref:
            change = (quoted - ref) / ref  # 상승률. CH: (72,000 - 59,750) / 59,750 = 20.5%
            item["reference_price"] = round(ref)
            item["change_pct"] = round(change * 100, 1)
            item["price_flag"] = change > ANOMALY_THRESHOLD

        if mine:
            delays = [int(h["ActualDays"]) - int(h["PromisedDays"]) for h in mine]
            item["past_orders"] = len(mine)
            item["avg_delay_days"] = round(mean(delays), 1)
            item["max_delay_days"] = max(delays)
        else:
            item["past_orders"] = 0
        findings.append(item)
    return findings
