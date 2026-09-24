"""电价、燃料库存、送出线路和提名的事务用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, PlanApprovalConflict, PlanInfeasible, ValidationFailed
from .models import (
    GenerationPlanRequest,
    GenerationUnit,
    IndexQuote,
    Facility,
    InventoryLot,
    MaintenanceWindow,
    NominationRequest,
    PriceCurve,
    Route,
    SupplyScenario,
    decimal_value,
    identifier,
    period_number,
)
from .planning import (
    AllocationRequest,
    PricePoint,
    allocate_capacity,
    canonical_json,
    decimal_text,
    delivered_after_loss,
    digest,
    effective_capacity,
    latest_streak,
    moving_average,
    quantize_volume,
    scenario_projection,
    weighted_inventory_cost,
)
from .storage import initialize, transaction
from .unitplan import InfeasiblePlan, UnitModel, deviation_report, solve, volume as plan_volume


ZERO = Decimal("0")

ROLE_PERMISSIONS = {
    "planner": {"quote.write", "catalog.write", "scenario.write", "scenario.run", "plan.write", "plan.read"},
    "dispatcher": {"nomination.write", "allocation.run", "transfer.write", "inventory.write", "plan.approve", "plan.actual", "plan.read"},
    "risk": {"outage.write", "scenario.approve", "report.read", "plan.read"},
    "auditor": {"report.read", "audit.read", "plan.read"},
}


class SupplyService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM supply_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM supply_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO supply_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO supply_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    def record_quote(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "quote.write")
        quote = IndexQuote.from_dict(raw)
        previous = self.connection.execute(
            "SELECT quote_id,source_revision FROM market_index_quotes WHERE market_index=? AND trade_date=? "
            "ORDER BY quote_id DESC LIMIT 1",
            (quote.market_index, quote.trade_date),
        ).fetchone()
        if previous is not None and previous["source_revision"] == quote.source_revision:
            raise Conflict("同一来源修订已登记")
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO market_index_quotes(market_index,trade_date,close_cny,source_revision,observed_at,"
                    "supersedes_quote_id,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        quote.market_index,
                        quote.trade_date,
                        decimal_text(quote.close_cny),
                        quote.source_revision,
                        quote.observed_at,
                        None if previous is None else previous["quote_id"],
                        actor_id,
                        self._now(),
                    ),
                )
                quote_id = int(cursor.lastrowid)
                self._audit(
                    "quote",
                    str(quote_id),
                    "quote.recorded",
                    actor_id,
                    {"market_index": quote.market_index, "trade_date": quote.trade_date},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("电价版本冲突") from exc
        return {"quote_id": quote_id, "market_index": quote.market_index, "trade_date": quote.trade_date}

    def price_summary(self, market_index: str, sessions: int = 20) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT q.trade_date,q.close_cny FROM market_index_quotes q "
            "JOIN (SELECT trade_date,max(quote_id) quote_id FROM market_index_quotes "
            "WHERE market_index=? GROUP BY trade_date) latest ON latest.quote_id=q.quote_id "
            "ORDER BY q.trade_date DESC LIMIT ?",
            (market_index.upper(), sessions),
        ).fetchall()
        points = [PricePoint(row["trade_date"], Decimal(row["close_cny"])) for row in rows]
        if not points:
            raise NotFound("没有基准电价")
        streak = latest_streak(points)
        average = moving_average(points, min(5, len(points)))
        latest = max(points, key=lambda item: item.trade_date)
        return {
            "market_index": market_index.upper(),
            "latest": {"trade_date": latest.trade_date, "close_cny": decimal_text(latest.close)},
            "latest_streak": None if streak is None else streak.as_dict(),
            "moving_average": None if average is None else decimal_text(average),
            "observations": len(points),
        }

    def create_facility(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        facility = Facility.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO facilities(facility_id,name,kind,timezone,capacity_mwh,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        facility.facility_id,
                        facility.name,
                        facility.kind,
                        facility.timezone,
                        decimal_text(facility.capacity_mwh),
                        self._now(),
                    ),
                )
                self._audit("facility", facility.facility_id, "facility.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("设施编号已经存在") from exc
        return dict(raw)

    def create_route(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        route = Route.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO routes(route_id,origin_id,destination_id,product,daily_capacity,"
                    "loss_basis_points,transit_hours,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        route.route_id,
                        route.origin_id,
                        route.destination_id,
                        route.product,
                        decimal_text(route.daily_capacity),
                        route.loss_basis_points,
                        route.transit_hours,
                        self._now(),
                    ),
                )
                self._audit("route", route.route_id, "route.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("送出线路编号冲突或设施不存在") from exc
        return self.route(route.route_id)

    def route(self, route_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM routes WHERE route_id=?", (route_id,)).fetchone()
        if row is None:
            raise NotFound("送出线路不存在")
        return dict(row)

    def announce_outage(
        self,
        actor_id: str,
        route_id: str,
        starts_at: str,
        ends_at: str | None,
        capacity_percent: object,
        reason: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "outage.write")
        self.route(route_id)
        try:
            start = parse_utc(starts_at, "starts_at")
            end = None if ends_at is None else parse_utc(ends_at, "ends_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if end is not None and end <= start:
            raise ValidationFailed("ends_at 必须晚于 starts_at")
        percentage = Decimal(str(capacity_percent))
        if percentage < 0 or percentage > 100:
            raise ValidationFailed("capacity_percent 必须在 0 到 100 之间")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO route_outages(route_id,starts_at,ends_at,capacity_percent,reason,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (route_id, utc_text(start), None if end is None else utc_text(end), decimal_text(percentage), reason, actor_id, self._now()),
            )
            outage_id = int(cursor.lastrowid)
            self._audit("route", route_id, "outage.announced", actor_id, {"outage_id": outage_id})
        return {"outage_id": outage_id, "route_id": route_id, "state": "announced"}

    def add_inventory_lot(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "inventory.write")
        lot = InventoryLot.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO inventory_lots(lot_id,facility_id,product,grade,quantity_mwh,available_mwh,"
                    "unit_cost_cny,received_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        lot.lot_id,
                        lot.facility_id,
                        lot.product,
                        lot.grade,
                        decimal_text(lot.quantity_mwh),
                        decimal_text(lot.quantity_mwh),
                        decimal_text(lot.unit_cost_cny),
                        lot.received_at,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("inventory_lot", lot.lot_id, "inventory.received", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("燃料批次冲突或设施不存在") from exc
        return self.inventory_lot(lot.lot_id)

    def inventory_lot(self, lot_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM inventory_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if row is None:
            raise NotFound("燃料批次不存在")
        return dict(row)

    def inventory_summary(self, facility_id: str, product: str) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT * FROM inventory_lots WHERE facility_id=? AND product=? ORDER BY received_at,lot_id",
            (facility_id, product),
        ).fetchall()
        return {"facility_id": facility_id, "product": product, **weighted_inventory_cost(rows)}

    def submit_nomination(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "nomination.write")
        nomination = NominationRequest.from_dict(raw)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM supply_idempotency WHERE scope='nomination' AND idempotency_key=?",
            (nomination.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同提名内容")
            return json.loads(stored["response_json"])
        route = self.route(nomination.route_id)
        if route["state"] != "active":
            raise InvalidState("送出线路当前不可提名")
        response = {
            "nomination_id": nomination.nomination_id,
            "route_id": nomination.route_id,
            "state": "submitted",
            "revision": 1,
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO nominations(nomination_id,route_id,shipper_id,service_date,requested_mwh,"
                    "priority,idempotency_key,submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        nomination.nomination_id,
                        nomination.route_id,
                        nomination.shipper_id,
                        nomination.service_date,
                        decimal_text(nomination.requested_mwh),
                        nomination.priority,
                        nomination.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                self.connection.execute(
                    "INSERT INTO supply_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('nomination',?,?,?,?)",
                    (nomination.idempotency_key, request_digest, canonical_json(response), self._now()),
                )
                self._audit("nomination", nomination.nomination_id, "nomination.submitted", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("提名编号或幂等键冲突") from exc
        return response

    def _capacity_for_date(self, route: sqlite3.Row, service_date: str) -> Decimal:
        start = service_date + "T00:00:00Z"
        end = service_date + "T23:59:59Z"
        rows = self.connection.execute(
            "SELECT capacity_percent FROM route_outages WHERE route_id=? AND state IN ('announced','active') "
            "AND starts_at<=? AND (ends_at IS NULL OR ends_at>=?) ORDER BY outage_id",
            (route["route_id"], end, start),
        ).fetchall()
        percentages = [Decimal(row["capacity_percent"]) for row in rows]
        return effective_capacity(Decimal(route["daily_capacity"]), percentages)

    def allocate(self, actor_id: str, route_id: str, service_date: str) -> dict[str, Any]:
        self._require(actor_id, "allocation.run")
        route = self.connection.execute("SELECT * FROM routes WHERE route_id=?", (route_id,)).fetchone()
        if route is None:
            raise NotFound("送出线路不存在")
        nominations = self.connection.execute(
            "SELECT * FROM nominations WHERE route_id=? AND service_date=? AND state='submitted' "
            "ORDER BY priority,submitted_at,nomination_id",
            (route_id, service_date),
        ).fetchall()
        if not nominations:
            raise InvalidState("没有待分配提名")
        requests = [
            AllocationRequest(
                row["nomination_id"],
                Decimal(row["requested_mwh"]),
                int(row["priority"]),
                row["submitted_at"],
            )
            for row in nominations
        ]
        available = self._capacity_for_date(route, service_date)
        input_value = [dict(row) for row in nominations]
        input_sha256 = digest({"route": dict(route), "nominations": input_value, "capacity": str(available)})
        result_rows = allocate_capacity(available, requests)
        result = {
            "route_id": route_id,
            "service_date": service_date,
            "available_capacity": decimal_text(available),
            "allocations": result_rows,
        }
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO allocation_runs(route_id,service_date,input_sha256,available_capacity,result_json,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (route_id, service_date, input_sha256, decimal_text(available), canonical_json(result), actor_id, self._now()),
            )
            for item in result_rows:
                state = "allocated" if Decimal(item["allocated_mwh"]) > 0 else "cancelled"
                self.connection.execute(
                    "UPDATE nominations SET allocated_mwh=?,state=?,revision=revision+1 "
                    "WHERE nomination_id=? AND state='submitted'",
                    (item["allocated_mwh"], state, item["nomination_id"]),
                )
            allocation_id = int(cursor.lastrowid)
            self._audit("route", route_id, "allocation.completed", actor_id, {"allocation_id": allocation_id})
        return {"allocation_id": allocation_id, **result}

    def dispatch_transfer(
        self,
        actor_id: str,
        transfer_id: str,
        nomination_id: str,
        lot_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        self._require(actor_id, "transfer.write")
        nomination = self.connection.execute(
            "SELECT n.*,r.loss_basis_points,r.transit_hours,r.origin_id FROM nominations n "
            "JOIN routes r ON r.route_id=n.route_id WHERE n.nomination_id=?",
            (nomination_id,),
        ).fetchone()
        if nomination is None:
            raise NotFound("提名不存在")
        if nomination["state"] != "allocated" or nomination["revision"] != expected_revision:
            raise InvalidState("提名不是当前可送电版本")
        lot = self.connection.execute("SELECT * FROM inventory_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if lot is None:
            raise NotFound("燃料批次不存在")
        allocated = Decimal(nomination["allocated_mwh"])
        available = Decimal(lot["available_mwh"])
        if lot["facility_id"] != nomination["origin_id"] or lot["product"] != self.route(nomination["route_id"])["product"]:
            raise Conflict("燃料批次与送出线路起点或电源类型不匹配")
        if available < allocated:
            raise Conflict("燃料库存不足以完成分配")
        expected_delivery = delivered_after_loss(allocated, int(nomination["loss_basis_points"]))
        departed_at = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE inventory_lots SET available_mwh=?,revision=revision+1 WHERE lot_id=? AND revision=?",
                (decimal_text(quantize_volume(available - allocated)), lot_id, lot["revision"]),
            )
            self.connection.execute(
                "UPDATE nominations SET state='in_transit',revision=revision+1 WHERE nomination_id=? AND revision=?",
                (nomination_id, expected_revision),
            )
            self.connection.execute(
                "INSERT INTO transfers(transfer_id,nomination_id,inventory_lot_id,loaded_mwh,"
                "expected_delivered_mwh,departed_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    transfer_id,
                    nomination_id,
                    lot_id,
                    decimal_text(allocated),
                    decimal_text(expected_delivery),
                    departed_at,
                    actor_id,
                    departed_at,
                ),
            )
            self._audit("transfer", transfer_id, "transfer.dispatched", actor_id, {"nomination_id": nomination_id})
        return {
            "transfer_id": transfer_id,
            "state": "in_transit",
            "loaded_mwh": decimal_text(allocated),
            "expected_delivered_mwh": decimal_text(expected_delivery),
            "expected_arrival": utc_text(parse_utc(departed_at) + timedelta(hours=int(nomination["transit_hours"]))),
        }

    def create_scenario(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "scenario.write")
        scenario = SupplyScenario.from_dict(raw)
        definition = canonical_json(raw)
        content_sha256 = hashlib.sha256(definition.encode("utf-8")).hexdigest()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO supply_scenarios(scenario_id,name,definition_json,content_sha256,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (scenario.scenario_id, scenario.name, definition, content_sha256, actor_id, self._now()),
                )
                self._audit("scenario", scenario.scenario_id, "scenario.created", actor_id, {"sha256": content_sha256})
        except sqlite3.IntegrityError as exc:
            raise Conflict("情景编号或内容已经存在") from exc
        return {"scenario_id": scenario.scenario_id, "state": "draft", "sha256": content_sha256}

    def approve_scenario(self, actor_id: str, scenario_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "scenario.approve")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE supply_scenarios SET state='approved',revision=revision+1 "
                "WHERE scenario_id=? AND state='draft' AND revision=?",
                (scenario_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("情景不是当前草稿版本")
            self._audit("scenario", scenario_id, "scenario.approved", actor_id, {})
        return {"scenario_id": scenario_id, "state": "approved", "revision": expected_revision + 1}

    def run_scenario(self, actor_id: str, scenario_id: str, as_of_date: str) -> dict[str, Any]:
        self._require(actor_id, "scenario.run")
        row = self.connection.execute(
            "SELECT * FROM supply_scenarios WHERE scenario_id=?", (scenario_id,)
        ).fetchone()
        if row is None:
            raise NotFound("情景不存在")
        if row["state"] != "approved":
            raise InvalidState("只有已批准情景可以运行")
        scenario = SupplyScenario.from_dict(json.loads(row["definition_json"]))
        price_row = self.connection.execute(
            "SELECT close_cny FROM market_index_quotes WHERE trade_date<=? ORDER BY trade_date DESC,quote_id DESC LIMIT 1",
            (as_of_date,),
        ).fetchone()
        if price_row is None:
            raise InvalidState("截止日期没有可用电价")
        routes = self.connection.execute("SELECT * FROM routes WHERE state='active' ORDER BY route_id").fetchall()
        inventory = self.connection.execute(
            "SELECT facility_id,product,sum(CAST(available_mwh AS REAL)) available_mwh "
            "FROM inventory_lots GROUP BY facility_id,product ORDER BY facility_id,product"
        ).fetchall()
        input_value = {
            "scenario_sha256": row["content_sha256"],
            "as_of_date": as_of_date,
            "price": price_row["close_cny"],
            "routes": [dict(item) for item in routes],
            "inventory": [dict(item) for item in inventory],
        }
        input_sha256 = digest(input_value)
        existing = self.connection.execute(
            "SELECT run_id,result_json FROM scenario_runs WHERE scenario_id=? AND as_of_date=? AND input_sha256=?",
            (scenario_id, as_of_date, input_sha256),
        ).fetchone()
        if existing is not None:
            return {"run_id": existing["run_id"], **json.loads(existing["result_json"]), "replayed": True}
        result = scenario_projection(
            current_price=Decimal(price_row["close_cny"]),
            market_index_drop_percent=scenario.market_index_drop_percent,
            routes=routes,
            inventory=inventory,
            route_capacity_changes=scenario.route_capacity_changes,
            demand_changes=scenario.demand_changes,
        )
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO scenario_runs(scenario_id,as_of_date,input_sha256,result_json,created_by,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (scenario_id, as_of_date, input_sha256, canonical_json(result), actor_id, self._now()),
            )
            run_id = int(cursor.lastrowid)
            self._audit("scenario", scenario_id, "scenario.executed", actor_id, {"run_id": run_id})
        return {"run_id": run_id, **result, "replayed": False}

    # ---- 机组出力计划 ----

    def create_generation_unit(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        unit = GenerationUnit.from_dict(raw)
        facility = self.connection.execute(
            "SELECT facility_id FROM facilities WHERE facility_id=?", (unit.facility_id,)
        ).fetchone()
        if facility is None:
            raise NotFound("设施不存在")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO generation_units(unit_id,name,facility_id,fuel_product,min_output_mwh,"
                    "max_output_mwh,ramp_up_mwh,ramp_down_mwh,startup_cost_cny,marginal_cost_cny,fuel_factor,"
                    "initial_output_mwh,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        unit.unit_id, unit.name, unit.facility_id, unit.fuel_product,
                        decimal_text(unit.min_output_mwh), decimal_text(unit.max_output_mwh),
                        decimal_text(unit.ramp_up_mwh), decimal_text(unit.ramp_down_mwh),
                        decimal_text(unit.startup_cost_cny), decimal_text(unit.marginal_cost_cny),
                        decimal_text(unit.fuel_factor), decimal_text(unit.initial_output_mwh),
                        actor_id, self._now(),
                    ),
                )
                self._audit("generation_unit", unit.unit_id, "unit.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("机组编号已经存在") from exc
        return self.generation_unit(unit.unit_id)

    def generation_unit(self, unit_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM generation_units WHERE unit_id=?", (unit_id,)).fetchone()
        if row is None:
            raise NotFound("机组不存在")
        return dict(row)

    def announce_maintenance(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "outage.write")
        window = MaintenanceWindow.from_dict(raw)
        unit = self.connection.execute(
            "SELECT unit_id,active FROM generation_units WHERE unit_id=?", (window.unit_id,)
        ).fetchone()
        if unit is None:
            raise NotFound("机组不存在")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO unit_maintenance_windows(unit_id,trade_date,start_period,end_period,reason,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (window.unit_id, window.trade_date, window.start_period, window.end_period,
                 window.reason, actor_id, self._now()),
            )
            window_id = int(cursor.lastrowid)
            self._audit("generation_unit", window.unit_id, "maintenance.announced", actor_id,
                        {"window_id": window_id, "trade_date": window.trade_date,
                         "start_period": window.start_period, "end_period": window.end_period})
        return {"window_id": window_id, "unit_id": window.unit_id, "trade_date": window.trade_date, "state": "announced"}

    def record_price_curve(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "quote.write")
        curve = PriceCurve.from_dict(raw)
        definition = canonical_json(raw)
        content_sha256 = hashlib.sha256(definition.encode("utf-8")).hexdigest()
        previous = self.connection.execute(
            "SELECT price_version_id FROM price_curves WHERE trade_date=? "
            "ORDER BY recorded_at DESC, rowid DESC LIMIT 1",
            (curve.trade_date,),
        ).fetchone()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO price_curves(price_version_id,trade_date,source_revision,points_json,"
                    "content_sha256,supersedes_price_version_id,recorded_by,recorded_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (curve.price_version_id, curve.trade_date, curve.source_revision, definition,
                     content_sha256, None if previous is None else previous["price_version_id"],
                     actor_id, self._now()),
                )
                self._audit("price_curve", curve.price_version_id, "price_curve.recorded", actor_id,
                            {"trade_date": curve.trade_date, "sha256": content_sha256})
        except sqlite3.IntegrityError as exc:
            raise Conflict("价格版本编号或同日来源修订冲突") from exc
        return {"price_version_id": curve.price_version_id, "trade_date": curve.trade_date,
                "source_revision": curve.source_revision, "sha256": content_sha256}

    def _load_plan_inputs(self, request: GenerationPlanRequest) -> dict[str, Any]:
        curve = self.connection.execute(
            "SELECT * FROM price_curves WHERE price_version_id=?", (request.price_version_id,)
        ).fetchone()
        if curve is None:
            raise NotFound("价格版本不存在")
        if curve["trade_date"] != request.trade_date:
            raise ValidationFailed("价格版本不属于计划交易日")
        if request.unit_ids:
            wanted = request.unit_ids
            rows = self.connection.execute(
                f"SELECT * FROM generation_units WHERE active=1 AND unit_id IN ({','.join('?' for _ in wanted)})",
                wanted,
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM generation_units WHERE active=1 ORDER BY unit_id"
            ).fetchall()
        found = {row["unit_id"] for row in rows}
        missing = [unit_id for unit_id in (request.unit_ids or tuple(sorted(found))) if unit_id not in found]
        if missing:
            raise NotFound(f"机组不存在或已停用: {','.join(missing)}")
        windows = self.connection.execute(
            "SELECT unit_id,start_period,end_period FROM unit_maintenance_windows WHERE trade_date=?",
            (request.trade_date,),
        ).fetchall()
        blocked: dict[str, frozenset[int]] = {}
        for row in windows:
            # 库中为 1 基时段，求解器内部使用 0 基
            periods = frozenset(range(row["start_period"] - 1, row["end_period"]))
            blocked[row["unit_id"]] = blocked.get(row["unit_id"], frozenset()) | periods
        return {"curve": curve, "units": rows, "blocked": blocked}

    def compute_plan(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """可重复的草稿/终稿计算：不预留任何燃料，相同输入始终返回同一方案。"""
        self._require(actor_id, "plan.write")
        request = GenerationPlanRequest.from_dict(raw)
        loaded = self._load_plan_inputs(request)
        curve_row: sqlite3.Row = loaded["curve"]
        unit_rows = loaded["units"]
        blocked: dict[str, frozenset[int]] = loaded["blocked"]
        curve = PriceCurve.from_dict(json.loads(curve_row["points_json"]))

        units = [
            UnitModel(
                unit_id=row["unit_id"],
                min_output=Decimal(row["min_output_mwh"]),
                max_output=Decimal(row["max_output_mwh"]),
                ramp_up=Decimal(row["ramp_up_mwh"]),
                ramp_down=Decimal(row["ramp_down_mwh"]),
                startup_cost=Decimal(row["startup_cost_cny"]),
                marginal_cost=Decimal(row["marginal_cost_cny"]),
                initial_output=Decimal(row["initial_output_mwh"]),
                blocked_periods=blocked.get(row["unit_id"], frozenset()),
            )
            for row in sorted(unit_rows, key=lambda item: item["unit_id"])
        ]
        prices = [curve.points[p] for p in range(1, 25)]
        demand = [request.demand[p] for p in range(1, 25)]

        input_value = {
            "trade_date": request.trade_date,
            "price_version": {"id": curve_row["price_version_id"], "sha256": curve_row["content_sha256"]},
            "reserve_percent": decimal_text(request.reserve_percent),
            "demand": {str(p): decimal_text(request.demand[p]) for p in range(1, 25)},
            "units": [
                {
                    "unit_id": row["unit_id"],
                    "revision": row["revision"],
                    "min_output_mwh": row["min_output_mwh"],
                    "max_output_mwh": row["max_output_mwh"],
                    "ramp_up_mwh": row["ramp_up_mwh"],
                    "ramp_down_mwh": row["ramp_down_mwh"],
                    "startup_cost_cny": row["startup_cost_cny"],
                    "marginal_cost_cny": row["marginal_cost_cny"],
                    "fuel_factor": row["fuel_factor"],
                    "initial_output_mwh": row["initial_output_mwh"],
                    "blocked_periods": sorted(period + 1 for period in blocked.get(row["unit_id"], frozenset())),
                }
                for row in sorted(unit_rows, key=lambda item: item["unit_id"])
            ],
        }
        input_sha256 = digest(input_value)

        same_plan = self.connection.execute(
            "SELECT plan_id,state,result_json,conflicts_json,input_sha256 FROM generation_plans WHERE plan_id=?",
            (request.plan_id,),
        ).fetchone()
        if same_plan is not None:
            if same_plan["input_sha256"] != input_sha256:
                raise Conflict("计划编号已用于不同输入，请使用新的 plan_id")
            return self._plan_response(same_plan, replayed=True)
        existing = self.connection.execute(
            "SELECT plan_id,state,result_json,conflicts_json FROM generation_plans WHERE input_sha256=?",
            (input_sha256,),
        ).fetchone()
        if existing is not None:
            return self._plan_response(existing, replayed=True)

        try:
            result = solve(units=units, prices=prices, demand=demand, reserve_percent=request.reserve_percent)
            conflicts = None
            state = "draft"
        except InfeasiblePlan as exc:
            result = None
            conflicts = exc.conflicts
            state = "infeasible"

        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO generation_plans(plan_id,trade_date,price_version_id,reserve_percent,input_json,"
                "input_sha256,result_json,conflicts_json,state,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (request.plan_id, request.trade_date, request.price_version_id,
                 decimal_text(request.reserve_percent), canonical_json(input_value), input_sha256,
                 None if result is None else canonical_json(result),
                 None if conflicts is None else canonical_json(conflicts),
                 state, actor_id, self._now()),
            )
            if result is not None:
                for period in result["periods"]:
                    for item in period["units"]:
                        self.connection.execute(
                            "INSERT INTO plan_unit_schedules(plan_id,period,unit_id,committed,started,"
                            "output_mwh,startup_cost_cny,energy_cost_cny) VALUES(?,?,?,?,?,?,?,?)",
                            (request.plan_id, period["period"], item["unit_id"],
                             1 if item["committed"] else 0, 1 if item["started"] else 0,
                             item["output_mwh"], item["startup_cost_cny"], item["energy_cost_cny"]),
                        )
            self._audit("generation_plan", request.plan_id, "plan.computed", actor_id,
                        {"state": state, "input_sha256": input_sha256})
        row = self.connection.execute(
            "SELECT plan_id,state,result_json,conflicts_json FROM generation_plans WHERE plan_id=?",
            (request.plan_id,),
        ).fetchone()
        if state == "infeasible":
            raise PlanInfeasible(json.loads(row["conflicts_json"]))
        return self._plan_response(row, replayed=False)

    def _plan_response(self, row: sqlite3.Row, *, replayed: bool) -> dict[str, Any]:
        if row["state"] == "infeasible":
            raise PlanInfeasible(json.loads(row["conflicts_json"]))
        return {"plan_id": row["plan_id"], "state": row["state"], "replayed": replayed,
                **json.loads(row["result_json"])}

    def get_plan(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        self._require(actor_id, "plan.read")
        row = self.connection.execute(
            "SELECT * FROM generation_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise NotFound("计划不存在")
        response = {
            "plan_id": row["plan_id"],
            "trade_date": row["trade_date"],
            "price_version_id": row["price_version_id"],
            "reserve_percent": row["reserve_percent"],
            "state": row["state"],
            "revision": row["revision"],
            "input_sha256": row["input_sha256"],
            "input": json.loads(row["input_json"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "approved_by": row["approved_by"],
            "approved_at": row["approved_at"],
        }
        if row["state"] == "infeasible":
            response["conflicts"] = json.loads(row["conflicts_json"])
        else:
            response.update(json.loads(row["result_json"]))
        if row["fuel_reserved"]:
            reservation = self.connection.execute(
                "SELECT * FROM fuel_reservations WHERE plan_id=?", (plan_id,)
            ).fetchone()
            items = self.connection.execute(
                "SELECT facility_id,fuel_product,lot_id,reserved_mwh,lot_revision "
                "FROM fuel_reservation_items WHERE reservation_id=? ORDER BY facility_id,lot_id",
                (reservation["reservation_id"],),
            ).fetchall()
            response["fuel_reservation"] = {
                "reservation_id": reservation["reservation_id"],
                "state": reservation["state"],
                "total_reserved_mwh": reservation["total_reserved_mwh"],
                "items": [dict(item) for item in items],
            }
        return response

    def approve_plan(self, actor_id: str, plan_id: str, expected_revision: int) -> dict[str, Any]:
        """批准草稿：以库存当前版本原子预留燃料；重复批准幂等。"""
        self._require(actor_id, "plan.approve")
        row = self.connection.execute(
            "SELECT * FROM generation_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise NotFound("计划不存在")
        if row["state"] == "infeasible":
            raise InvalidState("无解计划不能批准")
        if row["state"] == "approved":
            if row["revision"] != expected_revision:
                raise Conflict("计划修订版本不匹配")
            return {**self.get_plan(actor_id, plan_id), "idempotent": True}
        if row["state"] != "draft" or row["revision"] != expected_revision:
            raise InvalidState("计划不是当前草稿版本")

        result = json.loads(row["result_json"])
        unit_params = {
            item["unit_id"]: item
            for item in self.connection.execute(
                "SELECT unit_id,facility_id,fuel_product,fuel_factor FROM generation_units"
            ).fetchall()
        }
        needed_by_key: dict[tuple[str, str], Decimal] = {}
        for period in result["periods"]:
            for unit_row in period["units"]:
                if Decimal(unit_row["output_mwh"]) <= 0:
                    continue
                unit = unit_params.get(unit_row["unit_id"])
                if unit is None:
                    raise InvalidState("计划引用的机组已不存在，请重新计算")
                key = (unit["facility_id"], unit["fuel_product"])
                fuel = plan_volume(
                    Decimal(unit_row["output_mwh"]) * Decimal(unit["fuel_factor"])
                )
                needed_by_key[key] = needed_by_key.get(key, ZERO) + fuel

        with transaction(self.connection, immediate=True):
            # 先抢占计划行：并发的第二个批准在此得到 0 行，整单回滚
            claimed = self.connection.execute(
                "UPDATE generation_plans SET state='approved',revision=revision+1,fuel_reserved=1,"
                "approved_by=?,approved_at=? WHERE plan_id=? AND state='draft' AND revision=?",
                (actor_id, self._now(), plan_id, expected_revision),
            )
            if claimed.rowcount != 1:
                raise Conflict("计划已被批准或修订版本已变化")
            lots = self.connection.execute(
                "SELECT * FROM inventory_lots ORDER BY facility_id,product,received_at,lot_id"
            ).fetchall()
            available: dict[tuple[str, str], list[sqlite3.Row]] = {}
            for lot in lots:
                available.setdefault((lot["facility_id"], lot["product"]), []).append(lot)
            lot_rows = {lot["lot_id"]: lot for lot in lots}
            items: list[tuple[str, str, str, Decimal, int]] = []
            shortages: list[dict[str, object]] = []
            for (facility_id, product), needed in sorted(needed_by_key.items()):
                remaining = needed
                chosen: list[tuple[sqlite3.Row, Decimal]] = []
                for lot in available.get((facility_id, product), []):
                    lot_available = Decimal(lot["available_mwh"])
                    if lot_available <= 0 or remaining <= 0:
                        continue
                    take = plan_volume(min(lot_available, remaining))
                    if take <= 0:
                        continue
                    chosen.append((lot, take))
                    remaining = plan_volume(remaining - take)
                if remaining > 0:
                    shortages.append({
                        "code": "FUEL_INVENTORY_SHORTAGE",
                        "facility_id": facility_id,
                        "fuel_product": product,
                        "required_mwh": decimal_text(needed),
                        "shortfall_mwh": decimal_text(remaining),
                    })
                items.extend(
                    (facility_id, product, lot["lot_id"], take, int(lot["revision"]))
                    for lot, take in chosen
                )
            if shortages:
                raise PlanApprovalConflict(shortages)

            cursor = self.connection.execute(
                "INSERT INTO fuel_reservations(plan_id,state,total_reserved_mwh,created_by,created_at) "
                "VALUES(?, 'held', ?, ?, ?)",
                (plan_id, decimal_text(sum((item[3] for item in items), ZERO)), actor_id, self._now()),
            )
            reservation_id = int(cursor.lastrowid)
            for _facility_id, _product, lot_id, take, lot_revision in items:
                self.connection.execute(
                    "INSERT INTO fuel_reservation_items(reservation_id,lot_id,facility_id,fuel_product,"
                    "reserved_mwh,lot_revision) VALUES(?,?,?,?,?,?)",
                    (reservation_id, lot_id, _facility_id, _product, decimal_text(take), lot_revision),
                )
                updated_available = plan_volume(Decimal(lot_rows[lot_id]["available_mwh"]) - take)
                updated = self.connection.execute(
                    "UPDATE inventory_lots SET available_mwh=?,revision=revision+1 "
                    "WHERE lot_id=? AND revision=? AND CAST(available_mwh AS REAL)>=?",
                    (decimal_text(updated_available), lot_id, lot_revision, float(take)),
                )
                if updated.rowcount != 1:
                    raise Conflict("燃料库存版本已变化，请重新计算计划")
                lot_rows[lot_id] = self.connection.execute(
                    "SELECT * FROM inventory_lots WHERE lot_id=?", (lot_id,)
                ).fetchone()
            self._audit("generation_plan", plan_id, "plan.approved", actor_id,
                        {"reservation_id": reservation_id, "items": len(items)})
        return {**self.get_plan(actor_id, plan_id), "idempotent": False}

    def report_actual(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "plan.actual")
        plan_id = identifier(raw.get("plan_id"), "plan_id")
        points = raw.get("points")
        if not isinstance(points, list) or not points:
            raise ValidationFailed("points 必须是非空数组")
        row = self.connection.execute(
            "SELECT state FROM generation_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise NotFound("计划不存在")
        if row["state"] != "approved":
            raise InvalidState("只有已批准计划可以上报实际出力")
        parsed: list[tuple[int, str, Decimal]] = []
        for index, item in enumerate(points):
            if not isinstance(item, Mapping):
                raise ValidationFailed(f"points[{index}] 必须是对象")
            parsed.append((
                period_number(item.get("period"), f"points[{index}].period"),
                identifier(item.get("unit_id"), f"points[{index}].unit_id"),
                plan_volume(decimal_value(item.get("output_mwh"), f"points[{index}].output_mwh", minimum=Decimal("0"))),
            ))
        with transaction(self.connection, immediate=True):
            stored = 0
            for period, unit_id, output in parsed:
                cursor = self.connection.execute(
                    "INSERT INTO plan_actuals(plan_id,period,unit_id,output_mwh,reported_by,reported_at) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(plan_id,period,unit_id) DO UPDATE SET "
                    "output_mwh=excluded.output_mwh,reported_by=excluded.reported_by,reported_at=excluded.reported_at",
                    (plan_id, period, unit_id, decimal_text(output), actor_id, self._now()),
                )
                stored += cursor.rowcount
            self._audit("generation_plan", plan_id, "plan.actual.reported", actor_id, {"points": len(parsed)})
        return {"plan_id": plan_id, "state": row["state"], "stored_points": stored}

    def plan_deviation(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        self._require(actor_id, "plan.read")
        plan = self.connection.execute(
            "SELECT result_json FROM generation_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if plan is None:
            raise NotFound("计划不存在")
        if plan["result_json"] is None:
            raise InvalidState("无解计划没有出力序列")
        schedule: list[dict[str, object]] = []
        for period in json.loads(plan["result_json"])["periods"]:
            for unit_row in period["units"]:
                schedule.append({"period": period["period"], "unit_id": unit_row["unit_id"],
                                 "output_mwh": unit_row["output_mwh"]})
        actual_rows = self.connection.execute(
            "SELECT period,unit_id,output_mwh FROM plan_actuals WHERE plan_id=? ORDER BY period,unit_id",
            (plan_id,),
        ).fetchall()
        actuals = [dict(item) for item in actual_rows]
        report = deviation_report(schedule, actuals)
        report["plan_id"] = plan_id
        return report

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM supply_audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
