# ForgeOS Dispatch Dashboard (working clone)

Published to https://jhebert-forgepromo.github.io/dispatch-dashboard/dispatch.html

## Files

| File                        | Purpose                                                                 |
|-----------------------------|-------------------------------------------------------------------------|
| `regenerate.py`             | Renders `dispatch.html` / `data.json` from `Sales_Pipeline` git state.  |
| `regenerate.ps1`            | Wrapper: fetch, run python, git add/commit/push.                        |
| `freshness-check.ps1`       | Watchdog: alerts when `dispatch.html` is older than 20 min.             |
| `install-scheduled-tasks.ps1` | Idempotently (re)registers both scheduled tasks on host provisioning. |

## Refresh model

1. **Primary** — post-merge git hook on the `Sales_Pipeline` clone runs `regenerate.ps1`.
2. **Secondary** — Scheduled task `ForgeOS-Dashboard-Refresh` every 15 min (safety net only).
3. **Watchdog** — Scheduled task `ForgeOS-Dashboard-Freshness-Check` every 5 min.

Full runbook: `forgeos-consolidate/docs/runbooks/DISPATCH_DASHBOARD_REFRESH.md`.

## Bootstrap on a fresh host

```powershell
pwsh .\install-scheduled-tasks.ps1
```

To remove:

```powershell
pwsh .\install-scheduled-tasks.ps1 -RemoveOnly
```
