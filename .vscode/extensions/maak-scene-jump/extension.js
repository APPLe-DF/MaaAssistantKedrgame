'use strict';
/**
 * MAK SceneJump 节点跳转 —— 工作区扩展入口。
 *
 * 为 agent/custom/action/scene_jump_map.json 提供两类引用的跳转：
 *   - 场景引用（scenes 场景名 key、parent、from、to、via）→ 跳到 scenes.<name> 定义
 *   - 节点引用（detect、jump）→ 跳到 pipeline 资源中的节点定义
 * 并提供：悬停提示、查找引用（Shift+F12）、文档链接（下划线可点击）、节点名补全。
 *
 * 能力与 nekosu.maa-support 的 Pipeline 语义分析同源（建立节点索引并解析引用），
 * 因 maa-support 未暴露公共 API，此处独立实现其等价逻辑（见 lib/sceneMap.js）。
 */

const path = require('path');
const vscode = require('vscode');
const {
  scanJsoncStrings,
  refKind,
  isSceneRef,
  tokenAt,
  fieldAt,
  buildNodeIndex,
  offsetToPosition,
} = require('./lib/sceneMap');

const MAP_PATTERN = '**/agent/custom/action/scene_jump_map.json';

let nodeIndex = new Map(); // pipeline 节点名 -> 定义位置（资源文件变更时重建）

/**
 * 从 interface.json 的 resource[].path 推导真实资源目录（与 Maa 插件一致）。
 * 优先使用 assets/interface.json；找不到时回退到 assets/resource*。
 * 只索引真实游戏资源，不会把 install/install-mxu 等部署快照扫进来。
 */
async function resolveResourcePaths() {
  const root = vscode.workspace.workspaceFolders?.[0]?.uri;
  const dirs = new Set();
  const addIface = async (iface) => {
    try {
      const doc = await vscode.workspace.openTextDocument(iface);
      const obj = JSON.parse(doc.getText());
      for (const res of obj.resource || []) {
        for (const p of res.path || []) {
          dirs.add(vscode.Uri.joinPath(iface, '..', p));
        }
      }
    } catch {
      /* 忽略无法解析的 interface.json */
    }
  };

  if (root) {
    const primary = vscode.Uri.joinPath(root, 'assets', 'interface.json');
    try {
      await vscode.workspace.fs.stat(primary);
      await addIface(primary);
    } catch {
      const uris = await vscode.workspace.findFiles(
        '**/interface.json',
        '**/{node_modules,.git,deps}/**',
        20
      );
      for (const u of uris) await addIface(u);
    }
    if (!dirs.size) {
      for (const d of ['assets/resource', 'assets/resource_bilibili', 'assets/resource_taptap']) {
        dirs.add(vscode.Uri.joinPath(root, d));
      }
    }
  }
  return [...dirs];
}

function rel(p) {
  const root = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  return root && p.startsWith(root) ? path.relative(root, p) : p;
}

function docTokens(doc) {
  return scanJsoncStrings(doc.getText());
}

/**
 * 从当前文档现算场景信息（不依赖缓存，编辑后立即可用）。
 * 返回 { names: Set, locs: Map<场景名, Location>, defs: Map<场景名, { desc, def }> }
 */
function buildSceneInfo(doc) {
  const names = new Set();
  const locs = new Map();
  const defs = new Map();
  const toks = docTokens(doc);
  let scenesObj = {};
  try {
    const obj = JSON.parse(doc.getText());
    if (obj && obj.scenes && typeof obj.scenes === 'object') scenesObj = obj.scenes;
  } catch {
    /* 非严格 JSON 时仅用 token 信息 */
  }
  for (const t of toks) {
    if (t.isKey && t.segments.length === 1 && t.segments[0] === 'scenes') {
      names.add(t.value);
      locs.set(
        t.value,
        new vscode.Location(
          doc.uri,
          new vscode.Range(doc.positionAt(t.start), doc.positionAt(t.end))
        )
      );
      const data =
        scenesObj[t.value] && typeof scenesObj[t.value] === 'object'
          ? scenesObj[t.value]
          : {};
      defs.set(t.value, {
        desc: typeof data.desc === 'string' ? data.desc : '',
        def: `"${t.value}": ${JSON.stringify(data, null, 4)}`,
      });
    }
  }
  return { names, locs, defs };
}

async function rebuildIndex() {
  const dirs = await resolveResourcePaths();
  const files = [];
  for (const dir of dirs) {
    for (const sub of ['pipeline', 'tasks']) {
      const uris = await vscode.workspace.findFiles(
        new vscode.RelativePattern(vscode.Uri.joinPath(dir, sub), '**/*.{json,jsonc}'),
        '**/node_modules/**',
        20000
      );
      for (const uri of uris.slice(0, 8000)) {
        try {
          const doc = await vscode.workspace.openTextDocument(uri);
          files.push({ path: uri.fsPath, text: doc.getText() });
        } catch {
          /* 无法打开的文件跳过 */
        }
      }
    }
    // 与 Maa 插件一致，额外收录资源根目录下的 default_pipeline.json
    const defUri = vscode.Uri.joinPath(dir, 'default_pipeline.json');
    try {
      await vscode.workspace.fs.stat(defUri);
      const doc = await vscode.workspace.openTextDocument(defUri);
      files.push({ path: defUri.fsPath, text: doc.getText() });
    } catch {
      /* 不存在则跳过 */
    }
  }
  nodeIndex = buildNodeIndex(files);
}

function defsFor(token, kind, sceneInfo) {
  if (kind === 'scene') {
    // 场景 key 本身即定义，不提供跳转
    return [];
  }
  if (isSceneRef(kind)) {
    // 场景引用：跳到 scene_jump_map.json 的 scenes.<name> 定义
    const loc = sceneInfo.locs.get(token.value);
    return loc ? [loc] : [];
  }
  // 节点引用（detect / jump）：跳到 pipeline 资源中的节点定义
  return (nodeIndex.get(token.value) || []).map((d) => {
    const pos = offsetToPosition(d.text, d.offset);
    const start = new vscode.Position(pos.line, pos.col);
    return new vscode.Location(
      vscode.Uri.file(d.path),
      new vscode.Range(start, start.translate(0, token.value.length))
    );
  });
}

async function activate(context) {
  await rebuildIndex();

  const watcher = vscode.workspace.createFileSystemWatcher(
    '**/resource*/**/*.{json,jsonc}'
  );
  watcher.onDidChange(() => rebuildIndex());
  watcher.onDidCreate(() => rebuildIndex());
  watcher.onDidDelete(() => rebuildIndex());
  context.subscriptions.push(watcher);

  const selector = { language: 'json', pattern: MAP_PATTERN };

  // 定义跳转（Ctrl+Click / F12）
  context.subscriptions.push(
    vscode.languages.registerDefinitionProvider(selector, {
      provideDefinition(doc, position) {
        const t = tokenAt(docTokens(doc), doc.offsetAt(position));
        if (!t) return null;
        const kind = refKind(t);
        if (!kind) return null;
        const locs = defsFor(t, kind, buildSceneInfo(doc));
        return locs.length ? locs : null;
      },
    })
  );

  // 悬停提示
  context.subscriptions.push(
    vscode.languages.registerHoverProvider(selector, {
      provideHover(doc, position) {
        const t = tokenAt(docTokens(doc), doc.offsetAt(position));
        if (!t) return null;
        const kind = refKind(t);
        if (!kind) return null;
        const sceneInfo = buildSceneInfo(doc);
        const md = new vscode.MarkdownString();
        const scene = sceneInfo.defs.get(t.value);

        if (kind === 'scene') {
          // 场景定义本身：显示名称 + desc（无跳转）
          md.appendMarkdown(`**${t.value}** \`(场景定义)\``);
          if (scene && scene.desc) md.appendMarkdown(`\n\n${scene.desc}`);
          return new vscode.Hover(md);
        }

        if (isSceneRef(kind)) {
          // 场景引用：显示 desc + 场景定义内容 + 位置
          const title =
            scene && scene.desc ? `**${t.value}** — ${scene.desc}` : `**${t.value}**`;
          md.appendMarkdown(`${title}  \`(${kind} · 场景)\``);
          if (scene) {
            md.appendCodeblock(scene.def, 'json');
            const loc = sceneInfo.locs.get(t.value);
            if (loc) {
              md.appendMarkdown(
                `\n定义位置: \`${rel(loc.uri.fsPath)}:${loc.range.start.line + 1}\``
              );
            }
          } else {
            md.appendMarkdown('\n\n⚠ 该场景未在 scenes 中定义');
          }
          return new vscode.Hover(md);
        }

        // 节点引用（detect / jump）：显示节点名 + pipeline 定义位置
        const defs = nodeIndex.get(t.value) || [];
        md.appendMarkdown(`**${t.value}**  \`(${kind} · 节点)\``);
        if (!defs.length) {
          md.appendMarkdown('\n\n⚠ 未在 pipeline 资源中找到该节点定义');
        } else {
          md.appendMarkdown('\n\n**定义位置**\n\n');
          for (const d of defs.slice(0, 6)) {
            const pos = offsetToPosition(d.text, d.offset);
            md.appendMarkdown(`- \`${rel(d.path)}:${pos.line + 1}\`\n`);
          }
          if (defs.length > 6) md.appendMarkdown(`- …另有 ${defs.length - 6} 处\n`);
        }
        return new vscode.Hover(md);
      },
    })
  );

  // 查找引用（Shift+F12）
  context.subscriptions.push(
    vscode.languages.registerReferenceProvider(selector, {
      provideReferences(doc, position) {
        const t = tokenAt(docTokens(doc), doc.offsetAt(position));
        if (!t || !refKind(t)) return null;
        const out = [];
        for (const x of docTokens(doc)) {
          if (refKind(x) && x.value === t.value) {
            out.push(
              new vscode.Location(
                doc.uri,
                new vscode.Range(doc.positionAt(x.start), doc.positionAt(x.end))
              )
            );
          }
        }
        return out;
      },
    })
  );

  // 文档链接（下划线 + Ctrl+Click）
  context.subscriptions.push(
    vscode.languages.registerDocumentLinkProvider(selector, {
      provideDocumentLinks(doc) {
        const sceneInfo = buildSceneInfo(doc);
        const links = [];
        for (const t of docTokens(doc)) {
          const kind = refKind(t);
          if (!kind || kind === 'scene') continue; // 场景 key 自身即定义，不加链接
          const defs = defsFor(t, kind, sceneInfo);
          if (!defs.length) continue;
          const target = defs[0].uri.with({
            fragment: `L${defs[0].range.start.line + 1}`,
          });
          links.push(
            new vscode.DocumentLink(
              new vscode.Range(doc.positionAt(t.start), doc.positionAt(t.end)),
              target
            )
          );
        }
        return links;
      },
    })
  );

  // 节点名补全
  context.subscriptions.push(
    vscode.languages.registerCompletionItemProvider(
      selector,
      {
        provideCompletionItems(doc, position) {
          const f = fieldAt(doc.getText(), doc.offsetAt(position), docTokens(doc));
          if (!f) return [];
          const items = [];
          const add = (name) => {
            const it = new vscode.CompletionItem(name, vscode.CompletionItemKind.Reference);
            const w = doc.getWordRangeAtPosition(position);
            if (w) it.range = w;
            items.push(it);
          };
          if (f.kind === 'from' || f.kind === 'to' || f.kind === 'parent' || f.kind === 'via') {
            // 场景引用：补全 scenes 中定义的场景名
            for (const s of [...buildSceneInfo(doc).names].sort()) add(s);
          } else {
            // 节点引用（detect / jump）：补全 pipeline 节点名
            for (const n of [...nodeIndex.keys()].sort()) add(n);
          }
          return items;
        },
      },
      '"'
    )
  );

  // 手动刷新索引
  context.subscriptions.push(
    vscode.commands.registerCommand('maak-scene-jump.refreshIndex', async () => {
      await rebuildIndex();
      vscode.window.setStatusBarMessage('SceneJump 节点索引已刷新', 3000);
    })
  );
}

function deactivate() {}

module.exports = { activate, deactivate };
