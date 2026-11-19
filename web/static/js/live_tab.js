/**
 * LiveTab: Real-time track performance, options depth MTM updates, and intraday curve.
 */

window.LiveTab = {
  currentModeFilter: "all",
  selectedTrack: null,
  selectedMode: null,
  chart: null,
  liveData: null,
  includeBrokerage: true,

  init() {
    this.bindFilters();
    const canvas = document.getElementById("live-chart-canvas");
    if (canvas && window.IntradayChart) {
      this.chart = new window.IntradayChart(canvas);
    }

    const bToggle = document.getElementById("live-brokerage-toggle");
    if (bToggle) {
      bToggle.addEventListener("change", (e) => {
        this.includeBrokerage = e.target.checked;
        if (this.chart) this.chart.setIncludeBrokerage(this.includeBrokerage);
      });
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
    if (!overall || !this.liveData || !this.liveData.modes) return;

    let targetNet = overall.net_pnl;
    let targetMtm = overall.mtm_pnl;
    let targetWinRate = overall.win_rate;
    let targetOpen = overall.open || 0;
    let targetClosed = overall.closed || 0;
    let targetFees = overall.brokerage;

    if (this.currentModeFilter !== "all" && this.liveData.modes[this.currentModeFilter]) {
      const mData = this.liveData.modes[this.currentModeFilter];
      const tracks = Object.values(mData.tracks || {});
      const nTracks = tracks.length || 1;
      let mNet = 0, mMtm = 0, mWins = 0, mClosed = 0, mOpen = 0, mFees = 0;
      for (const t of tracks) {
        mNet += (t.net_pnl || 0);
        mMtm += (t.mtm_pnl || 0);
        mWins += (t.wins || 0);
        mClosed += (t.closed || 0);
        mOpen += (t.open || 0);
        mFees += (t.brokerage || 0);
      }
      targetNet = mNet / nTracks;
      targetMtm = mMtm / nTracks;
      targetWinRate = mClosed > 0 ? (mWins / mClosed) * 100 : 0;
      targetOpen = mOpen;
      targetClosed = mClosed;
      targetFees = mFees;
    }

    const netEl = document.getElementById("live-stat-net-pnl");
    const mtmEl = document.getElementById("live-stat-mtm");
    const winRateEl = document.getElementById("live-stat-win-rate");
    const openPosEl = document.getElementById("live-stat-open-pos");
    const closedEl = document.getElementById("live-stat-closed");
    const feesEl = document.getElementById("live-stat-fees");

    if (netEl) {
      netEl.innerText = window.App.formatINR(targetNet);
      netEl.className = "stat-value " + (targetNet >= 0 ? "val-profit" : "val-loss");
    }

    if (mtmEl) {
      mtmEl.innerText = `MTM (Avg): ${window.App.formatINR(targetMtm)}`;
      mtmEl.className = "stat-subtext " + (targetMtm >= 0 ? "val-profit" : "val-loss");
    }

    if (winRateEl) {
      winRateEl.innerText = `${targetWinRate.toFixed(1)}%`;
      winRateEl.className = "stat-value " + (targetWinRate >= 50 ? "val-profit" : "val-neutral");
    }

    if (openPosEl) {
      openPosEl.innerText = targetOpen;
      openPosEl.className = targetOpen > 0 ? "stat-value val-cyan" : "stat-value";
    }

    if (closedEl) {
      closedEl.innerText = `${targetClosed} Trades`;
    }

    if (feesEl) {
      feesEl.innerText = window.App.formatINR(targetFees, false);
    }
  },

  appendTrackRow(tbody, mName, trName, trStats) {
    const tr = document.createElement("tr");
    const isSelected = this.selectedTrack === trName && this.selectedMode === mName;
    if (isSelected) tr.classList.add("selected");

    const hasOpen = trStats.open > 0;
    const openBadge = hasOpen
      ? `<span class="badge badge-open"><span class="pulse-dot" style="width:6px;height:6px;"></span> ${trStats.open} OPEN</span>`
      : `<span class="badge badge-closed">0 OPEN</span>`;

    const netClass = trStats.net_pnl >= 0 ? "val-profit" : "val-loss";
    const mtmClass = trStats.mtm_pnl >= 0 ? "val-profit" : "val-loss";
    const startBudget = trStats.start_budget || 250000;
    const currentBudget = trStats.current_budget || (startBudget + trStats.net_pnl);

    tr.innerHTML = `
      <td><span class="badge badge-${mName}">${mName.toUpperCase()}</span></td>
      <td style="font-weight:600; color:#fff;">${trName}</td>
      <td>${openBadge}</td>
      <td class="font-mono text-center">${trStats.trades}</td>
      <td class="font-mono text-center" style="color:var(--accent-green);">${trStats.wins}</td>
      <td class="font-mono text-center" style="color:var(--accent-amber);">${trStats.breakevens}</td>
      <td class="font-mono text-center" style="color:var(--accent-red);">${trStats.losses}</td>
      <td class="font-mono text-center font-bold">${trStats.win_rate.toFixed(1)}%</td>
      <td class="font-mono text-right">${window.App.formatINR(startBudget, false)}</td>
      <td class="font-mono text-right font-bold">${window.App.formatINR(currentBudget, false)}</td>
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
  },

  renderTable() {
    const tbody = document.getElementById("live-track-tbody");
    if (!tbody || !this.liveData || !this.liveData.modes) return;

    tbody.innerHTML = "";
    const modes = this.liveData.modes;

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
      tbody.innerHTML = `<tr><td colspan="14" class="text-center" style="padding:24px; color:var(--text-dim);">No active trade journals found for mode '${this.currentModeFilter}'.</td></tr>`;
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
      this.chart.setData(data.performance_curve || [], this.includeBrokerage);
    }

    // Render Trades Table
    const tbody = document.getElementById("live-trades-tbody");
    if (!tbody) return;
    tbody.innerHTML = "";

    const trades = data.trades || [];
    for (const tr of trades) {
      const row = document.createElement("tr");
      row.style.cursor = "pointer";
      row.title = "Click to inspect Dual Intraday Charts (Underlying & Option)";

      const isClosed = tr.status === "CLOSED";
      const statusBadge = isClosed
        ? `<span class="badge badge-closed">CLOSED</span>`
        : `<span class="badge badge-open"><span class="pulse-dot" style="width:6px;height:6px;"></span> OPEN</span>`;

      const pnlClass = tr.net_pnl >= 0 ? "val-profit" : "val-loss";
      const retClass = tr.return_pct >= 0 ? "val-profit" : "val-loss";

      const exitReasonBadge = tr.exit_reason
        ? `<span class="badge badge-reason">${tr.exit_reason}</span>`
        : `<span style="color:var(--text-dim);">-</span>`;

      let exitPriceDisplay = "";
      if (isClosed) {
        exitPriceDisplay = `₹${tr.exit_price.toFixed(2)}`;
      } else {
        const quoteTime = tr.last_quote_time_str || "-";
        const quoteAge = (tr.last_quote_age_sec !== null && tr.last_quote_age_sec !== undefined)
          ? `<span style="color:var(--text-dim); font-size:10px;"> (${tr.last_quote_age_sec}s ago)</span>`
          : "";
        exitPriceDisplay = `<div style="display:flex; flex-direction:column; align-items:flex-end;">
          <span style="color:var(--accent-cyan); font-weight:600;">₹${tr.current_bid.toFixed(2)} (Bid)</span>
          <span style="font-size:10px; color:var(--text-muted);">Quote: ${quoteTime}${quoteAge}</span>
        </div>`;
      }

      const exitTime = isClosed ? tr.exit_time_str : `<span class="val-cyan">Active</span>`;

      row.innerHTML = `
        <td class="font-mono" style="font-size:11px; color:var(--text-dim);">${tr.trade_id.replace("TRD_", "")}</td>
        <td style="font-weight:600; color:#fff;">${tr.symbol}</td>
        <td class="font-mono">${tr.strike} ${tr.option_type}</td>
        <td class="font-mono text-center">${tr.quantity}</td>
        <td class="font-mono text-right">₹${tr.entry_price.toFixed(2)}</td>
        <td class="font-mono text-center">${tr.entry_time_str}</td>
        <td class="font-mono text-right">${exitPriceDisplay}</td>
        <td class="font-mono text-center">${exitTime}</td>
        <td class="text-center">${statusBadge}</td>
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
      tbody.innerHTML = `<tr><td colspan="13" class="text-center" style="padding:24px; color:var(--text-dim);">No individual trades recorded for this track yet.</td></tr>`;
    }
  },
};
