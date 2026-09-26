const bridge = window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);

const state = {
  overview: null,
  instances: [],
  timer: null,
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
];

// ---------- 表单脏标记 ----------

// 面板每 30 秒轮询一次，而刷新会重新回填表单。用户正改到一半时，
// 不能拿服务端的旧值把输入内容吹掉。
let dirty = false;

function markDirty() { dirty = true; }
function clearDirty() { dirty = false; }
function formDirty() { return dirty; }

function watchDirty() {
  for (const id of [
    "sweep_cron", "auto_enabled", "auto_clean_plugin_modules", "auto_adopt_cache_dirs",
    "napcat_cache_dirs", "media_dirs", ...NUMERIC_SETTINGS,
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
  // 取色器要显示当前生效的值，不然它一直停在 HTML 里写死的蓝色
  if (picker) picker.value = accent || "";
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

(async function init() {
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
  // 外观必须先定下来，否则黑幕会先闪一下浅色
  playIntro();
  await refresh();
  // 页面在后台时不轮询：既省事也避免看不见时静默改数据
  state.timer = setInterval(() => {
    if (document.visibilityState === "visible") refresh();
  }, 30000);
})();

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
  $("btn-spaces").onclick = showSpaces;
  $("btn-junk").onclick = showJunk;
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

// 开场总长约 4.8 秒：入场 1.4s / 副标题 1.1s / 到位后停 1.2s / 退幕 0.9s。
// 节奏刻意比一般开场慢——缓冲给得足，每个阶段都看得清，
// 退幕也不会卡在副标题刚到位的那一瞬间。
const INTRO_STEP = 300;        // 标题开始入场
const INTRO_MAIN = 1400;       // 标题入场时长
const INTRO_SUB_DELAY = 1600;  // 副标题入场起点（与标题重叠）
const INTRO_SUB = 600;         // 副标题只做渐变，不足一秒
const INTRO_HOLD = 1200;       // 全部到位后先停一下再退幕
const INTRO_EXIT = 900;
const INTRO_FALLBACK = 5200;   // 兜底：无论发生什么，到点必退幕
const INTRO_STARS = 26;        // 周围的小白点数量

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
      function frame(now) {
        let t = Math.min((now - start) / duration, 1);
        t = ease(t);
        el.style.opacity = from.o + (to.o - from.o) * t;
        // fadeOnly：只改不透明度。副标题要的就是「直接渐变、完全不位移」，
        // 连模糊都不留。
        if (!fadeOnly) {
          el.style.filter = `blur(${from.b + (to.b - from.b) * t}px)`;
          el.style.transform =
            `translate(-50%, ${from.y + (to.y - from.y) * t}px) ` +
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
  stage.style.transition = `opacity ${INTRO_EXIT}ms ease, transform ${INTRO_EXIT}ms ease`;
  stage.style.opacity = "0";
  stage.style.transform = "scale(1.06)";
  setTimeout(() => {
    document.documentElement.classList.remove("intro-on");
    stage.style.transition = "";
    stage.style.transform = "";
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

function playIntro() {
  const stage = $("intro");
  const title = $("intro-title");
  const sub = $("intro-sub");
  const stars = $("intro-stars");
  if (!stage || !title || !sub) return;

  // 开了「减少动效」就直接终态，不放动画
  if (prefersReducedMotion()) return;

  if (stars) makeStars(stars, INTRO_STARS);
  document.documentElement.classList.add("intro-on");
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
      const titleDone = interpolate(
        title, { o: 0, b: 16, s: 1.12, y: 0 }, { o: 1, b: 0, s: 1, y: 0 },
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

async function refresh() {
  try {
    const [overview, history] = await Promise.all([
      bridge.apiGet("overview"),
      bridge.apiGet("history", { limit: 40 }),
    ]);
    state.overview = overview;
    renderSchedule(overview);
    renderLayers(overview);
    renderPlugins(overview.module_rows || []);
    renderHistory(history.history || []);
    fillSettings(overview.settings || {});
    // 外观也从服务端配置回读：刷新页面后依旧是你上次选的那套。
    // 这条路不通的话，存进去了也读不回来。
    const look = lookFrom(overview.settings);
    applyTheme(look.theme, look.accent);
    // 表单脏了就别拿服务端数据冲刷——用户正改到一半的地址/阈值会被吹掉
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
    renderInstances();
    renderInstStatus(overview.napcat?.instances || []);
    renderLastError(overview.last_error || "");
  } catch (e) {
    $("auto-badge").textContent = "读取失败";
    $("auto-badge").className = "badge bad";
    $("schedule").innerHTML = `<span class="bad">${escapeHtml(e.message)}</span>`;
  }
}

async function runSweep(force, layers) {
  $("btn-sweep").disabled = true;
  $("btn-force").disabled = true;
  try {
    const res = await bridge.apiPost("sweep", { force, layers: layers || null });
    const freed = res.freed_bytes || 0;
    const acted = (res.acted || []).length;
    const errs = (res.records || []).filter((r) => r.error).length;
    if (errs) {
      toast(`完成，但有 ${errs} 层失败，详见下方历史。`, "bad");
    } else if (!freed) {
      // 0 字节是常态：干净的时候强制清理本来就无事可做。
      // 之前这里报「N 层执行了清理，释放 0 B」，看着就像按钮没反应。
      toast(
        "跑完了，但一个字节都没释放——这些位置本来就已经是干净的。\n" +
        "占用超过阈值时，定时体检会自己动手。",
        "warn",
      );
    } else {
      toast(`完成：${acted} 层清理了内容，释放 ${humanBytes(freed)}。`, "ok");
    }
  } catch (e) {
    toast("清理失败：" + e.message, "bad");
  } finally {
    $("btn-sweep").disabled = false;
    $("btn-force").disabled = false;
    await refresh();
  }
}

async function forceLayer(layer) {
  const label = (state.overview?.layers?.[layer]?.label) || layer;
  if (!await askConfirm(`强制清理「${label}」这一层？\n\n会无视阈值立即动手，其它层不受影响。`)) return;
  await runSweep(true, [layer]);
}

async function saveSettings() {
  clearDirty();
  const payload = {
    sweep_cron: $("sweep_cron").value.trim(),
    auto_enabled: $("auto_enabled").checked,
    auto_clean_plugin_modules: $("auto_clean_plugin_modules").checked,
    auto_adopt_cache_dirs: $("auto_adopt_cache_dirs").checked,
    napcat_cache_dirs: $("napcat_cache_dirs")
      .value.split("\n")
      .map((s) => s.trim())
      .filter(Boolean),
    media_dirs: $("media_dirs")
      .value.split("\n")
      .map((s) => s.trim())
      .filter(Boolean),
  };
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

// ---------- 渲染 ----------

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
  $("schedule").innerHTML = [
    ["NapCat", `${escapeHtml(napcat.endpoint || "未知")} ` +
      `<i class="${napcat.connected ? "ok" : "bad"}">${napcat.connected ? "连接正常" : escapeHtml(napcat.error || "连接异常")}</i>`],
    ["体检频率", escapeHtml(s.cron_effective || "未注册")],
    ["下次体检", escapeHtml(s.next_run || "未注册")],
    ["上次体检", escapeHtml(t.last_sweep_at || "从未")],
    ["上次释放", humanBytes(t.last_freed_bytes || 0)],
    ["累计释放", humanBytes(t.total_freed_bytes || 0)],
    ["累计体检次数", String(t.sweep_count || 0)],
  ]
    .map(([k, v]) => `<span><b>${k}：</b>${v}</span>`)
    .join("");
}

function renderLayers(overview) {
  const host = $("layers");
  const layers = Object.entries(overview.layers || {});
  if (!layers.length) {
    host.innerHTML = '<div class="empty">没有可维护的层。</div>';
    return;
  }
  host.innerHTML = layers.map(([key, l]) => {
    const cls = !l.enabled ? "off" : l.due ? "due" : "normal";
    const last = l.last || {};
    const lastText = last.last_run_at
      ? `上次 ${escapeHtml(last.last_run_at)}${last.last_freed_bytes ? ` · 释放 ${humanBytes(last.last_freed_bytes)}` : ""}`
      : "尚未执行";
    const bar = l.ratio === null || l.ratio === undefined
      ? ""
      : `<div class="bar"><i style="width:${l.ratio}%"></i></div>`;
    return `<div class="layer ${cls}">
      <div class="head2">
        <span class="name">${escapeHtml(l.label)}</span>
        <span class="amount">${escapeHtml(l.amount || "")}</span>
      </div>
      ${bar}
      <div class="meta">${escapeHtml(l.detail || "")}</div>
      <div class="last">${lastText}</div>
      ${l.config_error ? `<div class="err">目录配置有误：${escapeHtml(l.config_error)}</div>` : ""}
      ${last.last_error ? `<div class="err">上次错误：${escapeHtml(last.last_error)}</div>` : ""}
      <div class="row actions">
        <button class="btn mini" data-force="${escapeHtml(key)}">强清这层</button>
      </div>
    </div>`;
  }).join("");
  for (const el of host.querySelectorAll("[data-force]")) {
    el.onclick = () => forceLayer(el.dataset.force);
  }
}

function renderPlugins(rows) {
  const host = $("plugins");
  if (!rows.length) {
    host.innerHTML = '<div class="empty">没有已加载的插件模块。</div>';
    return;
  }
  host.innerHTML = rows
    .map(
      (r) => `<div class="plugin">
        <span class="badge ${r.activated ? "ok" : "off"}">${r.activated ? "运行中" : "已停用"}</span>
        <span class="id">${escapeHtml(r.display_name || r.plugin_id)} <span class="muted">${escapeHtml(r.plugin_id)}</span></span>
        <span class="n">${r.module_count} 个模块</span>
        ${r.self || r.activated ? "" : `<button class="btn mini" data-purge="${escapeHtml(r.plugin_id)}">立即清理</button>`}
      </div>`
    )
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
  host.innerHTML = records
    .map((r) => {
      const cls = r.error ? "err" : r.acted ? "acted" : "skip";
      const tag = r.error ? "失败" : r.acted ? "已清理" : "跳过";
      const detail = r.error || r.detail || r.skipped || "";
      const freed = r.freed_bytes ? ` · 释放 ${humanBytes(r.freed_bytes)}` : "";
      return `<div class="result ${cls}">
        <div class="head2">
          <span class="name">${escapeHtml(r.label || r.layer)}</span>
          <span class="meta">${escapeHtml(r.at)}${freed}</span>
        </div>
        <div class="detail ${r.error ? "bad" : ""}">[${tag}] ${escapeHtml(detail)}</div>
      </div>`;
    })
    .join("");
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
  host.innerHTML = state.instances.map((it, i) => `<div class="inst">
    <label class="check"><input type="checkbox" data-f="enable" data-i="${i}" ${it.enable ? "checked" : ""} /><span>启用</span></label>
    <input type="text" data-f="url" data-i="${i}" value="${escapeHtml(it.url)}" placeholder="http://127.0.0.1:3000 或 ws://127.0.0.1:3001" />
    <input type="text" data-f="token" data-i="${i}" value="${escapeHtml(it.token || "")}" placeholder="${it.token_set ? "已配置，留空保持不变" : "留空＝自动解析"}" ${it.token_set ? "data-has-token=\"1\"" : ""} />
    <input type="number" data-f="protocol_interval_hours" data-i="${i}" min="1" value="${it.protocol_interval_hours}" title="协议缓存清理间隔（小时）" />
    <button class="btn mini" data-del="${i}">删除</button>
  </div>`).join("");
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
  $("auto_enabled").checked = Boolean(s.auto_enabled);
  $("auto_clean_plugin_modules").checked = Boolean(s.auto_clean_plugin_modules);
  $("auto_adopt_cache_dirs").checked = Boolean(s.auto_adopt_cache_dirs);
  for (const key of NUMERIC_SETTINGS) {
    if (s[key] !== undefined) $(key).value = s[key];
  }
  $("napcat_cache_dirs").value = (s.napcat_cache_dirs || []).join("\n");
  $("media_dirs").value = (s.media_dirs || []).join("\n");
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
  if (res.checked < 2) {
    $("dead").innerHTML = '<div class="empty">只配了一个实例，没有重复可查。</div>';
    note.classList.add("hidden");
    return;
  }
  note.textContent = res.note || "";
  note.classList.remove("hidden");
  if (!rows.length) {
    $("dead").innerHTML = `<div class="empty">${res.checked} 个实例，没发现重复或长期失联的。</div>`;
    return;
  }
  $("dead").innerHTML = rows.map((r) => {
    const badge = r.removable
      ? '<span class="badge ok">可确认删除</span>'
      : '<span class="badge off">仅提示</span>';
    const btn = r.removable
      ? `<button class="btn mini" data-url="${escapeHtml(r.url)}" data-occ="${Number(r.occurrence) || 0}">删除这一行</button>`
      : "";
    return `<div class="plugin">
      <span class="id">${escapeHtml(r.url)}</span>
      <span class="n">${escapeHtml(r.reason)}</span>
      ${badge}${btn}
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
    const head = `<div class="plugin"><span class="id">${escapeHtml(g.title)}</span>` +
      `<span class="muted">${escapeHtml(g.hint || "")}</span></div>`;
    const rows = (g.rows || []).map((r) => `<div class="plugin">
      <span class="id">${escapeHtml(r.label)}</span>
      <span class="n">${humanBytes(r.size_bytes)} / ${Number(r.file_count) || 0} 个文件</span>
      <span class="muted">${escapeHtml(r.path)}</span>
      ${r.truncated ? '<span class="badge off">未测完</span>' : ""}
    </div>`).join("");
    return head + rows;
  }).join("");
  const warn = res.truncated
    ? '<p class="muted note">部分目录未测完（已达扫描上限），实际占用可能更大。</p>'
    : "";
  $("spaces").innerHTML =
    `<div class="kv"><span>合计：<b>${humanBytes(res.total_bytes || 0)}</b></span>` +
    `<span>耗时 ${Number(res.elapsed_ms) || 0} ms</span></div>` + html + warn +
    '<p class="muted note">纯只读统计。发现大的 QQ 目录，把它填进上面的「自选媒体目录」就能纳入自动清理。</p>';
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
    rows.map((r) => `<div class="plugin">
      <span class="id">${escapeHtml(r.path)}</span>
      <span class="n">${humanBytes(r.size_bytes)}</span>
      <span class="badge off">${escapeHtml(JUNK_LABEL[r.kind] || r.kind)}</span>
      <button class="btn mini" data-junk-kind="${escapeHtml(r.kind)}" data-junk-path="${escapeHtml(r.path)}">删除</button>
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

// ---------- 工具 ----------

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
