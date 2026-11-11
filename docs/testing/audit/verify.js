(() => {
  const vw = window.innerWidth, vh = window.innerHeight;
  const num = s => Number(String(s).replace("px","")) || 0;
  const de = document.documentElement;
  const issues = [];
  const seen = new Set();
  const push = (k, o) => { if (!seen.has(k)) { seen.add(k); issues.push(o); } };
  function parseColor(s) {
    if (!s) return null;
    if (s.indexOf("rgba") === 0 || s.indexOf("rgb") === 0) {
      const m = s.match(/[\d.]+/g); if (!m) return null;
      return { r: +m[0], g: +m[1], b: +m[2], a: m.length>3 ? +m[3] : 1 };
    }
    if (s.indexOf("color(srgb") === 0) {
      const m = s.match(/[\d.]+/g); if (!m) return null;
      return { r: +m[0]*255, g: +m[1]*255, b: +m[2]*255, a: m.length>3 ? +m[3] : 1 };
    }
    return null;
  }
  function over(fg, bg) {
    const a = fg.a === undefined ? 1 : fg.a;
    if (a >= 0.999) return { r: fg.r, g: fg.g, b: fg.b, a: 1 };
    return { r: fg.r*a + bg.r*(1-a), g: fg.g*a + bg.g*(1-a), b: fg.b*a + bg.b*(1-a), a: 1 };
  }
  function lum(c) { const f = v => { v/=255; return v<=0.03928? v/12.92 : Math.pow((v+0.055)/1.055,2.4); }; return 0.2126*f(c.r)+0.7152*f(c.g)+0.0722*f(c.b); }
  function ratio(a,b) { const L1=lum(a), L2=lum(b); return (Math.max(L1,L2)+0.05)/(Math.min(L1,L2)+0.05); }
  function effBg(el) {
    const layers = [];
    let node = el;
    while (node && node !== document.documentElement) {
      const c = parseColor(getComputedStyle(node).backgroundColor);
      if (c && c.a > 0) { layers.push(c); if (c.a >= 0.999) break; }
      node = node.parentElement;
    }
    const root = parseColor(getComputedStyle(document.documentElement).backgroundColor);
    let base = root && root.a>0 ? root : {r:255,g:255,b:255,a:1};
    for (let i = layers.length-1; i>=0; i--) base = over(layers[i], base);
    return base;
  }
  function visible(el) {
    const cs = getComputedStyle(el);
    if (cs.display==="none"||cs.visibility==="hidden") return null;
    if (num(cs.opacity) === 0) return null;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return null;
    let p = el;
    while (p && p !== document.body) {
      const pcs = getComputedStyle(p);
      if (pcs.display==="none"||pcs.visibility==="hidden") return null;
      p = p.parentElement;
    }
    return cs;
  }
  const all = Array.prototype.slice.call(document.querySelectorAll("body *"));

  // 1) 整页横向滚动条
  if (de.scrollWidth > vw + 1) push("page-hscroll", {kind:"page-hscroll", tag:"html", detail:"scrollWidth="+de.scrollWidth+" vw="+vw});

  // 2) 元素溢出视口（祖先里有横向滚动容器的算「有意为之」，跳过）
  for (const el of all) {
    const cs = visible(el); if (!cs) continue;
    const r = el.getBoundingClientRect();
    const tag = el.tagName.toLowerCase()+"."+(typeof el.className==="string"? el.className.split(" ").filter(Boolean).slice(0,2).join("."):"");
    if (r.right > vw + 2 && ["html","body"].indexOf(el.tagName.toLowerCase()) < 0) {
      let scrollable = false, anc = el.parentElement;
      while (anc && anc !== document.body) {
        const acs = getComputedStyle(anc);
        if (["auto","scroll"].indexOf(acs.overflowX) >= 0) { scrollable = true; break; }
        anc = anc.parentElement;
      }
      if (scrollable) continue;
      const p = el.parentElement, pcs = p ? getComputedStyle(p) : null;
      if (pcs && pcs.overflowX === "hidden") continue;
      push("ovfx:"+tag+"|"+Math.round(r.right), {kind:"overflow-x", tag, detail:"right="+Math.round(r.right)+" w="+Math.round(r.width)+" vw="+vw, text:(el.textContent||"").trim().slice(0,30)});
    }
  }

  // 3) 对比度（含 .btn-danger 与 .nav-group-title）
  for (const el of all) {
    if (el.children.length) continue;
    const t = (el.textContent||"").trim();
    if (!t) continue;
    const cs = visible(el); if (!cs) continue;
    const raw = parseColor(cs.color); if (!raw) continue;
    if (raw.a !== undefined && raw.a < 0.2) continue;
    const bg = effBg(el);
    const fg = over(raw, bg);
    const r = ratio(fg, bg);
    const fs = num(cs.fontSize);
    const big = fs >= 18.66 || (fs >= 14 && num(cs.fontWeight) >= 700);
    const need = big ? 3 : 4.5;
    const tag = el.tagName.toLowerCase()+"."+(typeof el.className==="string"? el.className.split(" ").filter(Boolean).slice(0,2).join("."):"");
    const disabled = (typeof el.className==="string" && el.className.indexOf("is-disabled")>=0) || cs.opacity !== "1" || el.disabled;
    if (r < need - 0.05) {
      push("c:"+tag+"|"+t.slice(0,18), {kind: disabled?"low-contrast-disabled":"low-contrast", tag, text:t.slice(0,40), ratio:+r.toFixed(2), fg:cs.color, bg:"rgb("+[bg.r,bg.g,bg.b].map(v=>Math.round(v)).join(",")+")", need});
    }
  }

  // 4) 按钮文字是否折行（LAY-04）
  //    用 Range 的行盒数量判断，min-height 撑出来的高度不算折行。
  function lineBoxes(el) {
    const range = document.createRange();
    range.selectNodeContents(el);
    const tops = [];
    for (const rect of Array.prototype.slice.call(range.getClientRects())) {
      if (rect.width === 0 && rect.height === 0) continue;
      if (!tops.some(t => Math.abs(t - rect.top) < 2)) tops.push(rect.top);
    }
    return tops.length;
  }
  for (const el of all) {
    if (typeof el.className!=="string" || el.className.indexOf("btn")<0) continue;
    const cs = visible(el); if (!cs) continue;
    const lines = lineBoxes(el);
    if (lines > 1) {
      push("btnwrap:"+(el.textContent||"").trim().slice(0,12), {kind:"button-wrap", tag:"button."+el.className, detail:"lines="+lines, text:(el.textContent||"").trim().slice(0,20)});
    }
  }

  // 5) 表头是否被压成竖排（LAY-03）
  for (const th of Array.prototype.slice.call(document.querySelectorAll(".data-table thead th"))) {
    const cs = visible(th); if (!cs) continue;
    const r = th.getBoundingClientRect();
    const fs = num(cs.fontSize);
    const lh = num(cs.lineHeight) || fs * 1.35;
    const lines = Math.round(r.height / lh);
    if (lines > 2) {
      push("th:"+(th.textContent||"").trim().slice(0,10), {kind:"th-stacked", tag:"th", detail:"lines="+lines+" h="+Math.round(r.height), text:(th.textContent||"").trim().slice(0,20)});
    }
  }

  // 6) 卡片布局是否生效（768–1199px 应该有 data-label 且没有表头）
  const tables = Array.prototype.slice.call(document.querySelectorAll("table.data-table"));
  const cardMode = vw >= 768 && vw <= 1199;
  for (const t of tables) {
    const head = t.querySelector("thead");
    if (!head) continue;
    if (cardMode) {
      if (getComputedStyle(head).display !== "none") {
        push("card-off:"+t.className, {kind:"card-layout-off", tag:"table", detail:"thead 仍然可见"});
      }
      const headCells = head.querySelectorAll("tr:first-child > th").length;
      Array.prototype.slice.call(t.querySelectorAll("tbody tr")).forEach(row => {
        const tds = Array.prototype.slice.call(row.children);
        // 空状态那一行只有一格（「暂无数据」），列名对不上，不算缺标签。
        if (tds.length !== headCells) return;
        const missing = tds.filter(td => !td.dataset.label).length;
        if (missing) push("nolabel", {kind:"missing-data-label", tag:"td", detail:"缺标签 "+missing+"/"+tds.length});
      });
    }
  }

  // 7) 勾选框 / 单选框不许被拉成长条（LAY-09）
  for (const el of all) {
    if (el.tagName !== "INPUT") continue;
    const t = (el.getAttribute("type") || "").toLowerCase();
    if (t !== "checkbox" && t !== "radio") continue;
    const cs = visible(el); if (!cs) continue;
    // 视觉隐藏的（主题卡片里的单选框）不算。
    if (cs.position === "absolute" && cs.clip !== "auto") continue;
    const r = el.getBoundingClientRect();
    if (r.width > 20 || r.height > 20) {
      push("cbsize:"+(el.name||t), {kind:"checkbox-stretched", tag:"input[type="+t+"]", detail:"w="+Math.round(r.width)+" h="+Math.round(r.height)});
    }
  }

  // 8) 左侧导航必须吸附住（LAY-08）
  const sidebar = document.querySelector(".sidebar");
  const topbar = document.querySelector(".topbar");
  if (sidebar && topbar && vw > 767) {
    const scs = getComputedStyle(sidebar);
    if (scs.position !== "sticky") {
      push("sbpos", {kind:"sidebar-not-sticky", tag:"aside.sidebar", detail:"position="+scs.position});
    }
    const sb = sidebar.getBoundingClientRect();
    const tb = topbar.getBoundingClientRect();
    const gap = Math.round(sb.top - tb.bottom);
    if (Math.abs(gap) > 2) {
      push("sbgap", {kind:"sidebar-gap", tag:"aside.sidebar", detail:"gap="+gap});
    }
    if (sb.bottom > vh + 2) {
      push("sbbottom", {kind:"sidebar-overflow", tag:"aside.sidebar", detail:"bottom="+Math.round(sb.bottom)+" vh="+vh});
    }
  }

  return {theme: de.dataset.theme, mode: de.dataset.colorMode, density: de.dataset.density, vw, vh, tables: tables.length, issues};
})()
