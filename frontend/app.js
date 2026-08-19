/* =========================================================================
 * Telegram → Cloudinary Migrator — dashboard client
 * Vanilla JS, no build step. Talks only to this backend; no credentials
 * ever reach the browser.
 * ========================================================================= */

(() => {
  "use strict";

  // ----------------------------------------------------------------- state --

  const state = {
    channels: [],
    chatKind: "all",
    search: "",
    chatId: null,
    chatTitle: "",
    typeFilter: "all",
    files: [],
    selected: new Set(),
    offsetId: 0,
    hasMore: false,
    loadingFiles: false,
    status: { state: "idle", active: false },
    wsRetry: 0,

    // auth
    unlocked: false,
    account: null,
    accounts: [],
    loginId: null,
    pane: "passcode",
    codeDeadline: null,
    cloudinaryDefaults: null,
    socket: null,
  };

  const ACTIVE_STATES = new Set(["scanning", "running", "paused", "stopping"]);

  const $ = (id) => document.getElementById(id);
  const el = {
    accountLine: $("accountLine"),
    accountName: $("accountName"),
    btnAccountMenu: $("btnAccountMenu"),
    accountMenu: $("accountMenu"),
    menuAccountList: $("menuAccountList"),
    btnAddAccount: $("btnAddAccount"),
    btnCloudinarySettings: $("btnCloudinarySettings"),
    btnSignOut: $("btnSignOut"),
    btnLock: $("btnLock"),
    authOverlay: $("authOverlay"),
    stepTrack: $("stepTrack"),
    stepSub: $("stepSub"),
    authError: $("authError"),
    inpPasscode: $("inpPasscode"),
    btnUnlock: $("btnUnlock"),
    knownAccounts: $("knownAccounts"),
    knownAccountList: $("knownAccountList"),
    inpPhone: $("inpPhone"),
    btnSendCode: $("btnSendCode"),
    maskedPhone: $("maskedPhone"),
    inpCode: $("inpCode"),
    codeTimer: $("codeTimer"),
    btnVerifyCode: $("btnVerifyCode"),
    btnBackToPhone: $("btnBackToPhone"),
    inpPassword: $("inpPassword"),
    passwordHint: $("passwordHint"),
    btnSubmitPassword: $("btnSubmitPassword"),
    inpCloudName: $("inpCloudName"),
    inpApiKey: $("inpApiKey"),
    inpApiSecret: $("inpApiSecret"),
    inpFolder: $("inpFolder"),
    inpFolderPerChat: $("inpFolderPerChat"),
    btnSaveCloudinary: $("btnSaveCloudinary"),
    btnSkipCloudinary: $("btnSkipCloudinary"),
    chatSearch: $("chatSearch"),
    chatList: $("chatList"),
    chatCount: $("chatCount"),
    refreshChats: $("refreshChats"),
    chatTitle: $("chatTitle"),
    chatMeta: $("chatMeta"),
    statusPill: $("statusPill"),
    statusText: $("statusText"),
    fileGrid: $("fileGrid"),
    fileHint: $("fileHint"),
    btnLoadMore: $("btnLoadMore"),
    btnSelectAll: $("btnSelectAll"),
    btnClear: $("btnClear"),
    btnSyncAll: $("btnSyncAll"),
    btnStart: $("btnStart"),
    btnPause: $("btnPause"),
    btnResume: $("btnResume"),
    btnStop: $("btnStop"),
    progressWrap: $("progressWrap"),
    progressFill: $("progressFill"),
    progressCounts: $("progressCounts"),
    progressCurrent: $("progressCurrent"),
    progressFailed: $("progressFailed"),
    selectionBar: $("selectionBar"),
    selectionCount: $("selectionCount"),
    selectionSize: $("selectionSize"),
    logView: $("logView"),
    autoScroll: $("autoScroll"),
    btnClearLog: $("btnClearLog"),
    wsDot: $("wsDot"),
    wsStatusText: $("wsStatusText"),
    storeCount: $("storeCount"),
  };

  // ---------------------------------------------------------------- helpers --

  function humanSize(bytes) {
    if (!bytes) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let value = bytes;
    let i = 0;
    while (value >= 1024 && i < units.length - 1) {
      value /= 1024;
      i += 1;
    }
    return `${i === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[i]}`;
  }

  function shortDate(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "";
    return d.toLocaleDateString(undefined, { day: "2-digit", month: "short", year: "numeric" });
  }

  function clockOf(iso) {
    const d = iso ? new Date(iso) : new Date();
    return d.toLocaleTimeString(undefined, { hour12: false });
  }

  function initials(name) {
    return (name || "?")
      .split(/\s+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((w) => w[0].toUpperCase())
      .join("");
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    let payload = null;
    try {
      payload = await response.json();
    } catch (_) {
      /* empty body */
    }
    if (!response.ok) {
      const detail = (payload && payload.detail) || `${response.status} ${response.statusText}`;
      const error = new Error(detail);
      error.status = response.status;
      error.code = payload && payload.code;
      // 401 = the dashboard locked out from under us (cookie expired, someone
      // hit Lock elsewhere). 428 = unlocked but no Telegram account bound.
      if (response.status === 401 && !path.startsWith("/api/auth/")) {
        handleSessionLost();
      } else if (response.status === 428 && error.code !== "no_cloudinary") {
        handleAccountLost();
      }
      throw error;
    }
    return payload;
  }

  // ------------------------------------------------------------------- log --

  function appendLog(level, message, extra) {
    const line = document.createElement("div");
    line.className = "log-line";
    line.dataset.level = level || "info";

    const time = document.createElement("span");
    time.className = "log-line__time";
    time.textContent = clockOf(extra && extra.ts);

    const text = document.createElement("span");
    text.className = "log-line__text";
    text.textContent = message;

    if (extra && extra.url) {
      text.append(" ");
      const link = document.createElement("a");
      link.href = extra.url;
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = "open";
      text.append(link);
    }

    line.append(time, text);
    el.logView.append(line);

    while (el.logView.childElementCount > 800) el.logView.firstElementChild.remove();
    if (el.autoScroll.checked) el.logView.scrollTop = el.logView.scrollHeight;
  }

  // -------------------------------------------------------------- channels --

  async function loadChannels() {
    el.chatList.innerHTML = '<p class="placeholder">Loading chats…</p>';
    try {
      const data = await api("/api/channels");
      state.channels = data.channels || [];
      renderChannels();
    } catch (error) {
      el.chatList.innerHTML = "";
      const p = document.createElement("p");
      p.className = "placeholder";
      p.textContent = `Couldn't load chats: ${error.message}`;
      el.chatList.append(p);
      appendLog("error", `Chat list failed: ${error.message}`);
    }
  }

  function visibleChannels() {
    const needle = state.search.trim().toLowerCase();
    return state.channels.filter((chat) => {
      if (state.chatKind !== "all" && chat.kind !== state.chatKind) return false;
      if (!needle) return true;
      return (
        chat.title.toLowerCase().includes(needle) ||
        (chat.username || "").toLowerCase().includes(needle)
      );
    });
  }

  function renderChannels() {
    const list = visibleChannels();
    el.chatList.innerHTML = "";

    if (!list.length) {
      const p = document.createElement("p");
      p.className = "placeholder";
      p.textContent = state.channels.length ? "No chats match that search." : "No chats found.";
      el.chatList.append(p);
    }

    for (const chat of list) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "chat-item" + (chat.id === state.chatId ? " is-active" : "");
      button.setAttribute("role", "option");
      button.setAttribute("aria-selected", String(chat.id === state.chatId));

      const avatar = document.createElement("span");
      avatar.className = "chat-item__avatar";
      avatar.textContent = initials(chat.title);

      const body = document.createElement("span");
      body.className = "chat-item__body";

      const name = document.createElement("span");
      name.className = "chat-item__name";
      name.textContent = chat.title;

      const sub = document.createElement("span");
      sub.className = "chat-item__sub";
      sub.textContent = chat.kind === "dm" ? "Direct" : chat.kind === "group" ? "Group" : "Channel";

      body.append(name, sub);
      button.append(avatar, body);

      if (chat.migrated_count) {
        const badge = document.createElement("span");
        badge.className = "chat-item__badge";
        badge.textContent = `${chat.migrated_count}`;
        badge.title = `${chat.migrated_count} file(s) already migrated`;
        button.append(badge);
      }

      button.addEventListener("click", () => selectChat(chat));
      el.chatList.append(button);
    }

    el.chatCount.textContent = `${list.length} of ${state.channels.length} chats`;
  }

  // ------------------------------------------------------------------ files --

  function selectChat(chat) {
    state.chatId = chat.id;
    state.chatTitle = chat.title;
    state.selected.clear();
    state.files = [];
    state.offsetId = 0;
    state.hasMore = false;

    el.chatTitle.textContent = chat.title;
    el.chatMeta.textContent =
      `${chat.kind === "dm" ? "Direct chat" : chat.kind === "group" ? "Group" : "Channel"}` +
      (chat.migrated_count ? ` · ${chat.migrated_count} already migrated` : "");

    renderChannels();
    loadFiles({ reset: true });
  }

  async function loadFiles({ reset = false } = {}) {
    if (state.chatId === null || state.loadingFiles) return;
    state.loadingFiles = true;

    if (reset) {
      state.files = [];
      state.offsetId = 0;
      el.fileGrid.innerHTML = "";
    }
    el.fileHint.textContent = "Scanning history…";
    el.btnLoadMore.disabled = true;

    try {
      const params = new URLSearchParams({
        offset_id: String(state.offsetId),
        limit: "60",
        type_filter: state.typeFilter,
      });
      const data = await api(`/api/channels/${state.chatId}/files?${params}`);

      const known = new Set(state.files.map((f) => f.message_id));
      for (const item of data.items) {
        if (!known.has(item.message_id)) state.files.push(item);
      }
      state.offsetId = data.next_offset_id || state.offsetId;
      state.hasMore = Boolean(data.has_more);
      renderFiles();
    } catch (error) {
      el.fileHint.textContent = `Couldn't list files: ${error.message}`;
      appendLog("error", `File list failed: ${error.message}`);
    } finally {
      state.loadingFiles = false;
      el.btnLoadMore.disabled = false;
      syncControls();
    }
  }

  function renderFiles() {
    el.fileGrid.innerHTML = "";

    for (const file of state.files) {
      el.fileGrid.append(fileCard(file));
    }

    const shown = state.files.length;
    if (!shown) {
      el.fileHint.textContent = state.hasMore
        ? "Nothing matched in this stretch of history — keep loading older files."
        : "No matching files in this chat.";
    } else {
      const migrated = state.files.filter((f) => f.migrated).length;
      const bytes = state.files.reduce((sum, f) => sum + (f.size || 0), 0);
      el.fileHint.textContent = `${shown} file${shown === 1 ? "" : "s"} listed · ${humanSize(
        bytes
      )} · ${migrated} already migrated`;
    }

    el.btnLoadMore.hidden = !state.hasMore;
    renderSelection();
  }

  function fileCard(file) {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "file-card";
    card.dataset.id = String(file.message_id);
    card.setAttribute("role", "option");
    if (file.migrated) card.classList.add("is-migrated");
    if (state.selected.has(file.message_id)) card.classList.add("is-selected");
    card.setAttribute("aria-selected", String(state.selected.has(file.message_id)));

    const icon = document.createElement("span");
    icon.className = "file-card__icon";
    icon.dataset.kind = file.kind;
    icon.textContent = (file.ext || "").replace(".", "").toUpperCase().slice(0, 4) || "FILE";

    const body = document.createElement("span");
    body.className = "file-card__body";

    const name = document.createElement("span");
    name.className = "file-card__name";
    name.textContent = file.filename;
    name.title = file.filename;

    const meta = document.createElement("span");
    meta.className = "file-card__meta";

    const size = document.createElement("span");
    size.textContent = humanSize(file.size);
    const date = document.createElement("span");
    date.textContent = shortDate(file.date);
    meta.append(size, date);

    if (file.migrated) {
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = "Migrated";
      meta.append(badge);
    }

    body.append(name, meta);
    card.append(icon, body);
    card.addEventListener("click", () => toggleFile(file, card));
    return card;
  }

  function toggleFile(file, card) {
    if (state.selected.has(file.message_id)) state.selected.delete(file.message_id);
    else state.selected.add(file.message_id);

    card.classList.toggle("is-selected", state.selected.has(file.message_id));
    card.setAttribute("aria-selected", String(state.selected.has(file.message_id)));
    renderSelection();
  }

  function renderSelection() {
    const count = state.selected.size;
    el.selectionBar.hidden = count === 0;
    if (count) {
      const bytes = state.files
        .filter((f) => state.selected.has(f.message_id))
        .reduce((sum, f) => sum + (f.size || 0), 0);
      el.selectionCount.textContent = `${count} file${count === 1 ? "" : "s"} selected`;
      el.selectionSize.textContent = humanSize(bytes);
    }
    syncControls();
  }

  // --------------------------------------------------------------- controls --

  function syncControls() {
    const s = state.status;
    const active = ACTIVE_STATES.has(s.state);
    const hasChat = state.chatId !== null;
    const hasFiles = state.files.length > 0;

    el.btnSelectAll.disabled = !hasFiles || active;
    el.btnClear.disabled = state.selected.size === 0;
    el.btnSyncAll.disabled = !hasChat || active;
    el.btnStart.disabled = !hasChat || state.selected.size === 0 || active;
    el.btnPause.disabled = !(s.state === "running" || s.state === "scanning");
    el.btnResume.disabled = s.state !== "paused";
    el.btnStop.disabled = !active;
    el.btnLoadMore.hidden = !state.hasMore;
  }

  function renderStatus(snapshot) {
    state.status = snapshot;
    const label =
      {
        idle: "Idle",
        scanning: "Scanning history",
        running: "Migrating",
        paused: "Paused",
        stopping: "Stopping",
      }[snapshot.state] || snapshot.state;

    el.statusPill.dataset.state = snapshot.state;
    el.statusText.textContent =
      snapshot.state === "idle" ? label : `${label} · ${snapshot.chat_title || ""}`.trim();

    const active = ACTIVE_STATES.has(snapshot.state);
    el.progressWrap.hidden = !active && !(snapshot.total && snapshot.state === "idle");

    const handled = (snapshot.done || 0) + (snapshot.failed || 0);
    const total = snapshot.total || 0;
    const pct = total ? Math.min(100, (handled / total) * 100) : 0;
    el.progressFill.style.width = `${pct}%`;
    el.progressCounts.textContent = `${handled} / ${total}`;
    el.progressFailed.textContent = snapshot.failed ? `${snapshot.failed} failed` : "";

    const current = snapshot.current_file;
    el.progressCurrent.textContent = current
      ? `${current.name} (${humanSize(current.size)})`
      : active
      ? "…"
      : "";

    highlightCurrent(current ? current.message_id : null);
    syncControls();
  }

  function highlightCurrent(messageId) {
    for (const card of el.fileGrid.children) {
      card.classList.toggle("is-current", messageId !== null && card.dataset.id === String(messageId));
    }
  }

  function markMigratedInGrid(messageId) {
    const file = state.files.find((f) => f.message_id === messageId);
    if (file) file.migrated = true;
    state.selected.delete(messageId);
    const card = el.fileGrid.querySelector(`[data-id="${messageId}"]`);
    if (card && !card.classList.contains("is-migrated")) {
      card.classList.add("is-migrated");
      card.classList.remove("is-selected");
      const meta = card.querySelector(".file-card__meta");
      if (meta && !meta.querySelector(".badge")) {
        const badge = document.createElement("span");
        badge.className = "badge";
        badge.textContent = "Migrated";
        meta.append(badge);
      }
    }
    renderSelection();
  }

  async function startMigration(messageIds) {
    try {
      const snapshot = await api("/api/migrate/start", {
        method: "POST",
        body: JSON.stringify({ chat_id: state.chatId, message_ids: messageIds }),
      });
      renderStatus(snapshot);
    } catch (error) {
      appendLog("error", error.message);
    }
  }

  async function control(action) {
    try {
      renderStatus(await api(`/api/migrate/${action}`, { method: "POST" }));
    } catch (error) {
      appendLog("error", error.message);
    }
  }

  // -------------------------------------------------------------- websocket --

  function connectSocket() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(`${proto}//${location.host}/ws/logs`);
    state.socket = socket;

    el.wsDot.dataset.state = "connecting";
    el.wsStatusText.textContent = "Connecting…";

    let keepalive = null;

    socket.addEventListener("open", () => {
      state.wsRetry = 0;
      el.wsDot.dataset.state = "on";
      el.wsStatusText.textContent = "Live";
      keepalive = setInterval(() => {
        if (socket.readyState === WebSocket.OPEN) socket.send("ping");
      }, 25000);
    });

    socket.addEventListener("message", (event) => {
      let payload;
      try {
        payload = JSON.parse(event.data);
      } catch (_) {
        return;
      }
      handleEvent(payload);
    });

    socket.addEventListener("close", () => {
      clearInterval(keepalive);
      el.wsDot.dataset.state = "off";
      if (!state.unlocked || !state.account) {
        el.wsStatusText.textContent = "Signed out";
        return; // nothing to reconnect to until sign-in
      }
      state.wsRetry += 1;
      const delay = Math.min(15000, 1000 * 2 ** Math.min(state.wsRetry, 4));
      el.wsStatusText.textContent = `Offline — retrying in ${Math.round(delay / 1000)}s`;
      setTimeout(connectSocket, delay);
    });

    socket.addEventListener("error", () => socket.close());
  }

  function handleEvent(payload) {
    switch (payload.type) {
      case "history":
        for (const event of payload.events || []) handleEvent(event);
        break;
      case "log":
        appendLog(payload.level, payload.message, payload);
        if (payload.level === "success" && payload.message_id) {
          markMigratedInGrid(payload.message_id);
        }
        break;
      case "progress": {
        const wasActive = ACTIVE_STATES.has(state.status.state);
        renderStatus(payload);
        if (wasActive && payload.state === "idle") {
          refreshAfterRun();
        }
        break;
      }
      case "file_progress":
        el.progressCurrent.textContent = `${payload.name} — ${payload.phase} ${payload.percent}%`;
        break;
      default:
        break;
    }
  }

  async function refreshAfterRun() {
    try {
      const data = await api("/api/channels");
      state.channels = data.channels || [];
      renderChannels();
      const health = await api("/api/health");
      el.storeCount.textContent = `${health.migrated_total} migrated`;
    } catch (_) {
      /* non-fatal */
    }
    if (state.chatId !== null) loadFiles({ reset: true });
  }


  // ------------------------------------------------------------------ auth --

  const PANES = ["passcode", "phone", "code", "password", "cloudinary"];
  const PANE_STEP = {
    passcode: "passcode",
    phone: "phone",
    code: "phone",
    password: "phone",
    cloudinary: "cloudinary",
  };
  const PANE_SUB = {
    passcode: "Unlock the dashboard to continue.",
    phone: "Sign in to the Telegram account you want to migrate from.",
    code: "Enter the code Telegram just sent you.",
    password: "This account has two-step verification switched on.",
    cloudinary: "Where should the files land?",
  };

  function setPane(pane) {
    state.pane = pane;
    clearAuthError();

    for (const name of PANES) {
      const node = document.querySelector(`[data-pane="${name}"]`);
      if (node) node.hidden = name !== pane;
    }

    const activeStep = PANE_STEP[pane];
    const order = ["passcode", "phone", "cloudinary"];
    for (const li of el.stepTrack.children) {
      const step = li.dataset.step;
      li.classList.toggle("is-active", step === activeStep);
      li.classList.toggle("is-done", order.indexOf(step) < order.indexOf(activeStep));
    }

    el.stepSub.textContent = PANE_SUB[pane] || "";
    el.authOverlay.hidden = false;

    const focusTarget = {
      passcode: el.inpPasscode,
      phone: el.inpPhone,
      code: el.inpCode,
      password: el.inpPassword,
      cloudinary: el.inpCloudName,
    }[pane];
    if (focusTarget) setTimeout(() => focusTarget.focus(), 40);
  }

  function hideOverlay() {
    el.authOverlay.hidden = true;
    clearAuthError();
  }

  function showAuthError(message) {
    el.authError.textContent = message;
    el.authError.hidden = false;
  }

  function clearAuthError() {
    el.authError.hidden = true;
    el.authError.textContent = "";
  }

  function busy(button, isBusy, label) {
    button.disabled = isBusy;
    if (isBusy) {
      button.dataset.label = button.textContent;
      button.textContent = label;
    } else if (button.dataset.label) {
      button.textContent = button.dataset.label;
    }
  }

  function handleSessionLost() {
    state.unlocked = false;
    state.account = null;
    closeSocket();
    resetWorkspace();
    setPane("passcode");
  }

  function handleAccountLost() {
    state.account = null;
    closeSocket();
    resetWorkspace();
    setPane("phone");
  }

  function closeSocket() {
    if (state.socket && state.socket.readyState <= WebSocket.OPEN) {
      state.socket.close();
    }
    state.socket = null;
  }

  function resetWorkspace() {
    state.channels = [];
    state.files = [];
    state.selected.clear();
    state.chatId = null;
    state.status = { state: "idle", active: false };
    el.chatList.innerHTML = "";
    el.fileGrid.innerHTML = "";
    el.chatTitle.textContent = "Pick a chat to begin";
    el.chatMeta.textContent = "Its PDFs, videos, HTML files and images show up here.";
    el.selectionBar.hidden = true;
    el.progressWrap.hidden = true;
    syncControls();
  }

  async function refreshSession() {
    let session;
    try {
      session = await api("/api/auth/session");
    } catch (error) {
      showAuthError(`Backend unreachable: ${error.message}`);
      setPane("passcode");
      return;
    }

    state.unlocked = Boolean(session.unlocked);
    state.accounts = session.accounts || [];
    state.cloudinaryDefaults = session.cloudinary_defaults || null;

    if (!state.unlocked) {
      setPane("passcode");
      return;
    }
    if (!session.account) {
      renderKnownAccounts();
      setPane("phone");
      return;
    }
    state.account = session.account;
    if (!session.account.cloudinary) {
      prefillCloudinary(null);
      setPane("cloudinary");
      return;
    }
    enterDashboard();
  }

  function enterDashboard() {
    hideOverlay();
    const account = state.account;
    el.accountName.textContent = account.display_name;
    el.accountLine.textContent = account.cloudinary
      ? `→ ${account.cloudinary.cloud_name}`
      : "No Cloudinary target set";
    el.storeCount.textContent = `${account.migrated_total || 0} migrated`;

    connectSocket();
    loadChannels();
    api("/api/migrate/status").then(renderStatus).catch(() => {});
  }

  function renderKnownAccounts() {
    const list = state.accounts || [];
    el.knownAccounts.hidden = list.length === 0;
    el.knownAccountList.innerHTML = "";

    for (const account of list) {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "account-row";

      const avatar = document.createElement("span");
      avatar.className = "account-row__avatar";
      avatar.textContent = initials(account.display_name);

      const body = document.createElement("span");
      body.className = "account-row__body";
      const name = document.createElement("span");
      name.className = "account-row__name";
      name.textContent = account.display_name;
      const meta = document.createElement("span");
      meta.className = "account-row__meta";
      meta.textContent = account.cloudinary_configured
        ? `${account.migrated_total} migrated · ${account.cloud_name}`
        : "Cloudinary not set up";
      body.append(name, meta);

      row.append(avatar, body);
      row.addEventListener("click", () => activateAccount(account.id));
      el.knownAccountList.append(row);
    }
  }

  async function unlock() {
    const passcode = el.inpPasscode.value;
    if (!passcode) return showAuthError("Enter the passcode from backend/.env.");

    busy(el.btnUnlock, true, "Unlocking…");
    try {
      const result = await api("/api/auth/unlock", {
        method: "POST",
        body: JSON.stringify({ passcode }),
      });
      el.inpPasscode.value = "";
      state.unlocked = true;
      state.accounts = result.accounts || [];
      renderKnownAccounts();
      setPane("phone");
    } catch (error) {
      showAuthError(error.message);
    } finally {
      busy(el.btnUnlock, false);
    }
  }

  async function sendCode() {
    const phone = el.inpPhone.value.trim();
    if (!phone) return showAuthError("Enter your phone number in international format.");

    busy(el.btnSendCode, true, "Sending…");
    try {
      const result = await api("/api/auth/telegram/send-code", {
        method: "POST",
        body: JSON.stringify({ phone }),
      });
      state.loginId = result.login_id;
      el.maskedPhone.textContent = result.phone;
      el.inpCode.value = "";
      startCodeTimer(result.expires_in);
      setPane("code");
    } catch (error) {
      showAuthError(error.message);
    } finally {
      busy(el.btnSendCode, false);
    }
  }

  function startCodeTimer(seconds) {
    state.codeDeadline = Date.now() + seconds * 1000;
    const tick = () => {
      if (state.pane !== "code") return;
      const left = Math.max(0, Math.round((state.codeDeadline - Date.now()) / 1000));
      el.codeTimer.textContent = left
        ? `Code expires in ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`
        : "This code has expired — request a new one.";
      if (left) setTimeout(tick, 1000);
    };
    tick();
  }

  async function verifyCode() {
    const code = el.inpCode.value.trim();
    if (!code) return showAuthError("Enter the code Telegram sent you.");

    busy(el.btnVerifyCode, true, "Verifying…");
    try {
      const result = await api("/api/auth/telegram/verify-code", {
        method: "POST",
        body: JSON.stringify({ login_id: state.loginId, code }),
      });
      if (result.needs_password) {
        el.passwordHint.textContent = result.hint ? `Hint: ${result.hint}` : "";
        el.inpPassword.value = "";
        setPane("password");
        return;
      }
      await onSignedIn(result.account);
    } catch (error) {
      showAuthError(error.message);
      if (error.code === "code_expired" || error.status === 410) setPane("phone");
    } finally {
      busy(el.btnVerifyCode, false);
    }
  }

  async function submitPassword() {
    const password = el.inpPassword.value;
    if (!password) return showAuthError("Enter your two-step verification password.");

    busy(el.btnSubmitPassword, true, "Signing in…");
    try {
      const result = await api("/api/auth/telegram/password", {
        method: "POST",
        body: JSON.stringify({ login_id: state.loginId, password }),
      });
      el.inpPassword.value = "";
      await onSignedIn(result.account);
    } catch (error) {
      showAuthError(error.message);
    } finally {
      busy(el.btnSubmitPassword, false);
    }
  }

  async function onSignedIn(account) {
    state.account = account;
    state.loginId = null;
    el.inpPhone.value = "";
    el.inpCode.value = "";
    try {
      const list = await api("/api/accounts");
      state.accounts = list.accounts || [];
    } catch (_) {
      /* non-fatal */
    }

    if (!account.cloudinary) {
      prefillCloudinary(null);
      setPane("cloudinary");
    } else {
      enterDashboard();
    }
  }

  function prefillCloudinary(existing) {
    const defaults = state.cloudinaryDefaults || {};
    const source = existing || {};
    el.inpCloudName.value = source.cloud_name || defaults.cloud_name || "";
    el.inpApiKey.value = source.api_key || defaults.api_key || "";
    el.inpApiSecret.value = "";
    el.inpFolder.value = source.folder || defaults.folder || "telegram_migration";
    el.inpFolderPerChat.checked =
      source.folder_per_chat !== undefined
        ? Boolean(source.folder_per_chat)
        : defaults.folder_per_chat !== false;
    el.btnSkipCloudinary.hidden = !existing;
  }

  async function saveCloudinary() {
    const body = {
      cloud_name: el.inpCloudName.value.trim(),
      api_key: el.inpApiKey.value.trim(),
      api_secret: el.inpApiSecret.value.trim(),
      folder: el.inpFolder.value.trim() || "telegram_migration",
      folder_per_chat: el.inpFolderPerChat.checked,
    };
    if (!body.cloud_name || !body.api_key || !body.api_secret) {
      return showAuthError("Cloud name, API key and API secret are all required.");
    }

    busy(el.btnSaveCloudinary, true, "Checking with Cloudinary…");
    try {
      const result = await api("/api/account/cloudinary", {
        method: "PUT",
        body: JSON.stringify(body),
      });
      state.account = { ...state.account, cloudinary: result.cloudinary };
      enterDashboard();
    } catch (error) {
      showAuthError(error.message);
    } finally {
      busy(el.btnSaveCloudinary, false);
    }
  }

  async function activateAccount(accountId) {
    try {
      const result = await api(`/api/accounts/${accountId}/activate`, { method: "POST" });
      closeSocket();
      resetWorkspace();
      state.account = result.account;
      closeMenu();
      if (!result.account.cloudinary) {
        prefillCloudinary(null);
        setPane("cloudinary");
      } else {
        enterDashboard();
      }
    } catch (error) {
      showAuthError(error.message);
      setPane("phone");
    }
  }

  async function signOutAccount() {
    if (!state.account) return;
    const ok = window.confirm(
      `Sign out of ${state.account.display_name}?\n\n` +
        "This revokes the session on Telegram and deletes this account's " +
        "migration history from the dashboard. Files already in Cloudinary stay there."
    );
    if (!ok) return;

    try {
      await api(`/api/accounts/${state.account.id}`, { method: "DELETE" });
    } catch (error) {
      showAuthError(error.message);
      return;
    }
    closeMenu();
    handleAccountLost();
    try {
      const list = await api("/api/accounts");
      state.accounts = list.accounts || [];
      renderKnownAccounts();
    } catch (_) {
      /* non-fatal */
    }
  }

  async function lockDashboard() {
    try {
      await api("/api/auth/lock", { method: "POST" });
    } catch (_) {
      /* locking is best-effort */
    }
    closeMenu();
    handleSessionLost();
  }

  function openMenu() {
    el.menuAccountList.innerHTML = "";
    for (const account of state.accounts) {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "menu__item";
      const isActive = state.account && account.id === state.account.id;
      row.textContent = (isActive ? "• " : "  ") + account.display_name;
      if (isActive) row.classList.add("menu__item--muted");
      else row.addEventListener("click", () => activateAccount(account.id));
      el.menuAccountList.append(row);
    }
    el.accountMenu.hidden = false;
    el.btnAccountMenu.setAttribute("aria-expanded", "true");
  }

  function closeMenu() {
    el.accountMenu.hidden = true;
    el.btnAccountMenu.setAttribute("aria-expanded", "false");
  }

  function bindAuthEvents() {
    const onEnter = (input, action) =>
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          action();
        }
      });

    el.btnUnlock.addEventListener("click", unlock);
    onEnter(el.inpPasscode, unlock);

    el.btnSendCode.addEventListener("click", sendCode);
    onEnter(el.inpPhone, sendCode);

    el.btnVerifyCode.addEventListener("click", verifyCode);
    onEnter(el.inpCode, verifyCode);
    el.inpCode.addEventListener("input", (event) => {
      event.target.value = event.target.value.replace(/\D/g, "");
    });

    el.btnBackToPhone.addEventListener("click", async () => {
      if (state.loginId) {
        try {
          await api("/api/auth/telegram/cancel", {
            method: "POST",
            body: JSON.stringify({ login_id: state.loginId }),
          });
        } catch (_) {
          /* the server sweeps stale logins anyway */
        }
        state.loginId = null;
      }
      setPane("phone");
    });

    el.btnSubmitPassword.addEventListener("click", submitPassword);
    onEnter(el.inpPassword, submitPassword);

    el.btnSaveCloudinary.addEventListener("click", saveCloudinary);
    onEnter(el.inpApiSecret, saveCloudinary);
    el.btnSkipCloudinary.addEventListener("click", () => {
      if (state.account && state.account.cloudinary) enterDashboard();
    });

    el.btnAccountMenu.addEventListener("click", (event) => {
      event.stopPropagation();
      el.accountMenu.hidden ? openMenu() : closeMenu();
    });
    document.addEventListener("click", (event) => {
      if (!el.accountMenu.hidden && !el.accountMenu.contains(event.target)) closeMenu();
    });

    el.btnAddAccount.addEventListener("click", () => {
      closeMenu();
      renderKnownAccounts();
      setPane("phone");
    });

    el.btnCloudinarySettings.addEventListener("click", () => {
      closeMenu();
      prefillCloudinary(state.account && state.account.cloudinary);
      setPane("cloudinary");
    });

    el.btnSignOut.addEventListener("click", signOutAccount);
    el.btnLock.addEventListener("click", lockDashboard);
  }

  // ------------------------------------------------------------------ wiring --

  function bindEvents() {
    el.chatSearch.addEventListener("input", (event) => {
      state.search = event.target.value;
      renderChannels();
    });

    el.refreshChats.addEventListener("click", loadChannels);

    document.querySelectorAll("[data-chat-kind]").forEach((chip) => {
      chip.addEventListener("click", () => {
        document.querySelectorAll("[data-chat-kind]").forEach((c) => c.classList.remove("is-active"));
        chip.classList.add("is-active");
        state.chatKind = chip.dataset.chatKind;
        renderChannels();
      });
    });

    document.querySelectorAll("[data-type]").forEach((chip) => {
      chip.addEventListener("click", () => {
        document.querySelectorAll("[data-type]").forEach((c) => c.classList.remove("is-active"));
        chip.classList.add("is-active");
        state.typeFilter = chip.dataset.type;
        state.selected.clear();
        loadFiles({ reset: true });
      });
    });

    el.btnLoadMore.addEventListener("click", () => loadFiles());

    el.btnSelectAll.addEventListener("click", () => {
      for (const file of state.files) {
        if (!file.migrated) state.selected.add(file.message_id);
      }
      renderFiles();
    });

    el.btnClear.addEventListener("click", () => {
      state.selected.clear();
      renderFiles();
    });

    el.btnStart.addEventListener("click", () => startMigration([...state.selected]));

    el.btnSyncAll.addEventListener("click", () => {
      const ok = window.confirm(
        `Sync every matching file in “${state.chatTitle}”?\n\n` +
          "Files already migrated are skipped automatically."
      );
      if (ok) startMigration(null);
    });

    el.btnPause.addEventListener("click", () => control("pause"));
    el.btnResume.addEventListener("click", () => control("resume"));
    el.btnStop.addEventListener("click", () => control("stop"));

    el.btnClearLog.addEventListener("click", () => {
      el.logView.innerHTML = "";
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && state.selected.size) {
        state.selected.clear();
        renderFiles();
      }
    });
  }

  async function boot() {
    bindEvents();
    bindAuthEvents();
    await refreshSession();
  }

  boot();
})();
