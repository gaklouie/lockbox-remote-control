// Minimal dependency-free line plot on a <canvas>.
//
//   const plot = new LinePlot(canvasElement, {xLabel: "Time (s)", yLabel: "Voltage (V)"});
//   plot.draw({
//     series: [{label, color, x: [...], y: [...], markers: false, alpha: 1, axis: "left"}],
//     rightAxis: {label, color},     // optional second y axis on the right, for series with
//                                    // axis: "right" (each axis is scaled to its own series)
//     xRange: [min, max],            // optional; otherwise fitted to the data
//     hLines: [0],                   // optional dashed horizontal lines (left axis)
//     vLines: [{x, y, label, axis}], // optional dotted vertical lines with a labelled marker at y
//     vSpans: [{start, end}],        // optional shaded vertical bands
//     rangeBars: [{lo, hi, color, label, axis}],  // optional bars beside the right edge
//     rightMargin: 92,               // optional fixed space (px) right of the plot area, so
//                                    // plots stacked above each other line up
//   });

class LinePlot {
  constructor(canvas, options = {}) {
    this.canvas = canvas;
    this.options = options;
    this.last = null;
    new ResizeObserver(() => { if (this.last) this.draw(this.last); }).observe(canvas);
  }

  static niceTicks(min, max, target) {
    const span = max - min;
    const raw = span / Math.max(1, target);
    const power = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 5, 10].map((m) => m * power).find((s) => s >= raw);
    const ticks = [];
    for (let v = Math.ceil(min / step) * step; v <= max + step * 1e-9; v += step) ticks.push(v);
    const decimals = Math.max(0, -Math.floor(Math.log10(step) + 1e-9));
    return {ticks, decimals};
  }

  static extent(arrays, pad) {
    let min = Infinity, max = -Infinity;
    for (const values of arrays) {
      for (const v of values) {
        if (v < min) min = v;
        if (v > max) max = v;
      }
    }
    if (!isFinite(min)) return [0, 1];
    if (max - min < 1e-9) { min -= 0.5; max += 0.5; }
    const margin = (max - min) * pad;
    return [min - margin, max + margin];
  }

  draw(data) {
    this.last = data;
    const canvas = this.canvas;
    const scale = window.devicePixelRatio || 1;
    const width = canvas.clientWidth, height = canvas.clientHeight;
    if (width === 0 || height === 0) return;  // hidden tab
    if (canvas.width !== Math.round(width * scale) || canvas.height !== Math.round(height * scale)) {
      canvas.width = Math.round(width * scale);
      canvas.height = Math.round(height * scale);
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(scale, 0, 0, scale, 0, 0);
    ctx.clearRect(0, 0, width, height);
    ctx.font = "12px system-ui, sans-serif";

    const series = data.series || [];
    const rangeBars = data.rangeBars || [];
    const rightSeries = series.filter((s) => s.axis === "right");
    const right = data.rightAxis && rightSeries.length ? data.rightAxis : null;
    const rightAxisWidth = right ? 54 : 0;   // tick numbers + rotated title
    const rightMargin = data.rightMargin || 16 + rangeBars.length * 11 + rightAxisWidth;
    const box = {left: 58, right: width - rightMargin, top: 10, bottom: height - 38};
    const [x0, x1] = data.xRange || LinePlot.extent(series.map((s) => s.x), 0);
    const [y0, y1] = LinePlot.extent(series.filter((s) => s.axis !== "right").map((s) => s.y), 0.08);
    const [r0, r1] = right ? LinePlot.extent(rightSeries.map((s) => s.y), 0.08) : [0, 1];
    const px = (x) => box.left + ((x - x0) / (x1 - x0 || 1)) * (box.right - box.left);
    const py = (y) => box.bottom - ((y - y0) / (y1 - y0 || 1)) * (box.bottom - box.top);
    const pyRight = (y) => box.bottom - ((y - r0) / (r1 - r0 || 1)) * (box.bottom - box.top);
    const yScale = (axis) => (axis === "right" && right ? pyRight : py);

    // Grid, ticks and labels.
    ctx.strokeStyle = "#e3e3e3";
    ctx.fillStyle = "#444";
    ctx.lineWidth = 1;
    const xt = LinePlot.niceTicks(x0, x1, (box.right - box.left) / 90);
    const yt = LinePlot.niceTicks(y0, y1, (box.bottom - box.top) / 45);
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    for (const v of xt.ticks) {
      ctx.beginPath(); ctx.moveTo(px(v), box.top); ctx.lineTo(px(v), box.bottom); ctx.stroke();
      ctx.fillText(v.toFixed(xt.decimals), px(v), box.bottom + 4);
    }
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (const v of yt.ticks) {
      ctx.beginPath(); ctx.moveTo(box.left, py(v)); ctx.lineTo(box.right, py(v)); ctx.stroke();
      ctx.fillText(v.toFixed(yt.decimals), box.left - 6, py(v));
    }
    ctx.textAlign = "center";
    ctx.textBaseline = "bottom";
    if (this.options.xLabel) ctx.fillText(this.options.xLabel, (box.left + box.right) / 2, height - 2);
    if (this.options.yLabel) {
      ctx.save();
      ctx.translate(12, (box.top + box.bottom) / 2);
      ctx.rotate(-Math.PI / 2);
      ctx.textBaseline = "middle";
      ctx.fillText(this.options.yLabel, 0, 0);
      ctx.restore();
    }

    // Right axis: numbers and title in its own color, outside any range bars.
    if (right) {
      const rt = LinePlot.niceTicks(r0, r1, (box.bottom - box.top) / 45);
      const labelX = box.right + 4 + rangeBars.length * 11 + 2;
      ctx.fillStyle = right.color;
      ctx.strokeStyle = right.color;
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      for (const v of rt.ticks) {
        ctx.beginPath(); ctx.moveTo(box.right - 5, pyRight(v)); ctx.lineTo(box.right, pyRight(v)); ctx.stroke();
        ctx.fillText(v.toFixed(rt.decimals), labelX, pyRight(v));
      }
      ctx.save();
      ctx.translate(width - 8, (box.top + box.bottom) / 2);
      ctx.rotate(Math.PI / 2);
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(right.label, 0, 0);
      ctx.restore();
    }

    // Everything below is clipped to the plot area.
    ctx.save();
    ctx.beginPath();
    ctx.rect(box.left, box.top, box.right - box.left, box.bottom - box.top);
    ctx.clip();

    ctx.fillStyle = "rgba(120,120,120,0.18)";
    for (const span of data.vSpans || []) {
      ctx.fillRect(px(span.start), box.top, px(span.end) - px(span.start), box.bottom - box.top);
    }
    ctx.strokeStyle = "#000";
    ctx.setLineDash([5, 4]);
    for (const y of data.hLines || []) {
      ctx.beginPath(); ctx.moveTo(box.left, py(y)); ctx.lineTo(box.right, py(y)); ctx.stroke();
    }
    ctx.strokeStyle = "#777";
    ctx.setLineDash([2, 3]);
    for (const line of data.vLines || []) {
      ctx.beginPath(); ctx.moveTo(px(line.x), box.top); ctx.lineTo(px(line.x), box.bottom); ctx.stroke();
    }
    ctx.setLineDash([]);

    for (const s of series) {
      ctx.globalAlpha = s.alpha === undefined ? 1 : s.alpha;
      ctx.strokeStyle = s.color;
      ctx.fillStyle = s.color;
      ctx.lineWidth = s.width || 1.5;
      const sy = yScale(s.axis);
      ctx.beginPath();
      for (let i = 0; i < s.x.length; i++) {
        if (i === 0) ctx.moveTo(px(s.x[i]), sy(s.y[i]));
        else ctx.lineTo(px(s.x[i]), sy(s.y[i]));
      }
      ctx.stroke();
      if (s.markers) {
        for (let i = 0; i < s.x.length; i++) {
          ctx.beginPath(); ctx.arc(px(s.x[i]), sy(s.y[i]), 1.6, 0, 2 * Math.PI); ctx.fill();
        }
      }
    }
    ctx.globalAlpha = 1;

    // Candidate labels sit on top of the traces.
    ctx.fillStyle = "#000";
    ctx.textAlign = "center";
    ctx.textBaseline = "bottom";
    ctx.font = "bold 12px system-ui, sans-serif";
    for (const line of data.vLines || []) {
      if (line.label === undefined) continue;
      const y = line.y === undefined ? box.top + 16 : yScale(line.axis)(line.y);
      ctx.beginPath(); ctx.arc(px(line.x), y, 3.5, 0, 2 * Math.PI); ctx.fill();
      ctx.fillText(line.label, px(line.x), y - 6);
    }
    ctx.restore();
    ctx.font = "12px system-ui, sans-serif";

    ctx.strokeStyle = "#333";
    ctx.lineWidth = 1;
    ctx.strokeRect(box.left, box.top, box.right - box.left, box.bottom - box.top);

    // Range bars just outside the right edge, cut off at the plot's top and bottom.
    rangeBars.forEach((bar, i) => {
      const by = yScale(bar.axis);
      const top = Math.max(box.top, Math.min(box.bottom, by(bar.hi)));
      const bottom = Math.max(box.top, Math.min(box.bottom, by(bar.lo)));
      ctx.fillStyle = bar.color;
      ctx.globalAlpha = 0.6;
      const x = box.right + 4 + i * 11;
      if (bottom - top < 1) {
        // Range entirely off-screen: a small arrow pointing toward it.
        const up = by(bar.lo) < box.top;
        const tip = up ? box.top : box.bottom;
        ctx.beginPath();
        ctx.moveTo(x + 3.5, tip);
        ctx.lineTo(x, tip + (up ? 8 : -8));
        ctx.lineTo(x + 7, tip + (up ? 8 : -8));
        ctx.fill();
      } else {
        ctx.fillRect(x, top, 7, bottom - top);
      }
      ctx.globalAlpha = 1;
    });

    // Legend, top-left inside the plot.
    const entries = series.filter((s) => s.label).map((s) => ({label: s.label, color: s.color, bar: false}))
      .concat(rangeBars.map((b) => ({label: b.label, color: b.color, bar: true})));
    if (entries.length) {
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      const legendWidth = Math.max(...entries.map((e) => ctx.measureText(e.label).width)) + 38;
      const lx = box.left + 8, ly = box.top + 8;
      ctx.fillStyle = "rgba(255,255,255,0.85)";
      ctx.fillRect(lx, ly, legendWidth, entries.length * 16 + 6);
      ctx.strokeStyle = "#bbb";
      ctx.strokeRect(lx, ly, legendWidth, entries.length * 16 + 6);
      entries.forEach((entry, i) => {
        const y = ly + 11 + i * 16;
        ctx.fillStyle = entry.color;
        ctx.globalAlpha = entry.bar ? 0.6 : 1;
        ctx.fillRect(lx + 6, y - (entry.bar ? 4 : 1), 20, entry.bar ? 8 : 2);
        ctx.globalAlpha = 1;
        ctx.fillStyle = "#222";
        ctx.fillText(entry.label, lx + 32, y);
      });
    }
  }
}
