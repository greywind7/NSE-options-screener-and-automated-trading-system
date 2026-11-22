/**
 * ChartEngine: High-Performance, Zero-Dependency HTML5 Canvas Chart Renderers.
 * Includes:
 *  1. IntradayChart: Smooth PnL curve with Gross/Net brokerage toggle & crosshairs.
 *  2. CandleChart: High-precision 1-Minute Candlestick / Line Chart with Volume bars and Entry/Exit markers.
 */

class IntradayChart {
  constructor(canvasElement) {
    this.canvas = canvasElement;
    this.ctx = canvasElement.getContext("2d");
    this.data = [];
    this.includeBrokerage = true;
    this.hoverIndex = -1;
    this.isHovering = false;

    this.padding = { top: 20, right: 30, bottom: 30, left: 65 };

    this._setupCanvas();
    this._bindEvents();
  }

  _setupCanvas() {
    const isSquare = this.canvas.classList.contains("chart-square") || !!this.canvas.closest(".chart-square-wrapper");
    const dpr = window.devicePixelRatio || 1;
    const rect = this.canvas.getBoundingClientRect();

    let w = rect.width;
    if (!w || w <= 0) {
      w = this.canvas.clientWidth || 600;
    }
    this.width = Math.round(w);

    if (isSquare) {
      // 1:1 square: height matches width exactly
      this.height = this.width;
      this.canvas.style.height = `${this.width}px`;
    } else {
      let h = rect.height;
      if (!h || h <= 0) {
        h = this.canvas.clientHeight || 440;
      }
      this.height = Math.round(h);
    }

    this.canvas.width = Math.round(this.width * dpr);
    this.canvas.height = Math.round(this.height * dpr);
    this.ctx.scale(dpr, dpr);
  }

  _bindEvents() {
    window.addEventListener("resize", () => {
      this._setupCanvas();
      this.render();
    });

    this.canvas.addEventListener("mousemove", (e) => {
      if (!this.data || this.data.length === 0) return;
      const rect = this.canvas.getBoundingClientRect();
      const mouseX = e.clientX - rect.left;
      const plotWidth = this.width - this.padding.left - this.padding.right;

      if (mouseX < this.padding.left || mouseX > this.width - this.padding.right) {
        this.isHovering = false;
        this.hoverIndex = -1;
        this.render();
        return;
      }

      const ratio = (mouseX - this.padding.left) / plotWidth;
      const index = Math.round(ratio * (this.data.length - 1));
      this.hoverIndex = Math.max(0, Math.min(this.data.length - 1, index));
      this.isHovering = true;
      this.render();
    });

    this.canvas.addEventListener("mouseleave", () => {
      this.isHovering = false;
      this.hoverIndex = -1;
      this.render();
    });
  }

  setIncludeBrokerage(val) {
    this.includeBrokerage = !!val;
    this.render();
  }

  setData(points, includeBrokerage = null) {
    this.data = points || [];
    if (includeBrokerage !== null) {
      this.includeBrokerage = !!includeBrokerage;
    }
    this.hoverIndex = -1;
    this.isHovering = false;
    this._setupCanvas();
    this.render();
    if (typeof window !== "undefined" && window.requestAnimationFrame) {
      window.requestAnimationFrame(() => {
        const r = this.canvas.getBoundingClientRect();
        if (r.width > 0 && (Math.abs(r.width - this.width) > 2 || Math.abs(r.height - this.height) > 2)) {
          this._setupCanvas();
          this.render();
        }
      });
    }
  }

  _getPnl(pt) {
    if (!pt) return 0;
    if (this.includeBrokerage) {
      return pt.net_pnl !== undefined ? pt.net_pnl : (pt.pnl !== undefined ? pt.pnl : 0);
    } else {
      return pt.gross_pnl !== undefined ? pt.gross_pnl : (pt.pnl !== undefined ? pt.pnl : 0);
    }
  }

  render() {
    const ctx = this.ctx;
    const w = this.width;
    const h = this.height;
    const p = this.padding;

    ctx.clearRect(0, 0, w, h);

    if (!this.data || this.data.length === 0) {
      ctx.fillStyle = "#64748b";
      ctx.font = "12px sans-serif";
      ctx.textAlign = "center";
      ctx.fillText("No trade performance data available yet", w / 2, h / 2);
      return;
    }

    const plotW = w - p.left - p.right;
    const plotH = h - p.top - p.bottom;

    // Determine min/max PnL based on brokerage toggle
    let minVal = Infinity;
    let maxVal = -Infinity;
    for (const pt of this.data) {
      const val = this._getPnl(pt);
      if (val < minVal) minVal = val;
      if (val > maxVal) maxVal = val;
    }

    // Always ensure 0 is in scale with padding
    minVal = Math.min(0, minVal);
    maxVal = Math.max(0, maxVal);
    const range = (maxVal - minVal) || 1000;
    const buffer = range * 0.15;
    const yMin = minVal - buffer;
    const yMax = maxVal + buffer;

    const getY = (val) => p.top + plotH - ((val - yMin) / (yMax - yMin)) * plotH;
    const getX = (idx) => p.left + (idx / Math.max(1, this.data.length - 1)) * plotW;

    // 1. Grid Lines & Y-Axis Labels
    ctx.lineWidth = 1;
    ctx.strokeStyle = "rgba(255, 255, 255, 0.05)";
    ctx.fillStyle = "#64748b";
    ctx.font = "10px monospace";
    ctx.textAlign = "right";

    const ySteps = 4;
    for (let i = 0; i <= ySteps; i++) {
      const val = yMin + (i / ySteps) * (yMax - yMin);
      const y = getY(val);
      ctx.beginPath();
      ctx.moveTo(p.left, y);
      ctx.lineTo(w - p.right, y);
      ctx.stroke();

      const label = "₹" + Math.round(val).toLocaleString("en-IN");
      ctx.fillText(label, p.left - 8, y + 3);
    }

    // 2. Zero Baseline
    const zeroY = getY(0);
    ctx.save();
    ctx.setLineDash([4, 4]);
    ctx.strokeStyle = "rgba(255, 255, 255, 0.25)";
    ctx.beginPath();
    ctx.moveTo(p.left, zeroY);
    ctx.lineTo(w - p.right, zeroY);
    ctx.stroke();
    ctx.restore();

    // 3. Draw Performance Curve & Area Gradient
    const lastPnl = this._getPnl(this.data[this.data.length - 1]);
    const isProfit = lastPnl >= 0;
    const strokeColor = isProfit ? "#10b981" : "#ef4444";
    const gradientTop = isProfit ? "rgba(16, 185, 129, 0.25)" : "rgba(239, 68, 68, 0.25)";

    // Gradient fill
    const grad = ctx.createLinearGradient(0, p.top, 0, p.top + plotH);
    grad.addColorStop(0, gradientTop);
    grad.addColorStop(1, "rgba(0, 0, 0, 0.0)");

    ctx.beginPath();
    ctx.moveTo(getX(0), getY(this._getPnl(this.data[0])));
    for (let i = 1; i < this.data.length; i++) {
      ctx.lineTo(getX(i), getY(this._getPnl(this.data[i])));
    }
    // Close to zeroY or bottom
    ctx.lineTo(getX(this.data.length - 1), zeroY);
    ctx.lineTo(getX(0), zeroY);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();

    // Line stroke
    ctx.lineWidth = 2;
    ctx.strokeStyle = strokeColor;
    ctx.beginPath();
    ctx.moveTo(getX(0), getY(this._getPnl(this.data[0])));
    for (let i = 1; i < this.data.length; i++) {
      ctx.lineTo(getX(i), getY(this._getPnl(this.data[i])));
    }
    ctx.stroke();

    // 4. X-Axis Time Labels
    ctx.fillStyle = "#64748b";
    ctx.textAlign = "center";
    const xStepCount = Math.min(6, this.data.length);
    for (let i = 0; i < xStepCount; i++) {
      const idx = Math.floor((i / (xStepCount - 1)) * (this.data.length - 1));
      const pt = this.data[idx];
      if (pt && pt.time) {
        ctx.fillText(pt.time, getX(idx), h - p.bottom + 18);
      }
    }

    // 5. Interactive Crosshair & Tooltip
    if (this.isHovering && this.hoverIndex >= 0 && this.hoverIndex < this.data.length) {
      const hPt = this.data[this.hoverIndex];
      const hVal = this._getPnl(hPt);
      const hX = getX(this.hoverIndex);
      const hY = getY(hVal);

      // Vertical guide line
      ctx.save();
      ctx.setLineDash([3, 3]);
      ctx.strokeStyle = "rgba(0, 240, 255, 0.6)";
      ctx.beginPath();
      ctx.moveTo(hX, p.top);
      ctx.lineTo(hX, p.top + plotH);
      ctx.stroke();
      ctx.restore();

      // Point pulse circle
      ctx.beginPath();
      ctx.arc(hX, hY, 5, 0, Math.PI * 2);
      ctx.fillStyle = strokeColor;
      ctx.fill();
      ctx.strokeStyle = "#fff";
      ctx.lineWidth = 2;
      ctx.stroke();

      // Tooltip Box
      const pnlStr = (hVal >= 0 ? "+₹" : "-₹") + Math.abs(hVal).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      const typeStr = this.includeBrokerage ? "Net" : "Gross";
      const tipText = `${hPt.time || ''} | ${typeStr} P&L: ${pnlStr}`;

      ctx.font = "bold 11px monospace";
      const textW = ctx.measureText(tipText).width;
      const boxW = textW + 20;
      const boxH = 24;

      let boxX = hX - boxW / 2;
      if (boxX < p.left) boxX = p.left;
      if (boxX + boxW > w - p.right) boxX = w - p.right - boxW;

      let boxY = hY - 34;
      if (boxY < p.top) boxY = hY + 12;

      ctx.fillStyle = "rgba(20, 27, 41, 0.95)";
      ctx.strokeStyle = "rgba(0, 240, 255, 0.5)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      if (typeof ctx.roundRect === "function") {
        ctx.roundRect(boxX, boxY, boxW, boxH, 4);
      } else {
        ctx.rect(boxX, boxY, boxW, boxH);
      }
      ctx.fill();
      ctx.stroke();

      ctx.fillStyle = hVal >= 0 ? "#10b981" : "#ef4444";
      ctx.textAlign = "center";
      ctx.fillText(tipText, boxX + boxW / 2, boxY + 16);
    }
  }
}

/**
 * CandleChart: Zero-dependency High-Performance Canvas Candlestick & Volume Renderer.
 * Supports Candlestick vs Line toggle, Volume Sub-Panel, and Entry/Exit Markers.
 */
class CandleChart {
  constructor(canvasElement) {
    this.canvas = canvasElement;
    this.ctx = canvasElement.getContext("2d");
    this.candles = [];
    this.markers = []; // [{timestamp, price, type: "ENTRY"|"EXIT", text: ""}]
    this.mode = "candle"; // "candle" or "line"
    this.hoverIndex = -1;
    this.isHovering = false;

    this.padding = { top: 25, right: 65, bottom: 25, left: 20 };

    this._setupCanvas();
    this._bindEvents();
  }

  _setupCanvas() {
    const isSquare = this.canvas.classList.contains("chart-square") || !!this.canvas.closest(".tam-chart-section") || !!this.canvas.closest(".chart-square-wrapper");
    const dpr = window.devicePixelRatio || 1;
    const rect = this.canvas.getBoundingClientRect();

    let w = rect.width;
    if (!w || w <= 0) {
      w = this.canvas.clientWidth || 500;
    }
    this.width = Math.round(w);

    if (isSquare) {
      // Guaranteed 1:1 square: height matches width exactly
      this.height = this.width;
      this.canvas.style.height = `${this.width}px`;
    } else {
      let h = rect.height;
      if (!h || h <= 0) {
        h = this.canvas.clientHeight || 440;
      }
      this.height = Math.round(h);
    }

    this.canvas.width = Math.round(this.width * dpr);
    this.canvas.height = Math.round(this.height * dpr);
    this.ctx.scale(dpr, dpr);
  }

  _bindEvents() {
    window.addEventListener("resize", () => {
      this._setupCanvas();
      this.render();
    });

    this.canvas.addEventListener("mousemove", (e) => {
      if (!this.candles || this.candles.length === 0) return;
      const rect = this.canvas.getBoundingClientRect();
      const mouseX = e.clientX - rect.left;
      const plotW = this.width - this.padding.left - this.padding.right;

      if (mouseX < this.padding.left || mouseX > this.width - this.padding.right) {
        this.isHovering = false;
        this.hoverIndex = -1;
        this.render();
        return;
      }

      const ratio = (mouseX - this.padding.left) / plotW;
      const idx = Math.round(ratio * (this.candles.length - 1));
      this.hoverIndex = Math.max(0, Math.min(this.candles.length - 1, idx));
      this.isHovering = true;
      this.render();
    });

    this.canvas.addEventListener("mouseleave", () => {
      this.isHovering = false;
      this.hoverIndex = -1;
      this.render();
    });
  }

  setData(candles, markers = [], mode = "candle") {
    this.candles = candles || [];
    if (markers && !Array.isArray(markers)) {
      const arr = [];
      if (markers.entryPrice && markers.entryTime) {
        arr.push({
          timestamp: markers.entryTime,
          price: markers.entryPrice,
          type: "ENTRY",
          reason: markers.entryReason || markers.text || "Entry",
        });
      }
      if (markers.exitPrice && markers.exitTime) {
        arr.push({
          timestamp: markers.exitTime,
          price: markers.exitPrice,
          type: "EXIT",
          reason: markers.exitReason || markers.text || "Exit",
        });
      }
      this.markers = arr;
    } else {
      this.markers = markers || [];
    }
    this.mode = mode || "candle";
    this.hoverIndex = -1;
    this.isHovering = false;
    this._setupCanvas();
    this.render();
    if (typeof window !== "undefined" && window.requestAnimationFrame) {
      window.requestAnimationFrame(() => {
        const r = this.canvas.getBoundingClientRect();
        if (r.width > 0 && (Math.abs(r.width - this.width) > 2 || Math.abs(r.height - this.height) > 2)) {
          this._setupCanvas();
          this.render();
        }
      });
    }
  }

  setMode(mode) {
    this.mode = mode;
    this.render();
  }

  render() {
    const ctx = this.ctx;
    const w = this.width;
    const h = this.height;
    const p = this.padding;

    ctx.clearRect(0, 0, w, h);

    if (!this.candles || this.candles.length === 0) {
      ctx.fillStyle = "#64748b";
      ctx.font = "12px sans-serif";
      ctx.textAlign = "center";
      ctx.fillText("No intraday candle data recorded for this instrument.", w / 2, h / 2);
      return;
    }

    const totalPlotH = h - p.top - p.bottom;
    const gap = 12;
    // 80% for price action, 20% for volume
    const pricePlotH = Math.floor((totalPlotH - gap) * 0.80);
    const volPlotH = totalPlotH - gap - pricePlotH;

    const priceTop = p.top;
    const volTop = priceTop + pricePlotH + gap;
    const plotW = w - p.left - p.right;

    // 1. Calculate price range (including markers to prevent clipping)
    let minPrice = Infinity;
    let maxPrice = -Infinity;
    let maxVol = 1;

    for (const c of this.candles) {
      if (c.low < minPrice) minPrice = c.low;
      if (c.high > maxPrice) maxPrice = c.high;
      if (c.volume > maxVol) maxVol = c.volume;
    }

    for (const m of this.markers) {
      if (m.price && m.price > 0) {
        if (m.price < minPrice) minPrice = m.price;
        if (m.price > maxPrice) maxPrice = m.price;
      }
    }

    const pRange = (maxPrice - minPrice) || 1.0;
    const pBuffer = pRange * 0.08;
    const pMin = minPrice - pBuffer;
    const pMax = maxPrice + pBuffer;

    const getPriceY = (val) => priceTop + pricePlotH - ((val - pMin) / (pMax - pMin)) * pricePlotH;
    const getVolY = (vol) => volTop + volPlotH - (vol / maxVol) * volPlotH;
    const getX = (idx) => p.left + (idx / Math.max(1, this.candles.length - 1)) * plotW;

    // Dynamic bar width
    const barW = Math.max(4, Math.min(24, (plotW / Math.max(1, this.candles.length)) * 0.72));

    // 2. Draw Price Grid & Y-Axis Labels
    ctx.lineWidth = 1;
    ctx.strokeStyle = "rgba(255, 255, 255, 0.05)";
    ctx.fillStyle = "#64748b";
    ctx.font = "10px monospace";
    ctx.textAlign = "left";

    const pSteps = 4;
    for (let i = 0; i <= pSteps; i++) {
      const val = pMin + (i / pSteps) * (pMax - pMin);
      const y = getPriceY(val);
      ctx.beginPath();
      ctx.moveTo(p.left, y);
      ctx.lineTo(w - p.right, y);
      ctx.stroke();

      const label = "₹" + val.toFixed(2);
      ctx.fillText(label, w - p.right + 6, y + 3);
    }

    // Divider between Price and Volume
    ctx.strokeStyle = "rgba(255, 255, 255, 0.1)";
    ctx.beginPath();
    ctx.moveTo(p.left, volTop - gap / 2);
    ctx.lineTo(w - p.right, volTop - gap / 2);
    ctx.stroke();

    // Volume Grid & Label
    const vSteps = 2;
    for (let i = 1; i <= vSteps; i++) {
      const vVal = (i / vSteps) * maxVol;
      const y = getVolY(vVal);
      ctx.strokeStyle = "rgba(255, 255, 255, 0.04)";
      ctx.beginPath();
      ctx.moveTo(p.left, y);
      ctx.lineTo(w - p.right, y);
      ctx.stroke();

      const vLabel = vVal >= 1000000 ? (vVal / 1000000).toFixed(1) + "M" : vVal >= 1000 ? Math.round(vVal / 1000) + "K" : Math.round(vVal).toString();
      ctx.fillText(vLabel, w - p.right + 6, y + 3);
    }

    // 3. Draw Volume Bars
    for (let i = 0; i < this.candles.length; i++) {
      const c = this.candles[i];
      const isBull = c.close >= c.open;
      const x = getX(i);
      const y = getVolY(c.volume);
      const barH = (volTop + volPlotH) - y;

      ctx.fillStyle = isBull ? "rgba(16, 185, 129, 0.45)" : "rgba(239, 68, 68, 0.45)";
      ctx.fillRect(x - barW / 2, y, barW, Math.max(1, barH));
    }

    // 4. Draw Price: Candlestick or Line
    if (this.mode === "candle") {
      for (let i = 0; i < this.candles.length; i++) {
        const c = this.candles[i];
        const isBull = c.close >= c.open;
        const color = isBull ? "#10b981" : "#ef4444";
        const x = getX(i);

        // Wick
        const yHigh = getPriceY(c.high);
        const yLow = getPriceY(c.low);
        ctx.strokeStyle = color;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(x, yHigh);
        ctx.lineTo(x, yLow);
        ctx.stroke();

        // Candle Body
        const yOpen = getPriceY(c.open);
        const yClose = getPriceY(c.close);
        const topY = Math.min(yOpen, yClose);
        const bodyH = Math.max(1, Math.abs(yClose - yOpen));

        ctx.fillStyle = color;
        ctx.fillRect(x - barW / 2, topY, barW, bodyH);
      }
    } else {
      // Line Mode
      const firstClose = this.candles[0].close;
      const lastClose = this.candles[this.candles.length - 1].close;
      const isUp = lastClose >= firstClose;
      const strokeColor = isUp ? "#10b981" : "#ef4444";

      // Area gradient
      const grad = ctx.createLinearGradient(0, priceTop, 0, priceTop + pricePlotH);
      grad.addColorStop(0, isUp ? "rgba(16, 185, 129, 0.2)" : "rgba(239, 68, 68, 0.2)");
      grad.addColorStop(1, "rgba(0, 0, 0, 0.0)");

      ctx.beginPath();
      ctx.moveTo(getX(0), getPriceY(this.candles[0].close));
      for (let i = 1; i < this.candles.length; i++) {
        ctx.lineTo(getX(i), getPriceY(this.candles[i].close));
      }
      ctx.lineTo(getX(this.candles.length - 1), priceTop + pricePlotH);
      ctx.lineTo(getX(0), priceTop + pricePlotH);
      ctx.closePath();
      ctx.fillStyle = grad;
      ctx.fill();

      // Stroke
      ctx.lineWidth = 2;
      ctx.strokeStyle = strokeColor;
      ctx.beginPath();
      ctx.moveTo(getX(0), getPriceY(this.candles[0].close));
      for (let i = 1; i < this.candles.length; i++) {
        ctx.lineTo(getX(i), getPriceY(this.candles[i].close));
      }
      ctx.stroke();
    }

    // 5. Draw Entry / Exit Markers (Dots on the chart, labels in corner)
    const markerDetails = [];
    for (const m of this.markers) {
      if (!m.timestamp || !m.price) continue;
      // Find candle containing marker timestamp (range containment)
      let bestIdx = 0;
      if (this.candles.length > 0) {
        if (m.timestamp <= this.candles[0].timestamp) {
          bestIdx = 0;
        } else if (m.timestamp >= this.candles[this.candles.length - 1].timestamp) {
          bestIdx = this.candles.length - 1;
        } else {
          for (let i = 0; i < this.candles.length - 1; i++) {
            if (m.timestamp >= this.candles[i].timestamp && m.timestamp < this.candles[i + 1].timestamp) {
              bestIdx = i;
              break;
            }
          }
        }
      }

      const mX = getX(bestIdx);
      const mY = getPriceY(m.price);
      const isEntry = m.type === "ENTRY";
      const markerColor = isEntry ? "#10b981" : "#ef4444";
      const candleTime = this.candles[bestIdx] ? this.candles[bestIdx].time : "";

      markerDetails.push({
        type: m.type,
        price: m.price,
        time: candleTime,
        color: markerColor,
        reason: m.reason || (isEntry ? "Entry" : "Exit"),
      });

      // Subtle vertical guide line across price plot
      ctx.save();
      ctx.setLineDash([3, 3]);
      ctx.strokeStyle = isEntry ? "rgba(16, 185, 129, 0.45)" : "rgba(239, 68, 68, 0.45)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(mX, priceTop);
      ctx.lineTo(mX, priceTop + pricePlotH);
      ctx.stroke();
      ctx.restore();

      // Outer glow circle
      ctx.beginPath();
      ctx.arc(mX, mY, 6, 0, Math.PI * 2);
      ctx.fillStyle = isEntry ? "rgba(16, 185, 129, 0.35)" : "rgba(239, 68, 68, 0.35)";
      ctx.fill();

      // Solid center dot with white ring
      ctx.beginPath();
      ctx.arc(mX, mY, 3.5, 0, Math.PI * 2);
      ctx.fillStyle = markerColor;
      ctx.fill();
      ctx.strokeStyle = "#ffffff";
      ctx.lineWidth = 1.5;
      ctx.stroke();
    }

    // 6. Draw Corner Legend / Labels for Markers (Top-Left Corner)
    if (markerDetails.length > 0) {
      let cornerX = p.left + 8;
      let cornerY = priceTop + 8;

      for (const md of markerDetails) {
        const symbol = md.type === "ENTRY" ? "▲" : "▼";
        const reasonStr = md.reason ? md.reason : (md.type === "ENTRY" ? "Entry" : "Exit");
        const labelText = `${symbol} ${reasonStr}: ₹${md.price.toFixed(2)}${md.time ? ' (' + md.time + ')' : ''}`;

        ctx.font = "bold 10px monospace";
        const textW = ctx.measureText(labelText).width;
        const bW = textW + 12;
        const bH = 18;

        ctx.fillStyle = "rgba(15, 20, 31, 0.88)";
        ctx.strokeStyle = md.color;
        ctx.lineWidth = 1;
        ctx.beginPath();
        if (typeof ctx.roundRect === "function") {
          ctx.roundRect(cornerX, cornerY, bW, bH, 3);
        } else {
          ctx.rect(cornerX, cornerY, bW, bH);
        }
        ctx.fill();
        ctx.stroke();

        ctx.fillStyle = md.color;
        ctx.textAlign = "left";
        ctx.fillText(labelText, cornerX + 6, cornerY + 12);

        cornerX += bW + 8;
      }
    }

    // 6. X-Axis Time Labels
    ctx.fillStyle = "#64748b";
    ctx.textAlign = "center";
    const xStepCount = Math.min(8, this.candles.length);
    for (let i = 0; i < xStepCount; i++) {
      const idx = Math.floor((i / (xStepCount - 1)) * (this.candles.length - 1));
      const c = this.candles[idx];
      if (c && c.time) {
        ctx.fillText(c.time, getX(idx), h - p.bottom + 16);
      }
    }

    // 7. Interactive Crosshair & Tooltip
    if (this.isHovering && this.hoverIndex >= 0 && this.hoverIndex < this.candles.length) {
      const c = this.candles[this.hoverIndex];
      const hX = getX(this.hoverIndex);
      const hY = getPriceY(c.close);

      // Vertical crosshair through entire chart
      ctx.save();
      ctx.setLineDash([3, 3]);
      ctx.strokeStyle = "rgba(0, 240, 255, 0.6)";
      ctx.beginPath();
      ctx.moveTo(hX, priceTop);
      ctx.lineTo(hX, volTop + volPlotH);
      ctx.stroke();
      ctx.restore();

      // Top floating status bar
      const isBull = c.close >= c.open;
      const statusColor = isBull ? "#10b981" : "#ef4444";
      const tipText = `${c.time} | O: ₹${c.open.toFixed(2)}  H: ₹${c.high.toFixed(2)}  L: ₹${c.low.toFixed(2)}  C: ₹${c.close.toFixed(2)} | Vol: ${c.volume.toLocaleString()}`;

      ctx.font = "bold 11px monospace";
      const textW = ctx.measureText(tipText).width;
      const boxW = textW + 20;
      const boxH = 22;
      const boxX = (w - boxW) / 2;
      const boxY = priceTop - 20;

      ctx.fillStyle = "rgba(15, 23, 42, 0.95)";
      ctx.strokeStyle = "rgba(0, 240, 255, 0.4)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.roundRect(boxX, boxY, boxW, boxH, 4);
      ctx.fill();
      ctx.stroke();

      ctx.fillStyle = statusColor;
      ctx.textAlign = "center";
      ctx.fillText(tipText, boxX + boxW / 2, boxY + 15);
    }
  }
}

// Polyfill roundRect if missing
if (!CanvasRenderingContext2D.prototype.roundRect) {
  CanvasRenderingContext2D.prototype.roundRect = function (x, y, w, h, r) {
    if (w < 2 * r) r = w / 2;
    if (h < 2 * r) r = h / 2;
    this.beginPath();
    this.moveTo(x + r, y);
    this.arcTo(x + w, y, x + w, y + h, r);
    this.arcTo(x + w, y + h, x, y + h, r);
    this.arcTo(x, y + h, x, y, r);
    this.arcTo(x, y, x + w, y, r);
    this.closePath();
    return this;
  };
}

window.IntradayChart = IntradayChart;
window.CandleChart = CandleChart;
