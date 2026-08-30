#!/usr/bin/env python3
"""Regenerate ForgeOS Dispatch dashboard from live git + gh state.

v3 — pipeline-stage view. Every tracked feature is placed at its FURTHEST
delivery stage so real WIP (branches, tests, PRs) is visible, not just merges.
"""
import argparse, json, os, re, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

# ------------ epic + project mapping ------------
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

# 7-stage pipeline in delivery order (further to the right = further along)
STAGES = ["not-started","in-progress","tests-written","pr-open","pr-failing","pr-behind","merged"]
STAGE_LABEL = {
    "not-started":  "Not started",
    "in-progress":  "In progress",
    "tests-written":"Tests written",
    "pr-open":      "PR open",
    "pr-failing":   "PR failing",
    "pr-behind":    "PR behind",
    "merged":       "Merged",
}


# ------------ shell + git helpers ------------
def run(cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, shell=False)
    return r.stdout.strip()


def list_feature_branches(src):
    """All local/feature-* branches in the source repo."""
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
    """Total LOC touched (insertions + deletions) vs main."""
    out = run(["git", "-C", src, "diff", "--shortstat", f"main...{branch}"])
    if not out:
        return 0
    ins = re.search(r"(\d+) insertions?", out)
    dele = re.search(r"(\d+) deletions?", out)
    total = 0
    if ins: total += int(ins.group(1))
    if dele: total += int(dele.group(1))
    return total


def commits_ahead(src, branch):
    out = run(["git", "-C", src, "rev-list", "--count", f"main..{branch}"])
    try:
        return int(out)
    except Exception:
        return 0


def test_files_added(src, branch):
    """Count of *new* test files added on the branch (files under tests/)."""
    out = run(["git", "-C", src, "diff", "--name-only", "--diff-filter=AM",
               f"main...{branch}", "--", "tests/"])
    if not out:
        return 0
    return sum(1 for line in out.splitlines() if line.strip().endswith(".py"))


def has_checklist(src, branch, slug):
    if not branch:
        return False
    out = run(["git", "-C", src, "ls-tree", "-r", "--name-only", branch])
    return f"docs/verification/{slug}-mvp-tester-checklist-" in out


# ------------ PR data (cached JSON from `gh pr list`) ------------
def load_prs(out_dir):
    """Load merged + open PR JSON cached by main(). Returns {}, {}, False if unavailable."""
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
        # Empty files (gh not authed) = degrade
        if not merged and not open_prs:
            pr_data_available = False
    except Exception as e:
        print(f"[warn] PR data load failed: {e}", file=sys.stderr)
        pr_data_available = False
    return merged, open_prs, pr_data_available


def pr_check_state(pr):
    """Return 'failing' | 'pending' | 'green' from a PR's statusCheckRollup."""
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


def pr_queued_count(pr):
    """How many check runs are QUEUED (waiting on a runner)."""
    rollup = pr.get("statusCheckRollup") or []
    n = 0
    for c in rollup:
        state = (c.get("state") or c.get("conclusion") or c.get("status") or "").upper()
        if state == "QUEUED":
            n += 1
    return n


# ------------ per-feature stage computation ------------
def compute_status(slug, all_branches, merged_set, merged_prs, open_prs, src):
    """Return the feature dict with its FURTHEST stage."""
    branch = branch_for_slug(slug, all_branches)
    info = {
        "slug": slug, "branch": branch,
        "loc": 0, "commits": 0, "tests_added": 0,
        "has_checklist": False,
        "pr": None, "pr_url": None,
        "pr_state": None, "pr_merge_state": None,
        "status": "not-started",
    }

    # No local branch — maybe already merged (branch cleaned up) via PR name match
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

    # Have a branch — gather git stats
    info["loc"] = loc_diff(src, branch)
    info["commits"] = commits_ahead(src, branch)
    info["tests_added"] = test_files_added(src, branch)
    info["has_checklist"] = has_checklist(src, branch, slug)

    # Merged?  (branch-name match against merged PRs, or locally merged)
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

    # Open PR?
    open_pr = open_prs.get(remote_name) or open_prs.get(branch)
    if open_pr:
        info["pr"] = open_pr["number"]
        info["pr_url"] = f"https://github.com/Forge-Promo-LLC/ForgeOS/pull/{open_pr['number']}"
        state = pr_check_state(open_pr)
        merge_state = (open_pr.get("mergeStateStatus") or "").upper()
        info["pr_state"] = state
        info["pr_merge_state"] = merge_state
        if state == "failing":
            info["status"] = "pr-failing"
        elif merge_state == "BEHIND":
            info["status"] = "pr-behind"
        else:
            info["status"] = "pr-open"
        return info

    # No PR — decide between tests-written / in-progress / not-started
    if info["commits"] == 0 and info["loc"] == 0:
        info["status"] = "not-started"
    elif info["has_checklist"] and info["tests_added"] > 0:
        info["status"] = "tests-written"
    else:
        info["status"] = "in-progress"
    return info


# ------------ rollups ------------
def build_data(src, out_dir):
    all_branches = list_feature_branches(src)
    merged_set = merged_branches(src)
    merged_prs, open_prs, pr_avail = load_prs(out_dir)

    features = {}
    for _epic, slugs in EPICS.items():
        for slug in slugs:
            features[slug] = compute_status(slug, all_branches, merged_set,
                                            merged_prs, open_prs, src)

    def stage_zero_counts():
        return {s: 0 for s in STAGES}

    # Pipeline aggregates (count / LOC / tests per stage)
    pipeline = {s: {"count": 0, "loc": 0, "tests": 0, "slugs": []} for s in STAGES}
    for slug, f in features.items():
        st = f["status"]
        pipeline[st]["count"] += 1
        pipeline[st]["loc"] += f.get("loc", 0) or 0
        pipeline[st]["tests"] += f.get("tests_added", 0) or 0
        pipeline[st]["slugs"].append(slug)

    # Headline stats
    stages_with_code = ["in-progress","tests-written","pr-open","pr-failing","pr-behind","merged"]
    code_written_loc   = sum(pipeline[s]["loc"]   for s in stages_with_code)
    code_written_tests = sum(pipeline[s]["tests"] for s in stages_with_code)
    code_written_features = sum(pipeline[s]["count"] for s in stages_with_code)
    awaiting_merge = pipeline["pr-open"]["count"] + pipeline["pr-failing"]["count"] + pipeline["pr-behind"]["count"]
    shipped        = pipeline["merged"]["count"]

    # Bottleneck panel (from live open-PR JSON)
    ci_queued = sum(pr_queued_count(pr) for pr in open_prs.values())
    ci_failing = sum(1 for pr in open_prs.values() if pr_check_state(pr) == "failing")
    behind_prs = sum(1 for pr in open_prs.values()
                     if (pr.get("mergeStateStatus") or "").upper() == "BEHIND")

    bottleneck = {
        "ci_queued": ci_queued,
        "ci_failing": ci_failing,
        "behind": behind_prs,
        "pr_data_available": pr_avail,
    }

    # Rollups per epic + project (used by v2 hierarchy)
    def rollup(slug_list):
        total = len(slug_list)
        counts = stage_zero_counts()
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
        "pipeline": pipeline,
        "headline": {
            "loc": code_written_loc,
            "tests": code_written_tests,
            "features_with_code": code_written_features,
            "awaiting_merge": awaiting_merge,
            "shipped": shipped,
        },
        "bottleneck": bottleneck,
        "projects": project_rollups,
        "epics": {epic: {**epic_rollups[epic], "slugs": slugs} for epic, slugs in EPICS.items()},
        "features": features,
        "summary": overall,
    }


# ============================================================
#  HTML template  (warm-cream aesthetic, kept from v2)
# ============================================================
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
  --green:#2d7a3f; --blue:#2563eb; --amber:#d97706; --red:#b91c1c;
  --lblue:#06b6d4; --gray:#6b7280; --lgray:#d1d5db;
  --stage-notstarted:#e5e2d8; --stage-inprogress:#d9cfae;
  --stage-tests:#f0d3a6; --stage-propen:#e6b47c;
  --stage-prfail:#e78f6d; --stage-prbehind:#dfb26a; --stage-merged:#c86a2c;
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif; padding:24px 20px 96px; }
h1 { margin:0 0 4px; font-size:28px; color:var(--accent); letter-spacing:-0.01em; }
.subtitle { color:var(--muted); font-size:13px; margin-bottom:22px; }

/* ---------- pipeline row ---------- */
.pipeline { display:grid; grid-template-columns:repeat(7,1fr); gap:10px; margin-bottom:22px; }
.stage {
  background:var(--card); border:1px solid var(--border); border-radius:12px;
  padding:14px 12px 12px; position:relative; min-height:130px;
  display:flex; flex-direction:column; justify-content:space-between;
}
.stage .stage-title {
  font-size:11px; letter-spacing:.04em; text-transform:uppercase;
  color:var(--muted); font-weight:700; margin-bottom:6px;
}
.stage .stage-count { font-size:34px; font-weight:800; line-height:1; color:var(--ink); }
.stage .stage-sub {
  color:var(--muted); font-size:11px; font-variant-numeric:tabular-nums;
  margin-top:8px; line-height:1.35;
}
.stage .stage-sub strong { color:var(--ink); font-weight:600; }
.stage .stage-bar {
  position:absolute; top:0; left:0; right:0; height:4px; border-radius:12px 12px 0 0;
}
.stage-notstarted .stage-bar { background:var(--stage-notstarted); }
.stage-inprogress .stage-bar { background:var(--stage-inprogress); }
.stage-tests     .stage-bar { background:var(--stage-tests); }
.stage-propen    .stage-bar { background:var(--stage-propen); }
.stage-prfail    .stage-bar { background:var(--stage-prfail); }
.stage-prbehind  .stage-bar { background:var(--stage-prbehind); }
.stage-merged    .stage-bar { background:var(--stage-merged); }
.stage.warn { border-color:#e78f6d; box-shadow:inset 0 0 0 1px rgba(231,143,109,.25); }
.stage.warn.amber { border-color:#dfb26a; box-shadow:inset 0 0 0 1px rgba(223,178,106,.25); }

/* ---------- headline stat cards ---------- */
.stats { display:grid; grid-template-columns:repeat(3,1fr); gap:14px; margin-bottom:22px; }
.stat {
  background:var(--card); border:1px solid var(--border); border-radius:12px;
  padding:16px 20px;
}
.stat .stat-label {
  font-size:12px; color:var(--muted); text-transform:uppercase;
  letter-spacing:.05em; font-weight:600;
}
.stat .stat-big {
  font-size:38px; font-weight:800; color:var(--accent); line-height:1.05; margin-top:4px;
}
.stat .stat-sub { color:var(--muted); font-size:12px; margin-top:6px; }

/* ---------- bottleneck ---------- */
.bottleneck {
  background:var(--card); border:1px solid var(--border); border-radius:12px;
  padding:12px 16px; margin-bottom:26px; display:flex; flex-wrap:wrap;
  gap:14px; align-items:center; font-size:13px;
}
.bottleneck .b-label {
  font-size:11px; text-transform:uppercase; letter-spacing:.05em;
  color:var(--muted); font-weight:700; margin-right:4px;
}
.bottleneck .b-chip {
  display:inline-flex; align-items:center; gap:6px; padding:4px 10px;
  border-radius:999px; background:var(--accent-soft); color:#7c3d15;
  font-size:12px; font-weight:600;
}
.bottleneck .b-chip.red { background:#f7d4c8; color:#7d1d12; }
.bottleneck .b-chip.amber { background:#f2dcae; color:#7a4c11; }
.bottleneck .b-chip.calm { background:#dfe9d8; color:#2d5027; }
"""

HTML_CSS2 = r"""
/* ---------- projects + epics (v2 hierarchy) ---------- */
h2 { font-size:18px; margin:22px 0 10px; color:var(--ink); }
.projects { display:flex; flex-wrap:wrap; gap:14px; margin-bottom:22px; }
.pcard { flex:1 1 240px; background:var(--card); border:1px solid var(--border);
  border-radius:12px; padding:14px 16px; }
.pcard h3 { margin:0 0 6px; font-size:15px; }
.pcard .big { font-size:26px; font-weight:700; color:var(--accent); line-height:1.1; }
.pcard .sub { color:var(--muted); font-size:12px; margin-top:6px; }
.pbar { background:var(--accent-soft); border-radius:6px; height:8px; overflow:hidden; margin-top:8px; }
.pbar > i { display:block; height:100%; background:var(--accent); border-radius:6px; transition:width .3s; }

details { background:var(--card); border:1px solid var(--border); border-radius:10px;
  margin-bottom:10px; overflow:hidden; }
details summary { cursor:pointer; padding:12px 16px; display:flex; align-items:center;
  gap:12px; list-style:none; font-weight:600; }
details summary::-webkit-details-marker { display:none; }
details summary::before { content:"\25b8"; color:var(--muted); font-size:12px; transition:transform .15s; }
details[open] summary::before { transform:rotate(90deg); }
.epic-name { flex:1; }
.epic-pct { font-size:13px; color:var(--muted); font-variant-numeric:tabular-nums;
  min-width:80px; text-align:right; }
.epic-bar { flex:0 0 120px; background:var(--accent-soft); height:6px; border-radius:4px; overflow:hidden; }
.epic-bar > i { display:block; height:100%; background:var(--accent); }
.features { padding:4px 16px 14px; border-top:1px solid var(--border); }
.frow { display:flex; align-items:center; gap:10px; padding:8px 0;
  border-bottom:1px dashed #eee; font-size:13px; }
.frow:last-child { border-bottom:none; }
.frow .slug { flex:1; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; }
.pill { display:inline-block; padding:2px 9px; border-radius:999px;
  font-size:11px; font-weight:700; color:#fff; letter-spacing:.02em; white-space:nowrap; }
.pill.not-started   { background:var(--lgray); color:#374151; }
.pill.in-progress   { background:var(--gray); }
.pill.tests-written { background:var(--lblue); }
.pill.pr-open       { background:var(--blue); }
.pill.pr-failing    { background:var(--red); }
.pill.pr-behind     { background:var(--amber); }
.pill.merged        { background:var(--green); }
.loc { color:var(--muted); font-size:11px; font-variant-numeric:tabular-nums; }
.pr-link { color:var(--accent); text-decoration:none; font-size:12px; font-weight:600; }
.pr-link:hover { text-decoration:underline; }
.check { color:var(--green); font-weight:700; }

.legend { margin-top:24px; padding:14px 18px; background:var(--card);
  border:1px solid var(--border); border-radius:10px; }
.legend h3 { margin:0 0 8px; font-size:13px; color:var(--muted);
  text-transform:uppercase; letter-spacing:.05em; }
.legend-items { display:flex; flex-wrap:wrap; gap:14px; align-items:center; }
.legend-items span { display:inline-flex; align-items:center; gap:6px; font-size:12px; }

.regen { position:fixed; bottom:16px; right:16px; background:var(--accent);
  color:#fff; border:none; padding:12px 18px; border-radius:999px;
  font-weight:600; cursor:pointer; box-shadow:0 4px 14px rgba(212,98,42,.35); font-size:13px; }
.regen:hover { background:#b8531e; }
.toast { position:fixed; bottom:70px; right:16px; background:#1f2937; color:#fff;
  padding:10px 14px; border-radius:8px; font-size:12px; opacity:0;
  transition:opacity .3s; pointer-events:none; }
.toast.show { opacity:1; }

@media (max-width:1000px) {
  .pipeline { grid-template-columns:repeat(4,1fr); }
  .stats { grid-template-columns:1fr; }
}
@media (max-width:640px) {
  .pipeline { grid-template-columns:repeat(2,1fr); }
  .projects { flex-direction:column; }
  .epic-bar { flex:0 0 60px; }
}
</style>
</head>
<body>
"""

HTML_BODY = r"""<h1>ForgeOS Progress</h1>
<div class="subtitle" id="updated"></div>

<div class="pipeline" id="pipeline"></div>

<div class="stats" id="stats"></div>

<div class="bottleneck" id="bottleneck"></div>

<h2>Projects</h2>
<div class="projects" id="projects"></div>

<h2>Epics</h2>
<div id="epics"></div>

<div class="legend">
  <h3>Pipeline stages</h3>
  <div class="legend-items">
    <span><span class="pill not-started">not-started</span>in the plan, no branch</span>
    <span><span class="pill in-progress">in-progress</span>branch + commits, no tests yet</span>
    <span><span class="pill tests-written">tests-written</span>tester checklist + new tests</span>
    <span><span class="pill pr-open">pr-open</span>PR open, checks green/pending</span>
    <span><span class="pill pr-failing">pr-failing</span>PR open, CI failing</span>
    <span><span class="pill pr-behind">pr-behind</span>PR open, behind main</span>
    <span><span class="pill merged">merged</span>shipped to main</span>
  </div>
</div>

<button class="regen" id="regenBtn" title="Run regenerate.ps1 locally to refresh from git">Regenerate</button>
<div class="toast" id="toast">Copied "regenerate dashboard" to clipboard</div>
"""

HTML_JS = r"""<script>
const DATA = __DATA__;

const STAGE_ORDER = ["not-started","in-progress","tests-written","pr-open","pr-failing","pr-behind","merged"];
const STAGE_LABEL = {
  "not-started":  "Not started",
  "in-progress":  "In progress",
  "tests-written":"Tests written",
  "pr-open":      "PR open",
  "pr-failing":   "PR failing",
  "pr-behind":    "PR behind",
  "merged":       "Merged"
};
const STAGE_CLASS = {
  "not-started":  "stage-notstarted",
  "in-progress":  "stage-inprogress",
  "tests-written":"stage-tests",
  "pr-open":      "stage-propen",
  "pr-failing":   "stage-prfail",
  "pr-behind":    "stage-prbehind",
  "merged":       "stage-merged"
};

function fmt(n){ return (n||0).toLocaleString(); }
function pill(status){ return '<span class="pill ' + status + '">' + status + '</span>'; }

function renderPipeline(){
  const el = document.getElementById('pipeline');
  const p = DATA.pipeline || {};
  el.innerHTML = STAGE_ORDER.map(function(s){
    const d = p[s] || {count:0,loc:0,tests:0};
    const warn = (s === "pr-failing") ? " warn" : (s === "pr-behind" ? " warn amber" : "");
    return '<div class="stage ' + STAGE_CLASS[s] + warn + '">' +
      '<div class="stage-bar"></div>' +
      '<div>' +
        '<div class="stage-title">' + STAGE_LABEL[s] + '</div>' +
        '<div class="stage-count">' + d.count + '</div>' +
      '</div>' +
      '<div class="stage-sub"><strong>' + fmt(d.loc) + '</strong> LOC · <strong>' +
        fmt(d.tests) + '</strong> tests</div>' +
    '</div>';
  }).join('');
}

function renderStats(){
  const h = DATA.headline || {};
  const el = document.getElementById('stats');
  el.innerHTML =
    '<div class="stat">' +
      '<div class="stat-label">Code written today</div>' +
      '<div class="stat-big">' + fmt(h.loc) + '</div>' +
      '<div class="stat-sub">LOC across <strong>' + fmt(h.features_with_code) +
        '</strong> features · <strong>' + fmt(h.tests) + '</strong> new test files</div>' +
    '</div>' +
    '<div class="stat">' +
      '<div class="stat-label">Awaiting merge</div>' +
      '<div class="stat-big">' + fmt(h.awaiting_merge) + '</div>' +
      '<div class="stat-sub">features with an open PR (green, failing, or behind)</div>' +
    '</div>' +
    '<div class="stat">' +
      '<div class="stat-label">Shipped to main</div>' +
      '<div class="stat-big">' + fmt(h.shipped) + '</div>' +
      '<div class="stat-sub">features merged</div>' +
    '</div>';
}

function renderBottleneck(){
  const b = DATA.bottleneck || {};
  const el = document.getElementById('bottleneck');
  const chips = [];
  if (!b.pr_data_available) {
    chips.push('<span class="b-chip amber">PR data unavailable — run <code>gh auth login</code></span>');
  } else {
    if (b.ci_queued > 0)  chips.push('<span class="b-chip amber">Waiting on ' + b.ci_queued + ' CI runner' + (b.ci_queued===1?'':'s') + '</span>');
    if (b.ci_failing > 0) chips.push('<span class="b-chip red">'    + b.ci_failing + ' failing CI</span>');
    if (b.behind > 0)     chips.push('<span class="b-chip amber">'  + b.behind    + ' BEHIND main</span>');
    if (chips.length === 0) chips.push('<span class="b-chip calm">All open PRs green &amp; up-to-date</span>');
  }
  el.innerHTML = '<span class="b-label">Bottlenecks</span>' + chips.join('');
}

function renderProjects(){
  const el = document.getElementById('projects');
  el.innerHTML = '';
  for (const [name, p] of Object.entries(DATA.projects)) {
    const label = p.total === 0 ? 'no features tracked yet' : (p.by_status.merged + ' of ' + p.total + ' shipped');
    el.insertAdjacentHTML('beforeend',
      '<div class="pcard"><h3>' + name + '</h3>' +
      '<div class="big">' + p.percent + '%</div>' +
      '<div class="pbar"><i style="width:' + p.percent + '%"></i></div>' +
      '<div class="sub">' + label + '</div></div>');
  }
}

function renderEpics(){
  const el = document.getElementById('epics');
  el.innerHTML = '';
  for (const [epic, e] of Object.entries(DATA.epics)) {
    let rows = '';
    for (const slug of e.slugs) {
      const f = DATA.features[slug];
      const loc = f.loc ? ('<span class="loc">' + f.loc + ' LOC</span>') : '';
      const pr  = f.pr  ? ('<a class="pr-link" href="' + f.pr_url + '" target="_blank" rel="noopener">#' + f.pr + '</a>') : '';
      const chk = f.has_checklist ? '<span class="check" title="verification checklist present">✓</span>' : '';
      rows += '<div class="frow">' + pill(f.status) +
              '<span class="slug">' + slug + '</span>' + chk + loc + pr + '</div>';
    }
    el.insertAdjacentHTML('beforeend',
      '<details><summary><span class="epic-name">' + epic + '</span>' +
      '<div class="epic-bar"><i style="width:' + e.percent + '%"></i></div>' +
      '<span class="epic-pct">' + e.percent + '% · ' + e.by_status.merged + '/' + e.total + '</span></summary>' +
      '<div class="features">' + rows + '</div></details>');
  }
}

function render(){
  document.getElementById('updated').textContent =
    'Last updated: ' + new Date(DATA.generated_at).toLocaleString() +
    (DATA.pr_data_available ? '' : ' — PR data unavailable');
  renderPipeline();
  renderStats();
  renderBottleneck();
  renderProjects();
  renderEpics();
}
render();

document.getElementById('regenBtn').addEventListener('click', function(){
  const msg = 'regenerate dashboard';
  if (navigator.clipboard) navigator.clipboard.writeText(msg).catch(function(){});
  const t = document.getElementById('toast');
  t.classList.add('show'); setTimeout(function(){t.classList.remove('show');}, 2200);
  window.open('mailto:jhebert@forgepromo.com?subject=Regenerate%20Dispatch&body=regenerate%20dashboard', '_blank');
});
</script>
</body>
</html>
"""


def build_html(data):
    return (HTML_HEAD + HTML_CSS2 + HTML_BODY + HTML_JS).replace("__DATA__", json.dumps(data))


# ------------ main ------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, help="Path to Sales_Pipeline clone (source of branches)")
    ap.add_argument("--out",    required=True, help="Path to dispatch-dashboard clone (output)")
    args = ap.parse_args()
    src, out = args.source, args.out
    os.makedirs(out, exist_ok=True)

    # Refresh PR cache from gh — only overwrite the cache if gh returns real JSON.
    # If gh is missing or unauth'd, keep the previous cache so the dashboard still
    # renders live-ish PR data instead of collapsing to zeros.
    def gh(fields, state, path):
        try:
            r = subprocess.run(
                ["gh", "pr", "list", "--state", state, "--limit", "100",
                 "--json", fields,
                 "--repo", "Forge-Promo-LLC/ForgeOS"],
                capture_output=True, text=True, check=False)
            body = (r.stdout or "").strip()
            if not body or not body.startswith("["):
                print(f"[warn] gh {state} returned no JSON — keeping existing cache", file=sys.stderr)
                return
            json.loads(body)  # validate
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
        except FileNotFoundError:
            print(f"[warn] gh binary not found — keeping existing cache", file=sys.stderr)
        except Exception as e:
            print(f"[warn] gh {state} refresh failed: {e} — keeping existing cache", file=sys.stderr)

    gh("number,title,mergedAt,headRefName,state",
       "merged", Path(out) / "_pr_merged.json")
    gh("number,title,headRefName,state,statusCheckRollup,mergeStateStatus,mergeable",
       "open",   Path(out) / "_pr_open.json")

    subprocess.run(["git", "-C", src, "fetch", "--all", "--prune"],
                   capture_output=True, check=False)

    data = build_data(src, out)
    with open(Path(out) / "data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    html = build_html(data)
    with open(Path(out) / "dispatch.html", "w", encoding="utf-8") as f:
        f.write(html)
    with open(Path(out) / "index.html", "w", encoding="utf-8") as f:
        f.write(html)

    p = data["pipeline"]
    print("Pipeline stage counts:")
    for s in STAGES:
        print(f"  {STAGE_LABEL[s]:14s} {p[s]['count']:3d}  ({p[s]['loc']:>6} LOC, {p[s]['tests']:>3} tests)")
    h = data["headline"]
    print(f"Headline — LOC:{h['loc']}  awaiting-merge:{h['awaiting_merge']}  shipped:{h['shipped']}")


if __name__ == "__main__":
    main()
