/**
 * LiveTab: Real-time track performance, options depth MTM updates, and intraday curve.
 */

window.LiveTab = {
  currentModeFilter: "all",
  selectedTrack: null,
  selectedMode: null,
  chart: null,
  liveData: null,

  init() {
    this.bindFilters();
    const canvas = document.getElementById("live-chart-canvas");
    if (canvas && window.IntradayChart) {
      this.chart = new window.IntradayChart(canvas);
    }
  },

  onActivate() {
    this.fetchOverview();
  },

  bindFilters() {
    const pills = document.querySelectorAll(".live-mode-pill");
    pills.forEach((p) => {
      p.addEventListener("click", () => {
        pills.forEach((x) => x.classList.remove("active"));
        p.classList.add("active");
        this.currentModeFilter = p.getAttribute("data-mode");
        this.renderTable();
      });
    });
  },

  async fetchOverview() {
    try {
      const res = await fetch("/api/live/overview");
      if (res.ok) {
        const data = await res.json();
        this.onLiveUpdate(data);
      }
    } catch (e) {
      console.error("Failed to fetch live overview:", e);
    }
  },

  onLiveUpdate(payload) {
    this.liveData = payload;
    this.renderStats(payload.overall);
    this.renderTable();

    // If a track is currently selected, refresh its details
    if (this.selectedTrack && this.selectedMode) {
      this.fetchTrackDetail(this.selectedMode, this.selectedTrack, false);
    }
  },

  renderStats(overall) {
    if (!overall) return;
    const netEl = document.getElementById("live-stat-net-pnl");
    const mtmEl = document.getElementById("live-stat-mtm");
    const winRateEl = document.getElementById("live-stat-win-rate");
    const openPosEl = document.getElementById("live-stat-open-pos");
    const closedEl = document.getElementById("live-stat-closed");
    const feesEl = document.getElementById("live-stat-fees");

    if (netEl) {
      netEl.innerText = window.App.formatINR(overall.net_pnl);
      netEl.className = "stat-value " + (overall.net_pnl >= 0 ? "val-profit" : "val-loss");
    }

    if (mtmEl) {
      mtmEl.innerText = `MTM: ${window.App.formatINR(overall.mtm_pnl)}`;
      mtmEl.className = "stat-subtext " + (overall.mtm_pnl >= 0 ? "val-profit" : "val-loss");
    }

    if (winRateEl) {
      winRateEl.innerText = `${overall.win_rate.toFixed(1)}%`;
      winRateEl.className = "stat-value " + (overall.win_rate >= 50 ? "val-profit" : "val-neutral");
    }

    if (openPosEl) {
      openPosEl.innerText = overall.open || 0;
      if (overall.open > 0) {
        openPosEl.className = "stat-value val-cyan";
      } else {
        openPosEl.className = "stat-value";
      }
    }

    if (closedEl) {
      closedEl.innerText = `${overall.closed} Trades`;
    }

    if (feesEl) {
      feesEl.innerText = window.App.formatINR(overall.brokerage, false);
    }
  },

  renderTable() {
    const tbody = document.getElementById("live-track-tbody");
    if (!tbody || !this.liveData || !this.liveData.modes) return;

    tbody.innerHTML = "";
    const modes = this.liveData.modes;

    for (const [mName, mData] of Object.entries(modes)) {
      if (this.currentModeFilter !== "all" && mName !== this.currentModeFilter) {
        continue;
      }

      for (const [trName, trStats] of Object.entries(mData.tracks || {})) {
        const tr = document.createElement("tr");
        const isSelected = this.selectedTrack === trName && this.selectedMode === mName;
        if (isSelected) tr.classList.add("selected");

        const hasOpen = trStats.open > 0;
        const openBadge = hasOpen
          ? `<span class="badge badge-open"><span class="pulse-dot" style="width:6px;height:6px;"></span> ${trStats.open} OPEN</span>`
          : `<span class="badge badge-closed">0 OPEN</span>`;

        const netClass = trStats.net_pnl >= 0 ? "val-profit" : "val-loss";
        const mtmClass = trStats.mtm_pnl >= 0 ? "val-profit" : "val-loss";

        tr.innerHTML = `
          <td><span class="badge badge-${mName}">${mName.toUpperCase()}</span></td>
          <td style="font-weight:600; color:#fff;">${trName}</td>
          <td>${openBadge}</td>
          <td class="font-mono text-center">${trStats.trades}</td>
          <td class="font-mono text-center" style="color:var(--accent-green);">${trStats.wins}</td>
          <td class="font-mono text-center" style="color:var(--accent-amber);">${trStats.breakevens}</td>
          <td class="font-mono text-center" style="color:var(--accent-red);">${trStats.losses}</td>
          <td class="font-mono text-center font-bold">${trStats.win_rate.toFixed(1)}%</td>
          <td class="font-mono text-right">${window.App.formatINR(trStats.gross_pnl)}</td>
          <td class="font-mono text-right" style="color:var(--text-dim);">${window.App.formatINR(trStats.brokerage, false)}</td>
          <td class="font-mono text-right ${mtmClass}">${window.App.formatINR(trStats.mtm_pnl)}</td>
          <td class="font-mono text-right font-bold ${netClass}">${window.App.formatINR(trStats.net_pnl)}</td>
        `;

        tr.addEventListener("click", () => {
          this.selectedMode = mName;
          this.selectedTrack = trName;
          this.renderTable();
          this.fetchTrackDetail(mName, trName, true);
        });

        tbody.appendChild(tr);
      }
    }

    if (tbody.children.length === 0) {
      tbody.innerHTML = `<tr><td colspan="12" class="text-center" style="padding:24px; color:var(--text-dim);">No active trade journals found for mode '${this.currentModeFilter}'.</td></tr>`;
    }
  },

  async fetchTrackDetail(mode, track, scrollIntoView = false) {
    try {
      const res = await fetch(`/api/live/track/${encodeURIComponent(mode)}/${encodeURIComponent(track)}`);
      if (res.ok) {
        const data = await res.json();
        this.renderDetailPanel(data);
        if (scrollIntoView) {
          const panel = document.getElementById("live-detail-panel");
          if (panel) panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
        }
      }
    } catch (e) {
      console.error("Failed to fetch track detail:", e);
    }
  },

  renderDetailPanel(data) {
    const panel = document.getElementById("live-detail-panel");
    if (!panel) return;
    panel.style.display = "flex";

    const titleEl = document.getElementById("live-detail-title");
    const badgeEl = document.getElementById("live-detail-mode-badge");
    const netEl = document.getElementById("live-detail-net");

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
    const tbody = document.getElementById("live-trades-tbody");
    if (!tbody) return;
    tbody.innerHTML = "";

    const trades = data.trades || [];
    for (const tr of trades) {
      const row = document.createElement("tr");
      const isClosed = tr.status === "CLOSED";
      const statusBadge = isClosed
        ? `<span class="badge badge-closed">CLOSED</span>`
        : `<span class="badge badge-open"><span class="pulse-dot" style="width:6px;height:6px;"></span> OPEN</span>`;

      const pnlClass = tr.net_pnl >= 0 ? "val-profit" : "val-loss";
      const retClass = tr.return_pct >= 0 ? "val-profit" : "val-loss";

      const exitReasonBadge = tr.exit_reason
        ? `<span class="badge badge-reason">${tr.exit_reason}</span>`
        : `<span style="color:var(--text-dim);">-</span>`;

      const exitPrice = isClosed ? `₹${tr.exit_price.toFixed(2)}` : `<span style="color:var(--accent-cyan);">₹${tr.current_bid.toFixed(2)} (Bid)</span>`;
      const exitTime = isClosed ? tr.exit_time_str : `<span class="val-cyan">Active</span>`;

      row.innerHTML = `
        <td class="font-mono" style="font-size:11px; color:var(--text-dim);">${tr.trade_id.replace("TRD_", "")}</td>
        <td style="font-weight:600; color:#fff;">${tr.symbol}</td>
        <td class="font-mono">${tr.strike} ${tr.option_type}</td>
        <td class="font-mono text-center">${tr.quantity}</td>
        <td class="font-mono text-right">₹${tr.entry_price.toFixed(2)}</td>
        <td class="font-mono text-center">${tr.entry_time_str}</td>
        <td class="font-mono text-right">${exitPrice}</td>
        <td class="font-mono text-center">${exitTime}</td>
        <td class="text-center">${statusBadge}</td>
        <td class="text-center">${exitReasonBadge}</td>
        <td class="font-mono text-right ${retClass}">${window.App.formatPct(tr.return_pct)}</td>
        <td class="font-mono text-right" style="color:var(--text-dim);">₹${tr.brokerage.toFixed(2)}</td>
        <td class="font-mono text-right font-bold ${pnlClass}">${window.App.formatINR(tr.net_pnl)}</td>
      `;
      tbody.appendChild(row);
    }

    if (trades.length === 0) {
      tbody.innerHTML = `<tr><td colspan="13" class="text-center" style="padding:24px; color:var(--text-dim);">No individual trades recorded for this track yet.</td></tr>`;
    }
  },
};
