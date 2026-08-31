from __future__ import annotations

import traceback
from typing import Any

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

from .general import parse_params

# agent 进程由 MaaFW 按任务逐次启动，进程内仅存在单一 tasker；
# 此模块级变量天然限定在单次任务生命周期内，无跨任务干扰风险。
_remaining: int | None = None


def _to_int(value: Any) -> int | None:
    """转换为 int；bool 与非数值返回 None"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


@AgentServer.custom_action("InitPVPBattleCount")
class InitPVPBattleCount(CustomAction):
    """
    参数：
    - target_count: 剩余战斗次数（必填，整数或可转换为整数的字符串）
    """

    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        try:
            return self._run(context, argv)
        except Exception:
            # 异常必须显式返回失败，否则会被 ctypes 静默忽略导致误判成功
            traceback.print_exc()
            return CustomAction.RunResult(success=False)

    def _run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        params = parse_params(argv.custom_action_param)

        target = _to_int(params.get("target_count"))
        if target is None:
            print(f"[PVP] InitPVPBattleCount: target_count 必须是整数，得到: {params.get('target_count')!r}")
            return CustomAction.RunResult(success=False)

        global _remaining
        _remaining = target
        print(f"[PVP] 剩余战斗次数: {target}")
        return CustomAction.RunResult(success=True)


@AgentServer.custom_action("CheckPVPBattleCount")
class CheckPVPBattleCount(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        try:
            return self._run(context, argv)
        except Exception:
            # 异常必须显式返回失败，否则会被 ctypes 静默忽略导致误判成功
            traceback.print_exc()
            return CustomAction.RunResult(success=False)

    def _run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult:
        global _remaining
        if _remaining is None:
            print("[PVP] CheckPVPBattleCount: remaining 未初始化，请先调用 InitPVPBattleCount")
            return CustomAction.RunResult(success=False)

        _remaining -= 1

        if _remaining <= 0:
            print("[PVP] 战斗次数已用完，返回主界面")
            _remaining = None
            context.override_pipeline(
                {
                    # 用完：回主页结束任务，并提示一次（toast）
                    "PVP_Do:CheckBattleCount": {
                        "next": ["PVP_Click:MainMenu"],
                        "focus": {
                            "Node.PipelineNode.Starting": {
                                "content": "PVP 战斗次数已打完，返回主页",
                                "display": ["log", "toast"],
                            },
                        },
                    },
                }
            )
            return CustomAction.RunResult(success=True)

        # 进度提示走 focus（Node.PipelineNode.Starting：本节点命中后进入其 next 评估时发送，
        # 携带本节点自身 focus，MFAAvalonia 与 MXU 均显示；仅日志，不打扰）
        context.override_pipeline(
            {
                "PVP_Do:CheckBattleCount": {
                    "focus": {
                        "Node.PipelineNode.Starting": {
                            "content": f"PVP 剩余战斗次数: {_remaining}",
                            "display": ["log"],
                        },
                    },
                },
            }
        )
        print(f"[PVP] 剩余战斗次数: {_remaining}")
        return CustomAction.RunResult(success=True)
