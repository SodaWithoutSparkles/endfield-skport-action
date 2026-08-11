# GitHub Actions Workflow

The repository ships with `.github/workflows/checkin.yaml`. This page explains what it does and how to adjust it.

## Schedule

```yaml
on:
  schedule:
    - cron: '0 0,6,12,18 * * *'
```

The workflow runs **4 times per day at 00:00, 06:00, 12:00, 18:00 UTC**. That's intentional:

- the first successful run of the day claims the reward
- if a run fails (network hiccup, server hiccup), the next run **retries** — the fail-closed status check guarantees nothing is signed twice, so retries are always safe

## Step-by-step

| Step                          | What it does                                                                                                          |
| ----------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `Checkout Code`               | Clones the repo                                                                                                       |
| `Set up Python`               | Installs Python 3.11 (stdlib only, nothing else)                                                                      |
| `Run Auto Sign Script`        | Runs `python main.py --config-source env [--force]` with secrets as env vars; writes step **outputs**                 |
| `Update Repository Variables` | Persists run results into repo **variables** (`gh variable set`) so future runs remember them — no git commits needed |

### Manual runs (`workflow_dispatch`)

```yaml
workflow_dispatch:
  inputs:
    force:
      description: 'Force execution (ignore same-day check-in restriction)'
      type: boolean
      default: true
```

- Triggered from **Actions → Skport Daily Auto Check-in → Run workflow**
- `force` defaults to `true` → the run bypasses the same-day `LAST_SIGNIN_DATE` skip (`--force` flag)

## Outputs

The script writes these outputs (via `GITHUB_OUTPUT`):

| Output       | Meaning                                                            |
| ------------ | ------------------------------------------------------------------ |
| `executed`   | `true` if profiles were processed, `false` if skipped/config error |
| `today_date` | Current UTC date (`YYYY-MM-DD`)                                    |
| `run_status` | `SUCCESS` · `FAILED` · `SKIPPED` · `CONFIG_ERROR`                  |

## Variables

The workflow uses repository **variables** (Settings → Secrets and variables → Actions → Variables) as its "database":

| Variable           | Written when                      | Purpose                                   |
| ------------------ | --------------------------------- | ----------------------------------------- |
| `LAST_SIGNIN_DATE` | Only when `run_status == SUCCESS` | Same-day dedup across scheduled runs      |
| `LAST_RUN_STATUS`  | Every executed run                | Last known outcome (`SUCCESS` / `FAILED`) |

### Why `LAST_SIGNIN_DATE` is only written on SUCCESS

If some profiles fail at 00:00, the script exits non-zero and the workflow does **not** record today's date. The 06:00 run therefore retries the failed profiles instead of skipping the day. This is what makes the 4×/day schedule a real retry mechanism.

### Same-day skip

At the start of a run, the script compares `LAST_SIGNIN_DATE` (passed via `vars.LAST_SIGNIN_DATE`) with the current UTC date:

- **match** → log "Already completed sign-in for today", write `executed=false` / `run_status=SKIPPED`, exit 0 (Discord is not notified — the successful run earlier already sent one)
- **no match** → run normally

## Failure behavior

| Situation                             | Script exit code | Workflow effect                                                                   |
| ------------------------------------- | ---------------- | --------------------------------------------------------------------------------- |
| All profiles succeeded                | `0`              | Variables updated, run green                                                      |
| Any profile failed                    | `1`              | Variables updated (`LAST_RUN_STATUS=FAILED`), run red, next scheduled run retries |
| Config error (no profiles / bad JSON) | `1`              | `run_status=CONFIG_ERROR`, no variables written                                   |
| Same-day skip                         | `0`              | `run_status=SKIPPED`, no variables written                                        |

**Note:** a failed run shows red in the Actions tab on purpose — that's the signal to look at the logs or the Discord message.
