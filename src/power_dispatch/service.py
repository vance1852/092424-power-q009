"""电价、燃料库存、送出线路和提名的事务用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import CRUDE_GRADES, PRODUCTS, IndexQuote, Facility, InventoryLot, NominationRequest, Route, SupplyScenario, date_text, decimal_value, identifier
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
from .unit_schedule import ZERO, PlanRequest, UnitParameters, qmw, solve_schedule


ROLE_PERMISSIONS = {
    "planner": {"quote.write", "curve.write", "catalog.write", "unit.write", "maintenance.write", "plan.write", "plan.read", "scenario.write", "scenario.run"},
    "dispatcher": {"nomination.write", "allocation.run", "transfer.write", "inventory.write", "plan.approve", "plan.read", "actual.write"},
    "risk": {"outage.write", "scenario.approve", "report.read", "plan.read"},
    "auditor": {"report.read", "audit.read", "plan.read"},
}

SOLVER_VERSION = "unit_schedule:1"


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

    # ------------------------------------------------------------------
    # 次日机组出力计划
    # ------------------------------------------------------------------

    @staticmethod
    def _hour_series(raw: Mapping[str, Any], field: str) -> list[Decimal]:
        value = raw.get(field)
        if not isinstance(value, (list, tuple)) or len(value) != 24:
            raise ValidationFailed(f"{field} 必须是 24 个小时点")
        return [decimal_value(point, f"{field}[{index}]") for index, point in enumerate(value)]

    def record_price_curve(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """登记次日 24 点峰谷价格曲线版本；同一来源修订不覆盖。"""
        self._require(actor_id, "curve.write")
        market_index = str(raw.get("market_index", "")).strip().upper()
        if market_index not in CRUDE_GRADES - {"CUSTOM"}:
            raise ValidationFailed("market_index 必须是受支持的基准电价")
        trade_date = date_text(raw.get("trade_date"), "trade_date")
        source_revision = identifier(raw.get("source_revision"), "source_revision")
        points = self._hour_series(raw, "prices")
        if any(point < 0 for point in points):
            raise ValidationFailed("电价不能为负数")
        points_text = [decimal_text(point) for point in points]
        content = canonical_json({
            "market_index": market_index,
            "trade_date": trade_date,
            "source_revision": source_revision,
            "prices": points_text,
        })
        content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO price_curve_versions(market_index,trade_date,source_revision,points_json,"
                    "recorded_by,created_at) VALUES(?,?,?,?,?,?)",
                    (market_index, trade_date, source_revision, content, actor_id, self._now()),
                )
                curve_id = int(cursor.lastrowid)
                self._audit("price_curve", str(curve_id), "price_curve.recorded", actor_id,
                            {"sha256": content_sha256, "trade_date": trade_date})
        except sqlite3.IntegrityError as exc:
            raise Conflict("同一价格曲线来源修订已登记") from exc
        return {
            "curve_id": curve_id,
            "market_index": market_index,
            "trade_date": trade_date,
            "source_revision": source_revision,
            "prices": points_text,
            "sha256": content_sha256,
        }

    def _latest_curve(self, market_index: str, trade_date: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM price_curve_versions WHERE market_index=? AND trade_date=? "
            "ORDER BY curve_id DESC LIMIT 1",
            (market_index.upper(), trade_date),
        ).fetchone()
        if row is None:
            raise NotFound("该结算日没有峰谷价格曲线版本")
        return row

    def price_curve(self, actor_id: str, market_index: str, trade_date: str) -> dict[str, Any]:
        self._require(actor_id, "plan.read")
        row = self._latest_curve(market_index, trade_date)
        body = json.loads(row["points_json"])
        return {"curve_id": row["curve_id"], **body}

    def register_unit(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """登记机组参数版本；参数变化产生新版本，旧版本被保留以重放历史计划。"""
        self._require(actor_id, "unit.write")
        unit = UnitParameters.from_dict(raw)
        facility_id = identifier(raw.get("facility_id"), "facility_id")
        product = str(raw.get("product", "")).strip()
        if product not in PRODUCTS:
            raise ValidationFailed("product 不是受支持的电源类型")
        facility = self.connection.execute(
            "SELECT facility_id FROM facilities WHERE facility_id=?", (facility_id,)
        ).fetchone()
        if facility is None:
            raise NotFound("设施不存在")
        params = unit.as_dict()
        content = canonical_json({"facility_id": facility_id, "product": product, "params": params})
        content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
        existing_same = self.connection.execute(
            "SELECT * FROM generation_unit_revisions WHERE unit_id=? AND content_sha256=?",
            (unit.unit_id, content_sha256),
        ).fetchone()
        if existing_same is not None:  # 相同内容重复登记：幂等返回现有版本。
            return {"unit_id": unit.unit_id, "revision": existing_same["revision"],
                    "state": existing_same["state"], "sha256": content_sha256, "unchanged": True}
        try:
            with transaction(self.connection, immediate=True):
                identity = self.connection.execute(
                    "SELECT * FROM generation_units WHERE unit_id=?", (unit.unit_id,)
                ).fetchone()
                if identity is None:
                    revision = 1
                    self.connection.execute(
                        "INSERT INTO generation_units(unit_id,facility_id,product,current_revision,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?)",
                        (unit.unit_id, facility_id, product, revision, actor_id, self._now()),
                    )
                else:
                    if identity["facility_id"] != facility_id or identity["product"] != product:
                        raise Conflict("机组所属设施与电源类型不可变更")
                    revision = int(identity["current_revision"]) + 1
                    self.connection.execute(
                        "UPDATE generation_unit_revisions SET state='retired' "
                        "WHERE unit_id=? AND state='active'",
                        (unit.unit_id,),
                    )
                self.connection.execute(
                    "INSERT INTO generation_unit_revisions(unit_id,revision,name,params_json,"
                    "content_sha256,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (unit.unit_id, revision, unit.name, canonical_json(params),
                     content_sha256, actor_id, self._now()),
                )
                self.connection.execute(
                    "UPDATE generation_units SET current_revision=? WHERE unit_id=?",
                    (revision, unit.unit_id),
                )
                self._audit("generation_unit", unit.unit_id, "unit.registered", actor_id,
                            {"sha256": content_sha256, "facility_id": facility_id, "revision": revision})
        except sqlite3.IntegrityError as exc:
            raise Conflict("机组登记冲突") from exc
        return {"unit_id": unit.unit_id, "revision": revision, "state": "active",
                "sha256": content_sha256, "unchanged": False}

    def _active_units(self, facility_id: str, product: str, unit_ids: tuple[str, ...] | None) -> list[sqlite3.Row]:
        rows = self.connection.execute(
            "SELECT r.*,u.facility_id,u.product FROM generation_unit_revisions r "
            "JOIN generation_units u ON u.unit_id=r.unit_id "
            "WHERE u.facility_id=? AND u.product=? AND r.state='active'",
            (facility_id, product),
        ).fetchall()
        if unit_ids is not None:
            selected = set(unit_ids)
            rows = [row for row in rows if row["unit_id"] in selected]
            found = {row["unit_id"] for row in rows}
            missing = sorted(selected - found)
            if missing:
                raise NotFound(f"机组不存在或未在役: {','.join(missing)}")
        if not rows:
            raise NotFound("没有可参与计划的在役机组")
        rows.sort(key=lambda row: row["unit_id"])
        return rows

    def declare_maintenance(
        self,
        actor_id: str,
        unit_id: str,
        trade_date: str,
        start_hour: int,
        end_hour: int,
        reason: str,
    ) -> dict[str, Any]:
        """登记某日的机组检修禁运窗口（小时区间，end_hour=24 表示含第 23 时段）。"""
        self._require(actor_id, "maintenance.write")
        unit = self.connection.execute(
            "SELECT u.unit_id FROM generation_units u "
            "JOIN generation_unit_revisions r ON r.unit_id=u.unit_id AND r.revision=u.current_revision "
            "WHERE u.unit_id=? AND r.state='active'",
            (unit_id,),
        ).fetchone()
        if unit is None:
            raise NotFound("在役机组不存在")
        day = date_text(trade_date, "trade_date")
        if isinstance(start_hour, bool) or not isinstance(start_hour, int) or not 0 <= start_hour <= 23:
            raise ValidationFailed("start_hour 必须是 0 到 23 的整数")
        if isinstance(end_hour, bool) or not isinstance(end_hour, int) or not 1 <= end_hour <= 24:
            raise ValidationFailed("end_hour 必须是 1 到 24 的整数")
        if end_hour <= start_hour:
            raise ValidationFailed("检修窗口 end_hour 必须晚于 start_hour")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationFailed("reason 不能为空")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO unit_maintenance_windows(unit_id,trade_date,start_hour,end_hour,reason,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (unit_id, day, start_hour, end_hour, reason.strip(), actor_id, self._now()),
            )
            window_id = int(cursor.lastrowid)
            self._audit("generation_unit", unit_id, "maintenance.declared", actor_id,
                        {"window_id": window_id, "trade_date": day,
                         "hours": [start_hour, end_hour]})
        return {"window_id": window_id, "unit_id": unit_id, "trade_date": day,
                "start_hour": start_hour, "end_hour": end_hour}

    def _maintenance_map(self, unit_rows: Sequence[sqlite3.Row], trade_date: str) -> dict[str, frozenset[int]]:
        blocked: dict[str, frozenset[int]] = {}
        for row in unit_rows:
            windows = self.connection.execute(
                "SELECT start_hour,end_hour FROM unit_maintenance_windows WHERE unit_id=? AND trade_date=?",
                (row["unit_id"], trade_date),
            ).fetchall()
            hours: set[int] = set()
            for window in windows:
                hours.update(range(window["start_hour"], window["end_hour"]))
            if hours:
                blocked[row["unit_id"]] = frozenset(hours)
        return blocked

    def _inventory_version(self, facility_id: str, product: str) -> tuple[str, Decimal]:
        """以当前全部燃料批次的修订号和可用量构成库存版本，返回摘要与加权单价。"""
        rows = self.connection.execute(
            "SELECT lot_id,available_mwh,unit_cost_cny,revision FROM inventory_lots "
            "WHERE facility_id=? AND product=? ORDER BY lot_id",
            (facility_id, product),
        ).fetchall()
        snapshot = [
            {"lot_id": row["lot_id"], "available_mwh": row["available_mwh"],
             "unit_cost_cny": row["unit_cost_cny"], "revision": row["revision"]}
            for row in rows
        ]
        summary = weighted_inventory_cost(rows)
        unit_cost = Decimal(summary["weighted_unit_cost_cny"]) if Decimal(summary["available_mwh"]) > 0 else ZERO
        return digest(snapshot), unit_cost

    def compute_plan(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """计算（或重算）次日机组出力草稿。草稿不预留任何燃料。

        同一 plan_id 以输入指纹幂等：输入未变则重放已保存结果；输入变化时
        旧草稿被标记为 superseded 并生成新版本，批准后的计划不可再计算。
        """
        self._require(actor_id, "plan.write")
        request = PlanRequest.from_dict(raw)
        unit_rows = self._active_units(request.facility_id, request.product, request.unit_ids)
        units = [UnitParameters.from_dict(json.loads(row["params_json"])) for row in unit_rows]
        if request.quote_id is not None:
            curve_row = self.connection.execute(
                "SELECT * FROM price_curve_versions WHERE curve_id=?", (request.quote_id,)
            ).fetchone()
            if curve_row is None:
                raise NotFound("价格曲线版本不存在")
            if curve_row["trade_date"] != request.trade_date:
                raise ValidationFailed("价格曲线版本与计划结算日不一致")
            curve_snapshot = {
                "kind": "curve_version",
                "curve_id": curve_row["curve_id"],
                "points_json": json.loads(curve_row["points_json"]),
            }
            curve_prices = tuple(Decimal(p) for p in curve_snapshot["points_json"]["prices"])
        else:
            curve_row = self.connection.execute(
                "SELECT * FROM price_curve_versions WHERE trade_date=? ORDER BY curve_id DESC LIMIT 1",
                (request.trade_date,),
            ).fetchone()
            if curve_row is not None:
                curve_snapshot = {
                    "kind": "curve_version",
                    "curve_id": curve_row["curve_id"],
                    "points_json": json.loads(curve_row["points_json"]),
                }
                curve_prices = tuple(Decimal(p) for p in curve_snapshot["points_json"]["prices"])
            else:
                curve_snapshot = {"kind": "inline", "prices": [decimal_text(p) for p in request.prices]}
                curve_prices = request.prices
        if curve_prices != request.prices:
            raise Conflict("请求价格曲线与所引用版本不一致")

        blocked = self._maintenance_map(unit_rows, request.trade_date)
        inventory_sha256, fuel_unit_cost = self._inventory_version(request.facility_id, request.product)
        for uid, output in request.initial_output_mw.items():
            if not any(row["unit_id"] == uid for row in unit_rows):
                raise ValidationFailed(f"initial_output_mw 引用了未参与计划的机组 {uid}")
        input_value = {
            "solver_version": SOLVER_VERSION,
            "request": canonical_json(raw),
            "units": [{"unit_id": row["unit_id"], "sha256": row["content_sha256"],
                       "params": json.loads(row["params_json"])} for row in unit_rows],
            "maintenance": {uid: sorted(hours) for uid, hours in sorted(blocked.items())},
            "quote": curve_snapshot,
            "inventory_version": inventory_sha256,
        }
        input_sha256 = digest(input_value)

        existing = self.connection.execute(
            "SELECT * FROM generation_plan_revisions WHERE plan_id=? ORDER BY revision DESC",
            (request.plan_id,),
        ).fetchall()
        if existing:
            latest = existing[0]
            if latest["state"] == "approved":
                raise InvalidState("计划已批准，不能重新计算")
            same = self.connection.execute(
                "SELECT * FROM generation_plan_revisions WHERE plan_id=? AND input_sha256=?",
                (request.plan_id, input_sha256),
            ).fetchone()
            if same is not None:
                return self._plan_response(same, replayed=True)

        result = solve_schedule(
            units=units,
            blocked_hours=blocked,
            prices=curve_prices,
            load_mw=request.load_mw,
            reserve_percent=request.reserve_percent,
            fuel_unit_cost=fuel_unit_cost,
            initial_output=request.initial_output_mw,
        )
        revision = 1 if not existing else max(row["revision"] for row in existing) + 1
        fuel_demand = result.get("fuel_demand", "0.000")
        try:
            with transaction(self.connection, immediate=True):
                if not existing:
                    self.connection.execute(
                        "INSERT INTO generation_plans(plan_id,trade_date,facility_id,product,current_revision,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                        (request.plan_id, request.trade_date, request.facility_id, request.product, revision,
                         actor_id, self._now()),
                    )
                else:
                    self.connection.execute(
                        "UPDATE generation_plan_revisions SET state='superseded' WHERE plan_id=? AND state='draft'",
                        (request.plan_id,),
                    )
                self.connection.execute(
                    "INSERT INTO generation_plan_revisions(plan_id,revision,trade_date,facility_id,product,"
                    "curve_id,request_json,input_sha256,quote_snapshot_json,feasible,result_json,conflicts_json,"
                    "inventory_version_sha256,fuel_unit_cost_cny,fuel_demand,state,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (request.plan_id, revision, request.trade_date, request.facility_id, request.product,
                     curve_snapshot.get("curve_id"), canonical_json(raw), input_sha256,
                     canonical_json(curve_snapshot), 1 if result["feasible"] else 0,
                     canonical_json(result), canonical_json(result["conflicts"]), inventory_sha256,
                     decimal_text(fuel_unit_cost), fuel_demand, "draft", actor_id, self._now()),
                )
                self.connection.execute(
                    "UPDATE generation_plans SET current_revision=? WHERE plan_id=?",
                    (revision, request.plan_id),
                )
                row = self.connection.execute(
                    "SELECT * FROM generation_plan_revisions WHERE plan_id=? AND revision=?",
                    (request.plan_id, revision),
                ).fetchone()
                self._audit("generation_plan", request.plan_id, "plan.computed", actor_id,
                            {"revision": revision, "feasible": result["feasible"],
                             "input_sha256": input_sha256})
        except sqlite3.IntegrityError as exc:  # 并发计算同一计划的同一修订号
            raise Conflict("计划正在被并发计算，请重试") from exc
        return self._plan_response(row, replayed=False)

    def _plan_response(self, row: sqlite3.Row, *, replayed: bool) -> dict[str, Any]:
        result = json.loads(row["result_json"])
        return {
            "plan_id": row["plan_id"],
            "revision": row["revision"],
            "state": row["state"],
            "trade_date": row["trade_date"],
            "facility_id": row["facility_id"],
            "product": row["product"],
            "curve_id": row["curve_id"],
            "feasible": bool(row["feasible"]),
            "input_sha256": row["input_sha256"],
            "inventory_version_sha256": row["inventory_version_sha256"],
            "approved_inventory_version_sha256": row["approved_inventory_version_sha256"],
            "fuel_unit_cost_cny": row["fuel_unit_cost_cny"],
            "replayed": replayed,
            **result,
        }

    def get_plan(self, actor_id: str, plan_id: str, revision: int | None = None) -> dict[str, Any]:
        self._require(actor_id, "plan.read")
        row = self._plan_row(plan_id, revision)
        return self._plan_response(row, replayed=False)

    def _plan_row(self, plan_id: str, revision: int | None = None) -> sqlite3.Row:
        if revision is None:
            row = self.connection.execute(
                "SELECT * FROM generation_plan_revisions WHERE plan_id=? ORDER BY revision DESC LIMIT 1",
                (plan_id,),
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT * FROM generation_plan_revisions WHERE plan_id=? AND revision=?",
                (plan_id, revision),
            ).fetchone()
        if row is None:
            raise NotFound("计划不存在")
        return row

    def approve_plan(self, actor_id: str, plan_id: str, expected_revision: int) -> dict[str, Any]:
        """批准草稿并以当前库存版本原子预留燃料；重复批准幂等。"""
        self._require(actor_id, "plan.approve")
        row = self.connection.execute(
            "SELECT * FROM generation_plan_revisions WHERE plan_id=? AND revision=?",
            (plan_id, expected_revision),
        ).fetchone()
        if row is None:
            latest = self.connection.execute(
                "SELECT revision,state FROM generation_plan_revisions WHERE plan_id=? ORDER BY revision DESC LIMIT 1",
                (plan_id,),
            ).fetchone()
            if latest is None:
                raise NotFound("计划不存在")
            raise Conflict(f"期望修订 {expected_revision} 不是最新修订 {latest['revision']}")
        if row["state"] == "approved":
            return self._plan_response(row, replayed=True)
        if row["state"] != "draft":
            raise InvalidState("只有草稿计划可以批准")
        if not row["feasible"]:
            raise InvalidState("无解草稿不能批准")
        fuel_demand = Decimal(row["fuel_demand"])
        with transaction(self.connection, immediate=True):
            approval_version, _ = self._inventory_version(row["facility_id"], row["product"])
            if approval_version != row["inventory_version_sha256"]:
                raise Conflict("库存版本自草稿计算后已变化，请重新计算后再批准")
            lots = self.connection.execute(
                "SELECT * FROM inventory_lots WHERE facility_id=? AND product=? ORDER BY received_at,lot_id",
                (row["facility_id"], row["product"]),
            ).fetchall()
            available_total = sum((Decimal(lot["available_mwh"]) for lot in lots), ZERO)
            if available_total < fuel_demand:
                raise Conflict(
                    f"燃料库存不足以批准计划：需要 {decimal_text(fuel_demand)}，"
                    f"可用 {decimal_text(quantize_volume(available_total))}"
                )
            remaining = fuel_demand
            for lot in lots:
                if remaining <= ZERO:
                    break
                take = min(Decimal(lot["available_mwh"]), remaining)
                if take <= ZERO:
                    continue
                self.connection.execute(
                    "UPDATE inventory_lots SET available_mwh=?,revision=revision+1 WHERE lot_id=?",
                    (decimal_text(quantize_volume(Decimal(lot["available_mwh"]) - take)), lot["lot_id"]),
                )
                self.connection.execute(
                    "INSERT INTO plan_fuel_reservations(plan_id,revision,lot_id,lot_revision,reserved_mwh,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (plan_id, expected_revision, lot["lot_id"], lot["revision"] + 1,
                     decimal_text(quantize_volume(take)), self._now()),
                )
                remaining -= take
            cursor = self.connection.execute(
                "UPDATE generation_plan_revisions SET state='approved',approved_by=?,approved_at=?,"
                "approved_inventory_version_sha256=? "
                "WHERE plan_id=? AND revision=? AND state='draft'",
                (actor_id, self._now(), approval_version, plan_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("计划状态已变化")
            self._audit("generation_plan", plan_id, "plan.approved", actor_id,
                        {"revision": expected_revision, "fuel_demand": row["fuel_demand"],
                         "inventory_version": row["inventory_version_sha256"]})
        approved = self.connection.execute(
            "SELECT * FROM generation_plan_revisions WHERE plan_id=? AND revision=?",
            (plan_id, expected_revision),
        ).fetchone()
        return self._plan_response(approved, replayed=False)

    def record_actual(self, actor_id: str, plan_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """登记计划日实际逐时出力，计算与原方案的偏差。重复登记幂等。"""
        self._require(actor_id, "actual.write")
        actual_outputs = raw.get("outputs_mw")
        if not isinstance(actual_outputs, Mapping):
            raise ValidationFailed("outputs_mw 必须是以机组为键的 24 点出力对象")
        series: dict[str, list[Decimal]] = {}
        for uid, values in actual_outputs.items():
            unit_id = identifier(uid, "outputs_mw 键")
            if not isinstance(values, (list, tuple)) or len(values) != 24:
                raise ValidationFailed(f"outputs_mw.{unit_id} 必须是 24 个小时点")
            series[unit_id] = [
                decimal_value(point, f"outputs_mw.{unit_id}[{index}]", minimum=ZERO)
                for index, point in enumerate(values)
            ]
        plan = self.connection.execute(
            "SELECT * FROM generation_plan_revisions WHERE plan_id=? ORDER BY revision DESC LIMIT 1",
            (plan_id,),
        ).fetchone()
        if plan is None:
            raise NotFound("计划不存在")
        if plan["state"] != "approved":
            raise InvalidState("只有已批准计划可以登记实际出力")
        result = json.loads(plan["result_json"])
        known = {row["unit_id"] for row in result["units"]}
        unknown = sorted(set(series) - known)
        if unknown:
            raise ValidationFailed(f"实际出力包含计划外机组: {','.join(unknown)}")
        actual_payload = {
            uid: [decimal_text(value) for value in values] for uid, values in sorted(series.items())
        }
        content = canonical_json(
            {"plan_id": plan_id, "revision": plan["revision"], "outputs_mw": actual_payload}
        )
        content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
        existing = self.connection.execute(
            "SELECT * FROM plan_actuals WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if existing is not None:
            if existing["content_sha256"] != content_sha256:
                raise Conflict("该计划已登记不同内容的实际出力")
            return self._deviation_report(plan, result, json.loads(existing["actual_json"]), replayed=True)

        deviation = self._build_deviation(result, series)
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO plan_actuals(plan_id,actual_json,content_sha256,recorded_by,created_at) "
                "VALUES(?,?,?,?,?)",
                (plan_id, canonical_json(actual_payload), content_sha256, actor_id, self._now()),
            )
            self._audit("generation_plan", plan_id, "plan.actual_recorded", actor_id,
                        {"revision": plan["revision"], "sha256": content_sha256})
        return self._deviation_report(plan, result, actual_payload, replayed=False)

    @staticmethod
    def _build_deviation(result: Mapping[str, Any], actual: Mapping[str, list[Decimal]]) -> dict[str, Any]:
        hours = []
        total_abs = ZERO
        for planned_hour in result["hours"]:
            hour = planned_hour["hour"]
            planned_outputs = {uid: Decimal(value) for uid, value in planned_hour["outputs_mw"].items()}
            actual_outputs = {uid: values[hour] for uid, values in actual.items()}
            unit_rows = []
            for uid in sorted(set(planned_outputs) | set(actual_outputs)):
                planned = planned_outputs.get(uid, ZERO)
                observed = actual_outputs.get(uid, ZERO)
                delta = qmw(observed - planned)
                total_abs += abs(delta)
                unit_rows.append({
                    "unit_id": uid,
                    "planned_mw": decimal_text(planned),
                    "actual_mw": decimal_text(observed),
                    "delta_mw": decimal_text(delta),
                })
            planned_generation = Decimal(planned_hour["generation_mw"])
            actual_generation = sum(actual_outputs.values(), ZERO)
            hours.append({
                "hour": hour,
                "planned_generation_mw": decimal_text(planned_generation),
                "actual_generation_mw": decimal_text(qmw(actual_generation)),
                "generation_delta_mw": decimal_text(qmw(actual_generation - planned_generation)),
                "units": unit_rows,
            })
        actual_payload = {
            uid: [decimal_text(value) for value in values] for uid, values in sorted(actual.items())
        }
        return {
            "actual": actual_payload,
            "hours": hours,
            "total_abs_deviation_mwh": decimal_text(qmw(total_abs)),
        }

    def _deviation_report(
        self, plan: sqlite3.Row, result: Mapping[str, Any], actual_payload: Mapping[str, Any], *, replayed: bool
    ) -> dict[str, Any]:
        actual = {uid: [Decimal(v) for v in values] for uid, values in actual_payload.items()}
        deviation = self._build_deviation(result, actual)
        return {
            "plan_id": plan["plan_id"],
            "plan_revision": plan["revision"],
            "plan_state": plan["state"],
            "replayed": replayed,
            "actual": deviation["actual"],
            "hours": deviation["hours"],
            "total_abs_deviation_mwh": deviation["total_abs_deviation_mwh"],
        }

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
