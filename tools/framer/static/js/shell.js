// The sidebar and clock shared by the Overview and Publishing pages.
const Shell = (() => {
  const NAV = [
    ['Projects', '/', ['M4 5h6v6H4z', 'M14 5h6v6h-6z', 'M4 15h6v4H4z', 'M14 15h6v4h-6z']],
    ['Overview', '/overview', ['M4 19V9', 'M10 19V5', 'M16 19v-7', 'M22 19H2']],
    ['Publishing', '/publishing', ['M12 15V4', 'M7 9l5-5 5 5', 'M5 20h14']],
  ];
  const RECENT = 3;
  const REFRESH_MS = 10000;

  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const icon = (paths, size = 15, width = 2) => `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="${width}" stroke-linecap="round" stroke-linejoin="round">${paths.map(p => `<path d="${p}"/>`).join('')}</svg>`;
  const dot = (color, pulse = false) => `<span class="dot${pulse ? ' pulse' : ''}" style="background:${color}"></span>`;
  const clockText = () => new Date().toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit' });

  let active = 'Overview';

  function render(projects, waiting) {
    const busy = projects.some(p => p.running);
    const counts = { Projects: String(projects.length), Overview: '', Publishing: waiting ? String(waiting) : '' };
    const nav = NAV.map(([label, href, paths]) => `<a class="nav-link${label === active ? ' active' : ''}" href="${href}">
      <span class="ico">${icon(paths)}</span><span class="grow">${label}</span>
      <span class="count">${label === 'Overview' && busy ? dot('var(--blue)', true) : ''}${counts[label]}</span></a>`).join('');
    const recent = projects.slice(0, RECENT).map(p => {
      const total = p.chapters || 1, done = (p.counts.complete || 0) + (p.counts.rendered || 0);
      return `<a class="recent" href="/?project=${encodeURIComponent(p.slug)}"><span class="top"><span>${esc(p.name)}</span>${p.running ? dot('var(--blue)', true) : ''}</span>
        <span class="mini-bar"><span style="width:${done / total * 100}%;background:var(--green)"></span><span style="width:${(p.running || 0) / total * 100}%;background:var(--blue)"></span></span></a>`;
    }).join('');
    document.getElementById('side').innerHTML = `<div class="brand"><div class="logo">RC</div>
      <div class="name"><b>ReCapper</b><span>v1.0.0</span></div></div>${nav}
      <span class="side-label">RECENT</span>${recent}`;
  }

  async function refresh() {
    try {
      const [listed, publishing] = await Promise.all([
        fetch('/api/projects', { cache: 'no-store' }).then(r => r.json()),
        fetch('/api/publishing', { cache: 'no-store' }).then(r => r.json())]);
      render(listed.projects, publishing.items.filter(i => i.status === 'waiting_approval').length);
    } catch (e) { /* the sidebar keeps what it last showed */ }
  }

  function init(page) {
    active = page;
    render([], 0);
    refresh();
    setInterval(refresh, REFRESH_MS);
    const clock = document.getElementById('clock');
    const tick = () => { clock.textContent = clockText(); };
    tick();
    setInterval(tick, 1000);
  }

  return { init, esc, icon, dot, refresh };
})();
