"""Program-level Termination Controller — decides whether the team may stop.

Design principles (EXOBoost research report):
- "Termination Controller 建议独立于 LLM"：a model may REQUEST close, but the
  PROGRAM decides. ``can_close`` is a pure deterministic gate over a
  ``TerminationState``; no LLM, no I/O.
- Termination is NOT 100% unanimity. It is:
  ``Stop = BudgetExceeded ∨ (Coverage ≥ θc ∧ CriticalOpenIssues = 0 ∧
  VerifierQueue = 0 ∧ Quorum ≥ θq)`` — encoded as an ordered gate where any
  hard reject short-circuits before the quorum check.
- Coverage is risk-weighted: critical coverage is a hard threshold, while the
  weighted coverage carries the soft threshold.

Self-contained: stdlib only.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Decision(str, Enum):
    APPROVE = "approve"
    PENDING = "pending"
    REJECT = "reject"


@dataclass
class TerminationState:
    """Everything the controller needs to decide. Derived, never raw text."""

    budget_exceeded: bool = False
    critical_coverage: float = 0.0
    weighted_coverage: float = 0.0
    pending_verifications: int = 0
    open_critical_issues: int = 0
    quorum: float = 0.0
    conflicts: int = 0


@dataclass
class TerminationPolicy:
    """Thresholds for the stop gate. Overridable per-run."""

    critical_coverage: float = 1.0
    weighted_coverage: float = 0.95
    quorum: float = 0.8
    max_pending_verifications: int = 0


_DECISION_LABEL = {
    Decision.APPROVE: "通过",
    Decision.PENDING: "待定",
    Decision.REJECT: "拒绝",
}


class TerminationController:
    """Pure decision gate over ``TerminationState``."""

    def __init__(self, policy: TerminationPolicy | None = None) -> None:
        self.policy = policy if policy is not None else TerminationPolicy()

    def can_close(self, state: TerminationState) -> tuple[Decision, str]:
        """The report's stop gate, in priority order.

        Budget exhaustion forces an APPROVE (progress preserved, not success).
        Each hard blocker rejects before quorum; quorum shortfall is PENDING
        (recoverable — not a hard reject).
        """
        p = self.policy
        if state.budget_exceeded:
            return Decision.APPROVE, "预算耗尽，强制停止（保留进度，不是完成）"
        if state.open_critical_issues > 0:
            return (
                Decision.REJECT,
                f"存在 {state.open_critical_issues} 个未解决的关键问题，不可收束",
            )
        if state.pending_verifications > p.max_pending_verifications:
            return (
                Decision.REJECT,
                f"验证队列仍有 {state.pending_verifications} 条待验证（上限 "
                f"{p.max_pending_verifications}），不可收束",
            )
        if state.critical_coverage < p.critical_coverage:
            return (
                Decision.REJECT,
                f"关键模块覆盖率 {state.critical_coverage:.2f} 低于阈值 "
                f"{p.critical_coverage:.2f}，不可收束",
            )
        if state.weighted_coverage < p.weighted_coverage:
            return (
                Decision.REJECT,
                f"加权覆盖率 {state.weighted_coverage:.2f} 低于阈值 "
                f"{p.weighted_coverage:.2f}，不可收束",
            )
        if state.conflicts > 0:
            return Decision.REJECT, f"存在 {state.conflicts} 处未裁决冲突，不可收束"
        if state.quorum < p.quorum:
            return (
                Decision.PENDING,
                f"共识度 {state.quorum:.2f} 未达阈值 {p.quorum:.2f}，等待更多确认",
            )
        return Decision.APPROVE, "满足全部收束条件，可以停止"

    @staticmethod
    def from_signals(
        *,
        coverage: dict,
        pending_verifications: int = 0,
        open_critical_issues: int = 0,
        quorum: float = 0.0,
        conflicts: int = 0,
        budget_used_ratio: float = 0.0,
        budget_exceeded: bool = False,
    ) -> TerminationState:
        """Build a ``TerminationState`` from a coverage dict + raw signals.

        Coverage dict: ``{"critical_modules": {"covered": n, "total": m}, ...}``.
        ``critical_coverage = covered/total`` for the critical bucket (1.0 when
        ``total == 0``). ``weighted_coverage`` comes from an optional ``weighted``
        key (a bucket dict or a scalar) and otherwise equals ``critical_coverage``.
        """
        coverage = coverage or {}
        crit_bucket = coverage.get("critical_modules") or coverage.get("critical") or {}
        crit_covered = int((crit_bucket.get("covered", 0) or 0)) if isinstance(crit_bucket, dict) else 0
        crit_total = int((crit_bucket.get("total", 0) or 0)) if isinstance(crit_bucket, dict) else 0
        critical_coverage = (crit_covered / crit_total) if crit_total > 0 else 1.0

        weighted = coverage.get("weighted")
        if isinstance(weighted, dict):
            w_covered = int(weighted.get("covered", 0) or 0)
            w_total = int(weighted.get("total", 0) or 0)
            weighted_coverage = (w_covered / w_total) if w_total > 0 else 1.0
        elif isinstance(weighted, (int, float)) and not isinstance(weighted, bool):
            weighted_coverage = float(weighted)
        else:
            weighted_coverage = critical_coverage

        return TerminationState(
            budget_exceeded=bool(budget_exceeded) or float(budget_used_ratio or 0.0) >= 1.0,
            critical_coverage=critical_coverage,
            weighted_coverage=weighted_coverage,
            pending_verifications=int(pending_verifications or 0),
            open_critical_issues=int(open_critical_issues or 0),
            quorum=float(quorum or 0.0),
            conflicts=int(conflicts or 0),
        )

    def render(self, decision: Decision, reason: str) -> str:
        """Short Chinese line for logs / prompt injection."""
        label = _DECISION_LABEL.get(decision, decision.value)
        return f"【终止裁决】{label}：{reason}"
