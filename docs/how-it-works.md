# How It Works

A technical walkthrough of `main.py` — useful if you want to trust (or modify) the automation.

## Architecture

```
main()
 ├─ load_config(source)          # env / json / both, env wins
 ├─ same-day skip check          # LAST_SIGNIN_DATE == today → exit 0 (Actions only)
 └─ for each profile:
     └─ checkin_flow(profile)
         ├─ check_attendence_status()   # GET attendance calendar
         │    └─ _request_with_refresh()  # 401 → refresh token once, retry
         │         └─ refresh_cache_token() + persist_refreshed_token()
         ├─ (skip if already signed in today)
         └─ do_checkin()                # POST check-in (same 401 handler)
              └─ _generate_headers() + _request_json()
```

All HTTP, signing, and JSON parsing lives in one place: `_request_json()`.

## The endpoint

|              |                                                                     |
| ------------ | ------------------------------------------------------------------- |
| Host         | `https://zonai.skport.com`                                          |
| Path         | `/web/v1/game/endfield/attendance`                                  |
| Methods      | `GET` (status) and `POST` (check-in)                                |
| Content-Type | `application/json`                                                  |
| Empty body   | The reference script signs and sends an **empty body** for the POST |

## The signature

Each request carries a `sign` header computed as an **HMAC-SHA256 → MD5 double hash** over a canonical string:

```
path + (query for GET | body for POST) + timestamp + {"platform":..,"timestamp":..,"dId":..,"vName":..}
```

```python
hmac_bytes = hmac.new(token, string_to_sign, hashlib.sha256).digest()
sign       = hashlib.md5(hmac_bytes.hex().encode()).hexdigest()
```

The token is `SK_TOKEN_CACHE_KEY`; the timestamp must be fresh (Unix seconds), which is why the script regenerates headers per request.

## The check-in flow (refresh-on-401, fail-closed)

`SK_TOKEN_CACHE_KEY` is assumed valid. Refreshing it is strictly a **401
handler** inside `_request_with_refresh()`:

1. **GET** the attendance calendar with the current token → the server returns
   `data.hasToday` and the list of claimed days. If `hasToday` is true →
   **skip the POST**, report "already checked in" (success).
2. If the server rejects the token (**HTTP 401** or code `10000`) → refresh
   `SK_TOKEN_CACHE_KEY` via `SK_OAUTH_CRED_KEY` (the durable credential) and
   **retry the request once** with the fresh token. The fresh token is written
   back to `config.json` (when present) and reused by the POST.
3. Otherwise **POST** the check-in → success is `message == "OK"`.

A **failed refresh** means `SK_OAUTH_CRED_KEY` has expired or changed: the
profile fails and the message tells you to re-copy both keys from DevTools. A
refresh that succeeds but still gets rejected means the two keys are out of
sync — same advice.

**Fail-closed:** if the GET fails for *any* reason (network, server error,
out-of-sync keys), the script **aborts without POSTing** — mimicking normal
user behavior. A user who can't load the page also can't claim twice. Combined
with the 4×/day workflow schedule, a transient failure just gets retried a few
hours later, and the server-side check guarantees a retry can never double-claim.

## Response codes

| Code          | Meaning                                                                                                     |
| ------------- | ----------------------------------------------------------------------------------------------------------- |
| `10000`       | Token expired — the script refreshes automatically; if it recurs after a refresh, both keys are out of sync |
| anything else | Falls back to the server's `message` / `msg` text (expected: `"OK"` on success)                             |

Known codes are mapped in the `KNOWN_CODES` dict.

## Exit codes

| Code | Meaning                                                                                                                |
| ---- | ---------------------------------------------------------------------------------------------------------------------- |
| `0`  | All profiles succeeded (or same-day skip); profiles missing credentials are skipped, not failed                        |
| `1`  | Any profile failed (including an expired oauth credential), or configuration error (missing profiles / malformed JSON) |

`sys.exit(1)` is what makes cron and CI treat a failed day as failed.

## Config loading

`load_config(source)` merges two failable loaders:

- `_load_config_from_env()` — reads `SKPORT_PROFILES_JSON`, `DISCORD_NOTIFY`, `MY_DISCORD_ID`, `DISCORD_WEBHOOK`, `LAST_SIGNIN_DATE`; raises on malformed JSON
- `_load_config_from_json()` — reads `config.json` (path resolved relative to the script, not the working directory); a missing file yields empty values, malformed content raises

Merging: **env wins** for any key it actually sets. Missing profiles → `ValueError` → exit 1.

## Security

- `_sanitize()` replaces `SK_OAUTH_CRED_KEY` / `SK_TOKEN_CACHE_KEY` values with `[REDACTED]` in every message before it reaches logs or Discord
- The Discord webhook URL (which carries a secret token) is redacted from webhook error logs
- `config.json` is git-ignored
- `REPLACE_ME` placeholders are treated as unset (webhooks, Discord IDs, credentials)
