"""贯通电价、送出线路、燃料库存、提名和情景分析的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import SupplyService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = SupplyService(connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
    for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
        service.create_user(user_id, user_id, role)
    for index, close in enumerate(("108", "105", "102", "100", "98", "96"), start=18):
        service.record_quote("plan", {"market_index": "PEAK_VALLEY", "trade_date": f"2026-09-{index}", "close_cny": close, "source_revision": f"rev-{index}", "observed_at": f"2026-09-{index}T21:00:00Z"})
    service.create_facility("plan", {"facility_id": "field-a", "name": "北部电厂", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_mwh": "500000"})
    service.create_facility("plan", {"facility_id": "terminal-b", "name": "沿海终端", "kind": "terminal", "timezone": "Asia/Shanghai", "capacity_mwh": "800000"})
    service.create_route("plan", {"route_id": "pipe-a-b", "origin_id": "field-a", "destination_id": "terminal-b", "product": "crude", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})
    service.add_inventory_lot("dispatch", {"lot_id": "lot-001", "facility_id": "field-a", "product": "crude", "grade": "PEAK_VALLEY", "quantity_mwh": "150000", "unit_cost_cny": "91.25", "received_at": "2026-09-24T06:00:00Z"})
    service.add_inventory_lot("dispatch", {"lot_id": "lot-gas-001", "facility_id": "field-a", "product": "diesel", "grade": "PEAK_VALLEY", "quantity_mwh": "60000", "unit_cost_cny": "18.50", "received_at": "2026-09-24T06:30:00Z"})
    service.submit_nomination("dispatch", {"nomination_id": "nom-001", "route_id": "pipe-a-b", "shipper_id": "refinery-east", "service_date": "2026-09-25", "requested_mwh": "80000", "priority": 10, "idempotency_key": "nom-key-001"})
    allocation = service.allocate("dispatch", "pipe-a-b", "2026-09-25")
    transfer = service.dispatch_transfer("dispatch", "transfer-001", "nom-001", "lot-001", 2)
    service.create_scenario("plan", {"scenario_id": "pipeline-restart", "name": "关键机组检修恢复与需求回落", "market_index_drop_percent": "9", "route_capacity_changes": {"pipe-a-b": "20"}, "demand_changes": {"field-a:crude": "-5"}})
    service.approve_scenario("risk", "pipeline-restart", 1)
    scenario = service.run_scenario("plan", "pipeline-restart", "2026-09-23")
    # 次日峰谷机组出力计划：登记 24 点价格曲线、两台机组参数与检修窗口。
    curve = service.record_price_curve("plan", {"market_index": "PEAK_VALLEY", "trade_date": "2026-09-25", "source_revision": "curve-20260925-1", "prices": [str(price) for price in (36, 34, 32, 31, 32, 36, 48, 72, 96, 118, 126, 130, 128, 124, 122, 120, 116, 108, 92, 74, 60, 48, 42, 38)]})
    service.register_unit("plan", {"unit_id": "unit-base", "facility_id": "field-a", "product": "diesel", "name": "基底煤机", "min_stable_mw": "60", "max_capacity_mw": "220", "ramp_mw_per_hour": "70", "heat_rate": "1.850", "startup_cost_cny": "800"})
    service.register_unit("plan", {"unit_id": "unit-peak", "facility_id": "field-a", "product": "diesel", "name": "调峰燃机", "min_stable_mw": "30", "max_capacity_mw": "120", "ramp_mw_per_hour": "120", "heat_rate": "2.650", "startup_cost_cny": "300"})
    service.declare_maintenance("plan", "unit-peak", "2026-09-25", 0, 5, "凌晨例行检修")
    plan = service.compute_plan("plan", {"plan_id": "plan-20260925", "trade_date": "2026-09-25", "facility_id": "field-a", "product": "diesel", "quote_id": curve["curve_id"], "reserve_percent": "10", "prices": [str(price) for price in (36, 34, 32, 31, 32, 36, 48, 72, 96, 118, 126, 130, 128, 124, 122, 120, 116, 108, 92, 74, 60, 48, 42, 38)], "load_mw": ["100", "95", "90", "88", "90", "95", "110", "140", "175", "200", "210", "215", "212", "208", "205", "202", "198", "188", "170", "145", "125", "110", "102", "98"], "initial_output_mw": {"unit-base": "100"}})
    approved_plan = service.approve_plan("dispatch", "plan-20260925", plan["revision"]) if plan["feasible"] else None
    result = {"status": "ok", "price": service.price_summary("PEAK_VALLEY"), "allocation_id": allocation["allocation_id"], "transfer": transfer, "scenario_run_id": scenario["run_id"], "plan": None if approved_plan is None else {"plan_id": approved_plan["plan_id"], "revision": approved_plan["revision"], "state": approved_plan["state"], "curve_id": approved_plan["curve_id"], "fuel_demand": approved_plan["fuel_demand"], "total_export_mwh": approved_plan["total_export_mwh"], "gross_margin_cny": approved_plan["gross_margin_cny"], "peak_hour": next(hour for hour in approved_plan["hours"] if hour["hour"] == 11)}, "audit": service.audit_chain("audit"), "workspace": workspace.name}
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行电厂调度服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
