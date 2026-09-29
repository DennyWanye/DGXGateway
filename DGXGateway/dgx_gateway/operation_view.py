from __future__ import annotations

from typing import Any


STEPS = ("准备", "停止源模型", "启动目标模型", "健康验证", "安全收尾")


_PHASES: dict[str, tuple[int, int, str]] = {
    "REQUESTED": (4, 0, "切换请求已受理"),
    "PREFLIGHT": (10, 0, "正在执行安全预检"),
    "LEASE_ACQUIRED": (16, 0, "已取得唯一部署锁"),
    "SOURCE_DRAINING": (25, 1, "正在排空源模型请求"),
    "SOURCE_STOPPING": (35, 1, "正在停止源模型"),
    "SOURCE_STOPPED_PROVEN": (42, 1, "已证明源模型完全停止"),
    "TARGET_STARTING": (55, 2, "正在启动目标模型"),
    "TARGET_HEALTH": (75, 3, "正在验证目标模型健康状态"),
    "TARGET_HEALTHY": (88, 3, "目标模型已通过健康验证"),
    "TARGET_RETIRING": (88, 3, "目标启动失败，正在安全清理"),
    "TARGET_HEAD_FAIL_CLOSING": (88, 3, "工作节点不可用，正在关闭主节点目标服务"),
    "TARGET_HEAD_FAIL_CLOSED": (89, 3, "主节点已进入受认证维护状态"),
    "TARGET_STOPPED_PROVEN": (89, 3, "已证明失败目标完全停止"),
    "ROLLBACK_STARTING": (90, 3, "正在恢复原模型"),
    "SOURCE_HEALTHY_AND_OTHERS_STOPPED_PROVEN": (92, 3, "原模型已恢复并通过验证"),
    "CANCEL_SAFE_STOPPED_PROVEN": (86, 3, "取消已确认，集群处于安全停止状态"),
    "CANCEL_ROLLBACK_STARTING": (86, 3, "正在取消并恢复原模型"),
    "CANCEL_SOURCE_RESTORED_PROVEN": (92, 3, "取消完成，原模型已恢复"),
    "RELEASING": (96, 4, "正在释放唯一部署锁"),
}

_NON_TERMINAL_OUTCOMES: dict[str, frozenset[str | None]] = {
    "REQUESTED": frozenset((None, "COMMITTED")),
    "PREFLIGHT": frozenset((None,)),
    "LEASE_ACQUIRED": frozenset((None,)),
    "SOURCE_DRAINING": frozenset((None,)),
    "SOURCE_STOPPING": frozenset((None,)),
    "SOURCE_STOPPED_PROVEN": frozenset((None,)),
    "TARGET_STARTING": frozenset((None,)),
    "TARGET_HEALTH": frozenset((None,)),
    "TARGET_HEALTHY": frozenset((None, "COMMITTED")),
    "TARGET_RETIRING": frozenset((None,)),
    "TARGET_HEAD_FAIL_CLOSING": frozenset((None,)),
    "TARGET_HEAD_FAIL_CLOSED": frozenset((None,)),
    "TARGET_STOPPED_PROVEN": frozenset((None, "FAILED_SAFE_STOPPED")),
    "ROLLBACK_STARTING": frozenset((None,)),
    "SOURCE_HEALTHY_AND_OTHERS_STOPPED_PROVEN": frozenset((None, "ROLLED_BACK")),
    "CANCEL_SAFE_STOPPED_PROVEN": frozenset((None, "CANCELED")),
    "CANCEL_ROLLBACK_STARTING": frozenset((None,)),
    "CANCEL_SOURCE_RESTORED_PROVEN": frozenset((None, "CANCELED")),
    "RELEASING": frozenset(("COMMITTED", "ROLLED_BACK", "FAILED_SAFE_STOPPED", "CANCELED")),
}

_TERMINAL: dict[tuple[str, str | None], tuple[str, str]] = {
    ("RELEASED", "COMMITTED"): ("success", "切换完成，目标模型已投入使用"),
    ("RELEASED", "ROLLED_BACK"): ("rollback", "切换未完成，已安全恢复原模型"),
    ("RELEASED", "FAILED_SAFE_STOPPED"): ("danger", "切换失败，相关模型已安全停止"),
    ("RELEASED", "CANCELED"): ("neutral", "切换已取消，集群已回到安全状态"),
    ("RELEASED", "RECOVERY_REQUIRED"): (
        "rollback", "切换事务已人工核验并安全收尾，请以当前模型状态为准"
    ),
    ("CANCELED", None): ("neutral", "切换正在取消"),
    ("CANCELED", "CANCELED"): ("neutral", "切换已取消"),
}


def _base(operation: Any) -> dict[str, Any]:
    return {
        "operation_id": operation.operation_id,
        "source_model": operation.source_model,
        "source_profile": getattr(operation, "source_profile", None),
        "target_model": operation.target_model,
        "target_profile": operation.target_profile,
        "phase": operation.phase,
        "outcome": operation.outcome,
        "cancel_requested": bool(operation.cancel_requested),
        "created_at": operation.created_at,
        "updated_at": operation.updated_at,
        "error": "operation_failed" if operation.error else None,
    }


def operation_view(operation: Any) -> dict[str, Any]:
    """Return the stable, redacted read-only UI representation of an operation."""
    value = _base(operation)
    phase = operation.phase
    outcome = operation.outcome
    if getattr(operation, "parent_job_id", None):
        value.update(parent_job_id=operation.parent_job_id, parent_lease_epoch=operation.parent_lease_epoch,
                     parent_lease_generation=operation.parent_lease_generation, resource_status="held_by_parent")

    if phase == "DELEGATED_COMPLETED":
        percent, step_index, message, tone, terminal, known = (
            100, 4, "本步骤已完成，父文本任务继续持有模型资源", "neutral", True, True
        )
        value["resource_status"] = "held_by_parent"
    elif phase == "RECOVERY_REQUIRED":
        percent, step_index, message, tone, terminal, known = (
            100, 4, "需要人工恢复确认，新的切换已锁定", "critical", True, True
        )
    elif (phase, outcome) in _TERMINAL:
        tone, message = _TERMINAL[(phase, outcome)]
        if phase == "RELEASED" and outcome == "COMMITTED" and operation.target_model is None:
            message = "模型已停止，内存释放检查通过"
        percent, step_index, terminal, known = 100, 4, True, True
    elif phase in _PHASES and outcome in _NON_TERMINAL_OUTCOMES.get(phase, frozenset()):
        percent, step_index, message = _PHASES[phase]
        tone, terminal, known = "working", False, True
    else:
        percent, step_index, message, tone, terminal, known = (
            100, 4, "状态无法识别，已禁止新的切换", "critical", False, False
        )

    value["progress"] = {
        "percent": percent,
        "step": STEPS[step_index],
        "step_index": step_index,
        "steps": list(STEPS),
        "message": message,
        "tone": tone,
        "terminal": terminal,
        "known": known,
    }
    return value
