// 源文件契约检查（docs/20 §6.3、§7.3、§10.1）。
// 目的：执行契约与发现错误。静态扫描不能证明任意 JavaScript 安全，真实隔离见 §9。
import { parseFragment } from 'parse5';
import * as cssTree from 'css-tree';
import * as acorn from 'acorn';

export const DEFAULT_LIMITS = {
  max_html_body_chars: 400_000,
  max_css_chars: 200_000,
  max_js_chars: 300_000,
  max_html_bytes: 10 * 1024 * 1024,
  max_interactions: 8,
  max_steps_per_interaction: 5,
  max_selector_chars: 128,
  max_asset_bytes: 6 * 1024 * 1024,
};

const ALLOWED_TOP_LEVEL = [
  'schema_version', 'title', 'html_body', 'css', 'javascript',
  'dependencies', 'asset_ids', 'reference_ids', 'interactions',
];
// 明确拒绝的能力：网络、导航、弹窗、动态导入、Worker、eval、任意安装与服务器代码。
const BANNED_TAGS = new Set([
  'script', 'iframe', 'frame', 'frameset', 'object', 'embed', 'base', 'link', 'meta',
  'form', 'portal', 'noembed', 'noframes', 'audio', 'video', 'source', 'track', 'applet',
]);
const BANNED_ATTRS = new Set([
  'src', 'srcset', 'action', 'formaction', 'data', 'poster', 'background', 'ping',
  'autofocus', 'form', 'httpequiv', 'http-equiv',
]);
// 只允许页内锚点（不产生对外导航）：href 值必须以 # 开头

function err(errors, code, message, field) {
  errors.push({ code, message, ...(field ? { field } : {}) });
}

function isPlainObject(value) {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function strList(errors, value, field, { maxItems, allowed = null, required = false } = {}) {
  if (value === undefined || value === null) {
    if (required) err(errors, 'SCHEMA_INVALID', `${field} 必填`, field);
    return [];
  }
  if (!Array.isArray(value) || value.some((v) => typeof v !== 'string' || !v)) {
    err(errors, 'SCHEMA_INVALID', `${field} 必须是字符串数组`, field);
    return [];
  }
  if (value.length > maxItems) err(errors, 'SCHEMA_INVALID', `${field} 最多 ${maxItems} 项`, field);
  if (allowed) {
    for (const v of value) {
      if (!allowed.has(v)) err(errors, 'DEPENDENCY_NOT_ALLOWED', `${field} 含未登记的项：${v}`, field);
    }
  }
  return value;
}

/** 校验并规范化 page_source.json（未知字段一律拒绝）。 */
export function checkPageSource(doc, { limits = DEFAULT_LIMITS, allowedImports, knownAssets, knownReferences } = {}) {
  const errors = [];
  if (!isPlainObject(doc)) {
    err(errors, 'SCHEMA_INVALID', 'page_source 顶层必须是对象');
    return { errors, source: null };
  }
  for (const key of Object.keys(doc)) {
    if (!ALLOWED_TOP_LEVEL.includes(key)) {
      err(errors, 'SCHEMA_INVALID', `不允许的字段：${key}（模型不得提供构建配置或服务器字段）`, key);
    }
  }
  if (doc.schema_version !== '1.0') err(errors, 'VERSION_UNSUPPORTED', 'schema_version 仅支持 1.0', 'schema_version');
  for (const [field, max] of [['title', 200], ['html_body', limits.max_html_body_chars], ['css', limits.max_css_chars], ['javascript', limits.max_js_chars]]) {
    const value = doc[field];
    if (typeof value !== 'string') {
      err(errors, 'SCHEMA_INVALID', `${field} 必须是字符串`, field);
    } else if (value.length > max) {
      err(errors, 'SCHEMA_INVALID', `${field} 超过 ${max} 字符`, field);
    }
  }
  if (typeof doc.title === 'string' && !doc.title.trim()) err(errors, 'SCHEMA_INVALID', 'title 不能为空', 'title');
  if (typeof doc.html_body === 'string' && !doc.html_body.trim()) err(errors, 'SCHEMA_INVALID', 'html_body 不能为空', 'html_body');

  const deps = strList(errors, doc.dependencies, 'dependencies', { maxItems: 12, allowed: allowedImports ?? new Set() });
  const assetIds = strList(errors, doc.asset_ids, 'asset_ids', { maxItems: 200 });
  const refIds = strList(errors, doc.reference_ids, 'reference_ids', { maxItems: 200 });
  if (knownAssets) {
    for (const id of assetIds) if (!knownAssets.has(id)) err(errors, 'ASSET_UNKNOWN', `引用了未登记的素材：${id}`, 'asset_ids');
  }
  if (knownReferences) {
    for (const id of refIds) if (!knownReferences.has(id)) err(errors, 'REFERENCE_UNKNOWN', `引用了未知来源 ID：${id}`, 'reference_ids');
  }

  const interactions = [];
  if (doc.interactions !== undefined && doc.interactions !== null) {
    if (!Array.isArray(doc.interactions) || doc.interactions.length > limits.max_interactions) {
      err(errors, 'SCHEMA_INVALID', `interactions 必须是不超过 ${limits.max_interactions} 项的数组`, 'interactions');
    } else {
      doc.interactions.forEach((it, idx) => interactions.push(checkInteraction(it, idx, limits, errors)));
    }
  }
  if (errors.length) return { errors, source: null, interactions: [] };

  const source = {
    schema_version: '1.0',
    title: doc.title.trim(),
    html_body: doc.html_body,
    css: doc.css ?? '',
    javascript: doc.javascript ?? '',
    dependencies: [...new Set(deps)],
    asset_ids: [...new Set(assetIds)],
    reference_ids: [...new Set(refIds)],
    interactions: interactions.filter(Boolean),
  };
  return { errors, source };
}

function checkSelector(errors, value, field) {
  if (typeof value !== 'string' || !value.trim()) {
    err(errors, 'SCHEMA_INVALID', `${field} 必须是非空选择器`, field);
    return null;
  }
  const sel = value.trim();
  if (sel.length > DEFAULT_LIMITS.max_selector_chars) {
    err(errors, 'SCHEMA_INVALID', `${field} 选择器过长`, field);
    return null;
  }
  if (!/^[A-Za-z0-9_#.\-:*>[\]="'~\s,()+]+$/.test(sel) || /:has-text|>>|\bor\b|\//.test(sel)) {
    err(errors, 'SCHEMA_INVALID', `${field} 只允许普通 CSS 选择器`, field);
    return null;
  }
  return sel;
}

function checkInteraction(raw, idx, limits, errors) {
  const field = `interactions[${idx}]`;
  if (!isPlainObject(raw)) {
    err(errors, 'SCHEMA_INVALID', `${field} 必须是对象`, field);
    return null;
  }
  for (const key of Object.keys(raw)) {
    if (!['id', 'description', 'steps', 'expectation'].includes(key)) {
      err(errors, 'SCHEMA_INVALID', `${field} 不允许的字段：${key}`, field);
    }
  }
  const id = typeof raw.id === 'string' && raw.id.trim() ? raw.id.trim().slice(0, 64) : null;
  if (!id) err(errors, 'SCHEMA_INVALID', `${field}.id 必填`, `${field}.id`);
  if (typeof raw.description !== 'string' || !raw.description.trim()) {
    err(errors, 'SCHEMA_INVALID', `${field}.description 必填（说明这一步在页面上做什么）`, `${field}.description`);
  }
  if (!Array.isArray(raw.steps) || !raw.steps.length || raw.steps.length > limits.max_steps_per_interaction) {
    err(errors, 'SCHEMA_INVALID', `${field}.steps 必须是 1..${limits.max_steps_per_interaction} 项`, `${field}.steps`);
    return null;
  }
  const steps = [];
  raw.steps.forEach((step, sIdx) => {
    const sField = `${field}.steps[${sIdx}]`;
    if (!isPlainObject(step)) {
      err(errors, 'SCHEMA_INVALID', `${sField} 必须是对象`, sField);
      return;
    }
    for (const key of Object.keys(step)) {
      if (!['action', 'target', 'value'].includes(key)) err(errors, 'SCHEMA_INVALID', `${sField} 不允许的字段：${key}`, sField);
    }
    if (!['click', 'fill', 'select', 'set_range'].includes(step.action)) {
      err(errors, 'SCHEMA_INVALID', `${sField}.action 只允许 click/fill/select/set_range`, `${sField}.action`);
      return;
    }
    const target = checkSelector(errors, step.target, `${sField}.target`);
    if (!target) return;
    let value = step.value;
    if (step.action === 'click') {
      if (value !== undefined) err(errors, 'SCHEMA_INVALID', `${sField}.value 在 click 中不允许`, `${sField}.value`);
      value = undefined;
    } else if (typeof value !== 'string' && typeof value !== 'number') {
      err(errors, 'SCHEMA_INVALID', `${sField}.value 必填`, `${sField}.value`);
      return;
    }
    steps.push({ action: step.action, target, ...(value === undefined ? {} : { value: String(value) }) });
  });
  const expectation = checkExpectation(raw.expectation, `${field}.expectation`, errors);
  if (!steps.length || !expectation) return null;
  return { id, description: String(raw.description).slice(0, 200), steps, expectation };
}

function checkExpectation(raw, field, errors) {
  if (!isPlainObject(raw)) {
    err(errors, 'SCHEMA_INVALID', `${field} 必填（没有可核对结果的交互不要声明）`, field);
    return null;
  }
  for (const key of Object.keys(raw)) {
    if (!['kind', 'target', 'value'].includes(key)) err(errors, 'SCHEMA_INVALID', `${field} 不允许的字段：${key}`, field);
  }
  const target = checkSelector(errors, raw.target, `${field}.target`);
  if (!target) return null;
  if (!['text_visible', 'element_visible', 'element_hidden', 'value_equals', 'count_equals'].includes(raw.kind)) {
    err(errors, 'SCHEMA_INVALID', `${field}.kind 不支持`, `${field}.kind`);
    return null;
  }
  if (raw.kind !== 'element_visible' && raw.kind !== 'element_hidden' && typeof raw.value !== 'string' && typeof raw.value !== 'number') {
    err(errors, 'SCHEMA_INVALID', `${field}.value 必填`, `${field}.value`);
    return null;
  }
  return { kind: raw.kind, target, ...(raw.value === undefined ? {} : { value: String(raw.value) }) };
}

// ---- HTML ----

function* iterNodes(node) {
  yield node;
  const children = node.childNodes ?? node.content?.childNodes ?? [];
  for (const child of children) yield* iterNodes(child);
}

/** 解析并检查 HTML 片段：禁用标签、内联事件、任何外部资源引用。 */
export function checkHtml(html, { assetIds = new Set() } = {}) {
  const errors = [];
  const warnings = [];
  let root;
  try {
    root = parseFragment(html, { sourceCodeLocationInfo: false });
  } catch (e) {
    err(errors, 'HTML_PARSE', `HTML 解析失败：${e?.message ?? e}`);
    return { errors, warnings, usedAssets: new Set(), nodeCount: 0 };
  }
  const usedAssets = new Set();
  let nodeCount = 0;
  for (const node of iterNodes(root)) {
    if (node.nodeName === '#comment') {
      warnings.push({ code: 'HTML_COMMENT', message: '页面正文含 HTML 注释：注释不会显示给读者，内容请写进正文' });
      continue;
    }
    // parse5 默认树适配器按 DOM 约定：元素节点的 nodeName/tagName 是大写标签名
    if (!node.tagName) continue;
    nodeCount += 1;
    const tag = String(node.tagName ?? '').toLowerCase();
    if (BANNED_TAGS.has(tag)) {
      err(errors, 'HTML_BANNED_TAG', `<${tag}> 不允许：交互写在 javascript 字段，用事件监听注册`);
      continue;
    }
    const attrs = node.attrs ?? [];
    let hrefLike = null;
    for (const attr of attrs) {
      const name = String(attr.prefix ? `${attr.prefix}:${attr.name}` : attr.name).toLowerCase();
      const value = attr.value ?? '';
      if (name.startsWith('on')) {
        err(errors, 'HTML_INLINE_EVENT', `<${tag}> 上有内联事件 ${name}`);
        continue;
      }
      if (name === 'src' || name === 'srcset') {
        err(errors, 'HTML_REMOTE_RESOURCE', `<${tag}> 的 ${name} 不允许：图片改用 <img data-asset="素材 ID">`);
        continue;
      }
      if (name === 'href' || name === 'xlink:href') {
        hrefLike = value;
        if (!value.startsWith('#')) {
          err(errors, 'HTML_REMOTE_RESOURCE', `<${tag}> 的 ${name} 只允许页内锚点 #id；外部链接由外层展示来源`);
        }
        continue;
      }
      if (BANNED_ATTRS.has(name)) {
        err(errors, 'HTML_BANNED_ATTR', `<${tag}> 不允许属性 ${name}`);
        continue;
      }
      if (name === 'data-ref') {
        if (!value.trim()) err(errors, 'HTML_REF_ID', 'data-ref 不能为空');
        continue;
      }
      if (name === 'data-asset') continue;
    }
    if (tag === 'img') {
      const asset = attrs.find((a) => a.name === 'data-asset')?.value?.trim();
      if (!asset) {
        err(errors, 'HTML_ASSET', '<img> 必须用 data-asset 引用已登记素材（没有素材就省略装饰图）');
      } else if (!assetIds.has(asset)) {
        err(errors, 'ASSET_UNKNOWN', `<img> 引用了未登记的素材：${asset}`);
      } else {
        usedAssets.add(asset);
      }
      if (hrefLike !== null) err(errors, 'HTML_REMOTE_RESOURCE', '<img> 不允许 src/href');
    }
    if (tag === 'svg' || tag === 'use') {
      for (const attr of attrs) {
        const name = String(attr.name).toLowerCase();
        if ((name === 'href' || name === 'xlink:href' || name === 'src') && !String(attr.value ?? '').startsWith('#')) {
          err(errors, 'HTML_REMOTE_RESOURCE', `<${tag}> 的 ${name} 只允许引用页内 ID`);
        }
      }
    }
  }
  if (nodeCount > 20000) warnings.push({ code: 'HTML_NODE_COUNT', message: `DOM 节点 ${nodeCount} 个，页面过长` });
  return { errors, warnings, usedAssets, nodeCount };
}

// ---- CSS ----

export function checkCss(css) {
  const errors = [];
  if (!css.trim()) return { errors };
  let ast;
  try {
    ast = cssTree.parse(css, {
      positions: false,
      onParseError(e) {
        throw new Error(`${e.message} (offset ${e.offset})`);
      },
    });
  } catch (e) {
    err(errors, 'CSS_PARSE', `CSS 解析失败：${e.message}`);
    return { errors };
  }
  cssTree.walk(ast, (node) => {
    if (node.type === 'Raw') {
      // css-tree 解析不了的内容会以 Raw 原样保留：说明样式写坏了
      err(errors, 'CSS_UNPARSED', `CSS 有无法解析的片段：${String(node.value ?? '').slice(0, 60)}`);
    }
    if (node.type === 'Atrule') {
      const name = String(node.name ?? '').toLowerCase();
      if (name === 'import') err(errors, 'CSS_REMOTE_RESOURCE', '@import 不允许：样式只能内联在本页 CSS 中');
      if (name === 'charset') err(errors, 'CSS_BANNED_ATRULE', '@charset 不需要（文档已声明 UTF-8）');
    }
    if (node.type === 'Url') {
      err(errors, 'CSS_REMOTE_RESOURCE', `CSS 里的 url(${node.value.slice(0, 80)}) 不允许：图标用内联 SVG 或字符`);
    }
  });
  return { errors };
}

// ---- JavaScript ----

const BANNED_GLOBALS = new Set([
  'fetch', 'XMLHttpRequest', 'WebSocket', 'WebSocketStream', 'EventSource', 'XDomainRequest',
  'RTCPeerConnection', 'webkitRTCPeerConnection', 'importScripts', 'Worker', 'SharedWorker',
  'localStorage', 'sessionStorage', 'indexedDB', 'openDatabase', 'eval',
]);
// 任何对象上的这些成员都拒绝：桥接与表单提交只能由可信外层完成
const BANNED_ANY_MEMBER = new Set(['postmessage', 'submit', 'serviceworker', 'eval']);
// 只在浏览器全局宿主上拒绝，避免把 el.style.top、data.parent 这类正常代码误判
const HOST_MEMBER_BANS = new Set([
  'open', 'close', 'location', 'top', 'parent', 'frames', 'frameelement', 'opener',
  'cookie', 'domain', 'write', 'writeln', 'sendbeacon', 'geolocation', 'clipboard',
  'createnodeiterator', 'evaluate',
]);
const GLOBAL_HOSTS = new Set(['window', 'globalThis', 'self', 'top', 'parent', 'document', 'navigator', 'location']);

function propKey(node) {
  if (!node.computed) return String(node.property?.name ?? '').toLowerCase();
  if (node.property?.type === 'Literal') return String(node.property.value).toLowerCase();
  return null; // 计算属性：无法静态判定
}

function* walkAst(node, seen = new Set()) {
  if (!node || typeof node !== 'object' || seen.has(node)) return;
  if (node.type) yield node;
  for (const key of Object.keys(node)) {
    if (key === 'type' || key === 'start' || key === 'end' || key === 'loc' || key === 'range') continue;
    const value = node[key];
    if (Array.isArray(value)) {
      for (const item of value) yield* walkAst(item, seen);
    } else if (value && typeof value === 'object') {
      yield* walkAst(value, seen);
    }
  }
}

export function checkJs(src, { allowedImports = new Set() } = {}) {
  const errors = [];
  if (!src.trim()) return { errors, imports: [] };
  let ast;
  try {
    ast = acorn.parse(src, {
      ecmaVersion: 'latest', sourceType: 'module', locations: false,
      allowAwaitOutsideFunction: true, allowHashBang: true,
    });
  } catch (e) {
    err(errors, 'JS_PARSE', `JavaScript 语法错误：${e.message}`);
    return { errors, imports: [] };
  }
  const imports = [];
  for (const node of walkAst(ast)) {
    switch (node.type) {
      case 'ImportDeclaration': {
        const spec = node.source?.value;
        imports.push(String(spec));
        if (typeof spec !== 'string' || !allowedImports.has(spec)) {
          err(errors, 'DEPENDENCY_NOT_ALLOWED', `import '${spec}' 不在可用库清单内（也不要写相对路径、URL 或 Node 模块）`);
        }
        break;
      }
      case 'ImportExpression':
        err(errors, 'JS_DYNAMIC_IMPORT', '不允许 import()：依赖必须写成静态 import');
        break;
      case 'Identifier':
        if (BANNED_GLOBALS.has(node.name)) {
          err(errors, 'JS_DENIED_RUNTIME', `不允许使用 ${node.name}（页面无网络、无存储、无 Worker）`);
        }
        break;
      case 'MemberExpression': {
        const key = propKey(node);
        const host = node.object?.type === 'Identifier' ? node.object.name : null;
        if (key === null && node.computed && GLOBAL_HOSTS.has(host)) {
          err(errors, 'JS_COMPUTED_ACCESS', `不允许对 ${host} 使用变量属性访问（绕过运行限制）`);
        }
        if (key && BANNED_ANY_MEMBER.has(key)) {
          err(errors, 'JS_DENIED_RUNTIME', `不允许使用 .${key}（来源展开与外部链接由可信外层负责）`);
        } else if (key && GLOBAL_HOSTS.has(host) && HOST_MEMBER_BANS.has(key)) {
          err(errors, 'JS_DENIED_RUNTIME', `不允许使用 ${host}.${key}：页面无网络、无导航、无弹窗、无存储`);
        }
        break;
      }
      case 'NewExpression': {
        const name = node.callee?.type === 'Identifier' ? node.callee.name : null;
        if (name === 'Function' || name === 'AsyncFunction' || name === 'GeneratorFunction') {
          err(errors, 'JS_EVAL', `不允许 new ${name}()`);
        }
        break;
      }
      case 'CallExpression': {
        const calleeName = node.callee?.type === 'Identifier' ? node.callee.name : null;
        if (calleeName === 'Function') err(errors, 'JS_EVAL', '不允许 Function() 构造器');
        if (calleeName === 'eval') err(errors, 'JS_EVAL', '不允许 eval()');
        if (calleeName === 'importScripts') err(errors, 'JS_DENIED_RUNTIME', '不允许 importScripts()');
        if (
          node.callee?.type === 'MemberExpression' &&
          node.callee.object?.type === 'Identifier' && node.callee.object.name === 'document' &&
          propKey(node.callee) === 'createelement'
        ) {
          const tag = node.arguments?.[0]?.value;
          if (typeof tag === 'string' && BANNED_TAGS.has(tag.toLowerCase())) {
            err(errors, 'JS_DENIED_RUNTIME', `不允许 document.createElement('${tag}')`);
          }
        }
        break;
      }
      case 'TaggedTemplateExpression':
        if (node.tag?.type === 'Identifier' && (node.tag.name === 'eval' || node.tag.name === 'Function')) {
          err(errors, 'JS_EVAL', `不允许 ${node.tag.name}`);
        }
        break;
      default:
        break;
    }
  }
  return { errors: uniqueErrors(errors), imports };
}

function uniqueErrors(errors) {
  const seen = new Set();
  return errors.filter((e) => {
    const k = `${e.code}|${e.message}`;
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });
}
