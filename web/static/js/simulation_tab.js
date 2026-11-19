/**
 * SimulationTab: Interactive parameter editing, track composer, async execution, and results matrix.
 */

window.SimulationTab = {
  config: null,
  customTracks: [],
  pollTimer: null,
  selectedTrack: null,
  selectedMode: null,
  chart: null,
  simResults: null,
  currentModeFilter: "all",

  init() {
    this.bindControls();
    const canvas = document.getElementById("sim-chart-canvas");
    if (canvas && window.IntradayChart) {
      this.chart = new window.IntradayChart(canvas);
    }
  },

  onActivate() {
    this.loadConfig();
  },

  bindControls() {
    // Run simulation button
    const runBtn = document.getElementById("sim-run-btn");
    if (runBtn) {
      runBtn.addEventListener("click", () => this.startSimulation());
    }

    // Reset config button
    const resetBtn = document.getElementById("sim-reset-btn");
    if (resetBtn) {
      resetBtn.addEventListener("click", () => this.loadConfig());
    }

    // Add Track button
    const addTrackBtn = document.getElementById("sim-add-track-btn");
    if (addTrackBtn) {
      addTrackBtn.addEventListener("click", () => this.openComposerModal());
    }

    // Modal close & save
    const modalClose = document.getElementById("composer-modal-close");
    if (modalClose) {
      modalClose.addEventListener("click", () => this.closeComposerModal());
    }

    const modalSave = document.getElementById("composer-modal-save");
    if (modalSave) {
      modalSave.addEventListener("click", () => this.saveCustomTrack());
    }

    // Mode filter pills in results
    const pills = document.querySelectorAll(".sim-mode-pill");
    pills.forEach((p) => {
      p.addEventListener("click", () => {
        pills.forEach((x) => x.classList.remove("active"));
        p.classList.add("active");
        this.currentModeFilter = p.getAttribute("data-mode");
        this.renderResultsMatrix();
      });
    });
  },

  async loadConfig() {
    try {
      const res = await fetch("/api/sim/config");
      if (res.ok) {
        this.config = await res.json();
        this.customTracks = [];
        this.populateDateSelect();
        this.populateFormFields();
        this.renderCustomTracksList();

        // Check current status
        this.checkSimStatus();
      }
    } catch (e) {
      console.error("Failed to load simulation config:", e);
    }
  },

  populateDateSelect() {
    const select = document.getElementById("sim-date-select");
    if (!select || !this.config) return;

    select.innerHTML = "";
    const dates = this.config.available_dates || [];
    for (const d of dates) {
      const opt = document.createElement("option");
      opt.value = d.date;
      opt.innerText = `${d.date} ${d.is_cached ? '⚡ Cached' : ''}`;
      select.appendChild(opt);
    }

    if (this.config.selected_date) {
      select.value = this.config.selected_date;
    }
  },

  populateFormFields() {
    if (!this.config) return;
    const g = this.config.global_settings || {};
    const b = this.config.base_tracks || {};

    // Global
    document.getElementById("sim-capital").value = g.capital || 250000;
    document.getElementById("sim-min-delay").value = g.min_delay_ms || 200;
    document.getElementById("sim-max-delay").value = g.max_delay_ms || 500;
    document.getElementById("sim-brokerage").value = g.brokerage_per_order || 30;
    document.getElementById("sim-min-pole").value = ((g.min_pole_pct || 0.005) * 100).toFixed(2);
    document.getElementById("sim-freeze-time").value = g.entry_freeze_time || "15:00";
    document.getElementById("sim-squareoff-time").value = g.eod_squareoff_time || "15:15";

    // Fast Momentum
    const fm = b.fast_momentum || {};
    document.getElementById("fm-target").value = ((fm.target_pct || 0.15) * 100).toFixed(1);
    document.getElementById("fm-sl").value = ((fm.sl_pct || 0.05) * 100).toFixed(1);
    document.getElementById("fm-decay-sec").value = fm.target_decay_sec || 900;
    document.getElementById("fm-decay-floor").value = ((fm.decay_floor_pct || 0.025) * 100).toFixed(1);
    document.getElementById("fm-stagnation").value = fm.stagnation_timeout_sec || 1800;
    document.getElementById("fm-be").value = ((fm.breakeven_trigger_pct || 0.025) * 100).toFixed(1);
    this.setModeCheckboxes("fm-mode", fm.modes || ["ITM", "ATM", "OTM"]);

    // Trend Pullback
    const tp = b.trend_pullback || {};
    document.getElementById("tp-target").value = ((tp.target_pct || 0.25) * 100).toFixed(1);
    document.getElementById("tp-sl").value = ((tp.sl_pct || 0.05) * 100).toFixed(1);
    document.getElementById("tp-trailing-step").value = ((tp.trailing_step_pct || 0.01) * 100).toFixed(1);
    document.getElementById("tp-timeout").value = tp.timeout_sec || 3600;
    document.getElementById("tp-be").value = ((tp.breakeven_trigger_pct || 0.025) * 100).toFixed(1);
    this.setModeCheckboxes("tp-mode", tp.modes || ["ITM", "ATM", "OTM"]);

    // Fixed Target
    const ft = b.fixed_target || {};
    document.getElementById("ft-target").value = ((ft.target_pct || 0.15) * 100).toFixed(1);
    document.getElementById("ft-sl").value = ((ft.sl_pct || 0.05) * 100).toFixed(1);
    document.getElementById("ft-fixed-inr").value = ft.fixed_target_inr || 750;
    document.getElementById("ft-timeout").value = ft.timeout_sec || 1800;
    document.getElementById("ft-be").value = ((ft.breakeven_trigger_pct || 0.025) * 100).toFixed(1);
    this.setModeCheckboxes("ft-mode", ft.modes || ["ITM", "ATM", "OTM"]);
  },

  setModeCheckboxes(prefix, modes) {
    ["ITM", "ATM", "OTM"].forEach((m) => {
      const el = document.getElementById(`${prefix}-${m.toLowerCase()}`);
      if (el) el.checked = modes.includes(m);
    });
  },

  getModeCheckboxes(prefix) {
    const list = [];
    ["ITM", "ATM", "OTM"].forEach((m) => {
      const el = document.getElementById(`${prefix}-${m.toLowerCase()}`);
      if (el && el.checked) list.push(m);
    });
    return list;
  },

  gatherPayload() {
    const date = document.getElementById("sim-date-select").value;

    const modes = [];
    if (document.getElementById("sim-mode-paper").checked) modes.push("paper");
    if (document.getElementById("sim-mode-delayed").checked) modes.push("paper_delayed");

    const global_settings = {
      capital: parseFloat(document.getElementById("sim-capital").value),
      min_delay_ms: parseFloat(document.getElementById("sim-min-delay").value),
      max_delay_ms: parseFloat(document.getElementById("sim-max-delay").value),
      brokerage_per_order: parseFloat(document.getElementById("sim-brokerage").value),
      min_pole_pct: parseFloat(document.getElementById("sim-min-pole").value) / 100.0,
      entry_freeze_time: document.getElementById("sim-freeze-time").value,
      eod_squareoff_time: document.getElementById("sim-squareoff-time").value,
      modes: modes,
    };

    const base_tracks = {
      fast_momentum: {
        enabled: document.getElementById("fm-enable-toggle").checked,
        target_pct: parseFloat(document.getElementById("fm-target").value) / 100.0,
        sl_pct: parseFloat(document.getElementById("fm-sl").value) / 100.0,
        target_decay_sec: parseInt(document.getElementById("fm-decay-sec").value),
        decay_floor_pct: parseFloat(document.getElementById("fm-decay-floor").value) / 100.0,
        stagnation_timeout_sec: parseInt(document.getElementById("fm-stagnation").value),
        breakeven_trigger_pct: parseFloat(document.getElementById("fm-be").value) / 100.0,
        modes: this.getModeCheckboxes("fm-mode"),
      },
      trend_pullback: {
        enabled: document.getElementById("tp-enable-toggle").checked,
        target_pct: parseFloat(document.getElementById("tp-target").value) / 100.0,
        sl_pct: parseFloat(document.getElementById("tp-sl").value) / 100.0,
        trailing_step_pct: parseFloat(document.getElementById("tp-trailing-step").value) / 100.0,
        timeout_sec: parseFloat(document.getElementById("tp-timeout").value),
        breakeven_trigger_pct: parseFloat(document.getElementById("tp-be").value) / 100.0,
        modes: this.getModeCheckboxes("tp-mode"),
      },
      fixed_target: {
        enabled: document.getElementById("ft-enable-toggle").checked,
        target_pct: parseFloat(document.getElementById("ft-target").value) / 100.0,
        sl_pct: parseFloat(document.getElementById("ft-sl").value) / 100.0,
        fixed_target_inr: parseFloat(document.getElementById("ft-fixed-inr").value),
        timeout_sec: parseFloat(document.getElementById("ft-timeout").value),
        breakeven_trigger_pct: parseFloat(document.getElementById("ft-be").value) / 100.0,
        modes: this.getModeCheckboxes("ft-mode"),
      },
    };

    return {
      date: date,
      global_settings: global_settings,
      base_tracks: base_tracks,
      custom_tracks: this.customTracks,
      composite_tracks: this.config ? this.config.composite_tracks : [],
    };
  },

  async startSimulation() {
    const payload = this.gatherPayload();
    const btn = document.getElementById("sim-run-btn");
    const statusText = document.getElementById("sim-status-text");

    if (btn) btn.disabled = true;
    if (statusText) statusText.innerHTML = `<span class="spinner"></span> Launching simulation...`;

    try {
      const res = await fetch("/api/sim/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      if (res.ok) {
        window.App.showToast("Simulation job started...", "info");
        this.pollStatus();
      } else {
        const err = await res.json();
        window.App.showToast(err.error || "Simulation start failed", "error");
        if (btn) btn.disabled = false;
        if (statusText) statusText.innerText = "Error starting";
      }
    } catch (e) {
      window.App.showToast(String(e), "error");
      if (btn) btn.disabled = false;
      if (statusText) statusText.innerText = "Failed";
    }
  },

  pollStatus() {
    if (this.pollTimer) clearInterval(this.pollTimer);

    this.pollTimer = setInterval(async () => {
      try {
        const res = await fetch("/api/sim/status");
        if (res.ok) {
          const st = await res.json();
          const statusText = document.getElementById("sim-status-text");
          const btn = document.getElementById("sim-run-btn");

          if (st.is_running) {
            if (statusText) {
              statusText.innerHTML = `<span class="spinner"></span> ${st.progress_status}`;
            }
          } else {
            clearInterval(this.pollTimer);
            this.pollTimer = null;
            if (btn) btn.disabled = false;

            if (st.error) {
              if (statusText) statusText.innerText = `Error: ${st.error}`;
              window.App.showToast(st.error, "error");
            } else if (st.has_results) {
              if (statusText) statusText.innerText = `Completed in ${st.last_run_time}s! ⚡`;
              window.App.showToast("Simulation completed successfully!", "success");
              this.fetchResults();
            }
          }
        }
      } catch (e) {
        console.error("Status poll error:", e);
      }
    }, 1000);
  },

  async fetchResults() {
    try {
      const res = await fetch("/api/sim/results");
      if (res.ok) {
        const data = await res.json();
        if (data.results) {
          this.simResults = data.results;
          this.renderResultsMatrix();
          const resultsPanel = document.getElementById("sim-results-panel");
          if (resultsPanel) {
            resultsPanel.style.display = "block";
            resultsPanel.scrollIntoView({ behavior: "smooth" });
          }
        }
      }
    } catch (e) {
      console.error("Failed to fetch simulation results:", e);
    }
  },

  renderResultsMatrix() {
    const tbody = document.getElementById("sim-matrix-tbody");
    if (!tbody || !this.simResults || !this.simResults.matrix) return;

    tbody.innerHTML = "";
    const matrix = this.simResults.matrix;

    for (const [mName, mTracks] of Object.entries(matrix)) {
      if (this.currentModeFilter !== "all" && mName !== this.currentModeFilter) {
        continue;
      }

      for (const [trName, st] of Object.entries(mTracks)) {
        const tr = document.createElement("tr");
        const isSelected = this.selectedTrack === trName && this.selectedMode === mName;
        if (isSelected) tr.classList.add("selected");

        const netClass = st.net_pnl >= 0 ? "val-profit" : "val-loss";
        const retClass = st.return_pct >= 0 ? "val-profit" : "val-loss";

        tr.innerHTML = `
          <td><span class="badge badge-${mName}">${mName.toUpperCase()}</span></td>
          <td style="font-weight:600; color:#fff;">${trName}</td>
          <td class="font-mono text-center">${st.trades}</td>
          <td class="font-mono text-center" style="color:var(--accent-green);">${st.wins}</td>
          <td class="font-mono text-center" style="color:var(--accent-amber);">${st.breakevens}</td>
          <td class="font-mono text-center" style="color:var(--accent-red);">${st.losses}</td>
          <td class="font-mono text-center font-bold">${st.win_rate.toFixed(1)}%</td>
          <td class="font-mono text-right">${window.App.formatINR(st.gross_pnl)}</td>
          <td class="font-mono text-right" style="color:var(--text-dim);">${window.App.formatINR(st.total_brokerage, false)}</td>
          <td class="font-mono text-right font-bold ${netClass}">${window.App.formatINR(st.net_pnl)}</td>
          <td class="font-mono text-right">${window.App.formatINR(st.end_capital, false)}</td>
          <td class="font-mono text-right font-bold ${retClass}">${window.App.formatPct(st.return_pct)}</td>
        `;

        tr.addEventListener("click", () => {
          this.selectedMode = mName;
          this.selectedTrack = trName;
          this.renderResultsMatrix();
          this.renderDrilldown(mName, trName);
        });

        tbody.appendChild(tr);
      }
    }

    if (tbody.children.length === 0) {
      tbody.innerHTML = `<tr><td colspan="12" class="text-center" style="padding:24px; color:var(--text-dim);">No simulation results for mode '${this.currentModeFilter}'.</td></tr>`;
    }
  },

  renderDrilldown(mode, track) {
    const detailPanel = document.getElementById("sim-detail-panel");
    if (!detailPanel || !this.simResults || !this.simResults.details) return;

    detailPanel.style.display = "flex";
    const modeDetails = this.simResults.details[mode] || {};
    const trackData = modeDetails[track] || { trades: [], curve: [] };

    document.getElementById("sim-detail-title").innerText = track;
    const badgeEl = document.getElementById("sim-detail-mode-badge");
    if (badgeEl) {
      badgeEl.className = `badge badge-${mode}`;
      badgeEl.innerText = mode.toUpperCase();
    }

    // Performance curve
    if (this.chart) {
      this.chart.setData(trackData.curve || []);
    }

    // Trades list
    const tbody = document.getElementById("sim-trades-tbody");
    if (!tbody) return;
    tbody.innerHTML = "";

    const trades = trackData.trades || [];
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
      tbody.innerHTML = `<tr><td colspan="12" class="text-center" style="padding:24px; color:var(--text-dim);">No simulated trades triggered for this track.</td></tr>`;
    }

    detailPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  },

  // Modal Composer
  openComposerModal() {
    const modal = document.getElementById("composer-modal");
    if (modal) modal.classList.add("active");
  },

  closeComposerModal() {
    const modal = document.getElementById("composer-modal");
    if (modal) modal.classList.remove("active");
  },

  saveCustomTrack() {
    const name = document.getElementById("composer-name").value.trim();
    if (!name) {
      window.App.showToast("Please enter a track name", "error");
      return;
    }

    const type = document.getElementById("composer-type").value;
    const arch = document.getElementById("composer-arch").value;
    const mode = document.getElementById("composer-mode").value;
    const target = parseFloat(document.getElementById("composer-target").value) / 100.0;
    const sl = parseFloat(document.getElementById("composer-sl").value) / 100.0;
    const timeout = parseFloat(document.getElementById("composer-timeout").value);

    const customObj = {
      type: type,
      name: name,
      archetype: arch,
      mode: mode,
      target_pct: target,
      sl_pct: sl,
      timeout_sec: timeout,
    };

    this.customTracks.push(customObj);
    this.renderCustomTracksList();
    this.closeComposerModal();
    window.App.showToast(`Added custom track '${name}'!`, "success");
  },

  renderCustomTracksList() {
    const container = document.getElementById("sim-custom-tracks-container");
    if (!container) return;

    if (this.customTracks.length === 0) {
      container.innerHTML = `<span style="color:var(--text-dim); font-size:11px;">No custom tracks added. Click '+ Add Track' to compose new variants.</span>`;
      return;
    }

    container.innerHTML = "";
    this.customTracks.forEach((c, idx) => {
      const chip = document.createElement("div");
      chip.className = "status-pill";
      chip.style.borderColor = "var(--accent-purple)";
      chip.innerHTML = `
        <span style="color:var(--accent-purple); font-weight:700;">${c.name}</span>
        <span style="color:var(--text-dim);">(${c.archetype} ${c.mode})</span>
        <button style="background:none; border:none; color:var(--text-dim); cursor:pointer; font-size:14px; margin-left:4px;" title="Remove">&times;</button>
      `;
      chip.querySelector("button").addEventListener("click", () => {
        this.customTracks.splice(idx, 1);
        this.renderCustomTracksList();
      });
      container.appendChild(chip);
    });
  },

  async checkSimStatus() {
    try {
      const res = await fetch("/api/sim/status");
      if (res.ok) {
        const st = await res.json();
        const statusText = document.getElementById("sim-status-text");
        if (st.cached_date && statusText) {
          statusText.innerText = `Dataset for ${st.cached_date} is cached in memory ⚡`;
        }
      }
    } catch (e) {}
  },
};
