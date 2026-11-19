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
  includeBrokerage: true,
  datesLoaded: false,
  dateOverviewCache: {},
  trackDetailCache: {},

  init() {
    this.bindControls();
    const canvas = document.getElementById("historical-chart-canvas");
    if (canvas && window.IntradayChart) {
      this.chart = new window.IntradayChart(canvas);
    }

    const bToggle = document.getElementById("hist-brokerage-toggle");
    if (bToggle) {
      bToggle.addEventListener("change", (e) => {
        this.includeBrokerage = e.target.checked;
        if (this.chart) this.chart.setIncludeBrokerage(this.includeBrokerage);
      });
    }
  },

  onActivate() {
    if (!this.datesLoaded) {
      this.loadDates();
    } else {
      // Keep existing rendered state intact! Just refresh canvas DPI if detail is open
      if (this.selectedTrack && this.chart) {
        this.chart.render();
      }
    }
  },

  bindControls() {
    const select = document.getElementById("historical-date-select");
    if (select) {
      select.addEventListener("change", (e) => {
        this.selectedDate = e.target.value;
        this.selectedTrack = null;
        this.selectedMode = null;
        const detailPanel = document.getElementById("historical-detail-panel");
        if (detailPanel) detailPanel.style.display = "none";
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
          let tag = "";
          if (d.has_journals && d.has_parquet) tag = "⚡ Parquet & 📜 Journal";
          else if (d.has_journals) tag = "📜 Journal";
          else if (d.has_parquet) tag = "⚡ Parquet";
          opt.innerText = `${d.date} (${tag})`;
          select.appendChild(opt);
        }

        this.datesLoaded = true;

        if (dates.length > 0 && !this.selectedDate) {
          // Default to the latest session date with trade journals
          const datesWithJournals = dates.filter((d) => d.has_journals);
          if (datesWithJournals.length > 0) {
            this.selectedDate = datesWithJournals[datesWithJournals.length - 1].date;
          } else {
            this.selectedDate = dates[dates.length - 1].date;
          }
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

    // Check client-side cache first
    if (this.dateOverviewCache[this.selectedDate]) {
      this.overviewData = this.dateOverviewCache[this.selectedDate];
      this.renderStats(this.overviewData.overall);
      this.renderTable();
      const detailPanel = document.getElementById("historical-detail-panel");
      if (detailPanel && !this.selectedTrack) detailPanel.style.display = "none";
      return;
    }

    try {
      const res = await fetch(`/api/historical/overview?date=${encodeURIComponent(this.selectedDate)}`);
      if (res.ok) {
        const data = await res.json();
        this.overviewData = data;
        this.dateOverviewCache[this.selectedDate] = data;
        this.renderStats(data.overall);
        this.renderTable();

        const detailPanel = document.getElementById("historical-detail-panel");
        if (detailPanel && !this.selectedTrack) detailPanel.style.display = "none";
      }
    } catch (e) {
      console.error("Failed to fetch historical overview:", e);
    }
  },

  renderStats(overall) {
    if (!overall || !this.overviewData || !this.overviewData.modes) return;

    let targetNet = overall.net_pnl;
    let targetWinRate = overall.win_rate;
    let targetTrades = overall.trades || 0;
    let targetFees = overall.brokerage;

    if (this.currentModeFilter !== "all" && this.overviewData.modes[this.currentModeFilter]) {
      const mData = this.overviewData.modes[this.currentModeFilter];
      const tracks = Object.values(mData.tracks || {});
      const nTracks = tracks.length || 1;
      let mNet = 0, mWins = 0, mTrades = 0, mFees = 0;
      for (const t of tracks) {
        mNet += (t.net_pnl || 0);
        mWins += (t.wins || 0);
        mTrades += (t.trades || 0);
        mFees += (t.brokerage || 0);
      }
      targetNet = mNet / nTracks;
      targetWinRate = mTrades > 0 ? (mWins / mTrades) * 100 : 0;
      targetTrades = mTrades;
      targetFees = mFees;
    }

    const netEl = document.getElementById("hist-stat-net-pnl");
    const winRateEl = document.getElementById("hist-stat-win-rate");
    const tradesEl = document.getElementById("hist-stat-trades");
    const feesEl = document.getElementById("hist-stat-fees");

    if (netEl) {
      netEl.innerText = window.App.formatINR(targetNet);
      netEl.className = "stat-value " + (targetNet >= 0 ? "val-profit" : "val-loss");
    }

    if (winRateEl) {
      winRateEl.innerText = `${targetWinRate.toFixed(1)}%`;
      winRateEl.className = "stat-value " + (targetWinRate >= 50 ? "val-profit" : "val-neutral");
    }

    if (tradesEl) {
      tradesEl.innerText = `${targetTrades} Trades`;
    }

    if (feesEl) {
      feesEl.innerText = window.App.formatINR(targetFees, false);
    }
  },

  appendTrackRow(tbody, mName, trName, trStats) {
    const tr = document.createElement("tr");
    const isSelected = this.selectedTrack === trName && this.selectedMode === mName;
    if (isSelected) tr.classList.add("selected");

    const netClass = trStats.net_pnl >= 0 ? "val-profit" : "val-loss";
    const startBudget = trStats.start_budget || 250000;
    const endBudget = trStats.end_budget || (startBudget + trStats.net_pnl);

    tr.innerHTML = `
      <td><span class="badge badge-${mName}">${mName.toUpperCase()}</span></td>
      <td style="font-weight:600; color:#fff;">${trName}</td>
      <td class="font-mono text-center">${trStats.trades}</td>
      <td class="font-mono text-center" style="color:var(--accent-green);">${trStats.wins}</td>
      <td class="font-mono text-center" style="color:var(--accent-amber);">${trStats.breakevens}</td>
      <td class="font-mono text-center" style="color:var(--accent-red);">${trStats.losses}</td>
      <td class="font-mono text-center font-bold">${trStats.win_rate.toFixed(1)}%</td>
      <td class="font-mono text-right">${window.App.formatINR(startBudget, false)}</td>
      <td class="font-mono text-right font-bold">${window.App.formatINR(endBudget, false)}</td>
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
  },

  renderTable() {
    const tbody = document.getElementById("hist-track-tbody");
    if (!tbody || !this.overviewData || !this.overviewData.modes) return;

    tbody.innerHTML = "";
    const modes = this.overviewData.modes;

    if (this.currentModeFilter === "all") {
      // 1. Gather all unique track names across all modes
      const allTrackNames = new Set();
      for (const mData of Object.values(modes)) {
        for (const trName of Object.keys(mData.tracks || {})) {
          allTrackNames.add(trName);
        }
      }

      // 2. Sort track names by net p&l of the paper mode (descending, biggest first)
      const sortedTrackNames = Array.from(allTrackNames).sort((a, b) => {
        const pnlA = modes.paper?.tracks?.[a]?.net_pnl ?? -Infinity;
        const pnlB = modes.paper?.tracks?.[b]?.net_pnl ?? -Infinity;
        return pnlB - pnlA;
      });

      // 3. For each track, render paper mode first, then corresponding paper_delayed track below it if available
      sortedTrackNames.forEach((trName) => {
        const modeOrder = ["paper", "paper_delayed"];
        for (const m of Object.keys(modes)) {
          if (!modeOrder.includes(m)) modeOrder.push(m);
        }

        modeOrder.forEach((mName) => {
          const trStats = modes[mName]?.tracks?.[trName];
          if (trStats) {
            this.appendTrackRow(tbody, mName, trName, trStats);
          }
        });
      });
    } else {
      // Filtered mode: sort tracks in that mode by net_pnl descending (biggest first)
      const mName = this.currentModeFilter;
      const mData = modes[mName];
      if (mData && mData.tracks) {
        const sortedEntries = Object.entries(mData.tracks).sort(([, a], [, b]) => {
          return (b.net_pnl || 0) - (a.net_pnl || 0);
        });

        sortedEntries.forEach(([trName, trStats]) => {
          this.appendTrackRow(tbody, mName, trName, trStats);
        });
      }
    }

    if (tbody.children.length === 0) {
      tbody.innerHTML = `<tr><td colspan="12" class="text-center" style="padding:24px; color:var(--text-dim);">No historical trade journals recorded for session ${this.selectedDate} under mode '${this.currentModeFilter}'.</td></tr>`;
    }
  },

  async fetchTrackDetail(mode, track) {
    const cacheKey = `${this.selectedDate}_${mode}_${track}`;
    if (this.trackDetailCache[cacheKey]) {
      this.renderDetailPanel(this.trackDetailCache[cacheKey]);
      const panel = document.getElementById("historical-detail-panel");
      if (panel) panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
      return;
    }

    try {
      const res = await fetch(
        `/api/historical/track/${encodeURIComponent(mode)}/${encodeURIComponent(track)}?date=${encodeURIComponent(this.selectedDate)}`
      );
      if (res.ok) {
        const data = await res.json();
        this.trackDetailCache[cacheKey] = data;
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
      this.chart.setData(data.performance_curve || [], this.includeBrokerage);
    }

    // Render Trades Table
    const tbody = document.getElementById("hist-trades-tbody");
    if (!tbody) return;
    tbody.innerHTML = "";

    const trades = data.trades || [];
    for (const tr of trades) {
      const row = document.createElement("tr");
      row.style.cursor = "pointer";
      row.title = "Click to inspect Dual Intraday Charts (Underlying & Option)";

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

      row.addEventListener("click", () => {
        window.App.openTradeModal(tr);
      });

      tbody.appendChild(row);
    }

    if (trades.length === 0) {
      tbody.innerHTML = `<tr><td colspan="12" class="text-center" style="padding:24px; color:var(--text-dim);">No individual trade logs found.</td></tr>`;
    }
  },
};
