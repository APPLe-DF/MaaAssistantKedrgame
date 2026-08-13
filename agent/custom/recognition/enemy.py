from __future__ import annotations

import json
from typing import Any

import numpy as np

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_recognition import CustomRecognition


@AgentServer.custom_recognition("Enemy_Healthbar")
class EnemyHealthbarRecognition(CustomRecognition):
    """基于血条的敌方单位识别器（目前仅敌人血条；载具后续再扩展）。

    原理：
      敌方敌人血条约 157x32，黑色底，含几个特征色块（颜色范围为 RGB 顺序）：
        - 弹药区（黄绿 C 形）位于血条左上角 [0,1,27,30]
        - 士气区（蓝色横条）位于 [32,26,117,4]
        - 血量区（红）、护甲区（白）等，状态下降时会露出黑色底
      以"血量红"为主锚点定位血条（敌方血量红、我方血量绿，可据此区分敌我；
      血量不满时红条变短但仍可定位），底部士气条 / 左上角弹药区作辅助锚点；
      再按固定偏移计算敌方单位位置。

    返回（detail.enemies，按屏幕从上到下、从左到右排序）：
      [{"bar": [x, y, w, h], "unit": [x, y, w, h], "type": "敌人"}, ...]

    Custom 使用示例（作为节点被 run_recognition 调用）：
        detail = context.run_recognition("Enemy_Healthbar", image)
        if detail and detail.hit:
            enemies = detail.detail["enemies"]

    custom_recognition_param:
      - roi: 搜索区域 [x, y, w, h]（默认使用节点配置的 roi，未配置则全图）
      - bar_size: 血条尺寸，默认 [157, 32]
      - unit_offset: 血条 -> 单位偏移，默认 [63, 39, 31, 44]
    """

    # 默认血条尺寸与单位偏移（敌人类）
    DEFAULT_BAR_SIZE = [157, 32]
    DEFAULT_UNIT_OFFSET = [63, 39, 31, 44]

    # 特征色块 RGB 颜色范围
    MORALE_LOWER = (44, 99, 127)
    MORALE_UPPER = (97, 197, 243)
    AMMO_LOWER = (57, 74, 0)
    AMMO_UPPER = (148, 189, 5)

    # 验证用辅助色（护甲核心白）
    ARMOR_CORE_LOWER = (251, 251, 251)
    ARMOR_CORE_UPPER = (255, 255, 255)

    # 锚点相对血条的偏移
    MORALE_AT = (32, 26)  # 士气蓝条左上角相对血条
    AMMO_AT = (0, 1)  # 弹药区左上角相对血条
    HP_AT = (31, 18)  # 血量区左上角相对血条 [31,18,60,7]
    MIN_MORALE_W = 40  # 士气蓝条最小宽度（血条宽约 117）
    MORALE_MAX_H = 8  # 士气蓝条最大高度（敌人血条士气条高约 4）
    MIN_BLOCK_AREA = 30  # 连通域最小像素数
    MIN_RED = 20  # 敌方血量红最小像素数（敌方血量红、我方血量绿，可区分敌我）
    HP_BLOCK_MAX_W = 70  # 血量红块最大宽度（血量区约 60 宽）
    HP_BLOCK_MAX_H = 15  # 血量红块最大高度（血量区约 7 高）
    # 弹药区确认：敌人血条左上角必有弹药 C 形（[0,1,27,30]），据此排除无弹药区的误检
    MIN_AMMO_ZONE = 10  # 血条左上角弹药区最小像素数
    AMMO_ZONE_W = 40  # 弹药区检查宽度
    AMMO_ZONE_H = 32  # 弹药区检查高度
    # UI 干扰过滤：顶部/底部大量 UI 元素，识别后丢弃这些区域的框。
    # 底部边界取 585：y=584 的敌方血条（下部遮挡）需保留，而 y=599 的底部 UI 需排除，
    # 600 边界无法区分二者。
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

        # argv.image 为 BGR，特征色块按 RGB 匹配
        img_rgb = argv.image[:, :, ::-1]
        sub = img_rgb[ry : ry + rh, rx : rx + rw]

        enemies = self._find_enemies(sub, (rx, ry), bar_size, unit_offset)
        if not enemies:
            return None

        first_bar = enemies[0]["bar"]
        return CustomRecognition.AnalyzeResult(
            box=(first_bar[0], first_bar[1], first_bar[2], first_bar[3]),
            detail={"enemies": enemies, "count": len(enemies)},
        )

    def _find_enemies(
        self,
        sub: np.ndarray,
        offset: tuple[int, int],
        bar_size: list,
        unit_offset: list,
    ) -> list[dict[str, Any]]:
        bw, bh = bar_size
        morale = self._mask(sub, self.MORALE_LOWER, self.MORALE_UPPER)
        ammo = self._mask(sub, self.AMMO_LOWER, self.AMMO_UPPER)
        red = self._red_mask(sub)
        armor = self._mask(sub, self.ARMOR_CORE_LOWER, self.ARMOR_CORE_UPPER)

        morale_blocks = self._find_blocks(morale, self.MIN_BLOCK_AREA)
        ammo_blocks = self._find_blocks(ammo, self.MIN_BLOCK_AREA)
        red_blocks = self._find_blocks(red, self.MIN_RED)

        bars: list[list[int]] = []

        # 锚点 1：敌方血量红（[31,18,60,7]，最可靠——敌方血量红、我方血量绿，
        # 血量不满时红条变短但仍可定位）
        for hx, hy, hw, hh in red_blocks:
            if hw > self.HP_BLOCK_MAX_W or hh > self.HP_BLOCK_MAX_H:
                continue
            bx, by = hx - self.HP_AT[0], hy - self.HP_AT[1]
            if self._valid_bar(sub, bx, by, bw, bh, red, morale, ammo, armor):
                bars.append([bx, by, bw, bh])

        # 锚点 2：底部士气条（血量红被完全遮挡时的兜底）
        for mx, my, mw, mh in morale_blocks:
            if mw < self.MIN_MORALE_W or mh > self.MORALE_MAX_H:
                continue
            bx, by = mx - self.MORALE_AT[0], my - self.MORALE_AT[1]
            if self._valid_bar(sub, bx, by, bw, bh, red, morale, ammo, armor):
                bars.append([bx, by, bw, bh])

        # 锚点 3：左上角弹药 C 形（备用兜底）
        if not bars:
            for ax, ay, aw, ah in ammo_blocks:
                if aw > bw * 0.4 or ah > 40:
                    continue
                bx, by = ax, ay - 1
                if self._valid_bar(sub, bx, by, bw, bh, red, morale, ammo, armor):
                    bars.append([bx, by, bw, bh])

        bars = self._dedupe_bars(bars)

        # UI 干扰过滤：仅当搜索区域足够高（完整战斗截图）时，丢弃顶部/底部 UI 区域的框。
        # 基于全局 y（b[1]+offset[1]）；血条裁剪等小区域不应用，避免误杀。
        if sub.shape[0] > self.MAX_BAR_Y - self.MIN_BAR_Y:
            bars = [
                b
                for b in bars
                if not (
                    b[1] + offset[1] < self.MIN_BAR_Y
                    or b[1] + offset[1] > self.MAX_BAR_Y
                )
            ]

        enemies: list[dict[str, Any]] = []
        for bar in bars:
            gx, gy = bar[0] + offset[0], bar[1] + offset[1]
            enemies.append(
                {
                    "bar": [gx, gy, bar[2], bar[3]],
                    "unit": [
                        gx + unit_offset[0],
                        gy + unit_offset[1],
                        unit_offset[2],
                        unit_offset[3],
                    ],
                    "type": "敌人",  # 载具后续再扩展
                }
            )
        enemies.sort(key=lambda e: (e["unit"][1], e["unit"][0]))
        return enemies

    def _valid_bar(
        self,
        sub: np.ndarray,
        bx: int,
        by: int,
        bw: int,
        bh: int,
        red: np.ndarray,
        morale: np.ndarray,
        ammo: np.ndarray,
        armor: np.ndarray,
    ) -> bool:
        """验证候选血条为敌方敌人血条。

        敌人血条必有红色血量（我方血条血量是绿色，会被血量红验证排除）；
        可能被遮挡/贴边，故只要求：与搜索区重叠足够 + 框内有红色血量 +
        至少一个辅助特征（士气/弹药/护甲白，防纯红色误检）。
        """
        ox0, oy0 = max(bx, 0), max(by, 0)
        ox1, oy1 = min(bx + bw, sub.shape[1]), min(by + bh, sub.shape[0])
        if ox1 <= ox0 or oy1 <= oy0:
            return False
        # 与搜索区重叠不足一半 → 视为其它血条碎片，丢弃
        inter = (ox1 - ox0) * (oy1 - oy0)
        if inter < (bw * bh) * 0.5:
            return False
        # 敌方：框内必须有红色血量
        red_cnt = int(red[oy0:oy1, ox0:ox1].sum())
        if red_cnt < self.MIN_RED:
            return False
        # 敌人血条必有左上角弹药 C 形（[0,1,27,30]），据此排除无弹药区的误检
        az0, ay0 = max(bx, 0), max(by, 0)
        az1, ay1 = min(bx + self.AMMO_ZONE_W, sub.shape[1]), min(
            by + self.AMMO_ZONE_H, sub.shape[0]
        )
        if az1 > az0 and ay1 > ay0:
            ammo_zone = int(ammo[ay0:ay1, az0:az1].sum())
        else:
            ammo_zone = 0
        if ammo_zone < self.MIN_AMMO_ZONE:
            return False
        # 辅助特征：士气/弹药/护甲白任一（防纯红色误检）
        aux = (
            int(morale[oy0:oy1, ox0:ox1].sum())
            + int(ammo[oy0:oy1, ox0:ox1].sum())
            + int(armor[oy0:oy1, ox0:ox1].sum())
        )
        return aux >= self.MIN_BLOCK_AREA

    @staticmethod
    def _red_mask(img: np.ndarray) -> np.ndarray:
        """敌方血量红色掩码：R 明显高于 G、B（我方血量是绿色，据此可区分敌我）"""
        R = img[:, :, 0].astype(np.int32)
        G = img[:, :, 1].astype(np.int32)
        B = img[:, :, 2].astype(np.int32)
        return (R >= 90) & (R >= G + 30) & (R >= B + 30)

    @staticmethod
    def _mask(img: np.ndarray, lower: tuple, upper: tuple) -> np.ndarray:
        return np.all(
            (img >= np.array(lower, dtype=np.int32))
            & (img <= np.array(upper, dtype=np.int32)),
            axis=-1,
        )

    @staticmethod
    def _find_blocks(
        mask: np.ndarray, min_area: int = 20
    ) -> list[tuple[int, int, int, int]]:
        """基于 8 邻域连通域查找，返回 [(x0, y0, w, h), ...]（原图坐标）"""
        h, w = mask.shape
        ys, xs = np.where(mask)
        pixels = set(zip(ys.tolist(), xs.tolist()))
        blocks: list[tuple[int, int, int, int]] = []
        while pixels:
            seed = pixels.pop()
            stack = [seed]
            comp: list[tuple[int, int]] = []
            while stack:
                y, x = stack.pop()
                comp.append((y, x))
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        if dy == 0 and dx == 0:
                            continue
                        ny, nx = y + dy, x + dx
                        if 0 <= ny < h and 0 <= nx < w and (ny, nx) in pixels:
                            pixels.remove((ny, nx))
                            stack.append((ny, nx))
            if len(comp) >= min_area:
                ys2 = [p[0] for p in comp]
                xs2 = [p[1] for p in comp]
                blocks.append(
                    (
                        min(xs2),
                        min(ys2),
                        max(xs2) - min(xs2) + 1,
                        max(ys2) - min(ys2) + 1,
                    )
                )
        return blocks

    @staticmethod
    def _dedupe_bars(bars: list[list[int]]) -> list[list[int]]:
        """按中心距离合并同一血条（两个锚点可能定位到同一个血条）"""
        kept: list[list[int]] = []
        for b in bars:
            cx, cy = b[0] + b[2] / 2, b[1] + b[3] / 2
            dup = False
            for k in kept:
                kx, ky = k[0] + k[2] / 2, k[1] + k[3] / 2
                if abs(cx - kx) < 40 and abs(cy - ky) < 20:
                    dup = True
                    break
            if not dup:
                kept.append(b)
        return kept
