from __future__ import annotations

import json
from typing import Any

import numpy as np

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_recognition import CustomRecognition

from .enemy import EnemyHealthbarRecognition as _Enemy


@AgentServer.custom_recognition("Vehicle_Healthbar")
class VehicleHealthbarRecognition(_Enemy):
    """基于血量条的载具识别器。

    载具血条约 125x14（不含状态区域），底色为黑色（同敌方单位血条）。
    血量条为白色长方形 [210,210,210]~[255,255,255]（满血约 117x11，
    非满血时缩短、右侧露出黑色底）。血量条极易被遮挡，识别时通过
    "高度在载具血量条范围（约 4~16）的连续白色区域"定位；
    被遮挡分裂的白色段按同行聚类合并。

    返回（detail.vehicles，按屏幕从上到下、从左到右排序）：
      [{"bar": [x, y, w, h], "unit": [x, y, w, h], "type": "载具"}, ...]

    Custom 使用示例：
        detail = context.run_recognition("Vehicle_Healthbar", image)
        if detail and detail.hit:
            vehicles = detail.detail["vehicles"]

    custom_recognition_param:
      - roi: 搜索区域 [x, y, w, h]
      - bar_size: 载具血条尺寸，默认 [125, 14]
      - unit_offset: 血条 -> 单位偏移，默认 [25, 43, 68, 42]
        （由血条 [387,297,125,14] -> 载具 [412,340,68,42] 标定）
    """

    DEFAULT_BAR_SIZE = [125, 14]
    DEFAULT_UNIT_OFFSET = [25, 43, 68, 42]  # 血条 -> 载具本体偏移

    # 载具血量条（白色）
    WHITE_LOWER = (210, 210, 210)
    WHITE_UPPER = (255, 255, 255)
    MIN_WHITE_H = 4  # 血量条白色最小高度
    MAX_WHITE_H = 16  # 血量条白色最大高度
    MIN_WHITE_CNT = 300  # 血条框内白色血量像素下限
    MIN_WHITE_W = 30  # 血量条整体最小宽度

    # 血量条左上角相对血条（实测 temp 满血：白条 (3,2,116,9)）
    HP_AT = (3, 2)
    # 被遮挡的白色段聚类参数
    GROUP_Y_TOL = 5  # 同一血条白色段 y 容差
    GROUP_X_GAP = 25  # 同一血条白色段 x 间隔上限（超过视为不同血条）
    # 排除敌人血条：有红色血量 / 士气蓝的白色条不是载具
    MIN_RED_EXCL = 20
    MIN_MORALE_EXCL = 30
    # UI 干扰过滤
    MIN_BAR_Y = 90
    MAX_BAR_Y = 585

    def analyze(
        self,
        context: Context,
        argv: CustomRecognition.AnalyzeArg,
    ) -> CustomRecognition.AnalyzeResult | None:
        params = json.loads(argv.custom_recognition_param or "{}")
        roi = argv.roi
        rx = getattr(roi, "x", 0)
        ry = getattr(roi, "y", 0)
        rw = getattr(roi, "w", 0)
        rh = getattr(roi, "h", 0)
        if rw <= 0 or rh <= 0:
            rw, rh = argv.image.shape[1], argv.image.shape[0]

        bar_size = params.get("bar_size", self.DEFAULT_BAR_SIZE)
        unit_offset = params.get("unit_offset", self.DEFAULT_UNIT_OFFSET)

        img_rgb = argv.image[:, :, ::-1]
        sub = img_rgb[ry : ry + rh, rx : rx + rw]

        vehicles = self._find_vehicles(sub, (rx, ry), bar_size, unit_offset)
        if not vehicles:
            return None

        first_bar = vehicles[0]["bar"]
        return CustomRecognition.AnalyzeResult(
            box=(first_bar[0], first_bar[1], first_bar[2], first_bar[3]),
            detail={"vehicles": vehicles, "count": len(vehicles)},
        )

    def _find_vehicles(
        self,
        sub: np.ndarray,
        offset: tuple[int, int],
        bar_size: list,
        unit_offset: list,
    ) -> list[dict[str, Any]]:
        bw, bh = bar_size
        white = self._mask(sub, self.WHITE_LOWER, self.WHITE_UPPER)
        red = self._red_mask(sub)
        morale = self._mask(sub, self.MORALE_LOWER, self.MORALE_UPPER)
        black = self._mask(sub, (0, 0, 0), (15, 15, 15))

        # 白色血量条连通块，过滤高度在载具血量条范围的横条
        blocks = self._find_blocks(white, 10)
        strips = [b for b in blocks if self.MIN_WHITE_H <= b[3] <= self.MAX_WHITE_H]

        groups = self._cluster_strips(strips)

        bars: list[list[int]] = []
        for g in groups:
            min_x = min(b[0] for b in g)
            max_x = max(b[0] + b[2] for b in g)
            min_y = min(b[1] for b in g)
            if max_x - min_x < self.MIN_WHITE_W:
                continue
            bx, by = min_x - self.HP_AT[0], min_y - self.HP_AT[1]
            if self._valid_vehicle(sub, bx, by, bw, bh, white, red, morale, black):
                bars.append([bx, by, bw, bh])

        # UI 干扰过滤（仅完整战斗截图时应用）
        if sub.shape[0] > self.MAX_BAR_Y - self.MIN_BAR_Y:
            bars = [
                b
                for b in bars
                if not (
                    b[1] + offset[1] < self.MIN_BAR_Y
                    or b[1] + offset[1] > self.MAX_BAR_Y
                )
            ]

        vehicles: list[dict[str, Any]] = []
        for bar in bars:
            gx, gy = bar[0] + offset[0], bar[1] + offset[1]
            vehicles.append(
                {
                    "bar": [gx, gy, bar[2], bar[3]],
                    "unit": [
                        gx + unit_offset[0],
                        gy + unit_offset[1],
                        unit_offset[2],
                        unit_offset[3],
                    ],
                    "type": "载具",
                }
            )
        vehicles.sort(key=lambda v: (v["unit"][1], v["unit"][0]))
        return vehicles

    def _cluster_strips(
        self, strips: list[tuple[int, int, int, int]]
    ) -> list[list[tuple[int, int, int, int]]]:
        """把被遮挡分裂的白色段聚合成完整血量条。

        同一载具血条的白色段 y 顶接近；组内按 x 排序，间隔超过 GROUP_X_GAP
        视为不同血条而拆开。
        """
        strips = sorted(strips, key=lambda b: (b[1], b[0]))
        groups: list[list[tuple[int, int, int, int]]] = []
        for b in strips:
            placed = False
            for g in groups:
                if any(abs(b[1] - s[1]) <= self.GROUP_Y_TOL for s in g):
                    g.append(b)
                    placed = True
                    break
            if not placed:
                groups.append([b])
        result: list[list[tuple[int, int, int, int]]] = []
        for g in groups:
            g.sort(key=lambda b: b[0])
            seg = [g[0]]
            for prev, cur in zip(g, g[1:]):
                if cur[0] - (prev[0] + prev[2]) > self.GROUP_X_GAP:
                    result.append(seg)
                    seg = [cur]
                else:
                    seg.append(cur)
            result.append(seg)
        return result

    def _valid_vehicle(
        self,
        sub: np.ndarray,
        bx: int,
        by: int,
        bw: int,
        bh: int,
        white: np.ndarray,
        red: np.ndarray,
        morale: np.ndarray,
        black: np.ndarray,
    ) -> bool:
        """验证候选载具血条。

        要求：与搜索区重叠足够；框内有足够白色血量条；无红色血量、无士气蓝
        （区别于敌人血条）；黑色底占比合理（载具血条底色为黑色）。
        """
        ox0, oy0 = max(bx, 0), max(by, 0)
        ox1, oy1 = min(bx + bw, sub.shape[1]), min(by + bh, sub.shape[0])
        if ox1 <= ox0 or oy1 <= oy0:
            return False
        inter = (ox1 - ox0) * (oy1 - oy0)
        if inter < (bw * bh) * 0.6:
            return False
        # 框内白色血量条
        white_cnt = int(white[oy0:oy1, ox0:ox1].sum())
        if white_cnt < self.MIN_WHITE_CNT:
            return False
        # 无红色血量 / 无士气蓝（排除敌人血条）
        red_cnt = int(red[oy0:oy1, ox0:ox1].sum())
        morale_cnt = int(morale[oy0:oy1, ox0:ox1].sum())
        if red_cnt >= self.MIN_RED_EXCL or morale_cnt >= self.MIN_MORALE_EXCL:
            return False
        # 黑色底占比合理（黑底 + 白色血量条占绝大部分）
        black_cnt = int(black[oy0:oy1, ox0:ox1].sum())
        return (black_cnt + white_cnt) >= inter * 0.5
