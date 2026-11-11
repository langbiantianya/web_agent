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
  if (de.scrollWidth > vw + 1) push("page-hscroll", {kind:"page-hscroll", tag:"html", detail:"scrollWidth="+de.scrollWidth+" vw="+vw});
  for (const el of all) {
    const cs = visible(el); if (!cs) continue;
    const r = el.getBoundingClientRect();
    const tag = el.tagName.toLowerCase()+"."+(typeof el.className==="string"? el.className.split(" ").filter(Boolean).slice(0,2).join("."):"");
    if (r.right > vw + 2 && ["html","body"].indexOf(el.tagName.toLowerCase()) < 0) {
      const p = el.parentElement, pcs = p ? getComputedStyle(p) : null;
      if (pcs && ["auto","scroll","hidden"].indexOf(pcs.overflowX) >= 0) continue;
      push("ovfx:"+tag+"|"+Math.round(r.right), {kind:"overflow-x", tag, detail:"right="+Math.round(r.right)+" w="+Math.round(r.width)+" vw="+vw, text:(el.textContent||"").trim().slice(0,30)});
    }
    if (el.children.length === 0 && (el.textContent||"").trim()) {
      const t = el.textContent.trim();
      if (el.scrollWidth > el.clientWidth + 2 && cs.overflow === "hidden" && cs.textOverflow === "ellipsis")
        push("clip:"+tag+"|"+t.slice(0,20), {kind:"clipped-text", tag, detail:t.slice(0,50)});
      if (el.scrollHeight > el.clientHeight + 2 && cs.overflow === "hidden")
        push("clipv:"+tag+"|"+t.slice(0,20), {kind:"clipped-vert-text", tag, detail:t.slice(0,50)});
      const fs = num(cs.fontSize);
      if (fs > 0 && fs < 10) push("tiny:"+tag, {kind:"tiny-font", tag, detail:"font-size="+fs});
    }
  }
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
  return {theme: de.dataset.theme, mode: de.dataset.colorMode, density: de.dataset.density, vw, vh, issues};
})()