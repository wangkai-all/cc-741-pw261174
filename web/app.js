/* ============================================================
   app.js — 电池退化数据工作台主逻辑
   状态 → 渲染；所有计算走同源 Flask API
   ============================================================ */
"use strict";

const API = {
  async get(url) {
    const r = await fetch(url);
    return r.json();
  },
  async post(url, body) {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    const data = await r.json().catch(() => ({}));
    return { status: r.status, data };
  },
};

const SCOPE_PARAMS = ["a", "b1", "b2", "tau"];
const SCOPE_OPTIONS = [
  ["cell", "每单体"],
  ["batch", "批次内共享"],
  ["global", "全部批次共享"],
];
const STATUS_META = {
  pending: { label: "待接受", cls: "info" },
  accepted: { label: "已接受 · 已追加测量", cls: "good" },
  superseded: { label: "已失效（假设已更改）", cls: "warn" },
};
const LEVEL_BADGE = {
  observed: ["good", "EOL 已在数据中穿过"],
  mild: ["info", "轻度外推"],
  strong: ["warn", "较强外推"],
  speculative: ["critical", "远距离外推 · 不可作确定结论"],
  fit_unavailable: ["critical", "拟合不可用"],
};

const App = {
  state: null,
  scenarios: [],
  profileKind: "life",
  profileData: null,
  profileLoading: false,
  activeTab: "params",
  dataFilter: "",
  showTruth: false,
  batchColors: {},
  pendingRecId: null,
  busy: 0,

  /* ---------------- 启动 ---------------- */
  async init() {
    this.bindStatic();
    this.applyStoredTheme();
    try {
      this.scenarios = (await API.get("/api/scenarios")).scenarios || [];
    } catch (e) {
      this.toast("无法连接本地服务，请确认 run.sh / python -m backend.server 正在运行。", "error");
      return;
    }
    this.renderScenarioButtons();
    await this.reloadState();
    window.addEventListener("resize", this.debounce(() => this.renderChartsOnly(), 160));
  },

  debounce(fn, ms) {
    let h;
    return (...a) => { clearTimeout(h); h = setTimeout(() => fn(...a), ms); };
  },

  async reloadState() {
    this.state = await API.get("/api/session");
    this.hydrateControls();
    this.renderAll();
    this.refreshProfile();
  },

  /* ---------------- 事件绑定 ---------------- */
  bindStatic() {
    document.getElementById("btn-theme").addEventListener("click", () => this.toggleTheme());
    document.getElementById("btn-model-doc").addEventListener("click", () => this.openModal("model-modal"));
    document.getElementById("btn-import").addEventListener("click", () => this.openModal("import-modal"));
    document.getElementById("btn-template").addEventListener("click", () => {
      window.location.href = "/api/template.csv";
    });
    document.getElementById("btn-reset").addEventListener("click", () => this.resetDemo());

    document.querySelectorAll("[data-close]").forEach((b) =>
      b.addEventListener("click", () => this.closeModal(b.dataset.close)));
    document.querySelectorAll(".modal-backdrop").forEach((m) =>
      m.addEventListener("click", (e) => { if (e.target === m) m.hidden = true; }));

    // 共享作用域
    document.querySelectorAll("#scope-grid select").forEach((sel) => {
      SCOPE_OPTIONS.forEach(([v, lab]) => {
        const o = document.createElement("option");
        o.value = v; o.textContent = lab; sel.appendChild(o);
      });
      sel.addEventListener("change", () => this.onScopeChange(sel.dataset.param, sel.value));
    });

    document.getElementById("opt-offset").addEventListener("change", (e) =>
      this.updateSettings({ model_options: {
        instrument_offset: e.target.checked,
        reference_instrument: this.state?.model_options?.reference_instrument } }));
    document.getElementById("opt-ref-inst").addEventListener("change", (e) =>
      this.updateSettings({ model_options: {
        instrument_offset: this.state.model_options.instrument_offset,
        reference_instrument: e.target.value } }));
    document.getElementById("opt-eol").addEventListener("change", (e) => {
      const v = parseFloat(e.target.value);
      if (isFinite(v) && v > 0 && v < 1000) this.updateSettings({ eol: v });
    });
    document.getElementById("opt-truth").addEventListener("change", (e) => {
      this.showTruth = e.target.checked;
      this.renderDegradation();
    });

    // profile tabs
    document.querySelectorAll("#profile-tabs button").forEach((b) =>
      b.addEventListener("click", () => {
        this.profileKind = b.dataset.pkind;
        document.querySelectorAll("#profile-tabs button").forEach((x) =>
          x.classList.toggle("active", x === b));
        this.refreshProfile();
      }));

    // bottom tabs
    document.querySelectorAll("#bottom-tabs button").forEach((b) =>
      b.addEventListener("click", () => {
        this.activeTab = b.dataset.tab;
        document.querySelectorAll("#bottom-tabs button").forEach((x) =>
          x.classList.toggle("active", x === b));
        ["params", "residuals", "data"].forEach((t) => {
          document.getElementById("tab-" + t).hidden = t !== this.activeTab;
        });
        if (this.activeTab === "residuals") this.renderResidual();
      }));
    document.getElementById("data-filter").addEventListener("input", (e) => {
      this.dataFilter = e.target.value.trim().toLowerCase();
      this.renderDataTable();
    });

    // recommend
    document.getElementById("btn-recommend").addEventListener("click", () => this.createRecommendation());
    document.getElementById("rec-cell").addEventListener("change", (e) =>
      this.updateSettings({ selected_cell: e.target.value }));

    // import modal
    document.getElementById("btn-pick-file").addEventListener("click", () =>
      document.getElementById("import-file").click());
    document.getElementById("import-file").addEventListener("change", (e) => this.onPickFile(e));
    const drop = document.getElementById("import-drop");
    ["dragover", "dragenter"].forEach((ev) => drop.addEventListener(ev, (e) => {
      e.preventDefault(); drop.classList.add("drag");
    }));
    ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => {
      e.preventDefault(); drop.classList.remove("drag");
    }));
    drop.addEventListener("drop", (e) => {
      const f = e.dataTransfer.files[0];
      if (f) this.readFile(f);
    });
    document.getElementById("btn-do-import").addEventListener("click", () => this.doImport());

    this.renderModelDoc();
  },

  /* ---------------- 主题 ---------------- */
  applyStoredTheme() {
    const saved = localStorage.getItem("batt-theme");
    const dark = saved ? saved === "dark"
      : window.matchMedia("(prefers-color-scheme: dark)").matches;
    document.documentElement.dataset.theme = dark ? "dark" : "light";
  },
  toggleTheme() {
    const cur = document.documentElement.dataset.theme;
    const next = cur === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("batt-theme", next);
    this.renderChartsOnly();
  },

  /* ---------------- 通用 UI ---------------- */
  toast(msg, type = "") {
    const stack = document.getElementById("toast-stack");
    const d = document.createElement("div");
    d.className = "toast " + type;
    d.textContent = msg;
    stack.appendChild(d);
    setTimeout(() => { d.style.opacity = "0"; d.style.transition = "opacity .4s"; }, 4200);
    setTimeout(() => d.remove(), 4700);
  },
  openModal(id) { document.getElementById(id).hidden = false; },
  closeModal(id) { document.getElementById(id).hidden = true; },

  setBusy(on) {
    this.busy += on ? 1 : -1;
    document.body.style.cursor = this.busy > 0 ? "progress" : "";
  },

  async callMut(fn, okMsg) {
    this.setBusy(true);
    try {
      const out = await fn();
      if (out.status >= 400) {
        const msg = out.data?.message || out.data?.error || "请求失败";
        this.toast(msg, "error");
        return out;
      }
      if (okMsg) this.toast(okMsg, "success");
      return out;
    } catch (e) {
      this.toast("网络/服务错误：" + e.message, "error");
      return null;
    } finally {
      this.setBusy(false);
    }
  },

  /* ---------------- 场景 ---------------- */
  renderScenarioButtons() {
    const box = document.getElementById("scenario-buttons");
    box.innerHTML = "";
    this.scenarios.forEach((s) => {
      const b = document.createElement("button");
      b.className = "scn-btn";
      b.dataset.sid = s.id;
      b.innerHTML = `${s.name}<span class="scn-tag">${s.n_rows} 行</span>`;
      b.addEventListener("click", () => this.loadScenario(s.id));
      box.appendChild(b);
    });
  },

  async loadScenario(sid) {
    const out = await this.callMut(
      () => API.post(`/api/session/scenario/${sid}`), null);
    if (out?.data) {
      this.state = out.data;
      this.pendingRecId = null;
      this.hydrateControls();
      this.renderAll();
      this.refreshProfile();
      this.toast(`已载入场景：${this.state.name}`, "success");
    }
  },

  async resetDemo() {
    if (!confirm("重置将清空已接受的合成测量与建议台账，并重新载入当前内置场景。确定？")) return;
    const out = await this.callMut(() => API.post("/api/session/reset"), "演示已重置");
    if (out?.data) {
      this.state = out.data;
      this.pendingRecId = null;
      this.hydrateControls();
      this.renderAll();
      this.refreshProfile();
    }
  },

  /* ---------------- 设置 ---------------- */
  hydrateControls() {
    const s = this.state;
    if (!s) return;
    document.querySelectorAll("#scope-grid select").forEach((sel) => {
      sel.value = s.scopes[sel.dataset.param] || "batch";
    });
    const off = document.getElementById("opt-offset");
    off.checked = !!s.model_options.instrument_offset;
    const refSel = document.getElementById("opt-ref-inst");
    const insts = s.fit?.layout_meta?.instruments || [];
    refSel.innerHTML = "";
    insts.forEach((i) => {
      const o = document.createElement("option");
      o.value = i; o.textContent = i + (i === s.model_options.reference_instrument ? "（δ=0）" : "");
      refSel.appendChild(o);
    });
    if (s.model_options.reference_instrument) refSel.value = s.model_options.reference_instrument;
    refSel.disabled = !off.checked;
    document.getElementById("opt-eol").value = s.meta?.eol ?? 80;
  },

  async onScopeChange(param, value) {
    const scopes = { ...this.state.scopes, [param]: value };
    const out = await this.callMut(() => API.post("/api/session/settings", { scopes }));
    if (out?.data) {
      this.state = out.data;
      this.renderAll();
      this.refreshProfile();
      this.toast("共享假设已更新并重新拟合；旧的待接受建议已标记失效。");
    } else {
      // 回滚下拉
      document.querySelector(`select[data-param="${param}"]`).value = this.state.scopes[param];
    }
  },

  async updateSettings(patch) {
    const out = await this.callMut(() => API.post("/api/session/settings", patch));
    if (out?.data) {
      this.state = out.data;
      this.hydrateControls();
      this.renderAll();
      this.refreshProfile();
    }
  },

  async selectCell(cell) {
    if (cell === this.state.selected_cell) return;
    // 本地立即响应选中，再同步后端（后端会重算状态但不重拟合）
    this.state.selected_cell = cell;
    this.renderCellTree();
    this.renderDegradation();
    this.renderRecForm();
    if (this.profileKind === "life") this.refreshProfile();
    await API.post("/api/session/settings", { selected_cell: cell });
  },

  /* ---------------- 总渲染 ---------------- */
  renderAll() {
    if (!this.state) return;
    this.buildBatchColors();
    this.renderScenarioBarState();
    this.renderScenarioDesc();
    this.renderBanners();
    this.renderCellTree();
    this.renderBudget();
    this.renderDegradation();
    this.renderProfileSummary();
    this.renderProfileChart();
    this.renderParamsTab();
    this.renderResidual();
    this.renderDataTable();
    this.renderRecForm();
    this.renderRecResult();
    this.renderLedger();
  },

  renderChartsOnly() {
    this.renderDegradation();
    this.renderProfileChart();
    if (this.activeTab === "residuals") this.renderResidual();
    this.renderLedgerMiniProfiles();
  },

  buildBatchColors() {
    const batches = this.state.fit?.layout_meta?.batches || [];
    const order = ["--cat-blue", "--cat-orange", "--cat-aqua", "--cat-yellow",
      "--cat-violet", "--cat-magenta", "--cat-green", "--cat-red"];
    const cs = getComputedStyle(document.documentElement);
    this.batchColors = {};
    batches.forEach((b, i) => {
      this.batchColors[b] = cs.getPropertyValue(order[i % order.length]).trim();
    });
  },

  renderScenarioBarState() {
    document.querySelectorAll(".scn-btn").forEach((b) =>
      b.classList.toggle("active", b.dataset.sid === this.state.scenario_id));
  },

  renderScenarioDesc() {
    const m = this.state.meta || {};
    const box = document.getElementById("scenario-desc");
    const hl = (m.highlights || []).map((h) => `<span class="hl-chip">${h}</span>`).join("");
    box.innerHTML = `<div>${m.description || this.state.name || ""}</div>
                     <div class="highlights">${hl}</div>`;
  },

  renderBanners() {
    const box = document.getElementById("banner-container");
    const s = this.state;
    const banners = [];
    (s.import_errors || []).forEach((e) =>
      banners.push(["critical", "⛔", typeof e === "string" ? e : (e.message || JSON.stringify(e))]));
    (s.import_warnings || []).slice(0, 4).forEach((w) =>
      banners.push(["warning", "⚠", typeof w === "string" ? w : (w.message || JSON.stringify(w))]));
    if (s.fit && !s.fit.ok) {
      (s.fit.feasibility || []).forEach((f) => banners.push(["critical", "⛔", f]));
      (s.fit.warnings || []).forEach((w) => banners.push(["warning", "⚠", w]));
    } else if (s.fit?.warnings?.length) {
      s.fit.warnings.slice(0, 3).forEach((w) => banners.push(["warning", "⚠", w]));
    }
    box.innerHTML = banners.map(([cls, ic, msg], i) => `
      <div class="banner ${cls}"><span class="b-icon">${ic}</span>
        <span>${msg}</span>
        <button class="b-close" data-bi="${i}" aria-label="关闭">✕</button>
      </div>`).join("");
    box.querySelectorAll(".b-close").forEach((b) =>
      b.addEventListener("click", () => { b.closest(".banner").remove(); }));
  },

  /* ---------------- 单体树 ---------------- */
  renderCellTree() {
    const s = this.state;
    const tree = document.getElementById("cell-tree");
    const lm = s.fit?.layout_meta || {};
    const batches = lm.batches || [];
    const cb = lm.cell_batch || {};
    const cells = lm.cells || s.cells_meta?.map((c) => c.cell) || [];
    const synCells = new Set(s.rows.filter((r) => r.provenance === "synthetic").map((r) => r.cell));
    const collapsed = this._collapsed || {};

    tree.innerHTML = "";
    if (!batches.length) {
      // 没有拟合布局时按 cell_batch 分组
      const groups = {};
      cells.forEach((c) => { const b = cb[c] || "默认批次"; (groups[b] = groups[b] || []).push(c); });
      Object.keys(groups).forEach((b) => batches.push(b));
    }
    batches.forEach((batch) => {
      const list = cells.filter((c) => (cb[c] || "默认批次") === batch);
      if (!list.length) return;
      const color = this.batchColors[batch] || Viz.palette().blue;
      const g = document.createElement("div");
      g.className = "batch-group" + (collapsed[batch] ? " collapsed" : "");
      g.innerHTML = `
        <button class="batch-head">
          <span class="batch-dot" style="background:${color}"></span>
          ${batch} · ${list.length} 单体
          <span class="chev">▼</span>
        </button>
        <div class="batch-cells"></div>`;
      g.querySelector(".batch-head").addEventListener("click", () => {
        g.classList.toggle("collapsed");
        this._collapsed = this._collapsed || {};
        this._collapsed[batch] = g.classList.contains("collapsed");
      });
      const wrap = g.querySelector(".batch-cells");
      list.forEach((c) => {
        const chip = document.createElement("button");
        chip.className = "cell-chip" + (c === s.selected_cell ? " active" : "");
        chip.innerHTML = c + (synCells.has(c) ? '<span class="syn-dot" title="含追加合成测量">◆</span>' : "");
        chip.addEventListener("click", () => this.selectCell(c));
        wrap.appendChild(chip);
      });
      tree.appendChild(g);
    });
  },

  /* ---------------- 预算 ---------------- */
  renderBudget() {
    const s = this.state;
    const used = s.used_samples || 0, total = s.budget_samples || 0;
    const rem = s.remaining_samples ?? (total - used);
    document.getElementById("budget-used").textContent = used;
    document.getElementById("budget-total").textContent = total;
    const fill = document.getElementById("budget-fill");
    const pct = total ? Math.min(100, (used / total) * 100) : 0;
    fill.style.width = pct + "%";
    fill.className = "budget-fill" + (rem <= 0 ? " full" : rem <= 1 ? " warn" : "");
    document.getElementById("budget-note").textContent = rem <= 0
      ? "破坏预算已用尽：不能再接受新的合成测量轮次（仍可查看历史建议与复核）。"
      : `剩余可破坏样本 ${rem} 个。接受建议时按本轮 n 计入。`;
  },

  /* ---------------- 参数解析 ---------------- */
  groupKeyFor(param, cell) {
    const scope = this.state.scopes[param];
    if (scope === "cell") return cell;
    const batch = this.state.fit.layout_meta.cell_batch[cell];
    if (scope === "batch") return `batch:${batch}`;
    return "global";
  },

  cellPhysical(cell) {
    const est = this.state.fit?.estimates;
    if (!est) return null;
    const gk = (p) => this.groupKeyFor(p, cell);
    const a = est.a[cell] ?? est.a[gk("a")] ?? Object.values(est.a)[0];
    const b1 = est.b1[gk("b1")];
    const b2 = est.b2[gk("b2")];
    const tau = est.tau[gk("tau")];
    if ([a, b1, b2, tau].some((v) => v == null || !isFinite(v))) return null;
    return { a, b1, b2, tau };
  },

  deltaFor(instrument) {
    const d = this.state.fit?.estimates?.delta || {};
    if (!this.state.model_options.instrument_offset) return 0;
    if (instrument === this.state.model_options.reference_instrument) return 0;
    return d[instrument] ?? d[`inst:${instrument}`] ?? 0;
  },

  curveValue(p, t, delta = 0) {
    const knee = Math.max(0, t - p.tau);
    return p.a - p.b1 * t - (p.b2 - p.b1) * knee + delta;
  },

  /* ---------------- 主退化图 ---------------- */
  currentLifeMarker() {
    const ok = (d) => d && d.available !== false && isFinite(d.x_hat);
    if (this.profileKind === "life" && ok(this.profileData)) {
      const p = this.profileData;
      return { hat: p.x_hat, lo: p.ci_low, hi: p.ci_high,
        lowOpen: p.low_open, highOpen: p.high_open };
    }
    if (ok(this.lifeData)) {
      const p = this.lifeData;
      return { hat: p.x_hat, lo: p.ci_low, hi: p.ci_high,
        lowOpen: p.low_open, highOpen: p.high_open };
    }
    return null;
  },

  lifeCacheKey() {
    const s = this.state;
    if (!s) return "none";
    const eol = document.getElementById("opt-eol")?.value || s.meta?.eol;
    return [s.selected_cell, eol, s.used_samples,
      s.fit?.ok ? Number(s.fit.nll).toFixed(3) : "fail"].join("|");
  },

  async refreshLifeMarker() {
    if (!this.state?.fit?.ok) { this.lifeData = null; return; }
    const key = this.lifeCacheKey();
    if (key === this._lifeKey && this.lifeData) return;
    this._lifeKey = key;
    const seq = (this._lifeSeq = (this._lifeSeq || 0) + 1);
    const target = { kind: "life", cell: this.state.selected_cell,
      eol: parseFloat(document.getElementById("opt-eol").value) || 80 };
    try {
      const out = await API.post("/api/profile", { target, n_grid: 17 });
      if (seq !== this._lifeSeq || this._lifeKey !== key) return;
      this.lifeData = out.data;
      this.renderDegradation();
    } catch { /* 标记缺失不阻塞主界面 */ }
  },

  renderDegradation() {
    const s = this.state;
    const box = document.getElementById("deg-chart");
    if (!s) return;
    const P = Viz.palette();
    const cell = s.selected_cell;
    const lm = s.fit?.layout_meta || {};
    const cb = lm.cell_batch || {};
    const batchOf = {};
    Object.keys(cb).forEach((c) => { batchOf[c] = cb[c]; });
    const candidates = this.activeDraftCandidates();

    // 图例
    this.renderLegend();

    const common = {
      rows: s.rows, selectedCell: cell, batchOf,
      batchColors: this.batchColors,
      instruments: lm.instruments || [],
      offsetEnabled: s.model_options.instrument_offset,
      eol: parseFloat(document.getElementById("opt-eol").value) || s.meta?.eol || 80,
      life: this.currentLifeMarker(),
      windows: s.meta?.windows || [],
      candidates,
      timeUnit: s.meta?.time_unit,
      rangeHi: s.meta?.range_hi,
      onSelectCell: (c) => this.selectCell(c),
    };

    const p = s.fit?.ok ? this.cellPhysical(cell) : null;
    if (p) {
      const insts = this.instrumentsForCell(cell);
      common.curveFns = insts.map((inst, i) => {
        const delta = this.deltaFor(inst);
        const isRef = inst === s.model_options.reference_instrument || i === 0;
        return {
          color: this.batchColors[cb[cell]] || P.blue,
          width: isRef ? 2.6 : 1.8,
          dash: isRef ? null : "7 4",
          opacity: isRef ? 1 : 0.85,
          fn: (t) => this.curveValue(p, t, delta),
        };
      });
      // 真值曲线
      if (this.showTruth && s.meta?.truth?.cells?.[cell]) {
        const tr = s.meta.truth.cells[cell];
        common.curveFns.push({
          color: P.muted, width: 1.6, dash: "2 4",
          fn: (t) => this.curveValue(tr, t, 0),
        });
      }
    }

    // 该单体最晚观测时间（证据边界）
    const ts = s.rows.filter((r) => r.cell === cell).map((r) => r.t);
    common.tEvidence = ts.length ? Math.max(...ts) : 0;

    Viz.degradation(box, common);
    this.renderExtrapBar();
  },

  instrumentsForCell(cell) {
    const set = new Set(this.state.rows.filter((r) => r.cell === cell).map((r) => r.instrument));
    const all = this.state.fit?.layout_meta?.instruments || [];
    const list = all.filter((i) => set.has(i));
    return list.length ? list : all;
  },

  renderLegend() {
    const P = Viz.palette();
    const lm = this.state.fit?.layout_meta || {};
    const cb = lm.cell_batch || {};
    const cell = this.state.selected_cell;
    const batches = lm.batches || [];
    const items = batches.map((b) =>
      `<span class="lg-item"><span class="lg-dot" style="background:${this.batchColors[b]}"></span>${b}</span>`);
    if (this.state.fit?.ok) {
      items.push(`<span class="lg-item"><span class="lg-swatch" style="background:${this.batchColors[cb[cell]] || P.blue}"></span>${cell} 拟合曲线</span>`);
    }
    items.push(`<span class="lg-item"><span class="lg-tri" style="color:${P.ink2}"></span>超量程删失</span>`);
    items.push(`<span class="lg-item"><span class="lg-diamond" style="background:${P.magenta}"></span>追加合成测量</span>`);
    items.push(`<span class="lg-item"><span class="lg-dot" style="background:${P.violet};border:2px solid ${P.violet};background:${P.surface}"></span>候选最优时间</span>`);
    if (this.showTruth)
      items.push(`<span class="lg-item"><span class="lg-swatch" style="background:${P.muted};background:repeating-linear-gradient(90deg,${P.muted} 0 3px,transparent 3px 5px);height:2px"></span>合成真值</span>`);
    document.getElementById("deg-legend").innerHTML = items.join("");
  },

  activeDraftCandidates() {
    // 右栏当前展示的 pending 建议的候选最优点
    const rec = this.currentDraftRec();
    if (!rec || rec.status !== "pending" || rec.params?.cell !== this.state.selected_cell) return [];
    return (rec.ranking?.candidates || []).map((c) => ({ best_t: c.best_t, best: c.best }));
  },

  currentDraftRec() {
    const recs = this.state.recommendations || [];
    if (this.pendingRecId) {
      const f = recs.find((r) => r.id === this.pendingRecId);
      if (f) return f;
    }
    return recs.find((r) => r.status === "pending") || recs[0] || null;
  },

  renderExtrapBar() {
    const bar = document.getElementById("extrap-bar");
    let ext = null;
    if (this.profileKind === "life" && this.profileData?.extrapolation) {
      ext = this.profileData.extrapolation;
    } else if (this.lifeData?.extrapolation) {
      ext = this.lifeData.extrapolation;
    } else {
      const rec = this.currentDraftRec();
      ext = rec?.extrapolation_before || null;
    }
    if (!ext) { bar.innerHTML = ""; return; }
    const [cls, label] = LEVEL_BADGE[ext.level] || ["info", ext.level];
    bar.innerHTML = `<span class="badge ${cls}">${label}</span>
      <span>${ext.text || ""}</span>
      <span class="hint">最晚观测 t=${Viz.fmt(ext.t_max_observed, 1)}；
        L 估计=${ext.life_hat != null ? Viz.fmt(ext.life_hat, 2) : "—"}
        ${ext.ci_low != null && ext.ci_high != null
          ? `；95% CI [${Viz.fmt(ext.ci_low, 2)}, ${Viz.fmt(ext.ci_high, 2)}]` : ""}
        ${ext.high_open ? "；上界开放" : ""}</span>`;
  },

  /* ---------------- 轮廓似然 ---------------- */
  currentProfileTarget() {
    const cell = this.state.selected_cell;
    const eol = parseFloat(document.getElementById("opt-eol").value) || 80;
    const kind = this.profileKind;
    if (kind === "life") return { kind: "life", cell, eol };
    if (kind === "delta") {
      const insts = this.state.fit?.layout_meta?.instruments || [];
      const ref = this.state.model_options.reference_instrument;
      const target = insts.find((i) => i !== ref) || insts[0];
      return target ? { kind: "delta", group: target } : { kind: "delta", group: "INST-B" };
    }
    // tau / b2 使用所选单体对应组
    return { kind, group: this.groupKeyFor(kind, cell) };
  },

  async refreshProfile() {
    if (!this.state?.fit) return;
    if (!this.state.fit.ok) {
      this.lifeData = null;
      this.profileData = { available: false, reason: "拟合失败：参数不可估计，轮廓似然无法计算。请查看失败原因或调整共享假设/导入数据。" };
      this.renderProfileSummary();
      this.renderProfileChart();
      this.renderDegradation();
      return;
    }
    this.profileLoading = true;
    this.profileData = null; // 目标已变，避免把旧轮廓/旧寿命标记画到新单体上
    this.renderProfileSummary();
    this.renderProfileChart();
    this.renderExtrapBar();
    const target = this.currentProfileTarget();
    const seq = (this._profSeq = (this._profSeq || 0) + 1);
    try {
      const out = await API.post("/api/profile", { target, n_grid: 21 });
      if (seq !== this._profSeq) return; // 被更新请求取代
      const p = out.data;
      if (out.status >= 400 || !p) {
        this.profileData = { available: false,
          reason: p?.message || p?.error || "轮廓似然请求失败。" };
      } else {
        this.profileData = p;
      }
      if (this.profileKind === "life" && this.profileData?.available !== false) {
        this.lifeData = p;
        this._lifeKey = this.lifeCacheKey();
      }
    } catch (e) {
      this.profileData = { available: false, reason: "轮廓似然请求失败：" + e.message };
    } finally {
      this.profileLoading = false;
    }
    this.renderProfileSummary();
    this.renderProfileChart();
    this.renderDegradation(); // life marker / extrap bar
    if (this.profileKind !== "life") this.refreshLifeMarker();
  },

  renderProfileSummary() {
    const box = document.getElementById("profile-summary");
    const p = this.profileData;
    if (this.profileLoading) {
      box.innerHTML = `<span class="inline-loading"><span class="spinner"></span> 正在计算轮廓似然（多点约束优化，约数秒）…</span>`;
      return;
    }
    if (!p) { box.innerHTML = ""; return; }
    if (p.available === false) {
      box.innerHTML = `<span class="badge critical">不可计算</span><span>${p.reason || ""}</span>`;
      return;
    }
    const badges = [];
    if (p.unreliable) badges.push(["critical", "结果不可靠"]);
    if (p.flat) badges.push(["critical", "轮廓平坦 · 不可分辨"]);
    else if (p.identifiable) badges.push(["good", "可分辨 · 有限 95% 区间"]);
    if (p.low_open || p.high_open)
      badges.push(["warn", (p.low_open && p.high_open) ? "双侧开放" : p.high_open ? "上界开放" : "下界开放"]);
    if (p.weakly_identifiable) badges.push(["warn", "可分辨性弱"]);
    if (p.wide) badges.push(["warn", "区间过宽 · 信息量不足"]);
    if (!badges.length) badges.push(["info", "已计算"]);

    const ci = (p.ci_low != null && p.ci_high != null)
      ? `<span class="profile-ci-text">95% CI：[${Viz.fmt(p.ci_low, 2)}, ${Viz.fmt(p.ci_high, 2)}] ${p.unit || ""}
         （宽度 ${Viz.fmt(p.ci_high - p.ci_low, 2)}）</span>`
      : `<span class="profile-ci-text">无有限 CI（阈值内未形成闭合区间）</span>`;
    box.innerHTML = badges.map(([c, t]) => `<span class="badge ${c}">${t}</span>`).join("") +
      `<span class="profile-ci-text">MLE = <b>${Viz.fmt(p.x_hat, 3)}</b> ${p.unit || ""}</span>${ci}`;
  },

  renderProfileChart() {
    const box = document.getElementById("profile-chart");
    if (this.profileLoading && !this.profileData) {
      box.innerHTML = `<div class="inline-loading" style="height:100%;align-items:center;justify-content:center;width:100%">
        <span class="spinner"></span> 轮廓似然计算中…</div>`;
      return;
    }
    Viz.profile(box, { profile: this.profileData });
  },

  /* ---------------- 参数表 ---------------- */
  seFor(kind, key) {
    const h = this.state.fit?.hessian_se;
    if (!h?.cov || !h.coordinate_index) return null;
    const gi = h.coordinate_index.group_index?.[
      kind === "b2" ? "ld" : kind === "b1" ? "lb1" : kind] || {};
    const idx = gi[key];
    if (idx == null) return null;
    const cov = h.cov;
    if (kind === "b1") {
      const est = this.state.fit.estimates.b1[key];
      const v = cov[idx]?.[idx];
      return v == null ? null : est * Math.sqrt(Math.max(0, v));
    }
    if (kind === "b2") {
      // b2 = exp(lb1_g) + exp(ld_g)，用协方差做 delta method
      const gi1 = h.coordinate_index.group_index.lb1[key];
      const idd = h.coordinate_index.group_index.ld[key];
      if (gi1 == null || idd == null) return null;
      const b1 = this.state.fit.estimates.b1[key];
      const d = this.state.fit.estimates.b2[key] - b1;
      const v11 = cov[gi1][gi1], vdd = cov[idd][idd], v1d = cov[gi1][idd];
      const vari = b1 * b1 * v11 + d * d * vdd + 2 * b1 * d * v1d;
      return Math.sqrt(Math.max(0, vari));
    }
    const v = cov[idx]?.[idx];
    return v == null ? null : Math.sqrt(Math.max(0, v));
  },

  renderParamsTab() {
    const root = document.getElementById("tab-params");
    const f = this.state.fit;
    if (!f) { root.innerHTML = ""; return; }
    if (!f.ok) {
      root.innerHTML = `<div class="fit-fail-panel">
        <h3>⛔ 拟合失败 / 不可识别</h3>
        <div>当前共享假设：${this.scopeSummaryText()}</div>
        <ul>${(f.feasibility || []).map((x) => `<li>${x}</li>`).join("")}
            ${(f.warnings || []).map((x) => `<li>${x}</li>`).join("")}</ul>
        <p class="card-note" style="margin-bottom:0">
          建议：把更多参数改为批次/全局共享、增加检测时间点，或切换到“全部批次共享”后重试。
          应用不会用不可信的拟合给出寿命结论。</p>
      </div>`;
      return;
    }
    const gof = f.gof || {};
    const tiles = [
      ["R²", Viz.fmt(gof.r2, 4)],
      ["RMSE", Viz.fmt(gof.rmse, 3)],
      ["精确读数 n", gof.n_exact ?? "—"],
      ["σ（残差 SD）", Viz.fmt(f.estimates.sigma, 3)],
      ["负对数似然", Viz.fmt(f.nll, 2)],
      ["AIC", Viz.fmt(f.aic, 1)],
      ["BIC", Viz.fmt(f.bic, 1)],
      ["Hessian 秩", `${f.hessian_se?.hessian_rank ?? "?"}/${f.layout_meta?.n_params ?? "?"}`],
    ];
    const tileHtml = `<div class="gof-grid">${tiles.map(([k, v]) =>
      `<div class="gof-tile"><div class="v">${v}</div><div class="k">${k}</div></div>`).join("")}</div>`;

    root.innerHTML = tileHtml + `<div class="param-sections">
      ${this.paramGroupTable("初始水平 a（每单体）", "a", f.estimates.a)}
      ${this.paramGroupTable("一阶段速率 b₁", "b1", f.estimates.b1)}
      ${this.paramGroupTable("转折点 τ", "tau", f.estimates.tau)}
      ${this.paramGroupTable("二阶段速率 b₂", "b2", f.estimates.b2)}
      ${this.paramGroupTable("检测仪偏移 δ", "delta", f.estimates.delta || {})}
    </div>
    ${f.hessian_se?.singular ? '<p class="card-note" style="color:var(--st-warning)">Hessian 奇异：部分参数标准误不可靠/相关，请看轮廓似然而非仅看 SE。</p>' : ""}`;
  },

  scopeSummaryText() {
    const L = { cell: "单体", batch: "批次", global: "全局" };
    return SCOPE_PARAMS.map((p) => `${p}=${L[this.state.scopes[p]]}`).join("，");
  },

  paramGroupTable(title, kind, dict) {
    const keys = Object.keys(dict || {});
    if (!keys.length) {
      return `<div><h4 style="margin:4px 0 6px;font-size:13px">${title}</h4>
        <div class="hint" style="font-size:12px">未启用（如检测仪偏移未建模）</div></div>`;
    }
    const rows = keys.map((k) => {
      const se = this.seFor(kind, k);
      return `<tr><td>${k}</td><td class="num">${Viz.fmt(dict[k], 4)}</td>
        <td class="num">${se != null ? Viz.fmt(se, 3) : "—"}</td></tr>`;
    }).join("");
    return `<div>
      <h4 style="margin:4px 0 6px;font-size:13px">${title}</h4>
      <table class="param-table"><thead><tr><th>参数组</th>
        <th class="num">估计</th><th class="num">Hessian SE</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
  },

  /* ---------------- 残差 ---------------- */
  renderResidual() {
    const box = document.getElementById("residual-chart");
    if (!box || this.activeTab !== "residuals") return;
    const rs = this.state.fit?.gof?.residuals || [];
    Viz.residuals(box, {
      residuals: rs,
      instruments: this.state.fit?.layout_meta?.instruments || [],
    });
  },

  /* ---------------- 数据表 ---------------- */
  renderDataTable() {
    const wrap = document.getElementById("data-table");
    if (!wrap) return;
    const f = this.dataFilter;
    let rows = [...this.state.rows].sort((a, b) =>
      (a.cell || "").localeCompare(b.cell || "") || a.t - b.t || (a.repeat || 0) - (b.repeat || 0));
    if (f) {
      rows = rows.filter((r) =>
        [r.cell, r.batch, r.instrument, r.provenance, r.record_id]
          .some((v) => String(v ?? "").toLowerCase().includes(f)));
    }
    document.getElementById("data-count").textContent =
      `显示 ${rows.length} / ${this.state.rows.length} 行`;
    const censTag = (r) => r.cens === 1 ? '<span class="tag cens-r">右删失 ≥</span>'
      : r.cens === -1 ? '<span class="tag cens-l">左删失 ≤</span>' : "";
    const provTag = (r) => r.provenance === "synthetic" ? '<span class="tag syn">合成追加</span>'
      : r.provenance === "imported" ? '<span class="tag imp">导入</span>' : "内置";
    wrap.innerHTML = `<table class="data-table"><thead><tr>
      <th>record</th><th>批次</th><th>单体</th><th>t</th><th>读数 y</th>
      <th>删失</th><th>界限</th><th>检测仪</th><th>重复</th><th>来源</th>
    </tr></thead><tbody>${rows.map((r) => `<tr>
      <td>${r.record_id || ""}</td><td>${r.batch || ""}</td><td><b>${r.cell}</b></td>
      <td>${Viz.fmt(r.t, 1)}</td><td>${Viz.fmt(r.y, 3)}</td>
      <td>${censTag(r)}</td><td>${r.limit != null ? Viz.fmt(r.limit, 2) : ""}</td>
      <td>${r.instrument || ""}</td><td>${r.repeat ?? 1}</td><td>${provTag(r)}</td>
    </tr>`).join("")}</tbody></table>`;
  },

  /* ---------------- 建议表单与结果 ---------------- */
  renderRecForm() {
    const cellSel = document.getElementById("rec-cell");
    const cells = this.state.fit?.layout_meta?.cells || [];
    if (cellSel.options.length !== cells.length ||
        ![...cellSel.options].some((o) => o.value === this.state.selected_cell)) {
      cellSel.innerHTML = cells.map((c) => `<option value="${c}">${c}</option>`).join("");
    }
    cellSel.value = this.state.selected_cell;

    const instSel = document.getElementById("rec-inst");
    const insts = this.state.fit?.layout_meta?.instruments || [];
    if (instSel.options.length !== insts.length) {
      instSel.innerHTML = insts.map((i) => `<option value="${i}">${i}</option>`).join("");
    }
    if (this.state.model_options.reference_instrument)
      instSel.value = this.state.model_options.reference_instrument;
  },

  async createRecommendation() {
    if (!this.state.fit?.ok) {
      this.toast("当前拟合不可用，无法设计检测；请先调整共享假设或数据。", "error");
      return;
    }
    const cell = document.getElementById("rec-cell").value;
    const n = parseInt(document.getElementById("rec-n").value, 10) || 1;
    const instrument = document.getElementById("rec-inst").value;
    const eol = parseFloat(document.getElementById("opt-eol").value) || 80;
    const btn = document.getElementById("btn-recommend");
    btn.disabled = true; btn.textContent = "计算中：Fisher 信息 + 寿命轮廓…";
    const out = await this.callMut(() => API.post("/api/recommend", { cell, n, instrument, eol }));
    btn.disabled = false; btn.textContent = "计算候选时间与预期信息增益";
    if (out?.status === 200 && out.data.recommendation) {
      this.pendingRecId = out.data.recommendation.id;
      await this.reloadState();
      this.pendingRecId = out.data.recommendation.id;
      this.renderRecResult();
      this.renderDegradation();
      this.toast("建议已生成并冻结预测，可接受或逐窗口选择时间。");
    }
  },

  renderRecResult() {
    const root = document.getElementById("rec-result");
    const rec = this.currentDraftRec();
    if (!rec) {
      root.innerHTML = `<div class="rec-empty">选择单体与试样数后点击计算。<br>
        候选时间来自场景定义的可用检测窗口，目标是缩小关键寿命参数 L<sub>EOL</sub> 的轮廓似然区间。</div>`;
      return;
    }
    const rk = rec.ranking || {};
    const cands = rk.candidates || [];
    const maxRed = Math.max(0.02, ...cands.map((c) => c.best?.reduction_frac || 0));
    const budgetLeft = this.state.remaining_samples ?? 0;
    const n = rec.params?.n || 1;
    const noBudget = budgetLeft < n;

    const predTiles = `
      <div class="pred-strip">
        <div class="pred-tile"><div class="k">接受前 95% 轮廓宽度</div>
          <div class="v">${rec.width_profile_before != null ? Viz.fmt(rec.width_profile_before, 2) : "开放"}</div></div>
        <div class="pred-tile"><div class="k">最优选择后预测宽度（校准）</div>
          <div class="v" style="color:var(--cat-blue)">${rec.predicted_top_width_after != null ? Viz.fmt(rec.predicted_top_width_after, 2) : "—"}</div></div>
      </div>`;

    const canAccept = rec.status === "pending" && budgetLeft >= n;
    const topAccept = `
      <button class="btn primary block" data-accept-top="${rec.id}" ${canAccept ? "" : "disabled"}
        style="margin:2px 0 10px">
        ${rec.status !== "pending" ? "该建议已处理"
          : noBudget ? "样本破坏预算不足，无法接受"
          : `接受最优建议：${rk.top_window_id || ""} 窗口 t=${Viz.fmt(rk.top_t, 1)}，追加 ${n} 个合成测量`}
      </button>`;

    const candHtml = cands.map((c, idx) => {
      const b = c.best || {};
      const red = b.reduction_frac || 0;
      const isTop = c.window_id === rk.top_window_id;
      const disabled = !canAccept;
      return `<div class="cand ${isTop ? "top" : ""}">
        <div class="cand-main">
          <div class="cand-time">${c.window_id} · t=${Viz.fmt(c.best_t, 1)}
            ${isTop ? '<span class="badge info" style="margin-left:6px">最优</span>' : ""}</div>
          <div class="cand-win">${c.label}（窗口 ${c.lo}–${c.hi}）· 预测后宽度 ${Viz.fmt(b.width_after_fisher, 2)}</div>
        </div>
        <div class="cand-metric">
          <div class="cand-w">−${Viz.fmt(red * 100, 1)}%</div>
          <div class="cand-red">预期收窄</div>
        </div>
        <div class="cand-bar"><i style="width:${Math.max(3, (red / maxRed) * 100)}%"></i></div>
        <div class="cand-actions">
          <button class="btn sm" data-accept-win="${rec.id}"
                  data-w="${c.window_id}"
                  ${disabled ? "disabled" : ""}>改选此窗口最佳点 t=${Viz.fmt(c.best_t, 1)}</button>
          ${b.already_measured ? '<span class="already">该时刻附近已有读数，重复信息较低</span>' : ""}
        </div>
      </div>`;
    }).join("");

    const statusBadge = STATUS_META[rec.status]
      ? `<span class="badge ${STATUS_META[rec.status].cls}">${STATUS_META[rec.status].label}</span>` : "";

    root.innerHTML = `
      <div class="rec-head" style="padding:0;background:none;margin-bottom:8px">
        <span class="rec-id">${rec.id}</span> ${statusBadge}
        <span class="hint">${rec.created_at || ""}</span>
      </div>
      ${rk.currently_unidentifiable ? '<div class="banner warning" style="margin-bottom:8px"><span class="b-icon">⚠</span><span>当前寿命参数在 Fisher 秩诊断中接近不可识别；建议优先选择能补全阶段二/转折点证据的晚窗口。</span></div>' : ""}
      ${predTiles}
      ${topAccept}
      <div class="candidate-list">${candHtml || '<div class="rec-empty">没有可用窗口。</div>'}</div>`;

    const topBtn = root.querySelector("[data-accept-top]");
    if (topBtn) topBtn.addEventListener("click", () =>
      this.acceptRecommendation(topBtn.dataset.acceptTop, {}));
    root.querySelectorAll("[data-accept-win]").forEach((btn) =>
      btn.addEventListener("click", () => {
        this.acceptRecommendation(btn.dataset.acceptWin,
          { window_id: btn.dataset.w });
      }));
  },

  async acceptRecommendation(rid, override) {
    if (this.acceptInFlight) return; // 防止双击/并发重复接受
    this.acceptInFlight = true;
    document.querySelectorAll("[data-accept-top],[data-accept-win]")
      .forEach((b) => { b.disabled = true; });
    try {
      const out = await this.callMut(
        () => API.post(`/api/recommendations/${rid}/accept`, { override }), null);
      if (out?.status === 200 && out.data.ok) {
        this.state = out.data.state;
        this.pendingRecId = rid;
        this.hydrateControls();
        this.renderAll();
        this.refreshProfile();
        const q = out.data.rec?.outcome?.prediction_quality;
        this.toast("已追加一轮合成测量并重新拟合。" + (q ? " 增益核对：" + q : ""), "success");
      } else if (out?.status === 409) {
        // 可能是重复点击/预算/失效：以服务端状态为准刷新
        await this.reloadState();
      }
    } finally {
      this.acceptInFlight = false;
    }
  },

  /* ---------------- 台账 ---------------- */
  renderLedger() {
    const root = document.getElementById("ledger");
    const recs = this.state.recommendations || [];
    if (!recs.length) {
      root.innerHTML = `<div class="ledger-empty">还没有建议记录。<br>生成建议后，它的预测会被冻结；接受并到达“新检测结果”后，可在此对比预测与实际信息增益，且原建议始终可追溯。</div>`;
      return;
    }
    root.innerHTML = recs.map((r) => this.ledgerCard(r)).join("");
    root.querySelectorAll("[data-reeval]").forEach((b) =>
      b.addEventListener("click", () => this.reevaluate(b.dataset.reeval)));
    this.renderLedgerMiniProfiles();
  },

  ledgerCard(r) {
    const sm = STATUS_META[r.status] || { label: r.status, cls: "info" };
    const p = r.params || {};
    const ch = r.chosen;
    const oc = r.outcome;
    const lc = r.latest_check;

    let gain = "";
    if (oc) {
      const ratio = oc.actual_over_predicted_width_ratio;
      const qCls = ratio != null && ratio < 0.6 ? "good"
        : ratio != null && ratio > 1.7 ? "warn" : "good";
      gain = `
        <div class="gain-compare">
          <div class="gain-box"><div class="k">预测后宽度（冻结）</div>
            <div class="v">${oc.predicted_width_after != null ? Viz.fmt(oc.predicted_width_after, 2) : "—"}</div></div>
          <div class="gain-arrow">→</div>
          <div class="gain-box"><div class="k">实际新轮廓宽度</div>
            <div class="v" style="color:var(--cat-aqua)">${oc.width_profile_after != null ? Viz.fmt(oc.width_profile_after, 2) : "开放"}</div></div>
        </div>
        <div class="gain-compare" style="grid-template-columns:1fr 1fr;margin-top:2px">
          <div class="gain-box"><div class="k">预测收窄</div><div class="v">${oc.predicted_reduction_frac != null ? Viz.fmt(oc.predicted_reduction_frac * 100, 1) + "%" : "—"}</div></div>
          <div class="gain-box"><div class="k">实际收窄</div><div class="v" style="color:var(--st-good)">${oc.actual_reduction_frac != null ? Viz.fmt(oc.actual_reduction_frac * 100, 1) + "%" : "区间开放"}</div></div>
        </div>
        <div class="quality-note ${qCls}">
          ${oc.prediction_quality || ""}
          ${ratio != null ? `（实际/预测宽度比 = ${Viz.fmt(ratio, 2)}）` : ""}
        </div>
        <div class="syn-values">合成测量：${(r.synthetic_rows || []).map((x) =>
          `${x.cell}@t=${Viz.fmt(x.t, 1)} → y=${Viz.fmt(x.y, 2)}${x.cens === 1 ? "（≥" + Viz.fmt(x.limit, 1) + " 删失）" : ""}`).join("；")}</div>
        <div class="mini-profile" data-mini="${r.id}"></div>
        <div class="rec-actions">
          <button class="btn sm" data-reeval="${r.id}">用当前全部数据再次核对增益</button>
          ${lc ? `<span class="hint" style="align-self:center">上次复核 ${lc.checked_at}：当前宽度 ${lc.width_now != null ? Viz.fmt(lc.width_now, 2) : "开放"}，相对最初再收窄 ${lc.additional_reduction_from_original != null ? Viz.fmt(lc.additional_reduction_from_original * 100, 1) + "%" : "—"}</span>` : ""}
        </div>`;
    }

    const chosenLine = ch
      ? `<div class="rec-frozen">接受时刻：<b>t=${Viz.fmt(ch.t, 1)}</b>
         （${ch.window_label || ch.window_id || "自定义"}）· n=${ch.n} · ${ch.instrument}
         ${ch.adjusted ? "· 含手动覆盖" : ""}</div>`
      : `<div class="rec-frozen">目标 ${p.cell} · EOL ${p.eol} · n=${p.n} · 仪器 ${p.instrument}
         · 冻结的接受前宽度 ${r.width_profile_before != null ? Viz.fmt(r.width_profile_before, 2) : "开放"}</div>`;

    return `<div class="rec-card ${r.status}">
      <div class="rec-head">
        <span class="rec-id">${r.id}</span>
        <span class="badge ${sm.cls}">${sm.label}</span>
        <span class="hint rec-status" style="margin-left:0">${r.created_at || ""}</span>
      </div>
      <div class="rec-body">
        ${chosenLine}
        ${r.status === "superseded" ? '<div class="quality-note warn">共享假设在建议生成后被修改；该建议及其预测保留留档，但不再作用于当前模型。</div>' : ""}
        ${gain}
      </div>
    </div>`;
  },

  renderLedgerMiniProfiles() {
    (this.state?.recommendations || []).forEach((r) => {
      const div = document.querySelector(`[data-mini="${r.id}"]`);
      if (!div || !r.outcome?.profile_after) return;
      // 避免重复 SVG 覆盖：仅在空时绘制
      if (div.children.length) return;
      Viz.miniProfile(div, r.profile_before, r.outcome.profile_after);
    });
  },

  async reevaluate(rid) {
    const out = await this.callMut(
      () => API.post(`/api/recommendations/${rid}/reevaluate`), null);
    if (out?.status === 200) {
      await this.reloadState();
      this.pendingRecId = rid;
      this.renderLedger();
      this.toast("已用当前全部数据重新核对（原始 outcome 未被覆盖）。");
    }
  },

  /* ---------------- CSV 导入 ---------------- */
  onPickFile(e) {
    const f = e.target.files[0];
    if (f) this.readFile(f);
  },
  readFile(f) {
    const reader = new FileReader();
    reader.onload = () => { document.getElementById("import-text").value = reader.result; };
    reader.readAsText(f, "utf-8");
  },
  async doImport() {
    const csv = document.getElementById("import-text").value;
    const errBox = document.getElementById("import-errors");
    if (!csv.trim()) {
      errBox.innerHTML = '<div class="ie">请先选择文件或粘贴 CSV 内容。</div>';
      return;
    }
    const out = await this.callMut(() => API.post("/api/session/import",
      { csv, filename: "导入的 CSV 数据" }));
    if (!out) return;
    if (out.status === 422 || out.status >= 400) {
      this.state = out.data && out.data.rows ? out.data : this.state;
      const errs = out.data?.import_errors || [];
      errBox.innerHTML = errs.map((e) =>
        `<div class="ie">${typeof e === "string" ? e : (e.message || JSON.stringify(e))}</div>`).join("")
        || '<div class="ie">导入失败，请检查列名与格式。</div>';
      if (out.data?.rows) this.renderBanners();
      return;
    }
    this.state = out.data;
    this.closeModal("import-modal");
    document.getElementById("import-text").value = "";
    errBox.innerHTML = "";
    this.hydrateControls();
    this.renderAll();
    this.refreshProfile();
    this.toast("CSV 导入成功并已完成拟合。", "success");
  },

  /* ---------------- 模型文档 ---------------- */
  renderModelDoc() {
    document.getElementById("model-doc-body").innerHTML = `
      <h4>1. 双阶段（连续拐点 / hockey-stick）退化函数</h4>
      <p>项目内的规范定义（见 <code>backend/engine/model.py</code> 与 <code>MODEL.md</code>）为：</p>
      <div class="formula">μ(t) = a − b₁·t − (b₂ − b₁)·(t − τ)₊ + δ_inst，&nbsp; (x)₊ = max(x, 0)，&nbsp; b₂ ≥ b₁</div>
      <p>τ 是未直接观测也可估计的转折点；函数在 τ 处连续，τ 前速率 b₁，τ 后速率 b₂。
      电池寿命 L<sub>EOL</sub> 由 μ(L)=y<sub>EOL</sub> 分段反解：若 a−b₁τ ≤ EOL，
      寿命落在第一阶段 L=(a−EOL)/b₁；否则 L=τ+[a−b₁τ−EOL]/b₂。</p>

      <h4>2. 层级共享参数，而不是把批次/仪器偏移当退化</h4>
      <p>a、b₁、b₂、τ 均可设为 <b>每单体 / 批次共享 / 全局共享</b>。
      检测仪偏移 δ<sub>inst</sub> 是独立的加性截距项，参考仪器 δ=0，
      它只整体平移该仪器的读数，不进入斜率项，因此不会把“换仪器造成的台阶”误判成单体加速退化。</p>

      <h4>3. 超量程/删失读数与似然</h4>
      <p>精确读数用高斯残差；写为 <code>&gt;=100</code> 的读数是右删失（真实值 ≥ 界限），
      <code>&lt;=65</code> 是左删失。似然使用 Tobit 形式：</p>
      <div class="formula">ℓᵢ = log φ((yᵢ−μᵢ)/σ) − log σ &nbsp;（精确）<br>
ℓᵢ = log[1 − Φ((Lᵢ−μᵢ)/σ)] &nbsp;（右删失），&nbsp; ℓᵢ = log Φ((Lᵢ−μᵢ)/σ) &nbsp;（左删失）</div>
      <p>参数用带解析梯度的多起点 L-BFGS-B 极大似然估计；Hessian 给出局部 SE 与秩诊断。</p>

      <h4>4. 轮廓似然与可分辨性</h4>
      <p>对目标参数（寿命 L、τ、b₂、δ 或其参数组）逐点固定并重新优化其余参数，
      得到轮廓负对数似然。95% 置信区间满足</p>
      <div class="formula">2[ℓ_max − ℓ_profile(x)] ≤ χ²₁,₀.₉₅ = 3.8415 &nbsp;⇔&nbsp; ΔNLL ≤ 1.9207</div>
      <p>曲线在搜索范围内不穿过阈值 → 区间开放/轮廓平坦，界面明确标记“不可分辨”，
      而不是只给 R²。Hessian SE 只作网格密度参考，最终区间以轮廓为准。</p>

      <h4>5. 下一次检测时间的设计准则</h4>
      <p>候选 t 只从场景/数据定义的<b>可用时间窗口</b>中选。对每个候选时刻计算
      期望 Fisher 信息 I = Σ wᵢ jᵢjᵢᵀ（删失观测的权重 wᵢ 由得分方差解析给出，
      超量程概率越大权重越低），再用 delta 方法与 Schur 补求寿命方差
      Var(L) ≈ gᵀI⁻¹g。选择使预测 95% 宽度最小的 t，并用当前轮廓宽度 ×
      Fisher 前后宽度比做校准。破坏样本数 n 受预算硬约束。</p>

      <h4>6. 可追溯与信息增益核对</h4>
      <p>生成建议时冻结：候选排序、最优 t、预测后宽度、接受前寿命轮廓。
      接受后生成一轮带随机噪声与量程删失的合成测量、重新拟合与轮廓，
      比较预测宽度与实际新轮廓宽度（比值与中文一致性判断）。
      共享假设改变只会把旧 pending 建议标记为 superseded，不会删除记录。</p>

      <h4>7. 外推证据政策</h4>
      <p>寿命晚于最晚观测时间时按差距分为 轻度 / 较强 / 远距离 外推，
      远距离或轮廓上界开放时界面强制提示“不可作确定结论”。
      拟合失败（如 3 个点对 5 个参数）直接报自由度不足，不输出寿命数字。</p>`;
  },
};

document.addEventListener("DOMContentLoaded", () => App.init());
