/**
 * SimulationTab: Interactive parameter editing, track composer, async execution,
 * multi-run session history, and dedicated unclipped Simulation Results Tab + New Browser Tab.
 */

window.SimulationTab = {
  config: null,
  customTracks: [],
  pollTimer: null,
  runs: [],
  currentRun: null,
  selectedTrack: null,
  selectedMode: null,
  chart: null,
  currentModeFilter: "all",
  includeBrokerage: true,
  isParamsCollapsed: false,
  simMode: "single",
  batchChart: null,
  batchWinnersChart: null,
  activeResultsSubview: "single",
  currentBatchId: null,
  batchRankBy: "net_pnl",
  batchRegimeFilter: "ALL",
  batchWindowFilter: "ALL",
  batchModeFilter: "all",
  batchSelectedTrack: "__portfolio__",
  batchCompareDelayed: true,
  batchChartMode: "cumulative",
  currentBatchReport: null,
  batchesList: [],
  _isBatchRunning: false,
  _batchFetchSeq: 0,

  init() {
    this.bindControls();

    const canvas = document.getElementById("sr-chart-canvas");
    if (canvas && window.IntradayChart) {
      this.chart = new window.IntradayChart(canvas);
    }

    const winnersCanvas = document.getElementById("br-winners-canvas");
    if (winnersCanvas && window.BatchWinnersChart) {
      this.batchWinnersChart = new window.BatchWinnersChart(winnersCanvas);
    }

    const batchCanvas = document.getElementById("br-equity-canvas");
    if (batchCanvas && window.BatchEquityDrawdownChart) {
      this.batchChart = new window.BatchEquityDrawdownChart(batchCanvas);
    }

    const bToggle = document.getElementById("sr-brokerage-toggle");
    if (bToggle) {
      bToggle.addEventListener("change", (e) => {
        this.includeBrokerage = e.target.checked;
        if (this.chart) this.chart.setIncludeBrokerage(this.includeBrokerage);
      });
    }

    const btnSquare = document.getElementById("sr-aspect-square");
    const btnWide = document.getElementById("sr-aspect-wide");
    const curveWrapper = document.getElementById("sr-curve-wrapper");
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

    // Synchronize simulation runs with active server session (clears queue if server restarted)
    this.syncWithServerSession();
    this.loadConfig();
  },

  async syncWithServerSession() {
    try {
      const res = await fetch("/api/sim/runs");
      if (res.ok) {
        const data = await res.json();
        const serverBootId = data.server_boot_id;
        const savedBootId = localStorage.getItem("sim_server_boot_id");

        if (serverBootId && savedBootId && savedBootId !== serverBootId) {
          // Server was restarted! Clean up old simulation queue and temporary data
          console.log(`[SimulationTab] Server restart detected (boot ID: ${serverBootId}). Cleared previous session simulations.`);
          this.clearRunsLocally(serverBootId);
          return;
        }

        if (serverBootId && !savedBootId) {
          localStorage.setItem("sim_server_boot_id", serverBootId);
        }

        // Same server session: merge server runs with client runs
        if (data.runs && data.runs.length > 0) {
          this.loadRunsFromServer(data.runs);
        }
      }
    } catch (e) {
      console.warn("Could not sync simulation runs with server session:", e);
    }
  },

  clearRunsLocally(newBootId) {
    this.runs = [];
    this.currentRun = null;
    this.currentBatchReport = null;
    this.batchesList = [];
    this.currentBatchId = null;
    this._batchFetchSeq = (this._batchFetchSeq || 0) + 1;
    try {
      localStorage.removeItem("sim_runs");
      if (newBootId) {
        localStorage.setItem("sim_server_boot_id", newBootId);
      }
    } catch (e) { }

    const navTab = document.getElementById("nav-tab-sim-results");
    if (navTab) navTab.style.display = "none";

    const runSelect = document.getElementById("sr-run-select");
    if (runSelect) runSelect.innerHTML = "";

    this.renderRunsList();
  },

  loadRunsFromServer(serverRuns) {
    let cachedRuns = [];
    try {
      const raw = localStorage.getItem("sim_runs");
      if (raw) cachedRuns = JSON.parse(raw);
    } catch (e) { }

    const map = new Map();
    // 1. Keep any existing in-memory / completed runs first
    this.runs.forEach((r) => map.set(r.id, r));
    cachedRuns.forEach((r) => {
      if (!map.has(r.id)) map.set(r.id, r);
    });

    // 2. Add or update from server runs
    serverRuns.forEach((sr) => {
      if (!map.has(sr.id)) {
        map.set(sr.id, {
          id: sr.id,
          date: sr.date,
          batch_id: sr.batch_id || null,
          startTime: sr.timestamp,
          status: "completed",
          runtime: sr.runtime,
          results: null,
        });
      } else {
        const existing = map.get(sr.id);
        if (!existing.batch_id && sr.batch_id) {
          existing.batch_id = sr.batch_id;
        }
        if (existing.status !== "completed") {
          existing.status = "completed";
          existing.runtime = sr.runtime || existing.runtime;
        }
      }
    });

    this.runs = Array.from(map.values());

    if (this.runs.length > 0) {
      const navTab = document.getElementById("nav-tab-sim-results");
      if (navTab) navTab.style.display = "inline-flex";
      if (!this.currentRun) this.currentRun = this.runs[0];
      this.populateRunSelect();
    }
    this.renderRunsList();
    this.hydrateMissingRuns();
  },

  async hydrateMissingRuns() {
    const missing = this.runs.filter((r) => r.status === "completed" && (!r.results || !r.results.matrix));
    if (missing.length === 0) return;
    for (const r of missing) {
      await this.fetchRunResults(r, false);
    }
    this.saveRunsToStorage();
    this.renderRunsList();
    if (this.currentRun && this.currentRun.results && document.getElementById("tab-sim-results")?.classList.contains("active")) {
      this.renderCurrentRun();
    }
  },

  async clearAllRunsAndTempFiles() {
    try {
      const res = await fetch("/api/sim/clear", { method: "POST" });
      const data = res.ok ? await res.json() : {};
      const bootId = data.server_boot_id || localStorage.getItem("sim_server_boot_id");
      this.clearRunsLocally(bootId);
      if (window.App && window.App.showToast) {
        window.App.showToast("Simulation queue, temporary files, and cached datasets cleared.", "success");
      }
    } catch (e) {
      this.clearRunsLocally();
      if (window.App && window.App.showToast) {
        window.App.showToast("Simulation queue cleared.", "info");
      }
    }
  },

  onActivate() {
    this.syncWithServerSession();
    if (!this.config) {
      this.loadConfig();
    }
    this.renderRunsList();
  },

  async onActivateResults() {
    this.populateRunSelect();
    if (!this.currentRun && this.runs.length > 0) {
      this.currentRun = this.runs[0];
    }
    if (this.currentRun) {
      if (this.currentRun.status === "completed" && (!this.currentRun.results || !this.currentRun.results.details)) {
        await this.fetchRunResults(this.currentRun);
        this.saveRunsToStorage();
      }
      if (this.currentRun.results) {
        this.renderCurrentRun();
      }
    }
  },

  bindControls() {
    // Mode Toggle (Single Day vs Batch Range)
    const btnSingle = document.getElementById("sim-type-single-btn");
    const btnBatch = document.getElementById("sim-type-batch-btn");
    const boxSingle = document.getElementById("sim-single-date-box");
    const boxBatch = document.getElementById("sim-batch-date-box");
    const runBtn = document.getElementById("sim-run-btn");

    if (btnSingle && btnBatch) {
      btnSingle.addEventListener("click", () => {
        btnSingle.classList.add("active");
        btnBatch.classList.remove("active");
        this.simMode = "single";
        if (boxSingle) boxSingle.style.display = "block";
        if (boxBatch) boxBatch.style.display = "none";
        if (runBtn) runBtn.innerHTML = `<span>🚀 Run Historical Simulation</span>`;
      });
      btnBatch.addEventListener("click", () => {
        btnBatch.classList.add("active");
        btnSingle.classList.remove("active");
        this.simMode = "batch";
        if (boxSingle) boxSingle.style.display = "none";
        if (boxBatch) boxBatch.style.display = "block";
        if (runBtn) runBtn.innerHTML = `<span>⚡ Run Batch Simulation</span>`;
      });
    }

    // Run simulation button
    if (runBtn) {
      runBtn.addEventListener("click", () => this.startSimulation());
    }

    // Clear Runs button
    const clearRunsBtn = document.getElementById("sim-clear-runs-btn");
    if (clearRunsBtn) {
      clearRunsBtn.addEventListener("click", () => this.clearAllRunsAndTempFiles());
    }

    // Track Profile selector change
    const profSelect = document.getElementById("sim-track-profile-select");
    if (profSelect) {
      profSelect.addEventListener("change", (e) => {
        this.loadConfig(e.target.value);
      });
    }

    // Reset config button
    const resetBtn = document.getElementById("sim-reset-btn");
    if (resetBtn) {
      resetBtn.addEventListener("click", () => {
        const curProf = document.getElementById("sim-track-profile-select")?.value || "default";
        this.loadConfig(curProf);
      });
    }

    // Add Track button
    const addTrackBtn = document.getElementById("sim-add-track-btn");
    if (addTrackBtn) {
      addTrackBtn.addEventListener("click", () => this.openComposerModal());
    }

    // Modal close & save for Composer
    const modalClose = document.getElementById("composer-modal-close");
    if (modalClose) {
      modalClose.addEventListener("click", () => this.closeComposerModal());
    }

    const modalSave = document.getElementById("composer-modal-save");
    if (modalSave) {
      modalSave.addEventListener("click", () => this.saveCustomTrack());
    }

    // Results Tab: Run selector
    const runSelect = document.getElementById("sr-run-select");
    if (runSelect) {
      runSelect.addEventListener("change", async (e) => {
        const found = this.runs.find((r) => r.id === e.target.value);
        if (found) {
          this.currentRun = found;
          this.selectedMode = null;
          this.selectedTrack = null;
          if (found.status === "completed" && (!found.results || !found.results.details)) {
            await this.fetchRunResults(found);
          }
          this.renderCurrentRun();
        }
      });
    }

    // Results Tab: Open in New Browser Tab button
    const openNewTabBtn = document.getElementById("sr-open-new-tab-btn");
    if (openNewTabBtn) {
      openNewTabBtn.addEventListener("click", () => {
        if (this.currentRun) {
          window.open(`/sim/results?run_id=${encodeURIComponent(this.currentRun.id)}`, "_blank");
        }
      });
    }

    // Results Tab: Back to Simulation Lab button
    const backBtn = document.getElementById("sr-back-to-sim-btn");
    if (backBtn) {
      backBtn.addEventListener("click", () => {
        window.App.switchTab("simulation");
      });
    }

    // Results Tab: Parameters toggle
    const paramsToggle = document.getElementById("sr-params-toggle");
    if (paramsToggle) {
      paramsToggle.addEventListener("click", () => this.toggleParamsCollapse());
    }

    // Mode filter pills in results tab
    const pills = document.querySelectorAll(".sr-mode-pill");
    pills.forEach((p) => {
      p.addEventListener("click", () => {
        pills.forEach((x) => x.classList.remove("active"));
        p.classList.add("active");
        this.currentModeFilter = p.getAttribute("data-mode");
        if (this.currentRun && this.currentRun.results) {
          this.renderRunStats();
          this.renderRunMatrix();
        }
      });
    });

    // Batch Report entrypoints & subview controls
    const openBatchBtn = document.getElementById("sim-open-batch-report-btn");
    if (openBatchBtn) {
      openBatchBtn.addEventListener("click", () => this.openBatchReport(null));
    }

    const backTopBtn = document.getElementById("sr-back-to-sim-top-btn");
    if (backTopBtn) {
      backTopBtn.addEventListener("click", () => window.App.switchTab("simulation"));
    }

    const subSingleBtn = document.getElementById("sr-subview-single-btn");
    const subBatchBtn = document.getElementById("sr-subview-batch-btn");
    if (subSingleBtn) {
      subSingleBtn.addEventListener("click", () => this.toggleResultsSubview("single"));
    }
    if (subBatchBtn) {
      subBatchBtn.addEventListener("click", () => this.openBatchReport(null));
    }

    const brBatchSel = document.getElementById("br-batch-select");
    if (brBatchSel) {
      brBatchSel.addEventListener("change", (e) => {
        this.selectBatch(e.target.value || "__all__", true);
      });
    }

    const brRankSel = document.getElementById("br-rank-select");
    if (brRankSel) {
      brRankSel.addEventListener("change", (e) => {
        this.batchRankBy = e.target.value || "net_pnl";
        document.querySelectorAll(".br-rank-pill").forEach((x) => {
          x.classList.toggle("active", x.getAttribute("data-metric") === this.batchRankBy);
        });
        this.fetchAndRenderBatchReport();
      });
    }

    const brWinSel = document.getElementById("br-window-select");
    if (brWinSel) {
      brWinSel.addEventListener("change", (e) => {
        this.batchWindowFilter = e.target.value || "ALL";
        this.fetchAndRenderBatchReport();
      });
    }

    const brCreateBtn = document.getElementById("br-create-batch-btn");
    if (brCreateBtn) {
      brCreateBtn.addEventListener("click", () => this.snapshotCurrentRunsAsBatch());
    }

    const brMdBtn = document.getElementById("br-export-md-btn");
    if (brMdBtn) brMdBtn.addEventListener("click", () => this.exportBatchReport("md"));

    const brCsvBtn = document.getElementById("br-export-csv-btn");
    if (brCsvBtn) brCsvBtn.addEventListener("click", () => this.exportBatchReport("csv"));

    const brTrackSel = document.getElementById("br-chart-track-select");
    if (brTrackSel) {
      brTrackSel.addEventListener("change", (e) => {
        this.batchSelectedTrack = e.target.value || "__portfolio__";
        if (this.currentBatchReport) {
          this.renderBatchEquityChart(this.currentBatchReport);
        }
      });
    }

    const brDelayToggle = document.getElementById("br-compare-delayed-toggle");
    if (brDelayToggle) {
      brDelayToggle.addEventListener("change", (e) => {
        this.batchCompareDelayed = !!e.target.checked;
        if (this.currentBatchReport) {
          this.renderBatchEquityChart(this.currentBatchReport);
        }
      });
    }

    document.querySelectorAll(".br-chart-mode-pill").forEach((pill) => {
      pill.addEventListener("click", () => {
        document.querySelectorAll(".br-chart-mode-pill").forEach((x) => x.classList.remove("active"));
        pill.classList.add("active");
        this.batchChartMode = pill.getAttribute("data-chart-mode") === "daily" ? "daily" : "cumulative";
        if (this.currentBatchReport) {
          this.renderBatchWinnersChart(this.currentBatchReport);
          this.renderBatchEquityChart(this.currentBatchReport);
        } else {
          if (this.batchWinnersChart) this.batchWinnersChart.setChartMode(this.batchChartMode);
          if (this.batchChart) this.batchChart.setChartMode(this.batchChartMode);
        }
      });
    });

    document.querySelectorAll(".br-regime-pill").forEach((pill) => {
      pill.addEventListener("click", () => {
        document.querySelectorAll(".br-regime-pill").forEach((x) => x.classList.remove("active"));
        pill.classList.add("active");
        this.batchRegimeFilter = pill.getAttribute("data-regime") || "ALL";
        this.fetchAndRenderBatchReport();
      });
    });

    document.querySelectorAll(".br-mode-pill").forEach((pill) => {
      pill.addEventListener("click", () => {
        document.querySelectorAll(".br-mode-pill").forEach((x) => x.classList.remove("active"));
        pill.classList.add("active");
        this.batchModeFilter = pill.getAttribute("data-mode") || "all";
        if (this.currentBatchReport) {
          this.renderBatchLeaderboard(this.currentBatchReport);
        }
      });
    });

    document.querySelectorAll(".br-rank-pill").forEach((pill) => {
      pill.addEventListener("click", () => {
        const metric = pill.getAttribute("data-metric") || "net_pnl";
        document.querySelectorAll(".br-rank-pill").forEach((x) => x.classList.remove("active"));
        pill.classList.add("active");
        this.batchRankBy = metric;
        if (brRankSel) brRankSel.value = metric;
        this.fetchAndRenderBatchReport();
      });
    });

    document.querySelectorAll("#sr-batch-view th.sortable-th").forEach((th) => {
      th.addEventListener("click", () => {
        const rankKey = th.getAttribute("data-rank");
        if (rankKey) {
          this.batchRankBy = rankKey;
          if (brRankSel) brRankSel.value = rankKey;
          document.querySelectorAll(".br-rank-pill").forEach((x) => {
            x.classList.toggle("active", x.getAttribute("data-metric") === rankKey);
          });
          this.fetchAndRenderBatchReport();
        }
      });
    });
  },

  async loadConfig(profile = null) {
    try {
      const activeProf = profile || document.getElementById("sim-track-profile-select")?.value || "default";
      const res = await fetch(`/api/sim/config?profile=${encodeURIComponent(activeProf)}`);
      if (res.ok) {
        this.config = await res.json();
        this.customTracks = [];
        this.populateProfileSelect();
        this.populateDateSelect();
        this.populateGlobalSettings();
        this.renderBaseTracks();
        this.renderCustomTracksList();
        this.checkSimStatus();
      }
    } catch (e) {
      console.error("Failed to load simulation config:", e);
    }
  },

  populateProfileSelect() {
    const sel = document.getElementById("sim-track-profile-select");
    const tag = document.getElementById("sim-active-profile-tag");
    if (!sel || !this.config?.available_profiles) return;
    const current = this.config.active_profile || "default";
    sel.innerHTML = "";
    for (const p of this.config.available_profiles) {
      const opt = document.createElement("option");
      opt.value = p.id;
      opt.innerText = p.name;
      if (p.id === current) opt.selected = true;
      sel.appendChild(opt);
    }
    if (tag) {
      const found = this.config.available_profiles.find((p) => p.id === current);
      tag.innerText = `Profile: ${found ? found.name : current}`;
    }
  },

  populateDateSelect() {
    const select = document.getElementById("sim-date-select");
    const fromSelect = document.getElementById("sim-from-date-select");
    const toSelect = document.getElementById("sim-to-date-select");
    if (!this.config) return;

    const dates = this.config.available_dates || [];

    if (select) {
      select.innerHTML = "";
      for (const d of dates) {
        const opt = document.createElement("option");
        opt.value = d.date;
        opt.innerText = `${d.date} ${d.is_cached ? '⚡ Cached' : ''}`;
        select.appendChild(opt);
      }
      if (this.config.selected_date) {
        select.value = this.config.selected_date;
      }
    }

    if (fromSelect && toSelect) {
      fromSelect.innerHTML = "";
      toSelect.innerHTML = "";
      for (const d of dates) {
        const optF = document.createElement("option");
        optF.value = d.date;
        optF.innerText = d.date;
        fromSelect.appendChild(optF);

        const optT = document.createElement("option");
        optT.value = d.date;
        optT.innerText = d.date;
        toSelect.appendChild(optT);
      }
      if (dates.length > 0) {
        fromSelect.value = dates[0].date;
        toSelect.value = dates[dates.length - 1].date;
      }
    }
  },

  populateGlobalSettings() {
    if (!this.config) return;
    const g = this.config.global_settings || {};
    const capEl = document.getElementById("sim-capital");
    if (capEl) capEl.value = g.capital || 250000;
    const minDelayEl = document.getElementById("sim-min-delay");
    if (minDelayEl) minDelayEl.value = g.min_delay_ms || 200;
    const maxDelayEl = document.getElementById("sim-max-delay");
    if (maxDelayEl) maxDelayEl.value = g.max_delay_ms || 500;
    const brokEl = document.getElementById("sim-brokerage");
    if (brokEl) brokEl.value = g.brokerage_per_order || 30;
    const minPoleEl = document.getElementById("sim-min-pole");
    if (minPoleEl) minPoleEl.value = ((g.min_pole_pct || 0.005) * 100).toFixed(2);
    const frzEl = document.getElementById("sim-freeze-time");
    if (frzEl) frzEl.value = g.entry_freeze_time || "15:00";
    const sqEl = document.getElementById("sim-squareoff-time");
    if (sqEl) sqEl.value = g.eod_squareoff_time || "15:15";
  },

  renderBaseTracks() {
    const container = document.getElementById("sim-base-tracks-container");
    if (!container || !this.config?.base_tracks) return;
    container.innerHTML = "";

    const titles = {
      fast_momentum: { label: "⚡ Fast Momentum Track", color: "var(--accent-cyan)" },
      trend_pullback: { label: "📈 Trend Pullback Track", color: "var(--accent-green)" },
      fixed_target: { label: "🎯 Fixed Target Track", color: "var(--accent-amber)" },
      structural_convexity: { label: "💎 Structural Convexity Track (Underlying Support SL)", color: "var(--accent-cyan)" },
      opening_thrust_convex: { label: "🚀 Opening Thrust Convex Track (09:15-10:30)", color: "var(--accent-green)" },
      delta_scalp: { label: "🎯 Delta Scalp Track (Deep ITM2 - Zero Theta)", color: "#38bdf8" },
      micro_chandelier: { label: "🕯️ Micro-Chandelier Track (Rapid Micro-Trails)", color: "#fbbf24" },
      gamma_runner: { label: "🔥 Gamma Runner Track (OTM2 Convexity)", color: "#f43f5e" },
      volatility_expansion: { label: "🌊 Volatility Expansion Track (IV Thrust)", color: "#a855f7" },
      adaptive_regime: { label: "⏳ Adaptive Regime Track (Multi-Phase)", color: "#34d399" },
      trend_pullback_v2: { label: "📈 Trend Pullback V2 Track (Spread-Calibrated SL)", color: "#2dd4bf" },
    };

    for (const [key, tCfg] of Object.entries(this.config.base_tracks)) {
      const meta = titles[key] || { label: `🛠️ ${key.replace(/_/g, " ").toUpperCase()}`, color: "var(--accent-cyan)" };
      const card = document.createElement("div");
      card.className = "track-card";
      card.id = `card-${key}`;
      card.dataset.trackKey = key;

      let fieldsHtml = "";
      for (const [pKey, pVal] of Object.entries(tCfg)) {
        if (pKey === "modes" || pKey === "enabled") continue;
        const isPct = pKey.endsWith("_pct");
        const valDisp = isPct && typeof pVal === "number" ? (pVal * 100).toFixed(1) : pVal;
        const label = pKey.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
        fieldsHtml += `
          <div class="form-group">
            <label class="form-label">${label}${isPct ? ' %' : ''}</label>
            <input type="${typeof pVal === 'number' ? 'number' : 'text'}" 
                   class="form-input track-field-input" 
                   data-param="${pKey}" 
                   data-is-pct="${isPct}" 
                   value="${valDisp}" 
                   ${typeof pVal === 'number' ? 'step="any"' : ''} />
          </div>`;
      }

      const availableModes = ["ITM2", "ITM1", "ATM", "OTM1", "OTM2"];
      const activeModes = tCfg.modes || ["ATM"];
      let modesHtml = `<div class="form-group" style="grid-column: 1 / -1;">
        <label class="form-label">Strike Modes</label>
        <div class="checkbox-group">`;
      for (const m of availableModes) {
        const isItmAlias = m === "ITM1" && activeModes.includes("ITM");
        const isOtmAlias = m === "OTM1" && activeModes.includes("OTM");
        const emitMode = isItmAlias ? "ITM" : (isOtmAlias ? "OTM" : m);
        const isChecked = activeModes.includes(m) || isItmAlias || isOtmAlias;
        modesHtml += `
          <label class="checkbox-label" style="display:inline-flex; align-items:center; gap:4px; font-size:11px; cursor:pointer;">
            <input type="checkbox" class="track-mode-cb" data-mode="${emitMode}" ${isChecked ? "checked" : ""} /> ${m}
          </label>`;
      }
      modesHtml += `</div></div>`;

      card.innerHTML = `
        <div class="track-card-header">
          <div class="track-card-title">
            <span style="color:${meta.color};">${meta.label}</span>
          </div>
          <div class="checkbox-label">
            <input type="checkbox" class="track-enable-toggle" ${tCfg.enabled !== false ? "checked" : ""} /> Enabled
          </div>
        </div>
        <div class="track-card-body">
          ${fieldsHtml}
          ${modesHtml}
        </div>
      `;
      container.appendChild(card);
    }
  },

  populateFormFields() {
    this.populateGlobalSettings();
    this.renderBaseTracks();
  },

  setModeCheckboxes(prefix, modes) {
    ["ITM2", "ITM1", "ATM", "OTM1", "OTM2"].forEach((m) => {
      const el = document.getElementById(`${prefix}-${m.toLowerCase()}`);
      if (el) el.checked = modes.includes(m);
    });
  },

  getModeCheckboxes(prefix) {
    const list = [];
    ["ITM2", "ITM1", "ATM", "OTM1", "OTM2"].forEach((m) => {
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

    const base_tracks = {};
    const cards = document.querySelectorAll(".track-card[data-track-key]");
    if (cards.length > 0) {
      cards.forEach((card) => {
        const key = card.dataset.trackKey;
        const enabled = card.querySelector(".track-enable-toggle")?.checked ?? true;
        const modes = [];
        card.querySelectorAll(".track-mode-cb:checked").forEach((cb) => modes.push(cb.dataset.mode));
        const params = { enabled, modes };
        card.querySelectorAll(".track-field-input").forEach((inp) => {
          const pKey = inp.dataset.param;
          const isPct = inp.dataset.isPct === "true";
          const rawVal = inp.value;
          const numVal = parseFloat(rawVal);
          params[pKey] = isPct ? (isNaN(numVal) ? 0.0 : numVal / 100.0) : (isNaN(numVal) ? rawVal : numVal);
        });
        base_tracks[key] = params;
      });
    } else if (this.config && this.config.base_tracks) {
      Object.assign(base_tracks, JSON.parse(JSON.stringify(this.config.base_tracks)));
    }

    const selProfile = document.getElementById("sim-track-profile-select")?.value || "default";
    const runId = `RUN-${this.runs.length + 1}`;

    return {
      run_id: runId,
      date: date,
      track_profile: selProfile,
      global_settings: global_settings,
      base_tracks: base_tracks,
      custom_tracks: JSON.parse(JSON.stringify(this.customTracks)),
      composite_tracks: this.config ? this.config.composite_tracks : [],
    };
  },

  startSimulation() {
    if (this.simMode === "batch") {
      this.startBatchSimulation();
    } else {
      this.startSingleSimulation();
    }
  },

  async startSingleSimulation() {
    const payload = this.gatherPayload();
    const btn = document.getElementById("sim-run-btn");
    const statusText = document.getElementById("sim-status-text");

    if (btn) btn.disabled = true;
    if (statusText) statusText.innerHTML = `<span class="spinner"></span> Launching simulation...`;

    const runId = payload.run_id;
    const run = {
      id: runId,
      date: payload.date,
      startTime: new Date().toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }),
      status: "in_progress",
      progressText: "Initializing engine...",
      payload: JSON.parse(JSON.stringify(payload)),
      runtime: null,
      results: null,
      error: null,
    };

    this.runs.unshift(run);
    this.renderRunsList();
    this.saveRunsToStorage();

    try {
      const res = await fetch("/api/sim/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      if (res.ok) {
        window.App.showToast(`Simulation ${runId} launched...`, "info");
        this.pollStatus(run);
      } else {
        const err = await res.json();
        run.status = "failed";
        run.error = err.error || "Failed to start simulation";
        this.renderRunsList();
        this.saveRunsToStorage();
        window.App.showToast(run.error, "error");
        if (btn) btn.disabled = false;
        if (statusText) statusText.innerText = "Error starting";
      }
    } catch (e) {
      run.status = "failed";
      run.error = String(e);
      this.renderRunsList();
      this.saveRunsToStorage();
      window.App.showToast(String(e), "error");
      if (btn) btn.disabled = false;
      if (statusText) statusText.innerText = "Failed";
    }
  },

  async runSingleSimAsync(payload) {
    const runId = payload.run_id;
    let run = this.runs.find((r) => r.id === runId);
    if (!run) {
      run = {
        id: runId,
        date: payload.date,
        batch_id: payload.batch_id || null,
        startTime: new Date().toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }),
        status: "in_progress",
        progressText: "Initializing engine...",
        payload: JSON.parse(JSON.stringify(payload)),
        runtime: null,
        results: null,
        error: null,
      };
      this.runs.unshift(run);
    } else {
      run.batch_id = payload.batch_id || run.batch_id || null;
      run.status = "in_progress";
      run.progressText = "Initializing engine...";
      run.error = null;
    }

    this.renderRunsList();
    this.saveRunsToStorage();

    try {
      const res = await fetch("/api/sim/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      if (!res.ok) {
        const err = await res.json();
        run.status = "failed";
        run.error = err.error || "Failed to start simulation";
        this.renderRunsList();
        this.saveRunsToStorage();
        return run;
      }
    } catch (e) {
      run.status = "failed";
      run.error = String(e);
      this.renderRunsList();
      this.saveRunsToStorage();
      return run;
    }

    return new Promise((resolve) => {
      let pollCount = 0;
      let hasSeenRunning = false;
      const timer = setInterval(async () => {
        try {
          pollCount++;
          const stRes = await fetch("/api/sim/status");
          if (stRes.ok) {
            const st = await stRes.json();
            if (st.server_boot_id) {
              const savedBoot = localStorage.getItem("sim_server_boot_id");
              if (savedBoot && savedBoot !== st.server_boot_id) {
                clearInterval(timer);
                this.clearRunsLocally(st.server_boot_id);
                resolve(run);
                return;
              }
            }

            if (st.is_running && st.current_job_id === run.id) {
              hasSeenRunning = true;
              run.progressText = st.progress_status || "Simulating trades...";
              this.renderRunsList();
              return;
            }

            if (st.is_running && st.current_job_id !== run.id) {
              return;
            }

            // st.is_running is false
            const jobFinished = (st.current_job_id === run.id && (st.has_results || st.error));
            if (!hasSeenRunning && !jobFinished && pollCount <= 15) {
              return;
            }

            clearInterval(timer);
            if (st.error && (st.current_job_id === run.id || !st.current_job_id)) {
              run.status = "failed";
              run.error = st.error;
            } else {
              await this.fetchRunResults(run, false);
              if (run.results) {
                run.status = "completed";
                run.runtime = run.runtime || st.last_run_time || 0.0;
              } else {
                run.status = "failed";
                run.error = st.error || `Results for ${run.id} (${run.date}) not found on server`;
              }
            }
            this.renderRunsList();
            this.saveRunsToStorage();
            resolve(run);
          }
        } catch (e) {
          clearInterval(timer);
          run.status = "failed";
          run.error = String(e);
          this.renderRunsList();
          this.saveRunsToStorage();
          resolve(run);
        }
      }, 700);
    });
  },

  async startBatchSimulation() {
    if (this._isBatchRunning) {
      window.App.showToast("A batch simulation is already running.", "info");
      return;
    }

    const fromEl = document.getElementById("sim-from-date-select");
    const toEl = document.getElementById("sim-to-date-select");
    const fromDate = fromEl ? fromEl.value : "";
    const toDate = toEl ? toEl.value : "";

    const allDates = (this.config?.available_dates || []).map((d) => d.date);
    const rangeDates = allDates.filter((d) => d >= fromDate && d <= toDate);

    if (rangeDates.length === 0) {
      window.App.showToast("No valid recording dates found in selected range.", "error");
      return;
    }

    const runBtn = document.getElementById("sim-run-btn");
    const statusText = document.getElementById("sim-status-text");
    const progBox = document.getElementById("sim-batch-progress");
    const progFill = document.getElementById("sim-batch-bar-fill");
    const progStatus = document.getElementById("sim-batch-status-label");
    const progPct = document.getElementById("sim-batch-pct-label");

    this._isBatchRunning = true;
    if (runBtn) runBtn.disabled = true;
    if (progBox) progBox.style.display = "flex";

    const basePayload = this.gatherPayload();
    this._batchSeq = (this._batchSeq || 0) + 1;
    const batchId = `BATCH-${Date.now()}-${this._batchSeq}`;
    const profileTag = basePayload.track_profile || "default";
    const timeTag = new Date().toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: false });
    const batchLabel = `Batch #${this._batchSeq} (${profileTag}, ${timeTag}): ${fromDate} → ${toDate}`;
    const batchRunIds = [];

    try {
      for (let i = 0; i < rangeDates.length; i++) {
        const curDate = rangeDates[i];
        const pct = Math.round((i / rangeDates.length) * 100);
        if (progFill) progFill.style.width = `${pct}%`;
        if (progPct) progPct.innerText = `${pct}%`;
        if (progStatus) progStatus.innerText = `Running ${i + 1}/${rangeDates.length}: ${curDate}`;
        if (statusText) statusText.innerText = `Batch simulating: ${curDate}...`;

        const payload = JSON.parse(JSON.stringify(basePayload));
        payload.date = curDate;
        this._batchRunSeq = (this._batchRunSeq || 0) + 1;
        payload.run_id = `RUN-${Date.now()}-${this._batchRunSeq}`;
        payload.batch_id = batchId;
        payload.batch_label = batchLabel;

        const completedRun = await this.runSingleSimAsync(payload);
        if (completedRun && completedRun.status === "completed") {
          batchRunIds.push(payload.run_id);
        }

        // Verify server reports idle before launching the next date in the batch
        for (let waitSec = 0; waitSec < 15; waitSec++) {
          try {
            const checkRes = await fetch("/api/sim/status");
            if (checkRes.ok) {
              const checkSt = await checkRes.json();
              if (!checkSt.is_running) break;
            }
          } catch (_) { }
          await new Promise((r) => setTimeout(r, 500));
        }
        // Clean settle delay between sequential batch runs
        await new Promise((r) => setTimeout(r, 400));
      }

      if (batchRunIds.length > 0) {
        try {
          await fetch("/api/sim/batches", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ batch_id: batchId, label: batchLabel, run_ids: batchRunIds }),
          });
        } catch (_) { }
      }

      if (progFill) progFill.style.width = "100%";
      if (progPct) progPct.innerText = "100%";
      if (progStatus) progStatus.innerText = `Batch Completed (${batchRunIds.length}/${rangeDates.length} dates)`;
      if (statusText) statusText.innerText = `Batch simulation completed (${batchRunIds.length}/${rangeDates.length} runs)! ⚡`;

      // Reveal Results Tab in navigation
      const navTab = document.getElementById("nav-tab-sim-results");
      if (navTab) navTab.style.display = "inline-flex";

      if (batchRunIds.length > 0) {
        window.App.showToast(`Batch completed: ${batchRunIds.length} simulations finished!`, "success");
      } else {
        window.App.showToast("Batch finished with no successful runs.", "error");
      }
      if (this.runs.length > 0) {
        this.populateRunSelect();
        this.currentRun = this.runs[0];
      }
      if (batchRunIds.length > 0) {
        this.resetBatchViewFilters();
        await this.openBatchReport(batchId);
      }
    } finally {
      this._isBatchRunning = false;
      if (runBtn) runBtn.disabled = false;
    }
  },

  pollStatus(run) {
    if (this.pollTimer) clearInterval(this.pollTimer);
    const pollStartedAt = Date.now();
    const maxPollMs = 15 * 60 * 1000;

    this.pollTimer = setInterval(async () => {
      if (Date.now() - pollStartedAt > maxPollMs) {
        clearInterval(this.pollTimer);
        this.pollTimer = null;
        const btn = document.getElementById("sim-run-btn");
        if (btn) btn.disabled = false;
        run.status = "failed";
        run.error = "Poll timeout exceeded";
        const statusText = document.getElementById("sim-status-text");
        if (statusText) statusText.innerText = "Error: Poll timeout exceeded";
        this.renderRunsList();
        return;
      }
      try {
        const res = await fetch("/api/sim/status");
        if (res.ok) {
          const st = await res.json();
          const statusText = document.getElementById("sim-status-text");
          const btn = document.getElementById("sim-run-btn");

          if (st.server_boot_id) {
            const savedBoot = localStorage.getItem("sim_server_boot_id");
            if (savedBoot && savedBoot !== st.server_boot_id) {
              clearInterval(this.pollTimer);
              this.pollTimer = null;
              if (btn) btn.disabled = false;
              this.clearRunsLocally(st.server_boot_id);
              if (statusText) statusText.innerText = "Server restarted. Simulation queue cleared.";
              if (window.App && window.App.showToast) {
                window.App.showToast("Server restart detected. Cleared previous simulation runs.", "info");
              }
              return;
            }
          }

          if (st.is_running) {
            run.progressText = st.progress_status || "Simulating trades...";
            if (statusText) {
              statusText.innerHTML = `<span class="spinner"></span> ${run.progressText}`;
            }
            this.renderRunsList();
          } else {
            // If the server hasn't started this job yet, wait
            if (st.current_job_id !== run.id && !st.has_results && !st.error) {
              return;
            }

            clearInterval(this.pollTimer);
            this.pollTimer = null;
            if (btn) btn.disabled = false;

            if (st.error) {
              run.status = "failed";
              run.error = st.error;
              if (statusText) statusText.innerText = `Error: ${st.error}`;
              window.App.showToast(st.error, "error");
              this.renderRunsList();
              this.saveRunsToStorage();
            } else if (st.has_results) {
              run.status = "completed";
              run.runtime = st.last_run_time || 0.0;
              if (statusText) statusText.innerText = `Completed in ${run.runtime}s! ⚡`;
              window.App.showToast(`Simulation ${run.id} finished successfully!`, "success");

              await this.fetchRunResults(run);
              this.renderRunsList();
              this.saveRunsToStorage();

              // Reveal Results Tab in navigation
              const navTab = document.getElementById("nav-tab-sim-results");
              if (navTab) navTab.style.display = "inline-flex";

              // Switch to the clean, unclipped Simulation Results tab!
              this.showRunResults(run);
            }
          }
        }
      } catch (e) {
        console.error("Status poll error:", e);
      }
    }, 1000);
  },

  async fetchRunResults(run, allowGlobalFallback = true) {
    try {
      const res = await fetch(`/api/sim/run/${encodeURIComponent(run.id)}`);
      if (res.ok) {
        const data = await res.json();
        if (data.run && data.run.results && (!data.run.id || data.run.id === run.id)) {
          run.results = data.run.results;
          run.runtime = data.run.runtime || run.runtime;
          run.batch_id = data.run.batch_id || run.batch_id || null;
          return;
        }
      }
      if (!allowGlobalFallback) return;
      // Fallback only for standalone single runs
      const res2 = await fetch("/api/sim/results");
      if (res2.ok) {
        const data2 = await res2.json();
        if (data2.results && (!run.date || data2.results.date === run.date)) {
          run.results = data2.results;
        }
      }
    } catch (e) {
      console.error("Failed to fetch simulation results:", e);
    }
  },

  saveRunsToStorage() {
    try {
      // Strip multi-megabyte trade details/candle arrays so batch histories fit cleanly within 5MB localStorage
      const compact = this.runs.slice(0, 30).map((r) => ({
        id: r.id,
        date: r.date,
        batch_id: r.batch_id || null,
        startTime: r.startTime,
        status: r.status,
        progressText: r.progressText,
        payload: r.payload,
        params: r.params,
        runtime: r.runtime,
        error: r.error,
        results: r.results
          ? { date: r.results.date, cached: r.results.cached, matrix: r.results.matrix }
          : null,
      }));
      localStorage.setItem("sim_runs", JSON.stringify(compact));
    } catch (e) {
      console.warn("Could not save sim_runs to localStorage:", e);
    }
  },

  renderRunsList() {
    const listEl = document.getElementById("sim-runs-list");
    const countEl = document.getElementById("sim-runs-count");
    if (!listEl) return;

    if (countEl) countEl.innerText = `${this.runs.length} Runs`;

    if (this.runs.length === 0) {
      listEl.innerHTML = `
        <div class="text-dim" style="font-size:11px; padding:12px 0; text-align:center;">
          No simulations launched in this session yet.<br/>Configure parameters and click "Run Historical Simulation" above.
        </div>
      `;
      return;
    }

    listEl.innerHTML = "";
    this.runs.forEach((r) => {
      const card = document.createElement("div");
      card.className = `sim-run-card status-${r.status}`;
      if (this.currentRun && this.currentRun.id === r.id) {
        card.classList.add("selected");
      }

      let badgeHtml = "";
      if (r.status === "in_progress") {
        badgeHtml = `<span class="badge" style="background:rgba(0,240,255,0.15); color:var(--accent-cyan); border:1px solid rgba(0,240,255,0.3);"><span class="spinner" style="width:8px; height:8px;"></span> RUNNING</span>`;
      } else if (r.status === "completed") {
        badgeHtml = `<span class="badge badge-win">COMPLETE</span>`;
      } else {
        badgeHtml = `<span class="badge badge-loss">FAILED</span>`;
      }

      let summaryHtml = "";
      if (r.status === "completed" && r.results && r.results.matrix) {
        let totalNet = 0;
        let totalTrades = 0;
        let totalWins = 0;
        const targetTracks = r.results.matrix.paper || Object.values(r.results.matrix)[0] || {};
        const trackCount = Object.keys(targetTracks).length || 1;
        for (const st of Object.values(targetTracks)) {
          totalNet += st.net_pnl || 0;
          totalTrades += st.trades || 0;
          totalWins += st.wins || 0;
        }
        const avgNet = trackCount > 0 ? totalNet / trackCount : 0;
        const netClass = avgNet >= 0 ? "val-profit" : "val-loss";
        const wr = totalTrades > 0 ? ((totalWins / totalTrades) * 100).toFixed(1) : "0.0";
        summaryHtml = `
          <div class="sim-run-card-summary">
            <span class="font-mono font-bold ${netClass}">Avg: ${window.App.formatINR(avgNet)}</span>
            <span style="font-size:10px; color:var(--text-dim);">${wr}% Win (${totalTrades} trds)</span>
            <div style="display:flex; gap:6px;">
              <span class="sim-run-view-btn" style="color:var(--accent-green);">View Tab</span>
              <button class="open-browser-tab-btn" title="Open in new browser tab" style="background:none; border:none; color:var(--accent-cyan); font-size:10px; cursor:pointer; font-weight:700;">↗ New Tab</button>
            </div>
          </div>
        `;
      } else if (r.status === "in_progress") {
        summaryHtml = `
          <div class="sim-run-card-progress">
            <span class="spinner"></span> ${r.progressText || 'Simulating...'}
          </div>
        `;
      } else if (r.status === "failed") {
        summaryHtml = `
          <div class="sim-run-card-error">
            ${r.error || 'Execution encountered an error'}
          </div>
        `;
      }

      card.innerHTML = `
        <div class="sim-run-card-header">
          <span class="sim-run-id">${r.id} • ${r.date}</span>
          ${badgeHtml}
        </div>
        <div class="sim-run-card-meta">
          <span>Started: ${r.startTime}</span>
          ${r.runtime ? `<span>• Runtime: ${r.runtime}s</span>` : ''}
        </div>
        ${summaryHtml}
      `;

      // New tab button handler
      const newTabBtn = card.querySelector(".open-browser-tab-btn");
      if (newTabBtn) {
        newTabBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          window.open(`/sim/results?run_id=${encodeURIComponent(r.id)}`, "_blank");
        });
      }

      // Card click switches to the clean tab
      card.addEventListener("click", () => {
        if (r.status === "completed") {
          this.showRunResults(r);
        } else if (r.status === "failed") {
          window.App.showToast(`Run failed: ${r.error}`, "error");
        } else {
          window.App.showToast(`Simulation is still running: ${r.progressText}`, "info");
        }
      });

      listEl.appendChild(card);
    });
  },

  populateRunSelect() {
    const select = document.getElementById("sr-run-select");
    if (!select) return;

    select.innerHTML = "";
    this.runs.forEach((r) => {
      if (r.status === "completed") {
        const opt = document.createElement("option");
        opt.value = r.id;
        opt.innerText = `${r.id} (${r.date}) — ${r.runtime || '-'}s`;
        select.appendChild(opt);
      }
    });

    if (this.currentRun) {
      select.value = this.currentRun.id;
    }
  },

  async showRunResults(run) {
    this.currentRun = run;
    this.selectedMode = null;
    this.selectedTrack = null;

    // Reveal nav tab
    const navTab = document.getElementById("nav-tab-sim-results");
    if (navTab) navTab.style.display = "inline-flex";

    // Switch to tab and Single Run subview
    window.App.switchTab("sim-results");
    this.toggleResultsSubview("single");
    this.populateRunSelect();
    if (run.status === "completed" && (!run.results || !run.results.details)) {
      await this.fetchRunResults(run);
    }
    this.renderCurrentRun();
  },

  renderCurrentRun() {
    if (!this.currentRun) return;
    const run = this.currentRun;

    // Header info in Tab
    const statusBadge = document.getElementById("sr-status-badge");
    if (statusBadge) {
      statusBadge.className = `badge ${run.status === 'completed' ? 'badge-win' : 'badge-loss'}`;
      statusBadge.innerText = (run.status || 'unknown').toUpperCase();
    }
    const runtimeBadge = document.getElementById("sr-runtime-badge");
    if (runtimeBadge) {
      runtimeBadge.innerText = `Elapsed: ${run.runtime || '-'}s`;
    }

    // Render parameters used
    this.renderRunParams(run);

    // Render executive totals averaged over active (mode, track) entries
    this.renderRunStats();

    // Render Matrix table
    this.renderRunMatrix();

    // Reset drilldown
    const detailPanel = document.getElementById("sr-detail-panel");
    if (detailPanel) detailPanel.style.display = "none";
  },

  renderRunStats() {
    const run = this.currentRun;
    if (!run) return;

    let totalGross = 0;
    let totalFees = 0;
    let totalNet = 0;
    let totalTrades = 0;
    let totalWins = 0;
    let entryCount = 0;
    const targetMode = this.currentModeFilter || "all";

    if (run.results && run.results.matrix) {
      const matrix = run.results.matrix;

      if (targetMode === "all") {
        for (const mTracks of Object.values(matrix)) {
          for (const st of Object.values(mTracks)) {
            entryCount += 1;
            totalGross += st.gross_pnl || 0;
            totalFees += st.total_brokerage || 0;
            totalNet += st.net_pnl || 0;
            totalTrades += st.trades || 0;
            totalWins += st.wins || 0;
          }
        }
      } else {
        const mTracks = matrix[targetMode] || {};
        for (const st of Object.values(mTracks)) {
          entryCount += 1;
          totalGross += st.gross_pnl || 0;
          totalFees += st.total_brokerage || 0;
          totalNet += st.net_pnl || 0;
          totalTrades += st.trades || 0;
          totalWins += st.wins || 0;
        }
      }
    }

    const avgNet = entryCount > 0 ? totalNet / entryCount : 0;
    const avgGross = entryCount > 0 ? totalGross / entryCount : 0;
    const modeTag = targetMode === "paper" ? "Paper" : (targetMode === "paper_delayed" ? "Delayed" : "All");

    const netBadge = document.getElementById("sr-net-badge");
    if (netBadge) {
      netBadge.className = `badge ${avgNet >= 0 ? 'badge-win' : 'badge-loss'}`;
      netBadge.innerText = `Avg (${modeTag}): ${window.App.formatINR(avgNet)}`;
    }

    const statNet = document.getElementById("sr-stat-net-pnl");
    if (statNet) {
      statNet.className = `stat-value font-mono ${avgNet >= 0 ? 'val-profit' : 'val-loss'}`;
      statNet.innerText = window.App.formatINR(avgNet);
    }

    const statGross = document.getElementById("sr-stat-gross-pnl");
    if (statGross) {
      statGross.className = `stat-value font-mono ${avgGross >= 0 ? 'val-profit' : 'val-loss'}`;
      statGross.innerText = window.App.formatINR(avgGross);
    }

    const winRate = totalTrades > 0 ? (totalWins / totalTrades) * 100 : 0;
    const statWr = document.getElementById("sr-stat-win-rate");
    if (statWr) statWr.innerText = `${winRate.toFixed(1)}%`;

    const statTrds = document.getElementById("sr-stat-trades");
    if (statTrds) statTrds.innerText = `${totalTrades} Trades`;

    const statFees = document.getElementById("sr-stat-brokerage");
    if (statFees) statFees.innerText = window.App.formatINR(totalFees, false);
  },

  toggleParamsCollapse() {
    this.isParamsCollapsed = !this.isParamsCollapsed;
    const body = document.getElementById("sr-params-body");
    const btn = document.getElementById("sr-params-toggle");
    if (body) {
      body.classList.toggle("collapsed", this.isParamsCollapsed);
    }
    if (btn) {
      btn.innerText = this.isParamsCollapsed ? "Expand Parameters ▼" : "Collapse Parameters ▲";
    }
  },

  renderRunParams(run) {
    const body = document.getElementById("sr-params-body");
    if (!body) return;

    const p = run.payload || run.params || {};
    const g = p.global_settings || {};
    const b = p.base_tracks || {};
    const c = p.custom_tracks || [];

    const modesStr = (g.modes || ["paper", "paper_delayed"]).map((m) => m.toUpperCase()).join(", ");
    const minPolePct = ((g.min_pole_pct || 0.005) * 100).toFixed(2);

    body.innerHTML = `
      <div class="srm-params-grid">
        <div class="srm-param-chip">
          <span class="chip-label">Recording Date</span>
          <span class="chip-val" style="color:var(--accent-cyan);">${run.date}</span>
        </div>
        <div class="srm-param-chip">
          <span class="chip-label">Active Modes</span>
          <span class="chip-val">${modesStr}</span>
        </div>
        <div class="srm-param-chip">
          <span class="chip-label">Capital / Track</span>
          <span class="chip-val">${window.App.formatINR(g.capital || 250000, false)}</span>
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
        <div style="font-size:11px; margin-top:2px;">
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
      <td class="font-mono text-right">${window.App.formatINR(st.start_capital || 250000, false)}</td>
      <td class="font-mono text-right font-bold ${endClass}">${window.App.formatINR(endCap, false)}</td>
      <td class="font-mono text-right font-bold ${grossClass}">${window.App.formatINR(st.gross_pnl)}</td>
      <td class="font-mono text-right" style="color:var(--text-dim);">${window.App.formatINR(st.total_brokerage, false)}</td>
      <td class="font-mono text-right font-bold ${netClass}">${window.App.formatINR(st.net_pnl)}</td>
      <td class="font-mono text-right font-bold ${retClass}">${window.App.formatPct(st.return_pct)}</td>
    `;

    tr.addEventListener("click", () => {
      this.selectedMode = mName;
      this.selectedTrack = trName;
      this.renderRunMatrix();
      this.renderRunDrilldown(mName, trName);
    });

    tbody.appendChild(tr);
  },

  renderRunMatrix() {
    const tbody = document.getElementById("sr-matrix-tbody");
    if (!tbody || !this.currentRun || !this.currentRun.results || !this.currentRun.results.matrix) return;

    tbody.innerHTML = "";
    const matrix = this.currentRun.results.matrix;

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

  renderRunDrilldown(mode, track) {
    const detailPanel = document.getElementById("sr-detail-panel");
    if (!detailPanel || !this.currentRun || !this.currentRun.results || !this.currentRun.results.details) return;

    detailPanel.style.display = "flex";
    const modeDetails = this.currentRun.results.details[mode] || {};
    const trackData = modeDetails[track] || { trades: [], curve: [] };

    document.getElementById("sr-detail-title").innerText = track;
    const badgeEl = document.getElementById("sr-detail-mode-badge");
    if (badgeEl) {
      badgeEl.className = `badge badge-${mode}`;
      badgeEl.innerText = mode.toUpperCase();
    }

    // Performance curve with brokerage toggle
    if (this.chart) {
      this.chart.setData(trackData.curve || [], this.includeBrokerage);
    }

    // Trades list
    const tbody = document.getElementById("sr-trades-tbody");
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
        <td class="font-mono text-right ${retClass}">${window.App.formatPct(tr.return_pct)}</td>
        <td class="font-mono text-right" style="color:var(--text-dim);">₹${(tr.brokerage || 60).toFixed(2)}</td>
        <td class="font-mono text-right font-bold ${pnlClass}">${window.App.formatINR(tr.net_pnl)}</td>
      `;

      row.addEventListener("click", () => {
        window.App.openTradeModal(tr);
      });

      tbody.appendChild(row);
    }

    const netEl = document.getElementById("sr-detail-net");
    if (netEl) {
      netEl.className = `stat-value font-mono ${netTrackPnl >= 0 ? 'val-profit' : 'val-loss'}`;
      netEl.innerText = window.App.formatINR(netTrackPnl);
    }

    if (trades.length === 0) {
      tbody.innerHTML = `<tr><td colspan="13" class="text-center" style="padding:24px; color:var(--text-dim);">No simulated trades triggered for this track.</td></tr>`;
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

  // =========================================================================
  // BATCH REPORT & MARKET REGIME ANALYTICS UI
  // =========================================================================
  toggleResultsSubview(viewName) {
    this.activeResultsSubview = viewName === "batch" ? "batch" : "single";
    const singleEl = document.getElementById("sr-single-view");
    const batchEl = document.getElementById("sr-batch-view");
    const btnSingle = document.getElementById("sr-subview-single-btn");
    const btnBatch = document.getElementById("sr-subview-batch-btn");

    if (singleEl) singleEl.style.display = this.activeResultsSubview === "single" ? "block" : "none";
    if (batchEl) batchEl.style.display = this.activeResultsSubview === "batch" ? "block" : "none";
    if (btnSingle) btnSingle.classList.toggle("active", this.activeResultsSubview === "single");
    if (btnBatch) btnBatch.classList.toggle("active", this.activeResultsSubview === "batch");

    if (this.activeResultsSubview === "batch") {
      setTimeout(() => {
        if (this.batchWinnersChart) {
          this.batchWinnersChart._setupCanvas();
          this.batchWinnersChart.render();
        }
        if (this.batchChart) {
          this.batchChart._setupCanvas();
          this.batchChart.render();
        }
      }, 20);
    }
  },

  resetBatchViewFilters() {
    this.batchSelectedTrack = "__portfolio__";
    this.batchRegimeFilter = "ALL";
    this.batchWindowFilter = "ALL";

    const winSel = document.getElementById("br-window-select");
    if (winSel) winSel.value = "ALL";
    document.querySelectorAll(".br-regime-pill").forEach((pill) => {
      pill.classList.toggle("active", pill.getAttribute("data-regime") === "ALL");
    });
    const trkSel = document.getElementById("br-chart-track-select");
    if (trkSel) trkSel.value = "__portfolio__";
  },

  selectBatch(batchId, shouldFetch = true) {
    const resolved = batchId || "__all__";
    const changed = resolved !== this.currentBatchId;
    this.currentBatchId = resolved;
    const sel = document.getElementById("br-batch-select");
    if (sel) sel.value = resolved;
    if (changed) {
      this.resetBatchViewFilters();
    } else {
      this.batchSelectedTrack = "__portfolio__";
    }
    if (shouldFetch) {
      this.fetchAndRenderBatchReport();
    }
  },

  async openBatchReport(batchId = null) {
    const navTab = document.getElementById("nav-tab-sim-results");
    if (navTab) navTab.style.display = "inline-flex";
    window.App.switchTab("sim-results");
    this.toggleResultsSubview("batch");
    if (batchId && batchId !== this.currentBatchId) {
      this.resetBatchViewFilters();
    }
    if (batchId !== null && batchId !== undefined) {
      this.currentBatchId = batchId;
    }
    await this.loadBatchList(batchId);
    await this.fetchAndRenderBatchReport();
  },

  async loadBatchList(preferredId = null) {
    const sel = document.getElementById("br-batch-select");
    try {
      const res = await fetch("/api/sim/batches");
      if (!res.ok) return;
      const data = await res.json();
      this.batchesList = data.batches || [];
      this.renderMultiBatchComparison(data.comparison || []);

      if (sel) {
        sel.innerHTML = `<option value="__all__">All Runs (Latest Per Date — ${data.completed_runs_count || 0})</option>`;
        this.batchesList.forEach((b) => {
          const opt = document.createElement("option");
          opt.value = b.batch_id;
          opt.innerText = `${b.label || b.batch_id} (${b.sessions}d: ${b.date_start} → ${b.date_end})`;
          sel.appendChild(opt);
        });

        const hasOpt = (val) => !!val && Array.from(sel.options).some((o) => o.value === val);
        const latestBatchId = this.batchesList.length > 0 ? this.batchesList[this.batchesList.length - 1].batch_id : null;
        let nextId = "__all__";
        if (hasOpt(preferredId)) {
          nextId = preferredId;
        } else if (this.currentBatchId && this.currentBatchId !== "__all__" && hasOpt(this.currentBatchId)) {
          nextId = this.currentBatchId;
        } else if (hasOpt(latestBatchId)) {
          nextId = latestBatchId;
        }
        if (nextId !== this.currentBatchId) {
          this.resetBatchViewFilters();
        }
        this.currentBatchId = nextId;
        sel.value = this.currentBatchId;
      }
    } catch (e) {
      console.warn("Could not load batch list:", e);
    }
  },

  async snapshotCurrentRunsAsBatch() {
    const completedRuns = this.runs.filter((r) => r.status === "completed");
    const unbatchedRuns = completedRuns.filter((r) => !r.batch_id);
    const targetRuns = unbatchedRuns.length > 0 ? unbatchedRuns : completedRuns;
    const targetIds = targetRuns.map((r) => r.id);
    if (targetIds.length === 0) {
      window.App.showToast("No completed runs available to snapshot.", "error");
      return;
    }
    const defaultLabel = unbatchedRuns.length > 0
      ? `Single Runs Snapshot (${targetIds.length} runs)`
      : `Combined Snapshot (${targetIds.length} runs)`;
    const label = prompt("Enter a label for this Batch Snapshot (e.g. V3 Gate Tuning):", defaultLabel);
    if (label === null) return;
    try {
      const res = await fetch("/api/sim/batches", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label: label.trim() || defaultLabel, run_ids: targetIds }),
      });
      if (res.ok) {
        const data = await res.json();
        const bid = data.batch?.batch_id;
        this.runs.forEach((r) => {
          if (targetIds.includes(r.id) && !r.batch_id) r.batch_id = bid;
        });
        this.saveRunsToStorage();
        this.resetBatchViewFilters();
        window.App.showToast(`Saved batch '${data.batch?.label || bid}'`, "success");
        await this.loadBatchList(bid);
        await this.fetchAndRenderBatchReport();
      }
    } catch (e) {
      window.App.showToast("Failed to save batch snapshot.", "error");
    }
  },

  async fetchAndRenderBatchReport() {
    const targetBatchId = this.currentBatchId || "__all__";
    const bid = encodeURIComponent(targetBatchId);
    const params = new URLSearchParams({
      regime: this.batchRegimeFilter || "ALL",
      window: this.batchWindowFilter || "ALL",
      rank_by: this.batchRankBy || "net_pnl",
    });

    this._batchFetchSeq = (this._batchFetchSeq || 0) + 1;
    const reqSeq = this._batchFetchSeq;

    try {
      const res = await fetch(`/api/sim/batch/${bid}?${params.toString()}`);
      if (reqSeq !== this._batchFetchSeq || targetBatchId !== (this.currentBatchId || "__all__")) {
        return;
      }
      if (!res.ok) {
        this.currentBatchReport = null;
        this.renderEmptyBatchReport();
        return;
      }
      const data = await res.json();
      if (reqSeq !== this._batchFetchSeq || targetBatchId !== (this.currentBatchId || "__all__")) {
        return;
      }
      if (data.report) {
        this.currentBatchReport = data.report;
        this.renderBatchReport(data.report);
      } else {
        this.currentBatchReport = null;
        this.renderEmptyBatchReport();
      }
    } catch (e) {
      if (reqSeq === this._batchFetchSeq) {
        console.warn("Error fetching batch report:", e);
      }
    }
  },

  renderEmptyBatchReport() {
    const setText = (id, txt) => {
      const el = document.getElementById(id);
      if (el) el.innerText = txt;
    };
    setText("br-stat-sessions", "0 / 0");
    setText("br-stat-daterange", "-");
    setText("br-stat-net-pnl", "₹0.00");
    setText("br-stat-gross-pnl", "Gross: ₹0.00");
    setText("br-stat-max-dd", "0.00%");
    setText("br-stat-max-dd-inr", "₹0.00");
    setText("br-stat-daily-wr", "0.0%");
    setText("br-stat-green-days", "0 / 0 Green Days");
    setText("br-stat-tracks", "0");
    setText("br-stat-modes", "-");

    ["br-spotlights-grid", "br-regime-breakdown", "br-intraday-windows-bar", "br-winners-legend"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.innerHTML = "";
    });
    setText("br-winners-subtitle", "0 Unique Winners");
    setText("br-chart-subtitle", "Portfolio Equal-Weighted");

    const trkSel = document.getElementById("br-chart-track-select");
    if (trkSel) trkSel.innerHTML = `<option value="__portfolio__">Equal-Weighted Portfolio Avg</option>`;

    const lb = document.getElementById("br-leaderboard-tbody");
    if (lb) {
      lb.innerHTML = `<tr><td colspan="16" class="text-center" style="padding:24px; color:var(--text-dim);">No completed batch runs found yet. Run a Single or Batch simulation first!</td></tr>`;
    }
    const sess = document.getElementById("br-sessions-tbody");
    if (sess) {
      sess.innerHTML = `<tr><td colspan="8" class="text-center" style="padding:18px; color:var(--text-dim);">No sessions recorded.</td></tr>`;
    }
    if (this.batchWinnersChart) this.batchWinnersChart.setData([]);
    if (this.batchChart) this.batchChart.setData([], [], "Portfolio", "Delayed");
  },

  renderMultiBatchComparison(comparison) {
    const panel = document.getElementById("br-comparison-panel");
    const tbody = document.getElementById("br-comparison-tbody");
    if (!panel || !tbody) return;

    if (!comparison || comparison.length < 2) {
      panel.style.display = "none";
      return;
    }

    panel.style.display = "block";
    tbody.innerHTML = "";
    comparison.forEach((c) => {
      const tr = document.createElement("tr");
      tr.style.cursor = "pointer";
      const netCls = c.portfolio_avg_net_pnl >= 0 ? "val-profit" : "val-loss";
      const topCls = c.top_track_net_pnl >= 0 ? "val-profit" : "val-loss";
      tr.innerHTML = `
        <td style="font-weight:700; color:#fff;">${c.batch_label || c.batch_id}</td>
        <td class="font-mono">${c.date_range}</td>
        <td class="font-mono text-center">${c.sessions}</td>
        <td class="font-mono text-right font-bold ${netCls}">${window.App.formatINR(c.portfolio_avg_net_pnl)}</td>
        <td class="font-mono text-right val-loss">-${c.portfolio_max_dd_pct.toFixed(2)}%</td>
        <td class="font-mono text-right">${c.portfolio_daily_win_rate_pct.toFixed(1)}%</td>
        <td class="font-mono" style="color:var(--accent-cyan);">${c.top_track}</td>
        <td class="font-mono text-right font-bold ${topCls}">${window.App.formatINR(c.top_track_net_pnl)}</td>
        <td class="font-mono">${c.best_calmar_track} (${c.best_calmar.toFixed(2)}x)</td>
      `;
      tr.addEventListener("click", () => {
        this.selectBatch(c.batch_id, true);
      });
      tbody.appendChild(tr);
    });
  },

  renderBatchReport(report) {
    const sm = report.summary || {};

    const elSess = document.getElementById("br-stat-sessions");
    if (elSess) elSess.innerText = `${sm.filtered_sessions || 0} / ${sm.total_sessions || 0}`;
    const elDates = document.getElementById("br-stat-daterange");
    if (elDates) elDates.innerText = sm.date_start ? `${sm.date_start} → ${sm.date_end}` : "-";

    const netVal = sm.portfolio_avg_net_pnl || 0;
    const elNet = document.getElementById("br-stat-net-pnl");
    if (elNet) {
      elNet.className = `stat-value font-mono ${netVal >= 0 ? "val-profit" : "val-loss"}`;
      elNet.innerText = window.App.formatINR(netVal);
    }
    const elGross = document.getElementById("br-stat-gross-pnl");
    if (elGross) elGross.innerText = `Gross: ${window.App.formatINR(sm.portfolio_avg_gross_pnl || 0)}`;

    const elDdPct = document.getElementById("br-stat-max-dd");
    if (elDdPct) {
      elDdPct.className = "stat-value font-mono val-loss";
      elDdPct.innerText = `-${(sm.portfolio_max_dd_pct || 0).toFixed(2)}%`;
    }
    const elDdInr = document.getElementById("br-stat-max-dd-inr");
    if (elDdInr) elDdInr.innerText = window.App.formatINR(-(sm.portfolio_max_dd_inr || 0));

    const elDwr = document.getElementById("br-stat-daily-wr");
    if (elDwr) elDwr.innerText = `${(sm.portfolio_daily_win_rate_pct || 0).toFixed(1)}%`;
    const elGreen = document.getElementById("br-stat-green-days");
    if (elGreen) elGreen.innerText = `${sm.portfolio_green_days || 0} / ${sm.filtered_sessions || 0} Green Days`;

    const elTrk = document.getElementById("br-stat-tracks");
    if (elTrk) elTrk.innerText = `${sm.track_count || 0}`;
    const elModes = document.getElementById("br-stat-modes");
    if (elModes) elModes.innerText = (sm.modes || []).map((m) => m.toUpperCase()).join(", ") || "-";

    this.renderBatchSpotlights(report.spotlights || {});
    this.renderBatchRegimes(report);
    this.populateBatchChartTrackSelect(report);
    this.renderBatchWinnersChart(report);
    this.renderBatchEquityChart(report);
    this.renderBatchLeaderboard(report);
    this.renderBatchTimeline(report);
  },

  SPOTLIGHT_METRIC_SPECS: [
    { key: "highest_net", title: "🏆 Highest Net P&L", tag: "Net P&L", color: "#10b981" },
    { key: "best_calmar", title: "📐 Best Calmar (Ret/DD)", tag: "Calmar", color: "#00f0ff" },
    { key: "most_consistent", title: "📈 Most Consistent (45°)", tag: "45° Slope", color: "#38bdf8" },
    { key: "max_green_days", title: "📅 Max Green Days", tag: "Max Green", color: "#a3e635" },
    { key: "lowest_drawdown", title: "🛡️ Lowest Max Drawdown", tag: "Low DD", color: "#f59e0b" },
    { key: "best_delayed_retention", title: "⚡ Best Delayed Retention", tag: "Delayed Ret", color: "#c084fc" },
  ],

  renderBatchSpotlights(spotlights) {
    const grid = document.getElementById("br-spotlights-grid");
    if (!grid) return;

    grid.innerHTML = "";
    this.SPOTLIGHT_METRIC_SPECS.forEach((c) => {
      const item = spotlights[c.key];
      if (!item) return;
      const div = document.createElement("div");
      div.className = "br-spotlight-card";
      div.style.cursor = "pointer";
      div.style.borderTop = `2px solid ${c.color}`;
      const cvStr = (item.pnl_cv !== null && item.pnl_cv !== undefined) ? `CV ${item.pnl_cv} | ` : "";
      div.innerHTML = `
        <div class="br-spotlight-label" style="color:${c.color};">${c.title}</div>
        <div class="br-spotlight-track">${item.track}</div>
        <div class="br-spotlight-val" style="color:${c.color};">${item.value_display}</div>
        <div class="br-spotlight-meta">${cvStr}Daily WR ${item.daily_win_rate_pct}% | DD -${item.max_drawdown_pct}%</div>
      `;
      div.addEventListener("click", () => {
        this.batchSelectedTrack = `${item.mode}::${item.track}`;
        const sel = document.getElementById("br-chart-track-select");
        if (sel) sel.value = this.batchSelectedTrack;
        if (this.currentBatchReport) {
          this.renderBatchEquityChart(this.currentBatchReport);
          this.renderBatchLeaderboard(this.currentBatchReport);
        }
      });
      grid.appendChild(div);
    });
  },

  renderBatchRegimes(report) {
    const regCounts = report.regime_counts || {};
    const total = report.summary?.total_sessions || 0;
    document.querySelectorAll(".br-regime-pill").forEach((pill) => {
      const rKey = pill.getAttribute("data-regime");
      const baseLabels = {
        ALL: "All Regimes",
        TREND_UP: "Trend Up",
        TREND_DOWN: "Trend Down",
        CHOPPY_RANGE: "Choppy/Range",
        V_REVERSAL: "V-Reversal",
        HIGH_VOL_EXPIRY: "High-Vol/Expiry",
      };
      const cnt = rKey === "ALL" ? total : (regCounts[rKey] || 0);
      pill.innerText = `${baseLabels[rKey] || rKey} (${cnt})`;
    });

    const strip = document.getElementById("br-regime-breakdown");
    if (!strip) return;
    strip.innerHTML = "";
    (report.regime_breakdown || []).forEach((rb) => {
      const card = document.createElement("div");
      card.className = "br-regime-mini-card";
      const cls = rb.avg_track_session_pnl >= 0 ? "val-profit" : "val-loss";
      card.innerHTML = `
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <span class="badge badge-regime-${rb.regime}">${rb.label}</span>
          <span class="font-mono" style="font-size:11px; color:var(--text-dim);">${rb.sessions}d</span>
        </div>
        <div class="font-mono font-bold ${cls}" style="font-size:13px;">Avg: ${window.App.formatINR(rb.avg_track_session_pnl)} / day</div>
        <div class="font-mono" style="font-size:10px; color:var(--text-muted);">Green: ${rb.green_sessions}/${rb.sessions} | WR: ${rb.win_rate_pct}% (${rb.total_trades} tr)</div>
      `;
      strip.appendChild(card);
    });
  },

  populateBatchChartTrackSelect(report) {
    const sel = document.getElementById("br-chart-track-select");
    if (!sel) return;
    const prev = this.batchSelectedTrack || "__portfolio__";
    sel.innerHTML = `<option value="__portfolio__">Equal-Weighted Portfolio Avg</option>`;

    const seen = new Set();
    (report.leaderboard || []).forEach((row) => {
      const key = `${row.mode}::${row.track}`;
      if (!seen.has(key)) {
        seen.add(key);
        const opt = document.createElement("option");
        opt.value = key;
        opt.innerText = `[${row.mode.toUpperCase()}] ${row.track} (${window.App.formatINR(row.net_pnl)})`;
        sel.appendChild(opt);
      }
    });

    const hasPrev = Array.from(sel.options).some((o) => o.value === prev);
    this.batchSelectedTrack = hasPrev ? prev : "__portfolio__";
    sel.value = this.batchSelectedTrack;
  },

  renderBatchWinnersChart(report) {
    const legendEl = document.getElementById("br-winners-legend");
    const subEl = document.getElementById("br-winners-subtitle");
    const spotlights = report.spotlights || {};
    const leaderboard = report.leaderboard || [];

    // Deduplicate spotlight winners by mode::track, collecting all metric colors won
    const dedupMap = new Map();
    this.SPOTLIGHT_METRIC_SPECS.forEach((sp) => {
      const item = spotlights[sp.key];
      if (!item) return;
      const key = `${item.mode}::${item.track}`;
      if (!dedupMap.has(key)) {
        const row = leaderboard.find((r) => r.mode === item.mode && r.track === item.track);
        if (row && Array.isArray(row.equity_curve) && row.equity_curve.length > 0) {
          dedupMap.set(key, {
            key,
            track: item.track,
            mode: item.mode,
            color: sp.color,
            colors: [sp.color],
            badges: [sp.tag],
            curve: row.equity_curve,
          });
        }
      } else {
        const existing = dedupMap.get(key);
        existing.colors.push(sp.color);
        existing.badges.push(sp.tag);
      }
    });

    const winnersList = Array.from(dedupMap.values());
    const modeTag = this.batchChartMode === "daily" ? "Day-by-Day" : "Cumulative";
    if (subEl) subEl.innerText = `${winnersList.length} Unique Winner${winnersList.length === 1 ? "" : "s"} (${modeTag})`;

    if (legendEl) {
      legendEl.innerHTML = "";
      winnersList.forEach((w) => {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "br-winner-chip";
        const dotsHtml = w.colors.map((c) => `<span class="br-winner-dot" style="background:${c};"></span>`).join("");
        chip.innerHTML = `${dotsHtml}<span>${w.track}</span><span style="color:var(--text-dim);">(${w.badges.join(" · ")})</span>`;
        chip.addEventListener("click", () => {
          this.batchSelectedTrack = w.key;
          const sel = document.getElementById("br-chart-track-select");
          if (sel) sel.value = w.key;
          this.renderBatchEquityChart(report);
          this.renderBatchLeaderboard(report);
        });
        legendEl.appendChild(chip);
      });
    }

    if (this.batchWinnersChart) {
      this.batchWinnersChart.setChartMode(this.batchChartMode);
      this.batchWinnersChart.setData(winnersList);
    }
  },

  renderBatchEquityChart(report) {
    if (!this.batchChart) return;
    const sub = document.getElementById("br-chart-subtitle");
    const leaderboard = report.leaderboard || [];
    const portCurves = report.portfolio_curves || {};

    let primary = [];
    let compare = [];
    let pLabel = "Portfolio (PAPER)";
    let cLabel = "Portfolio (DELAYED)";

    if (!this.batchSelectedTrack || this.batchSelectedTrack === "__portfolio__") {
      const primaryMode = portCurves.paper ? "paper" : Object.keys(portCurves)[0];
      primary = (primaryMode && portCurves[primaryMode]) || [];
      pLabel = `Portfolio Equal-Weighted (${(primaryMode || "paper").toUpperCase()})`;
      if (this.batchCompareDelayed && primaryMode !== "paper_delayed" && portCurves.paper_delayed) {
        compare = portCurves.paper_delayed;
        cLabel = "Portfolio (PAPER_DELAYED)";
      }
    } else {
      const parts = this.batchSelectedTrack.split("::");
      const selMode = parts[0];
      const selTrack = parts.slice(1).join("::");
      const row = leaderboard.find((r) => r.mode === selMode && r.track === selTrack);
      primary = (row && row.equity_curve) || [];
      pLabel = `${selTrack} (${selMode.toUpperCase()})`;
      if (this.batchCompareDelayed) {
        const peerMode = selMode === "paper" ? "paper_delayed" : "paper";
        const peer = leaderboard.find((r) => r.mode === peerMode && r.track === selTrack);
        if (peer && peer.equity_curve) {
          compare = peer.equity_curve;
          cLabel = `${selTrack} (${peerMode.toUpperCase()})`;
        }
      }
    }

    const modeTag = this.batchChartMode === "daily" ? "Day-by-Day" : "Cumulative";
    if (sub) sub.innerText = `${pLabel} • ${modeTag}`;
    this.batchChart.setChartMode(this.batchChartMode);
    this.batchChart.setData(primary, compare, pLabel, cLabel);
  },

  renderBatchLeaderboard(report) {
    const tbody = document.getElementById("br-leaderboard-tbody");
    if (!tbody) return;
    tbody.innerHTML = "";

    const rows = (report.leaderboard || []).filter((r) => {
      if (this.batchModeFilter === "all") return true;
      return r.mode === this.batchModeFilter;
    });

    if (rows.length === 0) {
      tbody.innerHTML = `<tr><td colspan="16" class="text-center" style="padding:24px; color:var(--text-dim);">No tracks match current regime/mode filter.</td></tr>`;
      return;
    }

    rows.forEach((r, idx) => {
      const tr = document.createElement("tr");
      const key = `${r.mode}::${r.track}`;
      if (this.batchSelectedTrack === key) tr.classList.add("selected");
      const netCls = r.net_pnl >= 0 ? "val-profit" : "val-loss";
      const retCls = r.return_pct >= 0 ? "val-profit" : "val-loss";
      const brokPct = r.brokerage_drag_pct !== null && r.brokerage_drag_pct !== undefined ? `${r.brokerage_drag_pct.toFixed(1)}%` : "-";
      const pfText = r.profit_factor_inf ? "INF" : r.profit_factor.toFixed(2);
      const delText = r.delayed_retention_pct !== null && r.delayed_retention_pct !== undefined ? `${r.delayed_retention_pct.toFixed(1)}%` : "-";
      const slopeVal = (r.linearity_score !== null && r.linearity_score !== undefined) ? `${r.linearity_score.toFixed(1)}` : "-";

      tr.innerHTML = `
        <td class="font-mono text-center" style="color:var(--text-dim);">${idx + 1}</td>
        <td><span class="badge badge-${r.mode}">${r.mode.toUpperCase()}</span></td>
        <td style="font-weight:600; color:#fff;">${r.track}</td>
        <td class="font-mono text-center">${r.sessions}</td>
        <td class="font-mono text-center">${r.daily_win_rate_pct.toFixed(1)}% <span style="color:var(--text-dim); font-size:10px;">(${r.green_days}/${r.sessions})</span></td>
        <td class="font-mono text-center">${r.trades}</td>
        <td class="font-mono text-center">${r.win_rate_pct.toFixed(1)}%</td>
        <td class="font-mono text-right font-bold ${netCls}">${window.App.formatINR(r.net_pnl)}</td>
        <td class="font-mono text-right ${retCls}">${window.App.formatPct(r.return_pct)}</td>
        <td class="font-mono text-right" style="color:var(--text-muted);">${brokPct}</td>
        <td class="font-mono text-right" style="color:#38bdf8;">${slopeVal}</td>
        <td class="font-mono text-right ${r.expectancy_inr >= 0 ? "val-profit" : "val-loss"}">${window.App.formatINR(r.expectancy_inr)}</td>
        <td class="font-mono text-right">${pfText}</td>
        <td class="font-mono text-right val-loss">-${r.max_drawdown_pct.toFixed(2)}%</td>
        <td class="font-mono text-right" style="color:var(--accent-cyan); font-weight:700;">${r.calmar_ratio.toFixed(2)}</td>
        <td class="font-mono text-right">${delText}</td>
      `;
      tr.addEventListener("click", () => {
        this.batchSelectedTrack = key;
        const sel = document.getElementById("br-chart-track-select");
        if (sel) sel.value = key;
        this.renderBatchLeaderboard(report);
        this.renderBatchEquityChart(report);
      });
      tbody.appendChild(tr);
    });
  },

  renderBatchTimeline(report) {
    const winBar = document.getElementById("br-intraday-windows-bar");
    if (winBar) {
      winBar.innerHTML = "";
      const activeWin = this.batchWindowFilter || "ALL";
      (report.intraday_breakdown || []).forEach((wb) => {
        const cls = wb.net_pnl >= 0 ? "val-profit" : "val-loss";
        const chip = document.createElement("div");
        chip.className = "srm-param-chip";
        chip.style.flex = "1";
        chip.style.cursor = "pointer";
        if (activeWin === wb.window) {
          chip.style.borderColor = "var(--accent-cyan)";
        }
        chip.innerHTML = `
          <span class="chip-label">${wb.label} (by Entry Time)</span>
          <span class="chip-val ${cls}">${window.App.formatINR(wb.net_pnl)} <span style="color:var(--text-muted); font-size:10px;">(${wb.trades}tr | ${wb.win_rate_pct}% WR)</span></span>
        `;
        chip.addEventListener("click", () => {
          const nextWin = activeWin === wb.window ? "ALL" : wb.window;
          this.batchWindowFilter = nextWin;
          const winSel = document.getElementById("br-window-select");
          if (winSel) winSel.value = nextWin;
          this.fetchAndRenderBatchReport();
        });
        winBar.appendChild(chip);
      });
    }

    const sessTbody = document.getElementById("br-sessions-tbody");
    if (sessTbody) {
      sessTbody.innerHTML = "";
      (report.sessions || []).forEach((s) => {
        const tr = document.createElement("tr");
        tr.style.cursor = "pointer";
        tr.title = "Click to inspect this date's Single Run Report";
        const bCls = s.basket_return_pct >= 0 ? "val-profit" : "val-loss";
        const pNet = s.paper_avg_net_pnl;
        const dNet = s.delayed_avg_net_pnl;
        const pCls = (pNet ?? 0) >= 0 ? "val-profit" : "val-loss";
        const dCls = (dNet ?? 0) >= 0 ? "val-profit" : "val-loss";
        const topCls = (s.top_track_pnl ?? 0) >= 0 ? "val-profit" : "val-loss";
        tr.innerHTML = `
          <td class="font-mono" style="color:#fff;">${s.date}</td>
          <td><span class="badge badge-regime-${s.regime}">${s.regime_label}</span></td>
          <td class="font-mono text-right font-bold ${bCls}">${window.App.formatPct(s.basket_return_pct)}</td>
          <td class="font-mono text-center">${s.trades ?? "-"}</td>
          <td class="font-mono text-center">${s.win_rate_pct !== undefined ? `${s.win_rate_pct.toFixed(1)}%` : "-"}</td>
          <td class="font-mono text-right font-bold ${pCls}">${pNet !== null && pNet !== undefined ? window.App.formatINR(pNet) : "-"}</td>
          <td class="font-mono text-right font-bold ${dCls}">${dNet !== null && dNet !== undefined ? window.App.formatINR(dNet) : "-"}</td>
          <td class="font-mono" style="font-size:11px;">
            <span style="color:var(--accent-cyan); font-weight:600;">${s.top_track || "-"}</span>
            <span class="${topCls}" style="margin-left:4px;">(${window.App.formatINR(s.top_track_pnl || 0)})</span>
          </td>
        `;
        tr.addEventListener("click", async () => {
          let found = this.runs.find((r) => r.id === s.run_id);
          if (!found && s.run_id) {
            try {
              const res = await fetch(`/api/sim/run/${encodeURIComponent(s.run_id)}`);
              if (res.ok) {
                const data = await res.json();
                if (data.run && data.run.results) {
                  found = {
                    id: data.run.id,
                    date: data.run.date,
                    batch_id: data.run.batch_id || null,
                    startTime: data.run.timestamp || "-",
                    status: "completed",
                    runtime: data.run.runtime || 0,
                    params: data.run.params || {},
                    results: data.run.results,
                  };
                  this.runs.unshift(found);
                  this.populateRunSelect();
                  this.renderRunsList();
                }
              }
            } catch (e) {
              console.warn("Could not hydrate run from batch session click:", e);
            }
          }
          if (found) {
            this.showRunResults(found);
          } else {
            window.App.showToast(`Run '${s.run_id}' is no longer in server memory.`, "error");
          }
        });
        sessTbody.appendChild(tr);
      });
    }
  },

  exportBatchReport(fmt) {
    const bid = encodeURIComponent(this.currentBatchId || "__all__");
    const params = new URLSearchParams({
      format: fmt || "md",
      regime: this.batchRegimeFilter || "ALL",
      window: this.batchWindowFilter || "ALL",
      rank_by: this.batchRankBy || "net_pnl",
    });
    window.open(`/api/sim/batch/${bid}/export?${params.toString()}`, "_blank");
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
    } catch (e) { }
  },
};
