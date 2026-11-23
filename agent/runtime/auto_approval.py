"""Conditional LangChain HITL for auto mode and replay-safe tool decisions."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from hashlib import sha256
import json
import sqlite3
from threading import Lock

from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.config import get_config

from agent.models.stream_lifecycle import ModelStreamCancelled
from agent.redaction import known_secret_refs
from agent.runtime.approval.classify import classify
from agent.runtime.approval.evidence import build_evidence, user_texts
from agent.runtime.approval.fingerprint import build_policy
from agent.runtime.approval.policy import MANUAL
from agent.runtime.approval.rules import REVOCABLE, static_rule
from agent.runtime.approval.types import Decision

# Shorter values would blank out unrelated review text when replaced.
_MIN_SECRET_LEN = 8
_BATCH_TIMEOUT = 18
_MAX_CONCURRENT_REVIEWS = 4
# Repeated blocks mean the agent and the reviewer disagree; the block that reaches a limit goes to the human.
_MAX_CONSECUTIVE_BLOCKS = 3
_MAX_SESSION_BLOCKS = 20

delegating_user_request = ContextVar("delegating_user_request", default="")
_STALE = "审批环境已变化，请在当前权限下重新发起。"
_REPLAY = "此调用已处理或执行结果未知，已阻止重复执行。"
_REVOKED = "放行条件已改变（工具信任、文件路径或沙箱模式），请重新发起调用。"
_BLOCKED_HINT = "不要改写命令绕过此拦截；请换一种方式完成任务，或向用户说明需要其明确授权。"


async def _classify_batch(evidence_by_id, api_key, trust_env, cancelled):
    """Batch wrapper keeps timeout constants patchable on this module."""
    slots = asyncio.Semaphore(_MAX_CONCURRENT_REVIEWS)

    async def review(evidence):
        async with slots:
            if cancelled():
                raise ModelStreamCancelled("Auto approval cancelled")
            return await classify(api_key, trust_env, evidence, cancelled)

    tasks = {call_id: asyncio.create_task(review(evidence)) for call_id, evidence in evidence_by_id.items()}
    try:
        done, _ = await asyncio.wait(tasks.values(), timeout=_BATCH_TIMEOUT, return_when=asyncio.FIRST_EXCEPTION)
        if cancelled():
            raise ModelStreamCancelled("Auto approval cancelled")
        return {call_id: task.result() if task in done else MANUAL for call_id, task in tasks.items()}
    finally:
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)


class ApprovalJournal:
    """Use the session saver's connection and transaction lock, or a private memory DB."""
    @staticmethod
    def has_thread(saver, thread):
        """Whether this thread ever recorded an Auto decision, so Ask must guard its replays."""
        journal = getattr(saver, "_auto_approval_journal", None)
        if journal is not None:
            conn, lock = journal.conn, journal.lock
        elif isinstance(saver, SqliteSaver):
            conn, lock = saver.conn, saver.lock
        else:
            return False
        prefix = f"{thread}:"
        with lock:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='auto_approval'").fetchone() is None:
                return False
            # The fixed hash length keeps a thread named "a" from matching keys of thread "a:b".
            return conn.execute(
                "SELECT 1 FROM auto_approval WHERE call_key >= ? AND call_key < ? AND length(call_key) = ? LIMIT 1",
                (prefix, f"{thread};", len(prefix) + 64),
            ).fetchone() is not None

    def __init__(self, saver):
        if isinstance(saver, SqliteSaver):
            self.conn, self.lock = saver.conn, saver.lock
        else:
            self.conn = sqlite3.connect(":memory:", check_same_thread=False)
            self.lock = Lock()
        self._blocks: dict[str, tuple[int, int]] = {}
        with self.lock, self.conn:
            if not self.conn.in_transaction:
                self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute("""CREATE TABLE IF NOT EXISTS auto_approval (
                call_key TEXT PRIMARY KEY, decision TEXT NOT NULL, reason TEXT NOT NULL,
                execution TEXT NOT NULL DEFAULT 'pending', policy_fingerprint TEXT)""")

    def lookup(self, key):
        with self.lock:
            row = self.conn.execute("SELECT decision, reason FROM auto_approval WHERE call_key=?", (key,)).fetchone()
        return Decision(*row) if row else None

    def record(self, key, result, fingerprint):
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO auto_approval(call_key,decision,reason,policy_fingerprint) VALUES(?,?,?,?)",
                (key, result.decision, result.reason, fingerprint),
            )
        return self.lookup(key)

    def count_review(self, thread, decision):
        """Track classifier blocks per thread, in memory only. True when this block reached a limit."""
        with self.lock:
            streak, total = self._blocks.get(thread, (0, 0))
            if decision.decision == "allow":
                streak = 0
            elif decision.decision == "deny":
                streak, total = streak + 1, total + 1
            reached = streak >= _MAX_CONSECUTIVE_BLOCKS or total >= _MAX_SESSION_BLOCKS
            self._blocks[thread] = (0, 0) if reached else (streak, total)
            return reached

    def transition(self, key, before, after):
        with self.lock, self.conn:
            return self.conn.execute("UPDATE auto_approval SET execution=? WHERE call_key=? AND execution=?",
                                     (after, key, before)).rowcount == 1

    def claim(self, key, before, fingerprint, *, interactive=False):
        # Input prompts interrupt inside the tool and must be enterable again on resume.
        after = before if interactive else "started"
        with self.lock, self.conn:
            changed = self.conn.execute(
                "UPDATE auto_approval SET execution=? WHERE call_key=? AND execution=? AND policy_fingerprint=?",
                (after, key, before, fingerprint),
            )
            if changed.rowcount:
                return None
            row = self.conn.execute("SELECT execution FROM auto_approval WHERE call_key=?", (key,)).fetchone()
            if row and row[0] == before:
                return _STALE
            return _REPLAY


def _call_key(thread, role, message, call):
    fields = [thread, role, message.id, call["id"], call["name"], call["args"]]
    return f"{thread}:{sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}"


class _BatchReview(HumanInTheLoopMiddleware):
    def _create_action_and_config(self, tool_call, config, state, runtime):
        action, review = super()._create_action_and_config(tool_call, config, state, runtime)
        action["tool_call_id"] = tool_call["id"]
        return action, review


class AutoApprovalMiddleware(HumanInTheLoopMiddleware):
    """Classify before HITL; the synchronous predicate only consults this batch."""
    @property
    def name(self):
        return "HumanInTheLoopMiddleware"

    def __init__(self, *, spec, backend, controller, journal, gated, trusted=(), ask_only=False):
        """``gated`` is the Ask-mode ``interrupt_on``. ``ask_only`` applies it as is and only guards replays."""
        super().__init__(interrupt_on=gated if ask_only else {})
        self.gated = frozenset(gated)
        self.ask_only = ask_only
        self.spec = spec
        self.backend = backend
        self.controller = controller
        self.journal = journal
        self.trusted = frozenset(trusted)
        self.custom = {tool.name for tool in spec.tools}

    def _sandbox_config(self):
        return getattr(getattr(self.backend, "executor", None), "config", None) or self.spec.sandbox

    def rule(self, call):
        return static_rule(
            call,
            gated=self.gated,
            backend=self.backend,
            sandbox_mode=getattr(self._sandbox_config(), "mode", None),
            trusted=self.trusted,
            custom=self.custom,
        )

    def _policy(self):
        config = self._sandbox_config()
        return build_policy(
            backend=self.backend,
            sandbox_config=config,
            ask_only=self.ask_only,
            trust_env=bool(getattr(self.spec, "trust_env", False)),
        )

    def evidence(self, state, call, environment):
        """Review input with configured credentials replaced, or None when the call cannot be judged."""
        parent = None
        if self.spec.role == "subagent":
            parent = getattr(self.controller, "approval_user_request", None) or ""
        return build_evidence(state["messages"], call, environment, parent_request=parent, secrets=known_secret_refs(
            getattr(self.spec, "settings", None), self.spec.deepseek_api_key,
            min_length=_MIN_SECRET_LEN,
        ))

    async def aafter_model(self, state, runtime):
        message = state["messages"][-1]
        if not isinstance(message, AIMessage) or not message.tool_calls:
            return None
        thread = get_config()["configurable"]["thread_id"]
        if self.ask_only and not any(
            self.journal.lookup(_call_key(thread, self.spec.role, message, call)) for call in message.tool_calls
        ):
            return super().after_model(state, runtime)
        environment, fingerprint = self._policy()
        decisions = {}
        keys = {}
        new_decisions = {}
        remote = {}
        for call in message.tool_calls:
            if self.controller.cancel_requested:
                raise ModelStreamCancelled("Auto approval cancelled")
            call_id = call["id"]
            key = keys[call_id] = _call_key(thread, self.spec.role, message, call)
            cached = self.journal.lookup(key)
            if cached is not None:
                decisions[call_id] = cached
                continue
            decision = MANUAL if self.ask_only else self.rule(call)
            if decision is None:
                evidence = self.evidence(state, call, environment)
                if evidence:
                    remote[call_id] = evidence
                    continue
                decision = MANUAL
            new_decisions[call_id] = decision
        reviewed = await _classify_batch(
            remote, self.spec.deepseek_api_key, self.spec.trust_env, lambda: self.controller.cancel_requested,
        ) if remote else {}
        if self.controller.cancel_requested:
            raise ModelStreamCancelled("Auto approval cancelled")
        for call_id, decision in reviewed.items():
            if self.journal.count_review(thread, decision):
                decision = Decision("ask", decision.reason)
            new_decisions[call_id] = decision
        for call_id, decision in new_decisions.items():
            decisions[call_id] = self.journal.record(keys[call_id], decision, fingerprint)
        if self.spec.role == "subagent":
            return None
        configs = {call["name"]: {
            "allowed_decisions": ["approve", "reject"],
            "when": lambda request: decisions[request.tool_call["id"]].decision == "ask",
        } for call in message.tool_calls}
        update = _BatchReview(configs).after_model(state, runtime)
        if update:
            # HITL removes rejected calls from the AI message before tool dispatch.
            for call in message.tool_calls:
                if decisions[call["id"]].decision == "ask":
                    self.journal.transition(keys[call["id"]], "pending", "approved")
        return update

    def after_model(self, state, runtime):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.aafter_model(state, runtime))
        # A library caller may invoke the synchronous graph inside an asyncio
        # application. Keep LangGraph's context without nesting event loops.
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(copy_context().run, asyncio.run, self.aafter_model(state, runtime)).result()

    def _before_tool(self, request):
        call = request.tool_call
        thread = request.runtime.config["configurable"]["thread_id"]
        message = next(m for m in reversed(request.state["messages"]) if isinstance(m, AIMessage))
        key = _call_key(thread, self.spec.role, message, call)
        decision = self.journal.lookup(key)
        if self.controller.cancel_requested:
            reason = "Cancelled by user."
        elif decision is None and self.ask_only:
            return None, None
        elif decision is None:
            reason = "缺少审批记录，操作已阻止。"
        elif decision.decision == "deny":
            reason = f"{decision.reason}{_BLOCKED_HINT}"
        elif decision.decision == "ask" and self.spec.role == "subagent":
            return None, ToolMessage(
                content=f"此操作需要人工审批，请由父 Agent 重新发起。{decision.reason}",
                name=call["name"], tool_call_id=call["id"], status="error",
                artifact={"auto_approval": "requires_parent_approval"},
            )
        elif self.ask_only and decision.decision == "allow":
            reason = "已退回 Ask；请在当前权限模式下重新发起操作。"
        elif decision in REVOCABLE and self.rule(call) != decision:
            reason = _REVOKED
        else:
            interactive = call["name"] == "request_human_input" and call["name"] not in self.custom
            before = "approved" if decision.decision == "ask" else "pending"
            _, fingerprint = self._policy()
            reason = self.journal.claim(key, before, fingerprint, interactive=interactive)
            if reason is None:
                return (None if interactive else key), None
        return None, ToolMessage(content=reason, name=call["name"], tool_call_id=call["id"], status="error")

    @contextmanager
    def delegation_context(self, request):
        if request.tool_call["name"] not in {"start_task", "start_background_task"}:
            yield
            return
        texts = user_texts(request.state["messages"])
        token = delegating_user_request.set(texts[-1] if texts else "")
        try:
            yield
        finally:
            delegating_user_request.reset(token)

    def wrap_tool_call(self, request, handler):
        key, blocked = self._before_tool(request)
        if blocked is not None:
            return blocked
        with self.delegation_context(request):
            result = super().wrap_tool_call(request, handler) if self.ask_only else handler(request)
        if key:
            self.journal.transition(key, "started", "finished")
        return result

    async def awrap_tool_call(self, request, handler):
        key, blocked = self._before_tool(request)
        if blocked is not None:
            return blocked
        with self.delegation_context(request):
            result = await super().awrap_tool_call(request, handler) if self.ask_only else await handler(request)
        if key:
            self.journal.transition(key, "started", "finished")
        return result
