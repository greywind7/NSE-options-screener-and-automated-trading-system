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
    } else if (tabName === "historical" && window.HistoricalTab) {
      window.HistoricalTab.onActivate();
    } else if (tabName === "simulation" && window.SimulationTab) {
      window.SimulationTab.onActivate();
    }
  },

  connectWebSocket() {
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
};

document.addEventListener("DOMContentLoaded", () => {
  window.App.init();
});
