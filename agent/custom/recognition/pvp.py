from __future__ import annotations

import traceback

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_recognition import CustomRecognition
from maa.pipeline import JOCR, JRecognitionType

from ..action.general import log_message, parse_params


def _ocr_text(detail) -> str:
    """取 OCR 识别文本（best_result，回退 all_results）"""
    if detail is None:
        return ""
    result = getattr(detail, "best_result", None)
    text = getattr(result, "text", "") or ""
    if text:
        return text
    for result in getattr(detail, "all_results", None) or []:
        text = getattr(result, "text", "") or ""
        if text:
            return text
    return ""


@AgentServer.custom_recognition("SelectPVPOpponent")
class SelectPVPOpponent(CustomRecognition):
    """识别三个对手的等级，选择等级最低的进行点击"""

    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> CustomRecognition.AnalyzeResult | None:
        try:
            return self._analyze(context, argv)
        except Exception:
            # 异常必须显式返回失败，否则会被 ctypes 静默忽略导致误判成功
            traceback.print_exc()
            return None

    def _analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> CustomRecognition.AnalyzeResult | None:
        params = parse_params(argv.custom_recognition_param)

        rois = params.get("rois", [])
        click_positions = params.get("click_positions", [])
        only_rec = params.get("only_rec", True)

        if len(rois) != 3 or len(click_positions) != 3:
            log_message(
                f"[PVP] SelectPVPOpponent: 需要3个ROI和3个点击位置，得到 roi={len(rois)} click={len(click_positions)}"
            )
            return None

        image = argv.image

        best_value: float | None = None
        best_index: int = -1

        for i, roi in enumerate(rois):
            detail = context.run_recognition_direct(
                JRecognitionType.OCR,
                JOCR(roi=tuple(roi), only_rec=only_rec),
                image,
            )
            text = _ocr_text(detail)
            try:
                value = float(text)
                log_message(f"[PVP] 对手{i + 1} 等级: {value}")
                if best_value is None or value < best_value:
                    best_value = value
                    best_index = i
            except (ValueError, TypeError):
                log_message(f"[PVP] 对手{i + 1} 无法识别等级: '{text}'")

        if best_index < 0:
            log_message("[PVP] SelectPVPOpponent: 未能识别任何对手的等级")
            return None

        click_x, click_y = click_positions[best_index]
        log_message(
            f"[PVP] 选择对手{best_index + 1} (等级最低: {best_value}), 点击位置: [{click_x}, {click_y}]"
        )

        return CustomRecognition.AnalyzeResult(
            box=[click_x, click_y, 10, 10],
            detail={
                "selected_index": best_index + 1,
                "selected_value": best_value,
            },
        )


@AgentServer.custom_recognition("ReadPVPResult")
class ReadPVPResult(CustomRecognition):
    """读取PVP战斗结果"""

    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> CustomRecognition.AnalyzeResult | None:
        try:
            return self._analyze(context, argv)
        except Exception:
            # 异常必须显式返回失败，否则会被 ctypes 静默忽略导致误判成功
            traceback.print_exc()
            return None

    def _analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> CustomRecognition.AnalyzeResult | None:
        params = parse_params(argv.custom_recognition_param)

        result_roi = params.get("result_roi", [500, 150, 300, 100])
        current_score_roi = params.get("current_score_roi", [500, 300, 200, 60])
        score_change_roi = params.get("score_change_roi", [710, 300, 100, 60])
        current_rank_roi = params.get("current_rank_roi", [500, 400, 200, 60])
        rank_change_roi = params.get("rank_change_roi", [710, 400, 100, 60])

        image = argv.image

        result_detail = context.run_recognition_direct(
            JRecognitionType.OCR,
            JOCR(roi=tuple(result_roi), only_rec=True),
            image,
        )

        if not result_detail or not result_detail.box:
            return None

        result_text = _ocr_text(result_detail)

        current_score = _ocr_text(
            context.run_recognition_direct(
                JRecognitionType.OCR,
                JOCR(
                    roi=tuple(current_score_roi),
                    only_rec=True,
                    color_filter="PVP_TextFilter",
                ),
                image,
            )
        )
        score_change = _ocr_text(
            context.run_recognition_direct(
                JRecognitionType.OCR,
                JOCR(
                    roi=tuple(score_change_roi),
                    only_rec=True,
                    color_filter="PVP_TextFilter",
                ),
                image,
            )
        )
        current_rank = _ocr_text(
            context.run_recognition_direct(
                JRecognitionType.OCR,
                JOCR(
                    roi=tuple(current_rank_roi),
                    only_rec=True,
                    color_filter="PVP_TextFilter",
                ),
                image,
            )
        )
        rank_change = _ocr_text(
            context.run_recognition_direct(
                JRecognitionType.OCR,
                JOCR(
                    roi=tuple(rank_change_roi),
                    only_rec=True,
                    color_filter="PVP_TextFilter",
                ),
                image,
            )
        )

        score_change_fmt = self._format_change(score_change)
        rank_change_fmt = self._format_change(rank_change)

        # 高级账号失败保护：仅以分数是否变化为准（分数变化区域 OCR 为空即视为保护）。
        # 实机存在「分数不变但排名仍下降」的情况，因此不能再要求排名也无变化，
        # 否则这类正常战斗会被误判为高账保护。
        protected = not score_change_fmt
        if protected:
            # 判定依据仅记 debug：结果文案会由下方 PVP_Click:ExitResult 的 focus 统一输出一次，
            # 避免同一条结果在识别器日志与节点 focus 中重复出现。
            log_message("[PVP] 未识别到分数变化，判定为高级账号失败保护")
            result_msg = (
                f"高账失败保护触发：本场不扣分，积分:{current_score or '-'} 排名:{current_rank or '-'}"
            )
        else:
            result_msg = (
                f"{result_text} 积分:{current_score}({score_change_fmt}) "
                f"排名:{current_rank}({rank_change_fmt})"
            )

        # 结果文案只通过节点 focus 输出一次（PVP_Read:Result 自身的 focus 仅提示“详情见日志”，
        # 详情集中在本节点，避免同一条变化结果打印两次）。
        # focus 键使用 Node.PipelineNode.Starting：该消息携带节点自身的 focus，
        # 且 MFAAvalonia 与 MXU 均支持此命名空间（MXU 另支持 Node.Action.*）。
        context.override_pipeline(
            {
                "PVP_Click:ExitResult": {
                    "focus": {
                        "Node.PipelineNode.Starting": {
                            "content": result_msg,
                            "display": ["log"],
                        },
                    },
                },
            }
        )

        return CustomRecognition.AnalyzeResult(
            box=result_detail.box,
            detail={
                "result": result_text or "战斗结束",
                "current_score": current_score or "-",
                "score_change": score_change_fmt or "-",
                "current_rank": current_rank or "-",
                "rank_change": rank_change_fmt or "-",
                "protected": protected,
            },
        )

    @staticmethod
    def _format_change(text: str) -> str:
        """格式化变化值，确保有正负号"""
        if not text:
            return ""
        if text.startswith(("+", "-")):
            return text
        return f"+{text}"
