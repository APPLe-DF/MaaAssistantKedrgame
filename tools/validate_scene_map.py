#!/usr/bin/env python3
"""解析并校验 scene_jump_map.json 中的节点引用。

对 scene_jump_map.json 里每一处“用到节点”的位置（scenes 的场景名 / detect / parent，
edges 的 from / to / jump / via）：
  1. 解析其在 pipeline 资源中的定义位置（文件:行号），输出可点击的跳转清单；
  2. 检查引用到不存在的节点（跨文件一致性校验，JSON Schema 无法表达）。

用法:
    python tools/validate_scene_map.py
    python tools/validate_scene_map.py --map agent/custom/action/scene_jump_map.json
    python tools/validate_scene_map.py --quiet   # 仅输出错误
"""

import argparse
import json
import re
import sys
from pathlib import Path

from validate_schema import find_line_number, load_jsonc

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MAP = REPO_ROOT / "agent" / "custom" / "action" / "scene_jump_map.json"


def scan_node_definitions(resource_dirs):
    """扫描 resource 下 pipeline/tasks 中的 JSON，建立 节点名 -> [(file, line)] 索引。

    line 为 None 表示未能定位到行号。
    """
    index = {}
    for rd in resource_dirs:
        for sub in ("pipeline", "tasks"):
            base = rd / sub
            if not base.exists():
                continue
            files = sorted([*base.rglob("*.json"), *base.rglob("*.jsonc")])
            for fp in files:
                try:
                    data = load_jsonc(fp)
                except Exception as e:
                    print(f"⚠  解析失败 {fp}: {e}", file=sys.stderr)
                    continue
                if not isinstance(data, dict):
                    continue
                for node in data:
                    if not isinstance(node, str):
                        continue
                    line = find_line_number(fp, f"/{node}")
                    index.setdefault(node, []).append((fp, line))
    return index


def find_scene_line(map_path, name):
    """在 scene_jump_map.json 中定位场景名（scenes.<name> key）的行号。"""
    pattern = re.compile(rf'"{re.escape(name)}"\s*:')
    try:
        with open(map_path, encoding="utf-8-sig") as f:
            for i, line in enumerate(f, 1):
                if pattern.search(line):
                    return i
    except Exception:
        pass
    return None


def fmt_defs(defs):
    """把定义列表格式化为可点击的 file:line 文本"""
    if not defs:
        return "（无定义）"
    parts = []
    for fp, line in defs[:3]:
        rel = fp.relative_to(REPO_ROOT).as_posix()
        parts.append(f"{rel}:{line}" if line else f"{rel}:?")
    if len(defs) > 3:
        parts.append(f"…另有 {len(defs) - 3} 处")
    return "、".join(parts)


def main():
    parser = argparse.ArgumentParser(
        description="解析并校验 scene_jump_map.json 的节点引用"
    )
    parser.add_argument(
        "--map", type=str, default=str(DEFAULT_MAP), help="scene_jump_map.json 路径"
    )
    parser.add_argument(
        "--resource-dirs",
        type=str,
        nargs="+",
        default=None,
        help="扫描的资源目录（默认 assets/resource*）",
    )
    parser.add_argument("--quiet", action="store_true", help="仅输出错误")
    args = parser.parse_args()

    map_path = Path(args.map)
    if not map_path.exists():
        print(f"❌ 找不到 map 文件: {map_path}")
        sys.exit(1)

    with open(map_path, encoding="utf-8-sig") as f:
        map_data = json.load(f)
    scenes = map_data.get("scenes", {})
    edges = map_data.get("edges", [])

    if args.resource_dirs:
        resource_dirs = [Path(p) for p in args.resource_dirs]
    else:
        resource_dirs = [
            REPO_ROOT / "assets" / "resource",
            REPO_ROOT / "assets" / "resource_bilibili",
            REPO_ROOT / "assets" / "resource_taptap",
        ]

    index = scan_node_definitions(resource_dirs)

    resolved = []  # (ref_path, node, definitions)
    errors = []  # (ref_path, node, 说明)

    def resolve_node(ref_path, node):
        """节点引用（detect/jump）：须能在 pipeline 资源中解析到定义。"""
        if not node:
            return
        defs = index.get(node)
        if defs:
            resolved.append((ref_path, node, defs))
        else:
            errors.append((ref_path, node, "节点引用未在 pipeline 中找到定义"))

    def check_scene(ref_path, node, kind):
        """场景引用（parent/from/to/via）：须在 scenes 中定义，定义位置即该场景条目。"""
        if not node:
            return
        if node in scenes:
            line = find_scene_line(map_path, node)
            resolved.append((ref_path, node, [(str(map_path), line)]))
        else:
            errors.append((ref_path, node, f"{kind}未在 scenes 中定义"))

    # ---- scenes 引用 ----
    for name, sc in scenes.items():
        detect = sc.get("detect")
        if detect:
            # 显式指定 detect：节点引用，须在 pipeline 中
            resolve_node(f"scenes.{name}.detect", detect)
        else:
            # 未指定 detect：detect 缺省为场景名，场景名须为 pipeline 节点
            resolve_node(f"scenes.{name}", name)
        parent = sc.get("parent")
        if parent:
            check_scene(f"scenes.{name}.parent", parent, "parent")

    # ---- edges 引用 ----
    for i, e in enumerate(edges):
        p = f"edges[{i}]"
        check_scene(f"{p}.from", e.get("from"), "from")
        raw_to = e.get("to")
        if isinstance(raw_to, list):
            # to 支持场景名列表（操作后有概率进入其中任意一个）
            for j, v in enumerate(raw_to):
                if v:
                    check_scene(f"{p}.to[{j}]", v, "to")
        else:
            check_scene(f"{p}.to", raw_to, "to")

        jump = e.get("jump")
        jump_list = jump if isinstance(jump, list) else [jump]
        for j, v in enumerate(jump_list):
            if v:
                ref = f"{p}.jump[{j}]" if isinstance(jump, list) else f"{p}.jump"
                resolve_node(ref, v)

        via = e.get("via") or []
        for j, v in enumerate(via):
            if v:
                check_scene(f"{p}.via[{j}]", v, "via")

    # ---- 输出 ----
    if not args.quiet:
        print("===== 引用解析（scene_jump_map）=====")
        for ref_path, node, defs in resolved:
            print(f"  ✓ {ref_path:38s} {node:42s} -> {fmt_defs(defs)}")
        print()

    for ref_path, node, why in errors:
        print(
            f"::error file={map_path.as_posix()},title=SceneJump 引用错误::{ref_path} = {node!r}: {why}"
        )
        print(f"  ❌ {ref_path} = {node!r}: {why}")

    print()
    if errors:
        print(f"❌ 共 {len(errors)} 处引用错误，{len(resolved)} 处引用正常")
        sys.exit(1)
    print(
        f"✓ 全部 {len(resolved)} 处引用均能解析（场景引用→scenes，节点引用→pipeline）"
    )


if __name__ == "__main__":
    main()
