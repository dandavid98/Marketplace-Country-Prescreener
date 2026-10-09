"""ui_shell.py - ProductWatch-style app shell (sidebar + header) for the prescreener.

Mirrors ProductWatch's AppShell / AppNav / Header layout (rna-data/productwatch,
frontend/src/shell/*) in plain server-rendered HTML. Styles live in static/ui.css.

Only pages the prescreener actually has are listed in the sidebar. ProductWatch's
other apps, sign-in, RBAC, command palette, Datadog, etc. are intentionally absent.
"""
from __future__ import annotations

from html import escape as html_escape
from pathlib import Path

from fastapi.responses import HTMLResponse

BRAND = "Prescreener"
STATIC_DIR = Path(__file__).with_name("static")

# Lucide icon bodies (lucide.dev, ISC license): the same icon set ProductWatch uses.
_ICONS = {
    "brand": '<path d="M3 7V5a2 2 0 0 1 2-2h2"/><path d="M17 3h2a2 2 0 0 1 2 2v2"/><path d="M21 17v2a2 2 0 0 1-2 2h-2"/><path d="M7 21H5a2 2 0 0 1-2-2v-2"/><circle cx="12" cy="12" r="3"/><path d="m16 16-1.9-1.9"/>',
    "search": '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
    "activity": '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>',
    "list-checks": '<path d="m3 17 2 2 4-4"/><path d="m3 7 2 2 4-4"/><path d="M13 6h8"/><path d="M13 12h8"/><path d="M13 18h8"/>',
    "house": '<path d="M15 21v-8a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v8"/><path d="M3 10a2 2 0 0 1 .709-1.528l7-5.999a2 2 0 0 1 2.582 0l7 5.999A2 2 0 0 1 21 10v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "chevron-right": '<path d="m9 18 6-6-6-6"/>',
    "panel-left-close": '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M9 3v18"/><path d="m16 15-3-3 3-3"/>',
    "panel-left": '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M9 3v18"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/>',
    "moon": '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
    "monitor": '<rect width="20" height="14" x="2" y="3" rx="2"/><line x1="8" x2="16" y1="21" y2="21"/><line x1="12" x2="12" y1="17" y2="21"/>',
}


def icon(name: str, extra_class: str = "") -> str:
    cls = f"icon {extra_class}".strip()
    return f'<svg class="{cls}" viewBox="0 0 24 24" aria-hidden="true" focusable="false">{_ICONS[name]}</svg>'


def _css_version() -> str:
    """Cache-buster so browsers pick up ui.css edits without a hard refresh."""
    try:
        return str(int((STATIC_DIR / "ui.css").stat().st_mtime))
    except OSError:
        return "0"


# Runs in <head>, before first paint, so a dark/collapsed preference never flashes.
_PRE_PAINT = """(function(){try{var r=document.documentElement,m=localStorage.getItem('prescreener_theme')||'system';
if(m==='dark'||(m==='system'&&matchMedia('(prefers-color-scheme: dark)').matches))r.classList.add('dark');
if(localStorage.getItem('prescreener_nav_collapsed')==='1')r.classList.add('nav-collapsed');}catch(e){}})();"""

_SHELL_SCRIPT = """(function(){
  var root = document.documentElement, mq = matchMedia('(prefers-color-scheme: dark)');
  function mode(){ return localStorage.getItem('prescreener_theme') || 'system'; }
  function applyTheme(){
    var m = mode();
    root.classList.toggle('dark', m === 'dark' || (m === 'system' && mq.matches));
    document.querySelectorAll('[data-theme-mode]').forEach(function(b){
      b.setAttribute('aria-pressed', String(b.dataset.themeMode === m));
    });
  }
  document.querySelectorAll('[data-theme-mode]').forEach(function(b){
    b.addEventListener('click', function(){ localStorage.setItem('prescreener_theme', b.dataset.themeMode); applyTheme(); });
  });
  mq.addEventListener('change', applyTheme);
  applyTheme();

  var navBtn = document.getElementById('nav-collapse');
  function syncNav(){
    var c = root.classList.contains('nav-collapsed');
    navBtn.setAttribute('aria-expanded', String(!c));
    navBtn.title = (c ? 'Expand' : 'Collapse') + ' navigation (Ctrl+B)';
  }
  function toggleNav(){
    var c = root.classList.toggle('nav-collapsed');
    localStorage.setItem('prescreener_nav_collapsed', c ? '1' : '0');
    syncNav();
  }
  navBtn.addEventListener('click', toggleNav);
  document.addEventListener('keydown', function(e){
    var t = e.target.tagName;
    if ((e.ctrlKey || e.metaKey) && !e.shiftKey && e.key.toLowerCase() === 'b' && t !== 'INPUT' && t !== 'TEXTAREA') {
      e.preventDefault(); toggleNav();
    }
  });
  syncNav();
})();"""


def _nav(active: str, latest_job_id: str | None) -> str:
    items = [("scan", "/", "New scan", "search")]
    # Live scan only exists once a job has been started this session.
    if latest_job_id:
        items.append(("job", f"/job/{latest_job_id}", "Live scan", "activity"))
    items.append(("queue", "/queue", "Review queue", "list-checks"))
    current = ' aria-current="page"'
    links = "".join(
        f'<a class="shell-item" href="{html_escape(href)}" title="{label}"'
        f'{current if key == active else ""}>'
        f'{icon(ico)}<span class="shell-label">{label}</span></a>'
        for key, href, label, ico in items
    )
    return f"""<nav class="shell-nav" aria-label="Main">
  <div class="shell-brand">{icon("brand")}<span class="shell-label">{BRAND}</span></div>
  <div class="shell-items">{links}</div>
  <div class="shell-foot">
    <button type="button" class="shell-icon-btn" id="nav-collapse" aria-label="Toggle navigation">
      {icon("panel-left-close", "when-expanded")}{icon("panel-left", "when-collapsed")}
    </button>
  </div>
</nav>"""


def _header(page_title: str) -> str:
    themes = "".join(
        f'<button type="button" data-theme-mode="{m}" aria-pressed="false" title="{m.title()} theme" aria-label="{m.title()} theme">{icon(ico)}</button>'
        for m, ico in (("light", "sun"), ("dark", "moon"), ("system", "monitor"))
    )
    return f"""<header class="shell-header">
  <ol class="crumbs">
    <li><a href="/" aria-label="Home">{icon("house")}</a></li>
    <li class="sep" aria-hidden="true">{icon("chevron-right")}</li>
    <li><h1 aria-current="page">{html_escape(page_title)}</h1></li>
  </ol>
  <div class="theme-toggle" role="group" aria-label="Color theme">{themes}</div>
</header>"""


def render_page(
    page_title: str,
    body: str,
    *,
    active: str,
    subtitle: str = "",
    extra_script: str = "",
    latest_job_id: str | None = None,
    doc_title: str = "Marketplace Country Prescreener",
) -> HTMLResponse:
    """Wrap page `body` in the ProductWatch-style shell."""
    sub = f'<p class="page-subtitle">{html_escape(subtitle)}</p>' if subtitle else ""
    return HTMLResponse(f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html_escape(page_title)} · {html_escape(doc_title)}</title>
  <script>{_PRE_PAINT}</script>
  <link rel="stylesheet" href="/static/ui.css?v={_css_version()}">
</head>
<body>
<div class="shell">
  {_nav(active, latest_job_id)}
  <div class="shell-main">
    {_header(page_title)}
    <main class="shell-content" id="content">
      {sub}
      {body}
    </main>
  </div>
</div>
<script>{_SHELL_SCRIPT}</script>
<script>{extra_script}</script>
</body>
</html>""")
