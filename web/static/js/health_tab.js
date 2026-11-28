/**
 * HealthTab: Process Status Monitor & Day's Structured Log Inspector.
 * Polls `/api/health/status` globally to turn header badge & overall banner RED immediately on error.
 */

window.HealthTab = {
  initialized: false,
  autoRefreshTimer: null,
  selectedFile: "ALL",
  selectedLevel: "ALL",
  selectedDate: "",
  searchQuery: "",

  init() {
    if (this.initialized) return;
    this.initialized = true;
    this.bindEvents();
    this.pollStatus();
    this.startBackgroundHeaderPoll();
  },

  onActivate() {
    this.pollStatus();
    this.fetchLogs();
  },

  startBackgroundHeaderPoll() {
    if (this.autoRefreshTimer) clearInterval(this.autoRefreshTimer);
    this.autoRefreshTimer = setInterval(() => {
      const auto = document.getElementById("health-auto-refresh")?.checked;
      if (window.App?.activeTab === "health") {
        if (auto !== false) {
          this.pollStatus();
          this.fetchLogs();
        }
      } else {
        // Lightweight global health check so header status badge flashes RED immediately on error
        this.pollStatus();
      }
    }, 6000);
  },

  async pollStatus() {
    try {
      const dateQ = this.selectedDate ? `?date=${encodeURIComponent(this.selectedDate)}` : "";
      const res = await fetch(`/api/health/status${dateQ}`);
      if (!res.ok) return;
      const data = await res.json();
      if (!this.selectedDate && data.date) {
        this.selectedDate = data.date;
        const dEl = document.getElementById("health-log-date");
        if (dEl && !dEl.value) dEl.value = data.date;
      }
      this.renderHeaderBadge(data);
      if (window.App?.activeTab === "health") {
        this.renderDashboard(data);
      }
    } catch (e) {
      console.error("Failed polling health status:", e);
    }
  },

  renderHeaderBadge(data) {
    const st = String(data.overall_status || "GREEN").toLowerCase();
    const dot = document.getElementById("nav-health-dot");
    const pill = document.getElementById("nav-health-badge");
    if (dot) dot.className = `health-dot ${st}`;
    if (pill) {
      pill.className = `health-nav-pill ${st}`;
      const errs = data.summary?.today_errors || 0;
      const dead = data.summary?.dead_processes || 0;
      const warns = data.summary?.today_warns || 0;
      if (st === "red") {
        pill.innerText = `RED (${errs}E/${dead}D)`;
      } else if (st === "yellow") {
        pill.innerText = `WARN (${warns})`;
      } else {
        pill.innerText = "OK";
      }
    }
  },

  renderDashboard(data) {
    const st = String(data.overall_status || "GREEN").toLowerCase();
    const banner = document.getElementById("health-overall-banner");
    const title = document.getElementById("health-overall-title");
    const reasonsEl = document.getElementById("health-overall-reasons");
    if (banner) banner.className = `health-banner ${st}`;
    if (title) {
      title.innerText =
        st === "red"
          ? "🚨 SYSTEM STATUS: RED (IMMEDIATE ATTENTION REQUIRED)"
          : st === "yellow"
          ? "⚠️ SYSTEM STATUS: YELLOW (WARNINGS / FREEZE ACTIVE)"
          : "✅ SYSTEM STATUS: GREEN (HEALTHY)";
      title.style.color =
        st === "red" ? "#fecaca" : st === "yellow" ? "var(--accent-amber)" : "var(--accent-green)";
    }
    if (reasonsEl) {
      reasonsEl.innerHTML = (data.reasons || []).map((r) => `<span>• ${r}</span>`).join(" &nbsp; ");
    }

    const s = data.summary || {};
    const setTxt = (id, val, cls = null) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.innerText = val;
      if (cls !== null) el.className = cls;
    };
    setTxt(
      "health-stat-procs",
      `${s.running_processes || 0} / ${s.registered_processes || 0}`,
      `stat-value ${(s.dead_processes || 0) > 0 ? "val-loss" : "val-profit"}`
    );
    setTxt(
      "health-stat-dead",
      String(s.dead_processes || 0),
      `stat-value ${(s.dead_processes || 0) > 0 ? "val-loss" : ""}`
    );
    setTxt(
      "health-stat-errors",
      String(s.today_errors || 0),
      `stat-value ${(s.today_errors || 0) > 0 ? "val-loss" : "val-profit"}`
    );
    setTxt(
      "health-stat-warns",
      String(s.today_warns || 0),
      `stat-value ${(s.today_warns || 0) > 0 ? "val-amber" : ""}`
    );

    this.renderProcesses(data.processes || []);
    this.renderInfra(data.ipc_sockets || [], data.shm || {});
    this.renderRecentErrors(data.recent_errors || []);
    this.renderLogFileDropdown(data.log_files || []);
  },

  renderProcesses(procs) {
    const tbody = document.getElementById("health-procs-tbody");
    if (!tbody) return;
    if (!procs.length) {
      tbody.innerHTML = `<tr><td colspan="10" class="text-center" style="padding:18px; color:var(--text-dim);">No registered processes in logs/pids.json. Launch services from Admin Ops or CLI.</td></tr>`;
      return;
    }
    tbody.innerHTML = procs
      .map(
        (p) => `
      <tr class="${!p.alive || p.today_errors > 0 ? "log-row-error" : ""}">
        <td><span class="badge badge-closed">${p.tier}</span></td>
        <td class="font-mono" style="font-weight:600; color:#fff;">${p.role}</td>
        <td class="font-mono">${p.pid}</td>
        <td class="text-center"><span class="badge ${p.alive ? "badge-win" : "badge-loss"}">${p.status}</span></td>
        <td class="font-mono text-right">${Number(p.cpu_pct || 0).toFixed(1)}%</td>
        <td class="font-mono text-right">${Number(p.rss_mb || 0).toFixed(1)} MB</td>
        <td class="font-mono text-center">${p.uptime || "-"}</td>
        <td class="font-mono text-center ${p.today_errors > 0 ? "val-loss font-bold" : ""}">${p.today_errors}</td>
        <td class="font-mono text-center" style="color:${p.today_warns > 0 ? "var(--accent-amber)" : "var(--text-dim)"};">${p.today_warns}</td>
        <td class="text-right">
          <button type="button" class="pill-btn health-view-log-btn" data-file="${p.log_file}">View Log</button>
          <button type="button" class="pill-btn health-kill-proc-btn" data-role="${p.role}" style="color:var(--accent-red);">Stop</button>
        </td>
      </tr>`
      )
      .join("");

    tbody.querySelectorAll(".health-view-log-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const f = btn.getAttribute("data-file") || "ALL";
        this.selectedFile = f;
        const sel = document.getElementById("health-log-file-select");
        if (sel) sel.value = f;
        this.fetchLogs();
      });
    });

    tbody.querySelectorAll(".health-kill-proc-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const role = btn.getAttribute("data-role");
        await fetch("/api/admin/process/stop", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ role }),
        });
        this.pollStatus();
      });
    });
  },

  renderInfra(sockets, shm) {
    const el = document.getElementById("health-infra-chips");
    if (!el) return;
    const parts = [];
    if (shm && shm.name) {
      const stateLabel = shm.active
        ? `ACTIVE (${shm.backend || "mmap"})`
        : shm.skipped
        ? "TEST_SKIP"
        : "OFFLINE (Start Core / Market Not Live)";
      parts.push(
        `<span class="badge ${shm.active ? "badge-win" : shm.skipped ? "badge-closed" : "badge-reason"}" title="${
          shm.reason || shm.path || shm.name
        }">SHM ${shm.name}: ${stateLabel}</span>`
      );
    }
    sockets.forEach((s) => {
      parts.push(
        `<span class="badge ${s.active ? "badge-win" : "badge-closed"}" title="${s.path}">${s.name}: ${
          s.active ? "BOUND" : "ABSENT"
        }</span>`
      );
    });
    el.innerHTML = parts.join(" ");
  },

  renderRecentErrors(errs) {
    const panel = document.getElementById("health-recent-errors-panel");
    const tbody = document.getElementById("health-recent-errors-tbody");
    if (!panel || !tbody) return;
    if (!errs.length) {
      panel.style.display = "none";
      tbody.innerHTML = "";
      return;
    }
    panel.style.display = "block";
    tbody.innerHTML = errs
      .map(
        (e) => `
      <tr class="log-row-error">
        <td>${e.time || "-"}</td>
        <td>${e.file}</td>
        <td>${e.logger}</td>
        <td style="color:#fecaca;">${this.escapeHtml(e.message)}</td>
      </tr>`
      )
      .join("");
  },

  renderLogFileDropdown(files) {
    const sel = document.getElementById("health-log-file-select");
    if (!sel) return;
    const cur = this.selectedFile || "ALL";
    sel.innerHTML =
      `<option value="ALL">All Today's Logs (${files.length} files)</option>` +
      files
        .map((f) => {
          const badge = f.today_errors > 0 ? ` [${f.today_errors} ERR]` : f.today_warns > 0 ? ` [${f.today_warns} WARN]` : "";
          return `<option value="${f.file}">${f.file}${badge} (${f.size_kb} KB)</option>`;
        })
        .join("");
    if (Array.from(sel.options).some((o) => o.value === cur)) {
      sel.value = cur;
    }
  },

  async fetchLogs() {
    try {
      const limit = Number(document.getElementById("health-log-limit")?.value || 300);
      const params = new URLSearchParams({
        file: this.selectedFile || "ALL",
        level: this.selectedLevel || "ALL",
        q: this.searchQuery || "",
        limit: String(limit),
      });
      if (this.selectedDate) params.set("date", this.selectedDate);

      const res = await fetch(`/api/health/logs?${params.toString()}`);
      if (!res.ok) return;
      const data = await res.json();
      this.renderLogRows(data);
    } catch (e) {
      console.error("Failed fetching logs:", e);
    }
  },

  renderLogRows(data) {
    const summaryEl = document.getElementById("health-log-counts");
    if (summaryEl && data.counts) {
      summaryEl.innerHTML = `
        <span class="badge badge-loss">Errors: ${data.counts.errors}</span>
        <span class="badge badge-reason">Warns: ${data.counts.warns}</span>
        <span class="badge badge-closed">Infos: ${data.counts.infos}</span>
        <span class="badge badge-entry">Matched: ${data.counts.matched}</span>
      `;
    }

    const tbody = document.getElementById("health-logs-tbody");
    if (!tbody) return;
    const entries = data.entries || [];
    if (!entries.length) {
      tbody.innerHTML = `<tr><td colspan="5" class="text-center" style="color:var(--text-dim); padding:18px;">No log entries matched for ${data.date} (${data.file} / ${data.level}).</td></tr>`;
      return;
    }

    tbody.innerHTML = entries
      .map((e) => {
        const rowCls = e.level === "ERROR" ? "log-row-error" : e.level === "WARN" ? "log-row-warn" : "";
        const lvlCls = e.level === "ERROR" ? "badge-loss" : e.level === "WARN" ? "badge-reason" : "badge-closed";
        return `
        <tr class="${rowCls}">
          <td style="white-space:nowrap; color:var(--text-dim);">${e.time || "-"}</td>
          <td><span class="badge ${lvlCls}">${e.level}</span></td>
          <td style="color:var(--accent-cyan);">${e.file}</td>
          <td style="color:var(--text-muted);">${e.logger}</td>
          <td style="white-space:pre-wrap; word-break:break-word; color:#f1f5f9;">${this.escapeHtml(e.message)}</td>
        </tr>`;
      })
      .join("");
  },

  escapeHtml(text) {
    return String(text || "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;");
  },

  bindEvents() {
    document.getElementById("health-refresh-btn")?.addEventListener("click", () => {
      this.pollStatus();
      this.fetchLogs();
    });

    document.getElementById("health-log-file-select")?.addEventListener("change", (e) => {
      this.selectedFile = e.target.value || "ALL";
      this.fetchLogs();
    });

    document.getElementById("health-log-date")?.addEventListener("change", (e) => {
      this.selectedDate = e.target.value || "";
      this.pollStatus();
      this.fetchLogs();
    });

    document.getElementById("health-log-limit")?.addEventListener("change", () => this.fetchLogs());

    document.querySelectorAll(".health-lvl-pill").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".health-lvl-pill").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        this.selectedLevel = btn.getAttribute("data-level") || "ALL";
        this.fetchLogs();
      });
    });

    document.getElementById("health-log-search")?.addEventListener("input", (e) => {
      this.searchQuery = e.target.value || "";
      this.fetchLogs();
    });
  },
};
