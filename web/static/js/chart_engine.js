/**
 * ChartEngine: High-Performance, Zero-Dependency HTML5 Canvas Intraday Curve Renderer.
 * Supports DPI scaling, smooth Bézier curves, zero baseline, gradients, and crosshair tooltips.
 */

class IntradayChart {
  constructor(canvasElement) {
    this.canvas = canvasElement;
    this.ctx = canvasElement.getContext("2d");
    this.data = [];
    this.hoverIndex = -1;
    this.isHovering = false;

    this.padding = { top: 20, right: 30, bottom: 30, left: 65 };

    this._setupCanvas();
    this._bindEvents();
  }

  _setupCanvas() {
    const rect = this.canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    this.width = rect.width || 600;
    this.height = rect.height || 220;

    this.canvas.width = this.width * dpr;
    this.canvas.height = this.height * dpr;
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

  setData(points) {
    this.data = points || [];
    this.hoverIndex = -1;
    this.isHovering = false;
    this._setupCanvas();
    this.render();
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

    // Determine min/max PnL
    let minVal = Infinity;
    let maxVal = -Infinity;
    for (const pt of this.data) {
      if (pt.pnl < minVal) minVal = pt.pnl;
      if (pt.pnl > maxVal) maxVal = pt.pnl;
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
    const lastPnl = this.data[this.data.length - 1].pnl;
    const isProfit = lastPnl >= 0;
    const strokeColor = isProfit ? "#10b981" : "#ef4444";
    const gradientTop = isProfit ? "rgba(16, 185, 129, 0.25)" : "rgba(239, 68, 68, 0.25)";

    // Gradient fill
    const grad = ctx.createLinearGradient(0, p.top, 0, p.top + plotH);
    grad.addColorStop(0, gradientTop);
    grad.addColorStop(1, "rgba(0, 0, 0, 0.0)");

    ctx.beginPath();
    ctx.moveTo(getX(0), getY(this.data[0].pnl));
    for (let i = 1; i < this.data.length; i++) {
      ctx.lineTo(getX(i), getY(this.data[i].pnl));
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
    ctx.moveTo(getX(0), getY(this.data[0].pnl));
    for (let i = 1; i < this.data.length; i++) {
      ctx.lineTo(getX(i), getY(this.data[i].pnl));
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
      const hX = getX(this.hoverIndex);
      const hY = getY(hPt.pnl);

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
      const pnlStr = (hPt.pnl >= 0 ? "+₹" : "-₹") + Math.abs(hPt.pnl).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      const tipText = `${hPt.time || ''} | P&L: ${pnlStr}`;

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
      ctx.roundRect(boxX, boxY, boxW, boxH, 4);
      ctx.fill();
      ctx.stroke();

      ctx.fillStyle = hPt.pnl >= 0 ? "#10b981" : "#ef4444";
      ctx.textAlign = "center";
      ctx.fillText(tipText, boxX + boxW / 2, boxY + 16);
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
