"""Crash-safe work-claim lease registry — two workers never share one item.

Design principles (EXOBoost research report):
- A work item is claimed by exactly one owner while its lease is active; a
  second ``acquire`` on the same item returns ``None``.
- Leases are time-bounded and heartbeat-extended. A worker that crashes stops
  heartbeating; its lease lapses past ``lease_until`` and is reclaimed
  automatically by the next ``acquire`` (or an explicit ``expire``).
- Deterministic time injection: every mutating method accepts ``now=`` so tests
  drive the clock without sleeping or wall-clock flakiness.
- ``from_dict`` re-expires stale leases on load, so a restored registry never
  presents a dead lease as live.

Self-contained: imports only stdlib.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

ACTIVE = "active"
RELEASED = "released"
EXPIRED = "expired"
VALID_STATUSES = (ACTIVE, RELEASED, EXPIRED)


@dataclass
class Claim:
    """One work-item lease. ``status`` is ``active|released|expired``."""

    claim_id: str
    work_item: str
    owner: str
    lease_until: float
    heartbeat: float
    status: str = "active"
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "claim_id": self.claim_id,
            "work_item": self.work_item,
            "owner": self.owner,
            "lease_until": self.lease_until,
            "heartbeat": self.heartbeat,
            "status": self.status,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Claim":
        status = str(data.get("status", ACTIVE))
        return cls(
            claim_id=str(data.get("claim_id", "")),
            work_item=str(data.get("work_item", "")),
            owner=str(data.get("owner", "")),
            lease_until=float(data.get("lease_until", 0.0) or 0.0),
            heartbeat=float(data.get("heartbeat", 0.0) or 0.0),
            status=status if status in VALID_STATUSES else ACTIVE,
            created_at=float(data.get("created_at", 0.0) or 0.0),
        )


class ClaimRegistry:
    """Lease registry keyed by work item, with deterministic time injection."""

    def __init__(self) -> None:
        self._claims: dict[str, Claim] = {}

    @staticmethod
    def _now(now: float | None) -> float:
        return time.time() if now is None else float(now)

    def _active_claim(self, work_item: str, now: float) -> Claim | None:
        for c in self._claims.values():
            if c.work_item == work_item and c.status == ACTIVE and c.lease_until > now:
                return c
        return None

    def acquire(
        self,
        work_item: str,
        owner: str,
        *,
        ttl: float = 600.0,
        now: float | None = None,
    ) -> str | None:
        """Claim ``work_item`` for ``owner``. Returns a claim id, or ``None``
        if the item is actively claimed by someone else. Expired leases are
        reclaimed first; re-acquiring your own item refreshes the lease."""
        now = self._now(now)
        work_item = str(work_item)
        owner = str(owner)
        ttl = float(ttl or 600.0)
        self.expire(now=now)  # reclaim stale leases first
        existing = self._active_claim(work_item, now)
        if existing is not None:
            if existing.owner == owner:
                existing.lease_until = now + ttl
                existing.heartbeat = now
                existing.status = ACTIVE
                return existing.claim_id
            return None
        claim_id = f"claim-{uuid.uuid4().hex[:6]}"
        self._claims[claim_id] = Claim(
            claim_id=claim_id,
            work_item=work_item,
            owner=owner,
            lease_until=now + ttl,
            heartbeat=now,
            status=ACTIVE,
            created_at=now,
        )
        return claim_id

    def heartbeat(
        self,
        claim_id: str,
        *,
        ttl: float = 600.0,
        now: float | None = None,
    ) -> bool:
        """Extend an active lease. Returns False when the claim is unknown,
        already released/expired, or past its deadline."""
        now = self._now(now)
        claim = self._claims.get(claim_id)
        if claim is None or claim.status != ACTIVE or claim.lease_until <= now:
            return False
        claim.lease_until = now + float(ttl or 600.0)
        claim.heartbeat = now
        return True

    def release(self, claim_id: str) -> bool:
        """Free an active claim so another worker may take the item."""
        claim = self._claims.get(claim_id)
        if claim is None or claim.status != ACTIVE:
            return False
        claim.status = RELEASED
        return True

    def is_taken(self, work_item: str, *, now: float | None = None) -> bool:
        now = self._now(now)
        return self._active_claim(str(work_item), now) is not None

    def owner_of(self, work_item: str, *, now: float | None = None) -> str | None:
        now = self._now(now)
        claim = self._active_claim(str(work_item), now)
        return claim.owner if claim is not None else None

    def expire(self, *, now: float | None = None) -> int:
        """Flip past-deadline active claims to ``expired``; return the count."""
        now = self._now(now)
        count = 0
        for claim in self._claims.values():
            if claim.status == ACTIVE and claim.lease_until <= now:
                claim.status = EXPIRED
                count += 1
        return count

    def active(self, *, now: float | None = None) -> list[Claim]:
        now = self._now(now)
        return [
            c
            for c in self._claims.values()
            if c.status == ACTIVE and c.lease_until > now
        ]

    def to_dict(self) -> dict:
        return {
            "claims": [
                c.to_dict()
                for c in sorted(self._claims.values(), key=lambda c: c.created_at)
            ]
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ClaimRegistry":
        reg = cls()
        for raw in (data or {}).get("claims") or []:
            if not isinstance(raw, dict) or not raw.get("claim_id"):
                continue
            claim = Claim.from_dict(raw)
            reg._claims[claim.claim_id] = claim
        reg.expire()  # re-expire stale leases on load
        return reg
