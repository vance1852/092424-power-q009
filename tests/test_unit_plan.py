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
from power_dispatch.errors import Conflict, Forbidden, InvalidState
from power_dispatch.service import SupplyService
from power_dispatch.storage import connect
from power_dispatch.unit_schedule import UnitParameters, solve_schedule

PEAK_PRICES = [Decimal(p) for p in [40] * 7 + [120] * 13 + [40] * 4]


def unit(unit_id: str, **overrides: object) -> UnitParameters:
    params = {
        "unit_id": unit_id,
        "name": f"机组-{unit_id}",
        "min_stable_mw": "50",
        "max_capacity_mw": "200",
        "ramp_mw_per_hour": "80",
        "heat_rate": "2.0",
        "startup_cost_cny": "500",
    }
    params.update(overrides)
    return UnitParameters.from_dict(params)


class SolverTests(unittest.TestCase):
    def solve(self, **overrides):
        kwargs = dict(
            units=[unit("g1"), unit("g2", min_stable_mw="30", max_capacity_mw="100",
                                    ramp_mw_per_hour="100", heat_rate="3.0", startup_cost_cny="200")],
            blocked_hours={},
            prices=PEAK_PRICES,
            load_mw=[Decimal("100")] * 24,
            reserve_percent=Decimal("10"),
            fuel_unit_cost=Decimal("20"),
            initial_output={"g1": Decimal("100")},
        )
        kwargs.update(overrides)
        return solve_schedule(**kwargs)

    def test_valley_serves_load_peak_exports_with_startup_sequence(self) -> None:
        result = self.solve()
        self.assertTrue(result["feasible"])
        valley = result["hours"][3]
        self.assertEqual(valley["committed_unit_ids"], ["g1"])
        self.assertEqual(valley["generation_mw"], "100.000")
        self.assertEqual(valley["export_mw"], "0.000")
        peak = result["hours"][10]
        self.assertEqual(peak["committed_unit_ids"], ["g1", "g2"])
        self.assertGreater(Decimal(peak["export_mw"]), Decimal("100"))
        self.assertGreater(Decimal(peak["revenue_cny"]), Decimal("0"))
        # 全天逐时校验最小稳定出力、爬坡与备用。
        previous = {"g1": Decimal("100")}
        units = {u.unit_id: u for u in [unit("g1"), unit("g2", min_stable_mw="30", max_capacity_mw="100",
                                                   ramp_mw_per_hour="100", heat_rate="3.0", startup_cost_cny="200")]}
        for hour in result["hours"]:
            for uid, value_text in hour["outputs_mw"].items():
                value = Decimal(value_text)
                prev = previous.get(uid, Decimal("0"))
                self.assertLessEqual(abs(value - prev), units[uid].ramp_mw_per_hour + Decimal("0.01"))
                self.assertGreaterEqual(value, units[uid].min_stable_mw - Decimal("0.01"))
            self.assertGreaterEqual(
                Decimal(hour["generation_mw"]), Decimal(hour["load_mw"]) - Decimal("0.01"))
            previous = {uid: Decimal(value_text) for uid, value_text in hour["outputs_mw"].items()}

    def test_solve_is_deterministic(self) -> None:
        first = json.dumps(self.solve(), sort_keys=True, ensure_ascii=False)
        second = json.dumps(self.solve(), sort_keys=True, ensure_ascii=False)
        self.assertEqual(first, second)

    def test_load_capacity_conflict_includes_evidence(self) -> None:
        blocked = {"g2": frozenset(range(24))}
        result = self.solve(blocked_hours=blocked, load_mw=[Decimal("190")] * 24)
        self.assertFalse(result["feasible"])
        conflict = result["conflicts"][0]
        self.assertEqual(conflict["code"], "reserve_capacity_shortfall")
        self.assertEqual(conflict["hour"], 0)
        self.assertEqual(conflict["shortfall_mw"], "9.000")
        self.assertEqual(conflict["available_units"], ["g1"])

    def test_ramp_conflict_when_load_jumps_beyond_ramp(self) -> None:
        slow = unit("g4", max_capacity_mw="300", ramp_mw_per_hour="40")
        result = solve_schedule(
            units=[slow],
            blocked_hours={},
            prices=[Decimal("40")] * 24,
            load_mw=[Decimal(x) for x in [100] * 12 + [200] * 12],
            reserve_percent=Decimal("0"),
            fuel_unit_cost=Decimal("20"),
            initial_output={"g4": Decimal("100")},
        )
        self.assertFalse(result["feasible"])
        self.assertEqual(result["conflicts"][0]["code"], "ramp_or_min_stable_infeasible")
        self.assertEqual(result["conflicts"][0]["hour"], 12)

    def test_maintenance_blocks_initially_online_unit(self) -> None:
        result = self.solve(
            blocked_hours={"g1": frozenset([0])},
            initial_output={"g1": Decimal("100")},
        )
        self.assertFalse(result["feasible"])
        self.assertEqual(result["conflicts"][0]["code"], "initial_state_under_maintenance")

    def test_min_stable_forces_export_and_costs_it(self) -> None:
        only = unit("g3", min_stable_mw="150", max_capacity_mw="300", ramp_mw_per_hour="300",
                    heat_rate="2.0", startup_cost_cny="0")
        result = solve_schedule(
            units=[only],
            blocked_hours={},
            prices=[Decimal("40")] * 24,
            load_mw=[Decimal("100")] * 24,
            reserve_percent=Decimal("0"),
            fuel_unit_cost=Decimal("20"),
        )
        self.assertTrue(result["feasible"])
        self.assertEqual(result["hours"][0]["generation_mw"], "150.000")
        self.assertEqual(result["hours"][0]["export_mw"], "50.000")


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
            "facility_id": "plant-1", "name": "一号电厂", "kind": "storage",
            "timezone": "Asia/Shanghai", "capacity_mwh": "1000",
        })
        self.service.add_inventory_lot("dispatch", {
            "lot_id": "fuel-1", "facility_id": "plant-1", "product": "diesel",
            "grade": "PEAK_VALLEY", "quantity_mwh": "50000", "unit_cost_cny": "20",
            "received_at": "2026-09-24T06:00:00Z",
        })
        self.prices = [str(p) for p in [40] * 7 + [120] * 13 + [40] * 4]
        self.curve = self.service.record_price_curve("plan", {
            "market_index": "PEAK_VALLEY", "trade_date": "2026-09-25",
            "source_revision": "cv-1", "prices": self.prices,
        })
        self.service.register_unit("plan", {
            "unit_id": "g1", "facility_id": "plant-1", "product": "diesel", "name": "煤机",
            "min_stable_mw": "50", "max_capacity_mw": "200", "ramp_mw_per_hour": "80",
            "heat_rate": "2.0", "startup_cost_cny": "500",
        })
        self.service.register_unit("plan", {
            "unit_id": "g2", "facility_id": "plant-1", "product": "diesel", "name": "燃机",
            "min_stable_mw": "30", "max_capacity_mw": "100", "ramp_mw_per_hour": "100",
            "heat_rate": "3.0", "startup_cost_cny": "200",
        })
        self.payload = {
            "plan_id": "plan-1", "trade_date": "2026-09-25", "facility_id": "plant-1",
            "product": "diesel", "quote_id": self.curve["curve_id"], "reserve_percent": "10",
            "prices": self.prices, "load_mw": ["100"] * 24,
            "initial_output_mw": {"g1": "100"},
        }

    def tearDown(self) -> None:
        self.connection.close()

    def test_curve_revision_is_replayable_and_not_overwritten(self) -> None:
        with self.assertRaises(Conflict):
            self.service.record_price_curve("plan", {
                "market_index": "PEAK_VALLEY", "trade_date": "2026-09-25",
                "source_revision": "cv-1", "prices": self.prices,
            })
        curve = self.service.price_curve("audit", "PEAK_VALLEY", "2026-09-25")
        self.assertEqual(curve["curve_id"], self.curve["curve_id"])
        self.assertEqual(curve["prices"], self.prices)

    def test_unit_parameter_change_creates_version(self) -> None:
        first = self.service.register_unit("plan", {
            "unit_id": "g1", "facility_id": "plant-1", "product": "diesel", "name": "煤机",
            "min_stable_mw": "50", "max_capacity_mw": "200", "ramp_mw_per_hour": "80",
            "heat_rate": "2.0", "startup_cost_cny": "500",
        })
        self.assertTrue(first["unchanged"])
        changed = self.service.register_unit("plan", {
            "unit_id": "g1", "facility_id": "plant-1", "product": "diesel", "name": "煤机",
            "min_stable_mw": "50", "max_capacity_mw": "200", "ramp_mw_per_hour": "90",
            "heat_rate": "2.0", "startup_cost_cny": "500",
        })
        self.assertFalse(changed["unchanged"])
        self.assertEqual(changed["revision"], 2)
        rows = self.connection.execute(
            "SELECT revision,state FROM generation_unit_revisions WHERE unit_id='g1' ORDER BY revision"
        ).fetchall()
        self.assertEqual([(row["revision"], row["state"]) for row in rows], [(1, "retired"), (2, "active")])

    def test_draft_does_not_consume_fuel_and_recompute_is_idempotent(self) -> None:
        first = self.service.compute_plan("plan", self.payload)
        self.assertTrue(first["feasible"])
        self.assertEqual(first["state"], "draft")
        self.assertEqual(self.service.inventory_lot("fuel-1")["available_mwh"], "50000")
        second = self.service.compute_plan("plan", self.payload)
        self.assertTrue(second["replayed"])
        self.assertEqual(second["revision"], first["revision"])
        self.assertEqual(second["input_sha256"], first["input_sha256"])

    def test_changed_input_supersedes_draft_and_preserves_history(self) -> None:
        self.service.compute_plan("plan", self.payload)
        changed = self.service.compute_plan("plan", dict(self.payload, load_mw=["110"] * 24))
        self.assertEqual(changed["revision"], 2)
        self.assertEqual(self.service.get_plan("plan", "plan-1", 1)["state"], "superseded")
        self.assertEqual(self.service.get_plan("plan", "plan-1", 2)["state"], "draft")

    def test_infeasible_plan_persists_conflict_set(self) -> None:
        self.service.declare_maintenance("plan", "g2", "2026-09-25", 0, 24, "全天检修")
        result = self.service.compute_plan("plan", dict(
            self.payload, load_mw=["190"] * 24,
        ))
        self.assertFalse(result["feasible"])
        self.assertEqual(result["conflicts"][0]["code"], "reserve_capacity_shortfall")
        stored = self.service.get_plan("plan", "plan-1")
        self.assertFalse(stored["feasible"])
        self.assertGreaterEqual(len(stored["conflicts"]), 1)
        with self.assertRaises(InvalidState):
            self.service.approve_plan("dispatch", "plan-1", 1)

    def test_approval_reserves_fuel_atomically_and_is_idempotent(self) -> None:
        self.service.compute_plan("plan", self.payload)
        approved = self.service.approve_plan("dispatch", "plan-1", 1)
        self.assertEqual(approved["state"], "approved")
        remaining = Decimal(self.service.inventory_lot("fuel-1")["available_mwh"])
        self.assertEqual(remaining, Decimal("50000") - Decimal(approved["fuel_demand"]))
        reservation = self.connection.execute(
            "SELECT * FROM plan_fuel_reservations WHERE plan_id='plan-1'"
        ).fetchone()
        self.assertEqual(reservation["reserved_mwh"], approved["fuel_demand"])
        again = self.service.approve_plan("dispatch", "plan-1", 1)
        self.assertTrue(again["replayed"])
        self.assertEqual(self.service.inventory_lot("fuel-1")["available_mwh"], format(remaining, "f"))
        with self.assertRaises(InvalidState):
            self.service.compute_plan("plan", self.payload)

    def test_approval_rejects_when_inventory_version_changed(self) -> None:
        self.service.compute_plan("plan", self.payload)
        self.service.add_inventory_lot("dispatch", {
            "lot_id": "fuel-2", "facility_id": "plant-1", "product": "diesel",
            "grade": "PEAK_VALLEY", "quantity_mwh": "100", "unit_cost_cny": "21",
            "received_at": "2026-09-24T07:00:00Z",
        })
        with self.assertRaises(Conflict):
            self.service.approve_plan("dispatch", "plan-1", 1)

    def test_approval_rejects_when_inventory_insufficient(self) -> None:
        self.service.compute_plan("plan", self.payload)
        self.connection.execute("UPDATE inventory_lots SET available_mwh='1' WHERE lot_id='fuel-1'")
        # 库存版本发生变化，必须先重算；重算后版本一致但总量不足。
        with self.assertRaises(Conflict):
            self.service.approve_plan("dispatch", "plan-1", 1)
        self.service.compute_plan("plan", self.payload)
        with self.assertRaises(Conflict):
            self.service.approve_plan("dispatch", "plan-1", 2)

    def test_actuals_report_deviation_and_are_idempotent(self) -> None:
        approved = self.service.approve_plan(
            "dispatch", "plan-1",
            self.service.compute_plan("plan", self.payload)["revision"],
        )
        actual = {
            uid: [hour["outputs_mw"].get(uid, "0.000") for hour in approved["hours"]]
            for uid in ("g1", "g2")
        }
        exact = self.service.record_actual("dispatch", "plan-1", {"outputs_mw": actual})
        self.assertEqual(exact["total_abs_deviation_mwh"], "0.000")
        self.assertTrue(self.service.record_actual(
            "dispatch", "plan-1", {"outputs_mw": actual})["replayed"])
        with self.assertRaises(Conflict):
            self.service.record_actual(
                "dispatch", "plan-1",
                {"outputs_mw": {uid: ["0"] * 24 for uid in ("g1", "g2")}},
            )

    def test_role_separation_for_plans(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.compute_plan("dispatch", self.payload)
        self.service.compute_plan("plan", self.payload)
        with self.assertRaises(Forbidden):
            self.service.approve_plan("plan", "plan-1", 1)
        with self.assertRaises(Forbidden):
            self.service.declare_maintenance("dispatch", "g2", "2026-09-25", 0, 4, "检修")


class PlanPersistenceTests(unittest.TestCase):
    def test_plan_and_deviation_survive_restart(self) -> None:
        directory = tempfile.mkdtemp()
        database = Path(directory) / "plan.sqlite3"
        connection = connect(database)
        service = SupplyService(connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
        for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("audit", "auditor")):
            service.create_user(user_id, user_id, role)
        service.create_facility("plan", {
            "facility_id": "plant-1", "name": "一号电厂", "kind": "storage",
            "timezone": "Asia/Shanghai", "capacity_mwh": "1000",
        })
        service.add_inventory_lot("dispatch", {
            "lot_id": "fuel-1", "facility_id": "plant-1", "product": "diesel",
            "grade": "PEAK_VALLEY", "quantity_mwh": "50000", "unit_cost_cny": "20",
            "received_at": "2026-09-24T06:00:00Z",
        })
        prices = [str(p) for p in [40] * 7 + [120] * 13 + [40] * 4]
        curve = service.record_price_curve("plan", {
            "market_index": "PEAK_VALLEY", "trade_date": "2026-09-25",
            "source_revision": "cv-1", "prices": prices,
        })
        service.register_unit("plan", {
            "unit_id": "g1", "facility_id": "plant-1", "product": "diesel", "name": "煤机",
            "min_stable_mw": "50", "max_capacity_mw": "200", "ramp_mw_per_hour": "80",
            "heat_rate": "2.0", "startup_cost_cny": "500",
        })
        payload = {
            "plan_id": "plan-9", "trade_date": "2026-09-25", "facility_id": "plant-1",
            "product": "diesel", "quote_id": curve["curve_id"], "reserve_percent": "0",
            "prices": prices, "load_mw": ["100"] * 24,
            "initial_output_mw": {"g1": "100"},
        }
        planned = service.compute_plan("plan", payload)
        service.approve_plan("dispatch", "plan-9", planned["revision"])
        connection.close()

        restarted = connect(database)
        service_after = SupplyService(restarted)
        fetched = service_after.get_plan("audit", "plan-9")
        self.assertEqual(fetched["state"], "approved")
        self.assertEqual(len(fetched["hours"]), 24)
        self.assertEqual(fetched["curve_id"], curve["curve_id"])
        self.assertIsNotNone(fetched["approved_inventory_version_sha256"])
        self.assertTrue(service_after.audit_chain("audit")["valid"])
        restarted.close()


class PlanApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.service = SupplyService(self.connection)
        self.app = JsonApplication(self.service)
        for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher")):
            self.service.create_user(user_id, user_id, role)
        self.service.create_facility("plan", {
            "facility_id": "plant-1", "name": "一号电厂", "kind": "storage",
            "timezone": "Asia/Shanghai", "capacity_mwh": "1000",
        })
        self.service.add_inventory_lot("dispatch", {
            "lot_id": "fuel-1", "facility_id": "plant-1", "product": "diesel",
            "grade": "PEAK_VALLEY", "quantity_mwh": "50000", "unit_cost_cny": "20",
            "received_at": "2026-09-24T06:00:00Z",
        })
        self.prices = [str(p) for p in [40] * 7 + [120] * 13 + [40] * 4]
        self.service.register_unit("plan", {
            "unit_id": "g1", "facility_id": "plant-1", "product": "diesel", "name": "煤机",
            "min_stable_mw": "50", "max_capacity_mw": "200", "ramp_mw_per_hour": "80",
            "heat_rate": "2.0", "startup_cost_cny": "500",
        })

    def tearDown(self) -> None:
        self.connection.close()

    def post(self, path: str, payload: dict[str, object], actor: str = "plan"):
        return self.app.handle(
            "POST", path, {"X-Actor-Id": actor},
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )

    def test_plan_compute_approve_actual_flow_over_http(self) -> None:
        curve = self.post("/price-curves", {
            "market_index": "PEAK_VALLEY", "trade_date": "2026-09-25",
            "source_revision": "cv-1", "prices": self.prices,
        })
        self.assertEqual(curve.status, 201)
        plan = self.post("/plans", {
            "plan_id": "plan-http", "trade_date": "2026-09-25", "facility_id": "plant-1",
            "product": "diesel", "quote_id": curve.body["curve_id"], "reserve_percent": "0",
            "prices": self.prices, "load_mw": ["100"] * 24,
            "initial_output_mw": {"g1": "100"},
        })
        self.assertEqual(plan.status, 201)
        self.assertTrue(plan.body["feasible"])
        approval = self.post("/plans/plan-http/approve", {"expected_revision": 1}, "dispatch")
        self.assertEqual(approval.status, 200)
        self.assertEqual(approval.body["state"], "approved")
        actual = {
            "g1": [hour["outputs_mw"].get("g1", "0.000") for hour in approval.body["hours"]],
        }
        recorded = self.post("/plans/plan-http/actuals", {"outputs_mw": actual}, "dispatch")
        self.assertEqual(recorded.status, 201)
        self.assertEqual(recorded.body["total_abs_deviation_mwh"], "0.000")
        fetched = self.app.handle(
            "GET", "/plans/plan-http", {"X-Actor-Id": "dispatch"},
        )
        self.assertEqual(fetched.status, 200)
        self.assertEqual(fetched.body["revision"], 1)

    def test_infeasible_plan_returns_conflict_evidence_over_http(self) -> None:
        curve = self.post("/price-curves", {
            "market_index": "PEAK_VALLEY", "trade_date": "2026-09-25",
            "source_revision": "cv-9", "prices": self.prices,
        })
        plan = self.post("/plans", {
            "plan_id": "plan-bad", "trade_date": "2026-09-25", "facility_id": "plant-1",
            "product": "diesel", "quote_id": curve.body["curve_id"], "reserve_percent": "0",
            "prices": self.prices, "load_mw": ["900"] * 24,
        })
        self.assertEqual(plan.status, 201)
        self.assertFalse(plan.body["feasible"])
        self.assertEqual(plan.body["conflicts"][0]["code"], "load_capacity_shortfall")


if __name__ == "__main__":
    unittest.main()
