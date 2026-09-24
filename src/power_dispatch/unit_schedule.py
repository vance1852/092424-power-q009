"""次日机组出力序列的确定性求解器。

输入为 24 点峰谷电价、区域负荷、机组参数（最小稳定出力、最大容量、
爬坡速率、热耗率、启停成本）与检修禁运窗口，输出可执行的逐时出力序列。

求解采用逐时动态规划：

1. 每个小时的状态是该小时在线机组集合；在线机组总容量必须覆盖负荷并
   保留 ``reserve_percent`` 的容量备用，检修机组全时段不可用。
2. 状态之间必须满足最小稳定出力与爬坡速率；给定在线组合后按边际成本
   （热耗率 × 燃料单价）经济调度满足负荷。
3. 当小时峰段电价高于边际成本、且扣除备用后仍有容量与爬坡余量时，按
   便宜机组优先超发外送；谷段仅满足负荷。
4. 路径净额 = 外送电收入 - 发电燃料成本 - 启停成本，选择全天净额最优路径。

同一在线组合可能由不同前驱到达、形成不同出力向量，未来小时的爬坡可行性
依赖具体出力，因此保留全部互不支配的路径（净额更优且每台机组下一小时
可达区间更宽者支配其他路径）。算法不依赖第三方库，全部使用 ``Decimal``，
无解时返回结构化的约束冲突证据。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping, Sequence

from .errors import ValidationFailed
from .models import date_text, decimal_value, identifier, required_text

HOURS = tuple(range(24))
ZERO = Decimal("0")
HUNDRED = Decimal("100")
TOLERANCE = Decimal("0.0005")


def qmw(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def qmoney(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def qrate(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class UnitParameters:
    unit_id: str
    name: str
    min_stable_mw: Decimal
    max_capacity_mw: Decimal
    ramp_mw_per_hour: Decimal
    heat_rate: Decimal
    startup_cost_cny: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "UnitParameters":
        minimum = decimal_value(raw.get("min_stable_mw"), "min_stable_mw", minimum=ZERO)
        maximum = decimal_value(raw.get("max_capacity_mw"), "max_capacity_mw", minimum=Decimal("0.001"))
        if maximum < minimum:
            raise ValidationFailed("max_capacity_mw 不能小于 min_stable_mw")
        ramp = decimal_value(raw.get("ramp_mw_per_hour"), "ramp_mw_per_hour", minimum=Decimal("0.001"))
        return cls(
            unit_id=identifier(raw.get("unit_id"), "unit_id"),
            name=required_text(raw.get("name"), "name"),
            min_stable_mw=qmw(minimum),
            max_capacity_mw=qmw(maximum),
            ramp_mw_per_hour=qmw(ramp),
            heat_rate=qrate(decimal_value(raw.get("heat_rate"), "heat_rate", minimum=ZERO)),
            startup_cost_cny=qmoney(
                decimal_value(raw.get("startup_cost_cny"), "startup_cost_cny", minimum=ZERO)
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "unit_id": self.unit_id,
            "name": self.name,
            "min_stable_mw": format(self.min_stable_mw, "f"),
            "max_capacity_mw": format(self.max_capacity_mw, "f"),
            "ramp_mw_per_hour": format(self.ramp_mw_per_hour, "f"),
            "heat_rate": format(self.heat_rate, "f"),
            "startup_cost_cny": format(self.startup_cost_cny, "f"),
        }


@dataclass(frozen=True, slots=True)
class PlanRequest:
    plan_id: str
    trade_date: str
    facility_id: str
    product: str
    reserve_percent: Decimal
    prices: tuple[Decimal, ...]
    load_mw: tuple[Decimal, ...]
    initial_output_mw: Mapping[str, Decimal]
    quote_id: int | None
    unit_ids: tuple[str, ...] | None

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "PlanRequest":
        quote_id = raw.get("quote_id")
        if quote_id is not None:
            if isinstance(quote_id, bool) or not isinstance(quote_id, int) or quote_id <= 0:
                raise ValidationFailed("quote_id 必须是正整数")
        reserve = decimal_value(
            raw.get("reserve_percent", 0), "reserve_percent", minimum=ZERO, maximum=HUNDRED
        )
        prices = cls._series(raw.get("prices"), "prices")
        load = cls._series(raw.get("load_mw"), "load_mw", minimum=ZERO)
        initial = raw.get("initial_output_mw", {})
        if not isinstance(initial, Mapping):
            raise ValidationFailed("initial_output_mw 必须是对象")
        initial_output = {
            identifier(key, "initial_output_mw 键"): qmw(
                decimal_value(value, f"initial_output_mw.{key}", minimum=ZERO)
            )
            for key, value in initial.items()
        }
        unit_ids = raw.get("unit_ids")
        if unit_ids is not None:
            if not isinstance(unit_ids, (list, tuple)) or not unit_ids:
                raise ValidationFailed("unit_ids 必须是非空数组")
            unit_ids = tuple(identifier(value, "unit_ids[]") for value in unit_ids)
            if len(set(unit_ids)) != len(unit_ids):
                raise ValidationFailed("unit_ids 不能重复")
        return cls(
            plan_id=identifier(raw.get("plan_id"), "plan_id"),
            trade_date=date_text(raw.get("trade_date"), "trade_date"),
            facility_id=identifier(raw.get("facility_id"), "facility_id"),
            product=required_text(raw.get("product"), "product", 32),
            reserve_percent=reserve,
            prices=prices,
            load_mw=load,
            initial_output_mw=initial_output,
            quote_id=quote_id,
            unit_ids=unit_ids,
        )

    @staticmethod
    def _series(value: object, field: str, *, minimum: Decimal | None = None) -> tuple[Decimal, ...]:
        if not isinstance(value, (list, tuple)) or len(value) != 24:
            raise ValidationFailed(f"{field} 必须是 24 个小时点")
        return tuple(
            qmw(decimal_value(point, f"{field}[{index}]", minimum=minimum))
            for index, point in enumerate(value)
        )


def _conflict(code: str, hour: int | None, message: str, **evidence: object) -> dict[str, object]:
    row: dict[str, object] = {"code": code, "hour": hour, "message": message}
    row.update(evidence)
    return row


def structural_conflicts(
    units: Sequence[UnitParameters],
    blocked: Mapping[str, frozenset[int]],
    load_mw: Sequence[Decimal],
    reserve_mw: Sequence[Decimal],
) -> list[dict[str, object]]:
    """检查与组合路径无关的硬性冲突：负荷与备用容量。"""
    conflicts: list[dict[str, object]] = []
    for hour in HOURS:
        available = [unit for unit in units if hour not in blocked.get(unit.unit_id, frozenset())]
        capacity = sum((unit.max_capacity_mw for unit in available), ZERO)
        names = sorted(unit.unit_id for unit in available)
        if capacity < load_mw[hour]:
            conflicts.append(_conflict(
                "load_capacity_shortfall", hour,
                "可用机组最大容量低于区域负荷",
                load_mw=format(load_mw[hour], "f"),
                available_capacity_mw=format(qmw(capacity), "f"),
                shortfall_mw=format(qmw(load_mw[hour] - capacity), "f"),
                available_units=names,
            ))
        elif capacity < load_mw[hour] + reserve_mw[hour]:
            conflicts.append(_conflict(
                "reserve_capacity_shortfall", hour,
                "可用机组容量不满足负荷加备用比例",
                load_mw=format(load_mw[hour], "f"),
                reserve_mw=format(reserve_mw[hour], "f"),
                required_capacity_mw=format(qmw(load_mw[hour] + reserve_mw[hour]), "f"),
                available_capacity_mw=format(qmw(capacity), "f"),
                shortfall_mw=format(qmw(load_mw[hour] + reserve_mw[hour] - capacity), "f"),
                available_units=names,
            ))
    return conflicts


def _dispatch(
    committed: Sequence[UnitParameters],
    *,
    load: Decimal,
    capacity_total: Decimal,
    reserve_need: Decimal,
    price: Decimal,
    fuel_unit_cost: Decimal,
    previous: Mapping[str, Decimal],
    merit_order: Sequence[UnitParameters],
) -> dict[str, object] | None:
    """给定在线组合做经济调度；无法满足负荷或爬坡时返回 None。

    先按边际成本从低到高填满负荷，再在 ``总容量 - 备用需求`` 的上限内，
    仅对边际成本低于当前电价的机组超发外送。
    """
    bounds: dict[str, tuple[Decimal, Decimal]] = {}
    for unit in committed:
        prev = previous.get(unit.unit_id, ZERO)
        low = max(unit.min_stable_mw, prev - unit.ramp_mw_per_hour)
        high = min(unit.max_capacity_mw, prev + unit.ramp_mw_per_hour)
        if high < unit.min_stable_mw - TOLERANCE:  # 爬坡不足以带上最小稳定出力
            return None
        bounds[unit.unit_id] = (low, high)
    floor = sum((low for low, _ in bounds.values()), ZERO)
    ramp_ceiling = sum((high for _, high in bounds.values()), ZERO)
    # 备用按在线容量预留：总出力不得超过 总容量 - 备用需求。
    ceiling = min(ramp_ceiling, capacity_total - reserve_need)
    if load > ceiling + TOLERANCE:
        return None
    # 爬坡下限可能高于负荷（高位外送后来不及降出力），超出部分作为强制外送。
    must_generate = max(load, floor)
    if must_generate > ceiling + TOLERANCE:
        return None

    output = {uid: low for uid, (low, _) in bounds.items()}

    def fill(amount: Decimal, *, profitable_only: bool) -> Decimal:
        """按边际成本从低到高填充出力，返回未填完的余量。"""
        remaining = amount
        for unit in merit_order:
            if unit.unit_id not in bounds or remaining <= TOLERANCE:
                continue
            if profitable_only and unit.heat_rate * fuel_unit_cost >= price:
                continue
            high = bounds[unit.unit_id][1]
            added = min(high - output[unit.unit_id], max(ZERO, remaining))
            output[unit.unit_id] += added
            remaining -= added
        return remaining

    # 先保证必发出力（负荷与爬坡下限中的较大者），再按盈利空间填满外送。
    if fill(max(ZERO, must_generate - floor), profitable_only=False) > TOLERANCE:
        return None
    fill(max(ZERO, ceiling - must_generate), profitable_only=True)

    final = {uid: qmw(value) for uid, value in output.items()}
    generation = qmw(sum(final.values(), ZERO))
    fuel = qmw(sum(
        (value * next(unit.heat_rate for unit in committed if unit.unit_id == uid)
         for uid, value in final.items()),
        ZERO,
    ))
    energy_cost = qmoney(fuel * fuel_unit_cost)
    export = qmw(max(ZERO, generation - load))
    revenue = qmoney(export * price)
    return {
        "outputs": final,
        "generation_mw": generation,
        "export_mw": export,
        "fuel": fuel,
        "energy_cost": energy_cost,
        "revenue": revenue,
    }


def _dominates(
    candidate_output: Mapping[str, Decimal],
    candidate_cost: Decimal,
    other_output: Mapping[str, Decimal],
    other_cost: Decimal,
    units_by_id: Mapping[str, UnitParameters],
) -> bool:
    """候选路径是否支配另一条：净额不更差，且每台机组下一小时可达区间不更窄。"""
    if candidate_cost > other_cost + TOLERANCE:
        return False
    for uid, value in other_output.items():
        candidate = candidate_output.get(uid, ZERO)
        unit = units_by_id[uid]
        candidate_low = max(unit.min_stable_mw, candidate - unit.ramp_mw_per_hour)
        other_low = max(unit.min_stable_mw, value - unit.ramp_mw_per_hour)
        candidate_high = min(unit.max_capacity_mw, candidate + unit.ramp_mw_per_hour)
        other_high = min(unit.max_capacity_mw, value + unit.ramp_mw_per_hour)
        if candidate_low > other_low + TOLERANCE or candidate_high < other_high - TOLERANCE:
            return False
    return True


def _output_key(outputs: Mapping[str, Decimal]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((uid, format(value, "f")) for uid, value in outputs.items()))


def solve_schedule(
    *,
    units: Sequence[UnitParameters],
    blocked_hours: Mapping[str, frozenset[int]],
    prices: Sequence[Decimal],
    load_mw: Sequence[Decimal],
    reserve_percent: Decimal,
    fuel_unit_cost: Decimal,
    initial_output: Mapping[str, Decimal] | None = None,
) -> dict[str, object]:
    """返回 ``feasible`` 标志、逐时计划和约束冲突证据。"""
    if not units:
        raise ValueError("至少需要一台机组")
    if len(units) > 12:
        raise ValueError("单次计划最多支持 12 台机组")
    units_by_id = {unit.unit_id: unit for unit in units}
    initial_output = dict(initial_output or {})
    for uid, output in initial_output.items():
        if uid not in units_by_id:
            raise ValueError(f"初始出力引用了未知机组 {uid}")
        unit = units_by_id[uid]
        if ZERO < output < unit.min_stable_mw - TOLERANCE or output > unit.max_capacity_mw + TOLERANCE:
            raise ValueError(f"机组 {uid} 的初始出力不在停机或稳定区间内")
    reserve = [qmw(load * reserve_percent / HUNDRED) for load in load_mw]
    initial_state = frozenset(uid for uid, output in initial_output.items() if output > ZERO)

    conflicts: list[dict[str, object]] = []
    for uid in sorted(initial_state):
        if 0 in blocked_hours.get(uid, frozenset()):
            conflicts.append(_conflict(
                "initial_state_under_maintenance", 0,
                "前一日末在线机组在计划首小时处于检修禁运窗口",
                unit_id=uid,
                initial_output_mw=format(initial_output[uid], "f"),
            ))
    conflicts.extend(structural_conflicts(units, blocked_hours, load_mw, reserve))
    if conflicts:
        return {"feasible": False, "conflicts": conflicts, "hours": []}

    merit_order = tuple(sorted(units, key=lambda u: (u.heat_rate, u.unit_id)))
    available_by_hour = {
        hour: frozenset(
            unit.unit_id for unit in units if hour not in blocked_hours.get(unit.unit_id, frozenset())
        )
        for hour in HOURS
    }

    # traces[hour][state] 保存到达该状态的互不支配节点；
    # 路径之间的爬坡可达性不同，不能只保留成本最低的一条。每个节点通过
    # parent 直接引用生成它的前驱，保证回溯链逐小时爬坡可行。
    traces: dict[int, dict[frozenset[str], list[dict[str, object]]]] = {}
    cold = {"state": initial_state, "outputs": dict(initial_output), "net_cost": ZERO, "parent": None}
    for hour in HOURS:
        candidates = sorted(available_by_hour[hour], key=lambda uid: (units_by_id[uid].heat_rate, uid))
        previous = [cold] if hour == 0 else [
            trace for state_traces in traces[hour - 1].values() for trace in state_traces
        ]
        reachable: dict[frozenset[str], list[dict[str, object]]] = {}
        for mask in range(1, 1 << len(candidates)):
            committed_ids = frozenset(
                uid for index, uid in enumerate(candidates) if mask & (1 << index)
            )
            committed = [units_by_id[uid] for uid in committed_ids]
            capacity_total = sum((unit.max_capacity_mw for unit in committed), ZERO)
            if capacity_total < load_mw[hour] + reserve[hour] - TOLERANCE:
                continue
            arrived: list[dict[str, object]] = []
            for prev in previous:
                shutting_down = prev["state"] - committed_ids
                if any(
                    prev["outputs"].get(uid, ZERO) > units_by_id[uid].ramp_mw_per_hour + TOLERANCE
                    for uid in shutting_down
                ):
                    continue
                dispatched = _dispatch(
                    committed,
                    load=load_mw[hour],
                    capacity_total=capacity_total,
                    reserve_need=reserve[hour],
                    price=prices[hour],
                    fuel_unit_cost=fuel_unit_cost,
                    previous=prev["outputs"],
                    merit_order=merit_order,
                )
                if dispatched is None:
                    continue
                startups = committed_ids - prev["state"]
                startup_cost = sum(
                    (units_by_id[uid].startup_cost_cny for uid in startups), ZERO
                )
                arrived.append({
                    "state": committed_ids,
                    "outputs": dispatched["outputs"],
                    "net_cost": prev["net_cost"] + dispatched["energy_cost"]
                    + startup_cost - dispatched["revenue"],
                    "parent": prev,
                    "startups": sorted(startups),
                    "startup_cost": startup_cost,
                    "dispatch": dispatched,
                })
            pruned: list[dict[str, object]] = []
            for trace in sorted(arrived, key=lambda item: (item["net_cost"], _output_key(item["outputs"]))):
                key = _output_key(trace["outputs"])
                if any(_output_key(other["outputs"]) == key for other in pruned):
                    continue
                if any(
                    _dominates(other["outputs"], other["net_cost"], trace["outputs"], trace["net_cost"], units_by_id)
                    for other in pruned
                ):
                    continue
                pruned = [
                    other for other in pruned
                    if not _dominates(trace["outputs"], trace["net_cost"], other["outputs"], other["net_cost"], units_by_id)
                ]
                pruned.append(trace)
            if pruned:
                reachable[committed_ids] = pruned
        if not reachable:
            conflicts.append(_conflict(
                "ramp_or_min_stable_infeasible", hour,
                "不存在同时满足最小稳定出力与爬坡速率的在线组合",
                load_mw=format(load_mw[hour], "f"),
                reserve_mw=format(reserve[hour], "f"),
                available_units=sorted(available_by_hour[hour]),
            ))
            return {"feasible": False, "conflicts": conflicts, "hours": []}
        traces[hour] = reachable

    # 沿父指针回溯全天净额最优的路径。
    endings = [trace for state_traces in traces[23].values() for trace in state_traces]
    current = min(endings, key=lambda item: (item["net_cost"], _output_key(item["outputs"])))
    chosen: dict[int, dict[str, object]] = {}
    for hour in range(23, -1, -1):
        chosen[hour] = current
        current = current["parent"]

    hours: list[dict[str, object]] = []
    unit_totals = {
        unit.unit_id: {"generation_mwh": ZERO, "fuel": ZERO, "startups": 0}
        for unit in units
    }
    total_startup_cost = ZERO
    for hour in HOURS:
        trace = chosen[hour]
        dispatched = trace["dispatch"]
        for uid, output in dispatched["outputs"].items():
            unit_totals[uid]["generation_mwh"] += output
            unit_totals[uid]["fuel"] += output * units_by_id[uid].heat_rate
        for uid in trace["startups"]:
            unit_totals[uid]["startups"] += 1
        total_startup_cost += trace["startup_cost"]
        hours.append({
            "hour": hour,
            "price_cny_per_mwh": format(prices[hour], "f"),
            "load_mw": format(load_mw[hour], "f"),
            "reserve_mw": format(reserve[hour], "f"),
            "committed_unit_ids": sorted(trace["state"]),
            "startup_unit_ids": trace["startups"],
            "outputs_mw": {uid: format(output, "f") for uid, output in sorted(dispatched["outputs"].items())},
            "generation_mw": format(dispatched["generation_mw"], "f"),
            "export_mw": format(dispatched["export_mw"], "f"),
            "energy_cost_cny": format(dispatched["energy_cost"], "f"),
            "revenue_cny": format(dispatched["revenue"], "f"),
            "startup_cost_cny": format(trace["startup_cost"], "f"),
        })

    total_generation = sum((item["generation_mwh"] for item in unit_totals.values()), ZERO)
    total_export = sum((Decimal(hour["export_mw"]) for hour in hours), ZERO)
    total_fuel = sum((item["fuel"] for item in unit_totals.values()), ZERO)
    total_energy_cost = sum((Decimal(hour["energy_cost_cny"]) for hour in hours), ZERO)
    total_revenue = sum((Decimal(hour["revenue_cny"]) for hour in hours), ZERO)
    unit_rows = []
    for unit in merit_order:
        totals = unit_totals[unit.unit_id]
        unit_rows.append({
            "unit_id": unit.unit_id,
            "generation_mwh": format(qmw(totals["generation_mwh"]), "f"),
            "fuel_demand": format(qmw(totals["fuel"]), "f"),
            "startups": totals["startups"],
        })
    return {
        "feasible": True,
        "conflicts": [],
        "hours": hours,
        "units": unit_rows,
        "total_generation_mwh": format(qmw(total_generation), "f"),
        "total_export_mwh": format(qmw(total_export), "f"),
        "fuel_demand": format(qmw(total_fuel), "f"),
        "energy_cost_cny": format(qmoney(total_energy_cost), "f"),
        "startup_cost_cny": format(qmoney(total_startup_cost), "f"),
        "revenue_cny": format(qmoney(total_revenue), "f"),
        "total_cost_cny": format(qmoney(total_energy_cost + total_startup_cost), "f"),
        "gross_margin_cny": format(qmoney(total_revenue - total_energy_cost - total_startup_cost)),
    }
