/* ============================================================
   charts.js — 纯手写响应式 SVG 图表（无外部依赖）
   Viz.degradation / Viz.profile / Viz.residuals
   ============================================================ */
"use strict";

const Viz = (() => {
  const NS = "http://www.w3.org/2000/svg";

  function palette() {
    const cs = getComputedStyle(document.documentElement);
    const g = (n) => cs.getPropertyValue(n).trim();
    return {
      blue: g("--cat-blue"), orange: g("--cat-orange"), aqua: g("--cat-aqua"),
      yellow: g("--cat-yellow"), magenta: g("--cat-magenta"), green: g("--cat-green"),
      violet: g("--cat-violet"), red: g("--cat-red"),
      good: g("--st-good"), warning: g("--st-warning"), critical: g("--st-critical"),
      surface: g("--surface"), page: g("--page"), ink: g("--ink"),
      ink2: g("--ink-2"), muted: g("--muted"), grid: g("--grid"), axis: g("--axis"),
      blueSoft: g("--blue-soft"), orangeSoft: g("--orange-soft"),
      redSoft: g("--red-soft"), greenSoft: g("--green-soft"),
    };
  }

  function el(name, attrs = {}, parent) {
    const node = document.createElementNS(NS, name);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null) continue;
      if (k === "text") node.textContent = v;
      else if (k === "className") node.setAttribute("class", v);
      else if (k === "style") node.setAttribute("style", v);
      else node.setAttribute(k, v);
    }
    if (parent) parent.appendChild(node);
    return node;
  }

  /* ---------- tooltip ---------- */
  const tt = () => document.getElementById("tooltip");
  function showTip(html, cx, cy) {
    const box = tt();
    box.innerHTML = html;
    box.hidden = false;
    const r = box.getBoundingClientRect();
    let x = cx + 14, y = cy + 14;
    if (x + r.width > window.innerWidth - 8) x = cx - r.width - 12;
    if (y + r.height > window.innerHeight - 8) y = cy - r.height - 12;
    box.style.left = Math.max(6, x) + "px";
    box.style.top = Math.max(6, y) + "px";
  }
  function hideTip() { const box = tt(); if (box) box.hidden = true; }

  function tipRows(rows) {
    return rows.map(([k, v]) =>
      `<div class="tt-row"><span>${k}</span><b>${v}</b></div>`).join("");
  }

  /* ---------- scales / ticks ---------- */
  function niceTicks(lo, hi, count = 6) {
    if (!(hi > lo)) { hi = lo + 1; }
    const span = hi - lo;
    const step0 = span / count;
    const mag = Math.pow(10, Math.floor(Math.log10(step0)));
    const norm = step0 / mag;
    let step;
    if (norm <= 1) step = 1 * mag;
    else if (norm <= 2) step = 2 * mag;
    else if (norm <= 5) step = 5 * mag;
    else step = 10 * mag;
    const start = Math.ceil(lo / step) * step;
    const out = [];
    for (let v = start; v <= hi + step * 1e-6; v += step) out.push(+v.toFixed(10));
    return out;
  }

  function fmt(v, d = 1) {
    if (v === null || v === undefined || !isFinite(v)) return "—";
    if (Math.abs(v) >= 1000) return v.toFixed(0);
    if (Math.abs(v) >= 100) return v.toFixed(1);
    if (Math.abs(v) >= 10) return v.toFixed(d);
    return v.toFixed(d + 1);
  }

  function baseSvg(container, h) {
    container.innerHTML = "";
    const w = Math.max(280, container.clientWidth || 600);
    const svg = el("svg", { width: w, height: h, role: "img" }, container);
    return { svg, w, h };
  }

  function drawAxes(svg, cfg) {
    const P = palette();
    const { w, h, m, x, y, xTicks, yTicks, xLabel, yLabel } = cfg;
    el("rect", { x: m.l, y: m.t, width: w - m.l - m.r, height: h - m.t - m.b,
      fill: P.surface }, svg);
    // grid + y labels
    const gGrid = el("g", { class: "svg-grid" }, svg);
    yTicks.forEach((tv) => {
      const yy = y(tv);
      el("line", { x1: m.l, x2: w - m.r, y1: yy, y2: yy }, gGrid);
      el("text", { x: m.l - 7, y: yy + 3.6, "text-anchor": "end",
        fill: P.muted, "font-size": 10.8, text: fmt(tv, tv % 1 === 0 ? 0 : 1) }, svg);
    });
    xTicks.forEach((tv) => {
      const xx = x(tv);
      el("line", { x1: xx, x2: xx, y1: m.t, y2: h - m.b,
        stroke: P.grid, "stroke-width": 1, "stroke-dasharray": "2 4" }, gGrid);
      el("text", { x: xx, y: h - m.b + 16, "text-anchor": "middle",
        class: "svg-axis", text: fmt(tv, 0) }, svg);
    });
    // axis lines
    el("line", { x1: m.l, x2: w - m.r, y1: h - m.b, y2: h - m.b,
      stroke: P.axis }, svg);
    el("line", { x1: m.l, x2: m.l, y1: m.t, y2: h - m.b, stroke: P.axis }, svg);
    if (xLabel) el("text", { x: m.l + (w - m.l - m.r) / 2, y: h - 6,
      "text-anchor": "middle", class: "svg-axis-label", text: xLabel }, svg);
    if (yLabel) {
      el("text", { transform: `translate(13,${m.t + (h - m.t - m.b) / 2}) rotate(-90)`,
        "text-anchor": "middle", class: "svg-axis-label", text: yLabel }, svg);
    }
  }

  /* ============================================================
     主退化图
     cfg: {rows, selectedCell, batchOf, batchColors, curves,
           eol, life, windows, candidates, truthCurve,
           tEvidence, xMaxExtra, onSelectCell, timeUnit, rangeHi}
     curves: [{key,color,dash,width,points:[{t,y}], opacity}]
     ============================================================ */
  function degradation(container, cfg) {
    const P = palette();
    const H = 380;
    const { svg, w, h } = baseSvg(container, H);
    const m = { l: 50, r: 20, t: 16, b: 40 };
    const iw = w - m.l - m.r, ih = h - m.t - m.b;

    const allRows = cfg.rows || [];
    let xmax = 10, ymin = Infinity, ymax = -Infinity;
    const considerY = (v) => { if (isFinite(v)) { ymin = Math.min(ymin, v); ymax = Math.max(ymax, v); } };
    allRows.forEach((r) => { xmax = Math.max(xmax, r.t); considerY(r.y); });
    const dataYMin = ymin, dataYMax = ymax;
    (cfg.windows || []).forEach((win) => { xmax = Math.max(xmax, win.hi); });
    (cfg.candidates || []).forEach((c) => { if (c.best_t != null) xmax = Math.max(xmax, c.best_t); });
    if (cfg.life) {
      if (isFinite(cfg.life.hat)) xmax = Math.max(xmax, cfg.life.hat + 6);
      if (isFinite(cfg.life.hi) && !cfg.life.highOpen) xmax = Math.max(xmax, cfg.life.hi + 4);
    }
    (cfg.curves || []).forEach((c) => (c.points || []).forEach((p) => considerY(p.y)));
    // 曲线在远端可能剧烈下冲，只让数据邻近区间参与 y 域，其余交给裁剪区
    const cyLo = Math.min(dataYMin, cfg.eol) - 18;
    const cyHi = Math.max(dataYMax, 100) + 10;
    (cfg.curveFns || []).forEach((c) => {
      for (let i = 0; i <= 80; i++) {
        const tv = (xmax * i) / 80;
        try {
          const yv = c.fn(tv);
          if (isFinite(yv) && yv >= cyLo && yv <= cyHi) considerY(yv);
        } catch { /* ignore */ }
      }
    });
    considerY(cfg.eol);
    if (cfg.rangeHi != null && isFinite(cfg.rangeHi)) considerY(cfg.rangeHi);
    xmax = xmax * 1.03;
    if (!isFinite(ymin)) { ymin = cfg.eol - 5; ymax = 100; }
    const ypad = Math.max(1.2, (ymax - ymin) * 0.08);
    ymin -= ypad; ymax += ypad;

    const x = (tv) => m.l + (tv / xmax) * iw;
    const y = (v) => m.t + ih - ((v - ymin) / (ymax - ymin)) * ih;
    const xTicks = niceTicks(0, xmax, Math.max(5, Math.round(iw / 95)));
    const yTicks = niceTicks(ymin, ymax, 6);

    drawAxes(svg, { w, h, m, x, y, xTicks, yTicks,
      xLabel: `检测时间 t（${cfg.timeUnit || "时间单位"}）`, yLabel: "容量 / 健康读数 y" });

    const clipId = "deg-clip-" + Math.random().toString(36).slice(2, 8);
    const defs = el("defs", {}, svg);
    const cp = el("clipPath", { id: clipId }, defs);
    el("rect", { x: m.l, y: m.t, width: iw, height: ih }, cp);

    /* 窗口浅色带 */
    (cfg.windows || []).forEach((win) => {
      const cx0 = x(win.lo), cx1 = x(win.hi);
      el("rect", { x: cx0, y: m.t, width: Math.max(1, cx1 - cx0), height: ih,
        fill: P.violet, "fill-opacity": 0.055 }, svg);
      el("text", { x: (cx0 + cx1) / 2, y: m.t + 12, "text-anchor": "middle",
        "font-size": 10, fill: P.violet, "font-weight": 700, text: win.id }, svg);
    });

    /* 外推区域 */
    const tEv = cfg.tEvidence;
    if (isFinite(tEv) && tEv < xmax) {
      el("rect", { x: x(tEv), y: m.t, width: x(xmax) - x(tEv), height: ih,
        fill: P.ink, "fill-opacity": 0.035 }, svg);
      el("line", { x1: x(tEv), x2: x(tEv), y1: m.t, y2: h - m.b,
        stroke: P.muted, "stroke-width": 1, "stroke-dasharray": "4 3" }, svg);
      el("text", { x: x(tEv) + 5, y: h - m.b - 6, "font-size": 10.3,
        fill: P.muted, text: "↑ 右侧为无观测外推区间" }, svg);
    }

    /* EOL 线 */
    if (cfg.eol >= ymin && cfg.eol <= ymax) {
      el("line", { x1: m.l, x2: w - m.r, y1: y(cfg.eol), y2: y(cfg.eol),
        stroke: P.critical, "stroke-width": 1.6, "stroke-dasharray": "7 4" }, svg);
      el("text", { x: w - m.r - 4, y: y(cfg.eol) - 5, "text-anchor": "end",
        "font-size": 11, "font-weight": 700, fill: P.critical, class: "svg-halo",
        text: `EOL = ${cfg.eol}` }, svg);
    }

    const gData = el("g", { "clip-path": `url(#${clipId})` }, svg);

    /* 函数曲线：在最终 x 域上采样 */
    (cfg.curveFns || []).forEach((c) => {
      const N = 170;
      let d = "";
      for (let i = 0; i <= N; i++) {
        const tv = (xmax * i) / N;
        let yv;
        try { yv = c.fn(tv); } catch { yv = NaN; }
        if (!isFinite(yv)) continue;
        d += `${d ? "L" : "M"}${x(tv).toFixed(1)},${y(yv).toFixed(1)}`;
      }
      if (d) el("path", { d, fill: "none", stroke: c.color || P.blue,
        "stroke-width": c.width || 2.4, "stroke-dasharray": c.dash || null,
        "stroke-linecap": "round", "stroke-linejoin": "round",
        opacity: c.opacity == null ? 1 : c.opacity }, gData);
    });

    /* 曲线（静态点） */
    (cfg.curves || []).forEach((c) => {
      if (!c.points || c.points.length < 2) return;
      const d = c.points.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.y).toFixed(1)}`).join("");
      el("path", { d, fill: "none", stroke: c.color || P.blue,
        "stroke-width": c.width || 2.2, "stroke-dasharray": c.dash || null,
        "stroke-linecap": "round", "stroke-linejoin": "round",
        opacity: c.opacity == null ? 1 : c.opacity }, gData);
    });

    /* 真值虚线 */
    if (cfg.truthCurve && cfg.truthCurve.length > 1) {
      const d = cfg.truthCurve.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.y).toFixed(1)}`).join("");
      el("path", { d, fill: "none", stroke: P.muted, "stroke-width": 1.6,
        "stroke-dasharray": "2 4", "stroke-linecap": "round" }, gData);
    }

    /* 寿命 CI 与交点 */
    if (cfg.life && isFinite(cfg.life.hat)) {
      const L = cfg.life;
      if (isFinite(L.lo) && L.lo >= 0 && !L.lowOpen) {
        el("line", { x1: x(L.lo), x2: x(L.hi || L.hat), y1: y(cfg.eol), y2: y(cfg.eol),
          stroke: P.blue, "stroke-width": 3.5, "stroke-linecap": "round", opacity: .35 }, gData);
        el("line", { x1: x(L.lo), x2: x(L.lo), y1: y(cfg.eol) - 5, y2: y(cfg.eol) + 5,
          stroke: P.blue, "stroke-width": 1.6 }, gData);
      }
      if (isFinite(L.hi) && !L.highOpen) {
        el("line", { x1: x(L.hi), x2: x(L.hi), y1: y(cfg.eol) - 5, y2: y(cfg.eol) + 5,
          stroke: P.blue, "stroke-width": 1.6 }, gData);
      }
      if (L.highOpen) {
        el("path", { d: `M${x(Math.min(L.hat + 4, xmax * .98)).toFixed(1)},${y(cfg.eol) - 5} l7,5 l-7,5`,
          fill: "none", stroke: P.blue, "stroke-width": 1.8 }, gData);
      }
      el("circle", { cx: x(L.hat), cy: y(cfg.eol), r: 5, fill: P.blue,
        stroke: P.surface, "stroke-width": 2 }, gData);
      el("text", { x: x(L.hat), y: y(cfg.eol) + 20, "text-anchor": "middle",
        "font-size": 11, "font-weight": 700, fill: P.blue, class: "svg-halo",
        text: `L≈${fmt(L.hat, 1)}${L.highOpen ? "（上界开放）" : ""}` }, svg);
    }

    /* 候选最优点 */
    (cfg.candidates || []).forEach((c) => {
      if (c.best_t == null || !c.best) return;
      const mu = c.best.mu;
      if (mu == null || mu < ymin || mu > ymax) return;
      el("circle", { cx: x(c.best_t), cy: y(mu), r: 6.5,
        fill: P.surface, stroke: P.violet, "stroke-width": 2,
        "stroke-dasharray": c.best.already_measured ? "2 2" : null }, gData);
      el("line", { x1: x(c.best_t), x2: x(c.best_t), y1: y(mu) - 11, y2: y(mu) - 6,
        stroke: P.violet, "stroke-width": 1.4 }, gData);
    });

    /* 数据点 */
    const instIndex = {};
    (cfg.instruments || []).forEach((v, i) => { instIndex[v] = i; });
    function marker(r) {
      const selected = r.cell === cfg.selectedCell;
      const color = cfg.batchColors[cfg.batchOf[r.cell]] || P.blue;
      const xx = x(r.t), yy = y(r.y);
      const isSyn = r.provenance === "synthetic";
      const fill = isSyn ? P.magenta : color;
      const stroke = selected ? P.surface : P.surface;
      const sw = selected ? 2 : 1.2;
      const rad = selected ? 5 : 3.1;
      const common = { "data-cell": r.cell, style: "cursor:pointer" };

      let node;
      if (r.cens === 1 || r.cens === -1) {
        // 删失：三角 + 朝外箭头
        const up = r.cens === 1;
        const pts = up
          ? `${xx},${yy - rad - 1} ${xx - rad - 1},${yy + rad} ${xx + rad + 1},${yy + rad}`
          : `${xx},${yy + rad + 1} ${xx - rad - 1},${yy - rad} ${xx + rad + 1},${yy - rad}`;
        node = el("polygon", { points: pts, fill, stroke, "stroke-width": sw, ...common }, gData);
        const ay = up ? yy - rad - 7 : yy + rad + 7;
        const dir = up ? -1 : 1;
        el("line", { x1: xx, x2: xx, y1: up ? yy - rad - 2 : yy + rad + 2,
          y2: ay + dir * 3, stroke: color, "stroke-width": 1.3 }, gData);
        el("path", { d: `M${xx - 3},${ay + dir * 3} L${xx},${ay} L${xx + 3},${ay + dir * 3}`,
          fill: "none", stroke: color, "stroke-width": 1.3 }, gData);
      } else if (isSyn) {
        const s = rad + 1.5;
        node = el("rect", { x: xx - s / 1.4, y: yy - s / 1.4, width: s * 1.42,
          height: s * 1.42, transform: `rotate(45 ${xx} ${yy})`,
          fill, stroke: P.surface, "stroke-width": 1.8, ...common }, gData);
      } else if (selected && cfg.offsetEnabled && (cfg.instruments || []).length > 1
                 && instIndex[r.instrument] === 1) {
        node = el("rect", { x: xx - rad, y: yy - rad, width: rad * 2, height: rad * 2,
          rx: 1.5, fill, stroke, "stroke-width": sw, ...common }, gData);
      } else {
        node = el("circle", { cx: xx, cy: yy, r: rad, fill,
          "fill-opacity": selected ? 1 : 0.62, stroke, "stroke-width": sw, ...common }, gData);
      }
      /* 命中区域 */
      const hit = el("circle", { cx: xx, cy: yy, r: 13, fill: "transparent",
        style: "cursor:pointer" }, gData);
      const handlers = {
        mouseenter: (ev) => {
          const rows2 = [
            ["单体", r.cell + (r.batch ? `（${r.batch}）` : "")],
            ["时间 t", `${fmt(r.t, 1)} ${cfg.timeUnit || ""}`],
          ];
          if (r.cens === 1) rows2.push(["读数", `≥ ${fmt(r.limit, 2)}（右删失/超上限）`]);
          else if (r.cens === -1) rows2.push(["读数", `≤ ${fmt(r.limit, 2)}（左删失/超下限）`]);
          else rows2.push(["读数 y", fmt(r.y, 3)]);
          rows2.push(["检测仪", r.instrument || "—"]);
          if (r.repeat && r.repeat > 1) rows2.push(["重复编号", r.repeat]);
          if (r.provenance === "synthetic") rows2.push(["来源", "接受建议后的合成测量"]);
          else if (r.provenance === "imported") rows2.push(["来源", "CSV 导入"]);
          showTip(`<div class="tt-title">${r.cell} · t=${fmt(r.t, 1)}</div>` + tipRows(rows2),
            ev.clientX, ev.clientY);
        },
        mousemove: (ev) => showTip(tt().innerHTML, ev.clientX, ev.clientY),
        mouseleave: hideTip,
        click: () => cfg.onSelectCell && cfg.onSelectCell(r.cell),
      };
      hit.addEventListener("mouseenter", handlers.mouseenter);
      hit.addEventListener("mousemove", handlers.mousemove);
      hit.addEventListener("mouseleave", handlers.mouseleave);
      hit.addEventListener("click", handlers.click);
      if (node) {
        node.addEventListener("mouseenter", handlers.mouseenter);
        node.addEventListener("mousemove", handlers.mousemove);
        node.addEventListener("mouseleave", handlers.mouseleave);
        node.addEventListener("click", handlers.click);
      }
    }
    allRows.forEach(marker);

    /* 悬停十字线（吸附最近点） */
    const cross = el("line", { y1: m.t, y2: h - m.b, stroke: P.axis,
      "stroke-width": 1, "stroke-dasharray": "3 3", visibility: "hidden",
      "pointer-events": "none" }, svg);
    const overlay = el("rect", { x: m.l, y: m.t, width: iw, height: ih,
      fill: "transparent" }, svg);
    overlay.addEventListener("mousemove", (ev) => {
      const rect = svg.getBoundingClientRect();
      const scaleX = w / rect.width, scaleY = h / rect.height;
      const mx = (ev.clientX - rect.left) * scaleX;
      const my = (ev.clientY - rect.top) * scaleY;
      let best = null, bd = 1e9;
      allRows.forEach((r) => {
        const px = x(r.t), py = y(r.y);
        const d = Math.hypot(px - mx, py - my);
        if (d < bd) { bd = d; best = r; }
      });
      if (best && bd < 22) {
        cross.setAttribute("x1", x(best.t));
        cross.setAttribute("x2", x(best.t));
        cross.setAttribute("visibility", "visible");
        const rows2 = [
          ["单体", best.cell + (best.batch ? `（${best.batch}）` : "")],
          ["时间 t", `${fmt(best.t, 1)} ${cfg.timeUnit || ""}`],
        ];
        if (best.cens === 1) rows2.push(["读数", `≥ ${fmt(best.limit, 2)}（右删失/超上限）`]);
        else if (best.cens === -1) rows2.push(["读数", `≤ ${fmt(best.limit, 2)}（左删失/超下限）`]);
        else rows2.push(["读数 y", fmt(best.y, 3)]);
        rows2.push(["检测仪", best.instrument || "—"]);
        if (best.provenance === "synthetic") rows2.push(["来源", "接受建议后的合成测量"]);
        showTip(`<div class="tt-title">${best.cell} · t=${fmt(best.t, 1)}</div>` + tipRows(rows2),
          ev.clientX, ev.clientY);
      } else {
        cross.setAttribute("visibility", "hidden");
        hideTip();
      }
    });
    overlay.addEventListener("click", (ev) => {
      const rect = svg.getBoundingClientRect();
      const mx = (ev.clientX - rect.left) * (w / rect.width);
      const my = (ev.clientY - rect.top) * (h / rect.height);
      let best = null, bd = 1e9;
      allRows.forEach((r) => {
        const d = Math.hypot(x(r.t) - mx, y(r.y) - my);
        if (d < bd) { bd = d; best = r; }
      });
      if (best && bd < 24 && cfg.onSelectCell) cfg.onSelectCell(best.cell);
    });
    overlay.addEventListener("mouseleave", () => {
      cross.setAttribute("visibility", "hidden");
      hideTip();
    });
  }

  /* ============================================================
     轮廓似然图
     cfg: {profile, compare?:profile, height?}
     ============================================================ */
  function profile(container, cfg) {
    const P = palette();
    const H = cfg.height || 250;
    const { svg, w, h } = baseSvg(container, H);
    const m = { l: 52, r: 16, t: 16, b: 38 };
    const iw = w - m.l - m.r, ih = h - m.t - m.b;
    const p = cfg.profile;
    if (!p || p.available === false || !p.grid || p.grid.length < 2) {
      el("text", { x: w / 2, y: h / 2, "text-anchor": "middle",
        fill: P.muted, "font-size": 13,
        text: (p && p.reason) || "当前拟合不可用，无法计算轮廓似然。" }, svg);
      return;
    }
    const cutoff = p.cutoff_dnll || 1.9207;
    const yCap = cutoff * 5.2;
    const pts = p.grid.filter((q) => isFinite(q.x) && isFinite(q.dnll));
    let xmin = Math.min(...pts.map((q) => q.x));
    let xmax = Math.max(...pts.map((q) => q.x));
    if (xmax - xmin < 1e-9) { xmin -= 1; xmax += 1; }
    const padx = (xmax - xmin) * 0.04;
    xmin -= padx; xmax += padx;
    const x = (v) => m.l + ((v - xmin) / (xmax - xmin)) * iw;
    const y = (v) => m.t + ih - (Math.min(v, yCap) / yCap) * ih;
    const xTicks = niceTicks(xmin, xmax, Math.max(4, Math.round(iw / 110)));
    const yTicks = [0, cutoff, cutoff * 2, cutoff * 3, cutoff * 4, yCap]
      .filter((v, i, a) => a.indexOf(v) === i);

    drawAxes(svg, { w, h, m, x, y, xTicks,
      yTicks: niceTicks(0, yCap, 5),
      xLabel: p.label || cfg.xLabel || "参数值",
      yLabel: "Δℓ（负对数似然差）" });

    /* CI 带 */
    if (isFinite(p.ci_low) && isFinite(p.ci_high) && !p.low_open && !p.high_open) {
      el("rect", { x: x(p.ci_low), y: m.t, width: x(p.ci_high) - x(p.ci_low),
        height: ih, fill: P.blue, "fill-opacity": 0.09 }, svg);
    }

    /* cutoff */
    el("line", { x1: m.l, x2: w - m.r, y1: y(cutoff), y2: y(cutoff),
      stroke: P.orange, "stroke-width": 1.6, "stroke-dasharray": "6 4" }, svg);
    el("text", { x: w - m.r - 3, y: y(cutoff) - 5, "text-anchor": "end",
      "font-size": 10.8, fill: P.orange, "font-weight": 700,
      text: `95% 阈值 Δℓ=1.92（2Δℓ=3.84）` }, svg);

    function curve(prof, color, width, dash) {
      const qs = prof.grid.filter((q) => isFinite(q.x) && isFinite(q.dnll) && q.converged !== false);
      if (qs.length < 2) return;
      const d = qs.map((q, i) =>
        `${i ? "L" : "M"}${x(q.x).toFixed(1)},${y(Math.max(0, q.dnll)).toFixed(1)}`).join("");
      el("path", { d, fill: "none", stroke: color, "stroke-width": width || 2.2,
        "stroke-dasharray": dash || null, "stroke-linejoin": "round",
        "stroke-linecap": "round" }, svg);
    }
    if (cfg.compare && cfg.compare.grid) curve(cfg.compare, P.aqua, 2, "6 4");
    curve(p, P.blue, 2.4);

    /* MLE 点 */
    if (isFinite(p.x_hat)) {
      el("circle", { cx: x(p.x_hat), cy: y(0), r: 4.6, fill: P.blue,
        stroke: P.surface, "stroke-width": 2 }, svg);
    }
    /* 开放边界箭头 */
    [[p.low_open, xmin, -1], [p.high_open, xmax, 1]].forEach(([open, xv, dir]) => {
      if (!open) return;
      const ax = x(xv) + (dir > 0 ? -2 : 2);
      el("path", { d: `M${ax},${m.t + 16} l${dir * 8},5 l${-dir * 8},5 z`,
        fill: P.critical }, svg);
      el("text", { x: ax + dir * 13, y: m.t + 24, "font-size": 10.5,
        fill: P.critical, "font-weight": 700, text: dir > 0 ? "上界开放" : "下界开放" }, svg);
    });

    /* hover 捕捉 */
    const hoverLine = el("line", { y1: m.t, y2: h - m.b, stroke: P.axis,
      "stroke-dasharray": "3 3", visibility: "hidden" }, svg);
    const hoverDot = el("circle", { r: 4.5, fill: P.blue, stroke: P.surface,
      "stroke-width": 1.6, visibility: "hidden" }, svg);
    const overlay = el("rect", { x: m.l, y: m.t, width: iw, height: ih,
      fill: "transparent" }, svg);
    overlay.addEventListener("mousemove", (ev) => {
      const rect = svg.getBoundingClientRect();
      const mx = (ev.clientX - rect.left) * (w / rect.width);
      const xv = xmin + ((mx - m.l) / iw) * (xmax - xmin);
      let best = null, bd = 1e9;
      pts.forEach((q) => { const d = Math.abs(q.x - xv); if (d < bd) { bd = d; best = q; } });
      if (!best) return;
      const px = x(best.x), py = y(Math.max(0, best.dnll));
      hoverLine.setAttribute("x1", px); hoverLine.setAttribute("x2", px);
      hoverLine.setAttribute("visibility", "visible");
      hoverDot.setAttribute("cx", px); hoverDot.setAttribute("cy", py);
      hoverDot.setAttribute("visibility", "visible");
      showTip(`<div class="tt-title">${p.label || "参数"} = ${fmt(best.x, 2)}</div>` +
        tipRows([["Δℓ", fmt(best.dnll, 3)], ["2Δℓ", fmt(2 * best.dnll, 3)],
                 ["是否在 95% 区间内", best.dnll <= cutoff ? "是" : "否"]]),
        ev.clientX, ev.clientY);
    });
    overlay.addEventListener("mouseleave", () => {
      hoverLine.setAttribute("visibility", "hidden");
      hoverDot.setAttribute("visibility", "hidden");
      hideTip();
    });
  }

  /* ============================================================
     残差图 cfg:{residuals, instruments}
     ============================================================ */
  function residuals(container, cfg) {
    const P = palette();
    const H = 260;
    const { svg, w, h } = baseSvg(container, H);
    const m = { l: 52, r: 18, t: 14, b: 38 };
    const iw = w - m.l - m.r, ih = h - m.t - m.b;
    const rs = cfg.residuals || [];
    if (!rs.length) {
      el("text", { x: w / 2, y: h / 2, "text-anchor": "middle", fill: P.muted,
        text: "无残差数据（拟合不可用）" }, svg);
      return;
    }
    let xmax = Math.max(...rs.map((r) => r.t)) * 1.04;
    let rmax = Math.max(...rs.map((r) => Math.abs(r.residual))) * 1.15;
    rmax = Math.max(rmax, 0.5);
    const x = (tv) => m.l + (tv / xmax) * iw;
    const y = (v) => m.t + ih / 2 - (v / rmax) * (ih / 2);
    drawAxes(svg, { w, h, m, x, y, xTicks: niceTicks(0, xmax, 6),
      yTicks: niceTicks(-rmax, rmax, 5), xLabel: "时间 t", yLabel: "残差 y−ŷ" });
    el("line", { x1: m.l, x2: w - m.r, y1: y(0), y2: y(0),
      stroke: P.ink2, "stroke-width": 1.4 }, svg);

    const insts = cfg.instruments || [];
    const colFor = (inst) => {
      if (insts.length <= 1) return P.blue;
      const i = insts.indexOf(inst);
      return [P.violet, P.aqua, P.orange, P.yellow][i % 4];
    };
    rs.forEach((r) => {
      const c = colFor(r.instrument);
      const isSyn = false;
      const dot = el("circle", { cx: x(r.t), cy: y(r.residual), r: 4,
        fill: c, "fill-opacity": .75, stroke: P.surface, "stroke-width": 1.4,
        style: "cursor:default" }, svg);
      dot.addEventListener("mousemove", (ev) => showTip(
        `<div class="tt-title">${r.cell} · t=${fmt(r.t, 1)}</div>` +
        tipRows([["残差", fmt(r.residual, 3)], ["观测", fmt(r.observed, 3)],
                 ["拟合", fmt(r.fitted, 3)], ["检测仪", r.instrument]]),
        ev.clientX, ev.clientY));
      dot.addEventListener("mouseleave", hideTip);
    });
  }

  /* 迷你前后叠加轮廓（台账用） */
  function miniProfile(container, before, after) {
    profile(container, { profile: before, compare: after, height: 120 });
  }

  return { degradation, profile, residuals, miniProfile, palette, showTip, hideTip, fmt };
})();
