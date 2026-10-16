/* Shared by index.html (dashboard) and cards.html (Xiaohongshu cards).
   Formatting helpers, the Chinese overlay, and the chart pieces (share bar, ladder bars, sparkline). */

const PM = (() => {
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const pct = x => x == null ? '—' : Math.round(x * 100) + '%';
  const pt = x => !x ? '' : (x >= 0 ? '+' : '−') + Math.abs(Math.round(x * 100)) + 'pt';
  const usd = x => x >= 1e6 ? '$' + (x / 1e6).toFixed(1) + 'M' : x >= 1e3 ? '$' + Math.round(x / 1e3) + 'k' : '$' + Math.round(x);
  const shortDate = d => { const [, m, dd] = d.split('-'); return `${+m}/${+dd}`; };
  // "数据截至 北京 10/16 03:27 · 加州 10/15 12:27" from day.generatedAt ("2026-10-16 03:27 +0800").
  const parseGen = s => new Date(String(s).replace(' ', 'T').replace(/ ([+-]\d{2})(\d{2})$/, '$1:$2'));
  const inZone = (d, tz) => { const p = Object.fromEntries(new Intl.DateTimeFormat('en-US', { timeZone: tz, month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false }).formatToParts(d).map(x => [x.type, x.value])); return `${p.month}/${p.day} ${p.hour}:${p.minute}`; };
  const stamp = day => { const d = parseGen(day.generatedAt); return isNaN(d) ? '' : `数据截至 北京 ${inZone(d, 'Asia/Shanghai')} · 加州 ${inZone(d, 'America/Los_Angeles')}`; };
  const cnDate = d => { const [y, m, dd] = d.split('-'); return `${y} 年 ${+m} 月 ${+dd} 日`; };

  const DOMAINS = [['politics', '时政'], ['finance', '金融'], ['tech', '科技']];
  const OFFICIAL_GROUPS = [['politics', '时政与全球', ['politics', 'world']], ['finance', '金融与加密', ['finance', 'crypto']], ['tech', '科技', ['tech']], ['culture', '文化体育', ['culture']]];

  // Chinese overlay written daily by the writing agent (reports/<date>.zh.json). Missing keys fall back to English.
  let L = {};
  const setOverlay = o => { L = o || {}; };
  const overlay = () => L;
  // Outcome labels that recur every day; proper nouns stay English.
  const ZH_OUTCOMES = {
    'Yes': '是', 'No': '否', 'No change': '不变', 'Democratic Party': '民主党', 'Republican Party': '共和党',
    'Democrats Sweep': '民主党全胜', 'Republicans Sweep': '共和党全胜', 'R Senate, D House': '共和党参院、民主党众院',
    'D Senate, R House': '民主党参院、共和党众院', '25 bps increase': '加息 25bp', '50+ bps increase': '加息 50bp+',
    '25 bps decrease': '降息 25bp', '50+ bps decrease': '降息 50bp+', 'Other': '其他',
  };
  const MONTH = { January: '1 月', February: '2 月', March: '3 月', April: '4 月', May: '5 月', June: '6 月', July: '7 月', August: '8 月', September: '9 月', October: '10 月', November: '11 月', December: '12 月' };
  const label = q => (L.labels && L.labels[q]) || q;
  const outcomeLabel = s => {
    if (!s) return '';
    if (L.outcomes && L.outcomes[s]) return L.outcomes[s];
    if (ZH_OUTCOMES[s]) return ZH_OUTCOMES[s];
    const m = s.match(/^(January|February|March|April|May|June|July|August|September|October|November|December) (\d{1,2})(?:, (\d{4}))?$/);
    if (m) return `${m[3] ? m[3] + ' 年 ' : ''}${MONTH[m[1]]} ${m[2]} 日前`;
    return s;
  };

  // Categorical slots in fixed order (dataviz palette); parties keep their conventional colours.
  const SLOTS = ['var(--c1)', 'var(--c2)', 'var(--c3)', 'var(--c4)', 'var(--c5)'];
  const colorFor = (lbl, i) => /Democrat/i.test(lbl) ? 'var(--c1)' : /Republican/i.test(lbl) ? 'var(--c8)' : SLOTS[i % SLOTS.length];

  /* Share bar for exclusive events: top outcomes as stacked segments, remainder as "其他". */
  function shareBar(e, n = 3) {
    const top = e.outcomes.slice(0, n).filter(o => o.yes != null);
    const rest = Math.max(0, 1 - top.reduce((s, o) => s + o.yes, 0));
    const segs = top.map((o, i) => ({ l: outcomeLabel(o.label), v: o.yes, c: colorFor(o.label, i), d: o.change1d }));
    if (rest >= 0.02) segs.push({ l: '其他', v: rest, c: 'var(--mid)', d: null, rest: true });
    return `<div class="share">${segs.map(s => `<i style="width:${(s.v * 100).toFixed(1)}%;background:${s.c}" title="${esc(s.l)} ${pct(s.v)}"></i>`).join('')}</div>
      <div class="legend">${segs.map(s => `<span><b style="background:${s.c}"></b>${esc(s.l)} <strong>${pct(s.v)}</strong>${s.d && Math.abs(s.d) >= 0.005 ? `<small>${pt(s.d)}</small>` : ''}</span>`).join('')}</div>`;
  }

  /* Ladder bars: one short bar per rung, highest first. Never stacked, since rungs are not shares. */
  function ladderBars(e, n = 4) {
    const rows = e.outcomes.slice(0, n).filter(o => o.yes != null);
    return `<div class="ladder">${rows.map(o => `<div class="rung"><span class="rl">${esc(outcomeLabel(o.label))}</span><div class="rt"><i style="width:${(o.yes * 100).toFixed(1)}%"></i></div><span class="rv">${pct(o.yes)}${o.change1d && Math.abs(o.change1d) >= 0.005 ? `<small>${pt(o.change1d)}</small>` : ''}</span></div>`).join('')}</div>`;
  }

  /* Binary market: a single Yes bar on a track. */
  function binaryBar(e) {
    const o = e.outcomes[0] || {};
    return `<div class="ladder"><div class="rung"><span class="rl">Yes</span><div class="rt"><i style="width:${((o.yes || 0) * 100).toFixed(1)}%"></i></div><span class="rv">${pct(o.yes)}${o.change1d && Math.abs(o.change1d) >= 0.005 ? `<small>${pt(o.change1d)}</small>` : ''}</span></div></div>`;
  }

  const outcomeChart = e => e.kind === 'exclusive' ? shareBar(e) : e.kind === 'ladder' ? ladderBars(e) : binaryBar(e);

  /* Diverging change bar centred at zero. */
  function changeBar(ch, max) {
    const w = Math.min(50, Math.abs(ch) / (max || 1) * 50);
    return `<div class="bar"><i class="${ch >= 0 ? 'up' : 'down'}" style="width:${w}%"></i></div>`;
  }

  /* Sparkline: 2px line, end dot with a surface ring. */
  function spark(pts, W = 96, H = 26, stroke = 2, r = 4) {
    if (!pts || pts.length < 2) return `<svg class="spark" viewBox="0 0 ${W} ${H}"></svg>`;
    const ys = pts.map(p => p[1]); const lo = Math.min(...ys), hi = Math.max(...ys), span = (hi - lo) || 0.01;
    const xy = pts.map((p, i) => [i / (pts.length - 1) * W, H - 3 - (p[1] - lo) / span * (H - 6)]);
    const d = xy.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ',' + p[1].toFixed(1)).join('');
    const [ex, ey] = xy[xy.length - 1];
    return `<svg class="spark" viewBox="0 0 ${W} ${H}"><title>${pct(ys[0])} → ${pct(ys[ys.length - 1])}</title><path d="${d}" fill="none" stroke="var(--c1)" stroke-width="${stroke}" stroke-linejoin="round" stroke-linecap="round"/><circle cx="${ex.toFixed(1)}" cy="${ey.toFixed(1)}" r="${r}" fill="var(--c1)" stroke="var(--surface)" stroke-width="${stroke}"/></svg>`;
  }

  /* Official board: one group (politics+world etc.), de-duplicated, skip list applied, biggest move first. */
  function officialRows(day, group, n = 8) {
    const g = OFFICIAL_GROUPS.find(x => x[0] === group);
    const skip = new Set(L.skip || []), seen = new Set(), out = [];
    g[2].flatMap(c => day.official[c] || []).sort((a, b) => Math.abs(b.change1d) - Math.abs(a.change1d)).forEach(m => {
      if (seen.has(m.question) || skip.has(m.question)) return;
      seen.add(m.question); out.push(m);
    });
    return out.slice(0, n);
  }
  const resolvedRows = (day, n = 8) => {
    const seen = new Set();
    return DOMAINS.flatMap(([d]) => day.resolved[d]).filter(r => !seen.has(r.question) && seen.add(r.question)).sort((a, b) => b.volume - a.volume).slice(0, n);
  };
  const newRows = (day, n = 6) => {
    const posts = day.feed.filter(p => p.kind === 'new').map(p => ({ title: p.text, outcomes: [], official: true, kind: 'binary' }));
    const seen = new Set(posts.map(p => p.title));
    return posts.concat(DOMAINS.flatMap(([d]) => day.new[d]).filter(e => !seen.has(e.title) && seen.add(e.title))).slice(0, n);
  };
  const hotRows = (day, n = 10) => DOMAINS.flatMap(([d]) => day.hot[d].filter(e => e.pinned)).concat(DOMAINS.flatMap(([d]) => day.hot[d].filter(e => !e.pinned).slice(0, 3))).slice(0, n);

  /* Load one day: data + overlay + post text + rendered PNG index. */
  async function loadDay(date) {
    const j = u => fetch(u, { cache: 'no-store' }).then(r => r.ok ? r.json() : null).catch(() => null);
    const t = u => fetch(u, { cache: 'no-store' }).then(r => r.ok ? r.text() : null).catch(() => null);
    const [day, zh, xhs, cards] = await Promise.all([j(`data/${date}.json`), j(`reports/${date}.zh.json`), t(`reports/${date}.xhs.md`), j(`cards/${date}/index.json`)]);
    setOverlay(zh);
    return { day, zh: zh || {}, xhs, cards: (cards && cards.cards) || [] };
  }
  const loadIndex = () => fetch('data/index.json', { cache: 'no-store' }).then(r => r.json());

  /* Zip the day's PNGs in the browser (JSZip from cdnjs) and trigger a download. */
  async function downloadZip(date, list, btn) {
    if (typeof JSZip === 'undefined') { alert('打包库没有加载，请逐张下载。'); return; }
    const orig = btn.textContent; btn.disabled = true;
    try {
      const zip = new JSZip();
      for (let i = 0; i < list.length; i++) {
        btn.textContent = `打包 ${i + 1}/${list.length}…`;
        zip.file(`polymarket-${date}-${list[i].file}`, await fetch(`cards/${date}/${list[i].file}`, { cache: 'no-store' }).then(r => r.blob()));
      }
      const a = document.createElement('a');
      a.href = URL.createObjectURL(await zip.generateAsync({ type: 'blob' })); a.download = `polymarket-${date}-cards.zip`;
      document.body.appendChild(a); a.click(); a.remove();
      btn.textContent = '已下载';
    } catch (e) { btn.textContent = '失败：' + e.message; }
    setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 2500);
  }

  return { esc, pct, pt, usd, shortDate, cnDate, stamp, DOMAINS, OFFICIAL_GROUPS, setOverlay, overlay, label, outcomeLabel,
           shareBar, ladderBars, binaryBar, outcomeChart, changeBar, spark, officialRows, resolvedRows, newRows, hotRows,
           loadDay, loadIndex, downloadZip };
})();
