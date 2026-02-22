/**
 * Remote Claw Admin Panel — JavaScript
 * Handles authentication, dashboard, game controls, queue management,
 * and live configuration editing.
 */
(function () {
  "use strict";

  // -- State ------------------------------------------------------------------
  let adminKey = sessionStorage.getItem("remote_claw_admin_key") || "";
  let dashboardInterval = null;
  let configFields = []; // Loaded from server
  let pendingChanges = {}; // key -> new value
  let lastDashboard = null; // Cache last dashboard response for mock flag

  // -- DOM Elements -----------------------------------------------------------
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => document.querySelectorAll(sel);

  const loginScreen = $("#login-screen");
  const adminPanel = $("#admin-panel");
  const loginForm = $("#login-form");
  const loginError = $("#login-error");
  const sidebar = $("#sidebar");
  const sidebarOverlay = $("#sidebar-overlay");

  // -- Helpers ----------------------------------------------------------------

  function headers() {
    return { "X-Admin-Key": adminKey, "Content-Type": "application/json" };
  }

  async function api(method, path, body) {
    const opts = { method, headers: headers() };
    if (body !== undefined) opts.body = JSON.stringify(body);
    const res = await fetch(path, opts);
    if (res.status === 403) {
      toast("Session expired. Please sign in again.", "error");
      logout();
      throw new Error("Forbidden");
    }
    return res;
  }

  function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  function formatUptime(seconds) {
    const d = Math.floor(seconds / 86400);
    const h = Math.floor((seconds % 86400) / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    if (d > 0) return `${d}d ${h}h ${m}m`;
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
  }

  function formatTimeAgo(isoStr) {
    if (!isoStr) return "";
    const now = Date.now();
    const then = new Date(isoStr + (isoStr.endsWith("Z") ? "" : "Z")).getTime();
    const diffS = Math.floor((now - then) / 1000);
    if (diffS < 0) return "just now";
    if (diffS < 60) return "just now";
    if (diffS < 3600) return `${Math.floor(diffS / 60)}m ago`;
    if (diffS < 86400) return `${Math.floor(diffS / 3600)}h ago`;
    return `${Math.floor(diffS / 86400)}d ago`;
  }

  function toast(message, type) {
    type = type || "info";
    const container = $("#toast-container");
    const el = document.createElement("div");
    el.className = `toast toast-${type}`;
    el.textContent = message;
    container.appendChild(el);
    setTimeout(() => { if (el.parentNode) el.remove(); }, 4200);
  }

  function setConnectionStatus(connected) {
    const dot = $("#sidebar-dot");
    const text = $("#sidebar-status-text");
    if (!dot || !text) return;
    if (connected) {
      dot.className = "sidebar-dot connected";
      text.textContent = "Connected";
    } else {
      dot.className = "sidebar-dot error";
      text.textContent = "Disconnected";
    }
  }

  // -- Authentication ---------------------------------------------------------

  function tryAutoLogin() {
    if (adminKey) {
      verifyKey(adminKey);
    }
  }

  async function verifyKey(key) {
    try {
      const res = await fetch("/admin/dashboard", {
        headers: { "X-Admin-Key": key },
      });
      if (res.ok) {
        adminKey = key;
        sessionStorage.setItem("remote_claw_admin_key", key);
        showAdmin();
        return true;
      }
    } catch (e) {
      // Network error
    }
    return false;
  }

  if (loginForm) {
    loginForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      loginError.textContent = "";
      const key = $("#admin-key-input").value.trim();
      if (!key) return;

      const btn = $("#login-btn");
      btn.disabled = true;
      const ok = await verifyKey(key);
      btn.disabled = false;
      if (!ok) {
        loginError.textContent = "Invalid admin key. Please try again.";
      }
    });
  }

  function logout() {
    adminKey = "";
    sessionStorage.removeItem("remote_claw_admin_key");
    if (adminPanel) adminPanel.classList.add("hidden");
    if (loginScreen) loginScreen.classList.remove("hidden");
    if (dashboardInterval) clearInterval(dashboardInterval);
    dashboardInterval = null;
    setConnectionStatus(false);
  }

  function showAdmin() {
    if (loginScreen) loginScreen.classList.add("hidden");
    if (adminPanel) adminPanel.classList.remove("hidden");
    setConnectionStatus(true);
    refreshDashboard();
    if (dashboardInterval) clearInterval(dashboardInterval);
    dashboardInterval = setInterval(refreshDashboard, 4000);
    loadConfig();
    loadQueue();
  }

  // Logout buttons
  if ($("#logout-btn")) {
    $("#logout-btn").addEventListener("click", logout);
  }
  if ($("#mobile-logout")) {
    $("#mobile-logout").addEventListener("click", logout);
  }

  // -- Navigation -------------------------------------------------------------

  $$(".nav-link").forEach((link) => {
    link.addEventListener("click", (e) => {
      e.preventDefault();
      const section = link.dataset.section;
      switchSection(section);
      closeSidebar();
    });
  });

  function openSidebar() {
    if (sidebar) sidebar.classList.add("open");
    if (sidebarOverlay) {
      sidebarOverlay.classList.remove("hidden");
      // Force reflow, then add visible class for transition
      void sidebarOverlay.offsetWidth;
      sidebarOverlay.classList.add("visible");
    }
  }

  function closeSidebar() {
    if (sidebar) sidebar.classList.remove("open");
    if (sidebarOverlay) {
      sidebarOverlay.classList.remove("visible");
      setTimeout(() => sidebarOverlay.classList.add("hidden"), 300);
    }
  }

  if ($("#menu-toggle")) {
    $("#menu-toggle").addEventListener("click", () => {
      if (sidebar && sidebar.classList.contains("open")) {
        closeSidebar();
      } else {
        openSidebar();
      }
    });
  }

  // Close sidebar when clicking overlay
  if (sidebarOverlay) {
    sidebarOverlay.addEventListener("click", closeSidebar);
  }

  function switchSection(name) {
    $$(".section").forEach((s) => s.classList.remove("active"));
    const target = $(`#section-${name}`);
    if (target) target.classList.add("active");

    $$(".nav-link").forEach((l) => l.classList.remove("active"));
    const link = $(`.nav-link[data-section="${name}"]`);
    if (link) link.classList.add("active");

    // Refresh data when switching
    if (name === "queue") loadQueue();
    if (name === "dashboard") refreshDashboard();
  }

  // -- Dashboard --------------------------------------------------------------

  async function refreshDashboard() {
    try {
      const res = await api("GET", "/admin/dashboard");
      if (!res.ok) {
        setConnectionStatus(false);
        return;
      }
      setConnectionStatus(true);
      const data = await res.json();
      lastDashboard = data;

      const el = (id) => $(`#${id}`);

      el("dash-uptime").textContent = formatUptime(data.uptime_seconds);
      el("dash-state").textContent = formatState(data.game_state);
      el("dash-viewers").textContent = String(data.viewer_count);
      el("dash-queue-size").textContent = String(data.queue ? data.queue.length : 0);

      // Stats
      const stats = data.stats || {};
      const total = stats.total_completed || 0;
      const wins = stats.total_wins || 0;
      el("dash-total-games").textContent = String(total);
      el("dash-win-rate").textContent = total > 0
        ? `${Math.round((wins / total) * 100)}%`
        : "--";

      // Flags
      toggleFlag("flag-paused", data.paused);
      toggleFlag("flag-gpio-locked", data.gpio_locked);
      // mock_gpio flag: derive from config if loaded, otherwise hide
      const mockField = configFields.find((f) => f.key === "mock_gpio");
      toggleFlag("flag-mock", mockField ? mockField.value === true || mockField.value === "true" : false);
      // Win sensor off flag
      toggleFlag("flag-win-sensor-off", data.win_sensor_enabled === false);

      // Active player
      if (data.active_player != null) {
        const activeEntry = (data.queue || []).find(
          (e) => e.state === "active" || e.state === "ready"
        );
        el("dash-active-player").textContent = activeEntry
          ? activeEntry.name
          : `Entry #${data.active_player}`;
        el("dash-try-info").textContent = `Try ${data.current_try} / ${data.max_tries}`;
      } else {
        el("dash-active-player").textContent = "None";
        el("dash-try-info").textContent = "Waiting for players";
      }

      // Recent results
      renderDashRecent(data.recent_results || []);

    } catch (e) {
      setConnectionStatus(false);
    }
  }

  function formatState(state) {
    const map = {
      idle: "Idle",
      ready_prompt: "Ready Prompt",
      moving: "Moving",
      dropping: "Dropping",
      post_drop: "Post-Drop",
      turn_end: "Turn End",
    };
    return map[state] || state;
  }

  function toggleFlag(id, visible) {
    const el = $(`#${id}`);
    if (el) el.classList.toggle("visible", !!visible);
  }

  function renderDashRecent(results) {
    const el = $("#dash-recent");
    if (!el) return;
    if (!results.length) {
      el.innerHTML = '<p class="text-dim">No recent games yet</p>';
      return;
    }
    el.innerHTML = results
      .map((r) => {
        const isWin = r.result === "win";
        const badge = isWin ? "badge-win" : "badge-loss";
        const label = isWin ? "WIN" : escapeHtml((r.result || "LOSS").toUpperCase());
        return `<div class="recent-entry">
          <span class="recent-badge ${badge}">${label}</span>
          <span class="recent-name">${escapeHtml(r.name)}</span>
          <span class="recent-time">${formatTimeAgo(r.completed_at)}</span>
        </div>`;
      })
      .join("");
  }

  // -- Game Controls ----------------------------------------------------------

  async function controlAction(path, btn) {
    const fb = $("#control-feedback");
    if (!fb) return;
    btn.disabled = true;
    try {
      const res = await api("POST", path);
      const data = await res.json();
      if (res.ok) {
        fb.classList.remove("hidden", "error");
        fb.classList.add("success");
        fb.textContent = data.warning || data.message || "Action completed successfully.";
        toast("Action completed.", "success");
      } else {
        throw new Error(data.detail || "Action failed");
      }
      refreshDashboard();
    } catch (e) {
      fb.classList.remove("hidden", "success");
      fb.classList.add("error");
      fb.textContent = "Action failed: " + e.message;
      toast("Action failed.", "error");
    } finally {
      btn.disabled = false;
      setTimeout(() => fb.classList.add("hidden"), 6000);
    }
  }

  if ($("#ctrl-advance")) {
    $("#ctrl-advance").addEventListener("click", function () {
      controlAction("/admin/advance", this);
    });
  }
  if ($("#ctrl-pause")) {
    $("#ctrl-pause").addEventListener("click", function () {
      controlAction("/admin/pause", this);
    });
  }
  if ($("#ctrl-resume")) {
    $("#ctrl-resume").addEventListener("click", function () {
      controlAction("/admin/resume", this);
    });
  }
  if ($("#ctrl-estop")) {
    $("#ctrl-estop").addEventListener("click", function () {
      if (confirm("Emergency Stop: This will lock all GPIO controls. Continue?")) {
        controlAction("/admin/emergency-stop", this);
      }
    });
  }
  if ($("#ctrl-unlock")) {
    $("#ctrl-unlock").addEventListener("click", function () {
      controlAction("/admin/unlock", this);
    });
  }

  // -- Queue Management -------------------------------------------------------

  async function loadQueue() {
    try {
      const res = await api("GET", "/admin/queue-details");
      if (!res.ok) return;
      const data = await res.json();
      renderQueue(data.entries || []);
    } catch (e) {
      // Silent
    }
  }

  function renderQueue(entries) {
    const tbody = $("#queue-tbody");
    if (!tbody) return;
    if (!entries.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="text-dim">Queue is empty</td></tr>';
      return;
    }

    tbody.innerHTML = entries
      .map((e) => {
        const stateClass = e.state === "active" ? "active" : e.state === "ready" ? "ready" : "waiting";
        return `<tr>
          <td class="mono">${escapeHtml(String(e.id))}</td>
          <td>${escapeHtml(e.name)}</td>
          <td><span class="state-badge ${stateClass}">${escapeHtml(e.state)}</span></td>
          <td>${escapeHtml(String(e.position))}</td>
          <td>${formatTimeAgo(e.created_at)}</td>
          <td><button class="kick-btn" data-id="${escapeHtml(String(e.id))}" data-name="${escapeHtml(e.name)}">Kick</button></td>
        </tr>`;
      })
      .join("");

    // Attach kick handlers
    tbody.querySelectorAll(".kick-btn").forEach((btn) => {
      btn.addEventListener("click", async function () {
        const id = this.dataset.id;
        const name = this.dataset.name;
        if (!confirm(`Remove "${name}" from the queue?`)) return;
        this.disabled = true;
        try {
          const res = await api("POST", `/admin/kick/${id}`);
          if (res.ok) {
            toast(`${name} removed from queue.`, "success");
          } else {
            const data = await res.json();
            toast(data.detail || "Failed to kick player.", "error");
          }
          loadQueue();
          refreshDashboard();
        } catch (e) {
          toast("Failed to kick player.", "error");
        }
        this.disabled = false;
      });
    });
  }

  if ($("#refresh-queue")) {
    $("#refresh-queue").addEventListener("click", loadQueue);
  }

  // -- Contacts CSV Download ------------------------------------------------

  if ($("#download-contacts")) {
    $("#download-contacts").addEventListener("click", async function () {
      this.disabled = true;
      try {
        const res = await fetch("/admin/contacts/csv", {
          headers: { "X-Admin-Key": adminKey },
        });
        if (!res.ok) {
          toast("Failed to download contacts.", "error");
          return;
        }
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = "remote_claw_contacts.csv";
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
        toast("Contacts CSV downloaded.", "success");
      } catch (e) {
        toast("Failed to download contacts.", "error");
      } finally {
        this.disabled = false;
      }
    });
  }

  // -- Configuration ----------------------------------------------------------

  async function loadConfig() {
    try {
      const res = await api("GET", "/admin/config");
      if (!res.ok) return;
      const data = await res.json();
      configFields = data.fields || [];
      pendingChanges = {};
      renderConfig();
    } catch (e) {
      // Silent
    }
  }

  function renderConfig() {
    const categoryFilter = $("#config-category-filter");
    if (!categoryFilter) return;

    // Build categories
    const categories = [...new Set(configFields.map((f) => f.category))];
    categoryFilter.innerHTML =
      '<option value="">All Categories</option>' +
      categories.map((c) => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join("");

    renderConfigFields(configFields);
    updateConfigActions();
  }

  function renderConfigFields(fields) {
    const container = $("#config-groups");
    if (!container) return;
    const searchTerm = ($("#config-search") ? $("#config-search").value : "").toLowerCase();
    const catFilter = $("#config-category-filter") ? $("#config-category-filter").value : "";

    // Filter
    let filtered = fields;
    if (searchTerm) {
      filtered = filtered.filter(
        (f) =>
          f.key.toLowerCase().includes(searchTerm) ||
          f.label.toLowerCase().includes(searchTerm) ||
          f.description.toLowerCase().includes(searchTerm) ||
          f.env_key.toLowerCase().includes(searchTerm)
      );
    }
    if (catFilter) {
      filtered = filtered.filter((f) => f.category === catFilter);
    }

    // Group by category
    const groups = {};
    for (const f of filtered) {
      if (!groups[f.category]) groups[f.category] = [];
      groups[f.category].push(f);
    }

    if (Object.keys(groups).length === 0) {
      container.innerHTML = '<p class="text-dim" style="padding: 20px 0;">No matching settings found.</p>';
      return;
    }

    let html = "";
    for (const [cat, catFields] of Object.entries(groups)) {
      html += `<div class="config-group">
        <div class="config-group-header">${escapeHtml(cat)}</div>`;

      for (const f of catFields) {
        const isChanged = f.key in pendingChanges;
        const currentValue = isChanged ? pendingChanges[f.key] : f.value;

        html += `<div class="config-field${isChanged ? " changed" : ""}" data-key="${f.key}">
          <div class="field-info">
            <div class="field-label">
              ${escapeHtml(f.label)}
              ${f.restart_required ? '<span class="restart-badge">restart</span>' : ""}
            </div>
            <div class="field-env-key">${escapeHtml(f.env_key)}</div>
            <div class="field-desc">${escapeHtml(f.description)}</div>
          </div>
          <div class="field-input-wrap">
            ${renderFieldInput(f, currentValue)}
            <div class="field-default">Default: ${escapeHtml(formatDefault(f.default))}</div>
          </div>
        </div>`;
      }

      html += "</div>";
    }

    container.innerHTML = html;

    // Attach input handlers
    container.querySelectorAll("input[data-config-key], select[data-config-key]").forEach((input) => {
      input.addEventListener("input", handleConfigInput);
      input.addEventListener("change", handleConfigInput);
    });

    container.querySelectorAll(".toggle[data-config-key]").forEach((toggle) => {
      toggle.addEventListener("click", handleToggleClick);
    });
  }

  function renderFieldInput(field, currentValue) {
    const key = field.key;

    if (field.type === "boolean") {
      const isOn = currentValue === true || currentValue === "true";
      return `<div class="toggle-wrap">
        <div class="toggle${isOn ? " on" : ""}" data-config-key="${key}"></div>
        <span class="toggle-label">${isOn ? "Enabled" : "Disabled"}</span>
      </div>`;
    }

    if (field.options) {
      const opts = field.options
        .map((o) => `<option value="${escapeHtml(o)}"${String(currentValue) === o ? " selected" : ""}>${escapeHtml(o)}</option>`)
        .join("");
      return `<select data-config-key="${key}">${opts}</select>`;
    }

    const inputType = field.type === "integer" || field.type === "number" ? "number" : "text";
    const step = field.type === "number" ? ' step="any"' : "";
    const val = currentValue != null ? currentValue : "";
    const modified = key in pendingChanges ? " modified" : "";

    if (field.key === "admin_api_key") {
      return `<input type="password" data-config-key="${key}" value="${escapeHtml(String(val))}" class="${modified}">`;
    }

    return `<input type="${inputType}"${step} data-config-key="${key}" value="${escapeHtml(String(val))}" class="${modified}">`;
  }

  function formatDefault(def) {
    if (def === true) return "true";
    if (def === false) return "false";
    if (def === null || def === undefined) return "none";
    return String(def);
  }

  function handleConfigInput(e) {
    const key = e.target.dataset.configKey;
    const field = configFields.find((f) => f.key === key);
    if (!field) return;

    let newValue = e.target.value;

    // Coerce type
    if (field.type === "integer") {
      newValue = newValue === "" ? 0 : parseInt(newValue, 10);
      if (isNaN(newValue)) newValue = 0;
    } else if (field.type === "number") {
      newValue = newValue === "" ? 0 : parseFloat(newValue);
      if (isNaN(newValue)) newValue = 0;
    }

    // Check if it's actually changed from the original
    if (String(newValue) === String(field.value)) {
      delete pendingChanges[key];
      e.target.classList.remove("modified");
    } else {
      pendingChanges[key] = newValue;
      e.target.classList.add("modified");
    }

    // Update the field's changed highlight
    const fieldEl = e.target.closest(".config-field");
    if (fieldEl) fieldEl.classList.toggle("changed", key in pendingChanges);

    updateConfigActions();
  }

  function handleToggleClick(e) {
    const toggle = e.currentTarget;
    const key = toggle.dataset.configKey;
    const field = configFields.find((f) => f.key === key);
    if (!field) return;

    const isCurrentlyOn = toggle.classList.contains("on");
    const newValue = !isCurrentlyOn;

    toggle.classList.toggle("on", newValue);
    const label = toggle.nextElementSibling;
    if (label) label.textContent = newValue ? "Enabled" : "Disabled";

    if (newValue === field.value) {
      delete pendingChanges[key];
    } else {
      pendingChanges[key] = newValue;
    }

    const fieldEl = toggle.closest(".config-field");
    if (fieldEl) fieldEl.classList.toggle("changed", key in pendingChanges);

    updateConfigActions();
  }

  function updateConfigActions() {
    const count = Object.keys(pendingChanges).length;
    const actions = $("#config-actions");
    const countEl = $("#config-change-count");
    if (!actions || !countEl) return;

    if (count > 0) {
      actions.classList.remove("hidden");
      countEl.textContent = `${count} unsaved change${count !== 1 ? "s" : ""}`;
    } else {
      actions.classList.add("hidden");
    }
  }

  // Save config
  if ($("#config-save-btn")) {
    $("#config-save-btn").addEventListener("click", async () => {
      const count = Object.keys(pendingChanges).length;
      if (count === 0) return;

      const fb = $("#config-feedback");
      const btn = $("#config-save-btn");
      if (btn) btn.disabled = true;

      try {
        const res = await api("PUT", "/admin/config", { changes: pendingChanges });
        const data = await res.json();
        if (res.ok) {
          if (fb) {
            fb.classList.remove("hidden", "error");
            fb.classList.add("success");
            fb.textContent = data.message;
          }
          toast("Configuration saved successfully.", "success");

          // Update local field values
          for (const [key, val] of Object.entries(pendingChanges)) {
            const field = configFields.find((f) => f.key === key);
            if (field) field.value = val;
          }
          pendingChanges = {};
          renderConfigFields(configFields);
          updateConfigActions();
        } else {
          throw new Error(data.detail || "Save failed");
        }
      } catch (e) {
        if (fb) {
          fb.classList.remove("hidden", "success");
          fb.classList.add("error");
          fb.textContent = "Save failed: " + e.message;
        }
        toast("Failed to save configuration.", "error");
      }
      if (btn) btn.disabled = false;
      if (fb) setTimeout(() => fb.classList.add("hidden"), 8000);
    });
  }

  // Discard config changes
  if ($("#config-discard-btn")) {
    $("#config-discard-btn").addEventListener("click", () => {
      pendingChanges = {};
      renderConfigFields(configFields);
      updateConfigActions();
      toast("Changes discarded.", "info");
    });
  }

  // Config search/filter
  if ($("#config-search")) {
    let searchTimeout = null;
    $("#config-search").addEventListener("input", () => {
      clearTimeout(searchTimeout);
      searchTimeout = setTimeout(() => renderConfigFields(configFields), 150);
    });
  }
  if ($("#config-category-filter")) {
    $("#config-category-filter").addEventListener("change", () => {
      renderConfigFields(configFields);
    });
  }

  // -- WLED Integration -------------------------------------------------------

  function showWledFeedback(message, type) {
    const fb = $("#wled-feedback");
    if (!fb) return;
    fb.classList.remove("hidden", "success", "error");
    fb.classList.add(type);
    fb.textContent = message;
    setTimeout(() => fb.classList.add("hidden"), 6000);
  }

  if ($("#wled-test-conn")) {
    $("#wled-test-conn").addEventListener("click", async function () {
      const statusEl = $("#wled-status");
      this.disabled = true;
      try {
        const res = await api("GET", "/admin/wled/test");
        const data = await res.json();
        if (data.ok && data.info) {
          const info = data.info;
          statusEl.innerHTML =
            '<span style="color:var(--clr-success)">Connected</span>' +
            ` &mdash; ${escapeHtml(info.name)} (v${escapeHtml(info.version)}, ${info.led_count} LEDs)`;
          showWledFeedback("Connection successful!", "success");
          toast("WLED device connected.", "success");
        } else {
          statusEl.innerHTML = '<span style="color:var(--clr-danger)">Failed</span>' +
            ` &mdash; ${escapeHtml(data.error || "Unknown error")}`;
          showWledFeedback("Connection failed: " + (data.error || "Unknown error"), "error");
          toast("WLED connection failed.", "error");
        }
      } catch (e) {
        statusEl.innerHTML = '<span style="color:var(--clr-danger)">Error</span>';
        showWledFeedback("Connection failed: " + e.message, "error");
        toast("WLED connection failed.", "error");
      }
      this.disabled = false;
    });
  }

  if ($("#wled-on")) {
    $("#wled-on").addEventListener("click", async function () {
      this.disabled = true;
      try {
        const res = await api("POST", "/admin/wled/on");
        const data = await res.json();
        if (data.ok) {
          showWledFeedback("LED strip turned on.", "success");
          toast("WLED on.", "success");
        } else {
          showWledFeedback(data.error || "Failed to turn on.", "error");
        }
      } catch (e) {
        showWledFeedback("Failed: " + e.message, "error");
      }
      this.disabled = false;
    });
  }

  if ($("#wled-off")) {
    $("#wled-off").addEventListener("click", async function () {
      this.disabled = true;
      try {
        const res = await api("POST", "/admin/wled/off");
        const data = await res.json();
        if (data.ok) {
          showWledFeedback("LED strip turned off.", "success");
          toast("WLED off.", "success");
        } else {
          showWledFeedback(data.error || "Failed to turn off.", "error");
        }
      } catch (e) {
        showWledFeedback("Failed: " + e.message, "error");
      }
      this.disabled = false;
    });
  }

  if ($("#wled-trigger-preset")) {
    $("#wled-trigger-preset").addEventListener("click", async function () {
      const input = $("#wled-preset-input");
      const presetId = parseInt(input.value, 10);
      if (!presetId || presetId < 1 || presetId > 250) {
        showWledFeedback("Enter a preset ID between 1 and 250.", "error");
        return;
      }
      this.disabled = true;
      try {
        const res = await api("POST", `/admin/wled/preset/${presetId}`);
        const data = await res.json();
        if (data.ok) {
          showWledFeedback(`Preset ${presetId} triggered.`, "success");
          toast(`WLED preset ${presetId} triggered.`, "success");
        } else {
          showWledFeedback(data.error || `Failed to trigger preset ${presetId}.`, "error");
        }
      } catch (e) {
        showWledFeedback("Failed: " + e.message, "error");
      }
      this.disabled = false;
    });
  }

  // -- Init -------------------------------------------------------------------
  tryAutoLogin();
})();
