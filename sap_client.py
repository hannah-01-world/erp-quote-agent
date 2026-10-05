"""ERP 데이터를 읽어오는 부분.

SAP S/4HANA의 OData API는 응답을 {"d": {"results": [...]}} 형태의 JSON으로 준다.
이 프로토타입은 같은 형태의 JSON 파일을 기본으로 읽고,
SAP_API_KEY 환경변수가 있으면 SAP Business Accelerator Hub 샌드박스를 직접 호출한다.
"""

import json
import os
from pathlib import Path

import requests

DATA_DIR = Path(__file__).parent / "data"
SANDBOX_BASE = "https://sandbox.api.sap.com/s4hanacloud/sap/opu/odata/sap"


def _results(payload: dict) -> list[dict]:
    """OData v2 응답에서 실제 레코드 목록만 꺼낸다."""
    return payload.get("d", {}).get("results", [])


def load_local(filename: str) -> list[dict]:
    with open(DATA_DIR / filename, encoding="utf-8") as f:
        return _results(json.load(f))


def fetch_sandbox(service: str, entity: str, top: int = 5) -> list[dict]:
    """SAP 샌드박스 OData 호출.

    service 예: API_PURCHASEREQ_PROCESS_SRV, entity 예: A_PurchaseRequisitionHeader
    정확한 서비스명과 엔티티명은 api.sap.com의 API 문서에서 확인한다.
    """
    api_key = os.environ.get("SAP_API_KEY")
    if not api_key:
        raise RuntimeError("SAP_API_KEY 환경변수가 없습니다. api.sap.com에서 발급받으세요.")
    url = f"{SANDBOX_BASE}/{service}/{entity}"
    resp = requests.get(
        url,
        headers={"APIKey": api_key, "Accept": "application/json"},
        params={"$top": top, "$format": "json"},
        timeout=20,
    )
    resp.raise_for_status()
    return _results(resp.json())


def get_quotations(rfq_id: str) -> list[dict]:
    return [q for q in load_local("quotations.json") if q["RequestForQuotation"] == rfq_id]


def get_purchase_history(material: str) -> list[dict]:
    return [p for p in load_local("purchase_history.json") if p["Material"] == material]
