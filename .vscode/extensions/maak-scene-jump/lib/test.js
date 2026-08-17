'use strict';
/**
 * 纯逻辑自测（node 直接运行，无需 vscode）。
 *   用法: node .vscode/extensions/maak-scene-jump/lib/test.js
 *   校验: 所有节点引用（scenes key/detect/parent、edges from/to/jump/via）
 *          都能在 pipeline 资源中解析到定义，且内部一致性（from/to/parent ∈ scenes）。
 */
const fs = require('fs');
const path = require('path');
const { scanJsoncStrings, refKind, isSceneRef, buildNodeIndex, fieldAt, parseJsonc } = require('./sceneMap');

const REPO_ROOT = path.resolve(__dirname, '..', '..', '..', '..');
const MAP_FILE = path.join(REPO_ROOT, 'agent', 'custom', 'action', 'scene_jump_map.json');
const mapText = fs.readFileSync(MAP_FILE, 'utf-8');

function rglob(dir, exts, out = []) {
  if (!fs.existsSync(dir)) return out;
  for (const ent of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, ent.name);
    if (ent.isDirectory()) rglob(p, exts, out);
    else if (exts.includes(path.extname(p))) out.push(p);
  }
  return out;
}

function resolveResourceDirs() {
  // 与扩展的 resolveResourcePaths 一致：从 assets/interface.json 的 resource[].path 推导
  const dirs = new Set();
  const ifacePath = path.join(REPO_ROOT, 'assets', 'interface.json');
  if (fs.existsSync(ifacePath)) {
    try {
      const iface = parseJsonc(fs.readFileSync(ifacePath, 'utf-8'));
      for (const res of iface.resource || []) {
        for (const p of res.path || []) {
          dirs.add(path.resolve(path.dirname(ifacePath), p));
        }
      }
    } catch (e) {
      console.warn(`解析 interface.json 失败: ${e}`);
    }
  }
  if (!dirs.size) {
    for (const d of ['assets/resource', 'assets/resource_bilibili', 'assets/resource_taptap']) {
      dirs.add(path.join(REPO_ROOT, d));
    }
  }
  return [...dirs];
}

function main() {
  // 1) 建立节点索引
  const files = [];
  for (const res of resolveResourceDirs()) {
    for (const sub of ['pipeline', 'tasks']) {
      for (const fp of rglob(path.join(res, sub), ['.json', '.jsonc'])) {
        files.push({ path: fp, text: fs.readFileSync(fp, 'utf-8') });
      }
    }
  }
  const index = buildNodeIndex(files);
  console.log(`索引节点数: ${index.size}，扫描文件数: ${files.length}`);

  // 2) 扫描 map
  const toks = scanJsoncStrings(mapText);

  // 内部一致性：scenes 场景名集合
  const sceneNames = new Set();
  for (const t of toks) {
    if (t.isKey && t.segments.length === 1 && t.segments[0] === 'scenes') sceneNames.add(t.value);
  }

  const missing = []; // { ref, node, why }
  let refCount = 0;

  for (const t of toks) {
    const kind = refKind(t);
    if (!kind) continue;
    refCount++;
    if (isSceneRef(kind)) {
      // 场景引用（parent/from/to/via）：必须存在于 scenes；scene key 本身即定义
      if (kind !== 'scene' && !sceneNames.has(t.value)) {
        missing.push({ ref: segPath(t.segments), node: t.value, why: '场景引用未在 scenes 中定义' });
      }
      continue;
    }
    // 节点引用（detect / jump）：必须能在 pipeline 中解析到定义
    if (!index.has(t.value)) {
      missing.push({ ref: segPath(t.segments), node: t.value, why: '节点引用未在 pipeline 中找到定义' });
    }
  }

  // 缺省 detect 的场景：detect 缺省为场景名，此时场景名必须能作为 pipeline 节点
  for (const name of sceneNames) {
    const hasDetect = toks.some(
      (x) => !x.isKey && x.segments.length === 3 && x.segments[0] === 'scenes' && x.segments[1] === name && x.segments[2] === 'detect'
    );
    if (!hasDetect && !index.has(name)) {
      missing.push({ ref: `scenes.${name}`, node: name, why: '缺省 detect 的场景名须为 pipeline 节点' });
    }
  }

  console.log(`引用总数: ${refCount}`);
  if (missing.length) {
    for (const m of missing) console.log(`  ❌ ${m.ref} = ${JSON.stringify(m.node)}: ${m.why}`);
    console.log(`❌ ${missing.length} 处引用错误`);
    process.exit(1);
  }
    console.log(`✓ 全部 ${refCount} 处引用均能解析（场景引用→scenes，节点引用→pipeline）`);
}

function segPath(segs) {
  return segs.map((s, i) => (typeof s === 'number' ? `[${s}]` : i === 0 ? s : `.${s}`)).join('');
}

// ── fieldAt（补全定位）自测 ───────────────────────────────────────────
function testFieldAt() {
  const toks = scanJsoncStrings(mapText);

  // 1) 光标落在已完成引用值内 → 返回对应类型
  const fromTok = toks.find((t) => t.segments[0] === 'edges' && t.segments[2] === 'from' && !t.isKey);
  const f1 = fieldAt(mapText, fromTok.start + 1, toks);
  if (!f1 || f1.kind !== 'from') throw new Error(`fieldAt 完成值错误: ${JSON.stringify(f1)}`);

  // 2) 光标在未完成值内（截断文本模拟输入中）→ 返回该字段
  const snip = '{"scenes": {"A": {"detect": "Status_X';
  const stoks = scanJsoncStrings(snip);
  const f2 = fieldAt(snip, snip.length, stoks);
  if (!f2 || f2.kind !== 'detect') throw new Error(`fieldAt 输入中错误: ${JSON.stringify(f2)}`);

  // 3) 光标在非引用位置（desc 值）→ null
  const descTok = toks.find((t) => t.segments[0] === 'scenes' && t.segments[2] === 'desc' && !t.isKey);
  const f3 = fieldAt(mapText, descTok.start + 1, toks);
  if (f3 !== null) throw new Error(`fieldAt 非引用位置错误: ${JSON.stringify(f3)}`);

  console.log('✓ fieldAt 补全定位测试通过');
}
testFieldAt();

// ── to 数组（多目标候选）场景引用分类自测 ─────────────────────────────
function testToArray() {
  const snip =
    '{"scenes":{"A":{},"B":{},"C":{}},"edges":[{"from":"A","to":["B","C"],"jump":["X"]}]}';
  const toks = scanJsoncStrings(snip);
  const kinds = toks.filter((t) => refKind(t)).map((t) => `${t.value}:${refKind(t)}`);
  if (kinds.indexOf('B:to') < 0 || kinds.indexOf('C:to') < 0) {
    throw new Error(`to 数组分类错误: ${kinds.join(',')}`);
  }
  console.log('✓ to 数组场景引用分类测试通过');
}
testToArray();

// ── 残缺 JSON 不死循环自测 ────────────────────────────────────────────
function testMalformed() {
  const cases = [
    '{,"a":1}',
    '{"a":1,,}',
    '{"a":]',
    '[,]',
    '}]},,,',
    '"unterminated',
    '{"a": 1} extra } ,',
    '[[[[[,',
    '{"a": [1,2,,3]}',
    '{"a": "x\\", }',
    '{"scenes": {"MainMenu": {"desc": "主',
    '{"edges": [{"from": "A", "to":',
  ];
  for (const s of cases) {
    const toks = scanJsoncStrings(s); // 若修复前会死循环
    if (!Array.isArray(toks)) throw new Error(`scanJsoncStrings 非数组: ${JSON.stringify(s)}`);
  }
  console.log(`✓ 残缺 JSON 不死循环测试通过（${cases.length} 例）`);
}
testMalformed();

main();
