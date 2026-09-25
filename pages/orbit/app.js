const bridge = window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);

const state = {
  overview: null,
  instances: [],
  timer: null,
};

const NUMERIC_SETTINGS = [
  "astrbot_cache_threshold_mb",
  "napcat_cache_threshold_mb",
  "napcat_cache_min_age_minutes",
  "napcat_protocol_interval_hours",
];

// ---------- 主题 ----------

function applyTheme(dark) {
  document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
}

// ---------- 初始化 ----------

(async function init() {
  try {
    const ctx = await bridge.ready();
    applyTheme(Boolean(ctx?.isDark));
    bridge.onContext((next) => applyTheme(Boolean(next?.isDark)));
  } catch (e) {
    console.warn("bridge context 不可用", e);
  }
  bindEvents();
  await refresh();
  state.timer = setInterval(refresh, 30000);
})();

function bindEvents() {
  $("btn-refresh").onclick = refresh;
  $("btn-sweep").onclick = () => runSweep(false);
  $("btn-force").onclick = () => {
    if (confirm("强制全清会无视阈值立即清理四层缓存，确定吗？")) runSweep(true);
  };
  $("btn-save").onclick = saveSettings;
  $("btn-discover").onclick = discover;
  $("btn-reauth").onclick = reResolve;
  $("btn-add-inst").onclick = () => {
    state.instances.push({ url: "", token: "", enable: true, protocol_interval_hours: 12 });
    renderInstances();
  };
  $("btn-save-inst").onclick = saveInstances;
  $("btn-dryrun").onclick = dryRun;
  window.addEventListener("beforeunload", () => {
    if (state.timer) clearInterval(state.timer);
  });
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
    state.instances = (overview.connection?.instances || []).map((r) => ({
      url: r.url, token: r.token, enable: r.enable,
      protocol_interval_hours: r.protocol_interval_hours,
    }));
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

async function runSweep(force) {
  $("btn-sweep").disabled = true;
  $("btn-force").disabled = true;
  try {
    const res = await bridge.apiPost("sweep", { force });
    const freed = res.freed_bytes || 0;
    const errs = (res.records || []).filter((r) => r.error).length;
    alert(
      errs
        ? `完成，但有 ${errs} 层失败，详见下方历史。`
        : `完成：${(res.acted || []).length} 层执行了清理，释放 ${humanBytes(freed)}。`
    );
  } catch (e) {
    alert("清理失败：" + e.message);
  } finally {
    $("btn-sweep").disabled = false;
    $("btn-force").disabled = false;
    await refresh();
  }
}

async function saveSettings() {
  const payload = {
    sweep_cron: $("sweep_cron").value.trim(),
    auto_enabled: $("auto_enabled").checked,
    auto_clean_plugin_modules: $("auto_clean_plugin_modules").checked,
    napcat_cache_dirs: $("napcat_cache_dirs")
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
    alert(res.ok ? "已保存并生效。" : `部分未保存：${res.error}`);
  } catch (e) {
    alert("保存失败：" + e.message);
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
  if (!confirm(`确定立即清理 ${pluginId} 的内存模块？`)) return;
  try {
    const res = await bridge.apiPost("purge", { plugin_id: pluginId });
    const r = res.record || {};
    alert(r.detail || r.skipped || "已处理");
  } catch (e) {
    alert("清理失败：" + e.message);
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
  $("schedule").innerHTML = [
    ["NapCat", `${escapeHtml(napcat.endpoint || "未知")} ` +
      `<i class="${napcat.connected ? "ok" : "bad"}">${napcat.connected ? "连接正常" : escapeHtml(napcat.error || "连接异常")}</i>`],
    ["体检频率", escapeHtml(s.cron_effective || "未注册")],
    ["下次体检", escapeHtml(s.next_run || "未注册")],
    ["上次体检", escapeHtml(t.last_sweep_at || "从未")],
    ["累计释放", humanBytes(t.last_freed_bytes || 0)],
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
  host.innerHTML = layers.map(([, l]) => {
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
    </div>`;
  }).join("");
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
  const astrbot = r.astrbot_size_bytes > r.astrbot_threshold_bytes
    ? `<div class="cand"><span class="path">AstrBot 磁盘缓存</span>
       <span class="muted">${humanBytes(r.astrbot_size_bytes)} / 阈值 ${humanBytes(r.astrbot_threshold_bytes)}</span>
       <span class="warn">会清理</span></div>`
    : `<div class="cand"><span class="path">AstrBot 磁盘缓存</span>
       <span class="muted">${humanBytes(r.astrbot_size_bytes)} / 阈值 ${humanBytes(r.astrbot_threshold_bytes)}</span>
       <span class="ok">不会删除</span></div>`;
  const samples = dirs.flatMap((d) => d.samples || []).slice(0, 6);
  $("dryrun-body").innerHTML = `
    <div class="kv">
      <span><b>目录来源：</b>${escapeHtml(r.dir_source || "—")}</span>
      <span><b>将删除：</b>${r.would_delete_files} 个文件 / ${humanBytes(r.would_delete_bytes)}</span>
      <span><b>待清模块：</b>${r.modules_pending} 个</span>
    </div>
    ${dirs.length ? `<div class="discover">${dirs.length ? rows : ""}</div>` : '<div class="empty">当前没有生效的缓存目录。</div>'}
    ${astrbot}
    ${samples.length ? `<div class="muted note">将被删除的文件（前 6 个）：<br>${samples.map(escapeHtml).join("<br>")}</div>` : ""}
    <p class="muted note">以上只是计算结果，<b>点它不会删除任何东西</b>。确认无误后再点「立即体检」或等下一次自动体检。</p>`;
}

function renderInstances() {
  const host = $("inst-list");
  if (!state.instances.length) {
    host.innerHTML = '<div class="empty">还没有实例，点右上角「添加实例」。</div>';
    return;
  }
  host.innerHTML = state.instances.map((it, i) => `<div class="inst">
    <label class="check"><input type="checkbox" data-f="enable" data-i="${i}" ${it.enable ? "checked" : ""} /><span>启用</span></label>
    <input type="text" data-f="url" data-i="${i}" value="${escapeHtml(it.url)}" placeholder="http://127.0.0.1:3000 或 ws://127.0.0.1:3001" />
    <input type="text" data-f="token" data-i="${i}" value="${escapeHtml(it.token)}" placeholder="留空＝自动解析" />
    <input type="number" data-f="protocol_interval_hours" data-i="${i}" min="1" value="${it.protocol_interval_hours}" title="协议缓存清理间隔（小时）" />
    <button class="btn mini" data-del="${i}">删除</button>
  </div>`).join("");
  for (const el of host.querySelectorAll("[data-i]")) {
    el.oninput = el.onchange = () => {
      const idx = Number(el.dataset.i);
      const field = el.dataset.f;
      state.instances[idx][field] = field === "enable" ? el.checked
        : field === "protocol_interval_hours" ? Number(el.value) || 12 : el.value;
    };
  }
  for (const el of host.querySelectorAll("[data-del]")) {
    el.onclick = () => {
      state.instances.splice(Number(el.dataset.del), 1);
      renderInstances();
    };
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
    alert("至少需要一个实例，并填写地址。");
    return;
  }
  $("btn-save-inst").disabled = true;
  try {
    const res = await bridge.apiPost("token", { action: "save_instances", instances: rows });
    alert(`已保存 ${res.count} 个实例。${res.note || ""}`);
    await probeToken();
    await refresh();
  } catch (e) {
    alert("保存失败：" + e.message);
  } finally {
    $("btn-save-inst").disabled = false;
  }
}

async function probeToken() {
  try {
    const res = await bridge.apiGet("token");
    renderTokenState(res);
    renderTokenConfigs(res.configs || []);
  } catch (e) {
    $("token-banner").className = "banner bad";
    $("token-banner").innerHTML = escapeHtml(e.message);
  }
}

async function reResolve() {
  try {
    const res = await bridge.apiPost("token", { action: "resolve" });
    alert(res.note || "已重新解析。");
    await probeToken();
    await refresh();
  } catch (e) {
    alert("解析失败：" + e.message);
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

function fillSettings(s) {
  $("sweep_cron").value = s.sweep_cron ?? "*/15 * * * *";
  $("auto_enabled").checked = Boolean(s.auto_enabled);
  $("auto_clean_plugin_modules").checked = Boolean(s.auto_clean_plugin_modules);
  for (const key of NUMERIC_SETTINGS) {
    if (s[key] !== undefined) $(key).value = s[key];
  }
  $("napcat_cache_dirs").value = (s.napcat_cache_dirs || []).join("\n");
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
