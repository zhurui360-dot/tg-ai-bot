/* ==========================================================================
   TG AI 机器人 · 面板前端
   纯原生 JS，无构建步骤、无外部依赖。
   所有交互走 data-act 事件委托：CSP 里 script-src 'self'，
   内联 onclick 会被浏览器直接拦掉，所以 HTML 里一个内联事件都没有。
   ========================================================================== */

(function () {
  "use strict";

  /* ======================================================================
     0. 小工具
     ====================================================================== */

  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

  var ESC_MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

  function esc(value) {
    return String(value === null || value === undefined ? "" : value).replace(/[&<>"']/g, function (c) {
      return ESC_MAP[c];
    });
  }

  function text(id, value) {
    var node = document.getElementById(id);
    if (node) node.textContent = value;
  }

  function show(node, visible) {
    if (!node) return;
    node.classList.toggle("hidden", !visible);
  }

  function num(value, fallback) {
    var n = parseFloat(value);
    return isNaN(n) ? fallback : n;
  }

  function truncate(str, max) {
    var s = String(str || "");
    return s.length > max ? s.slice(0, max) + "…" : s;
  }

  function firstChar(str) {
    var s = String(str || "").trim();
    return s ? s.charAt(0).toUpperCase() : "?";
  }

  /* SQLite 存的是不带时区的 UTC 时间戳（YYYY-MM-DD HH:MM:SS） */

  function parseTime(raw) {
    var s = String(raw || "").trim();
    if (!s) return null;
    if (/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?$/.test(s)) s = s.replace(" ", "T") + "Z";
    var d = new Date(s);
    return isNaN(d.getTime()) ? null : d;
  }

  function fmtTime(raw) {
    var d = parseTime(raw);
    if (!d) return "";
    var now = new Date();
    var sameDay = d.toDateString() === now.toDateString();
    var hm = ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2);
    if (sameDay) return "今天 " + hm;
    return (d.getMonth() + 1) + "/" + d.getDate() + " " + hm;
  }

  function relTime(raw) {
    var d = parseTime(raw);
    if (!d) return "";
    var diff = Math.floor((Date.now() - d.getTime()) / 1000);
    if (diff < 0) return "刚刚";
    if (diff < 60) return "刚刚";
    if (diff < 3600) return Math.floor(diff / 60) + " 分钟前";
    if (diff < 86400) return Math.floor(diff / 3600) + " 小时前";
    if (diff < 86400 * 7) return Math.floor(diff / 86400) + " 天前";
    return fmtTime(raw);
  }

  /* ======================================================================
     1. 请求层
     ====================================================================== */

  function ApiError(message, status) {
    this.name = "ApiError";
    this.message = message;
    this.status = status || 0;
  }
  ApiError.prototype = Object.create(Error.prototype);

  async function api(path, options) {
    var opts = options || {};
    var init = {
      method: opts.method || "GET",
      credentials: "same-origin",
      headers: { Accept: "application/json" }
    };
    if (opts.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }

    var res;
    try {
      res = await fetch(path, init);
    } catch (err) {
      throw new ApiError("连不上面板服务，检查容器是否还在运行", 0);
    }

    var raw = "";
    try { raw = await res.text(); } catch (err) { raw = ""; }
    var data = null;
    if (raw) {
      try { data = JSON.parse(raw); } catch (err) { data = null; }
    }

    if (res.status === 401 && path !== "/api/panel/login") {
      showLogin("登录已过期，请重新登录");
    }

    if (!res.ok) {
      var detail = data && (data.detail || data.message);
      if (Array.isArray(detail)) detail = detail.map(function (d) { return d.msg || String(d); }).join("；");
      if (detail && typeof detail === "object") detail = JSON.stringify(detail);
      throw new ApiError(detail || "请求失败（HTTP " + res.status + "）", res.status);
    }
    return data || {};
  }

  /* ======================================================================
     2. 状态
     ====================================================================== */

  var state = {
    ready: false,
    tab: "dashboard",
    version: "",
    standby: false,
    settings: {},
    bots: [],
    botList: [],
    agents: [],
    agentId: "",
    bot: {},
    memory: {},
    messages: [],
    msgStats: { total: 0, today: 0, chats: 0 },
    usage: null,
    memAgent: "",
    memScope: "",
    remStatus: "pending",
    lastDiag: null
  };

  var TRIGGER_LABELS = { mention: "被 @ 时回复", all: "每条都回", private: "只回私聊" };
  var SCOPE_LABELS = { global: "通用", chat: "会话", user: "某人" };

  function triggerLabel(value) {
    return TRIGGER_LABELS[value] || "被 @ 时回复";
  }

  function agentName(id) {
    var found = null;
    state.agents.forEach(function (a) {
      if (String(a.id) === String(id)) found = a;
    });
    if (found) return found.name || ("#" + id);
    if (id) return "已删除的智能体";
    return "跟随默认";
  }

  /* ======================================================================
     3. 提示 / 弹窗 / 按钮忙碌态
     ====================================================================== */

  function toast(message, type) {
    var wrap = $("#toastWrap");
    if (!wrap) return;
    var node = document.createElement("div");
    node.className = "toast" + (type ? " " + type : "");
    node.textContent = message;
    wrap.appendChild(node);
    setTimeout(function () {
      node.style.transition = "opacity .25s, transform .25s";
      node.style.opacity = "0";
      node.style.transform = "translateY(-6px)";
      setTimeout(function () { node.remove(); }, 260);
    }, 3600);
  }

  function busy(btn, on) {
    if (!btn) return;
    if (on) {
      if (!btn.dataset.label) btn.dataset.label = btn.innerHTML;
      btn.disabled = true;
      btn.innerHTML = '<span class="spin"></span>' + (btn.dataset.busy || "处理中");
    } else {
      btn.disabled = false;
      if (btn.dataset.label) {
        btn.innerHTML = btn.dataset.label;
        delete btn.dataset.label;
      }
    }
  }

  var modals = [];

  function openModal(config) {
    var mask = document.createElement("div");
    mask.className = "modal-mask";
    mask.innerHTML =
      '<div class="modal' + (config.wide ? " wide" : "") + '" role="dialog" aria-modal="true">' +
        '<div class="modal-head">' +
          "<h3>" + esc(config.title) + "</h3>" +
          '<button class="btn icon ghost" type="button" data-close aria-label="关闭">' +
            '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>' +
          "</button>" +
        "</div>" +
        '<div class="modal-body">' + config.body + "</div>" +
        (config.footer ? '<div class="modal-foot">' + config.footer + "</div>" : "") +
      "</div>";

    $("#modalRoot").appendChild(mask);

    var handle = {
      el: mask,
      body: $(".modal-body", mask),
      close: function () {
        mask.remove();
        modals = modals.filter(function (m) { return m !== handle; });
      }
    };

    mask.addEventListener("click", function (event) {
      if (event.target === mask || event.target.closest("[data-close]")) handle.close();
    });

    modals.push(handle);

    if (config.onMount) config.onMount(handle);

    var first = mask.querySelector("input:not([type=hidden]), textarea, select");
    if (first) setTimeout(function () { first.focus(); }, 40);

    return handle;
  }

  function confirmDialog(config) {
    return new Promise(function (resolve) {
      var settled = false;
      function done(value) {
        if (settled) return;
        settled = true;
        resolve(value);
      }
      var handle = openModal({
        title: config.title || "确认操作",
        body: '<p class="muted" style="margin:0;font-size:13px;line-height:1.75">' + esc(config.message || "") + "</p>",
        footer:
          '<button class="btn ghost" type="button" data-close>取消</button>' +
          '<button class="btn ' + (config.danger ? "danger" : "primary") + '" type="button" data-ok>' +
            esc(config.okText || "确定") +
          "</button>",
        onMount: function (modal) {
          $("[data-ok]", modal.el).addEventListener("click", function () {
            done(true);
            modal.close();
          });
          modal.el.addEventListener("click", function (event) {
            if (event.target === modal.el || event.target.closest("[data-close]")) done(false);
          });
        }
      });
      handle.el.addEventListener("modal:esc", function () { done(false); });
    });
  }

  /* 单行输入弹窗。确认返回字符串，取消返回 null（和 window.prompt 的语义一致，
     但样式统一、支持 Esc 关闭，也不会被浏览器拦截）。 */
  function promptDialog(config) {
    return new Promise(function (resolve) {
      var settled = false;
      function done(value) {
        if (settled) return;
        settled = true;
        resolve(value);
      }
      var inputId = "promptInput" + Date.now();
      var handle = openModal({
        title: config.title || "输入",
        body:
          (config.label ? '<label class="dim small" style="display:block;margin-bottom:6px">' +
            esc(config.label) + "</label>" : "") +
          '<input class="input" id="' + inputId + '" type="text" autocomplete="off" value="' +
            esc(config.value || "") + '">' +
          (config.hint ? '<p class="dim tiny" style="margin:8px 0 0">' + esc(config.hint) + "</p>" : ""),
        footer:
          '<button class="btn ghost" type="button" data-close>取消</button>' +
          '<button class="btn primary" type="button" data-ok>' + esc(config.okText || "确定") + "</button>",
        onMount: function (modal) {
          var input = document.getElementById(inputId);
          function submit() { done(input ? input.value : ""); modal.close(); }
          $("[data-ok]", modal.el).addEventListener("click", submit);
          if (input) {
            input.addEventListener("keydown", function (event) {
              if (event.key === "Enter") { event.preventDefault(); submit(); }
            });
          }
          modal.el.addEventListener("click", function (event) {
            if (event.target === modal.el || event.target.closest("[data-close]")) done(null);
          });
        }
      });
      handle.el.addEventListener("modal:esc", function () { done(null); });
    });
  }

  function emptyState(title, desc, actionHtml) {
    return (
      '<div class="empty">' +
        '<div class="empty-ico">' +
          '<svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 8v4M12 16h.01"/></svg>' +
        "</div>" +
        "<h4>" + esc(title) + "</h4>" +
        "<p>" + esc(desc) + "</p>" +
        (actionHtml || "") +
      "</div>"
    );
  }

  function copyText(value) {
    var str = String(value || "");
    function fallback() {
      var area = document.createElement("textarea");
      area.value = str;
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      try { document.execCommand("copy"); } catch (err) { /* 忽略 */ }
      area.remove();
      toast("已复制 " + str, "ok");
    }
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(str).then(function () {
        toast("已复制 " + str, "ok");
      }, fallback);
    } else {
      fallback();
    }
  }

  /* ======================================================================
     4. 登录 / 启动
     ====================================================================== */

  // 启动遮罩：HTML 里登录页默认是隐藏的，鉴权结果出来前什么都不显示。
  // 只有等得久了才亮遮罩，避免刷新时先闪一下登录框。
  var bootSplashTimer = null;

  function hideBootSplash() {
    if (bootSplashTimer) {
      clearTimeout(bootSplashTimer);
      bootSplashTimer = null;
    }
    show($("#bootSplash"), false);
  }

  function showLogin(message) {
    hideBootSplash();
    modals.slice().forEach(function (m) { m.close(); });
    show($("#appLayout"), false);
    show($("#loginScreen"), true);
    state.ready = false;
    var err = $("#panelLoginError");
    if (err) err.textContent = message || "";
    var pwd = $("#panelPassword");
    if (pwd) setTimeout(function () { pwd.focus(); }, 60);
  }

  function showApp() {
    hideBootSplash();
    show($("#loginScreen"), false);
    show($("#appLayout"), true);
    state.ready = true;
  }

  async function doLogin(event) {
    event.preventDefault();
    var pwd = ($("#panelPassword").value || "").trim();
    var errBox = $("#panelLoginError");
    var btn = $("#panelLoginBtn");
    errBox.textContent = "";
    if (!pwd) {
      errBox.textContent = "请输入面板密码";
      return;
    }
    busy(btn, true);
    try {
      await api("/api/panel/login", { method: "POST", body: { password: pwd } });
      $("#panelPassword").value = "";
      await loadAll();
      toast("登录成功", "ok");
    } catch (err) {
      errBox.textContent = err.message;
      $("#panelPassword").select();
    } finally {
      busy(btn, false);
    }
  }

  async function logout() {
    try { await api("/api/panel/logout", { method: "POST" }); } catch (err) { /* 无所谓 */ }
    showLogin("");
    $("#panelPassword").value = "";
    toast("已退出登录");
  }

  async function loadAll() {
    var data = await api("/api/bootstrap");
    applyBootstrap(data);
    showApp();
    await refreshAll();
    go(tabFromHash());
  }

  function applyBootstrap(data) {
    state.version = data.version || "";
    state.commit = data.commit || "";
    state.shortCommit = data.short_commit || (state.commit ? state.commit.slice(0, 8) : "");
    state.standby = !!data.standby;
    state.settings = data.settings || {};
    state.bots = data.bots || [];
    state.agentId = String(data.ai_agent_id || "");
    state.bot = data.bot || {};
    state.memory = data.memory || {};

    var title = state.settings.panel_title || "TG AI 机器人";
    document.title = title;
    text("brandName", title);
    // 侧边栏挂 commit 而不是写死的发布号：升级完能一眼看出代码真的换了。
    // 镜像内置代码没有 commit（空串），这时候退回版本号。
    var verText = state.shortCommit
      ? state.shortCommit
      : "v" + state.version;
    text("verLabel", verText + (state.standby ? " · 值守" : ""));
    // 版本号本身是仓库链接，title 里既交代提交又说明能点
    var verNode = $("#verLabel");
    if (verNode) {
      verNode.title = (state.commit ? "运行中的代码提交：" + state.commit + "　" : "") + "点击在 GitHub 上打开项目";
    }
    text("navBotCount", String(state.bots.length));
    text("navAgentCount", String((data.ai_agents || []).length));

    state.agents = data.ai_agents || [];
    fillSettings();
    renderDashboard();
    renderMemorySelect();
  }

  async function refreshAll() {
    await Promise.all([
      loadBotList().catch(function (err) { console.warn("机器人列表加载失败", err); }),
      loadAgentList().catch(function (err) { console.warn("智能体列表加载失败", err); }),
      loadMessages(12).catch(function (err) { console.warn("对话加载失败", err); })
    ]);
    renderDashboard();
    if (state.tab === "bots") renderBots();
    if (state.tab === "agents") renderAgents();
  }

  /* ======================================================================
     5. 页面切换
     ====================================================================== */

  var PAGES = {
    dashboard: "pageDashboard",
    bots: "pageBots",
    agents: "pageAgents",
    memory: "pageMemory",
    settings: "pageSettings"
  };

  function tabFromHash() {
    var name = (location.hash || "").replace("#", "").trim();
    return PAGES[name] ? name : "dashboard";
  }

  function go(tab) {
    if (!PAGES[tab]) tab = "dashboard";
    state.tab = tab;
    if (location.hash !== "#" + tab) {
      history.replaceState(null, "", "#" + tab);
    }
    $$(".nav-item[data-tab]").forEach(function (node) {
      node.classList.toggle("active", node.dataset.tab === tab);
    });
    Object.keys(PAGES).forEach(function (key) {
      show(document.getElementById(PAGES[key]), key === tab);
    });

    if (tab === "bots") {
      loadBotList().then(renderBots).catch(function (err) { toast(err.message, "err"); });
    } else if (tab === "agents") {
      loadAgentList().then(renderAgents).catch(function (err) { toast(err.message, "err"); });
    } else if (tab === "memory") {
      renderMemorySelect();
      loadMemory().catch(function (err) { toast(err.message, "err"); });
    } else if (tab === "settings") {
      fillSettings();
      // 先读本地状态把卡片填上，再去 GitHub 核对一次有没有新版本
      loadUpdateStatus().then(autoCheckUpdate);
      loadBackupStatus();
      loadReminders().catch(function (err) { console.warn("提醒列表加载失败", err); });
    } else {
      loadMessages(12).then(renderDashboard).catch(function () {});
      loadUsage().then(renderUsage);
    }
  }

  /* ======================================================================
     6. 数据加载
     ====================================================================== */

  async function loadBotList() {
    var data = await api("/api/bots");
    state.botList = data.bots || [];
    text("navBotCount", String(state.botList.length));
  }

  async function loadAgentList() {
    var data = await api("/api/ai/agents");
    state.agents = data.agents || [];
    text("navAgentCount", String(state.agents.length));
    renderMemorySelect();
  }

  async function loadMessages(limit) {
    var data = await api("/api/bot/messages?limit=" + (limit || 60));
    state.messages = data.messages || [];
    state.msgStats = data.stats || { total: 0, today: 0, chats: 0 };
    return state.messages;
  }

  async function loadUsage() {
    try {
      state.usage = await api("/api/usage?days=1");
    } catch (err) {
      // 用量只是参考信息，取不到不影响主功能
      console.warn("用量加载失败", err);
      state.usage = null;
    }
    return state.usage;
  }

  /* ======================================================================
     7. 概览
     ====================================================================== */

  function renderDashboard() {
    var bots = state.bots || [];
    var bot = state.bot || {};
    var stats = state.msgStats || { total: 0, today: 0, chats: 0 };
    var agents = state.agents || [];

    var enabledBots = bots.filter(function (b) { return b.enabled; }).length;
    text("stBots", String(bots.length));
    text("stBotsNote", bots.length ? enabledBots + " 个已启用" : "还没接入");

    var running = !!bot.running;
    text("stRun", running ? "运行中" : (bot.enabled ? "已停止" : "未启用"));
    var runBox = $("#stRun") ? $("#stRun").closest(".stat") : null;
    if (runBox) {
      runBox.classList.toggle("ok", running);
      runBox.classList.toggle("err", !running && !!bot.last_error);
      runBox.classList.toggle("warn", !running && !bot.last_error && !!bot.enabled);
    }
    var runNote = "等待启动";
    if (bot.last_error) runNote = truncate(bot.last_error, 46);
    else if (running && bot.last_poll) runNote = "上次轮询 " + relTime(bot.last_poll);
    else if (running) runNote = "长轮询中";
    else if (!bots.length) runNote = "没有可运行的机器人";
    text("stRunNote", runNote);

    text("stToday", String(stats.today || 0));
    text("stTodayNote", "累计 " + (stats.total || 0) + " 条 · " + (stats.chats || 0) + " 个会话");

    var noteTotal = 0;
    Object.keys(state.memory || {}).forEach(function (key) {
      noteTotal += num((state.memory[key] || {}).total, 0);
    });
    text("stNotes", String(noteTotal));
    text("stNotesNote", agents.length + " 个智能体");

    renderDashAlert(bot, bots);
    renderDashConfig(bot, agents);
    renderDashBots(bot);
    renderDashMessages();
  }

  function renderUsage() {
    var u = state.usage;
    if (!u) {
      text("stTokens", "—");
      text("stTokensNote", "统计不可用");
      return;
    }
    var tokens = num(u.total_tokens, 0);
    text("stTokens", tokens >= 10000 ? (tokens / 1000).toFixed(1) + "k" : String(tokens));

    // 上游没返回 usage 时 token 是 0。必须显式说明，否则用户会以为「没花钱」。
    var note;
    if (!u.calls) {
      note = "今天还没有调用";
    } else if (u.unmetered && u.unmetered === u.calls) {
      note = u.calls + " 次调用，接口没返回用量";
    } else if (u.unmetered) {
      note = u.calls + " 次调用，" + u.unmetered + " 次未计量";
    } else {
      note = u.calls + " 次调用 · 均 " + Math.round(u.avg_ms / 100) / 10 + "s";
    }
    text("stTokensNote", note);
  }

  function renderDashAlert(bot, bots) {    var box = $("#dashAlert");
    if (!box) return;
    var alerts = [];

    if (state.standby) {
      alerts.push(notice("warn", "值守模式（STANDBY=1）",
        "这个实例只提供面板，不会启动机器人。多个实例同时跑同一个 Bot Token 会互相抢消息，所以备用实例保持这样就行。"));
    }
    if (state.settings && state.settings.using_default_password) {
      alerts.push(notice("err", "面板还在用默认密码",
        "默认密码是 admin123，谁连上这个地址都能进来。去「设置」里改掉，改完所有旧会话立即失效。",
        '<button class="btn sm" type="button" data-act="go" data-tab="settings">去改密码</button>'));
    }
    if (!bots.length) {
      alerts.push(notice("brand", "还没有接入机器人",
        "去 Telegram 找 @BotFather 发 /newbot 拿一个 Token，粘进来就能用了。私聊必回、群里被 @ 时回复。",
        '<button class="btn sm primary" type="button" data-act="bot-form">添加机器人</button>'));
    } else if (!bot.running && bot.enabled && bot.last_error) {
      alerts.push(notice("err", "机器人没能跑起来",
        truncate(bot.last_error, 220),
        '<button class="btn sm" type="button" data-act="diagnose">网络自检</button>'));
    }

    box.innerHTML = alerts.join("");
  }

  function notice(type, title, body, action) {
    return (
      '<div class="notice ' + type + ' mb-12">' +
        '<svg class="n-ico" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 16v-4M12 8h.01"/></svg>' +
        '<div class="n-body grow">' +
          "<strong>" + esc(title) + "</strong>" +
          '<div class="mt-8">' + esc(body) + "</div>" +
          (action ? '<div class="mt-12">' + action + "</div>" : "") +
        "</div>" +
      "</div>"
    );
  }

  function renderDashConfig(bot, agents) {
    var box = $("#dashConfig");
    if (!box) return;
    var agent = null;
    agents.forEach(function (a) { if (String(a.id) === String(bot.ai_agent_id)) agent = a; });
    var rows = [
      ["出网地址", '<span class="code-inline">' + esc(bot.api_root || "https://api.telegram.org") + "</span>"],
      ["出网代理", bot.proxy ? '<span class="code-inline">' + esc(bot.proxy) + "</span>" : "直连（未配置代理）"],
      ["轮询状态", bot.running ? "运行中" : (bot.token_set ? "已停止" : "没有 Token")],
      ["触发方式", esc(triggerLabel(bot.trigger))],
      ["智能体", agent ? esc(agent.name) + ' <span class="dim tiny">' + esc(agent.model || "") + "</span>" : '<span class="dim">未绑定，使用默认</span>'],
      ["接口地址", bot.ai_base_url ? '<span class="code-inline">' + esc(bot.ai_base_url) + "</span>" : '<span class="dim">未配置</span>'],
      ["模型 / Key", esc(bot.ai_model || "未配置") + (bot.ai_key_set ? ' <span class="badge ok">Key 已存</span>' : ' <span class="badge warn">缺 Key</span>')],
      ["时区", esc((state.settings || {}).timezone || "Asia/Shanghai")]
    ];
    box.innerHTML = rows.map(function (row) {
      return "<dt>" + esc(row[0]) + "</dt><dd>" + row[1] + "</dd>";
    }).join("");
  }

  function renderDashBots(bot) {
    var box = $("#dashBots");
    if (!box) return;
    // status() 在没有真实机器人时会塞一条 id=0 的占位记录，这里滤掉
    var list = ((bot && bot.bots) || []).filter(function (item) { return item.id; });
    if (!list.length) list = state.bots || [];
    if (!list.length) {
      box.innerHTML = emptyState("暂无机器人", "接入第一个机器人后，这里会显示它的轮询状态和回复数。",
        '<button class="btn primary sm" type="button" data-act="bot-form">添加机器人</button>');
      return;
    }
    box.innerHTML = '<div class="row-list">' + list.map(function (item) {
      var alive = !!item.running;
      var badge = alive
        ? '<span class="badge ok"><span class="dot pulse"></span>运行中</span>'
        : (item.enabled
            ? '<span class="badge warn"><span class="dot"></span>已停止</span>'
            : '<span class="badge"><span class="dot"></span>已停用</span>');
      var meta = [];
      if (item.username) meta.push("@" + item.username);
      meta.push(triggerLabel(item.trigger));
      meta.push("回复 " + num(item.replies, 0) + " 条");
      if (item.last_error) meta.push("最近报错：" + truncate(item.last_error, 30));
      return (
        '<div class="row-item">' +
          '<div class="avatar' + (alive ? " ok" : "") + '">' + esc(firstChar(item.name || item.username)) + "</div>" +
          '<div class="grow">' +
            '<div class="title">' + esc(item.name || "未命名机器人") + badge + "</div>" +
            '<div class="meta">' + esc(meta.join(" · ")) + "</div>" +
          "</div>" +
          '<div class="actions">' +
            '<button class="btn sm ghost" type="button" data-act="bot-restart" data-id="' + esc(item.id) + '">重启</button>' +
            '<button class="btn sm ghost" type="button" data-act="go" data-tab="bots">管理</button>' +
          "</div>" +
        "</div>"
      );
    }).join("") + "</div>";
  }

  function renderDashMessages() {
    var box = $("#dashMessages");
    if (!box) return;
    var list = (state.messages || []).slice(0, 8);
    if (!list.length) {
      box.innerHTML = emptyState("还没有对话", "机器人在私聊里收到消息、或在群里被 @ 之后，这里就能看到实际收发的内容。");
      return;
    }
    box.innerHTML = '<div class="card-body">' + messageListHtml(list) + "</div>";
  }

  function messageListHtml(list) {
    var items = list.slice().reverse();
    return '<div class="msg-list">' + items.map(function (m) {
      var role = m.role === "assistant" ? "assistant" : "user";
      var meta = [];
      if (m.chat_title || m.chat_id) meta.push(m.chat_title || m.chat_id);
      if (m.author && role === "user") meta.push(m.author);
      meta.push(relTime(m.ts));
      return (
        '<div class="msg ' + role + '">' +
          '<div class="msg-bubble">' + esc(m.content) + "</div>" +
          '<div class="msg-meta">' + esc(meta.join(" · ")) + "</div>" +
        "</div>"
      );
    }).join("") + "</div>";
  }

  /* ======================================================================
     8. 机器人页
     ====================================================================== */

  function renderBots() {
    var box = $("#botsList");
    if (!box) return;
    var list = state.botList || [];

    if (!list.length) {
      box.innerHTML =
        '<div class="card"><div class="card-body">' +
          emptyState(
            "还没有机器人",
            "打开 Telegram，找 @BotFather 发 /newbot，拿到形如 123456789:AAH... 的 Token，粘进来即可。",
            '<button class="btn primary" type="button" data-act="bot-form">添加第一个机器人</button>'
          ) +
        "</div></div>";
      return;
    }

    var rows = list.map(function (item) {
      var st = item.status || {};
      var alive = !!st.running;
      var badges = "";
      if (item.default) badges += '<span class="badge brand">默认</span>';
      if (alive) badges += '<span class="badge ok"><span class="dot pulse"></span>运行中</span>';
      else if (item.enabled) badges += '<span class="badge warn"><span class="dot"></span>已停止</span>';
      else badges += '<span class="badge"><span class="dot"></span>已停用</span>';
      if (!item.token_set) badges += '<span class="badge err">缺 Token</span>';

      var meta = [];
      if (item.username) meta.push("@" + item.username);
      meta.push(triggerLabel(item.trigger));
      meta.push("智能体：" + agentName(item.ai_agent_id));
      meta.push("回复 " + num(st.replies, 0) + " 条");
      if (st.last_poll) meta.push("轮询 " + relTime(st.last_poll));

      var toggleText = item.enabled ? "停用" : "启用";

      return (
        '<div class="row-item">' +
          '<div class="avatar' + (alive ? " ok" : "") + '">' + esc(firstChar(item.name || item.username)) + "</div>" +
          '<div class="grow">' +
            '<div class="title">' + esc(item.name || "未命名机器人") + badges + "</div>" +
            '<div class="meta">' + esc(meta.join(" · ")) + "</div>" +
            (st.last_error ? '<div class="meta" style="color:var(--err)">' + esc(truncate(st.last_error, 90)) + "</div>" : "") +
          "</div>" +
          '<div class="actions">' +
            '<button class="btn sm ghost" type="button" data-act="bot-restart" data-id="' + esc(item.id) + '">重启</button>' +
            '<button class="btn sm ghost" type="button" data-act="bot-toggle" data-id="' + esc(item.id) + '">' + toggleText + "</button>" +
            '<button class="btn sm ghost" type="button" data-act="bot-edit" data-id="' + esc(item.id) + '">编辑</button>' +
            '<button class="btn sm danger" type="button" data-act="bot-del" data-id="' + esc(item.id) + '" data-name="' + esc(item.name || "") + '">删除</button>' +
          "</div>" +
        "</div>"
      );
    }).join("");

    var chats = [];
    list.forEach(function (item) {
      (item.chats || []).forEach(function (chat) {
        chats.push({ id: chat.id, title: chat.title || chat.username || String(chat.id), username: chat.username, bot: item.name });
      });
    });

    var chatHtml = "";
    if (chats.length) {
      chatHtml =
        '<div class="card">' +
          '<div class="card-head"><div><h3>已知会话</h3>' +
            '<p class="sub">机器人待过的群。点一下就复制群 ID，填到需要的地方省得自己翻</p></div></div>' +
          '<div class="row-list">' +
            chats.map(function (chat) {
              return (
                '<div class="row-item">' +
                  '<div class="avatar">' + esc(firstChar(chat.title)) + "</div>" +
                  '<div class="grow">' +
                    '<div class="title">' + esc(chat.title) + "</div>" +
                    '<div class="meta">' + esc((chat.username ? "@" + chat.username + " · " : "") + (chat.bot || "")) + "</div>" +
                  "</div>" +
                  '<div class="actions">' +
                    '<button class="btn sm ghost" type="button" data-act="copy" data-copy="' + esc(chat.id) + '">' +
                      '<span class="code-inline">' + esc(chat.id) + "</span>" +
                    "</button>" +
                  "</div>" +
                "</div>"
              );
            }).join("") +
          "</div>" +
        "</div>";
    }

    box.innerHTML =
      '<div class="card"><div class="card-head"><div><h3>机器人列表</h3>' +
        '<p class="sub">' + list.length + " 个，已启用 " + list.filter(function (b) { return b.enabled; }).length + " 个</p>" +
      "</div></div>" +
      '<div class="row-list">' + rows + "</div></div>" +
      chatHtml;
  }

  function openBotForm(botId) {
    var editing = null;
    (state.botList || []).forEach(function (b) {
      if (String(b.id) === String(botId)) editing = b;
    });

    var agentOptions = ['<option value="0">跟随默认智能体</option>'].concat(
      (state.agents || []).map(function (a) {
        var selected = editing && String(editing.ai_agent_id) === String(a.id) ? " selected" : "";
        return '<option value="' + esc(a.id) + '"' + selected + ">" + esc(a.name) + " · " + esc(a.model || "未设模型") + "</option>";
      })
    ).join("");

    var triggerOptions = ["mention", "all", "private"].map(function (key) {
      var selected = editing && editing.trigger === key ? " selected" : (!editing && key === "mention" ? " selected" : "");
      return '<option value="' + key + '"' + selected + ">" + TRIGGER_LABELS[key] + "</option>";
    }).join("");

    var body =
      '<form id="botForm" autocomplete="off">' +
        '<div class="field">' +
          "<label>Bot Token</label>" +
          '<input class="input mono" id="botToken" type="password" placeholder="' +
            (editing && editing.token_set ? "已保存，留空则不修改" : "123456789:AAHxxxxxxxxxxxxxxxxxxxxxxxxxx") + '">' +
          '<div class="hint">' + (editing ? "换 Token 会先校验再保存，校验不过不会改动现有配置。" : "在 Telegram 里找 @BotFather，发 /newbot 就能拿到。") + "</div>" +
        "</div>" +
        '<div class="grid-2">' +
          '<div class="field">' +
            "<label>显示名称</label>" +
            '<input class="input" id="botName" placeholder="留空就用 Telegram 里的名字" value="' + esc(editing ? editing.name : "") + '">' +
          "</div>" +
          '<div class="field">' +
            "<label>什么时候回复</label>" +
            '<select class="select" id="botTrigger">' + triggerOptions + "</select>" +
            '<div class="hint">群里默认只在被 @ 或回复它时说话，不刷屏。</div>' +
          "</div>" +
        "</div>" +
        '<div class="field">' +
          "<label>使用哪个智能体</label>" +
          '<select class="select" id="botAgent">' + agentOptions + "</select>" +
          '<div class="hint">智能体决定它是谁、用什么模型、什么语气。</div>' +
        "</div>" +
        '<div class="field">' +
          '<label class="switch">' +
            '<input type="checkbox" id="botEnabled"' + (!editing || editing.enabled ? " checked" : "") + ">" +
            '<span class="switch-track"></span>' +
            '<span class="switch-label">启用这个机器人</span>' +
          "</label>" +
        "</div>" +
      "</form>";

    openModal({
      title: editing ? "编辑机器人" : "添加机器人",
      body: body,
      footer:
        '<button class="btn ghost" type="button" data-close>取消</button>' +
        '<button class="btn primary" type="submit" form="botForm" data-busy="保存中">' +
          (editing ? "保存修改" : "校验并添加") +
        "</button>",
      onMount: function (modal) {
        var form = $("#botForm", modal.el);
        form.addEventListener("submit", async function (event) {
          event.preventDefault();
          var btn = $('button[type="submit"]', modal.el);
          var token = ($("#botToken", modal.el).value || "").trim();
          var payload = {
            name: ($("#botName", modal.el).value || "").trim(),
            trigger: $("#botTrigger", modal.el).value,
            ai_agent_id: $("#botAgent", modal.el).value,
            enabled: $("#botEnabled", modal.el).checked
          };
          if (!editing && !token) {
            toast("先填 Bot Token", "err");
            $("#botToken", modal.el).focus();
            return;
          }
          if (token) payload.token = token;

          busy(btn, true);
          try {
            if (editing) await api("/api/bots/" + editing.id, { method: "PUT", body: payload });
            else await api("/api/bots", { method: "POST", body: payload });
            modal.close();
            toast(editing ? "已保存" : "机器人已接入", "ok");
            await refreshAll();
            if (state.tab === "bots") renderBots();
          } catch (err) {
            toast(err.message, "err");
          } finally {
            busy(btn, false);
          }
        });
      }
    });
  }

  async function restartBot(btn) {
    var id = btn.dataset.id;
    busy(btn, true);
    try {
      await api("/api/bots/" + id + "/restart", { method: "POST" });
      toast("已重启", "ok");
      await refreshAll();
      renderBots();
    } catch (err) {
      toast(err.message, "err");
    } finally {
      busy(btn, false);
    }
  }

  async function toggleBot(btn) {
    var id = btn.dataset.id;
    var record = null;
    (state.botList || []).forEach(function (b) { if (String(b.id) === String(id)) record = b; });
    if (record && record.enabled) {
      var ok = await confirmDialog({
        title: "停用这个机器人？",
        message: "停用后它不再拉取消息、也不会回复，但配置和 Token 都会保留，随时可以再启用。",
        okText: "停用"
      });
      if (!ok) return;
    }
    busy(btn, true);
    try {
      await api("/api/bots/" + id + "/toggle", { method: "POST", body: {} });
      toast("已切换", "ok");
      await refreshAll();
      renderBots();
    } catch (err) {
      toast(err.message, "err");
    } finally {
      busy(btn, false);
    }
  }

  async function deleteBot(btn) {
    var id = btn.dataset.id;
    var name = btn.dataset.name || ("#" + id);
    var ok = await confirmDialog({
      title: "删除 " + name + "？",
      message: "会停止轮询并删掉这个机器人的配置。对话记录和记忆不受影响，但这个操作不能撤销。",
      okText: "删除",
      danger: true
    });
    if (!ok) return;
    busy(btn, true);
    try {
      await api("/api/bots/" + id, { method: "DELETE" });
      toast("已删除", "ok");
      await refreshAll();
      renderBots();
    } catch (err) {
      toast(err.message, "err");
    } finally {
      busy(btn, false);
    }
  }

  /* ======================================================================
     9. 智能体页
     ====================================================================== */

  function renderAgents() {
    var box = $("#agentsList");
    if (!box) return;
    var list = state.agents || [];
    if (!list.length) {
      box.innerHTML =
        '<div class="card"><div class="card-body">' +
          emptyState(
            "还没有智能体",
            "智能体 = 名字 + 模型 + 提示词。填一个 OpenAI 兼容接口（DeepSeek、Kimi、OpenAI 都行），机器人就有脑子了。",
            '<button class="btn primary" type="button" data-act="agent-form">新建第一个智能体</button>'
          ) +
        "</div></div>";
      return;
    }

    var rows = list.map(function (item) {
      var badges = "";
      if (item.is_default) badges += '<span class="badge brand">默认</span>';
      if (!item.enabled) badges += '<span class="badge">已停用</span>';
      if (!item.key_set) badges += '<span class="badge err">缺 Key</span>';

      var meta = [item.model || "未设模型", item.base_url || "未设地址"];
      var nums = "温度 " + (item.temperature || "0.7") + " · 上下文 " + (item.history_len || 0) + " 轮 · 上限 " + (item.max_tokens || 0) + (item.vision ? " · 可看图" : "");

      return (
        '<div class="row-item">' +
          '<div class="avatar' + (item.is_default ? " ok" : "") + '">' + esc(firstChar(item.name)) + "</div>" +
          '<div class="grow">' +
            '<div class="title">' + esc(item.name) + badges + "</div>" +
            '<div class="meta">' + esc(meta.join(" · ")) + "</div>" +
            '<div class="meta tiny">' + esc(nums) + "</div>" +
          "</div>" +
          '<div class="actions">' +
            '<button class="btn sm ghost" type="button" data-act="agent-test" data-id="' + esc(item.id) + '">测试</button>' +
            (item.is_default ? "" : '<button class="btn sm ghost" type="button" data-act="agent-default" data-id="' + esc(item.id) + '">设为默认</button>') +
            '<button class="btn sm ghost" type="button" data-act="agent-edit" data-id="' + esc(item.id) + '">编辑</button>' +
            '<button class="btn sm danger" type="button" data-act="agent-del" data-id="' + esc(item.id) + '" data-name="' + esc(item.name) + '">删除</button>' +
          "</div>" +
        "</div>"
      );
    }).join("");

    var defaultName = "";
    list.forEach(function (a) { if (a.is_default) defaultName = a.name; });

    box.innerHTML =
      '<div class="card">' +
        '<div class="card-head"><div><h3>智能体</h3>' +
          '<p class="sub">' + list.length + " 个，默认是 " + esc(defaultName || "未设置") + "</p></div></div>" +
        '<div class="row-list">' + rows + "</div>" +
      "</div>" +
      '<div class="card"><div class="card-body">' +
        '<dl class="kv">' +
          "<dt>什么是智能体</dt><dd>一组「接口地址 + Key + 模型 + 提示词」。同一个机器人换智能体，等于换一个性格和脑子。</dd>" +
          "<dt>怎么选模型</dt><dd>填到 /v1 为止，比如 DeepSeek 用 <span class=\"code-inline\">https://api.deepseek.com/v1</span>、模型名 <span class=\"code-inline\">deepseek-chat</span>。填完点「拉取模型」能直接列表里选。</dd>" +
          "<dt>提示词写什么</dt><dd>写「它是谁、怎么说话、不能做什么」。写得越具体，回话越像你要的那个人。</dd>" +
        "</dl>" +
      "</div></div>";
  }

  function openAgentForm(agentId) {
    var editing = null;
    (state.agents || []).forEach(function (a) {
      if (String(a.id) === String(agentId)) editing = a;
    });

    var body =
      '<form id="agentForm" autocomplete="off" data-agent-id="' + (editing ? esc(editing.id) : "") + '">' +
        '<div class="grid-2">' +
          '<div class="field">' +
            "<label>名字</label>" +
            '<input class="input" id="agName" placeholder="例如：小助手、客服阿May" value="' + esc(editing ? editing.name : "") + '">' +
            '<div class="hint">问它「你是谁」时会用这个名字自称。</div>' +
          "</div>" +
          '<div class="field">' +
            "<label>模型</label>" +
            '<div class="row">' +
              '<input class="input grow" id="agModel" list="agModelList" placeholder="deepseek-chat" value="' + esc(editing ? editing.model : "") + '">' +
              '<button class="btn sm" type="button" data-act="agent-models">拉取</button>' +
            "</div>" +
            '<datalist id="agModelList"></datalist>' +
            '<div class="hint">拉取不到也能手动填，名字对就行。</div>' +
          "</div>" +
        "</div>" +
        '<div class="field">' +
          "<label>接口地址</label>" +
          '<input class="input mono" id="agBaseUrl" placeholder="https://api.deepseek.com/v1" value="' + esc(editing ? editing.base_url : "") + '">' +
          '<div class="hint">任何 OpenAI 兼容接口都行，填到 /v1 为止。</div>' +
        "</div>" +
        '<div class="field">' +
          "<label>API Key</label>" +
          '<input class="input mono" id="agApiKey" type="password" placeholder="' +
            (editing && editing.key_set ? "已保存，留空则不修改" : "sk-...") + '">' +
          '<div class="hint">Key 只存在你这台机器上，接口返回时永远不下发原文。</div>' +
        "</div>" +
        '<div class="field">' +
          "<label>系统提示词</label>" +
          '<textarea class="textarea" id="agPrompt" rows="3" placeholder="你叫小助手，说话简短、直接。不聊政治，不确定的事先说不知道。">' + esc(editing ? editing.system_prompt : "") + "</textarea>" +
          '<div class="hint">这是「它是谁」的说明书。留空就用默认的通用助手设定。</div>' +
        "</div>" +
        '<div class="grid-3">' +
          '<div class="field">' +
            "<label>温度</label>" +
            '<input class="input" id="agTemp" type="number" step="0.1" min="0" max="2" value="' + esc(editing ? editing.temperature : "0.7") + '">' +
            '<div class="hint">0 最稳、2 最飘。</div>' +
          "</div>" +
          '<div class="field">' +
            "<label>最大回复 tokens</label>" +
            '<input class="input" id="agMaxTokens" type="number" min="64" max="8192" value="' + esc(editing ? editing.max_tokens : "1600") + '">' +
            '<div class="hint">太短会说到一半被切。8192 是多数模型的安全值，超长回复会自动分段发出。</div>' +
          "</div>" +
          '<div class="field">' +
            "<label>记住几轮上下文</label>" +
            '<input class="input" id="agHistory" type="number" min="0" max="40" value="' + esc(editing ? editing.history_len : "12") + '">' +
            '<div class="hint">0 表示每句都当新话题。</div>' +
          "</div>" +
        "</div>" +
        '<div class="grid-2">' +
          '<div class="field">' +
            "<label>最短回复间隔（秒）</label>" +
            '<input class="input" id="agInterval" type="number" min="0" max="60" value="' + esc(editing ? editing.min_interval : "3") + '">' +
            '<div class="hint">防止群里刷屏或被连续消息打断。</div>' +
          "</div>" +
          '<div class="field">' +
            "<label>单独代理（可选）</label>" +
            '<input class="input mono" id="agProxy" placeholder="留空跟随全局" value="' + esc(editing ? editing.proxy : "") + '">' +
            '<div class="hint">AI 接口和 Telegram 走的网络不一样时用。</div>' +
          "</div>" +
        "</div>" +
        '<div class="field">' +
          '<label class="switch">' +
            '<input type="checkbox" id="agVision"' + (editing && editing.vision ? " checked" : "") + ">" +
            '<span class="switch-track"></span>' +
            '<span class="switch-label">图片理解（能看用户发的图）</span>' +
          "</label>" +
          '<div class="hint">开了才会把图片传给它看。模型本身必须支持多模态（如 gpt-4o、qwen-vl、glm-4v），否则接口会报错；没开时收到图片会明确回复「看不了」，不会瞎猜内容。</div>' +
        "</div>" +
        '<div class="field">' +
          '<label class="switch">' +
            '<input type="checkbox" id="agEnabled"' + (!editing || editing.enabled ? " checked" : "") + ">" +
            '<span class="switch-track"></span>' +
            '<span class="switch-label">启用这个智能体</span>' +
          "</label>" +
        "</div>" +
      "</form>";

    openModal({
      title: editing ? "编辑智能体" : "新建智能体",
      wide: true,
      body: body,
      footer:
        '<button class="btn ghost" type="button" data-close>取消</button>' +
        (editing ? '<button class="btn" type="button" data-act="agent-test" data-id="' + esc(editing.id) + '">测试连通</button>' : "") +
        '<button class="btn primary" type="submit" form="agentForm" data-busy="保存中">' +
          (editing ? "保存修改" : "创建智能体") +
        "</button>",
      onMount: function (modal) {
        var form = $("#agentForm", modal.el);
        form.addEventListener("submit", async function (event) {
          event.preventDefault();
          var btn = $('button[type="submit"]', modal.el);
          var payload = {
            name: ($("#agName", modal.el).value || "").trim(),
            base_url: ($("#agBaseUrl", modal.el).value || "").trim(),
            model: ($("#agModel", modal.el).value || "").trim(),
            api_key: ($("#agApiKey", modal.el).value || "").trim(),
            system_prompt: ($("#agPrompt", modal.el).value || "").trim(),
            temperature: $("#agTemp", modal.el).value,
            max_tokens: $("#agMaxTokens", modal.el).value,
            history_len: $("#agHistory", modal.el).value,
            min_interval: $("#agInterval", modal.el).value,
            proxy: ($("#agProxy", modal.el).value || "").trim(),
            vision: $("#agVision", modal.el).checked,
            enabled: $("#agEnabled", modal.el).checked
          };
          if (!payload.name) { toast("给智能体起个名字", "err"); $("#agName", modal.el).focus(); return; }
          if (!payload.base_url) { toast("填一下接口地址", "err"); $("#agBaseUrl", modal.el).focus(); return; }
          if (!payload.model) { toast("填一下模型名", "err"); $("#agModel", modal.el).focus(); return; }
          if (!editing && !payload.api_key) { toast("填一下 API Key", "err"); $("#agApiKey", modal.el).focus(); return; }

          busy(btn, true);
          try {
            if (editing) await api("/api/ai/agents/" + editing.id, { method: "PUT", body: payload });
            else await api("/api/ai/agents", { method: "POST", body: payload });
            modal.close();
            toast(editing ? "已保存" : "智能体已创建", "ok");
            await refreshAll();
            if (state.tab === "agents") renderAgents();
          } catch (err) {
            toast(err.message, "err");
          } finally {
            busy(btn, false);
          }
        });
      }
    });
  }

  async function pullModels(btn) {
    var host = btn.closest(".modal");
    if (!host) return;
    var form = $("#agentForm", host);
    var agentId = form ? form.dataset.agentId : "";

    var payload = {
      base_url: ($("#agBaseUrl", host).value || "").trim(),
      api_key: ($("#agApiKey", host).value || "").trim(),
      proxy: ($("#agProxy", host).value || "").trim()
    };
    if (agentId) payload.agent_id = agentId;

    busy(btn, true);
    try {
      var data = await api("/api/ai/models", { method: "POST", body: payload });
      var list = data.models || [];
      var holder = $("#agModelList", host);
      if (holder) {
        holder.innerHTML = list.map(function (m) {
          return '<option value="' + esc(m) + '"></option>';
        }).join("");
      }
      var current = ($("#agModel", host).value || "").trim();
      if (!current && list.length) $("#agModel", host).value = list[0];
      toast("拿到 " + list.length + " 个模型，点模型框就能选", "ok");
    } catch (err) {
      toast(err.message, "err");
    } finally {
      busy(btn, false);
    }
  }

  async function testAgent(btn) {
    var id = btn.dataset.id;
    busy(btn, true);
    try {
      var data = await api("/api/ai/agents/" + id + "/test", { method: "POST" });
      toast("模型可用：" + (data.reply || "OK") + " · " + data.ms + "ms", "ok");
    } catch (err) {
      toast(err.message, "err");
    } finally {
      busy(btn, false);
    }
  }

  async function setDefaultAgent(btn) {
    var id = btn.dataset.id;
    busy(btn, true);
    try {
      await api("/api/ai/agents/" + id + "/default", { method: "POST" });
      toast("已设为默认智能体", "ok");
      await refreshAll();
      renderAgents();
    } catch (err) {
      toast(err.message, "err");
    } finally {
      busy(btn, false);
    }
  }

  async function deleteAgent(btn) {
    var id = btn.dataset.id;
    var name = btn.dataset.name || ("#" + id);
    var ok = await confirmDialog({
      title: "删除智能体「" + name + "」？",
      message: "会一并删掉这个智能体记录下的长期记忆。绑定了它的机器人会退回到「跟随默认」。",
      okText: "删除",
      danger: true
    });
    if (!ok) return;
    busy(btn, true);
    try {
      await api("/api/ai/agents/" + id, { method: "DELETE" });
      toast("已删除", "ok");
      await refreshAll();
      renderAgents();
    } catch (err) {
      toast(err.message, "err");
    } finally {
      busy(btn, false);
    }
  }

  /* ======================================================================
     10. 记忆页
     ====================================================================== */

  function renderMemorySelect() {
    var select = $("#memAgent");
    if (!select) return;
    var list = state.agents || [];
    if (!list.length) {
      select.innerHTML = '<option value="">没有智能体</option>';
      state.memAgent = "";
      return;
    }
    var wanted = state.memAgent || state.agentId || String(list[0].id);
    var exists = list.some(function (a) { return String(a.id) === String(wanted); });
    if (!exists) wanted = String(list[0].id);
    state.memAgent = wanted;

    select.innerHTML = list.map(function (a) {
      var selected = String(a.id) === String(wanted) ? " selected" : "";
      return '<option value="' + esc(a.id) + '"' + selected + ">" + esc(a.name) + "</option>";
    }).join("");
  }

  async function loadMemory() {
    var box = $("#memoryList");
    if (!box) return;

    var agentId = state.memAgent || ($("#memAgent") ? $("#memAgent").value : "");
    if (!agentId) {
      box.innerHTML = emptyState("还没有智能体", "记忆是挂在智能体上的。先去「智能体」建一个，再回来配记忆。",
        '<button class="btn primary sm" type="button" data-act="go" data-tab="agents">去创建</button>');
      return;
    }
    state.memAgent = agentId;

    box.innerHTML = '<p class="dim small mb-0">加载中…</p>';
    var query = "/api/memory?agent_id=" + encodeURIComponent(agentId);
    if (state.memScope) query += "&scope=" + encodeURIComponent(state.memScope);
    var data = await api(query);
    var notes = data.notes || [];
    var stats = data.stats || {};
    updateMemTabs(stats);

    if (!notes.length) {
      box.innerHTML = emptyState(
        state.memScope ? "这个分类下还没有记忆" : "还没有记忆",
        "聊天时它会在后台自动提炼稳定的事实，比如称呼、身份、喜好、约定好的事。你也可以手动补一条更准的。",
        '<button class="btn primary sm" type="button" data-act="note-form">手动添加</button>'
      );
      return;
    }

    box.innerHTML = '<div class="note-grid">' + notes.map(function (note) {
      var scope = SCOPE_LABELS[note.scope] || note.scope;
      var source = note.source === "manual" ? "手动" : "自动";
      var used = note.used_at ? "用过 " + relTime(note.used_at) : "没用过";
      return (
        '<div class="note">' +
          '<div class="note-top">' +
            '<span class="badge brand">' + esc(scope) + "</span>" +
            (note.scope_key ? '<span class="badge">' + esc(note.scope_key) + "</span>" : "") +
          "</div>" +
          '<div class="note-text">' + esc(note.content) + "</div>" +
          '<div class="note-foot">' +
            "<span>" + esc(source + " · " + fmtTime(note.created_at) + " · " + used) + "</span>" +
            '<button class="note-del" type="button" data-act="note-del" data-id="' + esc(note.id) + '">删除</button>' +
          "</div>" +
        "</div>"
      );
    }).join("") + "</div>";
  }

  function updateMemTabs(stats) {
    var map = { "": stats.total || 0, user: stats.user || 0, chat: stats.chat || 0, global: stats.global || 0 };
    $$("#memTabs .tab").forEach(function (tab) {
      if (!tab.dataset.label) tab.dataset.label = tab.textContent.trim();
      var scope = tab.dataset.scope || "";
      var count = map[scope] || 0;
      tab.textContent = tab.dataset.label + (count ? " " + count : "");
    });
  }

  function setMemScope(scope) {
    state.memScope = scope || "";
    $$("#memTabs .tab").forEach(function (tab) {
      tab.classList.toggle("on", (tab.dataset.scope || "") === state.memScope);
    });
    loadMemory().catch(function (err) { toast(err.message, "err"); });
  }

  function openNoteForm() {
    var agentId = state.memAgent || ($("#memAgent") ? $("#memAgent").value : "");
    if (!agentId) {
      toast("先建一个智能体，记忆才有归属", "err");
      return;
    }

    var body =
      '<form id="noteForm" autocomplete="off">' +
        '<div class="field">' +
          "<label>记给谁</label>" +
          '<select class="select" id="noteScope">' +
            '<option value="global">通用 —— 所有会话都参考</option>' +
            '<option value="user">某个人 —— 只在这个人说话时用</option>' +
            '<option value="chat">某个会话 —— 只在这个群里用</option>' +
          "</select>" +
        "</div>" +
        '<div class="field hidden" id="noteKeyField">' +
          "<label>会话 ID 或对方名字</label>" +
          '<input class="input" id="noteKey" placeholder="例如 群 ID -1001234567890，或对方昵称">' +
          '<div class="hint">写得越具体，越不容易串到别人身上。</div>' +
        "</div>" +
        '<div class="field">' +
          "<label>记住什么</label>" +
          '<textarea class="textarea" id="noteContent" rows="4" placeholder="他叫小王，在杭州做后端，喜欢先看结论。"></textarea>' +
          '<div class="hint">一条一件事，别超过 300 字。太长的拆成几条，命中率更高。</div>' +
        "</div>" +
      "</form>";

    openModal({
      title: "添加一条记忆",
      body: body,
      footer:
        '<button class="btn ghost" type="button" data-close>取消</button>' +
        '<button class="btn primary" type="submit" form="noteForm" data-busy="保存中">保存</button>',
      onMount: function (modal) {
        var scopeSelect = $("#noteScope", modal.el);
        var keyField = $("#noteKeyField", modal.el);
        scopeSelect.addEventListener("change", function () {
          keyField.classList.toggle("hidden", scopeSelect.value === "global");
        });

        $("#noteForm", modal.el).addEventListener("submit", async function (event) {
          event.preventDefault();
          var btn = $('button[type="submit"]', modal.el);
          var content = ($("#noteContent", modal.el).value || "").trim();
          var scope = scopeSelect.value;
          var key = ($("#noteKey", modal.el).value || "").trim();
          if (!content) { toast("写点内容再保存", "err"); $("#noteContent", modal.el).focus(); return; }
          if (scope !== "global" && !key) {
            toast(scope === "user" ? "填一下对方是谁" : "填一下会话 ID", "err");
            $("#noteKey", modal.el).focus();
            return;
          }
          busy(btn, true);
          try {
            await api("/api/memory", {
              method: "POST",
              body: { agent_id: agentId, content: content, scope: scope, scope_key: key }
            });
            modal.close();
            toast("记下了", "ok");
            await loadMemory();
          } catch (err) {
            toast(err.message, "err");
          } finally {
            busy(btn, false);
          }
        });
      }
    });
  }

  async function deleteNote(btn) {
    var id = btn.dataset.id;
    var ok = await confirmDialog({
      title: "删掉这条记忆？",
      message: "删掉之后，模型不会再参考它。下次聊天如果又提到，可能会重新提炼出来。",
      okText: "删除",
      danger: true
    });
    if (!ok) return;
    try {
      await api("/api/memory/" + id, { method: "DELETE" });
      toast("已删除", "ok");
      await loadMemory();
    } catch (err) {
      toast(err.message, "err");
    }
  }

  /* ======================================================================
     11. 设置页
     ====================================================================== */

  function fillSettings() {
    var s = state.settings || {};
    if ($("#setPassword")) $("#setPassword").value = "";
    if ($("#setTimezone")) $("#setTimezone").value = s.timezone || "Asia/Shanghai";
    if ($("#setMemEnabled")) $("#setMemEnabled").checked = String(s.memory_enabled) === "1";
    if ($("#setMemLearn")) $("#setMemLearn").checked = String(s.memory_learn) === "1";
    if ($("#setMemMax")) $("#setMemMax").value = s.memory_max_notes || "8";
    if ($("#setToolsEnabled")) $("#setToolsEnabled").checked = String(s.tools_enabled) === "1";
    if ($("#setToolsReminders")) $("#setToolsReminders").checked = String(s.tools_reminders) === "1";
    if ($("#setToolsSearch")) $("#setToolsSearch").checked = String(s.tools_web_search) === "1";
    if ($("#setToolsRename")) $("#setToolsRename").checked = String(s.tools_rename) === "1";
    if ($("#setOwnerId")) $("#setOwnerId").value = s.owner_id || "";
    var toolsWarn = $("#toolsWarn");
    if (toolsWarn) {
      toolsWarn.textContent = s.owner_id
        ? ""
        : "还没填管理员 ID，改名功能不会生效";
    }
    var warn = $("#pwdWarn");
    if (warn) {
      warn.textContent = s.using_default_password
        ? "当前还在用默认密码 admin123，建议立刻改掉"
        : (s.password_set ? "已设置自定义密码" : "还没设置面板密码");
    }
    if ($("#diagResult") && !state.lastDiag) {
      $("#diagResult").innerHTML = '<p class="dim small mb-0">还没检测。点右下角「开始检测」看看能不能连上 Telegram。</p>';
    } else if ($("#diagResult") && state.lastDiag) {
      renderDiagnose(state.lastDiag);
    }
  }

  async function savePanelSettings(btn) {
    var password = ($("#setPassword").value || "").trim();
    var timezone = ($("#setTimezone").value || "").trim();
    var payload = { timezone: timezone || "Asia/Shanghai" };
    if (password) {
      if (password.length < 8) {
        toast("密码至少 8 位", "err");
        $("#setPassword").focus();
        return;
      }
      payload.panel_password = password;
    }
    busy(btn, true);
    try {
      await api("/api/settings", { method: "PUT", body: payload });
      if (password) {
        toast("密码已更新，请用新密码重新登录", "ok");
        setTimeout(function () { showLogin("密码已更新，请重新登录"); }, 700);
        return;
      }
      toast("已保存", "ok");
      await loadAll();
    } catch (err) {
      toast(err.message, "err");
    } finally {
      busy(btn, false);
    }
  }

  async function saveMemorySettings(btn) {
    var payload = {
      memory_enabled: $("#setMemEnabled").checked ? "1" : "0",
      memory_learn: $("#setMemLearn").checked ? "1" : "0",
      memory_max_notes: $("#setMemMax").value || "8"
    };
    busy(btn, true);
    try {
      await api("/api/settings", { method: "PUT", body: payload });
      toast("记忆设置已保存", "ok");
      var data = await api("/api/bootstrap");
      applyBootstrap(data);
    } catch (err) {
      toast(err.message, "err");
    } finally {
      busy(btn, false);
    }
  }

  async function saveToolsSettings(btn) {
    var owner = ($("#setOwnerId").value || "").trim().replace(/^@/, "");
    if (owner && !/^\d+$/.test(owner)) {
      toast("管理员要填纯数字 ID，不是 @用户名。给它发 /id 就能看到", "err");
      $("#setOwnerId").focus();
      return;
    }
    if (!owner && $("#setToolsRename").checked) {
      // 不拦，但要让人明白为什么改名没反应——这是最常见的「明明开了却不能用」
      toast("没填管理员 ID，改名功能仍然不会生效", "warn");
    }
    var payload = {
      tools_enabled: $("#setToolsEnabled").checked ? "1" : "0",
      tools_reminders: $("#setToolsReminders").checked ? "1" : "0",
      tools_web_search: $("#setToolsSearch").checked ? "1" : "0",
      tools_rename: $("#setToolsRename").checked ? "1" : "0",
      owner_id: owner
    };
    busy(btn, true);
    try {
      await api("/api/settings", { method: "PUT", body: payload });
      toast("能力设置已保存", "ok");
      var data = await api("/api/bootstrap");
      applyBootstrap(data);
      await loadReminders();
    } catch (err) {
      toast(err.message, "err");
    } finally {
      busy(btn, false);
    }
  }

  /* 提醒列表。时间一律用后端算好的 due_local 显示，
     前端不碰 UTC，省得跟浏览器时区打架。 */

  var REM_STATUS = {
    pending: { label: "待发", cls: "brand" },
    done: { label: "已发", cls: "ok" },
    failed: { label: "失败", cls: "err" },
    canceled: { label: "已取消", cls: "" }
  };

  async function loadReminders() {
    var box = $("#reminderList");
    if (!box) return;
    box.innerHTML = '<p class="dim small mb-0">加载中…</p>';
    var status = state.remStatus || "pending";
    var data;
    try {
      data = await api("/api/reminders?status=" + encodeURIComponent(status) + "&limit=100");
    } catch (err) {
      box.innerHTML = '<p class="dim small mb-0">' + esc(err.message) + "</p>";
      return;
    }
    renderReminders(data, status);
  }

  function renderReminders(data, status) {
    var box = $("#reminderList");
    if (!box) return;
    var items = data.items || [];
    var sch = data.scheduler || {};

    var hint = $("#remHint");
    if (hint) {
      var bits = [];
      if (sch.running) {
        bits.push("调度器运行中，每 " + num(sch.interval, 20) + " 秒扫一次");
      } else if (state.standby) {
        bits.push("当前是值守模式，调度器不启动（避免和主实例重复发）");
      } else {
        bits.push("调度器未运行");
      }
      bits.push("待发 " + num(sch.pending, 0) + " 条");
      hint.textContent = bits.join(" · ");
    }

    if (!items.length) {
      var emptyText = status === "pending"
        ? "还没有待发的提醒。在聊天里说「20 分钟后提醒我关火」就会出现在这里。"
        : (status === "done"
            ? "还没有发过的提醒。"
            : (status === "failed" ? "没有失败的提醒——挺好。" : "一条提醒都没有。"));
      box.innerHTML = emptyState("这里空着", emptyText);
      return;
    }

    box.innerHTML = '<div class="row-list">' + items.map(function (item) {
      var meta = REM_STATUS[item.status] || { label: item.status || "未知", cls: "" };
      var when = item.due_local || item.due_exact || "";
      var sub = [];
      if (item.chat_title) sub.push(item.chat_title);
      if (item.author) sub.push("提的：" + item.author);
      if (item.status === "failed" && item.last_error) sub.push("报错：" + truncate(item.last_error, 40));
      if (item.status === "done" && item.fired_at) sub.push("发出于 " + fmtTime(item.fired_at));
      if (item.tries && item.status !== "done") sub.push("试过 " + num(item.tries, 0) + " 次");

      return (
        '<div class="row-item">' +
          '<div class="avatar' + (item.status === "done" ? " ok" : (item.status === "failed" ? " warn" : "")) + '">' +
            esc(firstChar(item.text || "提")) + "</div>" +
          '<div class="grow">' +
            '<div class="title">' + esc(item.text || "（空内容）") +
              '<span class="badge ' + meta.cls + '">' + esc(meta.label) + "</span>" +
            "</div>" +
            '<div class="meta">' + esc(sub.length ? sub.join(" · ") : when) + "</div>" +
          "</div>" +
          '<div class="actions">' +
            '<span class="dim small" style="margin-right:2px">' + esc(when) + "</span>" +
            '<button class="btn sm ghost" type="button" data-act="rem-del" data-id="' + esc(item.id) +
              '" data-text="' + esc(item.text || "") + '">删除</button>' +
          "</div>" +
        "</div>"
      );
    }).join("") + "</div>";
  }

  function setRemStatus(status) {
    state.remStatus = status || "";
    $$("#remTabs .tab").forEach(function (tab) {
      tab.classList.toggle("on", (tab.dataset.status || "") === state.remStatus);
    });
    loadReminders().catch(function (err) { toast(err.message, "err"); });
  }

  async function deleteReminder(btn) {
    var id = btn.dataset.id;
    var ok = await confirmDialog({
      title: "删掉这条提醒？",
      message: "「" + (btn.dataset.text || "") + "」删掉后到点就不会再发。",
      okText: "删除",
      danger: true
    });
    if (!ok) return;
    try {
      await api("/api/reminders/" + encodeURIComponent(id), { method: "DELETE" });
      toast("已删除", "ok");
      await loadReminders();
    } catch (err) {
      toast(err.message, "err");
    }
  }

  async function clearMessages(btn) {
    var ok = await confirmDialog({
      title: "清空对话记录？",
      message: "会删掉所有机器人的历史消息，模型之后不再记得前文。智能体配置和长期记忆都不受影响。",
      okText: "清空",
      danger: true
    });
    if (!ok) return;
    busy(btn, true);
    try {
      await api("/api/bot/messages", { method: "DELETE" });
      await loadMessages(12);
      renderDashboard();
      toast("已清空", "ok");
    } catch (err) {
      toast(err.message, "err");
    } finally {
      busy(btn, false);
    }
  }

  /* ======================================================================
     12. 在线升级
     ====================================================================== */

  // 仓库地址不是写死的：升级源可以换成自己的 fork（UPDATE_REPO 环境变量），
  // 卡片上的链接必须跟着变，否则会把用户带到官方仓库去。
  function syncRepoLinks(repo) {
    if (!repo) return;
    var slug = String(repo).trim();
    if (!/^[\w.-]+\/[\w.-]+$/.test(slug)) return;  // 不是 owner/name 就别拼链接
    var web = "https://github.com/" + slug;
    var pairs = [
      ["aboutRepo", web],
      ["aboutIssues", web + "/issues"],
      ["aboutOpen", web]
    ];
    pairs.forEach(function (pair) {
      var node = $("#" + pair[0]);
      if (node) node.setAttribute("href", pair[1]);
    });
    var label = $("#aboutRepo");
    if (label) label.textContent = "github.com/" + slug;
    var clone = $("#aboutClone");
    if (clone) clone.textContent = web + ".git";
    // 复制按钮的 data-copy 在 HTML 里写死，换仓库时要同步
    var copyBtn = document.querySelector('[data-act="copy"][data-clone-target]');
    if (copyBtn) copyBtn.dataset.copy = web + ".git";
  }

  async function loadUpdateStatus() {
    try {
      var data = await api("/api/update/status");
      state.update = data;
      // 单看 v1.0.0 永远不变，会让人以为没升级。把在跑的 commit 直接并进版本号。
      text("updVersion", (data.short || "镜像内置") + (data.version ? " · v" + data.version : ""));
      if (data.pending_restart) {
        text("updCommit", (data.short || "镜像内置") + "（已下载 " + data.installed_short + "，待重启）");
      } else {
        text("updCommit", data.short ? data.short + (data.installed_at ? "（" + data.installed_at + " 升级）" : "") : "镜像内置版本");
      }
      text("updRepo", data.repo + " @ " + data.branch);
      syncRepoLinks(data.repo);
      text("updCheckedAt", data.last_check ? "上次检查 " + data.last_check : "");
      // 代码下了但没重启，说明上次升级卡在重启环节，提示手动处理
      if (data.pending_restart) {
        var box = $("#updateResult");
        if (box && !box.innerHTML.trim()) {
          box.innerHTML = notice("warn", "有代码待重启生效",
            "已下载 " + data.installed_short + "，但当前仍运行 " + (data.short || "镜像内置") +
            "。重启容器即可生效。");
        }
      }
    } catch (err) {
      console.warn("升级状态加载失败", err);
    }
  }

  // 进入设置页时自动比对一次。
  // 之前只读 /api/update/status（本地缓存的状态），从不问 GitHub，所以卡片上
  // 永远只有「当前版本」，得用户自己想起来点按钮才知道有没有新版本 —— 看起来
  // 就是「检测不到新版本」。每次页面加载只自动查一次，切回标签不重复打扰。
  var updateAutoChecked = false;

  async function autoCheckUpdate() {
    if (updateAutoChecked) return;
    updateAutoChecked = true;
    var box = $("#updateResult");
    if (box) box.innerHTML = '<p class="dim small mb-0">正在检查更新…</p>';
    try {
      var data = await api("/api/update/check");
      renderUpdateCheck(data);
      text("updCheckedAt", "上次检查 " + (data.checked_at || ""));
    } catch (err) {
      // 自动检查失败不弹错误：可能是暂时断网，点按钮重试就行
      if (box) box.innerHTML = '<p class="dim small mb-0">自动检查失败，可点「检查更新」重试</p>';
    }
  }

  async function checkUpdate(btn) {
    if (btn) busy(btn, true);
    var box = $("#updateResult");
    if (box) box.innerHTML = '<p class="dim small mb-0">正在连接 GitHub…</p>';
    try {
      var data = await api("/api/update/check");
      renderUpdateCheck(data);
      text("updCheckedAt", "上次检查 " + (data.checked_at || ""));
    } catch (err) {
      if (box) box.innerHTML = notice("err", "检查失败", err.message);
    } finally {
      if (btn) busy(btn, false);
    }
  }

  function renderUpdateCheck(data) {
    var box = $("#updateResult");
    if (!box) return;
    if (!data.ok) {
      box.innerHTML = notice("err", data.error || "检查失败", data.hint || "确认容器能访问外网。");
      return;
    }
    var parts = [];
    if (data.pending_restart) {
      parts.push(notice("warn", "代码已下载，待重启",
        "最新代码 " + data.installed_short + " 已在本地，但当前仍运行 " +
        (data.current_short || "镜像内置") + "。点「立即升级」或重启容器即可生效。"));
    } else if (data.has_update) {
      parts.push(notice("warn", "发现新版本",
        "线上最新 " + data.remote_commit + "，当前 " + (data.current_short || "镜像内置") +
        (data.remote_message ? "。最新改动：" + data.remote_message : "")));
    } else {
      parts.push(notice("ok", "已是最新", data.note || ("线上最新 " + data.remote_commit + "，与当前一致。")));
    }
    box.innerHTML = parts.join("");
  }

  async function applyUpdate(btn) {
    var ok = await confirmDialog({
      title: "升级到最新版本？",
      message: "会从 GitHub 下载最新代码并重启服务，中断几秒。数据库、机器人配置、智能体、记忆都保留。",
      okText: "升级"
    });
    if (!ok) return;
    if (btn) busy(btn, true);
    var box = $("#updateResult");
    if (box) box.innerHTML = '<p class="dim small mb-0">正在下载代码…（大文件可能要几十秒）</p>';
    var data;
    try {
      // 必须显式 POST：后端这条路由只收 POST，默认 GET 会拿到 405。
      // 之前漏了这个参数，405 被下面的 catch 当成「重启断连」，界面显示
      // 「升级完成」而代码一个字节都没换 —— 就是「能检测不能升级」的元凶。
      data = await api("/api/update/apply", { method: "POST" });
    } catch (err) {
      // 只有「服务已经在重启、连接被掐断」才算成功。别的错误必须如实报出来，
      // 否则用户以为升了级，实际还在跑旧代码。
      if (err && err.status === 0) {
        if (box) box.innerHTML = notice("ok", "正在重启", "服务重启中，稍等几秒后页面自动刷新。");
        waitForRestart(null);
      } else if (box) {
        box.innerHTML = notice("err", "升级失败", (err && err.message) || "请求被拒绝。");
        if (btn) busy(btn, false);
      }
      return;
    }
    if (!data.ok) {
      if (box) box.innerHTML = notice("err", data.error || "升级失败", data.hint || "");
      if (btn) busy(btn, false);
      return;
    }
    if (data.already_latest) {
      if (box) box.innerHTML = notice("ok", "无需升级", data.message || "已经是最新版本。");
      if (btn) busy(btn, false);
      return;
    }
    if (box) box.innerHTML = notice("ok", data.needs_restart ? "代码已就绪" : "下载完成",
      data.message || "服务正在重启，页面会自动刷新。");
    waitForRestart(data.commit || null, btn);
  }

  // 等重启完成，并核对「重启后跑的是不是新版本」。
  // 只等 /health 变 ok 是不够的：进程可能压根没重启（execv 失败退化成退出），
  // 服务一直在跑，health 一直 ok，界面就会立刻判定成功并刷新，版本却没变。
  function waitForRestart(expectShort, btn) {
    var tries = 0;
    var seenDown = false;
    var timer = setInterval(function () {
      tries++;
      fetch("/health", { cache: "no-store" })
        .then(function (res) { return res.ok ? res.json() : null; })
        .then(function (data) {
          if (!data || data.status !== "ok") return;
          // 第一次成功要先确认服务确实中断过，否则是旧进程在应答
          if (!seenDown && tries < 3) return;
          clearInterval(timer);
          verifyUpgrade(expectShort, btn);
        })
        .catch(function () { seenDown = true; /* 连接断了，说明确实在重启 */ });
      if (tries > 40) {
        clearInterval(timer);
        toast("重启超时，请手动刷新页面", "err");
        if (btn) busy(btn, false);
      }
    }, 1500);
  }

  // 重启回来后核对「新代码是否真的加载了」。
  // 只等 /health 变 ok 是不够的：execv 可能压根没执行，旧进程一直在应答，
  // health 永远 ok，界面就会立刻判定成功并刷新 —— 版本却没变。
  // 用后端的 pending_restart 判断：「已下载的 commit」和「进程里的 commit」
  // 是否一致。字符串直接比会踩空值坑（镜像内置版本 running 为空串）。
  async function verifyUpgrade(expectShort, btn) {
    var s = null;
    try {
      s = await api("/api/update/status");
    } catch (err) {
      s = null;
    }
    var box = $("#updateResult");
    if (s && s.pending_restart) {
      if (box) {
        box.innerHTML = notice("err", "升级未生效",
          "新代码已下载（" + (s.installed_short || "") + "），但服务没有重启，" +
          "仍在运行 " + (s.short || "镜像内置版本") + "。请手动重启容器，或再点一次「立即升级」。");
      }
      if (btn) busy(btn, false);
      return;
    }
    var running = s ? (s.short || "").slice(0, 8) : "";
    var expect = (expectShort || "").slice(0, 8);
    if (expect && running !== expect) {
      if (box) {
        box.innerHTML = notice("err", "升级未生效",
          "目标版本 " + expect + "，重启后实际运行 " + (running || "镜像内置版本") +
          "。请手动重启容器后再试。");
      }
      if (btn) busy(btn, false);
      return;
    }
    // 成功不再整页刷新：刷新会把这条「升级完成」冲掉，用户只看到页面闪一下。
    // 就地更新版本行，让人明确看到新版本号。
    if (box) {
      box.innerHTML = notice("ok", "升级完成",
        "已切换到 " + (running || expect) + "，当前就是这个版本在跑。");
    }
    if (btn) busy(btn, false);
    toast("升级完成", "ok");
    // 版本号那一行要跟着变，否则还显示旧值，看着像没升
    loadUpdateStatus();
  }

  /* ======================================================================
     13. WebDAV 备份
     ====================================================================== */

  async function loadBackupStatus() {
    try {
      var data = await api("/api/backup/status");
      state.backup = data;
      fillBackupForm(data);
    } catch (err) {
      console.warn("备份状态加载失败", err);
    }
  }

  function fillBackupForm(s) {
    if (!s) return;
    // 密码永远不回显：输入框只用来「填新的」，已有就提示已保存
    var pass = $("#bkPass");
    if (pass) {
      pass.value = "";
      pass.placeholder = s.has_password ? "已保存，留空不修改" : "应用密码";
    }
    if ($("#bkUrl")) $("#bkUrl").value = s.url || "";
    if ($("#bkUser")) $("#bkUser").value = s.user || "";
    if ($("#bkDir")) $("#bkDir").value = s.dir || "";
    if ($("#bkPath")) $("#bkPath").value = s.path ? s.path.split("/").pop() : "";
    if ($("#bkProxy")) $("#bkProxy").value = s.proxy || "";
    if ($("#bkAuto")) $("#bkAuto").checked = !!s.auto;
    if ($("#bkInterval")) $("#bkInterval").value = s.interval || 1440;

    text("bkLast", s.last_push_at || "还没备份过");
    text("bkRemote", s.path || "—");

    var badge = $("#bkStatus");
    if (badge) {
      if (s.configured) {
        badge.textContent = "已配置 · " + s.path;
        badge.className = "dim small";
      } else if (s.url || s.user) {
        badge.textContent = "还差地址或密码";
        badge.className = "dim small";
      } else {
        badge.textContent = "还没配置";
        badge.className = "dim small";
      }
    }
  }

  /* 取输入框的值；元素不在时返回空串，避免整段逻辑因一个 null 崩掉 */
  function val(id) {
    var node = document.getElementById(id);
    return node ? String(node.value || "") : "";
  }

  function backupPayload() {
    var body = {
      url: val("bkUrl").trim(),
      user: val("bkUser").trim(),
      dir: val("bkDir").trim(),
      path: val("bkPath").trim() || "tg-ai-bot-backup.json",
      auto: $("#bkAuto") && $("#bkAuto").checked ? "1" : "0",
      interval: val("bkInterval").trim() || "1440",
      proxy: val("bkProxy").trim()
    };
    var pass = val("bkPass").trim();
    if (pass) body.password = pass;  // 留空 = 不改
    return body;
  }

  async function saveBackupConfig(btn) {
    if (btn) busy(btn, true);
    try {
      var data = await api("/api/backup/config", { method: "PUT", body: backupPayload() });
      state.backup = data.status;
      fillBackupForm(data.status);
      toast("备份设置已保存", "ok");
      return data.status;
    } catch (err) {
      toast(err.message, "err");
      return null;
    } finally {
      if (btn) busy(btn, false);
    }
  }

  async function verifyBackup(btn) {
    if (btn) busy(btn, true);
    var box = $("#backupResult");
    if (box) box.innerHTML = '<p class="dim small mb-0">正在连接 WebDAV…</p>';
    try {
      // 先把当前输入存下来，否则用户刚粘贴的密码校验时会用旧值
      var saved = await saveBackupConfig(null);
      var data = await api("/api/backup/verify", { method: "POST" });
      if (!data.ok) {
        if (box) box.innerHTML = notice("err", data.error || "校验失败", data.hint || "");
        return;
      }
      var steps = (data.steps || []).map(function (s) {
        return esc(s.step);
      }).join(" → ");
      var bits = [];
      bits.push(notice("ok", "连接正常",
        "已确认能写入。" + (data.target ? "备份会存到 " + data.target : "")));
      if (steps) {
        bits.push('<p class="dim small mb-0">已通过：' + steps + "</p>");
      }
      if (box) box.innerHTML = bits.join("");
      if (saved) fillBackupForm(saved);
    } catch (err) {
      if (box) box.innerHTML = notice("err", "校验失败", err.message);
    } finally {
      if (btn) busy(btn, false);
    }
  }

  async function pushBackup(btn) {
    if (btn) busy(btn, true);
    var box = $("#backupResult");
    if (box) box.innerHTML = '<p class="dim small mb-0">正在打包并上传…</p>';
    try {
      await saveBackupConfig(null);
      var data = await api("/api/backup/push", { method: "POST" });
      if (!data.ok) {
        if (box) box.innerHTML = notice("err", data.error || "备份失败", data.hint || "");
        return;
      }
      var c = data.counts || {};
      if (box) {
        box.innerHTML = notice("ok", "备份完成",
          "已上传 " + kbSize(data.bytes) + " 到 " + esc(data.path) +
          "（" + (c.bots || 0) + " 个机器人、" + (c.agents || 0) + " 个智能体、" +
          (c.notes || 0) + " 条记忆）。");
      }
      await loadBackupStatus();
    } catch (err) {
      if (box) box.innerHTML = notice("err", "备份失败", err.message);
    } finally {
      if (btn) busy(btn, false);
    }
  }

  function kbSize(bytes) {
    var n = Number(bytes) || 0;
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / 1024 / 1024).toFixed(2) + " MB";
  }

  async function restoreBackup(btn) {
    if (btn) busy(btn, true);
    var box = $("#backupResult");
    try {
      await saveBackupConfig(null);
      var preview = await api("/api/backup/preview");
      if (!preview.ok) {
        if (box) box.innerHTML = notice("err", preview.error || "读取备份失败", preview.hint || "");
        return;
      }
      var c = preview.counts || {};
      var ok = await confirmDialog({
        title: "从备份恢复？",
        message: "备份时间 " + (preview.exported_at || "未知") + "，包含 " +
          (c.bots || 0) + " 个机器人、" + (c.agents || 0) + " 个智能体、" +
          (c.notes || 0) + " 条记忆。会覆盖同名的智能体配置，记忆只增不重。" +
          "备份里没有密钥，恢复后要重填 Bot Token 与 AI Key。",
        okText: "恢复"
      });
      if (!ok) return;
      var data = await api("/api/backup/restore", { method: "POST" });
      var a = data.applied || {};
      if (box) {
        box.innerHTML = notice("ok", "恢复完成",
          "配置 " + (a.settings || 0) + " 项、智能体 " + (a.agents || 0) +
          " 个、记忆 " + (a.notes || 0) + " 条。" + (data.message || ""));
      }
      toast("已恢复，正在刷新", "ok");
      setTimeout(function () { location.reload(); }, 1200);
    } catch (err) {
      if (box) box.innerHTML = notice("err", "恢复失败", err.message);
    } finally {
      if (btn) busy(btn, false);
    }
  }

  async function restoreBackup(btn) {
    if (btn) busy(btn, true);
    var box = $("#backupResult");
    try {
      await saveBackupConfig(null);
      var preview = await api("/api/backup/preview");
      if (!preview.ok) {
        if (box) box.innerHTML = notice("err", preview.error || "读取备份失败", preview.hint || "");
        return;
      }
      var c = preview.counts || {};
      var ok = await confirmDialog({
        title: "从备份恢复？",
        message: "备份时间 " + (preview.exported_at || "未知") + "，包含 " +
          (c.bots || 0) + " 个机器人、" + (c.agents || 0) + " 个智能体、" +
          (c.notes || 0) + " 条记忆。会覆盖同名的智能体配置，记忆只增不重。" +
          "备份里没有密钥，恢复后要重填 Bot Token 与 AI Key。",
        okText: "恢复"
      });
      if (!ok) return;
      var data = await api("/api/backup/restore", { method: "POST" });
      var a = data.applied || {};
      if (box) {
        box.innerHTML = notice("ok", "恢复完成",
          "配置 " + (a.settings || 0) + " 项、智能体 " + (a.agents || 0) +
          " 个、记忆 " + (a.notes || 0) + " 条。" + (data.message || ""));
      }
      toast("已恢复，正在刷新", "ok");
      setTimeout(function () { location.reload(); }, 1200);
    } catch (err) {
      if (box) box.innerHTML = notice("err", "恢复失败", err.message);
    } finally {
      if (btn) busy(btn, false);
    }
  }

  /* ======================================================================
     14. 出网诊断
     ====================================================================== */

  async function runDiagnose(btn) {
    if (btn) busy(btn, true);
    try {
      var data = await api("/api/bot/diagnose");
      state.lastDiag = data;
      renderDiagnose(data);
      toast(data.ok ? "出网正常" : "出网不通，看诊断详情", data.ok ? "ok" : "err");
    } catch (err) {
      toast(err.message, "err");
    } finally {
      if (btn) busy(btn, false);
    }
  }

  function probeBadge(probe) {
    if (!probe) return '<span class="badge">未测试</span>';
    if (probe.ok) {
      return '<span class="badge ok"><span class="dot"></span>通 ' + esc(probe.status) + " · " + esc(probe.ms) + "ms</span>";
    }
    return '<span class="badge err"><span class="dot"></span>不通</span>';
  }

  function renderDiagnose(data) {
    var box = $("#diagResult");
    if (!box) return;
    var rows = [
      ["出网地址", '<span class="code-inline">' + esc(data.api_root || "-") + "</span>"],
      ["直连", probeBadge(data.direct) + errHint(data.direct)],
      ["代理", data.proxy_configured ? probeBadge(data.proxied) + ' <span class="dim tiny">' + esc(data.proxy) + "</span>" : '<span class="dim">未配置，走直连</span>'],
      ["实际使用", data.effective === "proxy" ? "代理" : "直连"]
    ];
    box.innerHTML =
      '<dl class="kv mb-16">' +
        rows.map(function (row) { return "<dt>" + esc(row[0]) + "</dt><dd>" + row[1] + "</dd>"; }).join("") +
      "</dl>" +
      notice(data.ok ? "ok" : "err", data.ok ? "出网正常" : "出网有问题", data.hint || "");
  }

  function errHint(probe) {
    if (!probe || probe.ok || !probe.error) return "";
    return ' <span class="dim tiny">' + esc(truncate(probe.error, 90)) + "</span>";
  }

  /* ======================================================================
     13. 对话总览弹窗
     ====================================================================== */

  async function openMessages() {
    var modal = openModal({
      title: "最近对话",
      wide: true,
      body: '<p class="dim small mb-0">加载中…</p>'
    });
    try {
      var data = await api("/api/bot/messages?limit=200");
      var list = data.messages || [];
      var stats = data.stats || {};
      if (!list.length) {
        modal.body.innerHTML = emptyState("还没有对话", "机器人在私聊里收到消息、或在群里被 @ 之后，这里就能看到实际收发的内容。");
        return;
      }
      modal.body.innerHTML =
        '<p class="dim small mb-12">共 ' + esc(stats.total || 0) + " 条 · 今天 " + esc(stats.today || 0) +
        " 条 · " + esc(stats.chats || 0) + " 个会话</p>" +
        messageListHtml(list);
    } catch (err) {
      modal.body.innerHTML = '<p class="err-text">' + esc(err.message) + "</p>";
    }
  }

  /* ======================================================================
     14. 事件委托
     ====================================================================== */

  var ACTIONS = {
    go: function (btn) { go(btn.dataset.tab); },
    diagnose: runDiagnose,
    "bot-form": function () { openBotForm(null); },
    "bot-edit": function (btn) { openBotForm(btn.dataset.id); },
    "bot-restart": restartBot,
    "bot-toggle": toggleBot,
    "bot-del": deleteBot,
    "agent-form": function () { openAgentForm(null); },
    "agent-edit": function (btn) { openAgentForm(btn.dataset.id); },
    "agent-test": testAgent,
    "agent-default": setDefaultAgent,
    "agent-del": deleteAgent,
    "agent-models": pullModels,
    "note-form": openNoteForm,
    "note-del": deleteNote,
    "save-panel": savePanelSettings,
    "save-memory": saveMemorySettings,
    "save-tools": saveToolsSettings,
    "rem-tab": function (btn) { setRemStatus(btn.dataset.status || ""); },
    "rem-refresh": function () { loadReminders().catch(function (err) { toast(err.message, "err"); }); },
    "rem-del": deleteReminder,
    "update-check": checkUpdate,
    "update-apply": applyUpdate,
    "backup-push": pushBackup,
    "backup-verify": verifyBackup,
    "backup-restore": restoreBackup,
    "clear-messages": clearMessages,
    messages: openMessages,
    logout: logout,
    copy: function (btn) { copyText(btn.dataset.copy); }
  };

  function handleClick(event) {
    var navItem = event.target.closest(".nav-item[data-tab]");
    if (navItem) {
      go(navItem.dataset.tab);
      return;
    }
    var tab = event.target.closest(".tab[data-scope]");
    if (tab) {
      setMemScope(tab.dataset.scope || "");
      return;
    }
    var trigger = event.target.closest("[data-act]");
    if (!trigger) return;
    var fn = ACTIONS[trigger.dataset.act];
    if (!fn) return;
    event.preventDefault();
    fn(trigger, event);
  }

  function handleKeydown(event) {
    if (event.key === "Escape" && modals.length) {
      var top = modals[modals.length - 1];
      top.el.dispatchEvent(new CustomEvent("modal:esc"));
      top.close();
    }
  }

  function bindStaticEvents() {
    var form = $("#loginForm");
    if (form) form.addEventListener("submit", doLogin);

    var memAgent = $("#memAgent");
    if (memAgent) {
      memAgent.addEventListener("change", function () {
        state.memAgent = memAgent.value;
        loadMemory().catch(function (err) { toast(err.message, "err"); });
      });
    }

    document.addEventListener("click", handleClick);
    document.addEventListener("keydown", handleKeydown);
  }

  /* ======================================================================
     15. 启动
     ====================================================================== */

  async function boot() {
    bindStaticEvents();
    // 250ms 内能问出结果就完全不显示任何东西（局域网通常这么快），
    // 超过才亮遮罩，宁可转个圈也别闪登录框。
    bootSplashTimer = setTimeout(function () {
      show($("#bootSplash"), true);
    }, 250);
    try {
      await loadAll();
    } catch (err) {
      if (err.status === 401) {
        showLogin("");
      } else {
        showLogin(err.message);
      }
    }
  }

  // 方便在控制台里排查：所有内部状态都在这里
  window.App = {
    state: state,
    go: go,
    refresh: refreshAll,
    diagnose: function () { return api("/api/bot/diagnose"); }
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
