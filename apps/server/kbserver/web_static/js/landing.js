// landing.js — 未登录说明页（#loginView）的动效编排
//
// 四件事：金尘画布、随滚动逐行入场、花茎生长点亮三个节点、末句「金蔷薇」落一次金粉。
// 只有 inbox.html 引入本模块；收件箱界面（#appView）不 import 这里，
// 已登录用户看到的登录视图始终不渲染，观察器也就停在「未相交」状态，不跑 rAF。

const RM = matchMedia("(prefers-reduced-motion: reduce)").matches;
const view = document.getElementById("loginView");
const clamp = (v, a, b) => (v < a ? a : v > b ? b : v);

// 首屏是 hidden 的：app.js 探测 /v1/auth/me 失败后才 unhide。隐藏期间所有矩形都是 0，
// 量了会得出假结论（花茎一上来就"长满"、节点全不亮），所以等它真的可见再量一次。
function whenShown(fn) {
  const io = new IntersectionObserver((es) => {
    if (!es[0].isIntersecting) return;
    io.disconnect();
    fn();
  }, { threshold: 0 });
  io.observe(view);
}

// ---------- 金尘：远处的小而暗、近处的大而亮，指针与滚动都只挪一点点 ----------
function dustField(canvas) {
  const ctx = canvas.getContext("2d");
  const sprite = makeSprite();
  let W = 0, H = 0, parts = [], clock = 0, last = 0, raf = 0;
  let shown = false, onScreen = false;
  const ptr = { x: 0.5, y: 0.5, tx: 0.5, ty: 0.5 };

  function makeSprite() {
    const r = 16;
    const s = document.createElement("canvas");
    s.width = s.height = r * 2;
    const c = s.getContext("2d");
    const g = c.createRadialGradient(r, r, 0, r, r, r);
    g.addColorStop(0, "rgba(255, 240, 196, .92)");
    g.addColorStop(0.38, "rgba(226, 176, 74, .42)");
    g.addColorStop(1, "rgba(226, 176, 74, 0)");
    c.fillStyle = g;
    c.beginPath();
    c.arc(r, r, r, 0, Math.PI * 2);
    c.fill();
    return s;
  }

  const seed = () => ({
    x: Math.random() * W, y: Math.random() * H,
    d: Math.random(),                  // 景深：决定大小、亮度、上升速度与视差幅度
    p: Math.random() * Math.PI * 2,    // 摆动与呼吸的相位
    sp: 0.35 + Math.random() * 0.75,
  });

  function fit() {
    const dpr = Math.min(devicePixelRatio || 1, 2);
    W = innerWidth;
    H = innerHeight;
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    // 手机地址栏收起会反复触发 resize：只补/裁粒子，不重排已有位置，免得整片金尘跳一下
    const want = Math.round(clamp((W * H) / 19000, 30, 105));
    while (parts.length < want) parts.push(seed());
    if (parts.length > want) parts.length = want;
    draw();
  }

  function draw() {
    ctx.clearRect(0, 0, W, H);
    const sy = scrollY * 0.05;
    ptr.x += (ptr.tx - ptr.x) * 0.06;
    ptr.y += (ptr.ty - ptr.y) * 0.06;
    for (const q of parts) {
      const dep = 0.25 + q.d * 0.75;
      let y = q.y - clock * (7 + q.sp * 15) * dep - sy * dep;
      y = ((y % H) + H) % H;
      let x = q.x + Math.sin(clock * 0.24 * q.sp + q.p) * (5 + q.d * 13)
            + (ptr.x - 0.5) * 26 * dep - (ptr.y - 0.5) * 12 * dep;
      x = ((x % W) + W) % W;
      const r = (0.9 + dep * 2.1) * 2;
      // 明暗只在 .78~1 之间浮动：是要有点活气的尘土，不是眨眼睛的星星
      ctx.globalAlpha = (0.09 + dep * 0.3) * (0.78 + 0.22 * Math.sin(clock * q.sp + q.p));
      ctx.drawImage(sprite, x - r, y - r, r * 2, r * 2);
    }
    ctx.globalAlpha = 1;
  }

  function loop(now) {
    if (!onScreen) { raf = 0; return; }
    const dt = Math.min((now - last) / 1000, 0.05);   // 切回后台标签页也不让金尘瞬移
    last = now;
    clock += dt;
    draw();
    raf = requestAnimationFrame(loop);
  }

  function sync() {
    onScreen = shown && !document.hidden;
    if (RM) { if (onScreen) draw(); return; }
    if (onScreen && !raf) { last = performance.now(); raf = requestAnimationFrame(loop); }
  }

  new IntersectionObserver((es) => { shown = es[0].isIntersecting; sync(); }, { threshold: 0 }).observe(view);
  document.addEventListener("visibilitychange", sync);
  addEventListener("resize", fit, { passive: true });
  if (matchMedia("(pointer: fine)").matches) {
    addEventListener("pointermove", (e) => {
      ptr.tx = e.clientX / innerWidth;
      ptr.ty = e.clientY / innerHeight;
      if (RM) draw();
    }, { passive: true });
  }
  fit();
  sync();
}

// ---------- 逐行入场：同一父级里的 .rv 依次错开 ----------
function reveals() {
  const items = [...view.querySelectorAll(".rv")];
  for (const el of items) {
    const kin = [...el.parentElement.children].filter((c) => c.classList.contains("rv"));
    el.style.setProperty("--i", kin.indexOf(el));
  }
  const io = new IntersectionObserver((es) => {
    for (const e of es) {
      if (!e.isIntersecting) continue;
      e.target.classList.add("in");
      io.unobserve(e.target);
    }
  }, { threshold: 0.05, rootMargin: "0px 0px -10% 0px" });
  for (const el of items) io.observe(el);
}

// ---------- 花茎：滚到哪长到哪，长过节点就把它点亮（点亮不回灭） ----------
function stemGrow() {
  const stem = view.querySelector(".stem");
  const fill = view.querySelector(".stem i");
  const steps = [...view.querySelectorAll(".step")];
  if (!stem || !fill) return;
  if (RM) {
    fill.style.setProperty("--grow", 1);
    for (const s of steps) s.classList.add("lit");
    return;
  }
  let queued = false;
  const measure = () => {
    queued = false;
    const box = stem.getBoundingClientRect();
    if (!box.height || box.bottom < 0 || box.top > innerHeight) return;
    const g = clamp((innerHeight * 0.72 - box.top) / (box.height * 0.86 || 1), 0, 1);
    fill.style.setProperty("--grow", g.toFixed(4));
    const tip = box.top + box.height * g;
    for (const s of steps) {
      if (s.classList.contains("lit")) continue;
      const node = s.querySelector(".node");
      if (node && node.getBoundingClientRect().top + 5 <= tip) s.classList.add("lit");
    }
  };
  addEventListener("scroll", () => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(measure);
  }, { passive: true });
  whenShown(measure);
}

// ---------- 顶栏：吸顶态 + 标题缩成胶囊 + 那条当进度用的发丝线 ----------
function topBar() {
  const top = document.getElementById("landTop");
  const bar = document.getElementById("landProgress");
  const title = view.querySelector(".land-title");
  let queued = false;
  const measure = () => {
    queued = false;
    const y = scrollY;
    top.classList.toggle("stuck", y > 8);
    view.classList.toggle("scrolled", y > 40);
    if (title) {
      // 首屏那行「金蔷薇」整行滚到顶栏下沿之上，就把品牌收成一颗悬着的胶囊：
      // 看着像那行字自己缩了上去。来回 26px 的迟滞带，免得停在边界上时一闪一闪。
      const box = title.getBoundingClientRect();
      if (box.height) {           // 登录视图还 hidden 时矩形全是 0，量了会凭空吸出一颗胶囊
        const line = top.getBoundingClientRect().bottom;
        const on = top.classList.contains("capsule");
        if (on ? box.bottom > line + 26 : box.bottom <= line) top.classList.toggle("capsule", !on);
      }
    }
    const max = document.documentElement.scrollHeight - innerHeight;
    bar.style.setProperty("--p", max > 40 ? (y / max).toFixed(4) : 0);
  };
  addEventListener("scroll", () => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(measure);
  }, { passive: true });
  addEventListener("resize", measure, { passive: true });
  measure();          // 刷新时可能停在页面中间：吸顶态要立刻对上
  whenShown(measure); // 进度条分母（文档总高）要等登录视图真的可见才量得准
}

// ---------- 末句「金蔷薇。」：背光一次亮起，落几粒金粉 ----------
function finale() {
  const word = document.getElementById("roseWord");
  if (!word) return;
  const io = new IntersectionObserver((es) => {
    if (!es[0].isIntersecting) return;
    io.disconnect();
    word.classList.add("lit");
    if (RM) return;
    const r = word.getBoundingClientRect();
    for (let i = 0; i < 10; i++) {
      const m = document.createElement("i");
      const size = 3 + Math.random() * 3;
      m.className = "mote";
      m.style.width = m.style.height = size + "px";
      m.style.left = r.left + r.width * (0.14 + Math.random() * 0.72) + "px";
      m.style.top = r.top + r.height * 0.55 + "px";
      m.style.setProperty("--dx", Math.round((i / 9 - 0.5) * 150 + (Math.random() - 0.5) * 26) + "px");
      m.style.setProperty("--dy", -Math.round(70 + Math.random() * 120) + "px");
      m.style.animationDelay = (Math.random() * 0.4).toFixed(2) + "s";
      view.appendChild(m);
      setTimeout(() => m.remove(), 2300);
    }
  }, { threshold: 0.6 });
  io.observe(word);
}

// ---------- 指针跟随的高光 ----------
function ctaGlow() {
  for (const btn of view.querySelectorAll(".land-cta")) {
    let box = null;
    btn.addEventListener("pointerenter", (e) => {
      if (e.pointerType !== "mouse") return;
      box = btn.getBoundingClientRect();
    });
    btn.addEventListener("pointermove", (e) => {
      if (!box) return;
      btn.style.setProperty("--mx", (((e.clientX - box.left) / box.width) * 100).toFixed(1) + "%");
      btn.style.setProperty("--my", (((e.clientY - box.top) / box.height) * 100).toFixed(1) + "%");
    });
    btn.addEventListener("pointerleave", () => { box = null; });
  }
}

// ---------- 页内锚点：自己滚，好让 fixed 顶栏留出高度 ----------
function anchors() {
  view.addEventListener("click", (e) => {
    const link = e.target.closest('a[href^="#"]');
    if (!link) return;
    const target = document.getElementById(link.getAttribute("href").slice(1));
    if (!target) return;
    e.preventDefault();
    target.scrollIntoView({ behavior: RM ? "auto" : "smooth", block: "start" });
  });
  document.getElementById("landBrand").addEventListener("click", () => {
    scrollTo({ top: 0, behavior: RM ? "auto" : "smooth" });
  });
}

dustField(document.getElementById("dustCanvas"));
reveals();
stemGrow();
topBar();
finale();
ctaGlow();
anchors();
