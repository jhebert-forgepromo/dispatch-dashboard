#!/usr/bin/env python3
"""Regenerate ForgeOS Dispatch dashboard from live git + gh state.

v4 — hierarchical Project -> Epic -> Feature -> Story -> Task tree.

- Auto-classifies every merged/open PR into an epic based on branch name,
  PR title, and conventional-commit scope.
- Rolls up progress %, feature counts, task counts at every level.
- Dashboard renders as expandable/collapsible tree with initiative filter
  persisted in localStorage.
- Runs standalone from cached PR JSON (no dependency on the Sales_Pipeline
  source repo). If --source is supplied AND readable, extra per-branch git
  stats (LOC, tests, commits ahead) are added.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ============================================================
#  Epic + Project classification
# ============================================================
#
# Each epic has an ordered list of (regex, source) rules. The FIRST match wins.
# `source` is what the rule inspects: "branch" or "title".
# Branch names are lowercased and compared verbatim, titles are lowercased.
#
# We keep the ordering carefully: rebrand & sage rules run before quote/etc
# so a "feat(sage): quote..." PR lands in sage-integration, not quote-lifecycle.
#
EPIC_RULES = [
    # ---- rebrand slice ----
    ("rebrand",           r"^rebrand[:(]"),                            # title
    # ---- SAGE integration ----
    ("sage-integration",  r"\bsage\b"),                                # branch/title
    # ---- CI + tooling + release ----
    ("ci-tooling",        r"(^|/|-)(ci|runner|gate-check|release-exe|forge-cli|pre-push|pr-triage|test-split|cost-model|ghas|sla-p2|rep-diagnostic)"),  # branch
    ("ci-tooling",        r"^(ci|tools?|chore|build|test|revert)[:(]"),  # title
    # ---- security / auth / SLA ----
    ("security",          r"(security|login|password|2fa|mfa|lockout|adr-02[13]|entra|sla-p6|sla-p7|audit-log|data-safety|team-roles|permission)"),  # branch/title
    # ---- SLA / artifact versioning family ----
    ("sla-artifacts",     r"(sla-p3|artifact-version|supersede|invoice-version|po-versioning|document-stamping|outlook-urgent)"),
    # ---- Reporting ----
    ("reporting",         r"(^|/)(rep|report|leaderboard|digest|kpi|health-check)"),  # branch
    ("reporting",         r"(leaderboard|kpi|digest|weekly)"),          # title
    # ---- Customer domain ----
    ("customer",          r"customer"),                                 # branch/title
    # ---- Line items + pricing ----
    ("line-items-pricing",r"(lineitem|line-item|pricing|pricebook|multi-currency|margin|payment-link|api-v1|outbound-webhook|supplier-po)"),
    # ---- UX / nav ----
    ("ux-nav",            r"^(ux|feat\(ux\)):"),                       # title
    ("ux-nav",            r"(ux|nav|kbd|shortcut|dark-mode|omnibox|typeahead|mobile|onboarding|recent-items|notifications-bell|feature-flags|session-timeout|inline-edit|drag-to-reorder|sortable|avatar|user-profile)"),  # branch/title
    # ---- Infrastructure / platform ----
    ("infra-platform",    r"(phase1|partition|multi-tenant|health|webhook|api-v1|feature-flags?|status-endpoint|db-backup|shipping-tracker|shipment|carrier|list-hygiene)"),
    # ---- Email / templates ----
    ("email-templates",   r"(email-template|email-templates|jinja)"),
    # ---- Quote lifecycle (must be near end so SAGE-tagged quote work goes to sage) ----
    ("quote-lifecycle",   r"quote"),
    # ---- Everything else ----
    ("planning-docs",     r"^docs[:(]"),                                # title
    ("planning-docs",     r"(plans-consolidated|ship-log|retro|branch-triage|adr-)"),
]

# Human-friendly labels per epic
EPIC_LABELS = {
    "quote-lifecycle":  "Quote Lifecycle",
    "customer":         "Customer",
    "ux-nav":           "UX & Navigation",
    "line-items-pricing":"Line Items & Pricing",
    "security":         "Security & Auth",
    "sla-artifacts":    "SLA & Artifact Versioning",
    "sage-integration": "SAGE Integration",
    "reporting":        "Reporting & KPIs",
    "ci-tooling":       "CI & Tooling",
    "infra-platform":   "Infrastructure & Platform",
    "email-templates":  "Email Templates",
    "rebrand":          "ForgeOS Rebrand",
    "planning-docs":    "Planning & Docs",
    "other":            "Uncategorized",
}

# Project -> list of epics (each epic belongs to exactly one project)
PROJECTS = {
    "ForgeOS Product": [
        "quote-lifecycle", "customer", "ux-nav", "line-items-pricing",
        "security", "sla-artifacts", "reporting", "email-templates",
        "infra-platform", "rebrand",
    ],
    "SAGE Integration": ["sage-integration"],
    "Dispatch Dashboard": [],  # populated from this repo's own commits
    "Ops & Delivery": ["ci-tooling", "planning-docs", "other"],
}

# Pipeline stages (delivery order left -> right)
STAGES = ["not-started", "in-progress", "tests-written", "pr-open",
          "pr-failing", "pr-behind", "merged"]
STAGE_LABEL = {
    "not-started":   "Not started",
    "in-progress":   "In progress",
    "tests-written": "Tests written",
    "pr-open":       "PR open",
    "pr-failing":    "PR failing",
    "pr-behind":     "PR behind",
    "merged":        "Merged",
}

REPO_SLUG = "Forge-Promo-LLC/ForgeOS"


# ============================================================
#  Helpers
# ============================================================
def run(cmd, cwd=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, shell=False)
    return r.stdout.strip()


def classify_epic(branch: str, title: str) -> str:
    """Return the epic slug for a PR based on branch + title."""
    hay_branch = (branch or "").lower()
    hay_title = (title or "").lower()
    for epic, pattern in EPIC_RULES:
        if re.search(pattern, hay_branch) or re.search(pattern, hay_title):
            return epic
    return "other"


def epic_to_project(epic: str) -> str:
    for proj, epics in PROJECTS.items():
        if epic in epics:
            return proj
    # If we can't find it, drop into Ops & Delivery
    return "Ops & Delivery"


# ---------- Feature extraction ----------
# We treat one branch as one "feature". A feature groups all PRs targeting the
# same branch (multiple retries etc). A feature that has multiple PRs becomes
# a Story with each PR as a Task; otherwise the feature IS its single task.

FEATURE_SLUG_RE = re.compile(
    r"^(?:local/|claude/|chore/|feature/)?"
    r"(?:feature-)?"
    r"(?P<core>[a-z0-9\-]+?)"
    r"(?:-20\d{2}-\d{2}-\d{2})?$",
    re.I,
)


def feature_slug(branch: str) -> str:
    """Normalize a branch to a feature slug (strip prefixes + trailing date)."""
    if not branch:
        return "(none)"
    b = branch.strip()
    # strip common prefixes
    for pref in ("local/", "claude/", "chore/", "feature/"):
        if b.startswith(pref):
            b = b[len(pref):]
            break
    if b.startswith("feature-"):
        b = b[len("feature-"):]
    # strip trailing YYYY-MM-DD or -N counter
    b = re.sub(r"-20\d{2}-\d{2}-\d{2}$", "", b)
    return b or branch


def feature_label(slug: str, title: str) -> str:
    """Human title for a feature. Prefer the PR title after 'type(scope): '."""
    if title:
        # Strip `type(scope):` prefix
        cleaned = re.sub(r"^\w+(?:\([^)]+\))?:\s*", "", title).strip()
        if cleaned:
            return cleaned
    return slug.replace("-", " ").title()


# ---------- PR utility ----------
def pr_check_state(pr):
    rollup = pr.get("statusCheckRollup") or []
    if not rollup:
        return "pending"
    for c in rollup:
        s = (c.get("state") or c.get("conclusion") or "").upper()
        if s in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED"):
            return "failing"
    for c in rollup:
        s = (c.get("state") or c.get("conclusion") or "").upper()
        if s in ("PENDING", "IN_PROGRESS", "QUEUED", ""):
            return "pending"
    return "green"


def pr_queued_count(pr):
    rollup = pr.get("statusCheckRollup") or []
    return sum(
        1
        for c in rollup
        if (c.get("state") or c.get("conclusion") or c.get("status") or "").upper() == "QUEUED"
    )


def pr_stage(pr, merged=False):
    if merged:
        return "merged"
    state = pr_check_state(pr)
    merge_state = (pr.get("mergeStateStatus") or "").upper()
    if state == "failing":
        return "pr-failing"
    if merge_state == "BEHIND":
        return "pr-behind"
    return "pr-open"


# ============================================================
#  Load PR data
# ============================================================
def load_prs(out_dir):
    merged, opens = [], []
    ok = True
    try:
        with open(Path(out_dir) / "_pr_merged.json", "r", encoding="utf-8") as f:
            merged = json.load(f)
        with open(Path(out_dir) / "_pr_open.json", "r", encoding="utf-8") as f:
            opens = json.load(f)
        if not merged and not opens:
            ok = False
    except Exception as e:
        print(f"[warn] PR load failed: {e}", file=sys.stderr)
        ok = False
    return merged, opens, ok


def refresh_pr_cache(out_dir):
    """Best-effort refresh via gh CLI. Silently keeps cache on failure."""
    def gh(fields, state, path):
        try:
            r = subprocess.run(
                ["gh", "pr", "list", "--state", state, "--limit", "500",
                 "--search", "sort:updated-desc",
                 "--json", fields, "--repo", REPO_SLUG],
                capture_output=True, text=True, check=False, timeout=60,
            )
            body = (r.stdout or "").strip()
            if body.startswith("["):
                json.loads(body)  # validate
                with open(path, "w", encoding="utf-8") as f:
                    f.write(body)
                print(f"[ok] refreshed {state} PR cache ({len(json.loads(body))} PRs)")
            else:
                print(f"[warn] gh {state} returned no JSON — keeping cache", file=sys.stderr)
        except Exception as e:
            print(f"[warn] gh {state} refresh failed: {e} — keeping cache", file=sys.stderr)

    gh("number,title,mergedAt,headRefName,state,url",
       "merged", Path(out_dir) / "_pr_merged.json")
    gh("number,title,headRefName,state,statusCheckRollup,mergeStateStatus,mergeable,url,updatedAt",
       "open", Path(out_dir) / "_pr_open.json")


# ============================================================
#  Build the hierarchy
# ============================================================
def build_features(merged_prs, opens_prs, dispatch_commits, since_iso):
    """Return {feature_slug: feature_dict} keyed by branch slug.

    A feature carries:
      - project, epic, slug, label
      - status (furthest pipeline stage across its PRs)
      - percent (100 if any PR merged, else based on stage)
      - tasks: [ {pr_number, title, status, url, merged_at} ]
    """
    features = {}

    def merged_after(pr):
        m = pr.get("mergedAt") or ""
        return m >= since_iso if m else False

    # Merged PRs
    for pr in merged_prs:
        if not merged_after(pr):
            continue
        branch = pr.get("headRefName", "")
        title = pr.get("title", "")
        slug = feature_slug(branch)
        epic = classify_epic(branch, title)
        proj = epic_to_project(epic)
        feat = features.setdefault(slug, {
            "slug": slug,
            "label": feature_label(slug, title),
            "branch": branch,
            "epic": epic,
            "project": proj,
            "tasks": [],
            "status": "not-started",
        })
        # Prefer the earliest-branch title (first-seen); replace if we get a longer real one
        if len(feature_label(slug, title)) > len(feat["label"]):
            feat["label"] = feature_label(slug, title)
        feat["tasks"].append({
            "pr": pr["number"],
            "title": title,
            "url": pr.get("url") or f"https://github.com/{REPO_SLUG}/pull/{pr['number']}",
            "status": "merged",
            "merged_at": pr.get("mergedAt"),
        })

    # Open PRs
    for pr in opens_prs:
        branch = pr.get("headRefName", "")
        title = pr.get("title", "")
        slug = feature_slug(branch)
        epic = classify_epic(branch, title)
        proj = epic_to_project(epic)
        feat = features.setdefault(slug, {
            "slug": slug,
            "label": feature_label(slug, title),
            "branch": branch,
            "epic": epic,
            "project": proj,
            "tasks": [],
            "status": "not-started",
        })
        stage = pr_stage(pr, merged=False)
        feat["tasks"].append({
            "pr": pr["number"],
            "title": title,
            "url": pr.get("url") or f"https://github.com/{REPO_SLUG}/pull/{pr['number']}",
            "status": stage,
            "updated_at": pr.get("updatedAt"),
        })

    # Dispatch Dashboard commits (this very repo)
    for c in dispatch_commits:
        slug = c["slug"]
        feat = features.setdefault(slug, {
            "slug": slug,
            "label": c["title"],
            "branch": "",
            "epic": "dispatch-dashboard-work",
            "project": "Dispatch Dashboard",
            "tasks": [],
            "status": "not-started",
        })
        feat["tasks"].append({
            "pr": None,
            "title": c["title"],
            "url": c.get("url"),
            "status": "merged",
            "merged_at": c["date"],
            "sha": c["sha"],
        })

    # Compute the furthest stage per feature (right-most wins)
    stage_rank = {s: i for i, s in enumerate(STAGES)}
    for feat in features.values():
        best = "not-started"
        for t in feat["tasks"]:
            if stage_rank.get(t["status"], 0) > stage_rank.get(best, 0):
                best = t["status"]
        feat["status"] = best
        feat["percent"] = 100 if best == "merged" else (
            80 if best in ("pr-open", "pr-behind") else
            60 if best == "pr-failing" else
            40 if best == "tests-written" else
            20 if best == "in-progress" else 0
        )

    return features


def dispatch_dashboard_commits(repo_dir, since_iso):
    """List `feat/`, `fix/`, `chore/`, `docs/` commits from this dashboard repo.
    Treated as one feature per commit. Purely additive.
    """
    if not (Path(repo_dir) / ".git").exists():
        return []
    out = run([
        "git", "-C", repo_dir, "log",
        f"--since={since_iso}",
        "--pretty=format:%H%x1f%s%x1f%cI",
    ])
    items = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 3:
            continue
        sha, title, date = parts
        # Skip auto-refresh commits
        if title.startswith("Dashboard refresh"):
            continue
        slug = "dispatch-" + re.sub(r"[^a-z0-9]+", "-", title.lower())[:40].strip("-")
        items.append({
            "sha": sha,
            "slug": slug,
            "title": title,
            "date": date,
            "url": f"https://github.com/jhebert-forgepromo/dispatch-dashboard/commit/{sha}",
        })
    return items


# ============================================================
#  Rollups (feature -> epic -> project)
# ============================================================
def rollup_group(features):
    """Return counts + percent for an iterable of features."""
    lst = list(features)
    total = len(lst)
    counts = {s: 0 for s in STAGES}
    task_total = 0
    task_done = 0
    for f in lst:
        counts[f["status"]] += 1
        for t in f["tasks"]:
            task_total += 1
            if t["status"] == "merged":
                task_done += 1
    features_done = counts["merged"]
    percent = round(features_done / total * 100) if total else 0
    return {
        "feature_total": total,
        "feature_done": features_done,
        "task_total": task_total,
        "task_done": task_done,
        "by_status": counts,
        "percent": percent,
    }


def build_data(out_dir, source_dir=None, since_iso="2026-08-27T00:00:00Z"):
    merged_prs, open_prs, pr_ok = load_prs(out_dir)
    dispatch = dispatch_dashboard_commits(out_dir, since_iso)

    features = build_features(merged_prs, open_prs, dispatch, since_iso)

    # Bucket by epic
    epics = {}
    for slug, f in features.items():
        epics.setdefault(f["epic"], []).append(f)

    # Bucket by project via epic mapping
    projects = {p: {"epics": {}, "features": []} for p in PROJECTS.keys()}
    # Ensure any dispatch-dashboard-work epic slides under Dispatch Dashboard
    for epic_slug, feats in epics.items():
        # If a dashboard-work epic exists, put it in Dispatch Dashboard project
        if epic_slug == "dispatch-dashboard-work":
            proj_name = "Dispatch Dashboard"
        else:
            proj_name = epic_to_project(epic_slug)
        projects.setdefault(proj_name, {"epics": {}, "features": []})
        projects[proj_name]["epics"][epic_slug] = feats
        projects[proj_name]["features"].extend(feats)

    # Build the nested tree the UI will consume
    tree = []
    for pname, pdata in projects.items():
        epic_list = []
        for epic_slug, feats in sorted(pdata["epics"].items()):
            # Sort features: in-progress first, merged last within same status? Actually put open work first.
            stage_rank = {s: i for i, s in enumerate(STAGES)}
            feats_sorted = sorted(feats, key=lambda f: (stage_rank[f["status"]], f["slug"]))
            feature_nodes = []
            for f in feats_sorted:
                # Determine if a feature is a "story" (multi-task) or single
                tasks = sorted(f["tasks"], key=lambda t: -(t["pr"] or 0))
                pct = f["percent"]
                feature_nodes.append({
                    "slug": f["slug"],
                    "label": f["label"],
                    "branch": f["branch"],
                    "status": f["status"],
                    "percent": pct,
                    "task_total": len(tasks),
                    "task_done": sum(1 for t in tasks if t["status"] == "merged"),
                    "tasks": tasks,
                })
            epic_list.append({
                "slug": epic_slug,
                "label": EPIC_LABELS.get(epic_slug, epic_slug.replace("-", " ").title()),
                **rollup_group(feats_sorted),
                "features": feature_nodes,
            })
        # Sort epics by feature_total desc so busiest at top
        epic_list.sort(key=lambda e: -e["feature_total"])
        proj_rollup = rollup_group(pdata["features"])
        tree.append({
            "name": pname,
            **proj_rollup,
            "epics": epic_list,
        })

    # Pipeline (feature-level) totals
    pipeline = {s: {"count": 0, "slugs": []} for s in STAGES}
    for f in features.values():
        pipeline[f["status"]]["count"] += 1
        pipeline[f["status"]]["slugs"].append(f["slug"])

    total_features = len(features)
    total_tasks = sum(len(f["tasks"]) for f in features.values())
    merged_tasks = sum(1 for f in features.values() for t in f["tasks"] if t["status"] == "merged")
    open_tasks = total_tasks - merged_tasks

    # Bottlenecks (live from open PRs)
    ci_queued = sum(pr_queued_count(pr) for pr in open_prs)
    ci_failing = sum(1 for pr in open_prs if pr_check_state(pr) == "failing")
    behind = sum(1 for pr in open_prs if (pr.get("mergeStateStatus") or "").upper() == "BEHIND")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "since": since_iso,
        "pr_data_available": pr_ok,
        "counts": {
            "features": total_features,
            "tasks": total_tasks,
            "tasks_merged": merged_tasks,
            "tasks_open": open_tasks,
            "prs_merged": len(merged_prs),
            "prs_open": len(open_prs),
        },
        "pipeline": pipeline,
        "bottleneck": {
            "ci_queued": ci_queued,
            "ci_failing": ci_failing,
            "behind": behind,
            "pr_data_available": pr_ok,
        },
        "projects": tree,
    }


# ============================================================
#  HTML (warm-cream aesthetic, tree UI with initiative filter)
# ============================================================
HTML_TEMPLATE = r"""<!doctype html>
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
  --stage-notstarted:#e5e2d8; --stage-inprogress:#d9cfae;
  --stage-tests:#f0d3a6; --stage-propen:#e6b47c;
  --stage-prfail:#e78f6d; --stage-prbehind:#dfb26a; --stage-merged:#c86a2c;
}
* { box-sizing:border-box; }
body {
  margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;
  padding:24px 20px 96px;
}
h1 {
  margin:0 0 4px; font-size:28px; color:var(--accent); letter-spacing:-0.01em;
}
h2 { font-size:18px; margin:22px 0 10px; color:var(--ink); }
.subtitle { color:var(--muted); font-size:13px; margin-bottom:22px; }
code { background:#f1ead6; padding:1px 5px; border-radius:4px; font-size:12px; }

/* ---------- Filter row ---------- */
.filter {
  background:var(--card); border:1px solid var(--border); border-radius:12px;
  padding:12px 16px; margin-bottom:22px; display:flex; flex-wrap:wrap;
  gap:12px; align-items:center;
}
.filter .f-label {
  font-size:11px; text-transform:uppercase; letter-spacing:.05em;
  color:var(--muted); font-weight:700;
}
.filter label {
  display:inline-flex; align-items:center; gap:6px; padding:5px 12px;
  border-radius:999px; background:#f1ead6; color:#5a4523; cursor:pointer;
  font-size:13px; font-weight:600; user-select:none;
  border:1px solid transparent;
}
.filter label:hover { border-color:var(--accent-soft); }
.filter input[type=checkbox] { margin:0; accent-color:var(--accent); cursor:pointer; }
.filter .actions { margin-left:auto; display:flex; gap:8px; }
.filter button {
  background:transparent; border:1px solid var(--border); border-radius:6px;
  padding:4px 10px; color:var(--muted); font-size:12px; cursor:pointer;
}
.filter button:hover { border-color:var(--accent); color:var(--accent); }

/* ---------- Stat cards ---------- */
.stats {
  display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin-bottom:22px;
}
.stat {
  background:var(--card); border:1px solid var(--border); border-radius:12px;
  padding:16px 20px;
}
.stat .stat-label {
  font-size:11px; color:var(--muted); text-transform:uppercase;
  letter-spacing:.05em; font-weight:600;
}
.stat .stat-big {
  font-size:34px; font-weight:800; color:var(--accent); line-height:1.05;
  margin-top:4px;
}
.stat .stat-sub { color:var(--muted); font-size:12px; margin-top:6px; }

/* ---------- Pipeline ---------- */
.pipeline {
  display:grid; grid-template-columns:repeat(7,1fr); gap:8px; margin-bottom:22px;
}
.stage {
  background:var(--card); border:1px solid var(--border); border-radius:10px;
  padding:12px 10px 10px; position:relative;
}
.stage-bar {
  position:absolute; top:0; left:0; right:0; height:4px; border-radius:10px 10px 0 0;
}
.stage.notstarted .stage-bar { background:var(--stage-notstarted); }
.stage.inprogress .stage-bar { background:var(--stage-inprogress); }
.stage.tests      .stage-bar { background:var(--stage-tests); }
.stage.propen     .stage-bar { background:var(--stage-propen); }
.stage.prfail     .stage-bar { background:var(--stage-prfail); }
.stage.prbehind   .stage-bar { background:var(--stage-prbehind); }
.stage.merged     .stage-bar { background:var(--stage-merged); }
.stage-title {
  font-size:10px; letter-spacing:.05em; text-transform:uppercase;
  color:var(--muted); font-weight:700;
}
.stage-count {
  font-size:26px; font-weight:800; line-height:1; margin-top:4px;
}

/* ---------- Bottleneck ---------- */
.bottleneck {
  background:var(--card); border:1px solid var(--border); border-radius:12px;
  padding:10px 16px; margin-bottom:22px; display:flex; flex-wrap:wrap;
  gap:12px; align-items:center; font-size:13px;
}
.bottleneck .b-label {
  font-size:11px; text-transform:uppercase; letter-spacing:.05em;
  color:var(--muted); font-weight:700;
}
.bottleneck .b-chip {
  padding:4px 10px; border-radius:999px; background:var(--accent-soft);
  color:#7c3d15; font-size:12px; font-weight:600;
}
.bottleneck .b-chip.red { background:#f7d4c8; color:#7d1d12; }
.bottleneck .b-chip.amber { background:#f2dcae; color:#7a4c11; }
.bottleneck .b-chip.calm { background:#dfe9d8; color:#2d5027; }

/* ---------- Tree ---------- */
.tree { }
details {
  background:var(--card); border:1px solid var(--border); border-radius:10px;
  margin-bottom:8px; overflow:hidden;
}
details[open] > summary { border-bottom:1px solid var(--border); }
details.project { border-color:var(--accent-soft); }
details.project > summary {
  background:linear-gradient(90deg,#fdf3e6,#fffdf7 60%);
  padding:14px 16px; font-size:16px; font-weight:700;
}
details.epic { margin:6px 12px; }
details.epic > summary {
  padding:9px 14px; font-size:14px; font-weight:600; background:#fbf4e0;
}
details.feature { margin:4px 24px; border-color:#eadfc4; }
details.feature > summary {
  padding:7px 12px; font-size:13px; background:#fffdf7;
}
details.feature:not([open]) > summary:not(.no-children)::marker { color:var(--accent); }
summary {
  cursor:pointer; list-style:none; display:flex; align-items:center; gap:10px;
  user-select:none;
}
summary::-webkit-details-marker { display:none; }
summary::before {
  content:"▸"; display:inline-block; width:14px; color:var(--muted);
  transition:transform .12s ease; flex-shrink:0;
}
details[open] > summary::before { transform:rotate(90deg); }
summary.leaf::before { content:"•"; color:var(--accent); }
.node-name { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.node-name .slug { color:var(--muted); font-size:11px; font-variant-numeric:tabular-nums; margin-left:4px; }
.node-meta { display:flex; gap:10px; align-items:center; font-size:12px; color:var(--muted); flex-shrink:0; }
.progress-bar {
  width:120px; height:8px; background:#f1ead6; border-radius:99px; overflow:hidden;
  flex-shrink:0;
}
.progress-bar i {
  display:block; height:100%; background:linear-gradient(90deg,var(--accent),#b8501e);
  transition:width .4s ease;
}
.pct { min-width:38px; text-align:right; font-weight:700; color:var(--ink); font-variant-numeric:tabular-nums; }
.counts { color:var(--muted); font-size:11px; font-variant-numeric:tabular-nums; }
.pill {
  display:inline-block; padding:1px 8px; border-radius:99px; font-size:10px;
  font-weight:700; text-transform:uppercase; letter-spacing:.03em; color:#fff;
  flex-shrink:0;
}
.pill.not-started  { background:#8f8676; }
.pill.in-progress  { background:#a68a4b; }
.pill.tests-written{ background:#c69a55; }
.pill.pr-open      { background:#3c8763; }
.pill.pr-failing   { background:#b0442a; }
.pill.pr-behind    { background:#a67623; }
.pill.merged       { background:#c86a2c; }

/* Task rows (leaves) */
.task-list { padding:6px 20px 10px 40px; }
.task-row {
  display:flex; align-items:center; gap:10px; padding:5px 0;
  border-bottom:1px dashed #eee1c8;
  font-size:12px;
}
.task-row:last-child { border-bottom:none; }
.task-row a { color:var(--blue); text-decoration:none; font-weight:600; }
.task-row a:hover { text-decoration:underline; }
.task-row .task-title {
  flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
  color:var(--ink);
}
.task-row .task-date { color:var(--muted); font-variant-numeric:tabular-nums; }

/* Toolbar buttons */
.toolbar { display:flex; gap:8px; margin-bottom:10px; }
.toolbar button {
  background:var(--card); border:1px solid var(--border); border-radius:6px;
  padding:5px 12px; color:var(--muted); font-size:12px; cursor:pointer;
}
.toolbar button:hover { border-color:var(--accent); color:var(--accent); }

.hidden { display:none !important; }
.empty {
  padding:14px; color:var(--muted); font-style:italic; text-align:center;
  border:1px dashed var(--border); border-radius:10px;
}

@media (max-width:900px) {
  .stats { grid-template-columns:repeat(2,1fr); }
  .pipeline { grid-template-columns:repeat(4,1fr); }
  .progress-bar { width:60px; }
}
@media (max-width:600px) {
  .pipeline { grid-template-columns:repeat(2,1fr); }
  .node-meta { flex-wrap:wrap; gap:6px; }
  .progress-bar { display:none; }
}
</style>
</head>
<body>
<h1>ForgeOS Progress</h1>
<div class="subtitle" id="updated"></div>

<div class="filter" id="filter"></div>
<div class="stats" id="stats"></div>
<div class="pipeline" id="pipeline"></div>
<div class="bottleneck" id="bottleneck"></div>

<h2>Initiatives</h2>
<div class="toolbar">
  <button id="expandAll">Expand all</button>
  <button id="collapseAll">Collapse all</button>
  <button id="openInProgress">Open in-progress only</button>
</div>
<div class="tree" id="tree"></div>

<script>
const DATA = __DATA__;
const STAGES = ["not-started","in-progress","tests-written","pr-open","pr-failing","pr-behind","merged"];
const STAGE_LABEL = {
  "not-started":"Not started","in-progress":"In progress","tests-written":"Tests written",
  "pr-open":"PR open","pr-failing":"PR failing","pr-behind":"PR behind","merged":"Merged"
};
const STAGE_CLASS = {
  "not-started":"notstarted","in-progress":"inprogress","tests-written":"tests",
  "pr-open":"propen","pr-failing":"prfail","pr-behind":"prbehind","merged":"merged"
};

const LS_KEY = "forgeos_filter_v1";
const activeProjects = new Set(loadFilter());

function loadFilter(){
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (raw) return JSON.parse(raw);
  } catch(_){}
  return DATA.projects.map(p => p.name);
}
function saveFilter(){
  try { localStorage.setItem(LS_KEY, JSON.stringify([...activeProjects])); } catch(_){}
}
function fmt(n){ return (n||0).toLocaleString(); }
function shortDate(iso){
  if (!iso) return "";
  return iso.slice(0,10);
}
function esc(s){
  return String(s||"").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
}

function renderFilter(){
  const el = document.getElementById('filter');
  const chips = DATA.projects.map(p => {
    const checked = activeProjects.has(p.name) ? "checked" : "";
    return `<label><input type="checkbox" data-proj="${esc(p.name)}" ${checked}> ${esc(p.name)} <span class="counts">${p.feature_done}/${p.feature_total}</span></label>`;
  }).join('');
  el.innerHTML = `<span class="f-label">Initiatives</span>${chips}
    <div class="actions">
      <button id="fAll">All</button><button id="fNone">None</button>
    </div>`;
  el.querySelectorAll('input[type=checkbox]').forEach(cb => {
    cb.addEventListener('change', () => {
      const name = cb.dataset.proj;
      if (cb.checked) activeProjects.add(name); else activeProjects.delete(name);
      saveFilter();
      applyFilter();
    });
  });
  document.getElementById('fAll').addEventListener('click', () => {
    DATA.projects.forEach(p => activeProjects.add(p.name));
    saveFilter(); renderFilter(); applyFilter();
  });
  document.getElementById('fNone').addEventListener('click', () => {
    activeProjects.clear(); saveFilter(); renderFilter(); applyFilter();
  });
}

function renderStats(){
  const c = DATA.counts;
  document.getElementById('stats').innerHTML = `
    <div class="stat">
      <div class="stat-label">Features tracked</div>
      <div class="stat-big">${fmt(c.features)}</div>
      <div class="stat-sub">across ${DATA.projects.length} initiatives since ${DATA.since.slice(0,10)}</div>
    </div>
    <div class="stat">
      <div class="stat-label">Tasks / PRs</div>
      <div class="stat-big">${fmt(c.tasks_merged)}<span style="font-size:20px;color:var(--muted)">/${fmt(c.tasks)}</span></div>
      <div class="stat-sub">${fmt(c.tasks_open)} still open · ${fmt(c.prs_merged)} merged PRs</div>
    </div>
    <div class="stat">
      <div class="stat-label">Awaiting merge</div>
      <div class="stat-big">${fmt(c.prs_open)}</div>
      <div class="stat-sub">open PRs across all initiatives</div>
    </div>
    <div class="stat">
      <div class="stat-label">Shipped features</div>
      <div class="stat-big">${fmt((DATA.pipeline.merged||{}).count||0)}</div>
      <div class="stat-sub">at least one PR merged to main</div>
    </div>`;
}

function renderPipeline(){
  const p = DATA.pipeline || {};
  document.getElementById('pipeline').innerHTML = STAGES.map(s => {
    const d = p[s] || {count:0};
    return `<div class="stage ${STAGE_CLASS[s]}">
      <div class="stage-bar"></div>
      <div class="stage-title">${STAGE_LABEL[s]}</div>
      <div class="stage-count">${d.count}</div>
    </div>`;
  }).join('');
}

function renderBottleneck(){
  const b = DATA.bottleneck || {};
  const chips = [];
  if (!b.pr_data_available){
    chips.push('<span class="b-chip amber">PR data unavailable — run <code>gh auth login</code></span>');
  } else {
    if (b.ci_queued>0) chips.push(`<span class="b-chip amber">${b.ci_queued} CI check${b.ci_queued===1?"":"s"} queued</span>`);
    if (b.ci_failing>0) chips.push(`<span class="b-chip red">${b.ci_failing} failing CI</span>`);
    if (b.behind>0) chips.push(`<span class="b-chip amber">${b.behind} BEHIND main</span>`);
    if (chips.length===0) chips.push('<span class="b-chip calm">All open PRs green &amp; up-to-date</span>');
  }
  document.getElementById('bottleneck').innerHTML = '<span class="b-label">Bottlenecks</span>' + chips.join('');
}

function pill(status){ return `<span class="pill ${status}">${status}</span>`; }

function progress(pct){
  return `<div class="progress-bar"><i style="width:${pct}%"></i></div><span class="pct">${pct}%</span>`;
}

function renderFeatureTasks(feat){
  return feat.tasks.map(t => {
    const link = t.pr ? `<a href="${esc(t.url)}" target="_blank" rel="noopener">#${t.pr}</a>` :
                        `<a href="${esc(t.url||'#')}" target="_blank" rel="noopener">${(t.sha||'').slice(0,7)}</a>`;
    return `<div class="task-row">${pill(t.status)}${link}
      <span class="task-title">${esc(t.title)}</span>
      <span class="task-date">${shortDate(t.merged_at || t.updated_at || '')}</span>
    </div>`;
  }).join('');
}

function renderFeature(feat){
  const isMulti = feat.tasks.length > 1;
  const summaryClass = isMulti ? "" : "leaf";
  const meta = `<span class="node-meta">${pill(feat.status)}<span class="counts">${feat.task_done}/${feat.task_total} tasks</span>${progress(feat.percent)}</span>`;
  const body = isMulti
    ? `<div class="task-list">${renderFeatureTasks(feat)}</div>`
    : `<div class="task-list">${renderFeatureTasks(feat)}</div>`;
  return `<details class="feature" data-status="${feat.status}">
    <summary class="${summaryClass}">
      <span class="node-name">${esc(feat.label)}<span class="slug"> · ${esc(feat.slug)}</span></span>
      ${meta}
    </summary>
    ${body}
  </details>`;
}

function renderEpic(epic){
  const meta = `<span class="node-meta"><span class="counts">${epic.feature_done}/${epic.feature_total} features · ${epic.task_done}/${epic.task_total} tasks</span>${progress(epic.percent)}</span>`;
  const body = epic.features.map(renderFeature).join('') || '<div class="empty">no features</div>';
  return `<details class="epic" data-epic="${esc(epic.slug)}">
    <summary>
      <span class="node-name">${esc(epic.label)}<span class="slug"> · ${esc(epic.slug)}</span></span>
      ${meta}
    </summary>
    ${body}
  </details>`;
}

function renderProject(proj){
  const meta = `<span class="node-meta"><span class="counts">${proj.feature_done}/${proj.feature_total} features · ${proj.task_done}/${proj.task_total} tasks</span>${progress(proj.percent)}</span>`;
  const body = proj.epics.length
    ? proj.epics.map(renderEpic).join('')
    : '<div class="empty" style="margin:12px">No epics in this initiative yet.</div>';
  return `<details class="project" data-project="${esc(proj.name)}" open>
    <summary>
      <span class="node-name">${esc(proj.name)}</span>
      ${meta}
    </summary>
    ${body}
  </details>`;
}

function renderTree(){
  document.getElementById('tree').innerHTML =
    DATA.projects.map(renderProject).join('');
  applyFilter();
}

function applyFilter(){
  document.querySelectorAll('details.project').forEach(el => {
    const name = el.dataset.project;
    if (activeProjects.has(name)) el.classList.remove('hidden');
    else el.classList.add('hidden');
  });
}

function render(){
  document.getElementById('updated').textContent =
    'Last updated: ' + new Date(DATA.generated_at).toLocaleString() +
    (DATA.pr_data_available ? '' : ' — PR data unavailable');
  renderFilter();
  renderStats();
  renderPipeline();
  renderBottleneck();
  renderTree();

  document.getElementById('expandAll').addEventListener('click', () => {
    document.querySelectorAll('details').forEach(d => d.open = true);
  });
  document.getElementById('collapseAll').addEventListener('click', () => {
    document.querySelectorAll('details.feature, details.epic').forEach(d => d.open = false);
  });
  document.getElementById('openInProgress').addEventListener('click', () => {
    document.querySelectorAll('details.feature').forEach(d => {
      const st = d.dataset.status;
      d.open = (st !== 'merged' && st !== 'not-started');
    });
    document.querySelectorAll('details.epic').forEach(d => {
      // Open epic if any of its features is open
      const anyOpen = [...d.querySelectorAll('details.feature')].some(x => x.open);
      d.open = anyOpen;
    });
  });
}
render();
</script>
</body>
</html>
"""


def build_html(data):
    return HTML_TEMPLATE.replace("__DATA__", json.dumps(data))


# ============================================================
#  Main
# ============================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=None, help="Optional Sales_Pipeline clone for git enrichment")
    ap.add_argument("--out", required=True, help="Path to dispatch-dashboard clone (output)")
    ap.add_argument("--since", default="2026-08-27T00:00:00Z", help="Cutoff for merged PR inclusion")
    ap.add_argument("--skip-gh", action="store_true", help="Do not refresh PR cache via gh CLI")
    args = ap.parse_args()

    out = args.out
    os.makedirs(out, exist_ok=True)

    if not args.skip_gh:
        refresh_pr_cache(out)

    data = build_data(out, source_dir=args.source, since_iso=args.since)

    with open(Path(out) / "data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    html = build_html(data)
    for name in ("dispatch.html", "index.html"):
        with open(Path(out) / name, "w", encoding="utf-8") as f:
            f.write(html)

    # Summary output
    print(f"\nFeatures tracked: {data['counts']['features']}")
    print(f"Tasks tracked:    {data['counts']['tasks']}  ({data['counts']['tasks_merged']} merged)")
    print(f"Merged PRs:       {data['counts']['prs_merged']}")
    print(f"Open PRs:         {data['counts']['prs_open']}")
    print("\nPer-initiative:")
    for p in data["projects"]:
        print(f"  {p['name']:22s} {p['percent']:3d}%  ({p['feature_done']}/{p['feature_total']} features, {p['task_done']}/{p['task_total']} tasks)")
    print("\nPipeline:")
    for s in STAGES:
        print(f"  {STAGE_LABEL[s]:14s} {data['pipeline'][s]['count']:3d}")


if __name__ == "__main__":
    main()
