#!/usr/bin/env python3
"""Regenerate ForgeOS Dispatch dashboard from live git + gh state."""
import argparse, json, os, re, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

EPICS = {
    "quote-lifecycle": ["quote-status-chips","quote-total-breakdown","quote-activity-log","quote-internal-notes","quote-notes","quote-templates-mvp","quote-pdf-download","quote-email-send","quote-reminder-cron","quote-signature-capture","quote-to-invoice","quote-duplicate","quote-bulk-delete","quote-export-csv","stale-nudge-mvp","days-since-sent-mvp","quote-pipeline-kanban"],
    "customer": ["customer-notes-tags","customer-credit-terms","customer-search-filter","customer-quotes-crosslink","copy-clipboard-customer","phone-address-autoformat"],
    "ux-nav": ["dark-mode-toggle","kbd-shortcuts-mvp","kbd-shortcut-modal","sortable-columns","session-timeout-warning","recent-items-sidebar"],
    "line-items-pricing": ["line-item-typeahead","lineitem-dup-delete","qty-discount-ladder","pricebook-admin-crud"],
    "security": ["login-lockout"],
    "sage-integration": ["sage-pragmatic-demo","sage-extract-prep","sage-extract-paid-script"],
    "reporting": ["rep-kpi-mvp"],
}

PROJECTS = {
    "ForgeOS Product": ["quote-lifecycle","customer","ux-nav","line-items-pricing","security","reporting"],
    "SAGE Integration": ["sage-integration"],
    "Dispatch Dashboard": [],
}


def run(cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, shell=False)
    return r.stdout.strip()


def list_feature_branches(src):
    out = run(["git", "-C", src, "branch", "--list", "local/feature-*"])
    branches = []
    for line in out.splitlines():
        name = line.strip().lstrip("+* ").strip()
        if name.startswith("local/feature-"):
            branches.append(name)
    return branches


def merged_branches(src):
    out = run(["git", "-C", src, "branch", "--merged", "main"])
    return {line.strip().lstrip("+* ").strip() for line in out.splitlines() if line.strip()}


def branch_for_slug(slug, all_branches):
    prefix = f"local/feature-{slug}-"
    for b in all_branches:
        if b.startswith(prefix):
            return b
    return None


def loc_diff(src, branch):
    out = run(["git", "-C", src, "diff", "--shortstat", f"main...{branch}"])
    if not out:
        return 0
    ins = re.search(r"(\d+) insertions?", out)
    dele = re.search(r"(\d+) deletions?", out)
    total = 0
    if ins: total += int(ins.group(1))
    if dele: total += int(dele.group(1))
    return total


def has_checklist(src, branch, slug):
    if not branch:
        return False
    out = run(["git", "-C", src, "ls-tree", "-r", "--name-only", branch])
    return f"docs/verification/{slug}-mvp-tester-checklist-" in out


def load_prs(out_dir):
    merged = {}
    open_prs = {}
    pr_data_available = True
    try:
        with open(Path(out_dir) / "_pr_merged.json", "r", encoding="utf-8") as f:
            for pr in json.load(f):
                merged[pr["headRefName"]] = pr
        with open(Path(out_dir) / "_pr_open.json", "r", encoding="utf-8") as f:
            for pr in json.load(f):
                open_prs[pr["headRefName"]] = pr
    except Exception as e:
        print(f"[warn] PR data load failed: {e}", file=sys.stderr)
        pr_data_available = False
    return merged, open_prs, pr_data_available


def pr_check_state(pr):
    rollup = pr.get("statusCheckRollup") or []
    if not rollup:
        return "pending"
    for c in rollup:
        state = (c.get("state") or c.get("conclusion") or "").upper()
        if state in ("FAILURE","ERROR","TIMED_OUT","CANCELLED"):
            return "failing"
    for c in rollup:
        state = (c.get("state") or c.get("conclusion") or "").upper()
        if state in ("PENDING","IN_PROGRESS","QUEUED",""):
            return "pending"
    return "green"


def compute_status(slug, all_branches, merged_set, merged_prs, open_prs, src):
    branch = branch_for_slug(slug, all_branches)
    info = {"slug": slug, "branch": branch, "loc": 0, "pr": None, "pr_url": None, "has_checklist": False}
    if not branch:
        for name, pr in {**merged_prs, **open_prs}.items():
            if slug in name:
                info["pr"] = pr["number"]
                info["pr_url"] = f"https://github.com/Forge-Promo-LLC/ForgeOS/pull/{pr['number']}"
                if name in merged_prs:
                    info["status"] = "merged"
                    return info
        info["status"] = "not-started"
        return info
    info["loc"] = loc_diff(src, branch)
    info["has_checklist"] = has_checklist(src, branch, slug)
    remote_name = branch.replace("local/", "", 1)
    pr = merged_prs.get(remote_name) or merged_prs.get(branch)
    if pr:
        info["pr"] = pr["number"]
        info["pr_url"] = f"https://github.com/Forge-Promo-LLC/ForgeOS/pull/{pr['number']}"
        info["status"] = "merged"
        return info
    if branch in merged_set:
        info["status"] = "merged"
        return info
    open_pr = open_prs.get(remote_name) or open_prs.get(branch)
    if open_pr:
        info["pr"] = open_pr["number"]
        info["pr_url"] = f"https://github.com/Forge-Promo-LLC/ForgeOS/pull/{open_pr['number']}"
        state = pr_check_state(open_pr)
        info["status"] = "pr-checks-failing" if state == "failing" else "pr-open"
        return info
    info["status"] = "tests-written" if info["has_checklist"] else "in-progress"
    return info


def build_data(src, out_dir):
    all_branches = list_feature_branches(src)
    merged_set = merged_branches(src)
    merged_prs, open_prs, pr_avail = load_prs(out_dir)
    features = {}
    for epic, slugs in EPICS.items():
        for slug in slugs:
            features[slug] = compute_status(slug, all_branches, merged_set, merged_prs, open_prs, src)
    def rollup(slug_list):
        total = len(slug_list)
        counts = {"merged":0,"pr-open":0,"pr-checks-failing":0,"tests-written":0,"in-progress":0,"not-started":0}
        for s in slug_list:
            counts[features[s]["status"]] += 1
        pct = round(counts["merged"] / total * 100) if total else 0
        return {"total": total, "by_status": counts, "percent": pct}
    epic_rollups = {epic: rollup(slugs) for epic, slugs in EPICS.items()}
    project_rollups = {}
    for proj, epic_list in PROJECTS.items():
        slug_list = [s for e in epic_list for s in EPICS.get(e, [])]
        project_rollups[proj] = {**rollup(slug_list), "epics": epic_list}
    overall = rollup([s for slugs in EPICS.values() for s in slugs])
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pr_data_available": pr_avail,
        "projects": project_rollups,
        "epics": {epic: {**epic_rollups[epic], "slugs": slugs} for epic, slugs in EPICS.items()},
        "features": features,
        "summary": overall,
    }


HTML_HEAD = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ForgeOS Progress</title>
<style>
:root {
  --bg:#faf6ef; --card:#fffdf7; --ink:#1f2937; --muted:#6b7280;
  --accent:#d4622a; --accent-soft:#f4d5c2; --border:#e7ddc9;
  --green:#2d7a3f; --blue:#2563eb; --amber:#d97706; --lblue:#06b6d4; --gray:#6b7280; --lgray:#d1d5db;
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif; padding:24px 20px 96px; }
h1 { margin:0 0 4px; font-size:28px; color:var(--accent); letter-spacing:-0.01em; }
.subtitle { color:var(--muted); font-size:13px; margin-bottom:20px; }
.overall { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:18px 22px; margin-bottom:22px; }
.overall-pct { font-size:38px; font-weight:700; color:var(--accent); line-height:1; }
.overall-txt { color:var(--muted); font-size:14px; margin-top:4px; }
.bar { background:var(--accent-soft); border-radius:6px; height:10px; overflow:hidden; margin-top:10px; }
.bar > i { display:block; height:100%; background:var(--accent); border-radius:6px; transition:width .3s; }
.projects { display:flex; flex-wrap:wrap; gap:14px; margin-bottom:28px; }
.pcard { flex:1 1 260px; background:var(--card); border:1px solid var(--border); border-radius:12px; padding:16px 18px; }
.pcard h3 { margin:0 0 6px; font-size:16px; }
.pcard .big { font-size:30px; font-weight:700; color:var(--accent); line-height:1.1; }
.pcard .sub { color:var(--muted); font-size:12px; margin-top:6px; }
h2 { font-size:18px; margin:24px 0 12px; color:var(--ink); }
"""

HTML_CSS2 = r"""details { background:var(--card); border:1px solid var(--border); border-radius:10px; margin-bottom:10px; overflow:hidden; }
details summary { cursor:pointer; padding:12px 16px; display:flex; align-items:center; gap:12px; list-style:none; font-weight:600; }
details summary::-webkit-details-marker { display:none; }
details summary::before { content:"\25b8"; color:var(--muted); font-size:12px; transition:transform .15s; }
details[open] summary::before { transform:rotate(90deg); }
.epic-name { flex:1; }
.epic-pct { font-size:13px; color:var(--muted); font-variant-numeric:tabular-nums; min-width:80px; text-align:right; }
.epic-bar { flex:0 0 120px; background:var(--accent-soft); height:6px; border-radius:4px; overflow:hidden; }
.epic-bar > i { display:block; height:100%; background:var(--accent); }
.features { padding:4px 16px 14px; border-top:1px solid var(--border); }
.frow { display:flex; align-items:center; gap:10px; padding:8px 0; border-bottom:1px dashed #eee; font-size:13px; }
.frow:last-child { border-bottom:none; }
.frow .slug { flex:1; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; }
.pill { display:inline-block; padding:2px 9px; border-radius:999px; font-size:11px; font-weight:600; color:#fff; letter-spacing:.02em; white-space:nowrap; }
.pill.merged { background:var(--green); }
.pill.pr-open { background:var(--blue); }
.pill.pr-checks-failing { background:var(--amber); }
.pill.tests-written { background:var(--lblue); }
.pill.in-progress { background:var(--gray); }
.pill.not-started { background:var(--lgray); color:#374151; }
.loc { color:var(--muted); font-size:11px; font-variant-numeric:tabular-nums; }
.pr-link { color:var(--accent); text-decoration:none; font-size:12px; font-weight:600; }
.pr-link:hover { text-decoration:underline; }
.check { color:var(--green); font-weight:700; }
.legend { margin-top:28px; padding:14px 18px; background:var(--card); border:1px solid var(--border); border-radius:10px; }
.legend h3 { margin:0 0 8px; font-size:13px; color:var(--muted); text-transform:uppercase; letter-spacing:.05em; }
.legend-items { display:flex; flex-wrap:wrap; gap:14px; align-items:center; }
.legend-items span { display:inline-flex; align-items:center; gap:6px; font-size:12px; }
.regen { position:fixed; bottom:16px; right:16px; background:var(--accent); color:#fff; border:none; padding:12px 18px; border-radius:999px; font-weight:600; cursor:pointer; box-shadow:0 4px 14px rgba(212,98,42,.35); font-size:13px; }
.regen:hover { background:#b8531e; }
.toast { position:fixed; bottom:70px; right:16px; background:#1f2937; color:#fff; padding:10px 14px; border-radius:8px; font-size:12px; opacity:0; transition:opacity .3s; pointer-events:none; }
.toast.show { opacity:1; }
@media (max-width:640px) { .projects { flex-direction:column; } .epic-bar { flex:0 0 60px; } }
</style>
</head>
<body>
"""

HTML_BODY = r"""<h1>ForgeOS Progress</h1>
<div class="subtitle" id="updated"></div>
<div class="overall">
  <div class="overall-pct" id="oPct">0%</div>
  <div class="overall-txt" id="oTxt"></div>
  <div class="bar"><i id="oBar" style="width:0%"></i></div>
</div>
<h2>Projects</h2>
<div class="projects" id="projects"></div>
<h2>Epics</h2>
<div id="epics"></div>
<div class="legend">
  <h3>Status</h3>
  <div class="legend-items">
    <span><span class="pill merged">merged</span>shipped to main</span>
    <span><span class="pill pr-open">pr-open</span>PR open, checks green</span>
    <span><span class="pill pr-checks-failing">pr-checks-failing</span>PR open, checks failing</span>
    <span><span class="pill tests-written">tests-written</span>branch + verification checklist</span>
    <span><span class="pill in-progress">in-progress</span>branch exists, no checklist</span>
    <span><span class="pill not-started">not-started</span>no branch yet</span>
  </div>
</div>
<button class="regen" id="regenBtn" title="Run regenerate.ps1 locally to refresh from git">Regenerate</button>
<div class="toast" id="toast">Copied "regenerate dashboard" to clipboard</div>
"""

HTML_JS = r"""<script>
const DATA = __DATA__;
function pill(status){ return '<span class="pill ' + status + '">' + status + '</span>'; }
function bar(pct){ return '<div class="bar"><i style="width:' + pct + '%"></i></div>'; }
function render(){
  const d = DATA;
  const upd = 'Last updated: ' + new Date(d.generated_at).toLocaleString() + (d.pr_data_available ? '' : ' — PR data unavailable');
  document.getElementById('updated').textContent = upd;
  const s = d.summary;
  document.getElementById('oPct').textContent = s.percent + '%';
  document.getElementById('oTxt').textContent = s.by_status.merged + ' of ' + s.total + ' features shipped';
  document.getElementById('oBar').style.width = s.percent + '%';
  const projEl = document.getElementById('projects');
  for (const [name, p] of Object.entries(d.projects)) {
    const label = p.total === 0 ? 'no features tracked yet' : (p.by_status.merged + ' of ' + p.total + ' shipped');
    projEl.insertAdjacentHTML('beforeend',
      '<div class="pcard"><h3>' + name + '</h3>' +
      '<div class="big">' + p.percent + '%</div>' +
      bar(p.percent) +
      '<div class="sub">' + label + '</div></div>');
  }
  const epicsEl = document.getElementById('epics');
  for (const [epic, e] of Object.entries(d.epics)) {
    let rows = '';
    for (const slug of e.slugs) {
      const f = d.features[slug];
      const loc = f.loc ? ('<span class="loc">' + f.loc + ' LOC</span>') : '';
      const pr = f.pr ? ('<a class="pr-link" href="' + f.pr_url + '" target="_blank" rel="noopener">#' + f.pr + '</a>') : '';
      const chk = f.has_checklist ? '<span class="check" title="verification checklist present">✓</span>' : '';
      rows += '<div class="frow">' + pill(f.status) + '<span class="slug">' + slug + '</span>' + chk + loc + pr + '</div>';
    }
    epicsEl.insertAdjacentHTML('beforeend',
      '<details><summary><span class="epic-name">' + epic + '</span>' +
      '<div class="epic-bar"><i style="width:' + e.percent + '%"></i></div>' +
      '<span class="epic-pct">' + e.percent + '% · ' + e.by_status.merged + '/' + e.total + '</span></summary>' +
      '<div class="features">' + rows + '</div></details>');
  }
}
render();
document.getElementById('regenBtn').addEventListener('click', function(){
  const msg = 'regenerate dashboard';
  if (navigator.clipboard) navigator.clipboard.writeText(msg).catch(function(){});
  const t = document.getElementById('toast'); t.classList.add('show'); setTimeout(function(){t.classList.remove('show');}, 2200);
  window.open('mailto:jhebert@forgepromo.com?subject=Regenerate%20Dispatch&body=regenerate%20dashboard', '_blank');
});
</script>
</body>
</html>
"""


def build_html(data):
    return (HTML_HEAD + HTML_CSS2 + HTML_BODY + HTML_JS).replace("__DATA__", json.dumps(data))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    src = args.source
    out = args.out
    os.makedirs(out, exist_ok=True)
    try:
        with open(Path(out) / "_pr_merged.json", "w", encoding="utf-8") as f:
            subprocess.run(["gh", "pr", "list", "--state", "merged", "--limit", "100",
                            "--json", "number,title,mergedAt,headRefName",
                            "--repo", "Forge-Promo-LLC/ForgeOS"],
                           stdout=f, stderr=subprocess.PIPE, check=False)
        with open(Path(out) / "_pr_open.json", "w", encoding="utf-8") as f:
            subprocess.run(["gh", "pr", "list", "--state", "open", "--limit", "100",
                            "--json", "number,title,headRefName,statusCheckRollup",
                            "--repo", "Forge-Promo-LLC/ForgeOS"],
                           stdout=f, stderr=subprocess.PIPE, check=False)
    except Exception as e:
        print(f"[warn] gh refresh failed: {e}", file=sys.stderr)
    subprocess.run(["git", "-C", src, "fetch", "--all", "--prune"], capture_output=True, check=False)
    data = build_data(src, out)
    with open(Path(out) / "data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    html = build_html(data)
    with open(Path(out) / "dispatch.html", "w", encoding="utf-8") as f:
        f.write(html)
    with open(Path(out) / "index.html", "w", encoding="utf-8") as f:
        f.write(html)
    s = data["summary"]
    print(f"Wrote dispatch.html + data.json — {s['by_status']['merged']}/{s['total']} merged ({s['percent']}%)")


if __name__ == "__main__":
    main()
