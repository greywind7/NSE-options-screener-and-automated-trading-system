/**
 * App.js: Central Controller for Tab Switching, WebSocket Lifecycle, and Global State.
 */

window.App = {
  activeTab: "live",
  ws: null,
  wsReconnectTimer: null,

  init() {
    this.bindTabButtons();
    this.connectWebSocket();
    this.startClock();

    // Initialize tabs
    if (window.LiveTab) window.LiveTab.init();
    if (window.HistoricalTab) window.HistoricalTab.init();
    if (window.SimulationTab) window.SimulationTab.init();

    this.initTradeModal();
  },

  bindTabButtons() {
    const btns = document.querySelectorAll(".tab-btn");
    btns.forEach((btn) => {
      btn.addEventListener("click", () => {
        const targetTab = btn.getAttribute("data-tab");
        this.switchTab(targetTab);
      });
    });
  },

  switchTab(tabName) {
    this.activeTab = tabName;

    // Toggle nav active classes
    document.querySelectorAll(".tab-btn").forEach((b) => {
      b.classList.toggle("active", b.getAttribute("data-tab") === tabName);
    });

    // Toggle view containers
    document.querySelectorAll(".tab-view").forEach((v) => {
      v.classList.toggle("active", v.id === `view-${tabName}`);
    });

    // Refresh active tab data
    if (tabName === "live" && window.LiveTab) {
      window.LiveTab.onActivate();
    } else if (tabName === "alerts" && window.AlertsTab) {
      window.AlertsTab.onActivate();
    } else if (tabName === "historical" && window.HistoricalTab) {
      window.HistoricalTab.onActivate();
    } else if (tabName === "simulation" && window.SimulationTab) {
      window.SimulationTab.onActivate();
    } else if (tabName === "sim-results" && window.SimulationTab) {
      window.SimulationTab.onActivateResults();
    }

    if (tabName !== "alerts" && window.AlertsTab) {
      window.AlertsTab.onDeactivate();
    }
  },

  connectWebSocket() {
    if (this.ws) {
      try {
        this.ws.onclose = null;
        this.ws.close();
      } catch (_) {}
      this.ws = null;
    }
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${proto}//${window.location.host}/api/live/ws`;

    try {
      this.ws = new WebSocket(wsUrl);

      this.ws.onopen = () => {
        this.updateSocketStatus(true);
        if (this.wsReconnectTimer) {
          clearInterval(this.wsReconnectTimer);
          this.wsReconnectTimer = null;
        }
      };

      this.ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          if (msg.type === "LIVE_UPDATE" || msg.type === "LIVE_OVERVIEW") {
            if (window.LiveTab) window.LiveTab.onLiveUpdate(msg.payload);
          } else if (msg.type === "TRACK_DETAIL") {
            if (window.LiveTab) window.LiveTab.onTrackDetail(msg.payload);
          }
        } catch (e) {
          console.error("WS Parse Error:", e);
        }
      };

      this.ws.onclose = () => {
        this.updateSocketStatus(false);
        this.scheduleReconnect();
      };

      this.ws.onerror = () => {
        this.updateSocketStatus(false);
      };
    } catch (e) {
      this.updateSocketStatus(false);
      this.scheduleReconnect();
    }
  },

  scheduleReconnect() {
    if (!this.wsReconnectTimer) {
      this.wsReconnectTimer = setInterval(() => {
        this.connectWebSocket();
      }, 3000);
    }
  },

  updateSocketStatus(connected) {
    const dot = document.getElementById("socket-dot");
    const text = document.getElementById("socket-text");
    if (dot && text) {
      if (connected) {
        dot.className = "pulse-dot";
        text.innerText = "Socket Live";
      } else {
        dot.className = "pulse-dot idle";
        text.innerText = "Polling Fallback";
      }
    }
  },

  startClock() {
    const clockEl = document.getElementById("market-clock");
    const updateTime = () => {
      const now = new Date();
      if (clockEl) {
        clockEl.innerText = now.toLocaleTimeString("en-GB", { timeZone: "Asia/Kolkata" }) + " IST";
      }
    };
    updateTime();
    setInterval(updateTime, 1000);
  },

  showToast(message, type = "info") {
    const container = document.getElementById("toast-container");
    if (!container) return;

    const toast = document.createElement("div");
    toast.className = `toast ${type}`;
    toast.innerText = message;
    container.appendChild(toast);

    setTimeout(() => {
      toast.style.opacity = "0";
      toast.style.transition = "opacity 0.3s ease";
      setTimeout(() => toast.remove(), 300);
    }, 3500);
  },

  formatINR(val, showSign = true) {
    if (val === null || val === undefined || isNaN(val)) return "₹0.00";
    const num = Number(val);
    const absStr = Math.abs(num).toLocaleString("en-IN", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
    if (num > 0 && showSign) return `+₹${absStr}`;
    if (num < 0) return `-₹${absStr}`;
    return `₹${absStr}`;
  },

  formatPct(val) {
    if (val === null || val === undefined || isNaN(val)) return "0.0%";
    const num = Number(val);
    const sign = num > 0 ? "+" : "";
    return `${sign}${num.toFixed(1)}%`;
  },

  // =========================================================================
  // DUAL INTRADAY TRADE ANALYSIS MODAL CONTROLLER
  // =========================================================================
  tamUnderlyingLocalChart: null,
  tamOptionLocalChart: null,
  tamUnderlyingFullChart: null,
  tamOptionFullChart: null,
  tamCurrentMode: "candle",
  tamCurrentRange: "local", // "local", "fullday", or "both"
  currentModalTrade: null,
  cachedUnderlyingCandles: [],
  cachedOptionCandles: [],
  cachedMarkers: [],
  cachedUMarkers: [],

  initTradeModal() {
    const modal = document.getElementById("trade-analysis-modal");
    const closeBtn = document.getElementById("tam-close");
    const btnCandle = document.getElementById("tam-mode-candle");
    const btnLine = document.getElementById("tam-mode-line");

    const btnRangeLocal = document.getElementById("tam-range-local");
    const btnRangeFull = document.getElementById("tam-range-fullday");
    const btnRangeBoth = document.getElementById("tam-range-both");

    if (closeBtn && modal) {
      closeBtn.addEventListener("click", () => {
        modal.classList.remove("active");
      });
      modal.addEventListener("click", (e) => {
        if (e.target === modal) modal.classList.remove("active");
      });
    }

    if (btnCandle && btnLine) {
      btnCandle.addEventListener("click", () => {
        btnCandle.classList.add("active");
        btnLine.classList.remove("active");
        this.tamCurrentMode = "candle";
        if (this.tamUnderlyingLocalChart) this.tamUnderlyingLocalChart.setMode("candle");
        if (this.tamOptionLocalChart) this.tamOptionLocalChart.setMode("candle");
        if (this.tamUnderlyingFullChart) this.tamUnderlyingFullChart.setMode("candle");
        if (this.tamOptionFullChart) this.tamOptionFullChart.setMode("candle");
      });

      btnLine.addEventListener("click", () => {
        btnLine.classList.add("active");
        btnCandle.classList.remove("active");
        this.tamCurrentMode = "line";
        if (this.tamUnderlyingLocalChart) this.tamUnderlyingLocalChart.setMode("line");
        if (this.tamOptionLocalChart) this.tamOptionLocalChart.setMode("line");
        if (this.tamUnderlyingFullChart) this.tamUnderlyingFullChart.setMode("line");
        if (this.tamOptionFullChart) this.tamOptionFullChart.setMode("line");
      });
    }

    if (btnRangeLocal && btnRangeFull && btnRangeBoth) {
      btnRangeLocal.addEventListener("click", () => this.setTamRange("local"));
      btnRangeFull.addEventListener("click", () => this.setTamRange("fullday"));
      btnRangeBoth.addEventListener("click", () => this.setTamRange("both"));
    }
  },

  setTamRange(range) {
    this.tamCurrentRange = range;
    const btnLocal = document.getElementById("tam-range-local");
    const btnFull = document.getElementById("tam-range-fullday");
    const btnBoth = document.getElementById("tam-range-both");

    if (btnLocal) btnLocal.classList.toggle("active", range === "local");
    if (btnFull) btnFull.classList.toggle("active", range === "fullday");
    if (btnBoth) btnBoth.classList.toggle("active", range === "both");

    const secLocal = document.getElementById("tam-section-local");
    const secFull = document.getElementById("tam-section-fullday");

    if (secLocal) {
      secLocal.style.display = (range === "local" || range === "both") ? "grid" : "none";
    }
    if (secFull) {
      secFull.style.display = (range === "fullday" || range === "both") ? "grid" : "none";
    }

    // Trigger re-render so canvases compute visible dimensions accurately
    setTimeout(() => {
      if ((range === "local" || range === "both")) {
        if (this.tamUnderlyingLocalChart) { this.tamUnderlyingLocalChart._setupCanvas(); this.tamUnderlyingLocalChart.render(); }
        if (this.tamOptionLocalChart) { this.tamOptionLocalChart._setupCanvas(); this.tamOptionLocalChart.render(); }
      }
      if ((range === "fullday" || range === "both")) {
        if (this.tamUnderlyingFullChart) { this.tamUnderlyingFullChart._setupCanvas(); this.tamUnderlyingFullChart.render(); }
        if (this.tamOptionFullChart) { this.tamOptionFullChart._setupCanvas(); this.tamOptionFullChart.render(); }
      }
    }, 50);
  },

  findCandleIdx(candles, ts) {
    if (!candles || candles.length === 0 || !ts) return 0;
    if (ts <= candles[0].timestamp) return 0;
    for (let i = 0; i < candles.length - 1; i++) {
      if (ts >= candles[i].timestamp && ts < candles[i + 1].timestamp) {
        return i;
      }
    }
    return candles.length - 1;
  },

  getLocalizedCandles(allCandles, entryTs, exitTs, pad = 10) {
    if (!allCandles || allCandles.length === 0) return [];
    if (!entryTs) return allCandles;

    const entryIdx = this.findCandleIdx(allCandles, entryTs);
    let exitIdx = allCandles.length - 1;
    if (exitTs && exitTs > entryTs) {
      exitIdx = this.findCandleIdx(allCandles, exitTs);
    }

    // Window with +- pad candles
    const startIdx = Math.max(0, entryIdx - pad);
    const endIdx = Math.min(allCandles.length, exitIdx + pad + 1);

    return allCandles.slice(startIdx, endIdx);
  },

  async openTradeModal(trade) {
    const modal = document.getElementById("trade-analysis-modal");
    if (!modal) return;
    this.currentModalTrade = trade;
    this.tamCurrentRange = "local";

    const titleEl = document.getElementById("tam-title");
    const pnlBadge = document.getElementById("tam-pnl-badge");
    const statusBadge = document.getElementById("tam-status-badge");
    const metaBar = document.getElementById("tam-meta-bar");

    const symbol = trade.symbol || "UNKNOWN";
    const strike = trade.strike || "";
    const optType = trade.option_type || "";
    const contractDesc = `${strike} ${optType}`.trim();
    const isClosed = trade.status === "CLOSED";

    if (titleEl) {
      titleEl.innerText = `${symbol} ${contractDesc} (Trade #${(trade.trade_id || "").replace("TRD_", "")})`;
    }

    if (statusBadge) {
      statusBadge.className = "badge " + (isClosed ? "badge-closed" : "badge-open");
      statusBadge.innerText = isClosed ? "CLOSED" : "OPEN (ACTIVE)";
    }

    if (pnlBadge) {
      const netPnl = trade.net_pnl !== undefined ? trade.net_pnl : (trade.pnl || 0);
      const retPct = trade.return_pct !== undefined ? trade.return_pct : 0;
      pnlBadge.className = "badge " + (netPnl >= 0 ? "badge-win" : "badge-loss");
      pnlBadge.innerText = `${this.formatINR(netPnl)} (${this.formatPct(retPct)})`;
    }

    if (metaBar) {
      const entryReasonStr = trade.entry_reason || trade.trigger || "Breakout";
      const exitPriceStr = isClosed ? `₹${Number(trade.exit_price || 0).toFixed(2)}` : `₹${Number(trade.current_bid || 0).toFixed(2)} (Bid)`;
      const exitTimeStr = isClosed ? (trade.exit_time_str || "-") : "Active";
      const exitReasonStr = trade.exit_reason || (isClosed ? "Manual / Target" : "Live Holding");
      const bFee = trade.brokerage !== undefined ? `₹${Number(trade.brokerage).toFixed(2)}` : "₹60.00";

      metaBar.innerHTML = `
        <div class="meta-item"><span class="meta-label">Contract:</span> <span class="meta-val">${symbol} ${contractDesc}</span></div>
        <div class="meta-item"><span class="meta-label">Qty:</span> <span class="meta-val">${trade.quantity || 0}</span></div>
        <div class="meta-item"><span class="meta-label">Entry Reason:</span> <span class="meta-val badge badge-entry">${entryReasonStr}</span></div>
        <div class="meta-item"><span class="meta-label">Entry:</span> <span class="meta-val">₹${Number(trade.entry_price || 0).toFixed(2)} @ ${trade.entry_time_str || '-'}</span></div>
        <div class="meta-item"><span class="meta-label">Exit/Bid:</span> <span class="meta-val">${exitPriceStr} @ ${exitTimeStr}</span></div>
        <div class="meta-item"><span class="meta-label">Exit Reason:</span> <span class="meta-val badge badge-reason">${exitReasonStr}</span></div>
        <div class="meta-item"><span class="meta-label">Brokerage:</span> <span class="meta-val">${bFee}</span></div>
        <div class="meta-item"><span class="meta-label">Timeframe:</span> <span class="meta-val" style="color:var(--accent-cyan);">5-Minute Bars</span></div>
      `;
    }

    modal.classList.add("active");

    // Initialize or reset canvas chart instances
    const uLocalCanvas = document.getElementById("tam-underlying-local-canvas");
    const oLocalCanvas = document.getElementById("tam-option-local-canvas");
    const uFullCanvas = document.getElementById("tam-underlying-canvas");
    const oFullCanvas = document.getElementById("tam-option-canvas");

    if (!this.tamUnderlyingLocalChart && uLocalCanvas && window.CandleChart) {
      this.tamUnderlyingLocalChart = new window.CandleChart(uLocalCanvas);
    }
    if (!this.tamOptionLocalChart && oLocalCanvas && window.CandleChart) {
      this.tamOptionLocalChart = new window.CandleChart(oLocalCanvas);
    }
    if (!this.tamUnderlyingFullChart && uFullCanvas && window.CandleChart) {
      this.tamUnderlyingFullChart = new window.CandleChart(uFullCanvas);
    }
    if (!this.tamOptionFullChart && oFullCanvas && window.CandleChart) {
      this.tamOptionFullChart = new window.CandleChart(oFullCanvas);
    }

    // Build markers
    const markers = [];
    const entryTs = Number(trade.entry_time || 0);
    const entryP = Number(trade.entry_price || 0);
    const entryReasonStr = trade.entry_reason || trade.trigger || "Breakout";
    const exitReasonStr = trade.exit_reason || (isClosed ? "Manual / Target" : "Live Holding");
    if (entryTs > 0) {
      markers.push({ timestamp: entryTs, price: entryP, type: "ENTRY", reason: entryReasonStr });
    }
    const exitTs = Number(trade.exit_time || 0);
    const exitP = Number(trade.exit_price || 0);
    if (exitTs > 0 && exitP > 0) {
      markers.push({ timestamp: exitTs, price: exitP, type: "EXIT", reason: exitReasonStr });
    }

    const tradeDate = trade.date || "";

    // Load Underlying 5-Minute Candles (use pre-computed if present)
    let uCandles = Array.isArray(trade.underlying_candles) && trade.underlying_candles.length > 0
      ? trade.underlying_candles
      : [];
    let uMarkers = [];

    if (uCandles.length === 0) {
      try {
        const uRes = await fetch(`/api/chart/candles?date=${encodeURIComponent(tradeDate)}&symbol=${encodeURIComponent(symbol)}&is_option=0&interval=5`);
        if (uRes.ok) {
          const uData = await uRes.json();
          uCandles = uData.candles || [];
        }
      } catch (e) {
        console.error("Failed to load underlying candles:", e);
      }
    }

    if (uCandles.length > 0) {
      for (const m of markers) {
        const cIdx = this.findCandleIdx(uCandles, m.timestamp);
        const closest = uCandles[cIdx];
        if (closest) {
          let uPrice = null;
          if (m.type === "ENTRY") {
            const rawP = trade.underlying_entry_price || trade.trigger_price || (trade.metadata && (trade.metadata.underlying_entry_price || trade.metadata.trigger_price));
            uPrice = rawP ? Number(rawP) : closest.open;
          } else {
            const rawExit = trade.underlying_exit_price || (trade.metadata && trade.metadata.underlying_exit_price);
            uPrice = rawExit ? Number(rawExit) : closest.close;
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

    // Load Option Contract 5-Minute Candles (use pre-computed if present)
    let oCandles = Array.isArray(trade.option_candles) && trade.option_candles.length > 0
      ? trade.option_candles
      : [];

    if (oCandles.length === 0) {
      try {
        const token = trade.token || "";
        const oRes = await fetch(`/api/chart/candles?date=${encodeURIComponent(tradeDate)}&token=${encodeURIComponent(token)}&symbol=${encodeURIComponent(symbol)}&is_option=1&interval=5`);
        if (oRes.ok) {
          const oData = await oRes.json();
          oCandles = oData.candles || [];
        }
      } catch (e) {
        console.error("Failed to load option candles:", e);
      }
    }

    // Calculate Localized Windows (±10 bars)
    const uLocalCandles = this.getLocalizedCandles(uCandles, entryTs, exitTs, 10);
    const oLocalCandles = this.getLocalizedCandles(oCandles, entryTs, exitTs, 10);

    // Apply data to Localized Charts
    if (this.tamUnderlyingLocalChart) {
      this.tamUnderlyingLocalChart.setData(uLocalCandles, uMarkers, this.tamCurrentMode);
    }
    if (this.tamOptionLocalChart) {
      this.tamOptionLocalChart.setData(oLocalCandles, markers, this.tamCurrentMode);
    }

    // Apply data to Full Day Charts
    if (this.tamUnderlyingFullChart) {
      this.tamUnderlyingFullChart.setData(uCandles, uMarkers, this.tamCurrentMode);
    }
    if (this.tamOptionFullChart) {
      this.tamOptionFullChart.setData(oCandles, markers, this.tamCurrentMode);
    }

    // Apply selected range view
    this.setTamRange(this.tamCurrentRange);
  },
};

document.addEventListener("DOMContentLoaded", () => {
  window.App.init();
});
