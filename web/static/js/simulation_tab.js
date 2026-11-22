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

  init() {
    this.bindControls();

    const canvas = document.getElementById("sr-chart-canvas");
    if (canvas && window.IntradayChart) {
      this.chart = new window.IntradayChart(canvas);
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
    try {
      localStorage.removeItem("sim_runs");
      if (newBootId) {
        localStorage.setItem("sim_server_boot_id", newBootId);
      }
    } catch (e) {}

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
    } catch (e) {}

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
          startTime: sr.timestamp,
          status: "completed",
          runtime: sr.runtime,
          results: null,
        });
      } else {
        const existing = map.get(sr.id);
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

  onActivateResults() {
    this.populateRunSelect();
    if (this.currentRun && this.currentRun.results) {
      this.renderCurrentRun();
    } else if (this.runs.length > 0) {
      this.currentRun = this.runs[0];
      this.renderCurrentRun();
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
      runSelect.addEventListener("change", (e) => {
        const found = this.runs.find((r) => r.id === e.target.value);
        if (found) {
          this.currentRun = found;
          this.selectedMode = null;
          this.selectedTrack = null;
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
          this.renderRunMatrix();
        }
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
        this.checkSimStatus();
      }
    } catch (e) {
      console.error("Failed to load simulation config:", e);
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

    const runId = `RUN-${this.runs.length + 1}`;

    return {
      run_id: runId,
      date: date,
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

            if (st.is_running) {
              run.progressText = st.progress_status || "Simulating trades...";
              this.renderRunsList();
            } else {
              // Ensure we do not resolve on stale idle state before the background worker sets up
              if (st.current_job_id !== run.id && pollCount <= 15) {
                return;
              }
              clearInterval(timer);
              if (st.error) {
                run.status = "failed";
                run.error = st.error;
              } else {
                run.status = "completed";
                run.runtime = st.last_run_time || 0.0;
                await this.fetchRunResults(run);
              }
              this.renderRunsList();
              this.saveRunsToStorage();
              resolve(run);
            }
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

    if (runBtn) runBtn.disabled = true;
    if (progBox) progBox.style.display = "flex";

    const basePayload = this.gatherPayload();

    for (let i = 0; i < rangeDates.length; i++) {
      const curDate = rangeDates[i];
      const pct = Math.round((i / rangeDates.length) * 100);
      if (progFill) progFill.style.width = `${pct}%`;
      if (progPct) progPct.innerText = `${pct}%`;
      if (progStatus) progStatus.innerText = `Running ${i + 1}/${rangeDates.length}: ${curDate}`;
      if (statusText) statusText.innerText = `Batch simulating: ${curDate}...`;

      const payload = JSON.parse(JSON.stringify(basePayload));
      payload.date = curDate;
      payload.run_id = `RUN-${this.runs.length + 1}`;

      await this.runSingleSimAsync(payload);
      // Clean delay between sequential batch runs to allow worker memory and caches to settle
      await new Promise((r) => setTimeout(r, 400));
    }

    if (progFill) progFill.style.width = "100%";
    if (progPct) progPct.innerText = "100%";
    if (progStatus) progStatus.innerText = `Batch Completed (${rangeDates.length} dates)`;
    if (statusText) statusText.innerText = `Batch simulation completed (${rangeDates.length} runs)! ⚡`;
    if (runBtn) runBtn.disabled = false;

    // Reveal Results Tab in navigation
    const navTab = document.getElementById("nav-tab-sim-results");
    if (navTab) navTab.style.display = "inline-flex";

    window.App.showToast(`Batch completed: ${rangeDates.length} simulations finished!`, "success");
    if (this.runs.length > 0) {
      this.populateRunSelect();
      this.currentRun = this.runs[0];
    }
  },

  pollStatus(run) {
    if (this.pollTimer) clearInterval(this.pollTimer);

    this.pollTimer = setInterval(async () => {
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

  async fetchRunResults(run) {
    try {
      const res = await fetch(`/api/sim/run/${encodeURIComponent(run.id)}`);
      if (res.ok) {
        const data = await res.json();
        if (data.run && data.run.results) {
          run.results = data.run.results;
          run.runtime = data.run.runtime || run.runtime;
          return;
        }
      }
      // Fallback
      const res2 = await fetch("/api/sim/results");
      if (res2.ok) {
        const data2 = await res2.json();
        if (data2.results) run.results = data2.results;
      }
    } catch (e) {
      console.error("Failed to fetch simulation results:", e);
    }
  },

  saveRunsToStorage() {
    try {
      localStorage.setItem("sim_runs", JSON.stringify(this.runs));
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
        let netTotal = 0;
        let totalTrades = 0;
        let totalWins = 0;
        for (const mTracks of Object.values(r.results.matrix)) {
          for (const st of Object.values(mTracks)) {
            netTotal += st.net_pnl || 0;
            totalTrades += st.trades || 0;
            totalWins += st.wins || 0;
          }
        }
        const netClass = netTotal >= 0 ? "val-profit" : "val-loss";
        const wr = totalTrades > 0 ? ((totalWins / totalTrades) * 100).toFixed(1) : "0.0";
        summaryHtml = `
          <div class="sim-run-card-summary">
            <span class="font-mono font-bold ${netClass}">${window.App.formatINR(netTotal)}</span>
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

  showRunResults(run) {
    this.currentRun = run;
    this.selectedMode = null;
    this.selectedTrack = null;

    // Reveal nav tab
    const navTab = document.getElementById("nav-tab-sim-results");
    if (navTab) navTab.style.display = "inline-flex";

    // Switch to tab
    window.App.switchTab("sim-results");
    this.populateRunSelect();
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

    // Compute executive totals averaged over active tracks
    let totalGross = 0;
    let totalFees = 0;
    let totalNet = 0;
    let totalTrades = 0;
    let totalWins = 0;
    let activeTrackCount = 0;

    if (run.results && run.results.matrix) {
      const matrix = run.results.matrix;
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

    const netBadge = document.getElementById("sr-net-badge");
    if (netBadge) {
      netBadge.className = `badge ${avgNet >= 0 ? 'badge-win' : 'badge-loss'}`;
      netBadge.innerText = `Avg: ${window.App.formatINR(avgNet)}`;
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

    // Render Matrix table
    this.renderRunMatrix();

    // Reset drilldown
    const detailPanel = document.getElementById("sr-detail-panel");
    if (detailPanel) detailPanel.style.display = "none";
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
