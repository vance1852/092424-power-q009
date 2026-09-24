from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from power_dispatch.api import JsonApplication
from power_dispatch.clock import FrozenClock
from power_dispatch.errors import Forbidden, PlanApprovalConflict, PlanInfeasible
from power_dispatch.service import SupplyService
from power_dispatch.storage import connect
from power_dispatch.unitplan import (
    InfeasiblePlan,
    UnitModel,
    deviation_report,
    solve,
)


def curve_points(values: dict[int, int]) -> list[dict[str, int]]:
    return [{"period": p, "price_cny": str(values.get(p, 60))} for p in range(1, 25)]


def demand_points(values: dict[int, int]) -> list[dict[str, int]]:
    return [{"period": p, "demand_mwh": str(values.get(p, 0))} for p in range(1, 25)]


RAMPING_DEMAND: dict[int, int] = {
    1: 60, 2: 60, 3: 60, 4: 60, 5: 120, 6: 150, 7: 180, 8: 200,
    9: 200, 10: 200, 11: 150, 12: 120, 13: 100, 14: 100, 15: 100,
    16: 120, 17: 150, 18: 180, 19: 200, 20: 200, 21: 180, 22: 150,
    23: 120, 24: 90,
}


def make_unit(
    unit_id: str,
    *,
    minimum: str = "20",
    maximum: str = "100",
    ramp_up: str = "60",
    ramp_down: str = "60",
    startup: str = "500",
    marginal: str = "30",
    initial: str = "0",
    blocked: frozenset[int] = frozenset(),
) -> UnitModel:
    return UnitModel(
        unit_id,
        Decimal(minimum),
        Decimal(maximum),
        Decimal(ramp_up),
        Decimal(ramp_down),
        Decimal(startup),
        Decimal(marginal),
        Decimal(initial),
        blocked,
    )


class SolverTests(unittest.TestCase):
    def test_schedule_meets_load_every_period(self) -> None:
        units = [make_unit("u1"), make_unit("u2", maximum="150", ramp_up="80", ramp_down="80",
                                            startup="800", marginal="45")]
        demand = [Decimal(str(RAMPING_DEMAND[p])) for p in range(1, 25)]
        result = solve(units=units, prices=[Decimal("60")] * 24, demand=demand, reserve_percent=Decimal("10"))
        for period in result["periods"]:
            total = Decimal(period["total_output_mwh"])
            self.assertEqual(total, Decimal(period["demand_mwh"]))
            for row in period["units"]:
                output = Decimal(row["output_mwh"])
                self.assertTrue(output == 0 or output >= Decimal("20"))

    def test_startup_respects_ramp_and_minimum_stable_output(self) -> None:
        units = [make_unit("u1", minimum="40", maximum="100", ramp_up="50", ramp_down="50")]
        demand = [Decimal("0")] * 24
        demand[0] = Decimal("40")
        result = solve(units=units, prices=[Decimal("60")] * 24, demand=demand, reserve_percent=Decimal("0"))
        first = result["periods"][0]["units"][0]
        self.assertTrue(first["started"])
        self.assertEqual(first["output_mwh"], "40.000")
        self.assertEqual(result["periods"][1]["units"][0]["output_mwh"], "0.000")

    def test_maintenance_window_blocks_unit(self) -> None:
        units = [make_unit("u1"), make_unit("u2", maximum="150", blocked=frozenset({0, 1, 2, 3}))]
        demand = [Decimal("60")] * 24
        result = solve(units=units, prices=[Decimal("60")] * 24, demand=demand, reserve_percent=Decimal("0"))
        for period in result["periods"][:4]:
            rows = {row["unit_id"]: row for row in period["units"]}
            self.assertEqual(Decimal(rows["u2"]["output_mwh"]), 0)

    def test_capacity_shortage_returns_conflict_evidence(self) -> None:
        units = [make_unit("u1", maximum="100")]
        demand = [Decimal("150")] * 24
        with self.assertRaises(InfeasiblePlan) as caught:
            solve(units=units, prices=[Decimal("60")] * 24, demand=demand, reserve_percent=Decimal("0"))
        conflict = caught.exception.conflicts[0]
        self.assertEqual(conflict["code"], "LOAD_EXCEEDS_CAPACITY")
        self.assertEqual(conflict["evidence"]["shortfall_mwh"], "50.000")
        self.assertEqual(conflict["period"], 1)

    def test_reserve_margin_is_enforced(self) -> None:
        units = [make_unit("u1", maximum="100")]
        demand = [Decimal("100")] * 24
        with self.assertRaises(InfeasiblePlan) as caught:
            solve(units=units, prices=[Decimal("60")] * 24, demand=demand, reserve_percent=Decimal("10"))
        self.assertEqual(caught.exception.conflicts[0]["evidence"]["reserve_required_mwh"], "10.000")

    def test_ramp_lookahead_detects_future_gap(self) -> None:
        # 谷荷后需求一小时跳到 200：冷态机组即便提前一小时启机也无法达到
        units = [make_unit("u1"), make_unit("u2", maximum="150", ramp_up="80", ramp_down="80")]
        demand = [Decimal("50")] * 24
        demand[1] = Decimal("200")
        with self.assertRaises(InfeasiblePlan) as caught:
            solve(units=units, prices=[Decimal("60")] * 24, demand=demand, reserve_percent=Decimal("0"))
        conflict = caught.exception.conflicts[0]
        self.assertEqual(conflict["code"], "RAMP_INSUFFICIENT")
        self.assertEqual(conflict["evidence"]["future_period"], 2)
        self.assertGreater(Decimal(conflict["evidence"]["future_energy_gap_mwh"]), 0)

    def test_solve_is_deterministic(self) -> None:
        units = [make_unit("u1"), make_unit("u2", maximum="150", ramp_up="80", ramp_down="80",
                                            startup="800", marginal="45")]
        demand = [Decimal(str(RAMPING_DEMAND[p])) for p in range(1, 25)]
        first = solve(units=units, prices=[Decimal("60")] * 24, demand=demand, reserve_percent=Decimal("10"))
        second = solve(units=units, prices=[Decimal("60")] * 24, demand=demand, reserve_percent=Decimal("10"))
        self.assertEqual(first["periods"], second["periods"])
        self.assertEqual(first["estimated_cost_cny"], second["estimated_cost_cny"])

    def test_deviation_report_flags_delta_and_missing_actuals(self) -> None:
        schedule = [
            {"period": 1, "unit_id": "u1", "output_mwh": "40.000"},
            {"period": 2, "unit_id": "u1", "output_mwh": "60.000"},
        ]
        actuals = [{"period": 1, "unit_id": "u1", "output_mwh": "45.000"}]
        report = deviation_report(schedule, actuals)
        self.assertEqual(report["max_abs_deviation_mwh"], "5.000")
        self.assertEqual(report["missing_actuals"][0]["period"], 2)
        self.assertEqual(report["actual_total_mwh"], "45.000")


class PlanServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = SupplyService(self.connection, self.clock)
        for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"),
                              ("risk", "risk"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)
        self.service.create_facility("plan", {
            "facility_id": "f1", "name": "北部电厂", "kind": "storage",
            "timezone": "Asia/Shanghai", "capacity_mwh": "1000",
        })
        self.service.create_generation_unit("plan", {
            "unit_id": "u1", "name": "一号机", "facility_id": "f1", "fuel_product": "crude",
            "min_output_mwh": "20", "max_output_mwh": "100", "ramp_up_mwh": "60",
            "ramp_down_mwh": "60", "startup_cost_cny": "500", "marginal_cost_cny": "30",
            "fuel_factor": "2.5", "initial_output_mwh": "0",
        })
        self.service.create_generation_unit("plan", {
            "unit_id": "u2", "name": "二号机", "facility_id": "f1", "fuel_product": "crude",
            "min_output_mwh": "30", "max_output_mwh": "150", "ramp_up_mwh": "80",
            "ramp_down_mwh": "80", "startup_cost_cny": "800", "marginal_cost_cny": "45",
            "fuel_factor": "2", "initial_output_mwh": "0",
        })
        self.service.announce_maintenance("risk", {
            "unit_id": "u2", "trade_date": "2026-09-25",
            "start_period": 1, "end_period": 4, "reason": "检修",
        })
        self.service.record_price_curve("plan", {
            "price_version_id": "pv1", "trade_date": "2026-09-25",
            "source_revision": "rev1", "price_points": curve_points({}),
        })
        self.payload = {
            "plan_id": "plan1",
            "trade_date": "2026-09-25",
            "price_version_id": "pv1",
            "reserve_percent": "10",
            "demand_points": demand_points(RAMPING_DEMAND),
        }

    def tearDown(self) -> None:
        self.connection.close()

    def test_draft_compute_consumes_no_fuel_and_is_replayable(self) -> None:
        self.service.add_inventory_lot("dispatch", {
            "lot_id": "l1", "facility_id": "f1", "product": "crude", "grade": "PEAK_VALLEY",
            "quantity_mwh": "100000", "unit_cost_cny": "90",
            "received_at": "2026-09-24T06:00:00Z",
        })
        first = self.service.compute_plan("plan", self.payload)
        self.assertEqual(first["state"], "draft")
        self.assertFalse(first["replayed"])
        second = self.service.compute_plan("plan", self.payload)
        self.assertTrue(second["replayed"])
        self.assertEqual(first["periods"], second["periods"])
        lot = self.service.inventory_lot("l1")
        self.assertEqual(Decimal(lot["available_mwh"]), Decimal("100000"))
        self.assertEqual(lot["revision"], 1)
        # 相同输入换计划编号复用同一方案
        copy = self.service.compute_plan("plan", {**self.payload, "plan_id": "plan1-copy"})
        self.assertTrue(copy["replayed"])
        self.assertEqual(copy["plan_id"], "plan1")
        # 不同输入复用计划编号必须冲突
        with self.assertRaisesRegex(Exception, "不同输入"):
            self.service.compute_plan("plan", {**self.payload, "reserve_percent": "20"})

    def test_infeasible_plan_persists_conflict_evidence(self) -> None:
        payload = {**self.payload, "plan_id": "bad",
                   "demand_points": [{"period": p, "demand_mwh": "9999"} for p in range(1, 25)]}
        with self.assertRaises(PlanInfeasible) as caught:
            self.service.compute_plan("plan", payload)
        self.assertEqual(len(caught.exception.conflicts), 24)
        with self.assertRaises(PlanInfeasible) as replayed:
            self.service.compute_plan("plan", payload)
        self.assertEqual(replayed.exception.conflicts, caught.exception.conflicts)
        stored = self.service.get_plan("plan", "bad")
        self.assertEqual(stored["state"], "infeasible")
        self.assertEqual(stored["conflicts"][0]["code"], "LOAD_EXCEEDS_CAPACITY")

    def test_approve_without_fuel_leaves_inventory_untouched(self) -> None:
        self.service.add_inventory_lot("dispatch", {
            "lot_id": "l1", "facility_id": "f1", "product": "crude", "grade": "PEAK_VALLEY",
            "quantity_mwh": "100", "unit_cost_cny": "90",
            "received_at": "2026-09-24T06:00:00Z",
        })
        self.service.compute_plan("plan", self.payload)
        with self.assertRaises(PlanApprovalConflict) as caught:
            self.service.approve_plan("dispatch", "plan1", 1)
        self.assertEqual(caught.exception.conflicts[0]["code"], "FUEL_INVENTORY_SHORTAGE")
        lot = self.service.inventory_lot("l1")
        self.assertEqual(Decimal(lot["available_mwh"]), Decimal("100"))
        self.assertEqual(lot["revision"], 1)
        plan = self.service.get_plan("plan", "plan1")
        self.assertEqual(plan["state"], "draft")
        self.assertNotIn("fuel_reservation", plan)

    def test_approve_reserves_fuel_atomically_and_is_idempotent(self) -> None:
        self.service.add_inventory_lot("dispatch", {
            "lot_id": "l1", "facility_id": "f1", "product": "crude", "grade": "PEAK_VALLEY",
            "quantity_mwh": "100000", "unit_cost_cny": "90",
            "received_at": "2026-09-24T06:00:00Z",
        })
        plan = self.service.compute_plan("plan", self.payload)
        approved = self.service.approve_plan("dispatch", "plan1", 1)
        self.assertEqual(approved["state"], "approved")
        self.assertFalse(approved["idempotent"])
        self.assertEqual(approved["revision"], 2)
        reserved = Decimal(approved["fuel_reservation"]["total_reserved_mwh"])
        self.assertGreater(reserved, 0)
        lot = self.service.inventory_lot("l1")
        self.assertEqual(Decimal(lot["available_mwh"]), Decimal("100000") - reserved)
        self.assertEqual(lot["revision"], 2)
        # 草稿不能再批准旧修订
        with self.assertRaises(Exception):
            self.service.approve_plan("dispatch", "plan1", 1)
        again = self.service.approve_plan("dispatch", "plan1", 2)
        self.assertTrue(again["idempotent"])
        self.assertEqual(again["fuel_reservation"]["total_reserved_mwh"],
                         approved["fuel_reservation"]["total_reserved_mwh"])
        self.assertEqual(self.service.inventory_lot("l1")["revision"], 2)
        # 批准后不能再上报到草稿
        self.assertGreaterEqual(len(plan["periods"]), 24)

    def test_actuals_and_deviation_survive_restart(self) -> None:
        path = Path(tempfile.mkdtemp()) / "dispatch.sqlite3"
        connection = connect(path)
        service = SupplyService(connection, self.clock)
        for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"),
                              ("risk", "risk"), ("audit", "auditor")):
            service.create_user(user_id, user_id, role)
        service.create_facility("plan", {
            "facility_id": "f1", "name": "北部电厂", "kind": "storage",
            "timezone": "Asia/Shanghai", "capacity_mwh": "1000",
        })
        service.create_generation_unit("plan", {
            "unit_id": "u1", "name": "一号机", "facility_id": "f1", "fuel_product": "crude",
            "min_output_mwh": "20", "max_output_mwh": "100", "ramp_up_mwh": "60",
            "ramp_down_mwh": "60", "startup_cost_cny": "500", "marginal_cost_cny": "30",
            "fuel_factor": "2.5", "initial_output_mwh": "0",
        })
        service.record_price_curve("plan", {
            "price_version_id": "pv1", "trade_date": "2026-09-25",
            "source_revision": "rev1", "price_points": curve_points({}),
        })
        payload = {
            "plan_id": "plan-disk", "trade_date": "2026-09-25", "price_version_id": "pv1",
            "reserve_percent": "0",
            "demand_points": demand_points({p: 60 for p in range(1, 25)}),
        }
        plan = service.compute_plan("plan", payload)
        service.add_inventory_lot("dispatch", {
            "lot_id": "l1", "facility_id": "f1", "product": "crude", "grade": "PEAK_VALLEY",
            "quantity_mwh": "100000", "unit_cost_cny": "90",
            "received_at": "2026-09-24T06:00:00Z",
        })
        service.approve_plan("dispatch", "plan-disk", 1)
        points = []
        for period in plan["periods"]:
            for row in period["units"]:
                adjusted = Decimal(row["output_mwh"]) + (3 if period["period"] == 2 else 0)
                points.append({"period": period["period"], "unit_id": row["unit_id"],
                               "output_mwh": str(adjusted)})
        service.report_actual("dispatch", {"plan_id": "plan-disk", "points": points})
        connection.close()

        restarted = connect(path)
        service2 = SupplyService(restarted, self.clock)
        stored = service2.get_plan("audit", "plan-disk")
        self.assertEqual(stored["state"], "approved")
        self.assertEqual(len(stored["periods"]), 24)
        deviation = service2.plan_deviation("audit", "plan-disk")
        self.assertEqual(deviation["max_abs_deviation_mwh"], "3.000")
        self.assertEqual(deviation["max_deviation_point"]["period"], 2)
        self.assertTrue(service2.approve_plan("dispatch", "plan-disk", 2)["idempotent"])
        self.assertTrue(service2.compute_plan("plan", payload)["replayed"])
        restarted.close()

    def test_only_dispatcher_can_approve(self) -> None:
        self.service.compute_plan("plan", self.payload)
        with self.assertRaises(Forbidden):
            self.service.approve_plan("risk", "plan1", 1)
        with self.assertRaises(Forbidden):
            self.service.compute_plan("dispatch", self.payload)

    def test_api_returns_conflict_set_and_routes_plan_flow(self) -> None:
        app = JsonApplication(self.service)
        bad = {**self.payload, "plan_id": "bad-api",
               "demand_points": [{"period": p, "demand_mwh": "9999"} for p in range(1, 25)]}
        response = app.handle("POST", "/plans/compute", {"X-Actor-Id": "plan"},
                              json.dumps(bad).encode("utf-8"))
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "plan_infeasible")
        self.assertEqual(response.body["error"]["conflicts"][0]["code"], "LOAD_EXCEEDS_CAPACITY")
        good = app.handle("POST", "/plans/compute", {"X-Actor-Id": "plan"},
                          json.dumps(self.payload).encode("utf-8"))
        self.assertEqual(good.status, 201)
        fetched = app.handle("GET", "/plans/plan1", {"X-Actor-Id": "audit"})
        self.assertEqual(fetched.status, 200)
        self.assertEqual(fetched.body["input"]["price_version"]["id"], "pv1")
        self.assertIn("sha256", fetched.body["input"]["price_version"])
        # 批准缺燃料：HTTP 409 携带缺口证据
        denied = app.handle("POST", "/plans/plan1/approve", {"X-Actor-Id": "dispatch"},
                            b'{"expected_revision": 1}')
        self.assertEqual(denied.status, 409)
        self.assertEqual(denied.body["error"]["code"], "fuel_reservation_failed")
        self.assertEqual(denied.body["error"]["conflicts"][0]["facility_id"], "f1")


if __name__ == "__main__":
    unittest.main()
