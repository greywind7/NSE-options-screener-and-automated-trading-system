/**
 * Standalone Simulation Report Controller.
 * Powers the clean new-tab view (/sim/results?run_id=...) with full-viewport scrolling.
 */

window.SimResultsPage = {
  run: null,
  chart: null,
  currentModeFilter: "all",
  includeBrokerage: true,
  selectedMode: null,
  selectedTrack: null,
  isParamsCollapsed: false,

  // Candle modal controllers
  tamCurrentMode: "candle",
  tamCurrentRange: "local",
  tamUnderlyingLocalChart: null,
  tamOptionLocalChart: null,
  tamUnderlyingFullChart: null,
  tamOptionFullChart: null,

  init() {
    this.bindControls();
    this.initChart();
    this.initCandleModal();
    this.loadRunData();
  },

  bindControls() {
    // Refresh button
    const refBtn = document.getElementById("srp-refresh-btn");
    if (refBtn) {
      refBtn.addEventListener("click", () => this.loadRunData(true));
    }

    // Parameters collapse
    const pToggle = document.getElementById("srp-params-toggle");
    if (pToggle) {
      pToggle.addEventListener("click", () => this.toggleParams());
    }

    // Brokerage toggle
    const bToggle = document.getElementById("srp-brokerage-toggle");
    if (bToggle) {
      bToggle.addEventListener("change", (e) => {
        this.includeBrokerage = e.target.checked;
        if (this.chart) this.chart.setIncludeBrokerage(this.includeBrokerage);
      });
    }

    // Mode filter pills
    const pills = document.querySelectorAll(".srp-mode-pill");
    pills.forEach((p) => {
      p.addEventListener("click", () => {
        pills.forEach((x) => x.classList.remove("active"));
        p.classList.add("active");
        this.currentModeFilter = p.getAttribute("data-mode");
        this.renderMatrix();
      });
    });
  },

  initChart() {
    const canvas = document.getElementById("srp-chart-canvas");
    if (canvas && window.IntradayChart) {
      this.chart = new window.IntradayChart(canvas);
    }

    const btnSquare = document.getElementById("srp-aspect-square");
    const btnWide = document.getElementById("srp-aspect-wide");
    const curveWrapper = document.getElementById("srp-curve-wrapper");
    if (btnSquare && btnWide && canvas) {
      btnSquare.addEventListener("click", () => {
        btnSquare.classList.add("active");
        btnWide.classList.remove("active");
        canvas.classList.add("chart-square");
        if (curveWrapper) curveWrapper.classList.remove("wide");
        if (this.chart) {
          this.chart._setupCanvas();
          this.chart.render();
        }
      });
      btnWide.addEventListener("click", () => {
        btnWide.classList.add("active");
        btnSquare.classList.remove("active");
        canvas.classList.remove("chart-square");
        if (curveWrapper) curveWrapper.classList.add("wide");
        if (this.chart) {
          canvas.style.height = "440px";
          this.chart._setupCanvas();
          this.chart.render();
        }
      });
    }
  },

  async loadRunData(forceServer = false) {
    const params = new URLSearchParams(window.location.search);
    const targetRunId = params.get("run_id") || "latest";

    // 1. Try local storage cache first for instant render
    if (!forceServer) {
      try {
        const raw = localStorage.getItem("sim_runs");
        if (raw) {
          const list = JSON.parse(raw);
          const found = list.find((r) => r.id === targetRunId || targetRunId === "latest");
          if (found && found.results) {
            this.run = found;
            this.renderAll();
            return;
          }
        }
      } catch (e) {
        console.warn("Local storage read error:", e);
      }
    }

    // 2. Fetch from backend API
    try {
      const endpoint = targetRunId === "latest" ? "/api/sim/run/latest" : `/api/sim/run/${encodeURIComponent(targetRunId)}`;
      const res = await fetch(endpoint);
      if (res.ok) {
        const data = await res.json();
        if (data.run) {
          this.run = data.run;
          this.renderAll();
          return;
        }
      } else {
        // Fallback to latest results endpoint
        const res2 = await fetch("/api/sim/results");
        if (res2.ok) {
          const data2 = await res2.json();
          if (data2.results) {
            this.run = {
              id: targetRunId,
              date: data2.results.date || "2026-11-19",
              runtime: "-",
              results: data2.results,
              payload: {},
            };
            this.renderAll();
            return;
          }
        }
      }
    } catch (e) {
      console.error("Failed to load run details:", e);
    }

    document.getElementById("srp-title").innerText = `Simulation Run Not Found (${targetRunId})`;
    const tbody = document.getElementById("srp-matrix-tbody");
    if (tbody) {
      tbody.innerHTML = `<tr><td colspan="13" class="text-center" style="padding:32px; color:var(--text-dim);">Simulation run '${targetRunId}' is not loaded or has no stored results.</td></tr>`;
    }
  },

  renderAll() {
    if (!this.run) return;

    // Header info
    document.title = `Sim Report // ${this.run.id} (${this.run.date})`;
    document.getElementById("srp-title").innerText = `Simulation ${this.run.id} • Session Date ${this.run.date}`;

    const runtimeEl = document.getElementById("srp-runtime-badge");
    if (runtimeEl) runtimeEl.innerText = `Elapsed: ${this.run.runtime || '-'}s`;

    // Render parameters used
    this.renderParams();

    // Compute executive totals averaged over active tracks
    let totalGross = 0;
    let totalFees = 0;
    let totalNet = 0;
    let totalTrades = 0;
    let totalWins = 0;
    let activeTrackCount = 0;

    if (this.run.results && this.run.results.matrix) {
      const matrix = this.run.results.matrix;
      const targetMode = this.currentModeFilter;

      if (targetMode === "all") {
        const uniqueTracks = new Set();
        for (const [mName, mTracks] of Object.entries(matrix)) {
          for (const [trName, st] of Object.entries(mTracks)) {
            uniqueTracks.add(trName);
            totalGross += st.gross_pnl || 0;
            totalFees += st.total_brokerage || 0;
            totalNet += st.net_pnl || 0;
            totalTrades += st.trades || 0;
            totalWins += st.wins || 0;
          }
        }
        activeTrackCount = Object.keys(matrix.paper || {}).length || uniqueTracks.size || 1;
      } else {
        const mTracks = matrix[targetMode] || {};
        activeTrackCount = Object.keys(mTracks).length || 1;
        for (const st of Object.values(mTracks)) {
          totalGross += st.gross_pnl || 0;
          totalFees += st.total_brokerage || 0;
          totalNet += st.net_pnl || 0;
          totalTrades += st.trades || 0;
          totalWins += st.wins || 0;
        }
      }
    }

    const avgNet = activeTrackCount > 0 ? totalNet / activeTrackCount : 0;
    const avgGross = activeTrackCount > 0 ? totalGross / activeTrackCount : 0;

    const netBadge = document.getElementById("srp-net-badge");
    if (netBadge) {
      netBadge.className = `badge ${avgNet >= 0 ? 'badge-win' : 'badge-loss'}`;
      netBadge.innerText = `Avg: ${this.formatINR(avgNet)}`;
    }

    const statNet = document.getElementById("srp-stat-net-pnl");
    if (statNet) {
      statNet.className = `stat-value font-mono ${avgNet >= 0 ? 'val-profit' : 'val-loss'}`;
      statNet.innerText = this.formatINR(avgNet);
    }

    const statGross = document.getElementById("srp-stat-gross-pnl");
    if (statGross) {
      statGross.className = `stat-value font-mono ${avgGross >= 0 ? 'val-profit' : 'val-loss'}`;
      statGross.innerText = this.formatINR(avgGross);
    }

    const winRate = totalTrades > 0 ? (totalWins / totalTrades) * 100 : 0;
    const statWr = document.getElementById("srp-stat-win-rate");
    if (statWr) statWr.innerText = `${winRate.toFixed(1)}%`;

    const statTrds = document.getElementById("srp-stat-trades");
    if (statTrds) statTrds.innerText = `${totalTrades} Trades`;

    const statFees = document.getElementById("srp-stat-brokerage");
    if (statFees) statFees.innerText = this.formatINR(totalFees, false);

    // Render Matrix table
    this.renderMatrix();

    // Reset drilldown
    const detailPanel = document.getElementById("srp-detail-panel");
    if (detailPanel) detailPanel.style.display = "none";
  },

  toggleParams() {
    this.isParamsCollapsed = !this.isParamsCollapsed;
    const body = document.getElementById("srp-params-body");
    const btn = document.getElementById("srp-params-toggle");
    if (body) body.classList.toggle("collapsed", this.isParamsCollapsed);
    if (btn) btn.innerText = this.isParamsCollapsed ? "Expand Parameters ▼" : "Collapse Parameters ▲";
  },

  renderParams() {
    const body = document.getElementById("srp-params-body");
    if (!body || !this.run) return;

    const p = this.run.payload || this.run.params || {};
    const g = p.global_settings || {};
    const b = p.base_tracks || {};
    const c = p.custom_tracks || [];

    const modesStr = (g.modes || ["paper", "paper_delayed"]).map((m) => m.toUpperCase()).join(", ");
    const minPolePct = ((g.min_pole_pct || 0.005) * 100).toFixed(2);

    body.innerHTML = `
      <div class="srm-params-grid">
        <div class="srm-param-chip">
          <span class="chip-label">Recording Date</span>
          <span class="chip-val" style="color:var(--accent-cyan);">${this.run.date}</span>
        </div>
        <div class="srm-param-chip">
          <span class="chip-label">Active Modes</span>
          <span class="chip-val">${modesStr}</span>
        </div>
        <div class="srm-param-chip">
          <span class="chip-label">Capital / Track</span>
          <span class="chip-val">${this.formatINR(g.capital || 250000, false)}</span>
        </div>
        <div class="srm-param-chip">
          <span class="chip-label">Brokerage Fee</span>
          <span class="chip-val">₹${g.brokerage_per_order || 30} / order (₹${(g.brokerage_per_order || 30) * 2} RT)</span>
        </div>
        <div class="srm-param-chip">
          <span class="chip-label">Execution Latency</span>
          <span class="chip-val">${g.min_delay_ms || 200}ms – ${g.max_delay_ms || 500}ms</span>
        </div>
        <div class="srm-param-chip">
          <span class="chip-label">Min Pole %</span>
          <span class="chip-val">${minPolePct}%</span>
        </div>
        <div class="srm-param-chip">
          <span class="chip-label">Entry Freeze</span>
          <span class="chip-val">${g.entry_freeze_time || "15:00"}</span>
        </div>
        <div class="srm-param-chip">
          <span class="chip-label">EOD Square-Off</span>
          <span class="chip-val">${g.eod_squareoff_time || "15:15"}</span>
        </div>
      </div>

      <div class="srm-archetypes-row">
        <div class="srm-arch-card">
          <div class="srm-arch-card-header">
            <span style="color:var(--accent-cyan);">⚡ Fast Momentum Archetype</span>
            <span class="badge ${b.fast_momentum?.enabled !== false ? 'badge-win' : 'badge-loss'}">${b.fast_momentum?.enabled !== false ? 'ENABLED' : 'DISABLED'}</span>
          </div>
          <div class="srm-arch-specs">
            <span>Target: <b>${((b.fast_momentum?.target_pct || 0.15) * 100).toFixed(1)}%</b></span>
            <span>SL: <b>${((b.fast_momentum?.sl_pct || 0.05) * 100).toFixed(1)}%</b></span>
            <span>BE: <b>${((b.fast_momentum?.breakeven_trigger_pct || 0.025) * 100).toFixed(1)}%</b></span>
            <span>Decay: <b>${b.fast_momentum?.target_decay_sec || 900}s</b> (Floor ${((b.fast_momentum?.decay_floor_pct || 0.025) * 100).toFixed(1)}%)</span>
            <span>Stagnation: <b>${b.fast_momentum?.stagnation_timeout_sec || 1800}s</b></span>
            <span>Strikes: <b>${(b.fast_momentum?.modes || ["ITM", "ATM", "OTM"]).join(", ")}</b></span>
          </div>
        </div>

        <div class="srm-arch-card">
          <div class="srm-arch-card-header">
            <span style="color:var(--accent-green);">📈 Trend Pullback Archetype</span>
            <span class="badge ${b.trend_pullback?.enabled !== false ? 'badge-win' : 'badge-loss'}">${b.trend_pullback?.enabled !== false ? 'ENABLED' : 'DISABLED'}</span>
          </div>
          <div class="srm-arch-specs">
            <span>Target: <b>${((b.trend_pullback?.target_pct || 0.25) * 100).toFixed(1)}%</b></span>
            <span>SL: <b>${((b.trend_pullback?.sl_pct || 0.05) * 100).toFixed(1)}%</b></span>
            <span>Trailing Step: <b>${((b.trend_pullback?.trailing_step_pct || 0.01) * 100).toFixed(1)}%</b></span>
            <span>BE: <b>${((b.trend_pullback?.breakeven_trigger_pct || 0.025) * 100).toFixed(1)}%</b></span>
            <span>Timeout: <b>${b.trend_pullback?.timeout_sec || 3600}s</b></span>
            <span>Strikes: <b>${(b.trend_pullback?.modes || ["ITM", "ATM", "OTM"]).join(", ")}</b></span>
          </div>
        </div>

        <div class="srm-arch-card">
          <div class="srm-arch-card-header">
            <span style="color:var(--accent-amber);">🎯 Fixed Target Archetype</span>
            <span class="badge ${b.fixed_target?.enabled !== false ? 'badge-win' : 'badge-loss'}">${b.fixed_target?.enabled !== false ? 'ENABLED' : 'DISABLED'}</span>
          </div>
          <div class="srm-arch-specs">
            <span>Target: <b>${((b.fixed_target?.target_pct || 0.15) * 100).toFixed(1)}%</b></span>
            <span>SL: <b>${((b.fixed_target?.sl_pct || 0.05) * 100).toFixed(1)}%</b></span>
            <span>Fixed Target: <b>₹${b.fixed_target?.fixed_target_inr || 750}</b></span>
            <span>BE: <b>${((b.fixed_target?.breakeven_trigger_pct || 0.025) * 100).toFixed(1)}%</b></span>
            <span>Timeout: <b>${b.fixed_target?.timeout_sec || 1800}s</b></span>
            <span>Strikes: <b>${(b.fixed_target?.modes || ["ITM", "ATM", "OTM"]).join(", ")}</b></span>
          </div>
        </div>
      </div>

      ${c.length > 0 ? `
        <div style="font-size:11px; margin-top:4px;">
          <span style="font-weight:700; color:var(--accent-purple);">Custom Tracks:</span>
          ${c.map((item) => `
            <span class="badge" style="background:rgba(168,85,247,0.15); color:var(--accent-purple); border:1px solid rgba(168,85,247,0.3); margin-left:4px;">
              ${item.name} (${item.archetype} ${item.mode})
            </span>
          `).join("")}
        </div>
      ` : ''}
    `;
  },

  appendMatrixRow(tbody, mName, trName, st) {
    const tr = document.createElement("tr");
    const isSelected = this.selectedTrack === trName && this.selectedMode === mName;
    if (isSelected) tr.classList.add("selected");

    const netClass = st.net_pnl >= 0 ? "val-profit" : "val-loss";
    const grossClass = st.gross_pnl >= 0 ? "val-profit" : "val-loss";
    const retClass = st.return_pct >= 0 ? "val-profit" : "val-loss";
    const endCap = st.end_capital !== undefined ? st.end_capital : (st.start_capital + st.net_pnl);
    const endClass = endCap >= (st.start_capital || 250000) ? "val-profit" : "val-loss";

    tr.innerHTML = `
      <td><span class="badge badge-${mName}">${mName.toUpperCase()}</span></td>
      <td style="font-weight:600; color:#fff;">${trName}</td>
      <td class="font-mono text-center">${st.trades}</td>
      <td class="font-mono text-center" style="color:var(--accent-green); font-weight:700;">${st.wins}</td>
      <td class="font-mono text-center" style="color:var(--accent-amber);">${st.breakevens}</td>
      <td class="font-mono text-center" style="color:var(--accent-red);">${st.losses}</td>
      <td class="font-mono text-center font-bold">${st.win_rate.toFixed(1)}%</td>
      <td class="font-mono text-right">${this.formatINR(st.start_capital || 250000, false)}</td>
      <td class="font-mono text-right font-bold ${endClass}">${this.formatINR(endCap, false)}</td>
      <td class="font-mono text-right font-bold ${grossClass}">${this.formatINR(st.gross_pnl)}</td>
      <td class="font-mono text-right" style="color:var(--text-dim);">${this.formatINR(st.total_brokerage, false)}</td>
      <td class="font-mono text-right font-bold ${netClass}">${this.formatINR(st.net_pnl)}</td>
      <td class="font-mono text-right font-bold ${retClass}">${this.formatPct(st.return_pct)}</td>
    `;

    tr.addEventListener("click", () => {
      this.selectedMode = mName;
      this.selectedTrack = trName;
      this.renderMatrix();
      this.renderDrilldown(mName, trName);
    });

    tbody.appendChild(tr);
  },

  renderMatrix() {
    const tbody = document.getElementById("srp-matrix-tbody");
    if (!tbody || !this.run || !this.run.results || !this.run.results.matrix) return;

    tbody.innerHTML = "";
    const matrix = this.run.results.matrix;

    if (this.currentModeFilter === "all") {
      // 1. Gather all unique track names across matrix
      const allTrackNames = new Set();
      for (const mTracks of Object.values(matrix)) {
        for (const trName of Object.keys(mTracks)) {
          allTrackNames.add(trName);
        }
      }

      // 2. Sort track names by net p&l of the paper mode (descending, biggest first)
      const sortedTrackNames = Array.from(allTrackNames).sort((a, b) => {
        const pnlA = matrix.paper?.[a]?.net_pnl ?? -Infinity;
        const pnlB = matrix.paper?.[b]?.net_pnl ?? -Infinity;
        return pnlB - pnlA;
      });

      // 3. For each track, render paper mode first, then corresponding paper_delayed track below it if available
      sortedTrackNames.forEach((trName) => {
        const modeOrder = ["paper", "paper_delayed"];
        for (const m of Object.keys(matrix)) {
          if (!modeOrder.includes(m)) modeOrder.push(m);
        }

        modeOrder.forEach((mName) => {
          const st = matrix[mName]?.[trName];
          if (st) {
            this.appendMatrixRow(tbody, mName, trName, st);
          }
        });
      });
    } else {
      // Filtered mode: sort tracks in that mode by net_pnl descending (biggest first)
      const mName = this.currentModeFilter;
      const mTracks = matrix[mName] || {};
      const sortedEntries = Object.entries(mTracks).sort(([, a], [, b]) => {
        return (b.net_pnl || 0) - (a.net_pnl || 0);
      });

      sortedEntries.forEach(([trName, st]) => {
        this.appendMatrixRow(tbody, mName, trName, st);
      });
    }

    if (tbody.children.length === 0) {
      tbody.innerHTML = `<tr><td colspan="13" class="text-center" style="padding:24px; color:var(--text-dim);">No simulation results for mode '${this.currentModeFilter}'.</td></tr>`;
    }
  },

  renderDrilldown(mode, track) {
    const detailPanel = document.getElementById("srp-detail-panel");
    if (!detailPanel || !this.run || !this.run.results || !this.run.results.details) return;

    detailPanel.style.display = "flex";
    const modeDetails = this.run.results.details[mode] || {};
    const trackData = modeDetails[track] || { trades: [], curve: [] };

    document.getElementById("srp-detail-title").innerText = track;
    const badgeEl = document.getElementById("srp-detail-mode-badge");
    if (badgeEl) {
      badgeEl.className = `badge badge-${mode}`;
      badgeEl.innerText = mode.toUpperCase();
    }

    // Performance curve
    if (this.chart) {
      this.chart.setData(trackData.curve || [], this.includeBrokerage);
    }

    // Trades list
    const tbody = document.getElementById("srp-trades-tbody");
    if (!tbody) return;
    tbody.innerHTML = "";

    const trades = trackData.trades || [];
    let netTrackPnl = 0;

    for (const tr of trades) {
      netTrackPnl += (tr.net_pnl || 0);
      const row = document.createElement("tr");
      row.style.cursor = "pointer";
      row.title = "Click to inspect Dual Intraday Charts (Underlying & Option)";

      const pnlClass = tr.net_pnl >= 0 ? "val-profit" : "val-loss";
      const retClass = tr.return_pct >= 0 ? "val-profit" : "val-loss";

      const entryReasonBadge = (tr.entry_reason || tr.trigger)
        ? `<span class="badge badge-entry">${tr.entry_reason || tr.trigger}</span>`
        : `<span style="color:var(--text-dim);">-</span>`;

      const exitReasonBadge = tr.exit_reason
        ? `<span class="badge badge-reason">${tr.exit_reason}</span>`
        : `<span style="color:var(--text-dim);">-</span>`;

      row.innerHTML = `
        <td class="font-mono" style="font-size:11px; color:var(--text-dim);">${(tr.trade_id || "").replace("TRD_", "")}</td>
        <td style="font-weight:600; color:#fff;">${tr.symbol}</td>
        <td class="font-mono">${tr.strike} ${tr.option_type}</td>
        <td class="font-mono text-center">${tr.quantity}</td>
        <td class="text-center">${entryReasonBadge}</td>
        <td class="font-mono text-right">₹${tr.entry_price ? tr.entry_price.toFixed(2) : '-'}</td>
        <td class="font-mono text-center">${tr.entry_time_str || '-'}</td>
        <td class="font-mono text-right">₹${tr.exit_price ? tr.exit_price.toFixed(2) : '-'}</td>
        <td class="font-mono text-center">${tr.exit_time_str || '-'}</td>
        <td class="text-center">${exitReasonBadge}</td>
        <td class="font-mono text-right ${retClass}">${this.formatPct(tr.return_pct)}</td>
        <td class="font-mono text-right" style="color:var(--text-dim);">₹${(tr.brokerage || 60).toFixed(2)}</td>
        <td class="font-mono text-right font-bold ${pnlClass}">${this.formatINR(tr.net_pnl)}</td>
      `;

      row.addEventListener("click", () => {
        this.openTradeModal(tr);
      });

      tbody.appendChild(row);
    }

    const netEl = document.getElementById("srp-detail-net");
    if (netEl) {
      netEl.className = `stat-value font-mono ${netTrackPnl >= 0 ? 'val-profit' : 'val-loss'}`;
      netEl.innerText = this.formatINR(netTrackPnl);
    }

    if (trades.length === 0) {
      tbody.innerHTML = `<tr><td colspan="13" class="text-center" style="padding:24px; color:var(--text-dim);">No simulated trades triggered for this track.</td></tr>`;
    }

    detailPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  },

  // Trade Modal Controllers for 5m Candles
  initCandleModal() {
    const modal = document.getElementById("trade-analysis-modal");
    const closeBtn = document.getElementById("tam-close");
    if (closeBtn && modal) {
      closeBtn.addEventListener("click", () => modal.classList.remove("active"));
      modal.addEventListener("click", (e) => {
        if (e.target === modal) modal.classList.remove("active");
      });
    }

    const cEqLocal = document.getElementById("tam-underlying-local-canvas");
    const cOptLocal = document.getElementById("tam-option-local-canvas");
    const cEqFull = document.getElementById("tam-underlying-canvas");
    const cOptFull = document.getElementById("tam-option-canvas");

    if (cEqLocal && window.CandleChart) this.tamUnderlyingLocalChart = new window.CandleChart(cEqLocal);
    if (cOptLocal && window.CandleChart) this.tamOptionLocalChart = new window.CandleChart(cOptLocal);
    if (cEqFull && window.CandleChart) this.tamUnderlyingFullChart = new window.CandleChart(cEqFull);
    if (cOptFull && window.CandleChart) this.tamOptionFullChart = new window.CandleChart(cOptFull);

    const btnCandle = document.getElementById("tam-mode-candle");
    const btnLine = document.getElementById("tam-mode-line");
    if (btnCandle && btnLine) {
      btnCandle.addEventListener("click", () => {
        btnCandle.classList.add("active");
        btnLine.classList.remove("active");
        this.tamCurrentMode = "candle";
        [this.tamUnderlyingLocalChart, this.tamOptionLocalChart, this.tamUnderlyingFullChart, this.tamOptionFullChart].forEach((c) => c && c.setMode("candle"));
      });
      btnLine.addEventListener("click", () => {
        btnLine.classList.add("active");
        btnCandle.classList.remove("active");
        this.tamCurrentMode = "line";
        [this.tamUnderlyingLocalChart, this.tamOptionLocalChart, this.tamUnderlyingFullChart, this.tamOptionFullChart].forEach((c) => c && c.setMode("line"));
      });
    }

    const btnLocal = document.getElementById("tam-range-local");
    const btnFull = document.getElementById("tam-range-fullday");
    const btnBoth = document.getElementById("tam-range-both");
    if (btnLocal && btnFull && btnBoth) {
      btnLocal.addEventListener("click", () => this.setTamRange("local"));
      btnFull.addEventListener("click", () => this.setTamRange("fullday"));
      btnBoth.addEventListener("click", () => this.setTamRange("both"));
    }
  },

  setTamRange(range) {
    this.tamCurrentRange = range;
    document.getElementById("tam-range-local")?.classList.toggle("active", range === "local");
    document.getElementById("tam-range-fullday")?.classList.toggle("active", range === "fullday");
    document.getElementById("tam-range-both")?.classList.toggle("active", range === "both");

    const secLocal = document.getElementById("tam-section-local");
    const secFull = document.getElementById("tam-section-fullday");
    if (secLocal) secLocal.style.display = (range === "local" || range === "both") ? "grid" : "none";
    if (secFull) secFull.style.display = (range === "fullday" || range === "both") ? "grid" : "none";

    setTimeout(() => {
      if (range === "local" || range === "both") {
        if (this.tamUnderlyingLocalChart) { this.tamUnderlyingLocalChart._setupCanvas(); this.tamUnderlyingLocalChart.render(); }
        if (this.tamOptionLocalChart) { this.tamOptionLocalChart._setupCanvas(); this.tamOptionLocalChart.render(); }
      }
      if (range === "fullday" || range === "both") {
        if (this.tamUnderlyingFullChart) { this.tamUnderlyingFullChart._setupCanvas(); this.tamUnderlyingFullChart.render(); }
        if (this.tamOptionFullChart) { this.tamOptionFullChart._setupCanvas(); this.tamOptionFullChart.render(); }
      }
    }, 50);
  },

  async openTradeModal(trade) {
    const modal = document.getElementById("trade-analysis-modal");
    if (!modal) return;

    const titleEl = document.getElementById("tam-title");
    const pnlBadge = document.getElementById("tam-pnl-badge");
    const metaBar = document.getElementById("tam-meta-bar");

    const symbol = trade.symbol || "UNKNOWN";
    const strike = trade.strike || "";
    const optType = trade.option_type || "";
    const contractDesc = `${strike} ${optType}`.trim();
    const entryReasonStr = trade.entry_reason || trade.trigger || "Breakout";
    const exitReasonStr = trade.exit_reason || "-";

    if (titleEl) titleEl.innerText = `${symbol} ${contractDesc} (Trade #${(trade.trade_id || "").replace("TRD_", "")})`;
    if (pnlBadge) {
      pnlBadge.className = `badge ${(trade.net_pnl || 0) >= 0 ? 'badge-win' : 'badge-loss'}`;
      pnlBadge.innerText = this.formatINR(trade.net_pnl || 0);
    }

    if (metaBar) {
      metaBar.innerHTML = `
        <div class="meta-item"><span class="meta-label">Contract:</span><span class="meta-val">${symbol} ${contractDesc}</span></div>
        <div class="meta-item"><span class="meta-label">Qty:</span><span class="meta-val">${trade.quantity}</span></div>
        <div class="meta-item"><span class="meta-label">Entry Reason:</span><span class="meta-val badge badge-entry">${entryReasonStr}</span></div>
        <div class="meta-item"><span class="meta-label">Entry:</span><span class="meta-val">₹${(trade.entry_price || 0).toFixed(2)} (${trade.entry_time_str || '-'})</span></div>
        <div class="meta-item"><span class="meta-label">Exit:</span><span class="meta-val">₹${(trade.exit_price || 0).toFixed(2)} (${trade.exit_time_str || '-'})</span></div>
        <div class="meta-item"><span class="meta-label">Exit Reason:</span><span class="meta-val badge badge-reason">${exitReasonStr}</span></div>
        <div class="meta-item"><span class="meta-label">Net Return:</span><span class="meta-val">${this.formatPct(trade.return_pct || 0)}</span></div>
      `;
    }

    modal.classList.add("active");

    const targetDate = trade.date || (this.run ? this.run.date : "2026-11-19");
    const optToken = trade.token || "";

    // Load candles (use pre-computed simulation candles if present)
    let eqAll = Array.isArray(trade.underlying_candles) && trade.underlying_candles.length > 0
      ? trade.underlying_candles
      : null;
    let optAll = Array.isArray(trade.option_candles) && trade.option_candles.length > 0
      ? trade.option_candles
      : null;

    if (!eqAll || !optAll) {
      try {
        const [eqRes, optRes] = await Promise.all([
          !eqAll ? fetch(`/api/chart/candles?date=${targetDate}&symbol=${symbol}&is_option=0&interval=5`) : Promise.resolve(null),
          !optAll && optToken ? fetch(`/api/chart/candles?date=${targetDate}&token=${optToken}&symbol=${symbol}&is_option=1&interval=5`) : Promise.resolve(null),
        ]);

        if (!eqAll && eqRes && eqRes.ok) {
          const eqData = await eqRes.json();
          eqAll = eqData.candles || [];
        }
        if (!optAll && optRes && optRes.ok) {
          const optData = await optRes.json();
          optAll = optData.candles || [];
        }
      } catch (err) {
        console.error("Failed to load candles:", err);
      }
    }

    eqAll = eqAll || [];
    optAll = optAll || [];

    const entryTs = trade.entry_time || 0;
    const exitTs = trade.exit_time || 0;

    const optMarkers = [];
    if (entryTs > 0 && trade.entry_price) {
      optMarkers.push({ timestamp: entryTs, price: trade.entry_price, type: "ENTRY", reason: entryReasonStr });
    }
    if (exitTs > 0 && trade.exit_price) {
      optMarkers.push({ timestamp: exitTs, price: trade.exit_price, type: "EXIT", reason: exitReasonStr });
    }

    const findCandleIndex = (candles, ts) => {
      if (!candles || candles.length === 0 || !ts) return 0;
      if (ts <= candles[0].timestamp) return 0;
      for (let i = 0; i < candles.length - 1; i++) {
        if (ts >= candles[i].timestamp && ts < candles[i + 1].timestamp) {
          return i;
        }
      }
      return candles.length - 1;
    };

    const uMarkers = [];
    if (eqAll.length > 0) {
      for (const m of optMarkers) {
        const cIdx = findCandleIndex(eqAll, m.timestamp);
        const candle = eqAll[cIdx];
        if (candle) {
          let uPrice = null;
          if (m.type === "ENTRY") {
            const rawP = trade.underlying_entry_price || trade.trigger_price || (trade.metadata && (trade.metadata.underlying_entry_price || trade.metadata.trigger_price));
            uPrice = rawP ? Number(rawP) : candle.open;
          } else {
            const rawExit = trade.underlying_exit_price || (trade.metadata && trade.metadata.underlying_exit_price);
            uPrice = rawExit ? Number(rawExit) : candle.close;
          }
          uMarkers.push({
            timestamp: m.timestamp,
            price: uPrice,
            type: m.type,
            reason: m.reason,
          });
        }
      }
    }

    const getLocalized = (candles, pad = 10) => {
      if (!candles || candles.length === 0 || !entryTs) return candles;
      const eIdx = findCandleIndex(candles, entryTs);
      let xIdx = candles.length - 1;
      if (exitTs && exitTs > entryTs) {
        xIdx = findCandleIndex(candles, exitTs);
      }
      return candles.slice(Math.max(0, eIdx - pad), Math.min(candles.length, xIdx + pad + 1));
    };

    if (this.tamUnderlyingLocalChart) {
      this.tamUnderlyingLocalChart.setData(getLocalized(eqAll), uMarkers);
    }
    if (this.tamOptionLocalChart) {
      this.tamOptionLocalChart.setData(getLocalized(optAll), optMarkers);
    }
    if (this.tamUnderlyingFullChart) {
      this.tamUnderlyingFullChart.setData(eqAll, uMarkers);
    }
    if (this.tamOptionFullChart) {
      this.tamOptionFullChart.setData(optAll, optMarkers);
    }

    this.setTamRange(this.tamCurrentRange);
    } catch (e) {
      console.error("Candle fetch error in sim results page:", e);
    }
  },

  formatINR(val, sign = true) {
    const num = Number(val) || 0;
    const abs = Math.abs(num);
    const prefix = sign ? (num > 0 ? "+₹" : num < 0 ? "-₹" : "₹") : "₹";
    const parts = abs.toFixed(2).split(".");
    let lastThree = parts[0].substring(parts[0].length - 3);
    const otherNumbers = parts[0].substring(0, parts[0].length - 3);
    if (otherNumbers !== "") lastThree = "," + lastThree;
    const formatted = otherNumbers.replace(/\B(?=(\d{2})+(?!\d))/g, ",") + lastThree + "." + parts[1];
    return `${prefix}${formatted}`;
  },

  formatPct(val) {
    const num = Number(val) || 0;
    const sign = num > 0 ? "+" : "";
    return `${sign}${num.toFixed(2)}%`;
  },
};

document.addEventListener("DOMContentLoaded", () => {
  window.SimResultsPage.init();
});
