"""견적 검증, 점수 계산, 이상 탐지. LLM 없이 Python으로만 계산한다.

같은 입력이면 언제 돌려도 같은 결과가 나와야 하고,
구매 결정은 나중에 감사(audit) 대상이 되므로 계산 근거를 숫자로 남긴다.
"""

from statistics import mean

DEFAULT_WEIGHTS = {"price": 0.40, "lead_time": 0.30, "quantity": 0.15, "condition": 0.15}
ANOMALY_THRESHOLD = 0.20  # 직전 구매 단가보다 20% 넘게 비싸면 플래그
BASE_CURRENCY = "KRW"


# ---------- 1. 데이터 정합성 검증 ----------

def validate(quotes: list[dict]) -> tuple[list[dict], list[dict]]:
    """비교에 쓸 수 있는 견적과 제외할 견적을 나눈다. 제외 사유를 남긴다."""
    valid, excluded = [], []
    for q in quotes:
        reasons = []
        if q.get("DocumentCurrency") != BASE_CURRENCY:
            reasons.append(f"통화 불일치({q.get('DocumentCurrency')}), 환율 기준 없이 비교 불가")
        for field in ("NetPriceAmount", "DeliveryLeadTimeInDays", "MinimumOrderQuantity", "ShippingCostAmount"):
            if str(q.get(field, "")).strip() == "":
                reasons.append(f"{field} 값 누락")
        if q.get("OrderQuantityUnit") != "EA":
            reasons.append(f"단위 불일치({q.get('OrderQuantityUnit')})")
        if reasons:
            excluded.append({"supplier": q["SupplierName"], "reasons": reasons})
        else:
            valid.append(q)
    return valid, excluded


# ---------- 2. 항목별 점수 (0~100) ----------

def _ratio_to_best(values: dict[str, float], lower_is_better: bool = True) -> dict[str, float]:
    """가장 좋은 값을 100점으로 두고 나머지를 비율로 환산한다."""
    if lower_is_better:
        best = min(values.values())
        return {k: round(best / v * 100, 1) if v else 100.0 for k, v in values.items()}
    best = max(values.values())
    return {k: round(v / best * 100, 1) if best else 100.0 for k, v in values.items()}


def _avg_delay(supplier: str, history: list[dict]) -> float:
    delays = [int(h["ActualDays"]) - int(h["PromisedDays"]) for h in history if h["Supplier"] == supplier]
    return mean(delays) if delays else 0.0


def score(quotes: list[dict], weights: dict | None = None, history: list[dict] | None = None) -> list[dict]:
    """history를 주면 견적 납기에 과거 평균 지연일을 더한 '실효 납기'로 납기 점수를 낸다."""
    w = weights or DEFAULT_WEIGHTS
    if abs(sum(w.values()) - 1.0) > 1e-6:
        raise ValueError(f"가중치 합이 1이 아닙니다: {sum(w.values())}")
    history = history or []

    qty = {q["SupplierName"]: float(q["RequestedQuantity"]) for q in quotes}
    price = {q["SupplierName"]: float(q["NetPriceAmount"]) for q in quotes}
    quoted_lead = {q["SupplierName"]: float(q["DeliveryLeadTimeInDays"]) for q in quotes}
    delay = {q["SupplierName"]: _avg_delay(q["Supplier"], history) for q in quotes}
    lead = {k: quoted_lead[k] + delay[k] for k in quoted_lead}
    moq = {q["SupplierName"]: float(q["MinimumOrderQuantity"]) for q in quotes}

    # Condition: 할인으로 아끼는 금액에서 배송비를 뺀 순이익(원). 클수록 좋다.
    cond_won = {
        q["SupplierName"]: price[q["SupplierName"]] * qty[q["SupplierName"]]
        * float(q["DiscountPercent"]) / 100 - float(q["ShippingCostAmount"])
        for q in quotes
    }
    lo, hi = min(cond_won.values()), max(cond_won.values())
    cond_score = {k: 100.0 if hi == lo else round((v - lo) / (hi - lo) * 100, 1) for k, v in cond_won.items()}

    # Quantity: 최소주문수량(MOQ)이 요청 수량 이하면 100점, 넘으면 초과분만큼 감점
    qty_score = {k: 100.0 if moq[k] <= qty[k] else round(qty[k] / moq[k] * 100, 1) for k in qty}

    price_score = _ratio_to_best(price)
    lead_score = _ratio_to_best(lead)

    rows = []
    for name in price:
        parts = {
            "price": price_score[name],
            "lead_time": lead_score[name],
            "quantity": qty_score[name],
            "condition": cond_score[name],
        }
        total = round(sum(parts[k] * w[k] for k in w), 1)
        rows.append({
            "supplier": name,
            "total": total,
            "scores": parts,
            "raw": {
                "unit_price": price[name],
                "quoted_lead_days": quoted_lead[name],
                "avg_past_delay_days": round(delay[name], 1),
                "effective_lead_days": round(lead[name], 1),
                "moq": moq[name],
                "requested_qty": qty[name],
                "condition_net_won": round(cond_won[name]),
            },
        })
    rows.sort(key=lambda r: r["total"], reverse=True)
    for i, r in enumerate(rows, start=1):
        r["rank"] = i
    return rows


def sensitivity(quotes: list[dict], history: list[dict] | None = None,
                weights: dict | None = None, step: float = 0.05) -> dict:
    """가중치 하나에서 step만큼 떼어 다른 항목에 옮겨보며 1위가 바뀌는지 확인한다."""
    base_w = weights or DEFAULT_WEIGHTS
    base_top = score(quotes, base_w, history)[0]["supplier"]
    flips, tried = [], 0
    for src in base_w:
        for dst in base_w:
            if src == dst or base_w[src] < step:
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
        mine = sorted(
            [h for h in history if h["Supplier"] == q["Supplier"]],
            key=lambda h: h["PurchaseOrderDate"],
        )
        quoted = float(q["NetPriceAmount"])
        if mine:
            ref, ref_basis = float(mine[-1]["NetPriceAmount"]), "해당 업체 직전 구매 단가"
        elif all_last_prices:
            ref, ref_basis = mean(all_last_prices), "거래 이력 없음, 같은 자재 전체 평균 구매 단가"
        else:
            ref, ref_basis = None, "비교 기준 없음"

        item = {"supplier": q["SupplierName"], "quoted_price": quoted, "reference_basis": ref_basis}
        if ref:
            change = (quoted - ref) / ref
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
