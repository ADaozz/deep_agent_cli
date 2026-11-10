"""Recovery policy for a previously interrupted session."""

from __future__ import annotations

from dataclasses import dataclass

from agent.sessions.contracts import StopReason


@dataclass
class RecoveryContext:
    text: str | None
    armed: bool = False

    @classmethod
    def for_stop_reason(cls, reason: StopReason) -> "RecoveryContext | None":
        descriptions = {
            StopReason.PENDING: "上一轮未记录正常结束状态，可能已异常退出。",
            StopReason.ABORTED: "上一轮已被用户中止。",
            StopReason.ERROR: "上一轮因执行错误结束。",
        }
        description = descriptions.get(reason)
        if description is None:
            return None
        return cls(
            text="[上一轮运行状态]\n"
            f"{description} 部分操作可能已经产生副作用。"
            "继续前先检查会话检查点和工作区现状。"
            "不要假定未完成的操作已经成功或失败。"
            "遵循用户当前的指令。"
        )
