"""Stage Machine — capability contract across penetration-testing phases.

Design principles (EXOBoost research report):
- Phase = Prompt × Tool Capability × Budget × Completion Contract. A stage is a
  CAPABILITY SET `C_s = (T_s, M_s, B_s, O_s)` — tools, memory scope, budget,
  completion object — not a sentence in the prompt.
- "Recon 不得提交漏洞、不得验证、不得出报告" is enforced at the PROGRAM layer:
  the tool set itself makes illegal actions unrepresentable, not a prompt.
- Tool definitions are part of the cacheable prefix → filter_schemas sorts
  deterministically so the stable prefix stays cacheable across turns.

Self-contained: no imports from drx_agent.agent.master (callers pass objects in).
"""

# allow: SIZE_OK — task-mandated single-file data contract: 4 tool frozensets +
# the CAPABILITIES table + StageMachine are one indivisible capability API that
# cannot be split without violating the deliverable spec.

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum

from drx_agent.agent.consensus import verification_outcome


class Stage(str, Enum):
    RECON = "recon"
    RESEARCH = "research"
    VERIFY = "verification"
    SYNTHESIS = "synthesis"


STAGE_ORDER: tuple[Stage, ...] = (Stage.RECON, Stage.RESEARCH, Stage.VERIFY, Stage.SYNTHESIS)

# Always-allowed control tools regardless of stage, so the machine can progress.
# Collaboration tools (forum/claim/team_status/request_close) are orthogonal to
# stage capability boundaries: any stage may coordinate with peers without
# weakening its own tool contract.
ALWAYS_ALLOWED: frozenset[str] = frozenset(
    {
        "todo_write", "blackboard_read", "blackboard_write", "stage_advance",
        "forum_post", "forum_read", "forum_threads", "forum_digest",
        "forum_pin", "forum_close", "forum_wait", "forum_subscribe", "forum_pending",
        "claim_acquire", "claim_release", "claim_status",
        "team_status", "request_close",
        "irc_send", "irc_inbox", "irc_reply", "irc_pending", "irc_close",
        "irc_admin_close",
        "note_update", "note_read", "note_clear",
    }
)

# The complete working tool set (available to RESEARCH minus handoff_submit /
# stage_advance, which are stage-transition tools rather than working tools).
_FULL_TOOLSET: frozenset[str] = frozenset(
    {
        "http_fetch", "web_search", "cve_lookup", "execute_bash", "execute_python",
        "shell_open", "shell_exec", "shell_signal", "shell_close", "shell_list",
        "parse_nmap", "parse_http", "read_artifact", "read_file", "write_file",
        "edit_file", "multi_edit_file", "grep", "todo_write", "task",
        "generate_report", "update_target", "record_finding",
        "update_finding_status", "list_findings", "cred_add", "cred_list",
        "cred_show", "cred_verify", "dispatch_sub_agent", "blackboard_write",
        "blackboard_read", "intent_add", "intent_list", "intent_claim",
        "intent_done", "intent_kill", "intent_batch", "wordlist_list",
        "wordlist_top", "oob_start", "oob_logs", "oob_stop", "evidence_add",
        "read_handoff",
    }
)

# Recon 只观察与建立入口，不得提交漏洞（record_finding）、不得验证、不得出报告、
# 不得写文件、不得派发子 Agent。update_finding_status / verify_finding /
# generate_report / write_file / edit_file / multi_edit_file / task /
# dispatch_sub_agent / intent_batch 均被排除在能力集之外。
_RECON_TOOLS: frozenset[str] = frozenset(
    {
        "read_file", "grep", "http_fetch", "web_search", "cve_lookup",
        "execute_bash", "execute_python", "shell_open", "shell_exec",
        "shell_signal", "shell_close", "shell_list", "parse_nmap", "parse_http",
        "update_target", "cred_add", "cred_list", "cred_show", "cred_verify",
        "todo_write", "blackboard_write", "blackboard_read", "intent_add",
        "intent_list", "intent_claim", "intent_done", "intent_kill",
        "read_artifact", "read_handoff", "evidence_add", "wordlist_list",
        "wordlist_top", "oob_start", "oob_logs", "oob_stop",
    }
)

# Verify 只做证伪：Candidate 永远不是 Fact，research 产出都当作待证伪假设。
_VERIFY_TOOLS: frozenset[str] = frozenset(
    {
        "read_file", "grep", "read_artifact", "read_handoff", "blackboard_read",
        "list_findings", "http_fetch", "execute_bash", "execute_python",
        "shell_list", "evidence_add", "verify_finding",
    }
)

# Synthesis 只读 + 出报告。
_SYNTHESIS_TOOLS: frozenset[str] = frozenset(
    {
        "list_findings", "read_artifact", "read_handoff", "blackboard_read",
        "evidence_add", "generate_report", "todo_write",
    }
)


@dataclass(frozen=True)
class StageCapability:
    """能力契约 `C_s = (T_s, M_s, B_s, O_s)`：工具、记忆范围、预算、完成对象。"""

    stage: Stage
    label: str
    allowed_tools: frozenset[str]
    memory_scope: tuple[str, ...]
    max_context: int
    max_turns: int
    completion_tool: str
    can_report: bool
    can_verify: bool
    can_modify: bool
    description: str


CAPABILITIES: dict[Stage, StageCapability] = {
    Stage.RECON: StageCapability(
        stage=Stage.RECON,
        label="侦察",
        allowed_tools=_RECON_TOOLS,
        memory_scope=("targets", "credentials", "blackboard"),
        max_context=12000,
        max_turns=20,
        completion_tool="stage_advance",
        can_report=False,
        can_verify=False,
        can_modify=False,
        description="只观察与建立入口：枚举目标、收集指纹、记录凭据，不得提交漏洞/验证/出报告。",
    ),
    Stage.RESEARCH: StageCapability(
        stage=Stage.RESEARCH,
        label="研究",
        allowed_tools=_FULL_TOOLSET,
        memory_scope=("targets", "findings", "credentials", "blackboard", "frontier"),
        max_context=16000,
        max_turns=30,
        completion_tool="stage_advance",
        can_report=True,
        can_verify=False,
        can_modify=True,
        description="全工具工作集：记录发现、写文件、派发子 Agent 深入验证攻击路径。",
    ),
    Stage.VERIFY: StageCapability(
        stage=Stage.VERIFY,
        label="验证",
        allowed_tools=_VERIFY_TOOLS,
        memory_scope=("findings", "evidence", "blackboard"),
        max_context=12000,
        max_turns=20,
        completion_tool="verify_finding",
        can_report=False,
        can_verify=True,
        can_modify=False,
        description="只证伪与确认：把研究产出当假设去 falsify，不得提交报告、不得写文件。",
    ),
    Stage.SYNTHESIS: StageCapability(
        stage=Stage.SYNTHESIS,
        label="汇总",
        allowed_tools=_SYNTHESIS_TOOLS,
        memory_scope=("findings", "evidence", "blackboard"),
        max_context=12000,
        max_turns=12,
        completion_tool="generate_report",
        can_report=True,
        can_verify=False,
        can_modify=False,
        description="只读 + 出报告：汇总已验证发现生成最终渗透测试报告。",
    ),
}


class StageMachine:
    """阶段状态机：能力集驱动的推进，永不回退。"""

    def __init__(self) -> None:
        self.stage: Stage = Stage.RECON
        # Append-only transition ledger: {from, to, ts, reason}.
        self.history: list[dict] = []

    def current(self) -> Stage:
        return self.stage

    def capability(self) -> StageCapability:
        return CAPABILITIES[self.stage]

    def next_stage(self) -> Stage | None:
        idx = STAGE_ORDER.index(self.stage)
        if idx + 1 >= len(STAGE_ORDER):
            return None
        return STAGE_ORDER[idx + 1]

    def allowed_tools(self) -> frozenset[str]:
        return self.capability().allowed_tools | ALWAYS_ALLOWED

    def is_allowed(self, tool_name: str) -> bool:
        return tool_name in self.allowed_tools()

    def filter_schemas(self, schemas: list[dict]) -> list[dict]:
        """Keep only stage-allowed tools; sort by name for a cache-friendly
        stable prefix."""
        allowed = self.allowed_tools()
        kept: list[dict] = []
        for schema in schemas:
            fn = schema.get("function") if isinstance(schema, dict) else None
            name = fn.get("name") if isinstance(fn, dict) else None
            if name in allowed:
                kept.append(schema)
        kept.sort(key=lambda s: s["function"]["name"])
        return kept

    def advance(self, reason: str = "") -> Stage | None:
        """Move to the next stage in STAGE_ORDER. Monotonic — never backwards."""
        nxt = self.next_stage()
        if nxt is None:
            return None
        self.history.append(
            {
                "from": self.stage.value,
                "to": nxt.value,
                "ts": time.time(),
                "reason": reason or "",
            }
        )
        self.stage = nxt
        return self.stage

    @staticmethod
    def _verification_complete(knowledge_base, handoff, frontier) -> bool:
        findings = knowledge_base.all_findings()
        for _host, finding in findings:
            verdict, conflict = verification_outcome(finding)
            if (
                getattr(finding, "status", "") == "suspected"
                or verdict not in ("confirmed", "rejected")
                or conflict
            ):
                return False
        candidates = (handoff.candidates if handoff is not None else []) or []
        for item in candidates:
            status = getattr(item, "status", "")
            if status == "rejected":
                continue
            if status != "verified" or not getattr(item, "evidence", None):
                return False
        if findings or candidates:
            return True
        # No candidates is a valid result only when an actual plan completed.
        # An empty KB/frontier alone does not establish coverage.
        intents = getattr(frontier, "_intents", None) or {}
        return bool(intents) and all(
            getattr(getattr(intent, "status", ""), "value", getattr(intent, "status", "")) == "done"
            for intent in intents.values()
        )

    def gate(self, handoff, frontier, knowledge_base) -> tuple[bool, str]:
        """Program-level gate: decides whether advancing from the CURRENT stage
        is legal. Returns (ok, reason)."""
        current = self.stage
        nxt = self.next_stage()
        if nxt is None:
            return False, "已处于最后阶段，无法推进"

        def _section_total(h, *sections: str) -> int:
            total = 0
            for section in sections:
                items = getattr(h, section, None) or []
                total += len(items)
            return total

        if current is Stage.RECON:
            if (
                _section_total(handoff, "facts", "hypotheses", "candidates", "entrypoints")
                == 0
            ):
                return (
                    False,
                    "RECON 无实质产出（facts/hypotheses/candidates/entrypoints 均空），不可推进",
                )
        if current is Stage.RESEARCH:
            if _section_total(handoff, "candidates", "hypotheses") == 0:
                return (
                    False,
                    "RESEARCH 无候选/假设（candidates/hypotheses 均空），不可进入 VERIFY",
                )
        if current is Stage.VERIFY:
            if not self._verification_complete(knowledge_base, handoff, frontier):
                return (
                    False,
                    "VERIFY 仍有未终局验证的候选，或缺少已完成计划，不可进入 SYNTHESIS",
                )
        return True, ""

    def render(self) -> str:
        cap = self.capability()
        allowed = sorted(self.allowed_tools())
        lines = [
            f"【当前阶段】{cap.stage.value} — {cap.label}",
            cap.description,
            f"能力：报告={'是' if cap.can_report else '否'} 验证={'是' if cap.can_verify else '否'} 写文件={'是' if cap.can_modify else '否'}",
            f"可用工具：{', '.join(allowed)}",
            f"完成契约：调用 {cap.completion_tool} 推进到下一阶段。",
        ]
        text = "\n".join(lines)
        if len(text) > 500:
            text = text[:499] + "…"
        return text

    def to_dict(self) -> dict:
        return {"stage": self.stage.value, "history": list(self.history)}

    @classmethod
    def from_dict(cls, data: dict) -> "StageMachine":
        data = data or {}
        sm = cls()
        raw = data.get("stage", Stage.RECON.value)
        try:
            sm.stage = Stage(raw)
        except ValueError:
            sm.stage = Stage.RECON
        sm.history = [
            h for h in (data.get("history") or []) if isinstance(h, dict)
        ]
        return sm
