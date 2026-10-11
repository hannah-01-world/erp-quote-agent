import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import sap_client
import scoring

RFQ = "7000000101"
FX = sap_client.get_fx_rates()
HISTORY = sap_client.get_purchase_history("UM-3000")


def _valid():
    valid, _ = scoring.validate(sap_client.get_quotations(RFQ), FX)
    return valid


def test_usd_quote_converted_but_excluded_for_missing_shipping():
    _, excluded = scoring.validate(sap_client.get_quotations(RFQ), FX)
    assert [e["supplier"] for e in excluded] == ["SJ Tech"]
    reasons = excluded[0]["reasons"]
    assert not any("통화" in r for r in reasons)          # 환율표에 USD가 있으니 통화 문제는 사라짐
    assert reasons == ["ShippingCostAmount 값 누락"]      # 배송비 누락만 남음
    assert "41.50 USD × 1,380" in excluded[0]["fx_note"]  # 환산 근거 기록


def test_usd_quote_with_shipping_is_converted_to_krw():
    sj = dict(next(q for q in sap_client.get_quotations(RFQ) if q["SupplierName"] == "SJ Tech"))
    sj["ShippingCostAmount"] = "20"
    valid, excluded = scoring.validate([sj], FX)
    assert excluded == []
    assert valid[0]["DocumentCurrency"] == "KRW"
    assert float(valid[0]["NetPriceAmount"]) == 57270.0     # 41.5 × 1,380
    assert float(valid[0]["ShippingCostAmount"]) == 27600.0  # 20 × 1,380


def test_unknown_currency_is_excluded():
    q = dict(sap_client.get_quotations(RFQ)[0], DocumentCurrency="EUR")
    _, excluded = scoring.validate([q], FX)
    assert any("환율표에 없는 통화" in r for r in excluded[0]["reasons"])


def test_same_input_same_result():
    assert scoring.score(_valid(), history=HISTORY) == scoring.score(_valid(), history=HISTORY)


def test_past_delay_changes_effective_lead_time():
    rows = {r["supplier"]: r for r in scoring.score(_valid(), history=HISTORY)}
    assert rows["HY Tech"]["raw"]["effective_lead_days"] == 10.0  # 견적 5일 + 평균 지연 5일
    assert rows["CH Tech"]["raw"]["effective_lead_days"] == 4.0   # 이력 없음, 견적 그대로


def test_weights_must_sum_to_one():
    try:
        scoring.score(_valid(), {"price": 0.5, "lead_time": 0.5, "quantity": 0.5, "condition": 0})
        assert False, "가중치 합 오류를 잡아야 함"
    except ValueError:
        pass


def test_ch_tech_price_flagged():
    findings = {f["supplier"]: f for f in scoring.check_anomalies(_valid(), HISTORY)}
    assert findings["CH Tech"]["price_flag"] is True
    assert findings["HG Tech"]["price_flag"] is False


def test_moq_over_request_is_penalized():
    rows = {r["supplier"]: r for r in scoring.score(_valid())}
    assert rows["CH Tech"]["scores"]["quantity"] == 50.0
