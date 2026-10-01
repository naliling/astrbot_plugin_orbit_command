const bridge = window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);

const state = {
  overview: null,
  instances: [],
  timer: null,
  refreshing: false,
  junk: null,
};

const NUMERIC_SETTINGS = [
  "astrbot_cache_threshold_mb",
  "astrbot_logs_threshold_mb",
  "napcat_cache_threshold_mb",
  "napcat_cache_min_age_minutes",
  "napcat_protocol_interval_hours",
  "media_threshold_mb",
  "media_min_age_days",
  "log_retention_days",
  "unreachable_limit",
];

const TOGGLE_SETTINGS = [
  "auto_enabled",
  "auto_clean_plugin_modules",
  "auto_adopt_cache_dirs",
  "auto_clean_plugin_junk",
];

const TEXTAREA_SETTINGS = ["napcat_cache_dirs", "media_dirs", "log_dirs"];

// ---------- 表单脏标记 ----------

// 面板每 30 秒轮询一次，而刷新会重新回填表单。用户正改到一半时，
// 不能拿服务端的旧值把输入内容吹掉。
let dirty = false;

function markDirty() { dirty = true; }
function clearDirty() { dirty = false; }
function formDirty() { return dirty; }

function watchDirty() {
  for (const id of [
    "sweep_cron", ...TOGGLE_SETTINGS, ...TEXTAREA_SETTINGS, ...NUMERIC_SETTINGS,
  ]) {
    const el = $(id);
    if (!el) continue;
    el.addEventListener("input", markDirty);
    el.addEventListener("change", markDirty);
  }
}

// ---------- 外观 ----------

const THEME_KEY = "orbit.theme";
const ACCENT_KEY = "orbit.accent";
const THEMES = ["deep", "cyber", "matrix", "light"];

function store(key, value) {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch (e) {
    /* 面板可能在禁用 storage 的沙箱里，取不到就算了 */
  }
}

function load(key) {
  try {
    return localStorage.getItem(key) || "";
  } catch (e) {
    return "";
  }
}

function hexToRgb(hex) {
  const v = String(hex || "").replace("#", "");
  const full = v.length === 3 ? v.split("").map((c) => c + c).join("") : v;
  const n = parseInt(full, 16);
  if (Number.isNaN(n) || full.length !== 6) return null;
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

// 相对亮度：用来决定主色上的文字该用深色还是浅色
function luminance(hex) {
  const rgb = hexToRgb(hex);
  if (!rgb) return 0.5;
  const [r, g, b] = rgb.map((c) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function applyTheme(theme, accent) {
  const root = document.documentElement;
  const name = THEMES.includes(theme) ? theme : (theme === "dark" ? "deep" : "light");
  root.setAttribute("data-theme", name);
  if (accent && hexToRgb(accent)) {
    const lum = luminance(accent);
    root.style.setProperty("--primary", accent);
    root.style.setProperty(
      "--glow-1",
      `rgba(${hexToRgb(accent).join(",")},${name === "light" ? 0.12 : 0.2})`,
    );
    // 亮主色配深字、暗主色配浅字，否则按钮上的字会糊掉
    root.style.setProperty("--on-primary", lum > 0.45 ? "#08111c" : "#ffffff");
  } else {
    // 自定义主色被清掉时，必须把行内覆盖也抹掉。
    // 行内样式优先级高于主题块，不 removeProperty 就会一直赖着，
    // 「恢复默认」点了等于没点。
    root.style.removeProperty("--primary");
    root.style.removeProperty("--glow-1");
    root.style.removeProperty("--on-primary");
  }
  for (const el of document.querySelectorAll("[data-theme-set]")) {
    el.setAttribute("aria-pressed", String(el.dataset.themeSet === name));
  }
  const picker = document.getElementById("theme-accent");
  // 没自定义过时也把当前主题的主色填进去：留空的话 input[type=color] 会显示成黑色，
  // 用户会以为「主色就是黑的」。
  if (picker) {
    const current = accent
      || (getComputedStyle(document.documentElement).getPropertyValue("--primary") || "").trim()
      || "#38bdf8";
    picker.value = current;
  }
}

// 跟随宿主的深浅色（用户没手动选过的时候才听它的）
function applyHostTheme(isDark) {
  if (load(THEME_KEY)) return;
  applyTheme(isDark ? "deep" : "light", load(ACCENT_KEY));
}

function initTheme() {
  // 这里只先猜一个，免得黑幕未亮时先闪一下浅色。
  // 真正的取值在第一次 refresh() 之后：那时候才会拿到服务端的配置。
  applyTheme(load(THEME_KEY) || "deep", load(ACCENT_KEY));
}

// 外观以**服务端配置**为准。只读 localStorage 的话，
// 存是存进去了、一刷新读不回来，看起来就等于没保存。
// lookOverride 只在「本地刚改、debounce 还没落盘」的那 600ms 里顶一下，
// 免得轮询把用户刚选的颜色弹回旧值。
let lookOverride = null;

function lookFrom(settings) {
  if (lookOverride) return lookOverride;
  const s = settings || {};
  return {
    theme: s.ui_theme || load(THEME_KEY) || "deep",
    accent: s.ui_accent || load(ACCENT_KEY) || "",
  };
}

function setLook(theme, accent) {
  lookOverride = { theme, accent };
  applyTheme(theme, accent);
  store(THEME_KEY, theme || null);
  store(ACCENT_KEY, accent || null);
  saveLook(theme, accent);
}

// 外观要落到后端配置：面板在 iframe 里，sandbox 缺 allow-same-origin 时
// localStorage 会直接抛 SecurityError，写入静默失败，自定义色就「保存不住」。
let savingLook = null;
function saveLook(theme, accent) {
  if (savingLook) clearTimeout(savingLook);
  savingLook = setTimeout(async () => {
    savingLook = null;
    try {
      await bridge.apiPost("settings", { ui_theme: theme, ui_accent: accent });
      // 存成了就交回服务端说了算
      if (lookOverride && lookOverride.theme === theme && lookOverride.accent === accent) {
        lookOverride = null;
      }
    } catch (e) {
      // 存不进去也要告诉用户，不能让他以为已经保存了
      toast("外观没能保存到配置：" + e.message, "bad");
    }
  }, 600);
}

function bindThemeBar() {
  for (const el of document.querySelectorAll("[data-theme-set]")) {
    el.onclick = () => setLook(el.dataset.themeSet, load(ACCENT_KEY));
  }
  const accent = $("theme-accent");
  const pick = () => setLook(load(THEME_KEY) || "deep", accent.value);
  // input 和 change 都要存：不同浏览器/不同 WebView 只触发其中一个，
  // 只绑 input 的话，有的环境压根存不下来。
  accent.oninput = pick;
  accent.onchange = () => {
    pick();
    toast(`主色已改成 ${accent.value}`, "ok");
  };
  $("theme-reset").onclick = () => {
    setLook("deep", "");
    toast("已恢复默认（跟随宿主的深浅色）", "ok");
  };
}

// ---------- 主题 ----------

// ---------- 初始化 ----------

// 首屏占位。在第一份数据回来之前，每个容器都是一个说人话的待命状态，
// 而不是九个空盒子——空 div 看着像加载坏了。
function primePlaceholders() {
  const hints = {
    schedule: "正在读取…",
    layers: "正在读取…",
    plugins: "正在读取…",
    history: "正在读取…",
    dead: "点「检测死实例」开始：只读磁盘与配置，NapCat 挂掉时也查得出重复。",
    junk: "点「扫一下」开始：没装完的插件目录、遗留安装包、__pycache__。",
  };
  for (const [id, text] of Object.entries(hints)) {
    const el = $(id);
    if (el) el.innerHTML = `<div class="empty">${escapeHtml(text)}</div>`;
  }
}

// 初始化放在文件末尾调用，不是在这里。
// 原因很实在：下面那堆 const INTRO_* 要到本文件中段才完成初始化，而
// playIntro() 会在 init() 里**同步**被调用。在声明之前碰到 const 属于 TDZ，
// 直接抛 ReferenceError——而这个错误发生在任何动画开始之前，后果是
// 开场动画静默失效（连 class 都来不及加上），面板看起来只是「没动画」。
// 写成函数声明 + 末尾调用，就彻底绕开了时序问题。
async function init() {
  try {
    const ctx = await bridge.ready();
    applyHostTheme(Boolean(ctx?.isDark));
    bridge.onContext((next) => applyHostTheme(Boolean(next?.isDark)));
  } catch (e) {
    console.warn("bridge context 不可用", e);
    initTheme();
  }
  initTheme();
  bindThemeBar();
  bindEvents();
  watchDirty();
  primePlaceholders();
  // 外观必须先定下来，否则幕布会先闪一下浅色
  playIntro();
  await refresh();
  // 页面在后台时不轮询：既省事也避免看不见时静默改数据
  state.timer = setInterval(() => {
    if (document.visibilityState === "visible") refresh();
  }, 30000);
}

function bindEvents() {
  $("btn-refresh").onclick = refresh;
  // 「按规则立即清理」不是体检，它会真的删。先把会动手的层摆出来再问一次。
  $("btn-sweep").onclick = async () => {
    const due = Object.entries(state.overview?.layers || {})
      .filter(([, l]) => l.enabled && l.due)
      .map(([, l]) => l.label);
    if (!due.length) {
      toast("当前没有超过阈值的层，没什么要清的。定时体检会自己处理。", "warn");
      return;
    }
    const msg = `按规则清理下列已超阈值的层，会真实删除文件：\n\n· ` +
      due.join("\n· ") + "\n\n确定继续？";
    if (await askConfirm(msg)) runSweep(false);
  };
  $("btn-force").onclick = async () => {
    if (await askConfirm("强制全清会无视阈值立即清理所有层，确定吗？")) runSweep(true);
  };
  $("btn-save").onclick = saveSettings;
  $("btn-discover").onclick = discover;
  $("btn-reauth").onclick = reResolve;
  $("btn-add-inst").onclick = () => {
    state.instances.push({ url: "", token: "", enable: true, protocol_interval_hours: 12 });
    markDirty();
    renderInstances();
  };
  $("btn-save-inst").onclick = saveInstances;
  $("btn-dryrun").onclick = dryRun;
  $("btn-dead").onclick = checkDead;
  $("btn-platform").onclick = checkPlatform;
  $("btn-spaces").onclick = showSpaces;
  $("btn-junk").onclick = showJunk;
  $("btn-junk-all").onclick = purgeAllJunk;
  $("notice-dead").onclick = checkDead;
  $("notice-ack").onclick = async () => {
    try {
      await bridge.apiPost("attention", { action: "ack" });
      await refresh();
      toast("已标记为看过了。", "ok");
    } catch (e) {
      toast("标记失败：" + e.message, "bad");
    }
  };
  window.addEventListener("beforeunload", () => {
    if (state.timer) clearInterval(state.timer);
  });
}

// ---------- 页面内对话框 ----------
// 面板跑在 iframe 里，sandbox 没有 allow-modals 时原生 confirm() 会被静默拦掉
// 并返回 false、alert() 返回 null。后果是：点了没反应，也没有任何提示。
// 所以这里全部自绘，不依赖浏览器原生对话框。

function toast(message, kind) {
  const el = $("ui-toast");
  el.textContent = String(message ?? "");
  el.className = "ui-toast" + (kind ? " " + kind : "");
  el.classList.remove("hidden");
  if (toast._timer) clearTimeout(toast._timer);
  toast._timer = setTimeout(() => el.classList.add("hidden"), 5000);
}

function askConfirm(message) {
  return new Promise((resolve) => {
    const layer = $("ui-layer");
    $("ui-msg").textContent = String(message ?? "");
    layer.classList.remove("hidden");
    const done = (value) => {
      layer.classList.add("hidden");
      $("ui-ok").onclick = null;
      $("ui-cancel").onclick = null;
      layer.onclick = null;
      resolve(value);
    };
    $("ui-ok").onclick = () => done(true);
    $("ui-cancel").onclick = () => done(false);
    layer.onclick = (e) => {
      if (e.target === layer) done(false);
    };
  });
}

// ---------- 开场动画 ----------

// 开场总长约 6.5 秒：幕布亮起 0.5s / 标题模糊散开 1.2s / 斜光扫过 1.5s /
// 副标题 0.8s / 到位后停 1.2s / 退幕 1.1s，外加等首屏数据的那一小段。
// 节奏刻意比一般开场慢——缓冲给得足，每个阶段都看得清，
// 退幕也不会卡在副标题刚到位的那一瞬间。
// 数值只有一个出处：style.css 的 :root 变量。CSS 动画和 JS 插值都从那里读，
// 改一个地方两边一起动，不会出现「CSS 放完了 JS 还在等」的半截状态。
const INTRO_STEP = introMs("--intro-title-delay", 220);   // 标题开始入场
const INTRO_MAIN = introMs("--intro-title-in", 1400);      // 标题入场时长
const INTRO_SUB_DELAY = introMs("--intro-sub-delay", 1100); // 副标题入场起点
const INTRO_SUB = introMs("--intro-sub-in", 800);          // 副标题只做渐变
const INTRO_HOLD = 600;        // 全部到位后停一下就退，别吊着
const INTRO_EXIT = introMs("--intro-exit", 800);
const INTRO_FALLBACK = 9000;   // 兜底：无论发生什么，到点必退幕
const INTRO_DATA_WAIT = 2000;  // 数据最多等这么久。再久就先把面板放出来
const INTRO_STARS = 26;        // 周围的小白点数量

function introMs(name, fallback) {
  try {
    const raw = getComputedStyle(document.documentElement)
      .getPropertyValue(name).trim();
    const value = parseFloat(raw);
    if (Number.isFinite(value) && value > 0) {
      return raw.endsWith("ms") ? value : value * 1000;
    }
  } catch (e) {
    /* 读不到就用兜底值 */
  }
  return fallback;
}

const easeOutExpo = (t) => (t === 1 ? 1 : 1 - Math.pow(2, -10 * t));
const easeBack = (t) => 1 + 2.7 * Math.pow(t - 1, 3) + 1.7 * Math.pow(t - 1, 2);

function prefersReducedMotion() {
  try {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch (e) {
    return false;
  }
}

function interpolate(el, from, to, duration, ease, delay, fadeOnly) {
  return new Promise((resolve) => {
    setTimeout(() => {
      const start = performance.now();
      // 两端模糊相同时整段不写 filter。高斯模糊不是合成属性，每帧重算一次
      // 等于把这段文字反复重绘，掉帧全出在这里。
      const hasBlur = from.b !== to.b;
      function frame(now) {
        let t = Math.min((now - start) / duration, 1);
        t = ease(t);
        el.style.opacity = from.o + (to.o - from.o) * t;
        // fadeOnly：只改不透明度。副标题要的就是「直接渐变、完全不位移」，
        // 连模糊都不留。
        if (!fadeOnly) {
          if (hasBlur) {
            el.style.filter = `blur(${from.b + (to.b - from.b) * t}px)`;
          }
          el.style.transform =
            `translateY(${from.y + (to.y - from.y) * t}px) ` +
            `scale(${from.s + (to.s - from.s) * t})`;
        }
        if (t < 1) {
          requestAnimationFrame(frame);
        } else {
          // 终态写死为 1，而不是清空：CSS 里这两个元素初始是 opacity:0，
          // 清空行内样式会让它回落到 0。标题靠 breathe 关键帧救得回来，
          // 副标题没有，就靠这条兜着。
          el.style.opacity = "1";
          if (!fadeOnly) {
            el.style.filter = "";
            el.style.transform = "";
            el.style.willChange = "";
          }
          resolve();
        }
      }
      requestAnimationFrame(frame);
    }, delay);
  });
}

function finishIntro(stage) {
  if (!stage || stage.dataset.done === "1") return;
  stage.dataset.done = "1";
  const root = document.documentElement;
  // 先在幕还全黑的时候把卡片的毛玻璃图层准备好（intro-prep 关掉了它们）。
  // 否则 blur(12px) 会在淡出的同一帧才第一次合成，平板上那一下就是
  // 「卡一卡才缓过来」。等两帧确保图层建完，再开始退。
  root.classList.add("intro-prep");
  requestAnimationFrame(() => requestAnimationFrame(() => {
    root.classList.add("intro-out");
  }));
  setTimeout(() => {
    // 留一个永久隐藏的 class，别把 intro-on / intro-out 摘干净：
    // .intro 的默认样式是「不透明可见」（为了第一帧不闪），摘干净它就回来了。
    root.classList.add("intro-done");
    root.classList.remove("intro-on");
    root.classList.remove("intro-prep");
    root.classList.remove("intro-out");
    stage.dataset.done = "";
  }, INTRO_EXIT);
}

// 周围的小白点。用固定种子的线性同余发生器生成：
// 看起来是随机的，但每次打开面板都是同一片，不会刷新一次换一片星。
function makeStars(host, count) {
  let seed = 20260927;
  const rand = () => {
    seed = (seed * 1103515245 + 12345) & 0x7fffffff;
    return seed / 0x7fffffff;
  };
  const parts = [];
  for (let i = 0; i < count; i++) {
    const size = (1.4 + rand() * 2.2).toFixed(2);
    const left = (rand() * 100).toFixed(2);
    const top = (rand() * 100).toFixed(2);
    const dur = (2.4 + rand() * 4.2).toFixed(2);
    const delay = (-rand() * 5).toFixed(2);
    const lo = (0.10 + rand() * 0.16).toFixed(2);
    const hi = (0.55 + rand() * 0.45).toFixed(2);
    parts.push(
      `<i style="width:${size}px;height:${size}px;left:${left}%;top:${top}%;` +
      `animation-duration:${dur}s;animation-delay:${delay}s;` +
      `--lo:${lo};--hi:${hi}"></i>`,
    );
  }
  host.innerHTML = parts.join("");
}

// 首屏数据有没有到。没到就不许退幕：否则退出去的是九个空盒子，
// 观感正好是「动画放完了页面还是空的，然后突然糊一堆东西出来」。
let introDataReady = false;
const introWaiters = [];

function markIntroDataReady() {
  introDataReady = true;
  const wait = $("intro-wait");
  if (wait) wait.style.opacity = "0";
  while (introWaiters.length) introWaiters.pop()();
}

function waitIntroData() {
  if (introDataReady) return Promise.resolve();
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, INTRO_DATA_WAIT);
    introWaiters.push(() => { clearTimeout(timer); resolve(); });
  });
}

function playIntro() {
  const stage = $("intro");
  const title = $("intro-title");
  const sub = $("intro-sub");
  const stars = $("intro-stars");
  if (!stage || !title || !sub) return;
  const root = document.documentElement;

  // 开了「减少动效」就**立刻把幕布撤掉**，不是「什么都不做」。
  // 幕布现在默认是可见不透明的（为了第一帧不闪），直接 return 会让它
  // 一直挡在 z-index:200 的位置——用户看到的是一块点不动的黑屏，
  // 要等 9 秒的 CSS 兜底才放行。
  if (prefersReducedMotion()) {
    root.classList.add("intro-done");
    return;
  }

  if (stars) makeStars(stars, INTRO_STARS);
  root.classList.add("intro-on");
  // 兜底：万一 rAF 被挂起、或中间抛错，到点必退幕，不能把面板永久挡住
  const bail = setTimeout(() => finishIntro(stage), INTRO_FALLBACK);
  // 不耐烦的人随手点一下/敲一下就跳过
  const skip = () => {
    clearTimeout(bail);
    finishIntro(stage);
    document.removeEventListener("keydown", skip);
    stage.removeEventListener("click", skip);
  };
  document.addEventListener("keydown", skip);
  stage.addEventListener("click", skip);

  (async () => {
    try {
      // 两个动画同时起跑，各自算自己的绝对延迟。
      // 不要写成「等标题跑完再开始副标题」——那样副标题的延迟就成了负数，
      // setTimeout 会当成 0，重叠点也就没了。
      // 标题只插值位移与缩放：模糊由下面那层 .intro-ghost 静态承担，
      // 两层一淡一显，看起来就是「从模糊里显出来」，但没有一帧在重算模糊。
      const titleDone = interpolate(
        title, { o: 0, b: 0, s: 1.08, y: 10 }, { o: 1, b: 0, s: 1, y: 0 },
        INTRO_MAIN, easeOutExpo, INTRO_STEP,
      );
      const subDone = interpolate(
        sub, { o: 0, b: 0, s: 1, y: 0 }, { o: 1, b: 0, s: 1, y: 0 },
        INTRO_SUB, easeOutExpo, INTRO_SUB_DELAY, true,
      );
      // 标题一落定就开始呼吸，正好接上副标题入场
      setTimeout(() => title.classList.add("breathe"), INTRO_STEP + INTRO_MAIN);
      await Promise.all([titleDone, subDone]);
      // 到位后停一下：不给这一拍，最终状态根本看不清就被抽走了
      await new Promise((r) => setTimeout(r, INTRO_HOLD));
    } catch (e) {
      console.warn("开场动画中断", e);
    }
    clearTimeout(bail);
    document.removeEventListener("keydown", skip);
    stage.removeEventListener("click", skip);
    await waitIntroData();
    finishIntro(stage);
  })();
}

/* ---------- 首次设置引导 ----------
   首次配置的门槛是「必须先让 NapCat 对外监听」，这一步很反直觉：
   用户在 NapCat 里配的是反向连接（它主动连出去），而插件需要一个
   能主动连过去的端点。这里把「为什么连不上 → 改哪里 → 填什么」
   摆成三步，不让人对着一个默认的 127.0.0.1:3000 猜。
   ------------------------------------------------ */

const CONNECTED = new Set(["ok", "ok_no_token"]);

function renderSetup(res) {
  const card = $("setup-card");
  const mode = res?.auth?.mode || "unknown";
  if (CONNECTED.has(mode)) {
    card.style.display = "none";
    return;
  }
  card.style.display = "";
  const hint = escapeHtml(res.hint || "还没配置 NapCat 地址。");
  const snip = res.snippet || {};
  const found = (res.configs || []).flatMap((c) =>
    (c.servers || [])
      .filter((s) => s.can_connect && s.enable && s.address)
      .map((s) => ({ file: c.file, address: s.address, has_token: s.has_token })),
  );
  const steps = [];
  steps.push(`<div class="setup-step"><span class="no">1</span><div class="bd">
    <b>为什么现在连不上</b><br>${hint}</div></div>`);
  if (!found.length) {
    steps.push(`<div class="setup-step"><span class="no">2</span><div class="bd">
      <b>让 NapCat 对外监听一个 HTTP 服务端</b><br>
      位置：${escapeHtml(snip.where || "NapCat 面板 → 网络配置 → HTTP 服务端")}<br>
      把下面这段加进去（端口已避开你已占用的）：
      <pre>${escapeHtml(snip.json || "")}</pre>
      <div class="setup-row">
        <button class="btn mini" data-copy="${escapeHtml(snip.json || "")}">复制配置</button>
        <span class="muted">改完保存后点右上角「刷新」重新探测。</span>
      </div></div></div>`);
  } else {
    steps.push(`<div class="setup-step"><span class="no">2</span><div class="bd">
      <b>已经找到可用端点</b><br>点「填入地址」加到实例列表，再点「保存实例」：</div></div>`);
  }
  steps.push(`<div class="setup-step"><span class="no">3</span><div class="bd">
    <b>填入地址并保存</b><br>
    <div class="setup-addr">${escapeHtml(found[0]?.address || snip.address || "http://127.0.0.1:3000")}</div>
    Token 留空即可，插件会自己解析。保存后这块会自动消失。</div></div>`);
  $("setup-body").innerHTML = `<div id="setup-steps">${steps.join("")}</div>`;
  for (const el of $("setup-body").querySelectorAll("[data-copy]")) {
    el.onclick = async () => {
      const text = el.dataset.copy;
      try {
        await navigator.clipboard.writeText(text);
        toast("配置已复制，粘到 NapCat 的 HTTP 服务端里。", "ok");
      } catch (e) {
        toast("浏览器不允许自动复制，请手动选中下面的文本。", "warn");
      }
    };
  }
}

// ---------- 数据 ----------

// 一块渲染失败就把后面全块留在「正在读取」，而失败信息又只写进两个容器里，
// 用户完全看不到——结果就是「一直读取」加上「不知道为什么」。
// 所以每块单独 try，失败当场报出来。
function renderEach(jobs) {
  const failed = [];
  for (const [name, run] of jobs) {
    try {
      run();
    } catch (e) {
      failed.push(`${name}：${(e && e.message) || e}`);
      console.error("渲染失败 " + name, e);
    }
  }
  return failed;
}

// 失败要显现在**最上面**，而不是藏在哪张卡的角落。
function showProblem(title, detail) {
  const badge = $("auto-badge");
  if (badge) {
    badge.textContent = title;
    badge.className = "badge bad";
  }
  const host = $("notice");
  if (!host) return;
  host.hidden = false;
  host.className = "notice bad";
  // **只能填内容，不能整块重写**。这里原来用 innerHTML 重建整条，
  // 把预置的 id="notice-title" / id="notice-items" 一起冲掉了；
  // 于是下一轮 renderAttention 拿到 null、每 30 秒报一次
  // "Cannot set properties of null"——而且再也恢复不了，
  // 一次报错就把自己永久拆了。
  const titleEl = $("notice-title");
  const box = $("notice-items");
  if (titleEl) titleEl.textContent = title;
  if (box) {
    box.innerHTML = `<div class="notice-item bad"><span class="dot"></span>
      <span class="n-body">${escapeHtml(String(detail || "").slice(0, 400))}</span></div>`;
  }
}

async function refresh() {
  // 轮询与手动刷新会撞在一起。overview 慢的时候（目录遍历 + 逐实例探测）
  // 两个请求会同时在飞，后到的旧响应把新数据盖掉。现在的做法是同一时刻
  // 只允许一个在跑，并给刷新按钮一个可见的转圈指示。
  if (state.refreshing) return;
  state.refreshing = true;
  const btn = $("btn-refresh");
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="spin"></span> 刷新中';
  }
  try {
    // 两个请求各自成败：history 挂了不该把 overview 一起拖下水。
    // 用 Promise.all 的话，只要历史那一条出错，已经拿到的总览也会被丢掉。
    const [ovRes, hiRes] = await Promise.allSettled([
      bridge.apiGet("overview"),
      bridge.apiGet("history", { limit: 40 }),
    ]);
    if (ovRes.status === "rejected") throw ovRes.reason;
    const overview = ovRes.value;
    const history = hiRes.status === "fulfilled" ? hiRes.value : { history: [] };
    state.overview = overview;
    // 外观与实例列表的输入都要先算出来，但每一块渲染都单独隔离。
    // 注意顺序：renderInstanceStatus 先把状态写进 state.statusByUrl，
    // renderInstances 再去每行读它，反过来这一轮就没有徽标。
    const look = lookFrom(overview.settings);
    applyTheme(look.theme, look.accent);
    if (!formDirty()) {
      state.instances = (overview.connection?.instances || []).map((r) => ({
        url: r.url, token: r.token || "", enable: r.enable,
        token_set: Boolean(r.token_set),
        protocol_interval_hours: r.protocol_interval_hours,
      }));
    }
    if (state.instances.length === 0 && !state.instancesLoaded) {
      state.instances = [{ url: "http://127.0.0.1:3000", token: "", enable: true,
                           protocol_interval_hours: 12 }];
    }
    state.instancesLoaded = true;
    const failed = renderEach([
      ["调度状态", () => renderSchedule(overview)],
      ["缓存层", () => renderLayers(overview)],
      ["插件模块", () => renderPlugins(overview.module_rows || [])],
      ["清理历史", () => renderHistory(history.history || [])],
      ["待办", () => renderAttention(overview.attention)],
      ["实例状态", () => renderInstanceStatus(overview.instance_status || [])],
      ["实例列表", () => renderInstances()],
      ["实例连接", () => renderInstStatus(overview.napcat?.instances || [])],
      ["最近错误", () => renderLastError(overview.last_error || "")],
      ["设置", () => fillSettings(overview.settings || {})],
    ]);
    if (failed.length) showProblem(`有 ${failed.length} 块没渲染出来`, failed.join("；"));
    if (hiRes.status === "rejected") {
      const box = $("history");
      if (box) box.innerHTML = '<div class="empty">清理历史没读到</div>';
    }
  } catch (e) {
    // 请求本身就失败了：每个容器都换成能看懂的提示，
    // 而不是留一句「正在读取」让人对着它干等
    showProblem("读取失败", (e && e.message) || e);
    for (const id of ["schedule", "layers", "plugins", "history"]) {
      const el = $(id);
      if (el) el.innerHTML = '<div class="empty">没能读到数据，点右上角「刷新」重试</div>';
    }
  } finally {
    // 不管成不成都放行退幕：请求卡住时开场会一直盖着，看起来就像死机
    markIntroDataReady();
    state.refreshing = false;
    if (btn) {
      btn.disabled = false;
      btn.textContent = "刷新";
    }
  }
}

// 清完之后把**逐层结果**摊开。只弹一句「已清理 N 层」等于什么都没说：
// 用户真正想知道的是「动了哪些、释放多少、哪些没动、为什么没动」。
function renderSweepResult(res) {
  const card = $("sweep-card");
  const records = (res && res.records) || [];
  $("sweep-card").style.display = "";
  if (!records.length) {
    $("sweep-summary").textContent = "";
    $("sweep-body").innerHTML = '<div class="empty">没有可执行的层</div>';
    return;
  }
  const freed = Number(res.freed_bytes) || 0;
  const acted = records.filter((r) => r.acted);
  const failed = records.filter((r) => r.error);
  const quiet = records.filter((r) => !r.acted && !r.error);
  const parts = [`动了 ${acted.length} 层`];
  if (freed) parts.push(`共释放 ${humanBytes(freed)}`);
  if (failed.length) parts.push(`${failed.length} 层失败`);
  if (quiet.length) parts.push(`${quiet.length} 层没动`);
  if (res.disk_freed !== null && res.disk_freed !== undefined) {
    const delta = Number(res.disk_freed);
    parts.push(
      delta >= 0
        ? `磁盘实际多了 ${humanBytes(delta)}`
        : `磁盘实际少了 ${humanBytes(-delta)}`
    );
  }
  $("sweep-summary").textContent = parts.join(" · ");
  const line = (r) => {
    const tag = r.error ? "失败" : r.acted ? "已清理" : "没动";
    const cls = r.error ? "find-why bad" : r.acted ? "find-why" : "find-why";
    const detail = r.error || r.detail || r.skipped || "";
    const size = r.freed_bytes ? ` · 释放 ${humanBytes(r.freed_bytes)}` : "";
    return `<div class="find">
      <div class="find-top">
        <span class="find-url">${escapeHtml(r.label || r.layer)}</span>
        <span class="badge ${r.error ? "bad" : r.acted ? "ok" : "off"}">${tag}</span>
        <span class="n">${humanBytes(Number(r.current) || 0)}${size}</span>
      </div>
      ${detail ? `<div class="${cls}">${escapeHtml(detail)}</div>` : ""}
    </div>`;
  };
  const order = (r) => (r.error ? 0 : r.acted ? 1 : 2);
  $("sweep-body").innerHTML = [...records].sort((a, b) => order(a) - order(b)).map(line).join("");
}

async function runSweep(force, layers) {
  $("btn-sweep").disabled = true;
  $("btn-force").disabled = true;
  try {
    const res = await bridge.apiPost("sweep", { force, layers: layers || null });
    renderEach([["清理结果", () => renderSweepResult(res)]]);
    await refresh();
  } catch (e) {
    showProblem("清理失败", (e && e.message) || e);
  } finally {
    $("btn-sweep").disabled = false;
    $("btn-force").disabled = false;
  }
}

async function forceLayer(layer) {
  const label = (state.overview?.layers?.[layer]?.label) || layer;
  if (!await askConfirm(`强制清理「${label}」这一层？\n\n会无视阈值立即动手，其它层不受影响。`)) return;
  await runSweep(true, [layer]);
}

async function saveSettings() {
  clearDirty();
  const payload = { sweep_cron: $("sweep_cron").value.trim() };
  for (const key of TOGGLE_SETTINGS) {
    payload[key] = $(key).checked;
  }
  for (const key of TEXTAREA_SETTINGS) {
    payload[key] = $(key).value.split("\n").map((s) => s.trim()).filter(Boolean);
  }
  for (const key of NUMERIC_SETTINGS) {
    payload[key] = Number($(key).value);
  }
  $("btn-save").disabled = true;
  try {
    const res = await bridge.apiPost("settings", payload);
    fillSettings(res.settings || {});
    toast(res.ok ? "已保存并生效。" : `部分未保存：${res.error}`, res.ok ? "ok" : "warn");
  } catch (e) {
    toast("保存失败：" + e.message, "bad");
  } finally {
    $("btn-save").disabled = false;
    await refresh();
  }
}

async function discover() {
  $("btn-discover").disabled = true;
  $("discover").innerHTML = '<div class="empty">正在搜索…</div>';
  try {
    const res = await bridge.apiGet("discover");
    renderDiscover(res.candidates || [], res.configured || [], res);
  } catch (e) {
    $("discover").innerHTML = `<div class="bad">${escapeHtml(e.message)}</div>`;
  } finally {
    $("btn-discover").disabled = false;
  }
}

async function purge(pluginId) {
  if (!await askConfirm(`确定立即清理 ${pluginId} 的内存模块？`)) return;
  try {
    const res = await bridge.apiPost("purge", { plugin_id: pluginId });
    const r = res.record || {};
    toast(r.detail || r.skipped || "已处理", "ok");
  } catch (e) {
    toast("清理失败：" + e.message, "bad");
  } finally {
    await refresh();
  }
}

// ---------- 需要处理的事 ----------

// 本插件不推送，所有提醒都聚到首屏这一条里。所以它必须比任何卡片都醒目：
// 吸顶、带级别色、逐条列出，并标出哪几条是上次打开之后新出现的。
// 只填内容不重建结构：这条每 30 秒刷一次，重建会把按钮的 hover 状态一起换掉。
function renderAttention(att) {
  const host = $("notice");
  const items = (att && att.items) || [];
  if (!items.length) {
    host.hidden = true;
    $("notice-items").innerHTML = "";
    return;
  }
  const unread = Number(att.unread) || 0;
  host.hidden = false;
  host.className = "notice " + (att.level || "warn");
  $("notice-title").textContent = items.length + " 件事需要处理" +
    (unread ? ` · ${unread} 条是你上次关闭面板之后出现的` : "");
  $("notice-items").innerHTML = items.map((it) => `
    <div class="notice-item ${escapeHtml(it.level || "info")}">
      <span class="dot"></span>
      <span class="n-body"><b>${escapeHtml(it.title)}</b>${escapeHtml(it.detail || "")}
        ${it.action && it.action.type === "remove_instance"
          ? `<button class="btn mini" data-att-url="${escapeHtml(it.action.url || "")}">删掉这一行</button>`
          : ""}
      </span>
      ${it.at ? `<span class="n-time">${escapeHtml(shortTime(it.at))}</span>` : ""}
    </div>`).join("");
  for (const el of $("notice-items").querySelectorAll("[data-att-url]")) {
    el.onclick = () => dropInstance(el.dataset.attUrl, 0);
  }
}

// ---------- 实例状态表 ----------

// 之前面板只能告诉你「这次探测没连上」。看不出一个挂着的是「你自己停用的」
// 还是「重复配的一个」还是「真的挂了」——而这三种要采取的动作完全不同。
const INSTANCE_STATE = {
  running: ["ok", "运行中"],
  disabled: ["off", "已停用"],
  refused: ["bad", "服务没启动"],
  timeout: ["warn", "服务无响应"],
  dns: ["bad", "主机名解析失败"],
  auth: ["warn", "Token 无效"],
  dead: ["warn", "疑似失联"],
  unknown: ["off", "尚未探测"],
};

// 实例状态表。地址在下面「改完点保存」的表单里已经出现一次，
// 这里再列一遍就成了同一屏里两排一模一样的字——所以这张表只给状态，
// 并把每条状态**钉回它对应的那一行**。
function renderInstanceStatus(rows) {
  const host = $("inst-table");
  state.statusByUrl = {};
  if (!rows || !rows.length) {
    host.innerHTML = "";
    return;
  }
  for (const row of rows) {
    const [cls, label] = INSTANCE_STATE[row.state] || INSTANCE_STATE.unknown;
    state.statusByUrl[row.url] = {
      cls, label, note: row.note, duplicate: row.duplicate_with,
    };
  }
  const worthTelling = rows.filter(
    (r) => r.duplicate_with || (r.note && r.state !== "running")
  );
  host.innerHTML = worthTelling.length
    ? `<div class="inst-alert">${worthTelling
        .map((r) => {
          const [cls] = INSTANCE_STATE[r.state] || INSTANCE_STATE.unknown;
          const text = r.duplicate_with
            ? `与 ${r.duplicate_with} 指向同一个 QQ 号`
            : r.note;
          return `<div class="notice-item ${r.duplicate_with ? "bad" : cls === "ok" ? "info" : cls}">
            <span class="dot"></span><span class="n-body">${escapeHtml(r.url)} — ${escapeHtml(text)}</span>
          </div>`;
        })
        .join("")}</div>`
    : "";
}

// 记录文件在哪、有没有接上次的——「累计释放突然清零」这种事，
// 光看数字分不清是记录丢了还是真的没清过。写出来让人自己判断。
function renderDiagnostics(d) {
  if (!d) return "";
  const bits = [];
  bits.push(d.state_exists
    ? (d.restored ? "已接上次的记录" : "记录是空的（还没跑过体检）")
    : "记录文件还没建");
  if (d.history_records) bits.push(`历史 ${d.history_records} 条`);
  return `<div class="kv kv-dim"><span title="${escapeHtml(d.state_path || "")}">
    <b>记录存于：</b>${escapeHtml(d.state_path || "未知")} · ${escapeHtml(bits.join(" · "))}
  </span></div>`;
}

function renderSchedule(overview) {
  const s = overview.schedule || {};
  const t = overview.totals || {};
  const napcat = overview.napcat || {};
  const on = Boolean(s.auto_enabled);
  const badge = $("auto-badge");
  badge.textContent = on ? "自动清理运行中" : "已暂停";
  badge.className = "badge " + (on ? "ok" : "off");
  // 运行中加一颗呼吸的小灯，一眼能看出状态
  badge.innerHTML = on
    ? `<span class="pulse"></span>${escapeHtml(badge.textContent)}`
    : escapeHtml(badge.textContent);
  // 数字分三级：可回收总量是这个面板存在的理由，给它最大的字号；
  // 累计与上次是参考；频率与时间是背景，灰、小、不抢视线。
  const reclaim = humanBytes(t.reclaimable_bytes || 0);
  $("schedule").innerHTML = `
    <div class="hero">
      <div class="hero-label">当前可回收</div>
      <div class="hero-value">${escapeHtml(reclaim)}</div>
      <div class="hero-sub">${escapeHtml(t.reclaimable_modules || 0)} 个模块 · 来自 ${Object.keys(overview.layers || {}).length} 个维护层</div>
    </div>
    <div class="kv">
      <span><b>累计释放：</b>${escapeHtml(humanBytes(t.total_freed_bytes || 0))}
        <span class="sub" title="共体检 ${t.sweep_count || 0} 次">${t.sweep_count || 0} 次</span></span>
      <span><b>上次释放：</b>${escapeHtml(humanBytes(t.last_freed_bytes || 0))}</span>
    </div>
    <div class="kv kv-dim">
      <span><b>体检频率：</b>${escapeHtml(s.cron_effective || "未注册")}</span>
      <span><b>下次体检：</b>${escapeHtml(shortTime(s.next_run) || "未注册")}</span>
      <span title="${escapeHtml(fullTime(t.last_sweep_at))}">
        <b>上次体检：</b>${escapeHtml(shortTime(t.last_sweep_at) || "从未")}</span>
    </div>
    ${renderDiagnostics(overview.diagnostics)}`;
}

function renderLayers(overview) {
  const host = $("layers");
  const entries = Object.entries(overview.layers || {});
  if (!entries.length) {
    host.innerHTML = '<div class="empty">没有可维护的层。</div>';
    return;
  }
  // 分组是这一块最要紧的改动：8 层平铺时，每张卡的边框、padding、按钮完全一样，
  // 扫一眼分不出哪几层真的该动手——而那正是打开面板要回答的问题。
  // 组内还要按「超得最多的排最前」：日志 2.29 GiB 与磁盘缓存 22 MiB 同样超阈值，
  // 但只有前者值得先管。
  const byUrgency = (a, b) => {
    const ra = a[1].ratio, rb = b[1].ratio;
    if (ra === null || ra === undefined) return rb === null || rb === undefined ? 0 : 1;
    if (rb === null || rb === undefined) return -1;
    return rb - ra;
  };
  const due = entries.filter(([, l]) => l.enabled && l.due).sort(byUrgency);
  const normal = entries.filter(([, l]) => l.enabled && !l.due).sort(byUrgency);
  const off = entries.filter(([, l]) => !l.enabled);
  const card = ([key, l]) => {
    const cls = !l.enabled ? "off" : l.due ? "due" : "normal";
    const last = l.last || {};
    const lastText = last.last_run_at
      ? `上次 ${escapeHtml(shortTime(last.last_run_at))}${
          last.last_freed_bytes ? ` · 释放 ${humanBytes(last.last_freed_bytes)}` : ""
        }${last.last_acted ? "" : " · 未动手"}`
      : "尚未执行";
    // 超阈值时进度条永远是满的，不带任何信息量（amount 里已经写了「/ 阈值」），
    // 却白白占掉一整行。删掉它，卡片矮三分之一。
    const ratio = l.ratio === null || l.ratio === undefined ? null : Math.min(100, l.ratio);
    const bar = ratio === null || l.due
      ? ""
      : `<div class="bar"><i style="width:${ratio}%"></i></div>`;
    const logs = l.logs ? renderLogsBrief(l.logs) : "";
    // 超阈值徽标用**未截断**的比例。早先这里用的是 min(100, ratio)，
    // 结果「超 37%」有、「超 143 倍」反而没有——恰恰是差得最远的那个没有标记。
    const over = l.ratio !== null && l.ratio !== undefined && l.ratio > 100
      ? `<span class="over">${
          l.ratio >= 1000 ? `超 ${(l.ratio / 100).toFixed(0)} 倍` : `超 ${l.ratio - 100}%`
        }</span>`
      : "";
    const notes = [l.why, l.detail && l.detail !== l.why ? l.detail : ""]
      .filter(Boolean)
      .map(escapeHtml)
      .join(" · ");
    return `<div class="layer ${cls}">
      <div class="head2">
        <span class="name">${l.due ? '<span class="pulse-dot"></span>' : ""}${escapeHtml(l.label)}</span>
        <span class="amount">${escapeHtml(l.amount || "")}${over}
          <button class="btn mini danger-hover" data-force="${escapeHtml(key)}">强清</button>
        </span>
      </div>
      ${bar}
      ${logs ? `<div class="logs">${logs}</div>` : ""}
      <div class="last">${notes}
        <span class="when">${lastText}</span>
        ${l.config_error ? `<span class="err">目录配置有误：${escapeHtml(l.config_error)}</span>` : ""}
        ${last.last_error ? `<span class="err">上次错误：${escapeHtml(last.last_error)}</span>` : ""}
      </div>
    </div>`;
  };
  const group = (title, list, hint, fold) => {
    if (!list.length) return "";
    const body = `<div class="layers">${list.map(card).join("")}</div>`;
    if (!fold) {
      return `<div class="lgroup">
        <div class="lgroup-head"><span class="lgroup-title">${escapeHtml(title)}</span>
        <span class="lgroup-hint">${escapeHtml(hint)}</span></div>${body}</div>`;
    }
    return `<details class="lgroup-fold">
      <summary>${escapeHtml(title)} · ${list.length}</summary>${body}</details>`;
  };
  host.innerHTML = [
    group("要动手", due, "已超条件，等定时体检或你手动清", false),
    group("正常", normal, "在阈值内，体检会跳过", false),
    group("关着的", off, "这一层没启用", true),
  ].join("");
  for (const el of host.querySelectorAll("[data-force]")) {
    el.onclick = () => forceLayer(el.dataset.force);
  }
}

// 日志层用插件自己的口径统计（上游那个数只是附注），所以这里比别的层多几行：
// 到底有多少个文件、多大、最旧的多大、轮转了几份。终端里看不到日志时，
// 这几个数字能直接回答「日志到底存不存在、存到哪去了」。
function renderLogsBrief(logs) {
  if (logs.missing) {
    return `<div class="last">没找到日志目录：${escapeHtml(logs.root || "")}</div>`;
  }
  const bits = [`${logs.file_count} 个文件`];
  if (logs.largest) bits.push(`最大 ${escapeHtml(logs.largest)}（${humanBytes(logs.largest_bytes)}）`);
  if (logs.rotated) bits.push(`轮转 ${logs.rotated} 份`);
  if (logs.oldest_days !== null && logs.oldest_days !== undefined) {
    bits.push(`最旧 ${logs.oldest_days} 天`);
  }
  if (logs.growth) {
    const sign = logs.growth > 0 ? "+" : "";
    bits.push(`较上次 ${sign}${humanBytes(logs.growth)}`);
  }
  return `<div class="last">${bits.join(" · ")}</div>`;
}

// 插件模块的判定是三态而不是两态。以前「拿不到元数据」被当成「已停用」，
// 于是一个正在跑的插件可能被自动抽掉模块；现在拿不准就归为 unknown，
// 标出来让人看，但绝不自动动。
const PLUGIN_STATE = {
  running: ["ok", "运行中"],
  stopped: ["off", "已停用"],
  unknown: ["warn", "状态未知"],
};

function renderPlugins(rows) {
  const host = $("plugins");
  if (!rows.length) {
    host.innerHTML = '<div class="empty">没有已加载的插件模块。</div>';
    return;
  }
  host.innerHTML = rows
    .map((r) => {
      const status = r.status || (r.activated ? "running" : "stopped");
      const [cls, label] = PLUGIN_STATE[status] || PLUGIN_STATE.unknown;
      const canPurge = !r.self && status !== "running";
      return `<div class="plugin">
        <span class="badge ${cls}">${escapeHtml(label)}</span>
        <span class="id" title="${escapeHtml(r.plugin_id)}">${escapeHtml(r.display_name || r.plugin_id)}</span>
        <span class="pid" title="${escapeHtml(r.plugin_id)}">${escapeHtml(r.plugin_id)}</span>
        <span class="n">${r.module_count} 个模块</span>
        ${canPurge ? `<button class="btn mini" data-purge="${escapeHtml(r.plugin_id)}">立即清理</button>` : ""}
      </div>`;
    })
    .join("");
  for (const el of host.querySelectorAll("[data-purge]")) {
    el.onclick = () => purge(el.dataset.purge);
  }
}

function renderHistory(records) {
  const host = $("history");
  $("history-count").textContent = records.length ? `最近 ${records.length} 条` : "";
  if (!records.length) {
    host.innerHTML = '<div class="empty">还没有清理记录。自动模式会在条件达成时自动记录。</div>';
    return;
  }
  const row = (r) => {
    const cls = r.error ? "err" : r.acted ? "acted" : "skip";
    const tag = r.error ? "失败" : r.acted ? "已清理" : "跳过";
    const detail = r.error || r.detail || r.skipped || "";
    const freed = r.freed_bytes ? ` · 释放 ${humanBytes(r.freed_bytes)}` : "";
    return `<div class="result ${cls}">
      <div class="head2">
        <span class="name">${escapeHtml(r.label || r.layer)}</span>
        <span class="meta">${escapeHtml(shortTime(r.at))}${freed}</span>
      </div>
      <div class="detail ${r.error ? "bad" : ""}">[${tag}] ${escapeHtml(detail)}</div>
    </div>`;
  };
  // 最近的常显，更旧的折起来。一屏摆四十条会让上面那些真正要看的东西被埋掉。
  const recent = records.slice(0, 6);
  const older = records.slice(6);
  const fold = older.length
    ? `<details class="lgroup-fold"><summary>更早的 ${older.length} 条</summary>${
        older.map(row).join("")
      }</details>`
    : "";
  host.innerHTML = fold + recent.map(row).join("");
}

function renderDiscover(candidates, configured, meta) {
  const host = $("discover");
  const chosen = new Set(configured);
  const roots = (meta && meta.searched_roots) || [];
  const head = `<div class="empty">已搜索：${escapeHtml(roots.join("、") || "—")}` +
    `${meta && meta.truncated ? "（已达扫描上限，结果可能不全）" : ""}</div>`;
  const rows = candidates
    .map((c) => {
      const on = chosen.has(c.path);
      const usable = c.exists && !c.error;
      // safe=false 的候选不提供「添加」：自动接管已跳过它们，
      // 手动添加同样不建议（可能含程序本体与配置）
      const allowed = usable && c.safe !== false;
      const mark = on ? "已配置" : allowed ? "可添加" : (c.reason || "不建议使用");
      const cls = on || allowed ? "" : "off";
      const button = on
        ? '<span class="badge ok">已配置</span>'
        : allowed
        ? `<button class="btn mini" data-add="${escapeHtml(c.path)}">添加</button>`
        : `<span class="muted">${escapeHtml(c.reason || "已自动排除")}</span>`;
      const size = usable
        ? `${humanBytes(c.size_bytes)} / ${c.file_count} 个文件`
        : escapeHtml(c.error || "不存在");
      return `<div class="cand ${cls}">
        <span class="path">${escapeHtml(c.path)}</span>
        <span class="muted">${size}</span>
        ${button}
        <span class="muted">${mark}</span>
      </div>`;
    })
    .join("");
  host.innerHTML = rows.length
    ? head + rows.join("")
    : head + '<div class="empty">没找到缓存目录。</div>';
  for (const el of host.querySelectorAll("[data-add]")) {
    el.onclick = () => {
      const box = $("napcat_cache_dirs");
      const lines = box.value.split("\n").map((s) => s.trim()).filter(Boolean);
      if (!lines.includes(el.dataset.add)) lines.push(el.dataset.add);
      box.value = lines.join("\n");
      el.outerHTML = '<span class="badge ok">已加入待保存</span>';
    };
  }
}

async function dryRun() {
  $("btn-dryrun").disabled = true;
  try {
    const res = await bridge.apiPost("dryrun", {});
    renderDryRun(res);
  } catch (e) {
    $("dryrun-card").style.display = "";
    $("dryrun-body").innerHTML = `<div class="bad">${escapeHtml(e.message)}</div>`;
  } finally {
    $("btn-dryrun").disabled = false;
  }
}

function renderDryRun(r) {
  $("dryrun-card").style.display = "";
  const dirs = r.directories || [];
  const rows = dirs.map((d) => `<div class="cand">
    <span class="path">${escapeHtml(d.path)}</span>
    <span class="muted">${humanBytes(d.size_bytes)}</span>
    <span class="${d.would_delete_files ? "warn" : "ok"}">${
      d.would_delete_files
        ? `将删 ${d.would_delete_files} 个 / ${humanBytes(d.would_delete_bytes)}`
        : "不会删除"
    }</span>
  </div>`).join("");
  const astrbotRows = [
    ["AstrBot 磁盘缓存", r.astrbot_size_bytes, r.astrbot_threshold_bytes],
    ["AstrBot 日志", r.astrbot_logs_size_bytes, r.astrbot_logs_threshold_bytes],
  ].map(([label, size, threshold]) => {
    const over = Number(size) > Number(threshold);
    return `<div class="cand"><span class="path">${escapeHtml(label)}</span>
       <span class="muted">${humanBytes(size)} / 阈值 ${humanBytes(threshold)}</span>
       <span class="${over ? "warn" : "ok"}">${over ? "会清理" : "不会删除"}</span></div>`;
  }).join("");
  const samples = dirs.flatMap((d) => d.samples || []).slice(0, 6);
  $("dryrun-body").innerHTML = `
    <div class="kv">
      <span><b>目录来源：</b>${escapeHtml(r.dir_source || "—")}</span>
      <span><b>将删除：</b>${r.would_delete_files} 个文件 / ${humanBytes(r.would_delete_bytes)}</span>
      <span><b>待清模块：</b>${r.modules_pending} 个</span>
    </div>
    ${dirs.length ? `<div class="discover">${dirs.length ? rows : ""}</div>` : '<div class="empty">当前没有生效的缓存目录。</div>'}
    ${astrbotRows}
    ${samples.length ? `<div class="muted note">将被删除的文件（前 6 个）：<br>${samples.map(escapeHtml).join("<br>")}</div>` : ""}
    <p class="muted note">以上只是计算结果，<b>点它不会删除任何东西</b>。确认无误后再点「立即体检」或等下一次自动体检。</p>`;
}

function renderInstances() {
  const host = $("inst-list");
  if (!state.instances.length) {
    host.innerHTML = '<div class="empty">还没有实例，点右上角「添加实例」。</div>';
    return;  }
  host.innerHTML = state.instances.map((it, i) => {
    const status = (state.statusByUrl || {})[it.url];
    return `<div class="inst">
      <span class="inst-state" title="${escapeHtml(status ? status.note : "")}">${
        status ? `<span class="badge ${status.cls}">${escapeHtml(status.label)}</span>` : ""
      }</span>
      <label class="check"><input type="checkbox" data-f="enable" data-i="${i}" ${it.enable ? "checked" : ""} /><span>启用</span></label>
      <input type="text" data-f="url" data-i="${i}" value="${escapeHtml(it.url)}" placeholder="http://127.0.0.1:3000 或 ws://127.0.0.1:3001" />
      <input type="text" data-f="token" data-i="${i}" value="${escapeHtml(it.token || "")}" placeholder="${it.token_set ? "已配置，留空保持不变" : "留空＝自动解析"}" ${it.token_set ? "data-has-token=\"1\"" : ""} />
      <input type="number" data-f="protocol_interval_hours" data-i="${i}" min="1" value="${it.protocol_interval_hours}" title="协议缓存清理间隔（小时）" />
      <button class="btn mini" data-del="${i}">删除</button>
    </div>`;
  }).join("");
  for (const el of host.querySelectorAll("[data-i]")) {
    el.oninput = el.onchange = () => {
      const idx = Number(el.dataset.i);
      const field = el.dataset.f;
      state.instances[idx][field] = field === "enable" ? el.checked
        : field === "protocol_interval_hours" ? Number(el.value) || 12 : el.value;
      markDirty();
    };
  }
  for (const el of host.querySelectorAll("[data-del]")) {
    el.onclick = () => {
      state.instances.splice(Number(el.dataset.del), 1);
      markDirty();
      renderInstances();
    };
  }
  // 传输层不通时禁用 Token 输入框，并说清楚为什么
  if (state.transportDown) {
    for (const el of host.querySelectorAll('[data-f="token"]')) {
      el.disabled = true;
      el.title = "服务当前连不上，改 Token 试不出来；先按上面的提示把服务起起来";
    }
  }
}

function renderInstStatus(rows) {
  if (!rows || !rows.length) return;
  $("token-banner").className = "banner warn";
  $("token-banner").innerHTML =
    "<b>各实例连接状态</b><br>" + rows.map((r) => {
      const mark = r.connected ? "正常" : `异常（${escapeHtml(r.error || "未知")}）`;
      return `${escapeHtml(r.url)}：${mark}${r.last_run_at ? ` · 上次清理 ${escapeHtml(r.last_run_at)}` : ""}`;
    }).join("<br>");
}

async function saveInstances() {
  const rows = state.instances.filter((r) => r.url && r.url.trim());
  if (!rows.length) {
    toast("至少需要一个实例，并填写地址。", "warn");
    return;
  }
  $("btn-save-inst").disabled = true;
  try {
    const res = await bridge.apiPost("token", { action: "save_instances", instances: rows });
    clearDirty();
    toast(`已保存 ${res.count} 个实例。${res.note || ""}`, "ok");
    await probeToken();
    await refresh();
  } catch (e) {
    toast("保存失败：" + e.message, "bad");
  } finally {
    $("btn-save-inst").disabled = false;
  }
}

async function probeToken() {
  try {
    const res = await bridge.apiGet("token");
    renderTokenState(res);
    renderTokenConfigs(res.configs || []);
    renderSetup(res);
  } catch (e) {
    $("token-banner").className = "banner bad";
    $("token-banner").innerHTML = escapeHtml(e.message);
  }
}

async function reResolve() {
  try {
    const res = await bridge.apiPost("token", { action: "resolve" });
    toast(res.note || "已重新解析。", "ok");
    await probeToken();
    await refresh();
  } catch (e) {
    toast("解析失败：" + e.message, "bad");
  }
}

const AUTH_STYLE = {
  ok: ["ok", "连接正常"],
  ok_no_token: ["ok", "连接正常（无需 Token）"],
  auth_required: ["bad", "需要 Token"],
  auth_failed: ["bad", "Token 无效"],
  unreachable: ["bad", "服务没启动"],
  timeout: ["warn", "服务无响应"],
  dns: ["bad", "主机名解析失败"],
  error: ["bad", "连接异常"],
  bad_url: ["bad", "地址不合法"],
  no_url: ["off", "未配置地址"],
  unknown: ["off", "尚未解析"],
};

// 传输层问题：此时改 Token 毫无意义，输入框应当禁用
const TRANSPORT_DOWN = new Set(["unreachable", "timeout", "dns", "error"]);

function renderLastError(text) {
  const banner = $("last-error");
  if (text) {
    banner.className = "banner bad";
    banner.innerHTML = `<b>最近一次真实错误：</b><br>${escapeHtml(text)}`;
  } else {
    banner.className = "banner hidden";
  }
}

function renderTokenState(res) {
  const auth = res.auth || {};
  const [cls, label] = AUTH_STYLE[auth.mode] || ["off", auth.mode || "未知"];
  const rows = [
    ["整体状态", `<i class="${cls}">${label}</i>`],
    ["Token 来源", escapeHtml(auth.source || "自动解析中")],
    ["说明", escapeHtml(auth.detail || "")],
  ];
  $("inst-state").innerHTML = rows
    .map(([k, v]) => `<span><b>${k}：</b>${v}</span>`)
    .join("")
    + (res.hint ? `<span class="warn">${escapeHtml(res.hint)}</span>` : "");
  // 传输层不通的时候，改 Token 毫无意义——连接都没建立，填什么都试不出来。
  // 这就是 TRANSPORT_DOWN 存在的理由，之前定义了却没接上。
  state.transportDown = TRANSPORT_DOWN.has(auth.mode);
  renderInstances();
}

function renderTokenConfigs(configs) {
  const host = $("token-list");
  const rows = [];
  for (const c of configs) {
    if (c.error) {
      rows.push(`<div class="cand off"><span class="path">${escapeHtml(c.file)}</span>
        <span class="muted">${escapeHtml(c.error)}</span></div>`);
      continue;
    }
    for (const s of c.servers) {
      const kindLabel = {
        http_server: "HTTP 服务端",
        ws_server: "WebSocket 服务端",
        http_client: "HTTP 客户端（主动外连）",
        ws_client: "WebSocket 客户端（反向连接）",
      }[s.kind] || s.kind;
      const state = !s.enable ? "已停用" : s.has_token ? "有 Token" : "无 Token";
      const reachable = s.can_connect && s.enable
        ? `<button class="btn mini" data-apply="${escapeHtml(c.file)}|${escapeHtml(s.address)}">填入地址</button>`
        : '<span class="muted">不可连接</span>';
      rows.push(`<div class="cand ${s.can_connect && s.enable ? "" : "off"}">
        <span class="path">${escapeHtml(s.address || s.url || "（无监听端点）")}
          <span class="muted">${escapeHtml(kindLabel)}${c.account ? ` · QQ ${escapeHtml(c.account)}` : ""}</span></span>
        <span class="muted">${state}</span>${reachable}
      </div>`);
    }
  }
  host.innerHTML = rows.length
    ? rows.join("")
    : '<div class="empty">本机没找到 NapCat 的 onebot11*.json——它多半在另一个容器里。' +
      "这时插件仍会尝试自动解析 Token，必要时再手动填。</div>";
  for (const el of host.querySelectorAll("[data-apply]")) {
    el.onclick = () => {
      const [file, address] = el.dataset.apply.split("|");
      applyFound(file, address);
    };
  }
}

// 「填入地址」：把探测到的监听端点搬进对应的一行。
// Token 一律不碰——面板本来就拿不到它，只能继续自动解析。
function applyFound(file, address) {
  const target = address || "";
  if (!target) {
    toast("这个端点没有地址可用", "warn");
    return;
  }
  const row = state.instances.find((it) => it.url === target);
  if (row) {
    toast("这一行已经是这个地址了", "warn");
    return;
  }
  const blank = state.instances.find((it) => !it.url || !it.url.trim());
  if (blank) {
    blank.url = target;
    markDirty();
    renderInstances();
    toast(`已填入 ${target}，点「保存实例」生效。`, "ok");
    return;
  }
  state.instances.push({ url: target, token: "", enable: true, protocol_interval_hours: 12 });
  markDirty();
  renderInstances();
  toast(`已新增一行 ${target}，点「保存实例」生效。`, "ok");
}

function fillSettings(s) {
  if (formDirty()) return;   // 用户正在改，别拿服务端的旧值覆盖回去
  $("sweep_cron").value = s.sweep_cron ?? "*/15 * * * *";
  for (const key of TOGGLE_SETTINGS) {
    if ($(key)) $(key).checked = Boolean(s[key]);
  }
  for (const key of NUMERIC_SETTINGS) {
    if (s[key] !== undefined && $(key)) $(key).value = s[key];
  }
  for (const key of TEXTAREA_SETTINGS) {
    if ($(key)) $(key).value = (s[key] || []).join("\n");
  }
}

// ---------- 死实例检测 ----------

async function checkDead() {
  const btn = $("btn-dead");
  btn.disabled = true;
  $("dead").innerHTML = '<div class="empty">正在逐个实例探测…</div>';
  try {
    const res = await bridge.apiGet("dead-instances");
    renderDead(res);
  } catch (e) {
    $("dead").innerHTML = `<div class="empty">检测失败：${escapeHtml(e.message)}</div>`;
  } finally {
    btn.disabled = false;
  }
}

function renderDead(res) {
  const rows = res.rows || [];
  const note = $("dead-note");
  note.textContent = res.note || "";
  note.classList.remove("hidden");
  if (!rows.length) {
    $("dead").innerHTML = `<div class="empty">${
      res.checked
        ? `${res.checked} 个实例，没发现重复或长期失联的。`
        : "还没有配置任何 NapCat 实例。"
    }</div>`;
    return;
  }
  $("dead").innerHTML = rows.map((r) => {
    const badge = r.removable
      ? '<span class="badge ok">可确认删除</span>'
      : '<span class="badge off">仅提示</span>';
    const btn = r.removable
      ? `<button class="btn mini" data-url="${escapeHtml(r.url)}" data-occ="${Number(r.occurrence) || 0}">删除这一行</button>`
      : "";
    // 依据来源要摆在明面上：静态证据（磁盘上的 QQ 号 / 同一份配置文件）
    // 在 NapCat 连不上时也能成立，这正是原来看不出重复的原因。
    const basis = r.basis === "static"
      ? '<span class="badge warn">磁盘证据</span>'
      : '<span class="badge off">实测</span>';
    // 地址与说明分两行：挤在同一行时，长地址会被逐字符拆成
    // 「htt p:// 12 7.0. 0.1: 300 2」。
    return `<div class="find">
      <div class="find-top">
        <span class="find-url">${escapeHtml(r.url)}</span>
        ${basis}${badge}${btn}
      </div>
      <div class="find-why">${escapeHtml(r.reason)}</div>
    </div>`;
  }).join("");
  for (const el of $("dead").querySelectorAll("[data-url]")) {
    el.onclick = () => dropInstance(el.dataset.url, Number(el.dataset.occ) || 0);
  }
}

async function dropInstance(url, occurrence) {
  const which = occurrence ? `（第 ${occurrence + 1} 个同地址的条目）` : "";
  if (!await askConfirm(`确定从配置里删掉 ${url} 这一行${which}？\n\n只改配置，不会动 NapCat 磁盘上的任何数据，删错了可以加回来。`)) return;
  try {
    await bridge.apiPost("instance", { url, occurrence });
    await refresh();
    await checkDead();
  } catch (e) {
    toast("删除失败：" + e.message, "bad");
  }
}

// ---------- 平台配置体检（只读） ----------

// Orbit 自己只连 NapCat 用的那几行；AstrBot 侧配了几条反向 WS、有没有两条
// 撞了同一个端口，**以前完全没读过** cmd_config.json，所以一律看不见。
async function checkPlatform() {
  const btn = $("btn-platform");
  btn.disabled = true;
  $("platform").innerHTML = '<div class="empty">正在读平台配置…</div>';
  try {
    renderPlatform(await bridge.apiGet("platform"));
  } catch (e) {
    $("platform").innerHTML = `<div class="empty">读不到：${escapeHtml(e.message)}</div>`;
  } finally {
    btn.disabled = false;
  }
}

function renderPlatform(res) {
  const note = $("platform-note");
  note.textContent = res.note || "";
  note.classList.remove("hidden");
  const files = (res.files || []).filter((f) => (f.entries || []).length);
  if (!files.length) {
    $("platform").innerHTML =
      '<div class="empty">没找到平台配置（data/cmd_config.json 读不到或没有 platform 列表）</div>';
    return;
  }
  const blocks = files.map((f) => {
    const head = `<div class="find"><div class="find-top">
      <span class="find-url">${escapeHtml(f.name)}</span>
      <span class="muted">${escapeHtml(f.path)}</span></div></div>`;
    const rows = f.entries.map((e) => `<div class="find">
      <div class="find-top">
        <span class="find-url">${escapeHtml(e.name)}</span>
        ${e.port ? `<span class="badge off">端口 ${e.port}</span>` : ""}
        <span class="badge ${e.enable ? "ok" : "off"}">${e.enable ? "启用" : "已停用"}</span>
        ${e.orphan_port ? '<span class="badge warn">没找到对应的 NapCat</span>' : ""}
        ${e.token_marker ? `<span class="muted">凭据 ${escapeHtml(e.token_marker)}</span>` : ""}
      </div>
    </div>`).join("");
    return head + rows;
  }).join("");
  const conflicts = (res.conflicts || []).map((c) => `<div class="find">
    <div class="find-top">
      <span class="find-url">${c.kind === "duplicate" ? "重复配置" : "端口抢占"}</span>
      <span class="badge bad">端口 ${c.port}</span>
    </div>
    <div class="find-why bad">${escapeHtml(c.detail)}</div>
    <div class="find-why">${escapeHtml((c.names || []).join("、"))}</div>
  </div>`).join("");
  $("platform").innerHTML =
    (conflicts ? `<div class="lgroup"><div class="lgroup-head">
       <span class="lgroup-title">有 ${res.conflicts.length} 处冲突</span>
       <span class="lgroup-hint">同一个端口只能绑一次</span></div>${conflicts}</div>` : "") +
    `<div class="lgroup"><div class="lgroup-head">
      <span class="lgroup-title">全部配置（${res.total} 条）</span>
      <span class="lgroup-hint">凭据只编号比对，原文与派生值都不外发</span></div>${blocks}</div>` +
    '<p class="muted note">这一块<strong>只读</strong>。改平台配置要动 AstrBot 的核心配置，' +
    '删错一条可能让某个机器人直接掉线，所以这里只帮你把它找出来；' +
    '确认哪条是多余的之后，回 AstrBot 的「机器人」页面自己删。</p>';
}

// ---------- 空间地图（只读） ----------

async function showSpaces() {
  const btn = $("btn-spaces");
  btn.disabled = true;
  $("spaces").innerHTML = '<div class="empty">正在统计目录体积…</div>';
  try {
    const res = await bridge.apiGet("spaces");
    renderSpaces(res);
  } catch (e) {
    $("spaces").innerHTML = `<div class="empty">统计失败：${escapeHtml(e.message)}</div>`;
  } finally {
    btn.disabled = false;
  }
}

function renderSpaces(res) {
  const groups = res.groups || [];
  if (!groups.length) {
    $("spaces").innerHTML = '<div class="empty">没找到可统计的目录。</div>';
    return;
  }
  const html = groups.map((g) => {
    const head = `<div class="find"><div class="find-top">
        <span class="find-url">${escapeHtml(g.title)}</span>
        <span class="muted">${escapeHtml(g.hint || "")}</span></div></div>`;
    const rows = (g.rows || []).map((r) => `<div class="find">
      <div class="find-top">
        <span class="find-url">${escapeHtml(r.label)}</span>
        <span class="n">${humanBytes(r.size_bytes)} / ${Number(r.file_count) || 0} 个文件</span>
        ${r.truncated ? '<span class="badge off">未测完</span>' : ""}
        ${r.kind ? `<span class="badge off">${escapeHtml(r.kind)}</span>` : ""}
        ${r.orphan ? '<span class="badge warn">没人用了</span>' : ""}
      </div>
      <div class="find-why">${escapeHtml(r.path)}</div>
    </div>`).join("");
    return head + rows;
  }).join("");
  const warn = res.truncated
    ? '<p class="muted note">部分目录未测完（已达扫描上限），实际占用可能更大。</p>'
    : "";
  $("spaces").innerHTML =
    `<div class="kv"><span>合计：<b>${humanBytes(res.total_bytes || 0)}</b></span>` +
    `<span>耗时 ${Number(res.elapsed_ms) || 0} ms</span></div>` + html + warn +
    '<p class="muted note">纯只读统计。标了「没人用的」是插件卸载后留下的数据目录，' +
    '插件只会提示不自动删——里面可能有你特意留着的东西。</p>';
}

// ---------- 插件目录里的客观垃圾 ----------

const JUNK_LABEL = { broken: "没装完的插件目录", zip: "遗留安装包", pycache: "__pycache__" };

async function showJunk() {
  const btn = $("btn-junk");
  btn.disabled = true;
  $("junk").innerHTML = '<div class="empty">正在扫描…</div>';
  try {
    const res = await bridge.apiGet("junk");
    renderJunk(res);
  } catch (e) {
    $("junk").innerHTML = `<div class="empty">扫描失败：${escapeHtml(e.message)}</div>`;
  } finally {
    btn.disabled = false;
  }
}

function renderJunk(res) {
  const note = $("junk-note");
  note.textContent = res.note || "";
  note.classList.remove("hidden");
  state.junk = res;
  const rows = [];
  for (const kind of ["broken", "zip", "pycache"]) {
    for (const item of res[kind] || []) {
      rows.push({ ...item, kind });
    }
  }
  if (!rows.length) {
    $("junk").innerHTML = '<div class="empty">插件目录很干净，没找到垃圾。</div>';
    return;
  }
  rows.sort((a, b) => b.size_bytes - a.size_bytes);
  $("junk").innerHTML =
    `<div class="kv"><span>合计可删：<b>${humanBytes(res.total_bytes || 0)}</b></span>` +
    `<span>${rows.length} 项</span></div>` +
    rows.map((r) => `<div class="find">
      <div class="find-top">
        <span class="find-url">${escapeHtml(r.path)}</span>
        <span class="n">${humanBytes(r.size_bytes)}</span>
        <span class="badge off">${escapeHtml(JUNK_LABEL[r.kind] || r.kind)}</span>
        <button class="btn mini" data-junk-kind="${escapeHtml(r.kind)}" data-junk-path="${escapeHtml(r.path)}">删除</button>
      </div>
    </div>`).join("");
  for (const el of $("junk").querySelectorAll("[data-junk-path]")) {
    el.onclick = () => dropJunk(el.dataset.junkKind, el.dataset.junkPath);
  }
}

async function dropJunk(kind, path) {
  if (!await askConfirm(`确定删除这一项？\n\n${path}\n\n只删这一项，不影响别的插件。`)) return;
  try {
    const res = await bridge.apiPost("junk", { kind, path });
    toast(`已删除，释放 ${humanBytes(res.freed_bytes || 0)}。`, "ok");
    await showJunk();
  } catch (e) {
    toast("删除失败：" + e.message, "bad");
  }
}

// 一键清除。与逐项删除的判据完全相同（就是 _classify_junk 那三类），
// 区别只在于少点几十次。所以它坚持两道：先列清单、确认后才动手。
// 「装了但没加载」这类需要推断的，永远不在这条路径上。
async function purgeAllJunk() {
  const btn = $("btn-junk-all");
  btn.disabled = true;
  try {
    if (!state.junk) {
      $("junk").innerHTML = '<div class="empty">正在扫描…</div>';
      state.junk = await bridge.apiGet("junk");
    }
    const res = state.junk;
    const plan = res.all || {};
    if (!plan.count) {
      toast("没有可一键清除的垃圾。", "ok");
      return;
    }
    const msg = `将清除以下客观垃圾，合计 ${humanBytes(plan.bytes || 0)}：\n\n` +
      `· 没装完的插件目录 ${plan.broken || 0} 个\n` +
      `· 遗留安装包 ${plan.zip || 0} 个\n` +
      `· __pycache__ ${plan.pycache || 0} 个\n\n` +
      "只删这些满足条件的目录与文件；装了但没加载的插件一律不动。\n确定继续？";
    if (!await askConfirm(msg)) return;
    const done = await bridge.apiPost("junk", { action: "purge_all" });
    state.junk = null;
    const bits = [`已清除 ${done.deleted || 0} 项`];
    if (done.freed_bytes) bits.push(`释放 ${humanBytes(done.freed_bytes)}`);
    if (done.protected) bits.push(`${done.protected} 项在 7 天年龄锁内`);
    if (done.failed) bits.push(`${done.failed} 项没删掉`);
    // 逐项明细：只说「删了 N 项」，用户没法判断是自己预期的东西。
    const detail = (done.detail || []).join("；");
    toast(
      bits.join(" · ") + (detail ? "\\n\\n" + detail : ""),
      done.failed ? "warn" : (done.deleted ? "ok" : "warn")
    );
    await showJunk();
  } catch (e) {
    toast("清除失败：" + e.message, "bad");
  } finally {
    btn.disabled = false;
  }
}

// ---------- 工具 ----------

// 时间戳直接扔 ISO 串很难扫：「2026-09-30T16:15:02+08:00」里的年份和时区
// 每天看都是一样的，淹没了真正有用的小时与分钟。完整值放 title 里。
function shortTime(value) {
  const text = String(value || "").trim();
  const matched = text.match(/^\d{4}-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
  if (!matched) return text;
  return `${matched[1]}-${matched[2]} ${matched[3]}:${matched[4]}`;
}

function fullTime(value) {
  return String(value || "").replace("T", " ").replace(/[+-]\d{2}:\d{2}$/, "");
}

function humanBytes(value) {
  let size = Number(value) || 0;
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  for (const unit of units) {
    if (Math.abs(size) < 1024 || unit === units[units.length - 1]) {
      return unit === "B" ? `${size.toFixed(0)} B` : `${size.toFixed(2)} ${unit}`;
    }
    size /= 1024;
  }
  return `${size.toFixed(2)} TiB`;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

// 整个文件跑完再启动：上方所有 const 到这里都已初始化。
init();
