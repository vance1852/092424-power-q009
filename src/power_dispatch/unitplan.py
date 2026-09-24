"""机组次日出力计划的确定性求解。

规则覆盖：最小稳定出力、最大出力、上下爬坡速率、启停成本、
检修禁运窗口、区域负荷平衡与备用比例。求解采用带前瞻的两阶段
贪心：每个时段先根据当前与未来可达出力确定开机集合，再按边际
成本分配负荷。结果使用 Decimal 并量化到 0.001 MWh，保证可重复。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping, Sequence


ZERO = Decimal("0")
HUNDRED = Decimal("100")
VOLUME_QUANTUM = Decimal("0.001")

CONFLICT_LOAD_EXCEEDS_CAPACITY = "LOAD_EXCEEDS_CAPACITY"
CONFLICT_MAINTENANCE_UNREACHABLE = "MAINTENANCE_UNREACHABLE"
CONFLICT_MUST_RUN_BLOCKS_SHED = "MUST_RUN_BLOCKS_SHED"
CONFLICT_RAMP_INSUFFICIENT = "RAMP_INSUFFICIENT"
CONFLICT_MUST_RUN_EXCESS = "MUST_RUN_EXCESS"


def volume(value: Decimal) -> Decimal:
    return value.quantize(VOLUME_QUANTUM, rounding=ROUND_HALF_UP)


def text(value: Decimal) -> str:
    return format(volume(value), "f")


class InfeasiblePlan(Exception):
    """无解计划，携带具体约束冲突集合。"""

    def __init__(self, conflicts: Sequence[Mapping[str, object]]) -> None:
        super().__init__("机组计划无解")
        self.conflicts = [dict(item) for item in conflicts]


@dataclass(frozen=True, slots=True)
class UnitModel:
    unit_id: str
    min_output: Decimal
    max_output: Decimal
    ramp_up: Decimal
    ramp_down: Decimal
    startup_cost: Decimal
    marginal_cost: Decimal
    initial_output: Decimal
    blocked_periods: frozenset[int]


def _period_label(period: int) -> str:
    return f"{period:02d}:00"


def _conflict(code: str, p: int, message: str, evidence: Mapping[str, object]) -> dict[str, object]:
    return {
        "code": code,
        "period": p + 1,
        "period_label": _period_label(p),
        "message": message,
        "evidence": dict(evidence),
    }


def solve(
    *,
    units: Sequence[UnitModel],
    prices: Sequence[Decimal],
    demand: Sequence[Decimal],
    reserve_percent: Decimal,
    periods: int = 24,
) -> dict[str, object]:
    """返回可执行出力序列；无解时抛出 InfeasiblePlan。"""
    if len(prices) != periods or len(demand) != periods:
        raise ValueError("价格与负荷必须覆盖全部计划时段")
    ordered_units = tuple(sorted(units, key=lambda item: item.unit_id))
    by_id = {unit.unit_id: unit for unit in ordered_units}
    reserve_need = [volume(demand[p] * reserve_percent / HUNDRED) for p in range(periods)]

    # 第一阶段检查：不计爬坡时，各时段检修禁运后的总容量必须覆盖负荷与备用。
    static_conflicts: list[dict[str, object]] = []
    for p in range(periods):
        available = [unit for unit in ordered_units if p not in unit.blocked_periods]
        capacity = sum((unit.max_output for unit in available), ZERO)
        target = volume(demand[p] + reserve_need[p])
        if capacity < target:
            static_conflicts.append(_conflict(
                CONFLICT_LOAD_EXCEEDS_CAPACITY, p,
                "可用机组最大出力不能满足区域负荷与备用",
                {
                    "demand_mwh": text(demand[p]),
                    "reserve_required_mwh": text(reserve_need[p]),
                    "required_capacity_mwh": text(target),
                    "available_capacity_mwh": text(capacity),
                    "shortfall_mwh": text(target - capacity),
                    "unavailable_units": [
                        unit.unit_id for unit in ordered_units if p in unit.blocked_periods
                    ],
                },
            ))
    if static_conflicts:
        raise InfeasiblePlan(static_conflicts)

    previous = {unit.unit_id: unit.initial_output for unit in ordered_units}
    period_rows: list[dict[str, object]] = []
    total_generation = ZERO
    total_energy_cost = ZERO
    total_startup_cost = ZERO

    def future_energy_gap(p: int, online: set[str], current: Mapping[str, Decimal]) -> tuple[int, Decimal] | None:
        """从时段 p 的开机状态出发，未来各时段按最快爬坡能否满足负荷。

        在线机组以本时段可达出力（current，调用方传入上限）连续爬坡，
        停机机组可在未来最早的连续可用时段启机。
        """
        for q in range(p + 1, periods):
            achievable = ZERO
            for unit in ordered_units:
                if q in unit.blocked_periods:
                    continue
                if unit.unit_id in online:
                    if any(t in unit.blocked_periods for t in range(p + 1, q + 1)):
                        # 中途被检修打断，按未来冷态启机的最乐观情况估计
                        start = next(
                            (s for s in range(p + 1, q + 1)
                             if not any(t in unit.blocked_periods for t in range(s, q + 1))),
                            None,
                        )
                        if start is None:
                            continue
                        achievable += min(unit.max_output, unit.ramp_up * (q - start + 1))
                    else:
                        achievable += min(unit.max_output, current[unit.unit_id] + unit.ramp_up * (q - p))
                else:
                    start = next(
                        (s for s in range(p + 1, q + 1)
                         if not any(t in unit.blocked_periods for t in range(s, q + 1))),
                        None,
                    )
                    if start is None:
                        continue
                    # 冷态启机首时段出力上限即爬坡速率
                    achievable += min(unit.max_output, unit.ramp_up * (q - start + 1))
            gap = demand[q] - achievable
            if gap > ZERO:
                return q, volume(gap)
        return None

    for p in range(periods):
        lower: dict[str, Decimal] = {}
        upper: dict[str, Decimal] = {}
        for unit in ordered_units:
            if p in unit.blocked_periods:
                if previous[unit.unit_id] > unit.ramp_down:
                    raise InfeasiblePlan([_conflict(
                        CONFLICT_MAINTENANCE_UNREACHABLE, p,
                        "机组进入检修前不能按爬坡速率降到停机",
                        {"unit_id": unit.unit_id,
                         "previous_output_mwh": text(previous[unit.unit_id]),
                         "ramp_down_mwh": text(unit.ramp_down)},
                    )])
                lower[unit.unit_id] = ZERO
                upper[unit.unit_id] = ZERO
                continue
            if previous[unit.unit_id] > ZERO:
                can_stop = previous[unit.unit_id] <= unit.ramp_down
                low = ZERO if can_stop else volume(max(unit.min_output, previous[unit.unit_id] - unit.ramp_down))
                high = volume(min(unit.max_output, previous[unit.unit_id] + unit.ramp_up))
            else:
                low = ZERO
                high = volume(min(unit.max_output, unit.ramp_up)) if unit.ramp_up >= unit.min_output else ZERO
            if p + 1 in unit.blocked_periods:
                high = volume(min(high, unit.ramp_down))
                if low > high:
                    raise InfeasiblePlan([_conflict(
                        CONFLICT_MUST_RUN_BLOCKS_SHED, p,
                        "最小稳定出力高于爬坡下限，无法在检修前停机",
                        {"unit_id": unit.unit_id,
                         "min_output_mwh": text(unit.min_output),
                         "ramp_down_mwh": text(unit.ramp_down)},
                    )])
            lower[unit.unit_id] = low
            upper[unit.unit_id] = high

        # 承诺阶段：必在线机组（lower>0）保持运行，其余按当前与未来需要启机。
        online = {unit.unit_id for unit in ordered_units if lower[unit.unit_id] > ZERO}
        started: set[str] = set()

        def committed_capacity() -> Decimal:
            return sum(
                (by_id[unit_id].max_output for unit_id in online),
                ZERO,
            )

        def dispatch_outputs(committed: set[str]) -> dict[str, Decimal]:
            """按边际成本把当前负荷分配给已承诺机组，作为前瞻的预热基线。"""
            dispatched = {unit.unit_id: ZERO for unit in ordered_units}
            for unit_id in committed:
                dispatched[unit_id] = lower[unit_id]
            remaining = volume(demand[p] - sum(dispatched.values(), ZERO))
            for unit in sorted(
                (by_id[unit_id] for unit_id in committed),
                key=lambda item: (item.marginal_cost, item.startup_cost, item.unit_id),
            ):
                if remaining <= ZERO:
                    break
                room = upper[unit.unit_id] - dispatched[unit.unit_id]
                add = volume(min(room, remaining))
                dispatched[unit.unit_id] += add
                remaining = volume(remaining - add)
            return dispatched

        def start_candidates() -> list[UnitModel]:
            floor = sum((lower[unit_id] for unit_id in online), ZERO)
            return [
                unit for unit in ordered_units
                if unit.unit_id not in online and p not in unit.blocked_periods
                and upper[unit.unit_id] >= unit.min_output
                and floor + unit.min_output <= demand[p]
            ]

        target_capacity = volume(demand[p] + reserve_need[p])

        def look_ahead() -> tuple[int, Decimal] | None:
            return future_energy_gap(p, online, dispatch_outputs(online))

        while committed_capacity() < target_capacity or look_ahead() is not None:
            candidates = start_candidates()
            if not candidates:
                reachable = sum(
                    (upper[unit.unit_id] for unit in ordered_units if p not in unit.blocked_periods),
                    ZERO,
                )
                floor = sum((lower[unit_id] for unit_id in online), ZERO)
                blocked_by_floor = [
                    unit.unit_id for unit in ordered_units
                    if unit.unit_id not in online and p not in unit.blocked_periods
                    and upper[unit.unit_id] >= unit.min_output
                    and floor + unit.min_output > demand[p]
                ]
                if blocked_by_floor:
                    raise InfeasiblePlan([_conflict(
                        CONFLICT_MUST_RUN_EXCESS, p,
                        "为满足未来负荷需要提前启机，但当前负荷不能容纳机组最小稳定出力",
                        {"demand_mwh": text(demand[p]),
                         "must_run_output_mwh": text(floor),
                         "candidate_units": sorted(blocked_by_floor),
                         "online_units": sorted(online)},
                    )])
                evidence: dict[str, object] = {
                    "demand_mwh": text(demand[p]),
                    "reserve_required_mwh": text(reserve_need[p]),
                    "committed_capacity_mwh": text(committed_capacity()),
                    "reachable_capacity_mwh": text(reachable),
                    "online_units": sorted(online),
                }
                future = look_ahead()
                if committed_capacity() >= target_capacity and future is not None:
                    future_period, gap = future
                    evidence["future_period"] = future_period + 1
                    evidence["future_period_label"] = _period_label(future_period)
                    evidence["future_demand_mwh"] = text(demand[future_period])
                    evidence["future_energy_gap_mwh"] = text(gap)
                raise InfeasiblePlan([_conflict(
                    CONFLICT_RAMP_INSUFFICIENT, p,
                    "受爬坡速率限制，在役与可启动机组不能达到当前或未来负荷要求",
                    evidence,
                )])
            choice: tuple[Decimal, str] | None = None
            for unit in candidates:
                if committed_capacity() < target_capacity:
                    # 上一时段仍在役的机组留运不重复计启停成本
                    startup = ZERO if previous[unit.unit_id] > ZERO else unit.startup_cost
                    spread = startup / unit.min_output if unit.min_output > ZERO else ZERO
                    rank = volume(unit.marginal_cost + spread)
                else:
                    # 未来需要提前开机：优先边际成本低的机组
                    rank = unit.marginal_cost
                candidate_key = (rank, unit.unit_id)
                if choice is None or candidate_key < choice:
                    choice = candidate_key
            assert choice is not None
            chosen = by_id[choice[1]]
            online.add(chosen.unit_id)
            if previous[chosen.unit_id] == ZERO:
                started.add(chosen.unit_id)
            lower[chosen.unit_id] = chosen.min_output

        # 出力阶段：在线机组的最小出力之和不能超过负荷（不允许超发）。
        floor = sum((lower[unit_id] for unit_id in online), ZERO)
        if floor > demand[p]:
            raise InfeasiblePlan([_conflict(
                CONFLICT_MUST_RUN_EXCESS, p,
                "在役机组最小稳定出力之和高于区域负荷，无法压出力",
                {"demand_mwh": text(demand[p]),
                 "must_run_output_mwh": text(floor),
                 "excess_mwh": text(floor - demand[p]),
                 "online_units": sorted(online)},
            )])
        output = dispatch_outputs(online)
        merit_order = sorted(
            ordered_units,
            key=lambda item: (item.marginal_cost, item.startup_cost, item.unit_id),
        )
        remaining_demand = volume(demand[p] - sum(
            (output[unit_id] for unit_id in online), ZERO))
        if remaining_demand > ZERO:
            raise InfeasiblePlan([_conflict(
                CONFLICT_RAMP_INSUFFICIENT, p,
                "在线机组受爬坡上限约束不能覆盖区域负荷",
                {"demand_mwh": text(demand[p]),
                 "reserve_required_mwh": text(reserve_need[p]),
                 "committed_output_mwh": text(sum(
                     (output[unit_id] for unit_id in online), ZERO)),
                 "shortfall_mwh": text(remaining_demand)},
            )])

        period_total = ZERO
        unit_output_rows: list[dict[str, object]] = []
        for unit in merit_order:
            value = volume(output[unit.unit_id]) if unit.unit_id in online else ZERO
            is_started = unit.unit_id in started
            startup_cost = unit.startup_cost if is_started else ZERO
            energy_cost = volume(value * unit.marginal_cost)
            total_energy_cost += energy_cost
            total_startup_cost += startup_cost
            total_generation += value
            period_total += value
            row = {
                "unit_id": unit.unit_id,
                "committed": value > ZERO,
                "started": is_started,
                "output_mwh": text(value),
                "startup_cost_cny": format(startup_cost.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f"),
                "energy_cost_cny": format(energy_cost.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f"),
            }
            unit_output_rows.append(row)
        period_rows.append({
            "period": p + 1,
            "period_label": _period_label(p),
            "price_cny": format(prices[p].quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f"),
            "demand_mwh": text(demand[p]),
            "reserve_required_mwh": text(reserve_need[p]),
            "total_output_mwh": text(period_total),
            "units": unit_output_rows,
        })
        previous = output

    return {
        "periods": period_rows,
        "total_generation_mwh": text(total_generation),
        "energy_cost_cny": format(total_energy_cost.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f"),
        "startup_cost_cny": format(total_startup_cost.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f"),
        "estimated_cost_cny": format(
            (total_energy_cost + total_startup_cost).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f"
        ),
    }


def deviation_report(
    schedule: Sequence[Mapping[str, object]],
    actuals: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """对比计划出力与实际出力，给出逐点和总量偏差证据。"""
    planned: dict[tuple[int, str], Decimal] = {}
    for row in schedule:
        planned[(int(row["period"]), str(row["unit_id"]))] = Decimal(str(row["output_mwh"]))
    actual_map: dict[tuple[int, str], Decimal] = {}
    for row in actuals:
        actual_map[(int(row["period"]), str(row["unit_id"]))] = Decimal(str(row["output_mwh"]))

    rows: list[dict[str, object]] = []
    total_abs = ZERO
    max_abs = ZERO
    max_key: tuple[int, str] | None = None
    for key in sorted(actual_map):
        period, unit_id = key
        planned_value = planned.get(key, ZERO)
        actual_value = volume(actual_map[key])
        delta = volume(actual_value - planned_value)
        abs_delta = abs(delta)
        total_abs += abs_delta
        if abs_delta > max_abs:
            max_abs = abs_delta
            max_key = key
        rows.append({
            "period": period,
            "period_label": _period_label(period - 1),
            "unit_id": unit_id,
            "planned_mwh": text(planned_value),
            "actual_mwh": text(actual_value),
            "delta_mwh": text(delta),
            "abs_delta_mwh": text(abs_delta),
        })
    missing = [
        {"period": period, "period_label": _period_label(period - 1), "unit_id": unit_id}
        for period, unit_id in sorted(planned.keys() - actual_map.keys())
    ]
    planned_total = sum(planned.get(key, ZERO) for key in actual_map) + sum(
        value for key, value in planned.items() if key not in actual_map
    )
    actual_total = sum(actual_map.values(), ZERO)
    return {
        "rows": rows,
        "missing_actuals": missing,
        "reported_points": len(actual_map),
        "planned_points": len(planned),
        "planned_total_mwh": text(planned_total),
        "actual_total_mwh": text(volume(actual_total)),
        "total_abs_deviation_mwh": text(total_abs),
        "max_abs_deviation_mwh": text(max_abs),
        "max_deviation_point": None if max_key is None else {
            "period": max_key[0], "unit_id": max_key[1],
        },
    }
