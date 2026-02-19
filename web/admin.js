/**
 * ECLAW Admin Panel — JavaScript
 * Handles authentication, dashboard, game controls, queue management,
 * and live configuration editing.
 */
(function () {
  "use strict";

  // -- State ------------------------------------------------------------------
  let adminKey = sessionStorage.getItem("eclaw_admin_key") || "";
  let dashboardInterval = null;
  let configFields = []; // Loaded from server
  let pendingChanges = {}; // key -> new value

  // -- DOM Elements -----------------------------------------------------------
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => document.querySelectorAll(sel);

  const loginScreen = $("#login-screen");
  const adminPanel = $("#admin-panel");
  const loginForm = $("#login-form");
  const loginError = $("#login-error");
  const sidebar = $("#sidebar");

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
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
  }

  function formatTimeAgo(isoStr) {
    if (!isoStr) return "";
    const now = Date.now();
    const then = new Date(isoStr + (isoStr.endsWith("Z") ? "" : "Z")).getTime();
    const diffS = Math.floor((now - then) / 1000);
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
    setTimeout(() => el.remove(), 4200);
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
        sessionStorage.setItem("eclaw_admin_key", key);
        showAdmin();
        return true;
      }
    } catch (e) {
      // Network error
    }
    return false;
  }

  loginForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    loginError.textContent = "";
    const key = $("#admin-key-input").value.trim();
    if (!key) return;

    const ok = await verifyKey(key);
    if (!ok) {
      loginError.textContent = "Invalid admin key.";
    }
  });

  function logout() {
    adminKey = "";
    sessionStorage.removeItem("eclaw_admin_key");
    adminPanel.classList.add("hidden");
    loginScreen.classList.remove("hidden");
    clearInterval(dashboardInterval);
  }

  function showAdmin() {
    loginScreen.classList.add("hidden");
    adminPanel.classList.remove("hidden");
    refreshDashboard();
    dashboardInterval = setInterval(refreshDashboard, 5000);
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
      // Close mobile sidebar
      sidebar.classList.remove("open");
    });
  });

  if ($("#menu-toggle")) {
    $("#menu-toggle").addEventListener("click", () => {
      sidebar.classList.toggle("open");
    });
  }

  // Close sidebar on content click (mobile)
  if ($("#content")) {
    $("#content").addEventListener("click", () => {
      sidebar.classList.remove("open");
    });
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
      if (!res.ok) return;
      const data = await res.json();

      $("#dash-uptime").textContent = formatUptime(data.uptime_seconds);
      $("#dash-state").textContent = formatState(data.game_state);
      $("#dash-viewers").textContent = data.viewer_count;
      $("#dash-queue-size").textContent = data.queue ? data.queue.length : 0;

      // Stats
      const stats = data.stats || {};
      const total = (stats.total_completed || 0);
      const wins = (stats.wins || 0);
      $("#dash-total-games").textContent = total;
      $("#dash-win-rate").textContent = total > 0
        ? `${Math.round((wins / total) * 100)}%`
        : "--";

      // Flags
      toggleFlag("flag-paused", data.paused);
      toggleFlag("flag-gpio-locked", data.gpio_locked);
      toggleFlag("flag-mock", true); // Always show if mock_gpio detected from config

      // Active player
      if (data.active_player != null) {
        const activeEntry = (data.queue || []).find(
          (e) => e.state === "active" || e.state === "ready"
        );
        $("#dash-active-player").textContent = activeEntry
          ? activeEntry.name
          : `Entry #${data.active_player}`;
        $("#dash-try-info").textContent = `Try ${data.current_try}/${data.max_tries}`;
      } else {
        $("#dash-active-player").textContent = "None";
        $("#dash-try-info").textContent = "";
      }

      // Recent results
      renderDashRecent(data.recent_results || []);

    } catch (e) {
      // Silent — dashboard is non-critical
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
    if (!results.length) {
      el.innerHTML = '<p class="text-dim">No recent games</p>';
      return;
    }
    el.innerHTML = results
      .map((r) => {
        const isWin = r.result === "win";
        const badge = isWin ? "badge-win" : "badge-loss";
        const label = isWin ? "WIN" : (r.result || "").toUpperCase();
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
    btn.disabled = true;
    try {
      const res = await api("POST", path);
      const data = await res.json();
      fb.classList.remove("hidden", "error");
      fb.classList.add("success");
      fb.textContent = data.warning || data.message || "Action completed.";
      toast("Action completed.", "success");
      refreshDashboard();
    } catch (e) {
      fb.classList.remove("hidden", "success");
      fb.classList.add("error");
      fb.textContent = "Action failed: " + e.message;
      toast("Action failed.", "error");
    } finally {
      btn.disabled = false;
      setTimeout(() => fb.classList.add("hidden"), 5000);
    }
  }

  $("#ctrl-advance").addEventListener("click", function () {
    controlAction("/admin/advance", this);
  });
  $("#ctrl-pause").addEventListener("click", function () {
    controlAction("/admin/pause", this);
  });
  $("#ctrl-resume").addEventListener("click", function () {
    controlAction("/admin/resume", this);
  });
  $("#ctrl-estop").addEventListener("click", function () {
    if (confirm("Emergency Stop: This will lock all GPIO controls. Continue?")) {
      controlAction("/admin/emergency-stop", this);
    }
  });
  $("#ctrl-unlock").addEventListener("click", function () {
    controlAction("/admin/unlock", this);
  });

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
    if (!entries.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="text-dim">Queue is empty</td></tr>';
      return;
    }

    tbody.innerHTML = entries
      .map((e) => {
        const stateClass = e.state === "active" ? "active" : e.state === "ready" ? "ready" : "waiting";
        return `<tr>
          <td class="mono">${e.id}</td>
          <td>${escapeHtml(e.name)}</td>
          <td><span class="state-badge ${stateClass}">${e.state}</span></td>
          <td>${e.position}</td>
          <td>${formatTimeAgo(e.created_at)}</td>
          <td><button class="kick-btn" data-id="${e.id}" data-name="${escapeHtml(e.name)}">Kick</button></td>
        </tr>`;
      })
      .join("");

    // Attach kick handlers
    tbody.querySelectorAll(".kick-btn").forEach((btn) => {
      btn.addEventListener("click", async function () {
        const id = this.dataset.id;
        const name = this.dataset.name;
        if (!confirm(`Remove ${name} from the queue?`)) return;
        try {
          await api("POST", `/admin/kick/${id}`);
          toast(`${name} removed.`, "success");
          loadQueue();
          refreshDashboard();
        } catch (e) {
          toast("Failed to kick player.", "error");
        }
      });
    });
  }

  if ($("#refresh-queue")) {
    $("#refresh-queue").addEventListener("click", loadQueue);
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
    const container = $("#config-groups");
    const categoryFilter = $("#config-category-filter");

    // Build categories
    const categories = [...new Set(configFields.map((f) => f.category))];
    categoryFilter.innerHTML =
      '<option value="">All Categories</option>' +
      categories.map((c) => `<option value="${c}">${c}</option>`).join("");

    renderConfigFields(configFields);
    updateConfigActions();
  }

  function renderConfigFields(fields) {
    const container = $("#config-groups");
    const searchTerm = ($("#config-search").value || "").toLowerCase();
    const catFilter = $("#config-category-filter").value;

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
      container.innerHTML = '<p class="text-dim">No matching settings found.</p>';
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
            <div class="field-env-key">${f.env_key}</div>
            <div class="field-desc">${escapeHtml(f.description)}</div>
          </div>
          <div class="field-input-wrap">
            ${renderFieldInput(f, currentValue)}
            <div class="field-default">Default: ${formatDefault(f.default, f.type)}</div>
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
        .map((o) => `<option value="${o}"${String(currentValue) === o ? " selected" : ""}>${o}</option>`)
        .join("");
      return `<select data-config-key="${key}">${opts}</select>`;
    }

    const inputType = field.type === "integer" || field.type === "number" ? "number" : "text";
    const step = field.type === "number" ? 'step="any"' : "";
    const val = currentValue != null ? currentValue : "";
    const modified = key in pendingChanges ? " modified" : "";
    const isSensitive = field.key === "admin_api_key" ? ' type="password"' : "";

    if (isSensitive) {
      return `<input type="password" data-config-key="${key}" value="${escapeHtml(String(val))}" class="${modified}">`;
    }

    return `<input type="${inputType}" ${step} data-config-key="${key}" value="${escapeHtml(String(val))}" class="${modified}">`;
  }

  function formatDefault(def, type) {
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
    if (field.type === "integer") newValue = parseInt(newValue, 10) || 0;
    else if (field.type === "number") newValue = parseFloat(newValue) || 0;

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

    if (count > 0) {
      actions.classList.remove("hidden");
      countEl.textContent = `${count} change${count !== 1 ? "s" : ""}`;
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
      try {
        const res = await api("PUT", "/admin/config", { changes: pendingChanges });
        const data = await res.json();
        if (res.ok) {
          fb.classList.remove("hidden", "error");
          fb.classList.add("success");
          fb.textContent = data.message;
          toast("Configuration saved.", "success");

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
        fb.classList.remove("hidden", "success");
        fb.classList.add("error");
        fb.textContent = "Save failed: " + e.message;
        toast("Failed to save configuration.", "error");
      }
      setTimeout(() => fb.classList.add("hidden"), 8000);
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
    $("#config-search").addEventListener("input", () => {
      renderConfigFields(configFields);
    });
  }
  if ($("#config-category-filter")) {
    $("#config-category-filter").addEventListener("change", () => {
      renderConfigFields(configFields);
    });
  }

  // -- Init -------------------------------------------------------------------
  tryAutoLogin();
})();
