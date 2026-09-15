"""DRX-Operator: Autonomous Red-Team Penetration Testing Expert System.

Agent-First architecture: TUI is a thin shell, the Agent is the
first-class citizen.  All operations are LLM tool calls.
"""

import asyncio
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drx_agent.event_bus import EventBus, Event, EventType
from drx_agent.tui.app import DrxAgentApp
from drx_agent.tui.transcript import TranscriptLog
from drx_agent.safety.gate import SafetyGate
from drx_agent.agent.knowledge_base import KnowledgeBase
from drx_agent.agent.task_scheduler import TaskScheduler, TaskPriority, ScheduledTask
from drx_agent.agent.master import MasterAgent
from drx_agent.engine.python_sandbox import PythonSandbox
from drx_agent.engine.bash_sandbox import BashSandbox
from drx_agent.engine.script_library import ScriptLibrary
from drx_agent.engine.process import cancel_task
from drx_agent.skills.registry import SkillsRegistry
from drx_agent.session.manager import SessionManager
from drx_agent.session.usage import usage_status
from drx_agent.agent.frontier import Frontier
from drx_agent.agent.handoff import Handoff
from drx_agent.agent.stage import StageMachine
from drx_agent.llm.base import LLMConfig
from drx_agent.llm.output_tokens import CUSTOM_MODEL_MAX_OUTPUT_TOKENS, model_output_limit
from drx_agent.mcp.manager import MCPManager
from drx_agent.hooks.manager import HookManager

logger = logging.getLogger(__name__)


def _build_one_provider(spec: dict):
    
    provider_name = (
        os.environ.get("DRX_LLM_PROVIDER") or spec.get("provider") or ""
    ).lower()
    is_exo = provider_name in ("exo", "qwen_exo", "qwen-exo")

    if provider_name in ("anthropic", "claude"):
        env_key = os.environ.get("ANTHROPIC_API_KEY")
    elif provider_name in ("openai",):
        env_key = os.environ.get("OPENAI_API_KEY")
    elif is_exo:
        env_key = ""
    else:
        env_key = os.environ.get("DEEPSEEK_API_KEY")
    api_key = (
        os.environ.get("DRX_LLM_API_KEY")
        or env_key
        or spec.get("api_key", "")
    )
    # Local EXO servers are unauthenticated by default, so an empty key is
    # valid for them; every other provider keeps the existing empty-key skip.
    if not api_key and not is_exo:
        logger.warning("Provider %r has no API key — skipped", provider_name or "default")
        return None

    api_interface = (os.environ.get("DRX_LLM_INTERFACE") or "").strip().lower()
    if api_interface not in ("chat", "responses"):
        api_interface = "chat"

    model = os.environ.get("DRX_LLM_MODEL") or spec.get("model", "deepseek-chat")
    base_url = os.environ.get("DRX_LLM_BASE_URL") or spec.get("base_url", "")
    max_tokens = spec.get("max_tokens")
    model_max_tokens = spec.get("model_max_tokens")
    if model_max_tokens is None:
        model_max_tokens = model_output_limit(model, provider_name, base_url)
    if model_max_tokens is None:
        model_max_tokens = CUSTOM_MODEL_MAX_OUTPUT_TOKENS
    config = LLMConfig(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=float(spec.get("temperature", 0.7)),
        max_tokens=int(max_tokens) if max_tokens is not None else None,
        api_interface=api_interface,
        model_max_tokens=int(model_max_tokens) if model_max_tokens is not None else None,
        omit_max_output_tokens=spec.get("omit_max_output_tokens", False),
        max_tokens_field=spec.get("max_tokens_field"),
        always_send_max_tokens=spec.get("always_send_max_tokens"),
        clamp_output_to_model_max=spec.get("clamp_output_to_model_max"),
        provider=provider_name,
    )
    try:
        if provider_name in ("anthropic", "claude"):
            from drx_agent.llm.anthropic_provider import AnthropicProvider
            return AnthropicProvider(config)
        if provider_name in ("openai",):
            from drx_agent.llm.openai_provider import OpenAIProvider
            return OpenAIProvider(config)
        if is_exo:
            from drx_agent.llm.exo_provider import EXOProvider
            # Optional startup hint only: if DRX_EXO_INGEST_PATHS is set, name the
            # project files that WOULD be ingested into long-term knowledge. No
            # network I/O happens here — ingestion is deferred to the caller.
            _ingest_paths = os.environ.get("DRX_EXO_INGEST_PATHS", "").strip()
            if _ingest_paths:
                _paths = [p.strip() for p in _ingest_paths.split(",") if p.strip()]
                if _paths:
                    logger.info(
                        "EXO knowledge ingestion requested at startup "
                        "(deferred, no I/O now): %s",
                        ", ".join(_paths),
                    )
            control_url = (
                spec.get("control_url")
                or os.environ.get("DRX_EXO_CONTROL_URL", "")
            )
            return EXOProvider(config, control_url=control_url)
        from drx_agent.llm.deepseek_provider import DeepSeekProvider
        return DeepSeekProvider(config)
    except Exception as exc:
        logger.warning("Failed to build provider %r: %s", provider_name, exc)
        return None


def _config_path() -> str:
    """Resolve the config file path across run modes (frozen binary / dev).

    Priority: $DRX_CONFIG → ~/.config/drx-operator/default_config.json →
    <executable-dir>/configs/default_config.json (frozen) → package-relative.
    """
    env = os.environ.get("DRX_CONFIG")
    if env:
        return os.path.abspath(env)
    user_cfg = os.path.join(
        os.path.expanduser("~"), ".config", "drx-operator", "default_config.json"
    )
    if os.path.isfile(user_cfg):
        return user_cfg
    if getattr(sys, "frozen", False):
        adjacent = os.path.join(
            os.path.dirname(sys.executable), "configs", "default_config.json"
        )
        if os.path.isfile(adjacent):
            return adjacent
    return os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "configs", "default_config.json")
    )


def _build_llm_provider(event_bus=None):
    
    cfg_path = _config_path()
    if not os.path.isfile(cfg_path):
        logger.warning("LLM config not found at %s", cfg_path)
        return None
    try:
        with open(cfg_path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except Exception as exc:
        logger.warning("Failed to load LLM config: %s", exc)
        return None

    llm_cfg = data.get("llm", {})
    if not llm_cfg.get("enabled", True):
        return None

    primary = _build_one_provider(llm_cfg)
    providers = [primary] if primary else []

    for fb_spec in llm_cfg.get("fallback") or []:
        if not isinstance(fb_spec, dict):
            continue
        fb = _build_one_provider(fb_spec)
        if fb is not None:
            providers.append(fb)

    if not providers:
        logger.warning("No usable LLM provider configured")
        return None

    retry_cfg = llm_cfg.get("retry") or {}

    def _notify(msg: str):
        if event_bus is not None:
            try:
                event_bus.publish(Event(
                    type=EventType.STATUS_UPDATE, data={"text": msg}
                ))
                event_bus.publish(Event(
                    type=EventType.AGENT_MESSAGE,
                    data={"text": f"⚠ {msg}", "source": "system"},
                ))
            except Exception:
                pass

    from drx_agent.llm.resilient import ResilientProvider
    return ResilientProvider(
        providers=providers,
        max_retries=int(retry_cfg.get("max_retries", 3)),
        base_delay=float(retry_cfg.get("base_delay", 1.0)),
        max_delay=float(retry_cfg.get("max_delay", 30.0)),
        notify=_notify,
    )


class DrxAgent:
    """DRX-Operator main controller — connects all subsystems."""

    # execute_bash may run these; destructive ops are still blocked by
    # BashSandbox.BLOCKED_PATTERNS and the PermissionEngine. None → no whitelist.
    BASH_WHITELIST = [
        "cat", "tac", "head", "tail", "less", "more", "nl", "wc",
        "file", "stat", "ls", "tree", "pwd", "readlink", "realpath",
        "strings", "od", "xxd", "hexdump",
        "grep", "egrep", "fgrep", "rg", "ag",
        "awk", "gawk", "sed", "cut", "tr", "sort", "uniq", "paste",
        "diff", "comm", "join", "column", "fold", "fmt", "rev", "expand",
        "find", "locate", "which", "whereis", "type",
        "base64", "base32", "md5sum", "sha1sum", "sha256sum", "sha512sum",
        "r2", "radare2", "objdump", "readelf", "nm", "gdb", "ltrace", "strace",
        "patchelf", "checksec", "rabin2", "r2pipe", "xxd", "hexdump", "od",
        "shasum", "cksum", "uuencode", "uudecode",
        "curl", "wget", "dig", "host", "nslookup", "whois",
        "ping", "ping6", "traceroute", "traceroute6", "tracepath",
        "nc", "ncat", "socat", "telnet", "openssl",
        "ip", "ifconfig", "netstat", "ss", "arp", "route", "ipcalc",
        "nmap", "masscan", "rustscan", "naabu",
        "sqlmap", "nikto", "hydra", "medusa", "patator",
        "gobuster", "feroxbuster", "ffuf", "wfuzz", "dirb",
        "amass", "subfinder", "httpx", "nuclei", "katana", "waybackurls",
        "dnsx", "dnsenum", "fierce", "theHarvester",
        "responder", "crackmapexec", "impacket-secretsdump",
        "uname", "hostname", "id", "whoami", "groups", "users", "w", "who",
        "uptime", "date", "env", "printenv", "getent", "lscpu", "lsblk",
        "ps", "top", "htop", "free", "df", "du", "mount",
        "tar", "gzip", "gunzip", "zcat", "bzip2", "bunzip2", "xz", "unxz",
        "zip", "unzip", "7z", "7za", "ar",
        "git",
        "echo", "printf", "true", "false", "test", "[", "yes", "seq",
        "sleep", "timeout", "tee", "xargs", "env",
        "ssh", "scp", "sftp", "rsync",
        "python", "python3", "perl", "ruby", "node", "deno", "php",
        "bash", "sh", "dash", "zsh", "fish", "lua",
    ]

    def __init__(self):
        self.event_bus = EventBus()
        self.transcript = TranscriptLog(self.event_bus)

        self.safety_gate = SafetyGate()

        self.knowledge_base = KnowledgeBase()

        self.python_sandbox = PythonSandbox()
        # Config overrides: bash.whitelist null → everything allowed; [...] → exact list; extra_whitelist → append.
        bash_whitelist = list(self.BASH_WHITELIST)
        cfg = {}
        try:
            cfg_path = _config_path()
            with open(cfg_path, "r", encoding="utf-8") as fp:
                cfg = json.load(fp)
            bash_cfg = cfg.get("bash") or {}
            if "whitelist" in bash_cfg:
                bash_whitelist = bash_cfg["whitelist"]
            extra = bash_cfg.get("extra_whitelist") or []
            if bash_whitelist is not None and extra:
                bash_whitelist = list(bash_whitelist) + list(extra)
        except Exception:
            pass
        self.bash_sandbox = BashSandbox(command_whitelist=bash_whitelist)

        self.script_library = ScriptLibrary()
        self.skills_registry = SkillsRegistry()

        collaboration = cfg.get("collaboration", {})
        if not isinstance(collaboration, dict):
            raise ValueError("collaboration configuration must be an object")
        scheduler_config = collaboration.get("scheduler", {})
        if not isinstance(scheduler_config, dict):
            raise ValueError("collaboration.scheduler must be an object")
        self.scheduler = TaskScheduler(
            max_concurrent_per_target=scheduler_config.get("max_concurrent_per_target", 4),
            max_concurrent=scheduler_config.get("max_concurrent", 16),
            global_qps=scheduler_config.get("global_qps"),
        )

        session_dir = os.path.join(os.path.dirname(__file__), "..", "sessions")
        self.session_manager = SessionManager(storage_dir=os.path.abspath(session_dir))

        self.llm_provider = _build_llm_provider(event_bus=self.event_bus)

        cfg_path = _config_path()
        self.mcp_manager = MCPManager.from_config_file(cfg_path)
        self._setup_task: asyncio.Task | None = None

        self.hooks = HookManager()
        try:
            with open(cfg_path, "r", encoding="utf-8") as fp:
                cfg = json.load(fp)
            self.hooks.load_from_config(cfg.get("hooks") or [])
        except Exception:
            pass

        self.master = MasterAgent(
            event_bus=self.event_bus,
            scheduler=self.scheduler,
            python_sandbox=self.python_sandbox,
            bash_sandbox=self.bash_sandbox,
            knowledge_base=self.knowledge_base,
            safety_gate=self.safety_gate,
            skills_registry=self.skills_registry,
            script_library=self.script_library,
            llm_provider=self.llm_provider,
            mcp_manager=self.mcp_manager,
            hooks=self.hooks,
            collaboration_config=collaboration,
        )

        try:
            with open(cfg_path, "r", encoding="utf-8") as fp:
                _cfg = json.load(fp)
            _win = (_cfg.get("llm") or {}).get("context_window")
            if _win:
                self.master.model_context_window_override = int(_win)
        except Exception:
            pass

        self._load_skills()

        self._setup_session_handlers()

    async def async_setup(self) -> None:
        """One-time async startup: connect MCP servers, etc."""
        if self.master._closing:
            return
        if getattr(self, "_setup_task", None) is None:
            self._setup_task = asyncio.create_task(self.mcp_manager.start_all())
        try:
            await asyncio.shield(self._setup_task)
        except asyncio.CancelledError:
            if not self._setup_task.done():
                cancel_task(self._setup_task)
            await asyncio.gather(self._setup_task, return_exceptions=True)
            raise
        if self.mcp_manager.clients:
            count = sum(len(c.tools) for c in self.mcp_manager.clients.values())
            self.event_bus.publish(Event(
                type=EventType.STATUS_UPDATE,
                data={"text": (
                    f"MCP: {len(self.mcp_manager.clients)} server(s), "
                    f"{count} tool(s) ready"
                )},
            ))

    async def async_teardown(self) -> None:
        self.master._closing = True
        setup = getattr(self, "_setup_task", None)
        try:
            if setup is not None:
                if not setup.done():
                    cancel_task(setup)
                await asyncio.gather(setup, return_exceptions=True)
            await self.master.async_shutdown()
        finally:
            await self.mcp_manager.close_all()

    def _load_skills(self):
        skills_dir = os.path.join(os.path.dirname(__file__), "..", "skills")
        abs_path = os.path.abspath(skills_dir)
        if os.path.isdir(abs_path):
            loaded = self.skills_registry.load_from_directory(abs_path)
            if loaded > 0:
                self.event_bus.publish(Event(
                    type=EventType.STATUS_UPDATE,
                    data={"text": f"Loaded {loaded} skills"}
                ))

    def save_session(self) -> str:
        """Persist the current snapshot, propagating errors to the caller."""
        return self.session_manager.save(
            kb=self.knowledge_base,
            messages=self.master.messages,
            active_targets=[t["host"] for t in self.knowledge_base.list_targets()],
            name=f"session-{len(self.master.messages)}msgs",
            todos=self.master.todos,
            mode=self.master.mode,
            session_usage=self.master.session_usage,
            frontier=self.master.frontier.to_dict(),
            handoff=self.master.handoff.to_dict() if self.master.handoff is not None else None,
            stage=self.master.stage_machine.to_dict(),
            forum=self.master.forum.to_dict(),
            claims=self.master.claims.to_dict(),
            moderator=self.master.moderator.to_dict(),
            irc=self.master.irc.to_dict(),
            project_note=self.master.project_note.to_dict(),
            team=self.master._export_team_state(),
            transcript=self.transcript.export(),
        )

    def _setup_session_handlers(self):
        def handle_save(event: Event):
            if self.master._closing:
                return
            try:
                sid = self.save_session()
                self.event_bus.publish(Event(
                    type=EventType.AGENT_MESSAGE,
                    data={
                        "text": (
                            f"💾 会话已保存: {sid} "
                            f"(messages={len(getattr(self.master, 'messages', []))}, "
                            f"targets={len(self.knowledge_base.list_targets())}, "
                            f"creds={len(self.knowledge_base.list_credentials())})"
                        ),
                        "source": "system",
                    }
                ))
            except Exception as e:
                self.event_bus.publish(Event(
                    type=EventType.ERROR,
                    data={"message": f"Save failed: {e}"}
                ))

        async def handle_restore(event: Event):
            try:
                sessions = self.session_manager.list_sessions()
                if not sessions:
                    self.event_bus.publish(Event(
                        type=EventType.AGENT_MESSAGE,
                        data={"text": "没有可恢复的会话。", "source": "system"}
                    ))
                    return
                latest = sessions[0]
                restored = self.session_manager.restore(latest["id"])
                if restored is None:
                    raise ValueError("Restore returned no data")
                if (not isinstance(restored["todos"], list)
                        or any(not isinstance(todo, dict) for todo in restored["todos"])):
                    raise ValueError("Saved todos must be a list of objects")
                if restored["mode"] not in ("act", "plan"):
                    raise ValueError("Invalid saved operating mode")

                # Validate every replacement, including display history, without
                # disturbing live producers or an outstanding approval.
                from drx_agent.agent.forum import Forum
                from drx_agent.agent.claims import ClaimRegistry
                from drx_agent.agent.moderator import Moderator
                from drx_agent.agent.irc import IRC
                from drx_agent.agent.project_note import ProjectNote

                frontier = Frontier.from_dict(
                    restored["frontier"], max_intents=self.master.frontier.max_intents,
                )
                frontier.reconcile_restored()
                raw_handoff = restored["handoff"]
                handoff = Handoff.from_dict(raw_handoff) if raw_handoff else None
                stage = StageMachine.from_dict(restored["stage"])
                forum = Forum.from_dict(restored["forum"])
                claims = ClaimRegistry.from_dict(restored["claims"])
                moderator = Moderator.from_dict(restored["moderator"])
                irc = IRC.from_dict(restored["irc"])
                project_note = ProjectNote.from_dict(restored["project_note"])
                ballot, members, run_id, residents = self.master._decode_team_state(
                    restored["team"], stage=stage.stage.value,
                )
                candidate = TranscriptLog(EventBus())
                try:
                    candidate.restore_messages(restored["messages"])
                    if restored["transcript"] is not None:
                        candidate.restore(restored["transcript"])
                    history = candidate.export()
                finally:
                    candidate.close()

                await self.master.prepare_restore()
                try:
                    # No await between the successful preflight barrier and commit.
                    self.transcript.restore(history)

                    self.knowledge_base = restored["kb"]
                    self.master.knowledge_base = restored["kb"]
                    self.master.messages = restored["messages"]
                    self.master.todos = restored["todos"]
                    self.master.frontier = frontier
                    self.master.handoff = handoff
                    self.master.stage_machine = stage
                    self.master.forum = forum
                    self.master.claims = claims
                    self.master.moderator = moderator
                    self.master.irc = irc
                    self.master.project_note = project_note
                    self.master.mode = restored["mode"]
                    self.master.session_usage = restored["session_usage"]
                    self.master._recent_request_ts.clear()
                    self.master.ballot = ballot
                    self.master._team_members = members
                    self.master._run_id = run_id
                    self.master._resident_workers = residents
                finally:
                    self.master.finish_restore()

                self.event_bus.publish(Event(
                    type=EventType.SESSION_RESTORED,
                    data={"session_id": latest["id"]},
                ))
                self.event_bus.publish(Event(
                    type=EventType.STATUS_UPDATE,
                    data={"tasks": [
                        {"name": t.get("content", ""), "status": t.get("status", "pending"), "id": t.get("id", "")}
                        for t in self.master.todos
                    ]},
                ))
                self.event_bus.publish(Event(
                    type=EventType.STATUS_UPDATE,
                    data={
                        **usage_status(self.master.session_usage),
                        "rate": 0,
                        "mode": self.master.mode,
                        "active_targets": len(self.knowledge_base.list_targets()),
                    },
                ))
                self.event_bus.publish(Event(
                    type=EventType.AGENT_MESSAGE,
                    data={
                        "text": (
                            f"会话已恢复: {latest['name']} "
                            f"(messages={len(self.master.messages)}, "
                            f"targets={len(self.knowledge_base.list_targets())}, "
                            f"creds={len(self.knowledge_base.list_credentials())}, "
                            f"mode={self.master.mode})"
                        ),
                        "source": "system",
                    }
                ))
            except Exception as e:
                self.event_bus.publish(Event(
                    type=EventType.ERROR,
                    data={"message": f"Restore failed: {e}"}
                ))

        self.event_bus.subscribe(EventType.SESSION_SAVE, handle_save)
        self.event_bus.subscribe(
            EventType.SESSION_RESTORE,
            lambda event: self.master._schedule(handle_restore(event)),
        )


def main() -> int:
    """Entry point — create agent and launch TUI."""
    agent = DrxAgent()
    app = DrxAgentApp(agent.event_bus, drx_agent=agent)
    app.run()
    return app.return_code or 0


if __name__ == "__main__":
    sys.exit(main())

