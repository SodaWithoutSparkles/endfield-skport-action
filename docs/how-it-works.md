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
         │    └─ _generate_headers() + _request_json()
         ├─ (skip if already signed in today)
         └─ do_checkin()                # POST check-in
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

## The two-step flow (fail-closed)

1. **GET** the attendance calendar → the server returns `data.hasToday` and the list of claimed days.
2. If `hasToday` is true → **skip the POST**, report "already checked in" (success).
3. Otherwise **POST** the check-in → success is `message == "OK"`.

**Fail-closed:** if the GET fails for *any* reason (network, server error, token expiry), the script **aborts without POSTing** — mimicking normal user behavior. A user who can't load the page also can't claim twice. Combined with the 4×/day workflow schedule, a transient failure just gets retried a few hours later, and the server-side check guarantees a retry can never double-claim.

## Response codes

| Code          | Meaning                                                                         |
| ------------- | ------------------------------------------------------------------------------- |
| `10000`       | Token expired — update `SK_TOKEN_CACHE_KEY`                                     |
| anything else | Falls back to the server's `message` / `msg` text (expected: `"OK"` on success) |

Known codes are mapped in the `KNOWN_CODES` dict.

## Exit codes

| Code | Meaning                                                                        |
| ---- | ------------------------------------------------------------------------------ |
| `0`  | All profiles succeeded (or same-day skip)                                      |
| `1`  | Any profile failed, or configuration error (missing profiles / malformed JSON) |

`sys.exit(1)` is what makes cron and CI treat a failed day as failed.

## Config loading

`load_config(source)` merges two failable loaders:

- `_load_config_from_env()` — reads `SKPORT_PROFILES_JSON`, `DISCORD_NOTIFY`, `MY_DISCORD_ID`, `DISCORD_WEBHOOK`, `LAST_SIGNIN_DATE`; raises on malformed JSON
- `_load_config_from_json()` — reads `config.json` (path resolved relative to the script, not the working directory); a missing file yields empty values, malformed content raises

Merging: **env wins** for any key it actually sets. Missing profiles → `ValueError` → exit 1.

## Security

- `_sanitize()` replaces `SK_OAUTH_CRED_KEY` / `SK_TOKEN_CACHE_KEY` values with `[REDACTED]` in every message before it reaches logs or Discord
- `config.json` is git-ignored
- `REPLACE_ME` placeholders are treated as unset (webhooks, Discord IDs, credentials)
