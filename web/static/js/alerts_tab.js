/**
 * Alerts Tab Controller: Live alerts monitoring, top 5 cards display,
 * session alerts log table, and dedicated 5m candlestick chart modal with trigger markers.
 */
window.AlertsTab = {
  initialized: false,
  pollTimer: null,
  currentDate: "",
  currentDirFilter: "all",
  searchQuery: "",
  alertsData: null,
  chart: null,
  currentChartMode: "candle",
  currentModalAlert: null,

  init() {
    if (this.initialized) return;
    this.initialized = true;

    // Filter controls
    const dirPills = document.querySelectorAll(".alert-dir-pill");
    dirPills.forEach((btn) => {
      btn.addEventListener("click", () => {
        dirPills.forEach((p) => p.classList.remove("active"));
        btn.classList.add("active");
        this.currentDirFilter = btn.dataset.dir || "all";
        this.renderTable();
      });
    });

    const searchInput = document.getElementById("alerts-search-input");
    if (searchInput) {
      searchInput.addEventListener("input", (e) => {
        this.searchQuery = (e.target.value || "").trim().toLowerCase();
        this.renderTable();
      });
    }

    const refreshBtn = document.getElementById("alerts-refresh-btn");
    if (refreshBtn) {
      refreshBtn.addEventListener("click", () => {
        this.loadAlerts();
      });
    }

    const dateSelect = document.getElementById("alerts-date-select");
    if (dateSelect) {
      dateSelect.addEventListener("change", (e) => {
        this.currentDate = e.target.value;
        this.loadAlerts();
      });
    }

    // Modal elements
    const modal = document.getElementById("alert-chart-modal");
    const closeBtn = document.getElementById("acm-close");
    if (closeBtn && modal) {
      closeBtn.addEventListener("click", () => modal.classList.remove("active"));
      modal.addEventListener("click", (e) => {
        if (e.target === modal) modal.classList.remove("active");
      });
    }

    const btnCandle = document.getElementById("acm-mode-candle");
    const btnLine = document.getElementById("acm-mode-line");
    if (btnCandle && btnLine) {
      btnCandle.addEventListener("click", () => {
        btnCandle.classList.add("active");
        btnLine.classList.remove("active");
        this.currentChartMode = "candle";
        if (this.chart) this.chart.setMode("candle");
      });
      btnLine.addEventListener("click", () => {
        btnLine.classList.add("active");
        btnCandle.classList.remove("active");
        this.currentChartMode = "line";
        if (this.chart) this.chart.setMode("line");
      });
    }

    const canvas = document.getElementById("acm-canvas");
    if (canvas && window.CandleChart) {
      this.chart = new window.CandleChart(canvas);
    }
  },

  async onActivate() {
    this.init();
    await this.populateDates();
    await this.loadAlerts();

    // Start polling every 3 seconds while on alerts tab
    if (this.pollTimer) clearInterval(this.pollTimer);
    this.pollTimer = setInterval(() => {
      this.loadAlerts(true);
    }, 3000);
  },

  onDeactivate() {
    if (this.pollTimer) {
      clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
  },

  async populateDates() {
    const select = document.getElementById("alerts-date-select");
    if (!select) return;

    try {
      const res = await fetch("/api/historical/dates");
      if (res.ok) {
        const data = await res.json();
        const dates = data.dates || [];
        select.innerHTML = "";

        // Add today option first
        const todayStr = new Date().toISOString().split("T")[0];
        const optToday = document.createElement("option");
        optToday.value = todayStr;
        optToday.innerText = `Today (${todayStr})`;
        select.appendChild(optToday);

        for (const d of dates) {
          if (d.date !== todayStr) {
            const opt = document.createElement("option");
            opt.value = d.date;
            opt.innerText = d.date;
            select.appendChild(opt);
          }
        }

        if (!this.currentDate) {
          this.currentDate = todayStr;
        }
        select.value = this.currentDate;
      }
    } catch (e) {
      console.error("Failed to populate alert dates:", e);
    }
  },

  async loadAlerts(silent = false) {
    const targetDate = this.currentDate || "";
    try {
      const res = await fetch(`/api/alerts?date=${encodeURIComponent(targetDate)}`);
      if (res.ok) {
        const data = await res.json();
        this.alertsData = data;
        this.renderLatestCards(data.latest_5 || []);
        this.renderTable();

        const badge = document.getElementById("alerts-total-badge");
        if (badge) badge.innerText = `${data.total_count || 0} Alerts`;
      }
    } catch (e) {
      if (!silent) console.error("Failed to load alerts:", e);
    }
  },

  renderLatestCards(latest5) {
    const container = document.getElementById("alerts-latest-cards");
    if (!container) return;

    if (!latest5 || latest5.length === 0) {
      container.innerHTML = `
        <div class="text-dim" style="font-size:12px; grid-column:1/-1; padding:24px; text-align:center; background:var(--bg-card); border-radius:var(--radius-md); border:1px solid var(--border-subtle);">
          No setup alerts recorded yet for this session date.
        </div>
      `;
      return;
    }

    container.innerHTML = latest5
      .map((a, idx) => {
        const isBull = (a.direction || "").toUpperCase() === "BULL";
        const dirClass = isBull ? "alert-card-bull" : "alert-card-bear";
        const dirBadge = isBull
          ? `<span class="badge badge-win">BULL 📈</span>`
          : `<span class="badge badge-loss">BEAR 📉</span>`;
        const archName = (a.archetype || "MOMENTUM_BREAKOUT").replace(/_/g, " ");

        return `
          <div class="alert-card ${dirClass}" data-alert-index="${idx}">
            <div class="alert-card-header">
              <span class="alert-card-symbol">${a.symbol}</span>
              ${dirBadge}
            </div>
            <div class="alert-card-archetype">${archName}</div>
            <div class="alert-card-body">
              <span class="alert-card-price">₹${Number(a.trigger_price || 0).toFixed(2)}</span>
              <span style="font-size:11px; font-family:var(--font-mono); color:var(--text-dim);">${a.time_str || '-'}</span>
            </div>
            <div class="alert-card-metrics">
              <span>rVOL: <b style="color:var(--text-main);">${Number(a.rvol || 0).toFixed(1)}x</b></span>
              <span>CLV: <b style="color:var(--text-main);">${Number(a.clv || 0).toFixed(2)}</b></span>
              <span>ATR: <b style="color:var(--text-main);">₹${Number(a.atr || 0).toFixed(2)}</b></span>
            </div>
            <div class="alert-card-footer">
              <span>Alert #${(a.alert_id || "").slice(-6)}</span>
              <span style="color:var(--accent-cyan);">Inspect Chart 📊</span>
            </div>
          </div>
        `;
      })
      .join("");

    // Attach click listeners to cards
    const cards = container.querySelectorAll(".alert-card");
    cards.forEach((c) => {
      c.addEventListener("click", () => {
        const idx = parseInt(c.dataset.alertIndex, 10);
        if (latest5[idx]) {
          this.openAlertChart(latest5[idx]);
        }
      });
    });
  },

  renderTable() {
    const tbody = document.getElementById("alerts-table-tbody");
    if (!tbody || !this.alertsData) return;

    let alerts = this.alertsData.alerts || [];

    // Filter by direction
    if (this.currentDirFilter !== "all") {
      alerts = alerts.filter(
        (a) => (a.direction || "").toUpperCase() === this.currentDirFilter
      );
    }

    // Filter by search query
    if (this.searchQuery) {
      alerts = alerts.filter(
        (a) =>
          (a.symbol || "").toLowerCase().includes(this.searchQuery) ||
          (a.archetype || "").toLowerCase().includes(this.searchQuery)
      );
    }

    if (alerts.length === 0) {
      tbody.innerHTML = `
        <tr>
          <td colspan="9" class="text-center text-dim" style="padding:24px;">No alerts match filter criteria.</td>
        </tr>
      `;
      return;
    }

    tbody.innerHTML = alerts
      .map((a, idx) => {
        const isBull = (a.direction || "").toUpperCase() === "BULL";
        const dirBadge = isBull
          ? `<span class="badge badge-win">BULL 📈</span>`
          : `<span class="badge badge-loss">BEAR 📉</span>`;
        const archName = (a.archetype || "").replace(/_/g, " ");

        return `
          <tr style="cursor:pointer;" data-alert-id="${a.alert_id || idx}">
            <td class="text-center font-mono">${a.time_str || "-"}</td>
            <td><b>${a.symbol}</b></td>
            <td class="text-center">${dirBadge}</td>
            <td style="color:var(--accent-cyan); font-weight:600; font-size:11px;">${archName}</td>
            <td class="text-right font-mono font-bold">₹${Number(a.trigger_price || 0).toFixed(2)}</td>
            <td class="text-right font-mono">${Number(a.rvol || 0).toFixed(2)}x</td>
            <td class="text-right font-mono">${Number(a.clv || 0).toFixed(2)}</td>
            <td class="text-right font-mono">₹${Number(a.atr || 0).toFixed(2)}</td>
            <td class="text-center">
              <button class="btn btn-secondary btn-sm acm-inspect-btn" style="padding:3px 8px; font-size:11px;">Chart 📊</button>
            </td>
          </tr>
        `;
      })
      .join("");

    // Attach click listeners to rows
    const rows = tbody.querySelectorAll("tr");
    rows.forEach((r, idx) => {
      r.addEventListener("click", () => {
        if (alerts[idx]) {
          this.openAlertChart(alerts[idx]);
        }
      });
    });
  },

  async openAlertChart(alert) {
    const modal = document.getElementById("alert-chart-modal");
    if (!modal) return;
    this.currentModalAlert = alert;

    const titleEl = document.getElementById("acm-title");
    const dirBadge = document.getElementById("acm-dir-badge");
    const archBadge = document.getElementById("acm-arch-badge");
    const metaBar = document.getElementById("acm-meta-bar");

    const symbol = alert.symbol || "UNKNOWN";
    const isBull = (alert.direction || "").toUpperCase() === "BULL";
    const archName = (alert.archetype || "MOMENTUM_BREAKOUT").replace(/_/g, " ");

    if (titleEl) {
      titleEl.innerText = `${symbol} — Setup Alert (${archName})`;
    }

    if (dirBadge) {
      dirBadge.className = `badge ${isBull ? 'badge-win' : 'badge-loss'}`;
      dirBadge.innerText = isBull ? "BULL 📈" : "BEAR 📉";
    }

    if (archBadge) {
      archBadge.innerText = archName;
    }

    if (metaBar) {
      metaBar.innerHTML = `
        <div class="meta-item"><span class="meta-label">Trigger Price:</span> <span class="meta-val" style="color:var(--accent-green); font-size:13px; font-weight:700;">₹${Number(alert.trigger_price || 0).toFixed(2)}</span></div>
        <div class="meta-item"><span class="meta-label">Trigger Time:</span> <span class="meta-val">${alert.time_str || '-'}</span></div>
        <div class="meta-item"><span class="meta-label">Strict rVOL:</span> <span class="meta-val" style="color:var(--accent-cyan);">${Number(alert.rvol || 0).toFixed(2)}x</span></div>
        <div class="meta-item"><span class="meta-label">CLV Index:</span> <span class="meta-val">${Number(alert.clv || 0).toFixed(2)}</span></div>
        <div class="meta-item"><span class="meta-label">14-ATR:</span> <span class="meta-val">₹${Number(alert.atr || 0).toFixed(2)}</span></div>
        <div class="meta-item"><span class="meta-label">Timeframe:</span> <span class="meta-val" style="color:var(--accent-amber);">5-Minute Bars</span></div>
      `;
    }

    modal.classList.add("active");

    const canvas = document.getElementById("acm-canvas");
    if (!this.chart && canvas && window.CandleChart) {
      this.chart = new window.CandleChart(canvas);
    }

    // Fetch candles from alert service
    const targetDate = alert.date || this.currentDate || "";
    const tPrice = alert.trigger_price || 0;
    const tTs = alert.timestamp || 0;

    try {
      const url = `/api/alerts/candles?symbol=${encodeURIComponent(symbol)}&date=${encodeURIComponent(targetDate)}&trigger_price=${tPrice}&trigger_ts=${tTs}`;
      const res = await fetch(url);
      if (res.ok) {
        const data = await res.json();
        const candles = data.candles || [];
        const markers = data.markers || [];

        if (this.chart) {
          this.chart.setData(candles, markers, this.currentChartMode);
        }
      }
    } catch (e) {
      console.error("Failed to load alert candles:", e);
    }
  },
};
