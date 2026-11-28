/**
 * AdminTab: Operational control plane inside the Web UI.
 * Covers Pre-Market selectable steps, Core service selective launch/stop,
 * Per-Mode Granular Strategy & Track Matrix, Freeze/Unfreeze, Live Stop,
 * Groww TOTP token refresh, Reconcile, Post-Market actions, and Audit Trail.
 */

window.AdminTab = {
  configData: null,
  initialized: false,
  selectedActivity: "premarket",

  init() {
    if (this.initialized) return;
    this.initialized = true;
    this.bindEvents();
    this.setActivity(this.selectedActivity);
  },

  setActivity(activity) {
    this.selectedActivity = activity || "premarket";
    const sel = document.getElementById("admin-activity-select");
    if (sel && sel.value !== this.selectedActivity) sel.value = this.selectedActivity;
    document.querySelectorAll(".admin-activity-pill").forEach((btn) => {
      btn.classList.toggle("active", btn.getAttribute("data-activity") === this.selectedActivity);
    });
    document.querySelectorAll(".admin-activity-panel").forEach((panel) => {
      const panelAct = panel.getAttribute("data-activity");
      if (this.selectedActivity === "all" || panelAct === this.selectedActivity) {
        panel.style.display = "";
      } else {
        panel.style.display = "none";
      }
    });
  },

  onActivate() {
    this.refreshConfig();
  },

  async refreshConfig() {
    try {
      const res = await fetch("/api/admin/config");
      if (!res.ok) return;
      const data = await res.json();
      this.configData = data;
      this.renderAll(data);
    } catch (e) {
      console.error("Failed loading admin config:", e);
    }
  },

  renderAll(data) {
    this.renderStatusBar(data);
    this.renderPremarketSteps(data.premarket_steps || []);
    this.renderCoreServices(data.core_services || [], data.running_processes || {});
    this.renderModeTrackMatrix(data);
    this.renderPostmarketActions(data.postmarket_actions || []);
    this.renderRunningProcesses(data.running_processes || {});
    this.renderAuditTable(data.recent_audit || []);

    const dtInput = document.getElementById("admin-premarket-date");
    if (dtInput && !dtInput.value) dtInput.value = data.session_date || "";
    const pmDate = document.getElementById("admin-postmarket-date");
    if (pmDate && !pmDate.value) pmDate.value = data.session_date || "";
  },

  renderStatusBar(data) {
    const authEl = document.getElementById("admin-auth-badges");
    if (authEl && data.auth) {
      const up = data.auth.upstox_access_token || {};
      const gw = data.auth.groww_access_token || {};
      authEl.innerHTML = `
        <span class="badge ${up.configured ? "badge-win" : "badge-loss"}">Upstox Token: ${up.masked || "Not Set"}</span>
        <span class="badge ${gw.configured ? "badge-win" : "badge-loss"}">Groww Token: ${gw.masked || "Not Set"}</span>
        <span class="badge ${data.core_rms_running ? "badge-win" : "badge-closed"}">Core RMS: ${data.core_rms_running ? "ONLINE (Decoupled)" : "OFFLINE"}</span>
        <span class="badge ${data.rest_fallback_enabled ? "badge-reason" : "badge-closed"}">REST Fallback: ${data.rest_fallback_enabled ? "ENABLED" : "DISABLED"}</span>
      `;
    }

    const frzEl = document.getElementById("admin-freeze-badges");
    if (frzEl && data.freeze_state) {
      const chips = ["live", "paper", "paper_delayed", "sim"].map((m) => {
        const frozen = Boolean(data.freeze_state[m]);
        return `<span class="badge ${frozen ? "badge-loss" : "badge-win"}">${m.toUpperCase()}: ${frozen ? "FROZEN" : "ACTIVE"}</span>`;
      });
      frzEl.innerHTML = chips.join(" ");
    }
  },

  renderPremarketSteps(steps) {
    const box = document.getElementById("admin-premarket-steps");
    if (!box || box.dataset.rendered === "1") return;
    box.dataset.rendered = "1";
    box.innerHTML = steps
      .map(
        (s) => `
      <label class="admin-check-item">
        <input type="checkbox" class="admin-pm-step-cb" value="${s.id}" checked />
        <div>
          <div style="font-weight:600; color:#fff;">${s.label}</div>
          <div style="font-size:11px; color:var(--text-dim);">${s.description}</div>
        </div>
      </label>`
      )
      .join("");
  },

  renderCoreServices(services, runningProcs) {
    const box = document.getElementById("admin-core-services");
    if (!box) return;
    const prevChecked = new Set(
      Array.from(document.querySelectorAll(".admin-core-cb:checked")).map((c) => c.value)
    );
    const hasPrev = document.querySelectorAll(".admin-core-cb").length > 0;

    box.innerHTML = services
      .map((svc) => {
        const proc = runningProcs[svc.id];
        const isAlive = proc && proc.alive;
        const checked = hasPrev ? prevChecked.has(svc.id) : svc.id !== "market_recorder";
        const badge = isAlive
          ? `<span class="badge badge-win" style="font-size:10px;">RUNNING (${proc.pid})</span>`
          : `<span class="badge badge-closed" style="font-size:10px;">STOPPED</span>`;
        return `
      <label class="admin-check-item">
        <input type="checkbox" class="admin-core-cb" value="${svc.id}" ${checked ? "checked" : ""} />
        <div style="flex:1;">
          <div style="display:flex; justify-content:space-between; align-items:center; gap:6px;">
            <span style="font-weight:600; color:#fff;">${svc.label}</span>
            ${badge}
          </div>
          <div style="font-size:11px; color:var(--text-dim);">${svc.description}</div>
        </div>
      </label>`;
      })
      .join("");
  },

  renderModeTrackMatrix(data) {
    const container = document.getElementById("admin-mode-track-matrix");
    if (!container || container.dataset.rendered === "1") return;
    container.dataset.rendered = "1";

    const modes = data.modes || ["paper", "paper_delayed", "live"];
    const tracks = data.tracks || [];
    const runningProcs = data.running_processes || {};

    container.innerHTML = modes
      .map((mode) => {
        const modeChecked = mode === "paper" ? "checked" : "";
        const trackCheckboxes = tracks
          .map((t) => {
            const checked = t.enabled_default ? "checked" : "";
            const procRole = `exec_${mode}_${t.name}`;
            const isUp = runningProcs[procRole]?.alive;
            return `
          <label class="track-chip-opt" title="${t.type} | ${t.moneyness} (${t.source})">
            <input type="checkbox" class="admin-track-cb" data-mode="${mode}" value="${t.name}" ${checked} />
            <span style="flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${t.name}</span>
            ${isUp ? `<span style="color:var(--accent-green);">●</span>` : ""}
          </label>`;
          })
          .join("");

        return `
      <div class="mode-track-box">
        <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px;">
          <label style="display:flex; align-items:center; gap:8px; cursor:pointer;">
            <input type="checkbox" class="admin-mode-cb" value="${mode}" ${modeChecked} />
            <span class="badge badge-${mode}">${mode.toUpperCase()}</span>
            <span style="font-size:11px; color:var(--text-muted);">Select individual tracks for ${mode}</span>
          </label>
          <div class="pill-group">
            <button type="button" class="pill-btn admin-mode-sel-btn" data-mode="${mode}" data-sel="all">All</button>
            <button type="button" class="pill-btn admin-mode-sel-btn" data-mode="${mode}" data-sel="defaults">Defaults</button>
            <button type="button" class="pill-btn admin-mode-sel-btn" data-mode="${mode}" data-sel="none">None</button>
          </div>
        </div>
        <div class="track-chip-grid">${trackCheckboxes}</div>
      </div>`;
      })
      .join("");

    // Wire per-mode All/Defaults/None buttons & show/hide Live confirm
    container.querySelectorAll(".admin-mode-sel-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const m = btn.getAttribute("data-mode");
        const sel = btn.getAttribute("data-sel");
        const defaults = new Set((this.configData && this.configData.default_tracks) || []);
        container.querySelectorAll(`.admin-track-cb[data-mode="${m}"]`).forEach((cb) => {
          if (sel === "all") cb.checked = true;
          else if (sel === "none") cb.checked = false;
          else cb.checked = defaults.has(cb.value);
        });
      });
    });

    container.querySelectorAll(".admin-mode-cb").forEach((cb) => {
      cb.addEventListener("change", () => this.toggleLiveGuard());
    });
    this.toggleLiveGuard();
  },

  toggleLiveGuard() {
    const liveChecked = document.querySelector('.admin-mode-cb[value="live"]')?.checked;
    const guard = document.getElementById("admin-live-guard");
    if (guard) guard.style.display = liveChecked ? "block" : "none";
  },

  renderPostmarketActions(actions) {
    const box = document.getElementById("admin-postmarket-actions");
    if (!box || box.dataset.rendered === "1") return;
    box.dataset.rendered = "1";
    box.innerHTML = actions
      .map(
        (a) => `
      <label class="admin-check-item">
        <input type="checkbox" class="admin-postm-cb" value="${a.id}" ${a.id !== "report" ? "checked" : ""} />
        <div>
          <div style="font-weight:600; color:#fff;">${a.label}</div>
          <div style="font-size:11px; color:var(--text-dim);">${a.description}</div>
        </div>
      </label>`
      )
      .join("");
  },

  renderRunningProcesses(runningProcs) {
    const tbody = document.getElementById("admin-procs-tbody");
    if (!tbody) return;
    const entries = Object.values(runningProcs || {});
    if (entries.length === 0) {
      tbody.innerHTML = `<tr><td colspan="5" class="text-center" style="color:var(--text-dim); padding:12px;">No registered processes in logs/pids.json.</td></tr>`;
      return;
    }
    tbody.innerHTML = entries
      .sort((a, b) => String(a.tier).localeCompare(String(b.tier)) || String(a.role).localeCompare(String(b.role)))
      .map(
        (p) => `
      <tr>
        <td><span class="badge badge-closed">${p.tier || "SYSTEM"}</span></td>
        <td class="font-mono" style="color:#fff;">${p.role}</td>
        <td class="font-mono">${p.pid || "-"}</td>
        <td><span class="badge ${p.alive ? "badge-win" : "badge-loss"}">${p.alive ? "RUNNING" : "DEAD"}</span></td>
        <td class="text-right">
          <button type="button" class="pill-btn admin-stop-proc-btn" data-role="${p.role}" style="color:var(--accent-red);">Stop</button>
        </td>
      </tr>`
      )
      .join("");

    tbody.querySelectorAll(".admin-stop-proc-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const role = btn.getAttribute("data-role");
        await this.postJson("/api/admin/process/stop", { role }, `Stopped ${role}`);
      });
    });
  },

  renderAuditTable(rows) {
    const tbody = document.getElementById("admin-audit-tbody");
    if (!tbody) return;
    if (!rows || rows.length === 0) {
      tbody.innerHTML = `<tr><td colspan="4" class="text-center" style="color:var(--text-dim); padding:12px;">No admin actions audited yet.</td></tr>`;
      return;
    }
    tbody.innerHTML = rows
      .map(
        (r) => `
      <tr>
        <td class="font-mono" style="font-size:11px; color:var(--text-dim);">${String(r.timestamp || "").replace("T", " ").slice(0, 19)}</td>
        <td class="font-mono" style="color:#fff;">${r.action}</td>
        <td><span class="badge ${r.status === "ok" || r.status === "success" ? "badge-win" : "badge-loss"}">${String(r.status || "").toUpperCase()}</span></td>
        <td class="font-mono" style="font-size:11px; color:var(--text-muted);">${JSON.stringify(r.detail || {})}</td>
      </tr>`
      )
      .join("");
  },

  setBanner(id, message, ok = true) {
    const el = document.getElementById(id);
    if (!el) return;
    el.style.display = "block";
    el.style.color = ok ? "var(--accent-green)" : "var(--accent-red)";
    el.innerText = message;
  },

  async postJson(url, body, successPrefix, bannerId = null) {
    try {
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}),
      });
      const data = await res.json();
      if (!res.ok || data.status === "error") {
        const err = data.error || (data.errors && data.errors.join("; ")) || "Operation failed";
        if (bannerId) this.setBanner(bannerId, `Error: ${err}`, false);
        window.App?.showToast?.(err, true);
      } else {
        const summary = `${successPrefix} (${JSON.stringify(data.spawned ? { spawned: data.spawned.length } : data.status)})`;
        if (bannerId) this.setBanner(bannerId, summary, true);
        window.App?.showToast?.(successPrefix, false);
      }
      await this.refreshConfig();
      if (window.HealthTab) window.HealthTab.pollStatus();
      return data;
    } catch (e) {
      if (bannerId) this.setBanner(bannerId, `Request failed: ${e}`, false);
      return null;
    }
  },

  bindEvents() {
    document.getElementById("admin-refresh-btn")?.addEventListener("click", () => this.refreshConfig());

    document.getElementById("admin-activity-select")?.addEventListener("change", (e) => {
      this.setActivity(e.target.value || "premarket");
    });

    document.querySelectorAll(".admin-activity-pill").forEach((btn) => {
      btn.addEventListener("click", () => {
        this.setActivity(btn.getAttribute("data-activity") || "premarket");
      });
    });

    // Pre-market launch
    document.getElementById("admin-pm-select-all")?.addEventListener("click", () => {
      document.querySelectorAll(".admin-pm-step-cb").forEach((c) => (c.checked = true));
    });
    document.getElementById("admin-pm-select-none")?.addEventListener("click", () => {
      document.querySelectorAll(".admin-pm-step-cb").forEach((c) => (c.checked = false));
    });
    document.getElementById("admin-run-premarket-btn")?.addEventListener("click", async () => {
      const steps = Array.from(document.querySelectorAll(".admin-pm-step-cb:checked")).map((c) => c.value);
      if (steps.length === 0) {
        this.setBanner("admin-premarket-result", "Select at least one pre-market step.", false);
        return;
      }
      this.setBanner("admin-premarket-result", `Running pre-market (${steps.join(", ")})...`, true);
      await this.postJson(
        "/api/admin/premarket",
        {
          steps,
          exchange: document.getElementById("admin-premarket-exchange")?.value || "NSE",
          session_date: document.getElementById("admin-premarket-date")?.value || "",
          volume_days: Number(document.getElementById("admin-premarket-days")?.value || 30),
          reset: Boolean(document.getElementById("admin-premarket-reset")?.checked),
        },
        "Pre-market pipeline finished",
        "admin-premarket-result"
      );
    });

    // Core tier launch / stop
    document.getElementById("admin-start-core-btn")?.addEventListener("click", async () => {
      const services = Array.from(document.querySelectorAll(".admin-core-cb:checked")).map((c) => c.value);
      if (services.length === 0) {
        this.setBanner("admin-core-result", "Select at least one Core service.", false);
        return;
      }
      await this.postJson(
        "/api/admin/core/start",
        {
          services,
          feed_mode: document.getElementById("admin-core-feed-mode")?.value || "ws",
          debug: Boolean(document.getElementById("admin-core-debug")?.checked),
        },
        "Core services launched",
        "admin-core-result"
      );
    });
    document.getElementById("admin-stop-selected-core-btn")?.addEventListener("click", async () => {
      const services = Array.from(document.querySelectorAll(".admin-core-cb:checked")).map((c) => c.value);
      await this.postJson("/api/admin/core/stop", { services }, "Stopped selected core services", "admin-core-result");
    });
    document.getElementById("admin-stop-all-core-btn")?.addEventListener("click", async () => {
      await this.postJson("/api/admin/core/stop", {}, "Stopped all core tier services", "admin-core-result");
    });

    // Strategy granular launch / stop
    document.getElementById("admin-start-strategy-btn")?.addEventListener("click", async () => {
      const modes = Array.from(document.querySelectorAll(".admin-mode-cb:checked")).map((c) => c.value);
      const tracks_by_mode = {};
      modes.forEach((m) => {
        tracks_by_mode[m] = Array.from(document.querySelectorAll(`.admin-track-cb[data-mode="${m}"]:checked`)).map((c) => c.value);
      });
      await this.postJson(
        "/api/admin/strategy/start",
        {
          modes,
          tracks_by_mode,
          continue_state: Boolean(document.getElementById("admin-strat-continue")?.checked),
          spawn_scoring: Boolean(document.getElementById("admin-strat-scoring")?.checked),
          allow_fallback_rms: Boolean(document.getElementById("admin-strat-fallback-rms")?.checked),
          capital: document.getElementById("admin-strat-capital")?.value || null,
          eod_freeze_time: document.getElementById("admin-strat-freeze-time")?.value || null,
          eod_time: document.getElementById("admin-strat-eod-time")?.value || null,
          confirm_live: Boolean(document.getElementById("admin-live-confirm-cb")?.checked),
          confirm_live_phrase: document.getElementById("admin-live-confirm-phrase")?.value || "",
        },
        "Strategy & tracks launched",
        "admin-strategy-result"
      );
    });
    document.getElementById("admin-stop-strategy-btn")?.addEventListener("click", async () => {
      const modes = Array.from(document.querySelectorAll(".admin-mode-cb:checked")).map((c) => c.value);
      await this.postJson("/api/admin/strategy/stop", { modes }, "Stopped selected strategy modes", "admin-strategy-result");
    });
    document.getElementById("admin-stop-all-strategy-btn")?.addEventListener("click", async () => {
      await this.postJson("/api/admin/strategy/stop", {}, "Stopped all strategies & tracks", "admin-strategy-result");
    });

    // Freeze / Unfreeze quick pills
    document.querySelectorAll(".admin-freeze-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const mode = btn.getAttribute("data-mode");
        const act = btn.getAttribute("data-act");
        await this.postJson("/api/admin/action", { action: act, params: { mode } }, `${act.toUpperCase()} (${mode}) applied`);
      });
    });

    // Emergency Stop Live
    document.getElementById("admin-stop-live-btn")?.addEventListener("click", async () => {
      const phrase = document.getElementById("admin-stop-live-phrase")?.value || "";
      const stopProcs = Boolean(document.getElementById("admin-stop-live-procs")?.checked);
      await this.postJson(
        "/api/admin/action",
        { action: "stop_live", params: { confirm_phrase: phrase, stop_processes: stopProcs } },
        "LIVE execution frozen/stopped",
        "admin-risk-result"
      );
    });

    // REST Fallback & Reconcile & Stop All
    document.getElementById("admin-rf-enable-btn")?.addEventListener("click", () =>
      this.postJson("/api/admin/action", { action: "rest_fallback", params: { state: "enable" } }, "REST fallback enabled", "admin-risk-result")
    );
    document.getElementById("admin-rf-disable-btn")?.addEventListener("click", () =>
      this.postJson("/api/admin/action", { action: "rest_fallback", params: { state: "disable" } }, "REST fallback disabled", "admin-risk-result")
    );
    document.getElementById("admin-reconcile-btn")?.addEventListener("click", () =>
      this.postJson("/api/admin/action", { action: "reconcile", params: {} }, "Groww broker reconciliation finished", "admin-risk-result")
    );
    document.getElementById("admin-stop-all-btn")?.addEventListener("click", () =>
      this.postJson("/api/admin/action", { action: "stop_all", params: {} }, "Stopped all registered processes", "admin-risk-result")
    );

    // Groww TOTP token refresh
    document.getElementById("admin-groww-token-btn")?.addEventListener("click", async () => {
      const totp = document.getElementById("admin-groww-totp")?.value || "";
      await this.postJson(
        "/api/admin/action",
        { action: "refresh_groww_token", params: { totp } },
        "Groww daily token refreshed & saved",
        "admin-risk-result"
      );
    });

    // Post-market launch
    document.getElementById("admin-run-postmarket-btn")?.addEventListener("click", async () => {
      const actions = Array.from(document.querySelectorAll(".admin-postm-cb:checked")).map((c) => c.value);
      if (actions.length === 0) {
        this.setBanner("admin-postmarket-result", "Select at least one post-market action.", false);
        return;
      }
      await this.postJson(
        "/api/admin/postmarket",
        {
          actions,
          session_date: document.getElementById("admin-postmarket-date")?.value || "",
          keep_raw: Boolean(document.getElementById("admin-postmarket-keep-raw")?.checked),
          archive_all: Boolean(document.getElementById("admin-postmarket-all")?.checked),
        },
        "Post-market tasks finished",
        "admin-postmarket-result"
      );
    });
  },
};
