# Troubleshooting

## Messages and their meaning

### `Token expired` / `out of sync` (code `10000`)

The server rejected `SK_TOKEN_CACHE_KEY` (response code `10000`). The script
refreshes it automatically via `SK_OAUTH_CRED_KEY`, so you normally never see
this. It surfaces only when the refresh path itself fails:

- **`refresh failed`** → `SK_OAUTH_CRED_KEY` has expired or changed: log out
  and back in on game.skport.com, then re-copy it (and the token) from DevTools.
- **`out of sync`** → the refresh succeeded but the request was rejected again:
  `SK_OAUTH_CRED_KEY` and `SK_TOKEN_CACHE_KEY` don't belong to the same
  session. Re-copy **both** from Local Storage (see [setup.md](setup.md#finding-your-keys-devtools)).

### `Check-in failed for ... HTTP Error 4xx`

- `403` / `401` → usually a wrong or expired credential (see above), or a wrong `gameId` / `server` combination
- `429` → rate-limited; the next scheduled run (6 h later) will retry automatically

### `Check-in failed for ... Network Error`

Timeout or connection issue reaching `zonai.skport.com`. The fail-closed flow means **nothing was claimed** — the next scheduled run retries. Nothing to do.

### `Check-in failed for ... Execution Error`

An unexpected client-side exception. Check the workflow log for the full traceback and open an issue.

### `Invalid JSON response from server.`

The API returned a non-JSON body (often a gateway error page). Retry on the next scheduled run.

### `[Profile N] Skip: Missing configuration credentials.`

The profile is missing `SK_OAUTH_CRED_KEY` or `gameId` (or they still contain `REPLACE_ME`). `SK_TOKEN_CACHE_KEY` is optional — the script refreshes it automatically. Fix the profile and re-run.

### `discord_notify is enabled but no webhook URL is configured; notifications disabled.`

`discordWebhook` / `DISCORD_WEBHOOK` is empty or still `REPLACE_ME`. Either set a real webhook URL or ignore the warning — the check-in itself still runs.

### `Configuration error: No profiles configured...`

The script couldn't find any profiles in `config.json` and/or `SKPORT_PROFILES_JSON` (with `--config-source env` the env var must be present; with `json` the file must contain `profiles`). Exit code is 1.

### `Configuration error: SKPORT_PROFILES_JSON is not valid JSON`

The secret isn't valid JSON. Validate it locally:

```bash
python -c "import json,sys; json.load(open('profiles.json')); print('OK')"
```

## GitHub Actions specific

### The run is red but Discord says everything succeeded

A failed profile makes the whole run exit 1 (that's deliberate — see [github-actions.md](github-actions.md#failure-behavior)). Check `run_status` in the last step's output; `LAST_RUN_STATUS` repository variable records it.

### Manual runs force by default

`workflow_dispatch` has `force: true` by default, so a manual run **bypasses** the same-day skip and re-checks the server. That's safe (fail-closed), but it will report "already checked in today" if the reward was already claimed.

### No Discord message at all

1. Is `DISCORD_WEBHOOK` set (not `REPLACE_ME`)?
2. Is `DISCORD_NOTIFY` set to `"false"` anywhere? (An unset secret defaults to `true` — but an explicitly empty secret in a workflow *without* the `|| 'true'` fallback would disable it. The shipped workflow includes the fallback.)
3. Did the run get skipped (`run_status: SKIPPED`)? The earlier successful run already sent the day's message.

### Timestamps / timezones

All dedup logic uses **UTC**. The schedule is UTC. "Today" for the same-day skip means the UTC calendar day.

## Still stuck?

Open an issue with:
- the failing step's log (redact secrets — the script already redacts the two credential values, but check for anything else)
- your `run_status` / `LAST_RUN_STATUS`
- whether a manual run reproduces the problem
