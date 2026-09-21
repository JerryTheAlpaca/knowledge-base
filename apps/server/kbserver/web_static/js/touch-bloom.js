// touch-bloom.js — 点一下花冠：指尖迸一小片金粉，就近那一圈花瓣向外轻翻
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
    const rings = layers.map((layer, i) => {
      const b = layer.getBBox();
      // 翻的方向跟着所属那层的 --dir（首屏有），收件箱那朵没有就按奇偶交替
      const dir = parseFloat(layer.parentElement.style.getPropertyValue("--dir")) || (i % 2 ? -1 : 1);
      layer.style.transformBox = "fill-box";
      layer.style.transformOrigin = "center";
      return { layer, dir, reach: Math.max(-b.x, b.x + b.width, -b.y, b.y + b.height) };
    }).sort((a, b) => b.reach - a.reach);
    const gap = (rings[0].reach - rings[rings.length - 1].reach) / (rings.length - 1) || 12;
    geo = { rings, gap, pad: rings[0].reach + gap * 0.6 };
  }

  // 轻翻：胀出去一点再落回来，落在瓣尖上时最明显，落在两层中间就只是抖一下
  function nudge(ring, k) {
    if (ring.anim) ring.anim.cancel();
    const out = 1 + 0.028 * k;
    const deg = ring.dir * 1.25 * k;
    ring.anim = ring.layer.animate([
      { transform: "scale(1) rotate(0deg)" },
      { offset: 0.26, transform: `scale(${out.toFixed(4)}) rotate(${deg.toFixed(2)}deg)` },
      { offset: 0.62, transform: `scale(${(1 + (out - 1) * 0.3).toFixed(4)}) rotate(${(-deg * 0.26).toFixed(2)}deg)` },
      { transform: "scale(1) rotate(0deg)" },
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
    let near = geo.rings[0], bd = Infinity;
    for (const ring of geo.rings) {
      const d = Math.abs(ring.reach - r);
      if (d < bd) { bd = d; near = ring; }
    }
    nudge(near, 1 - 0.45 * Math.min(1, bd / geo.gap));
    burst(e.clientX, e.clientY);
  });
}
