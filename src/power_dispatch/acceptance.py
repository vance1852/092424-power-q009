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
    service.submit_nomination("dispatch", {"nomination_id": "nom-001", "route_id": "pipe-a-b", "shipper_id": "refinery-east", "service_date": "2026-09-25", "requested_mwh": "80000", "priority": 10, "idempotency_key": "nom-key-001"})
    allocation = service.allocate("dispatch", "pipe-a-b", "2026-09-25")
    transfer = service.dispatch_transfer("dispatch", "transfer-001", "nom-001", "lot-001", 2)
    service.create_scenario("plan", {"scenario_id": "pipeline-restart", "name": "关键机组检修恢复与需求回落", "market_index_drop_percent": "9", "route_capacity_changes": {"pipe-a-b": "20"}, "demand_changes": {"field-a:crude": "-5"}})
    service.approve_scenario("risk", "pipeline-restart", 1)
    scenario = service.run_scenario("plan", "pipeline-restart", "2026-09-23")

    service.create_generation_unit("plan", {"unit_id": "gu-1", "name": "一号机组", "facility_id": "field-a", "fuel_product": "crude", "min_output_mwh": "20", "max_output_mwh": "100", "ramp_up_mwh": "60", "ramp_down_mwh": "60", "startup_cost_cny": "500", "marginal_cost_cny": "30", "fuel_factor": "2.5", "initial_output_mwh": "0"})
    service.create_generation_unit("plan", {"unit_id": "gu-2", "name": "二号机组", "facility_id": "field-a", "fuel_product": "crude", "min_output_mwh": "30", "max_output_mwh": "150", "ramp_up_mwh": "80", "ramp_down_mwh": "80", "startup_cost_cny": "800", "marginal_cost_cny": "45", "fuel_factor": "2", "initial_output_mwh": "0"})
    service.announce_maintenance("risk", {"unit_id": "gu-2", "trade_date": "2026-09-25", "start_period": 1, "end_period": 4, "reason": "例行检修"})
    service.record_price_curve("plan", {"price_version_id": "curve-20260925", "trade_date": "2026-09-25", "source_revision": "day-ahead-1", "price_points": [{"period": p, "price_cny": "60"} for p in range(1, 25)]})
    demand_curve = {1: 60, 2: 60, 3: 60, 4: 60, 5: 120, 6: 150, 7: 180, 8: 200, 9: 200, 10: 200, 11: 150, 12: 120, 13: 100, 14: 100, 15: 100, 16: 120, 17: 150, 18: 180, 19: 200, 20: 200, 21: 180, 22: 150, 23: 120, 24: 90}
    plan_payload = {"plan_id": "plan-20260925", "trade_date": "2026-09-25", "price_version_id": "curve-20260925", "reserve_percent": "10", "demand_points": [{"period": p, "demand_mwh": str(demand_curve[p])} for p in range(1, 25)]}
    draft = service.compute_plan("plan", plan_payload)
    replay = service.compute_plan("plan", plan_payload)
    approved_plan = service.approve_plan("dispatch", "plan-20260925", 1)
    actual_points = [
        {"period": period["period"], "unit_id": unit["unit_id"], "output_mwh": unit["output_mwh"]}
        for period in draft["periods"] for unit in period["units"]
    ]
    service.report_actual("dispatch", {"plan_id": "plan-20260925", "points": actual_points})
    deviation = service.plan_deviation("audit", "plan-20260925")
    result = {"status": "ok", "price": service.price_summary("PEAK_VALLEY"), "allocation_id": allocation["allocation_id"], "transfer": transfer, "scenario_run_id": scenario["run_id"], "plan": {"plan_id": draft["plan_id"], "state": approved_plan["state"], "replayed": replay["replayed"], "periods": len(draft["periods"]), "fuel_reserved_mwh": approved_plan["fuel_reservation"]["total_reserved_mwh"], "max_abs_deviation_mwh": deviation["max_abs_deviation_mwh"]}, "audit": service.audit_chain("audit"), "workspace": workspace.name}
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
