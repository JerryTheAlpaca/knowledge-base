// touch-bloom.js — 点一下花冠：指尖迸一小片金粉，手下那一块花瓣（好几层）一起向外胀一下
//
// 两朵共用（说明页首屏 .rose-bloom、收件箱首页 svg.rose）。两份内联副本只是 id 前缀不同，
// 「花心 = .plant 里那个 translate(300 260) 的 g，一圈花瓣 = 一个带滤镜的 g 套一个 use#RingN」
// 是同构的，所以这里按结构找层、不写死 gd/lg 前缀，以后改造型不用回来改本模块。

const RM = matchMedia("(prefers-reduced-motion: reduce)").matches;
const COLORS = ["#fffdf2", "#fff3d0", "#ffe4a8", "#f4ca72"];
const MAX_SPARKS = 70;   // 连点时给粉粒一个上限，否则页面上会堆出一片金雾
let sparks = 0;

export function attachBloomTouch(svg) {
  if (!svg || RM) return;
  const plant = svg.querySelector(".plant");
  const core = plant && plant.querySelector("g[transform]");
  const layers = plant
    ? [...plant.querySelectorAll("g[filter] > use")]
        .filter((u) => /Ring\d+$/.test(u.getAttribute("href") || ""))
        .map((u) => u.parentElement)
    : [];
  if (!core || layers.length < 2) return;

  // 首次点击才量层：未登录时 loginView 是 hidden 的，那时候量什么都是 0
  let geo = null;
  function measure() {
    const rings = layers.map((layer) => {
      const b = layer.getBBox();
      layer.style.transformBox = "fill-box";
      layer.style.transformOrigin = "center";   // 原点必须在花心，scale 才是往外胀
      return { layer, reach: Math.max(-b.x, b.x + b.width, -b.y, b.y + b.height) };
    }).sort((a, b) => b.reach - a.reach);
    rings.forEach((ring, at) => { ring.at = at; });
    const gap = (rings[0].reach - rings[rings.length - 1].reach) / (rings.length - 1) || 12;
    geo = { rings, gap, pad: rings[0].reach + gap * 0.6 };
  }

  // 轻翻：整块一起胀出去再落回来，权重 k 决定这一层出多少力
  function nudge(ring, k) {
    if (ring.anim) ring.anim.cancel();
    ring.anim = ring.layer.animate([
      { transform: "scale(1)" },
      { offset: 0.26, transform: `scale(${(1 + 0.055 * k).toFixed(4)})` },
      { transform: "scale(1)" },
    ], { duration: 760, easing: "cubic-bezier(.2, .72, .28, 1)" });
  }

  function flash(x, y) {
    const el = document.createElement("i");
    el.className = "touch-flash";
    el.style.left = x + "px";
    el.style.top = y + "px";
    document.body.appendChild(el);
    el.animate([
      { opacity: 0, transform: "scale(.35)" },
      { opacity: 1, transform: "scale(1)", offset: 0.3 },
      { opacity: 0, transform: "scale(1.5)" },
    ], { duration: 520, easing: "cubic-bezier(.2, .7, .3, 1)" })
      .addEventListener("finish", () => el.remove());
  }

  // 迸粉：往外撒一小撮并往上飘一点，跟着这页金尘的脾气，不是四散的烟花
  function burst(x, y) {
    flash(x, y);
    const n = 13 + Math.floor(Math.random() * 4);
    for (let i = 0; i < n && sparks < MAX_SPARKS; i++) {
      const p = document.createElement("i");
      p.className = "gold-spark";
      const s = (1.6 + Math.random() * 2).toFixed(1);
      p.style.cssText = `width:${s}px;height:${s}px;background:${COLORS[i % COLORS.length]};left:${x}px;top:${y}px`;
      document.body.appendChild(p);
      sparks++;
      const a = Math.random() * Math.PI * 2;
      const d = 13 + Math.random() * 30;
      const dx = Math.cos(a) * d;
      const dy = Math.sin(a) * d * 0.8 - 13 - Math.random() * 12;
      p.animate([
        { transform: "translate(0,0) scale(.4)", opacity: 0 },
        { transform: `translate(${(dx * 0.4).toFixed(1)}px,${(dy * 0.4).toFixed(1)}px) scale(1)`, opacity: 1, offset: 0.24 },
        { transform: `translate(${dx.toFixed(1)}px,${dy.toFixed(1)}px) scale(.5)`, opacity: 0 },
      ], { duration: 640 + Math.random() * 260, easing: "cubic-bezier(.2, .7, .3, 1)", fill: "forwards" })
        .addEventListener("finish", () => { p.remove(); sparks--; });
    }
  }

  svg.addEventListener("click", (e) => {
    const ctm = core.getScreenCTM();
    if (!ctm) return;
    if (!geo) measure();
    const p = new DOMPoint(e.clientX, e.clientY).matrixTransform(ctm.inverse());
    const r = Math.hypot(p.x, p.y);
    if (r > geo.pad) return;   // 方框四角是空的，点在花冠外就别冒粉
    let hit = geo.rings[0], bd = Infinity;
    for (const ring of geo.rings) {
      const d = Math.abs(ring.reach - r);
      if (d < bd) { bd = d; hit = ring; }
    }
    // 不是一圈应，是一整块应：压住的那层出全力，左右邻层搭一把，按层数退到零。
    // 半径相近的层本来就叠在一起，只动一层看不出是花在动，好几层一起才看得出来。
    for (const ring of geo.rings) {
      const band = Math.max(0, 1 - Math.abs(ring.at - hit.at) / 3.4);
      const near = 1 - Math.min(1, Math.abs(ring.reach - r) / (geo.gap * 2));
      const w = band * (0.55 + 0.45 * near);
      if (w > 0.06) nudge(ring, w);
    }
    burst(e.clientX, e.clientY);
  });
}
