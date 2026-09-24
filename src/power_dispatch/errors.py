"""供应服务向 API 和 CLI 暴露的稳定错误。"""


class SupplyError(RuntimeError):
    code = "supply_error"
    status = 400


class NotFound(SupplyError):
    code = "not_found"
    status = 404


class Conflict(SupplyError):
    code = "conflict"
    status = 409


class Forbidden(SupplyError):
    code = "forbidden"
    status = 403


class InvalidState(SupplyError):
    code = "invalid_state"
    status = 409


class ValidationFailed(SupplyError):
    code = "validation_failed"
    status = 422


class PlanInfeasible(SupplyError):
    code = "plan_infeasible"
    status = 422

    def __init__(self, conflicts: list[dict]) -> None:
        super().__init__("机组计划无解")
        self.conflicts = conflicts


class PlanApprovalConflict(Conflict):
    code = "fuel_reservation_failed"

    def __init__(self, conflicts: list[dict]) -> None:
        super().__init__("批准失败：燃料库存不足以原子预留")
        self.conflicts = conflicts
