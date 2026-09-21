import { test } from 'node:test';
import assert from 'node:assert/strict';

import { renderTask, RenderError, sha256Hex } from '../src/build.mjs';
import { sha256Base64 } from '../src/shell.mjs';
import { codesOf, makeTask, pageSource, RUNTIME_VERSION, tinyPng } from './helpers.mjs';

async function build(opts) {
  const ctx = await makeTask(opts);
  try {
    return { ctx, ...(await renderTask({ taskDir: ctx.taskDir, outFile: `${ctx.outDir}/index.html` })) };
  } finally {
    // 断言失败时保留目录便于排查；成功路径清理
    if (!process.env.KB_KEEP_TMP) await ctx.cleanup();
  }
}

async function expectDiagnostics(opts, ...expected) {
  try {
    await build(opts);
  } catch (err) {
    assert.ok(err instanceof RenderError, `期望 RenderError，实际 ${err}`);
    const codes = codesOf(err);
    for (const code of expected) assert.ok(codes.includes(code), `缺少诊断 ${code}，实际 ${codes.join(',')}`);
    return codes;
  }
  assert.fail('构建本应失败');
}

function unescapeAttr(value) {
  return value.replaceAll('&quot;', '"').replaceAll('&lt;', '<').replaceAll('&gt;', '>').replaceAll('&amp;', '&');
}

function childDocFrom(html) {
  const match = /<iframe id="kb-frame"[^>]*srcdoc="([\s\S]*?)"><\/iframe>/.exec(html);
  assert.ok(match, '成品里找不到 srcdoc 子页面');
  return unescapeAttr(match[1]);
}

function inlineScripts(doc) {
  return [...doc.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
}

test('单文件成品：可信外层 + sandbox srcdoc 子页面，CSP 哈希与内联脚本一致', async () => {
  const { html } = await build({ source: pageSource() });
  const text = html.toString('utf8');
  assert.match(text, /<iframe id="kb-frame"[^>]*sandbox="allow-scripts"/);
  assert.ok(!/allow-same-origin/.test(text), '不允许放开 allow-same-origin');
  const child = childDocFrom(text);
  const scripts = inlineScripts(child);
  assert.equal(scripts.length, 1, '子页面应只有一个内联脚本');
  const hash = sha256Base64(Buffer.from(scripts[0], 'utf8'));
  const outerCsp = /<meta http-equiv="Content-Security-Policy" content="([^"]+)">/.exec(text)[1];
  const childCsp = /<meta http-equiv="Content-Security-Policy" content="([^"]+)">/.exec(child)[1];
  assert.ok(outerCsp.includes(`sha256-${hash}`), 'srcdoc 继承外层策略，外层必须含子页面脚本哈希');
  assert.ok(childCsp.includes(`sha256-${hash}`));
  for (const directive of ["connect-src 'none'", "default-src 'none'", "img-src data:", 'font-src data:', "form-action 'none'"]) {
    assert.ok(outerCsp.includes(directive), `外层 CSP 缺少 ${directive}`);
  }
  // 子页面里不能有任何远程资源
  assert.ok(!/https?:\/\//.test(child), '子页面出现远程地址');
});

test('图表与公式依赖按实际使用打包，字体转 data URL', async () => {
  const source = pageSource({
    html_body: '<main><h1>图</h1><canvas id="c" width="300" height="150"></canvas><div id="m"></div><p>正文足够长以便通过主体检查。</p></main>',
    javascript: "import Chart from 'chart.js';\nimport katex from 'katex';\nnew Chart(document.getElementById('c'), { type: 'bar', data: { labels: ['a'], datasets: [{ data: [1] }] } });\nkatex.render('a^2', document.getElementById('m'), { throwOnError: false, trust: false });",
    dependencies: ['chart.js', 'katex'],
  });
  const { html, report } = await build({ source });
  const text = html.toString('utf8');
  const child = childDocFrom(text);
  assert.ok(!/url\(["']?fonts\//.test(child), 'KaTeX 仍引用了远程/相对字体');
  assert.ok(child.includes('data:font/woff2;base64,'), 'KaTeX 字体未内联');
  assert.equal(report.build.esbuild_inputs >= 3, true, '图表依赖没有真正进入构建');
  assert.deepEqual(report.dependencies.map((d) => d.specifier), ['chart.js', 'katex']);
  assert.ok(report.licenses.some((l) => l.name === '@kurkle/color'), '未登记传递依赖的许可证');
});

test('未登记的依赖、Node 模块与相对路径一律拒绝', async () => {
  await expectDiagnostics(
    { source: pageSource({ dependencies: ['d3'], javascript: "import * as d3 from 'd3';\nd3.scaleLinear();" }) },
    'DEPENDENCY_NOT_ALLOWED',
  );
  await expectDiagnostics(
    { source: pageSource({ dependencies: [], javascript: "import fs from 'node:fs';\nfs.readFileSync('x');" }) },
    'DEPENDENCY_NOT_ALLOWED',
  );
  await expectDiagnostics(
    { source: pageSource({ dependencies: [], javascript: "import helper from './helper.js';\nhelper();" }) },
    'DEPENDENCY_NOT_ALLOWED',
  );
  // 声明与实际 import 不一致也要拒绝（防止只声明不用的库被偷偷引入）
  await expectDiagnostics(
    { source: pageSource({ dependencies: [], javascript: "import Chart from 'chart.js';\nnew Chart();" }) },
    'DEPENDENCY_NOT_ALLOWED',
  );
});

test('HTML 契约：禁用标签、内联事件与任何外部资源引用', async () => {
  const cases = [
    ['<main><script>alert(1)</script></main>', 'HTML_BANNED_TAG'],
    ['<main><iframe src="https://evil.example"></iframe></main>', 'HTML_BANNED_TAG'],
    ['<main><meta http-equiv="refresh" content="0;url=https://evil.example"></main>', 'HTML_BANNED_TAG'],
    ['<main><base href="https://evil.example"></main>', 'HTML_BANNED_TAG'],
    ['<main><img src="https://cdn.example/a.png"></main>', 'HTML_REMOTE_RESOURCE'],
    ['<main><a href="https://evil.example">点我</a><p>正文正文正文正文正文。</p></main>', 'HTML_REMOTE_RESOURCE'],
    ['<main><div onclick="steal()">x</div><p>正文正文正文正文正文。</p></main>', 'HTML_INLINE_EVENT'],
    ['<main><object data="x"></object><p>正文正文正文正文正文。</p></main>', 'HTML_BANNED_TAG'],
    ['<main><svg><use href="https://evil.example/i.svg#x"/></svg><p>正文正文正文。</p></main>', 'HTML_REMOTE_RESOURCE'],
    ['<main><img data-asset="nope"></main>', 'ASSET_UNKNOWN'],
  ];
  for (const [body, code] of cases) {
    // eslint-disable-next-line no-await-in-loop
    await expectDiagnostics({ source: pageSource({ html_body: body }) }, code);
  }
});

test('CSS 契约：url() 与 @import 都算资源依赖', async () => {
  await expectDiagnostics(
    { source: pageSource({ css: '@font-face{font-family:X;src:url(https://fonts.example/x.woff2)}' }) },
    'CSS_REMOTE_RESOURCE',
  );
  await expectDiagnostics({ source: pageSource({ css: '@import "https://cdn.example/a.css";' }) }, 'CSS_REMOTE_RESOURCE');
  await expectDiagnostics({ source: pageSource({ css: 'a{background:url(/local.png)}' }) }, 'CSS_REMOTE_RESOURCE');
  // 写坏的 CSS 一定被结构检查发现（解析异常或残留未解析片段）
  await expectDiagnostics({ source: pageSource({ css: 'a{color:red;;;} b{{' }) }, 'CSS_PARSE');
});

test('JS 契约：网络、导航、弹窗、动态导入、Worker 与 eval', async () => {
  const snippets = [
    ["fetch('/v1/items')", 'JS_DENIED_RUNTIME'],
    ["new XMLHttpRequest()", 'JS_DENIED_RUNTIME'],
    ["new WebSocket('wss://x')", 'JS_DENIED_RUNTIME'],
    ["new Worker('/w.js')", 'JS_DENIED_RUNTIME'],
    ["navigator.serviceWorker.register('/w.js')", 'JS_DENIED_RUNTIME'],
    ['eval("1+1")', 'JS_EVAL'],
    ['new Function("return 1")()', 'JS_EVAL'],
    ["import('./x.js')", 'JS_DYNAMIC_IMPORT'],
    ["window.open('https://evil')", 'JS_DENIED_RUNTIME'],
    ["window.location = 'https://evil'", 'JS_DENIED_RUNTIME'],
    ["parent.postMessage({a:1},'*')", 'JS_DENIED_RUNTIME'],
    ["globalThis['fe'+'tch']('/')", 'JS_COMPUTED_ACCESS'],
    ['localStorage.setItem("k","v")', 'JS_DENIED_RUNTIME'],
  ];
  for (const [js, code] of snippets) {
    // eslint-disable-next-line no-await-in-loop
    await expectDiagnostics({ source: pageSource({ javascript: js }) }, code);
  }
  // 正常 DOM 代码不得被误判
  await build({
    source: pageSource({
      html_body: '<main><h1>标题</h1><input id="r" type="range"><output id="o"></output><p>正文正文正文正文。</p></main>',
      javascript: "const r=document.getElementById('r');\nconst o=document.getElementById('o');\nconst el=document.createElement('p');\nel.style.top='0';\nel.textContent='n='+r.value;\no.after(el);\nr.addEventListener('input',()=>{o.textContent=r.value});\nwindow.addEventListener('resize',()=>{});\nrequestAnimationFrame(()=>{});",
    }),
  });
});

test('内联边界：模型文本里的 </script> 与 <!-- 不能提前结束脚本', async () => {
  const source = pageSource({
    html_body: '<main><h1>标题</h1><p>正文里出现 &lt;/title&gt;&lt;script&gt;&lt;iframe&gt; 这样的字面量，必须按文本处理。</p></main>',
    javascript: 'const s = "</script><script>alert(1)</" + "script>";\nconst c = "<!-- -->";\ndocument.title = s.length + c.length;',
  });
  const { html } = await build({ source });
  const text = html.toString('utf8');
  const child = childDocFrom(text);
  assert.equal(inlineScripts(child).length, 1, '子页面脚本被模型文本截断');
  // 脚本内部出现的 </script 必须已被转义成 <\/script：整个子页面只允许一个真实结束标签
  assert.equal((child.match(/<\/script>/g) ?? []).length, 1);
  assert.ok(!child.includes('alert(1)</script>'));
  assert.equal((text.match(/<script>/g) ?? []).length, 1, '外层应只有一个可执行脚本（桥接），其余是 JSON 数据');
  assert.ok(text.includes('<script type="application/json" id="kb-meta">'));
});

test('素材：只接受类型可核对且摘要一致的栅格图片', async () => {
  const png = tinyPng();
  const ok = await build({
    assets: [{ asset_id: 'a1', name: 'a1.png', mime: 'image/png', bytes: png }],
    source: pageSource({
      html_body: '<main><h1>标题</h1><img data-asset="a1" alt="图示"><p>正文正文正文正文。</p></main>',
      asset_ids: ['a1'],
    }),
  });
  assert.ok(ok.html.toString('utf8').includes('data:image/png;base64,'));
  assert.deepEqual(ok.report.assets.map((a) => a.asset_id), ['a1']);
  await expectDiagnostics(
    {
      assets: [{ asset_id: 'a1', name: 'a1.png', mime: 'image/png', bytes: Buffer.from('<html><script>bad()</script></html>') }],
      source: pageSource({ html_body: '<main><img data-asset="a1"></main>', asset_ids: ['a1'] }),
    },
    'ASSET_MIME',
  );
});

test('任务目录边界：未声明文件、缺失文件、摘要篡改与运行时版本', async () => {
  await expectDiagnostics(
    { source: pageSource(), extraFiles: { 'input/extra.json': Buffer.from('{}') } },
    'TASK_FILE_UNDECLARED',
  );
  await expectDiagnostics(
    { source: pageSource(), taskOverrides: { input_hash: '0'.repeat(64) } },
    'INPUT_HASH_MISMATCH',
  );
  await expectDiagnostics(
    { source: pageSource(), taskOverrides: { runtime_version: 'share-runtime-9.9.9' } },
    'RUNTIME_VERSION',
  );
  await expectDiagnostics(
    {
      source: pageSource({ html_body: '<main><img data-asset="gone"></main>', asset_ids: ['gone'] }),
      taskOverrides: { assets: [{ asset_id: 'gone', file: 'assets/gone.png', mime: 'image/png', sha256: sha256Hex(Buffer.from('x')), bytes: 1 }] },
    },
    'TASK_FILE_MISSING',
  );
});

test('交互声明契约：数量、动作、选择器与断言都有上限', async () => {
  const many = Array.from({ length: 9 }, (_, i) => ({
    id: `i${i}`,
    description: '点一下',
    steps: [{ action: 'click', target: '#b' }],
    expectation: { kind: 'element_visible', target: '#b' },
  }));
  await expectDiagnostics({ source: pageSource({ interactions: many }) }, 'SCHEMA_INVALID');
  await expectDiagnostics({
    source: pageSource({
      interactions: [{ id: 'x', description: 'd', steps: [{ action: 'eval', target: '#b' }], expectation: { kind: 'element_visible', target: '#b' } }],
    }),
  }, 'SCHEMA_INVALID');
  await expectDiagnostics({
    source: pageSource({
      interactions: [{ id: 'x', description: 'd', steps: [{ action: 'click', target: 'a:has-text("甲")' }], expectation: { kind: 'element_visible', target: '#b' } }],
    }),
  }, 'SCHEMA_INVALID');
});

test('模型不得提供构建配置或服务器字段', async () => {
  await expectDiagnostics({ source: { ...pageSource(), package_json: {} } }, 'SCHEMA_INVALID');
  await expectDiagnostics({ source: { ...pageSource(), build_command: 'npm i' } }, 'SCHEMA_INVALID');
  await expectDiagnostics({ source: { ...pageSource(), storage_key: 'ab/cd' } }, 'SCHEMA_INVALID');
  await expectDiagnostics({ source: { ...pageSource(), schema_version: '2.0' } }, 'VERSION_UNSUPPORTED');
});

test('体积超限明确失败，不产出半成品', async () => {
  await expectDiagnostics(
    {
      source: pageSource({ html_body: `<main><h1>标题</h1><p>${'很长'.repeat(4000)}</p></main>` }),
      taskOverrides: { limits: { max_html_bytes: 4096 } },
    },
    'HTML_TOO_LARGE',
  );
});

test('引用与覆盖说明只透传公开字段到外层', async () => {
  const { html } = await build({
    source: pageSource({
      html_body: '<main><h1>标题</h1><button type="button" data-ref="ref1">看来源</button><p>正文正文正文正文。</p></main>',
      reference_ids: ['ref1'],
    }),
    references: [{ ref_id: 'ref1', title: '某材料', author: '某人', url: 'https://example.org/a', quote: '原文短引', revision: 3 }],
    taskOverrides: { coverage_notes: ['两篇均已读取完整正文'], limitations: ['缺少第三篇的原始数据'] },
  });
  const text = html.toString('utf8');
  assert.ok(text.includes('某材料') && text.includes('https://example.org/a'));
  assert.ok(text.includes('缺少第三篇的原始数据'));
  assert.ok(!/source_pack|api_key|cookie/i.test(text), '成品出现了私有字段');
});
