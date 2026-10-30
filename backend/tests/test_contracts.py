from decimal import Decimal

import pytest
from pydantic import ValidationError

from app import contracts as c
from app.models import Base

FORECAST = dict(product_id="FG001", target_month="2026-11", horizon=1, p50="100", p80="120", p90="150",
                p50_method="median", quantile_model_version="q-0.1", trained_until="2026-10-31")


def test_forecast_valid():
    r = c.ForecastRow(**FORECAST)
    assert r.p90 == Decimal("150")


@pytest.mark.parametrize("patch", [
    dict(p80="90"),                 # p50 > p80
    dict(p90="110"),                # p80 > p90
    dict(horizon=0),
    dict(target_month="2026-13"),
    dict(target_month="2026-11-01"),
    dict(p50="NaN"),
    dict(p50="-1"),
    dict(unknown_field="x"),
])
def test_forecast_rejects(patch):
    with pytest.raises(ValidationError):
        c.ForecastRow(**{**FORECAST, **patch})


def test_monthly_demand_period_format():
    ok = c.MonthlyDemandRow(product_id="FG001", period="2026-01", demand_qty="1,000".replace(",", ""), unit="EA")
    assert ok.demand_qty == Decimal("1000")
    with pytest.raises(ValidationError):
        c.MonthlyDemandRow(product_id="FG001", period="2026-1", demand_qty="1", unit="EA")
    with pytest.raises(ValidationError):
        c.MonthlyDemandRow(product_id="FG001", period="2026-01", demand_qty="1,000", unit="EA")   # 쉼표 숫자는 파서가 먼저 정규화해야 함


def test_bom_rules():
    base = dict(bom_id="B1", product_id="FG001", bom_version="V1", status="DRAFT", is_active=False, valid_from="2026-01-01")
    c.BomHeaderRow(**base)
    with pytest.raises(ValidationError):
        c.BomHeaderRow(**{**base, "is_active": True})                   # 미승인 BOM 활성화
    with pytest.raises(ValidationError):
        c.BomHeaderRow(**{**base, "valid_to": "2025-12-31"})


def test_inventory_and_capacity_rules():
    with pytest.raises(ValidationError):
        c.InventoryRow(item_type="PRODUCT", item_id="FG001", as_of_date="2026-10-01", on_hand_qty="5", allocated_qty="6", unit="EA")
    with pytest.raises(ValidationError):
        c.CapacityRow(resource_id="L1", resource_name="Line 1", regular_hours_per_day="20", overtime_hours_per_day_max="5")


def test_contract_fields_exist_as_db_columns():
    """계약의 필드 이름이 DB 열과 어긋나지 않는다 (component_id 는 두 열로 갈라지는 알려진 예외)."""
    pairs = {
        "unit": c.UnitRow, "product": c.ProductRow, "material": c.MaterialRow, "monthly_demand": c.MonthlyDemandRow,
        "sales_order": c.SalesOrderRow, "bom_header": c.BomHeaderRow, "bom_line": c.BomLineRow, "inventory": c.InventoryRow,
        "production_capacity": c.CapacityRow, "demand_forecast": c.ForecastRow,
    }
    for table, model in pairs.items():
        cols = set(Base.metadata.tables[table].columns.keys())
        missing = set(model.model_fields) - cols - ({"component_id"} if table == "bom_line" else set())
        assert not missing, f"{table}: 계약에만 있는 필드 {missing}"
