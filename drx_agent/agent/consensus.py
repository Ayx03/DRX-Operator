"""Program-level Termination Controller — decides whether the team may stop.

Design principles (EXOBoost research report):
- "Termination Controller 建议独立于 LLM"：a model may REQUEST close, but the
  PROGRAM decides. ``can_close`` is a pure deterministic gate over a
  ``TerminationState``; no LLM, no I/O.
- Termination is NOT 100% unanimity. It is:
  ``Stop = WorkersDrained ∧ (BudgetExceeded ∨ (WorkAndMessagesDrained ∧
  Coverage ≥ θc ∧ CriticalOpenIssues = 0 ∧ VerifierQueue = 0 ∧ Quorum ≥ θq))``.
  Budget exhaustion permits an incomplete report, never a completion claim.
- Coverage is risk-weighted: critical coverage is a hard threshold, while the
  weighted coverage carries the soft threshold.

Self-contained: stdlib only.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
import math
import time
import uuid


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
    open_work: int = 0
    active_workers: int = 0
    pending_messages: int = 0


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


def verification_outcome(finding) -> tuple[str, bool]:
    """Return the effective verdict and whether verification needs adjudication.

    Structured verification takes precedence over a stale finding status.
    Legacy manual confirmations require evidence; a status label alone is not
    verification. A terminal adjudication resolves earlier verifier disagreement.
    """
    verification = getattr(finding, "verification", None) or {}
    if verification:
        adjudicated = verification.get("adjudicated")
        effective = adjudicated if isinstance(adjudicated, dict) else verification
        verdict = str(effective.get("verdict") or "")
        if isinstance(adjudicated, dict) and verdict in ("confirmed", "rejected"):
            return verdict, False
        a = verification.get("verifier_a") or {}
        b = verification.get("verifier_b") or {}
        a_verdict, b_verdict = a.get("verdict"), b.get("verdict")
        disagreement = bool(a_verdict and b_verdict and a_verdict != b_verdict)
        if disagreement:
            return "uncertain", True
        if not verdict and a_verdict and a_verdict == b_verdict:
            verdict = str(a_verdict)
        verdict = verdict or "uncertain"
        return verdict, verdict == "uncertain"
    status = str(getattr(finding, "status", "") or "")
    if status == "retracted":
        return "rejected", False
    if status in ("confirmed", "exploited"):
        evidence = getattr(finding, "evidence", None) or []
        if any(
            getattr(e, "value", "") or getattr(e, "cve", "")
            or getattr(e, "payload", "") or getattr(e, "result", "")
            or getattr(e, "evidence_id", "")
            for e in evidence
        ):
            return "confirmed", False
    return "pending", False


class TerminationController:
    """Pure decision gate over ``TerminationState``."""

    def __init__(self, policy: TerminationPolicy | None = None) -> None:
        self.policy = policy if policy is not None else TerminationPolicy()

    def can_close(self, state: TerminationState) -> tuple[Decision, str]:
        """The report's stop gate, in priority order.

        Live workers must drain before any stop. Budget exhaustion then allows
        an incomplete stop with remaining work/messages preserved. Otherwise all
        hard blockers reject before quorum; quorum shortfall is PENDING.
        """
        p = self.policy
        if state.active_workers > 0:
            return Decision.REJECT, f"仍有 {state.active_workers} 个运行中的 worker，不可收束"
        if state.budget_exceeded:
            return Decision.APPROVE, "预算耗尽，强制停止（保留进度，不是完成）"
        if state.open_work > 0:
            return Decision.REJECT, f"仍有 {state.open_work} 项未完成任务，不可收束"
        if state.pending_messages > 0:
            return Decision.REJECT, f"仍有 {state.pending_messages} 条未交付或待处理消息，不可收束"
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
        open_work: int = 0,
        active_workers: int = 0,
        pending_messages: int = 0,
    ) -> TerminationState:
        """Build a ``TerminationState`` from a coverage dict + raw signals.

        Coverage dict: ``{"critical_modules": {"covered": n, "total": m}, ...}``.
        ``critical_coverage = covered/total`` for the critical bucket; absent or
        zero-total coverage is unknown (0.0), never evidence of completion.
        ``weighted`` may be a bucket or scalar; absent weights use the critical ratio.
        """
        coverage = coverage or {}
        crit_bucket = coverage.get("critical_modules") or coverage.get("critical") or {}
        crit_covered = int((crit_bucket.get("covered", 0) or 0)) if isinstance(crit_bucket, dict) else 0
        crit_total = int((crit_bucket.get("total", 0) or 0)) if isinstance(crit_bucket, dict) else 0
        critical_coverage = (crit_covered / crit_total) if crit_total > 0 else 0.0

        weighted = coverage.get("weighted")
        if isinstance(weighted, dict):
            w_covered = float(weighted.get("covered", 0) or 0)
            w_total = float(weighted.get("total", 0) or 0)
            weighted_coverage = (w_covered / w_total) if w_total > 0 else 0.0
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
            open_work=int(open_work or 0),
            active_workers=int(active_workers or 0),
            pending_messages=int(pending_messages or 0),
        )

    def render(self, decision: Decision, reason: str) -> str:
        """Short Chinese line for logs / prompt injection."""
        label = _DECISION_LABEL.get(decision, decision.value)
        return f"【终止裁决】{label}：{reason}"


class TeamBallot:
    """A unanimous vote over a frozen, runtime-supplied electorate.

    The caller supplies authenticated actor identities; this class never derives
    membership or votes from task completion. A negative vote still permits the
    remaining members to be heard, but can never produce approval. Snapshots are
    detached from internal state, and deadlines are Unix wall-clock timestamps.
    """

    HISTORY_LIMIT = 32
    _DECISIONS = frozenset({"approve", "reject", "abstain"})
    _TERMINAL = frozenset({"approved", "rejected", "expired", "invalidated"})

    def __init__(self, timeout_s: float = 180):
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
            raise ValueError("timeout_s must be a positive finite number")
        try:
            timeout = float(timeout_s)
        except OverflowError as exc:
            raise ValueError("timeout_s must be a positive finite number") from exc
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout_s must be a positive finite number")
        self.timeout_s = timeout
        self._round: dict | None = None
        self._history: list[dict] = []

    @staticmethod
    def _text(value: str, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a nonblank string")
        return value

    @classmethod
    def _electorate(cls, members: list[str]) -> list[str]:
        if not isinstance(members, list) or not members:
            raise ValueError("members must be a nonempty list of unique identities")
        for member in members:
            cls._text(member, "member")
        if len(set(members)) != len(members):
            raise ValueError("members must be a nonempty list of unique identities")
        return list(members)

    def _finish(self, status: str, reason: str) -> None:
        assert self._round is not None
        self._round["status"] = status
        self._round["reason"] = reason
        # Keep one final snapshot per round, including later invalidation.
        self._history = [
            item for item in self._history
            if item["round_id"] != self._round["round_id"]
        ]
        self._history.append(deepcopy(self._round))
        self._history = self._history[-self.HISTORY_LIMIT:]

    def _expire(self) -> None:
        if (
            self._round is not None
            and self._round["status"] == "pending"
            and time.time() >= self._round["deadline"]
        ):
            self._finish("expired", "Ballot deadline elapsed before all members voted")

    def open(
        self, *, stage: str, purpose: str, proposal: str,
        members: list[str], fingerprint: str,
    ) -> dict:
        self._text(stage, "stage")
        self._text(purpose, "purpose")
        if purpose not in {"close", "stage_advance"}:
            raise ValueError("purpose must be close or stage_advance")
        self._text(proposal, "proposal")
        self._text(fingerprint, "fingerprint")
        electorate = self._electorate(members)
        self._expire()
        if self._round is not None and self._round["status"] == "pending":
            raise ValueError("A pending ballot must be explicitly invalidated before reopening")
        deadline = time.time() + self.timeout_s
        if not math.isfinite(deadline):
            raise ValueError("Ballot deadline must be finite")
        self._round = {
            "round_id": uuid.uuid4().hex,
            "stage": stage,
            "purpose": purpose,
            "proposal": proposal,
            "members": electorate,
            "votes": {},
            "pending_members": list(electorate),
            "deadline": deadline,
            "status": "pending",
            "reason": "Waiting for all members to vote",
            "fingerprint": fingerprint,
        }
        return self.status()

    def cast(self, round_id: str, actor: str, decision: str, reason: str) -> dict:
        self._text(round_id, "round_id")
        self._text(actor, "actor")
        self._text(decision, "decision")
        self._text(reason, "reason")
        if decision not in self._DECISIONS:
            raise ValueError("decision must be approve, reject, or abstain")
        if self._round is None or round_id != self._round["round_id"]:
            raise ValueError("Unknown or stale ballot round")
        self._expire()
        if self._round["status"] != "pending":
            raise ValueError("Ballot is no longer pending")
        if actor not in self._round["members"]:
            raise ValueError("Actor is not a member of this ballot")
        vote = {"decision": decision, "reason": reason}
        previous = self._round["votes"].get(actor)
        if previous is not None:
            if previous == vote:
                return self.status()
            raise ValueError("Actor has already voted in this round")
        self._round["votes"][actor] = vote
        self._round["pending_members"].remove(actor)
        if not self._round["pending_members"]:
            unanimous = all(
                item["decision"] == "approve"
                for item in self._round["votes"].values()
            )
            self._finish(
                "approved" if unanimous else "rejected",
                "All members approved" if unanimous
                else "At least one member rejected or abstained",
            )
        return self.status()

    def status(self, fingerprint: str | None = None) -> dict:
        if fingerprint is not None:
            self._text(fingerprint, "fingerprint")
        self._expire()
        if (
            self._round is not None
            and fingerprint is not None
            and fingerprint != self._round["fingerprint"]
            and self._round["status"] != "invalidated"
        ):
            self.invalidate("Task fingerprint changed")
        if self._round is None:
            return {
                "round_id": None, "stage": None, "purpose": None,
                "proposal": None, "members": [], "votes": {},
                "pending_members": [], "deadline": None, "status": "idle",
                "reason": "No ballot has been opened", "fingerprint": None,
            }
        return deepcopy(self._round)

    def invalidate(self, reason: str) -> None:
        self._text(reason, "reason")
        if self._round is not None and self._round["status"] != "invalidated":
            self._finish("invalidated", reason)

    def to_dict(self) -> dict:
        self._expire()
        return {
            "timeout_s": self.timeout_s,
            "round": deepcopy(self._round),
            "history": deepcopy(self._history),
        }

    @classmethod
    def _restore_round(cls, data: dict) -> dict:
        """Reject malformed persisted state rather than restoring false approval."""
        if not isinstance(data, dict):
            raise ValueError("Serialized ballot round must be a dictionary")
        for name in ("round_id", "stage", "purpose", "proposal", "fingerprint", "reason"):
            cls._text(data.get(name), name)
        if data["purpose"] not in {"close", "stage_advance"}:
            raise ValueError("purpose must be close or stage_advance")
        members = cls._electorate(data.get("members"))
        deadline = data.get("deadline")
        if (
            isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(deadline)
        ):
            raise ValueError("Serialized ballot deadline must be finite")
        votes = data.get("votes")
        if not isinstance(votes, dict):
            raise ValueError("Serialized ballot votes must be a dictionary")
        for actor, vote in votes.items():
            if actor not in members or not isinstance(vote, dict):
                raise ValueError("Serialized ballot contains an invalid voter")
            cls._text(vote.get("decision"), "decision")
            if vote["decision"] not in cls._DECISIONS:
                raise ValueError("decision must be approve, reject, or abstain")
            cls._text(vote.get("reason"), "reason")
        pending = [member for member in members if member not in votes]
        if data.get("pending_members") != pending:
            raise ValueError("Serialized pending members do not match votes")
        status = data.get("status")
        if not isinstance(status, str) or status not in cls._TERMINAL | {"pending"}:
            raise ValueError("Invalid serialized ballot status")
        unanimous = not pending and all(v["decision"] == "approve" for v in votes.values())
        if (
            (status == "pending" and not pending)
            or (status == "approved" and not unanimous)
            or (status == "rejected" and (pending or unanimous))
            or (status == "expired" and not pending)
        ):
            raise ValueError("Serialized ballot status does not match votes")
        return deepcopy(data)

    @classmethod
    def from_dict(cls, data: dict) -> TeamBallot:
        if not isinstance(data, dict):
            raise ValueError("Serialized ballot must be a dictionary")
        ballot = cls(timeout_s=data.get("timeout_s", 180))
        history = data.get("history", [])
        if not isinstance(history, list):
            raise ValueError("Serialized ballot history must be a list")
        for item in history[-cls.HISTORY_LIMIT:]:
            restored = cls._restore_round(item)
            if restored["status"] not in cls._TERMINAL:
                raise ValueError("Serialized ballot history must contain terminal rounds")
            ballot._history.append(restored)
        if data.get("round") is not None:
            ballot._round = cls._restore_round(data["round"])
        ballot._expire()
        return ballot
