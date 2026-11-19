/**
 * HistoricalTab: Historical date selection, track performance matrix, curves, and trade journals.
 */

window.HistoricalTab = {
  selectedDate: null,
  currentModeFilter: "all",
  selectedTrack: null,
  selectedMode: null,
  chart: null,
  overviewData: null,

  init() {
    this.bindControls();
    const canvas = document.getElementById("historical-chart-canvas");
    if (canvas && window.IntradayChart) {
      this.chart = new window.IntradayChart(canvas);
    }
  },

  onActivate() {
    this.loadDates();
  },

  bindControls() {
    const select = document.getElementById("historical-date-select");
    if (select) {
      select.addEventListener("change", (e) => {
        this.selectedDate = e.target.value;
        this.selectedTrack = null;
        this.selectedMode = null;
        this.fetchOverview();
      });
    }

    const pills = document.querySelectorAll(".hist-mode-pill");
    pills.forEach((p) => {
      p.addEventListener("click", () => {
        pills.forEach((x) => x.classList.remove("active"));
        p.classList.add("active");
        this.currentModeFilter = p.getAttribute("data-mode");
        this.renderTable();
      });
    });
  },

  async loadDates() {
    try {
      const res = await fetch("/api/historical/dates");
      if (res.ok) {
        const data = await res.json();
        const dates = data.dates || [];
        const select = document.getElementById("historical-date-select");
        if (!select) return;

        select.innerHTML = "";
        for (const d of dates) {
          const opt = document.createElement("option");
          opt.value = d.date;
          opt.innerText = `${d.date} ${d.has_parquet ? '⚡' : '📜'}`;
          select.appendChild(opt);
        }

        if (dates.length > 0 && !this.selectedDate) {
          this.selectedDate = dates[0].date;
          select.value = this.selectedDate;
        }

        this.fetchOverview();
      }
    } catch (e) {
      console.error("Failed to load historical dates:", e);
    }
  },

  async fetchOverview() {
    if (!this.selectedDate) return;
    try {
      const res = await fetch(`/api/historical/overview?date=${encodeURIComponent(this.selectedDate)}`);
      if (res.ok) {
        const data = await res.json();
        this.overviewData = data;
        this.renderStats(data.overall);
        this.renderTable();

        const detailPanel = document.getElementById("historical-detail-panel");
        if (detailPanel) detailPanel.style.display = "none";
      }
    } catch (e) {
      console.error("Failed to fetch historical overview:", e);
    }
  },

  renderStats(overall) {
    if (!overall) return;
    const netEl = document.getElementById("hist-stat-net-pnl");
    const winRateEl = document.getElementById("hist-stat-win-rate");
    const tradesEl = document.getElementById("hist-stat-trades");
    const feesEl = document.getElementById("hist-stat-fees");

    if (netEl) {
      netEl.innerText = window.App.formatINR(overall.net_pnl);
      netEl.className = "stat-value " + (overall.net_pnl >= 0 ? "val-profit" : "val-loss");
    }

    if (winRateEl) {
      winRateEl.innerText = `${overall.win_rate.toFixed(1)}%`;
      winRateEl.className = "stat-value " + (overall.win_rate >= 50 ? "val-profit" : "val-neutral");
    }

    if (tradesEl) {
      tradesEl.innerText = `${overall.trades} Trades`;
    }

    if (feesEl) {
      feesEl.innerText = window.App.formatINR(overall.brokerage, false);
    }
  },

  renderTable() {
    const tbody = document.getElementById("hist-track-tbody");
    if (!tbody || !this.overviewData || !this.overviewData.modes) return;

    tbody.innerHTML = "";
    const modes = this.overviewData.modes;

    for (const [mName, mData] of Object.entries(modes)) {
      if (this.currentModeFilter !== "all" && mName !== this.currentModeFilter) {
        continue;
      }

      for (const [trName, trStats] of Object.entries(mData.tracks || {})) {
        const tr = document.createElement("tr");
        const isSelected = this.selectedTrack === trName && this.selectedMode === mName;
        if (isSelected) tr.classList.add("selected");

        const netClass = trStats.net_pnl >= 0 ? "val-profit" : "val-loss";

        tr.innerHTML = `
          <td><span class="badge badge-${mName}">${mName.toUpperCase()}</span></td>
          <td style="font-weight:600; color:#fff;">${trName}</td>
          <td class="font-mono text-center">${trStats.trades}</td>
          <td class="font-mono text-center" style="color:var(--accent-green);">${trStats.wins}</td>
          <td class="font-mono text-center" style="color:var(--accent-amber);">${trStats.breakevens}</td>
          <td class="font-mono text-center" style="color:var(--accent-red);">${trStats.losses}</td>
          <td class="font-mono text-center font-bold">${trStats.win_rate.toFixed(1)}%</td>
          <td class="font-mono text-right">${window.App.formatINR(trStats.gross_pnl)}</td>
          <td class="font-mono text-right" style="color:var(--text-dim);">${window.App.formatINR(trStats.brokerage, false)}</td>
          <td class="font-mono text-right font-bold ${netClass}">${window.App.formatINR(trStats.net_pnl)}</td>
        `;

        tr.addEventListener("click", () => {
          this.selectedMode = mName;
          this.selectedTrack = trName;
          this.renderTable();
          this.fetchTrackDetail(mName, trName);
        });

        tbody.appendChild(tr);
      }
    }

    if (tbody.children.length === 0) {
      tbody.innerHTML = `<tr><td colspan="10" class="text-center" style="padding:24px; color:var(--text-dim);">No historical trade journals recorded for session ${this.selectedDate} under mode '${this.currentModeFilter}'.</td></tr>`;
    }
  },

  async fetchTrackDetail(mode, track) {
    try {
      const res = await fetch(
        `/api/historical/track/${encodeURIComponent(mode)}/${encodeURIComponent(track)}?date=${encodeURIComponent(this.selectedDate)}`
      );
      if (res.ok) {
        const data = await res.json();
        this.renderDetailPanel(data);
        const panel = document.getElementById("historical-detail-panel");
        if (panel) panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }
    } catch (e) {
      console.error("Failed to fetch historical track detail:", e);
    }
  },

  renderDetailPanel(data) {
    const panel = document.getElementById("historical-detail-panel");
    if (!panel) return;
    panel.style.display = "flex";

    const titleEl = document.getElementById("hist-detail-title");
    const badgeEl = document.getElementById("hist-detail-mode-badge");
    const netEl = document.getElementById("hist-detail-net");

    if (titleEl) titleEl.innerText = data.track;
    if (badgeEl) {
      badgeEl.className = `badge badge-${data.mode}`;
      badgeEl.innerText = data.mode.toUpperCase();
    }
    if (netEl) {
      const net = data.stats.net_pnl;
      netEl.innerText = window.App.formatINR(net);
      netEl.className = "stat-value " + (net >= 0 ? "val-profit" : "val-loss");
    }

    // Render Performance Curve
    if (this.chart) {
      this.chart.setData(data.performance_curve || []);
    }

    // Render Trades Table
    const tbody = document.getElementById("hist-trades-tbody");
    if (!tbody) return;
    tbody.innerHTML = "";

    const trades = data.trades || [];
    for (const tr of trades) {
      const row = document.createElement("tr");
      const pnlClass = tr.net_pnl >= 0 ? "val-profit" : "val-loss";
      const retClass = tr.return_pct >= 0 ? "val-profit" : "val-loss";

      const exitReasonBadge = tr.exit_reason
        ? `<span class="badge badge-reason">${tr.exit_reason}</span>`
        : `<span style="color:var(--text-dim);">-</span>`;

      row.innerHTML = `
        <td class="font-mono" style="font-size:11px; color:var(--text-dim);">${tr.trade_id.replace("TRD_", "")}</td>
        <td style="font-weight:600; color:#fff;">${tr.symbol}</td>
        <td class="font-mono">${tr.strike} ${tr.option_type}</td>
        <td class="font-mono text-center">${tr.quantity}</td>
        <td class="font-mono text-right">₹${tr.entry_price.toFixed(2)}</td>
        <td class="font-mono text-center">${tr.entry_time_str}</td>
        <td class="font-mono text-right">₹${tr.exit_price ? tr.exit_price.toFixed(2) : '-'}</td>
        <td class="font-mono text-center">${tr.exit_time_str}</td>
        <td class="text-center">${exitReasonBadge}</td>
        <td class="font-mono text-right ${retClass}">${window.App.formatPct(tr.return_pct)}</td>
        <td class="font-mono text-right" style="color:var(--text-dim);">₹${tr.brokerage.toFixed(2)}</td>
        <td class="font-mono text-right font-bold ${pnlClass}">${window.App.formatINR(tr.net_pnl)}</td>
      `;
      tbody.appendChild(row);
    }

    if (trades.length === 0) {
      tbody.innerHTML = `<tr><td colspan="12" class="text-center" style="padding:24px; color:var(--text-dim);">No individual trade logs found.</td></tr>`;
    }
  },
};
