# Setup Guide

## Prerequisites

- Python **3.10+** (no third-party packages needed)
- A browser where you're logged in to [game.skport.com](https://game.skport.com) with the account(s) you want to automate
- *(Optional)* A [Discord webhook](https://support.discord.com/hc/en-us/articles/228383668-Intro-to-Webhooks) URL

---

## Finding your keys (DevTools)

The skport website stores the credentials the API needs in the browser's **Local Storage**. These values are what the script uses to authenticate.

### 1. Log in

Open <https://game.skport.com> and log in with the account you want to automate.

### 2. Open Local Storage

- **Firefox:** press **F12** → **Storage** tab → **Local Storage** → `https://game.skport.com`
- **Chrome/Edge:** press **F12** → **Application** tab → **Local Storage** → `https://game.skport.com`

You'll see a table similar to this (values blurred here — yours will be plain text):

![Firefox Storage panel showing SK_OAUTH_CRED_KEY and SK_TOKEN_CACHE_KEY rows](assets/storage.png)

### 3. Copy the values

| Key name             | Copy into config as  | Required? |
| -------------------- | -------------------- | --------- |
| `SK_OAUTH_CRED_KEY`  | `SK_OAUTH_CRED_KEY`  | ✅ yes     |
| `SK_TOKEN_CACHE_KEY` | `SK_TOKEN_CACHE_KEY` | optional  |

Copy the **value** (the right-hand column), not the key name. Both are long opaque strings.
`SK_OAUTH_CRED_KEY` is the only required credential — `SK_TOKEN_CACHE_KEY` is a session token the script **refreshes automatically** via `SK_OAUTH_CRED_KEY` when it is missing or expired, so you can leave it empty.

### 4. Find your game ID and server

Open DevTools → **Network** tab, reload the page, and click on any request to `zonai.skport.com` (e.g. the attendance call). In the **Headers** section find the `sk-game-role` header — its format is:

```
3_{gameId}_{server}
```

![Network tab with the sk-game-role header visible](assets/network_tools.png)

- **`gameId`** = the `{gameId}` part
- **`server`** = the `{server}` part (commonly `2`)

> **Multi-account?** Repeat for each account: log out, log in with the next account, and copy its values into a separate profile entry.

---

## Local configuration (`config.json`)

Create `config.json` in the repo root (it is git-ignored — it will never be committed). Start from the example:

```bash
cp example_config.json config.json
```

Then fill in your real values (any field you don't fill yet can stay as `REPLACE_ME`):

```json
{
    "profiles": [
        {
            "SK_OAUTH_CRED_KEY": "your-oauth-value-here",
            "SK_TOKEN_CACHE_KEY": "",
            "gameId": "123456789",
            "server": "2",
            "language": "en",
            "accountName": "Player1",
            "myDiscordID": "123456789012345678"
        }
    ],
    "discordNotify": true,
    "myDiscordID": "123456789012345678",
    "discordWebhook": "https://discord.com/api/webhooks/..."
}
```

`SK_TOKEN_CACHE_KEY` is optional — leave it empty and the script refreshes it automatically via `SK_OAUTH_CRED_KEY` on the first run.

Run it:

```bash
python main.py --config-source json
```

### Scheduling locally

Add a cron job (Linux/macOS) or Task Scheduler (Windows) to run the same command daily:

```cron
0 0 * * * cd /path/to/endfield-skport-action && python main.py --config-source json
```

The script's server-side check prevents double sign-ins, so extra runs are safe.

---

## GitHub Actions setup

### 1. Secrets

In your repository go to **Settings → Secrets and variables → Actions → Secrets** and add:

| Secret                 | Value                                                                      |
| ---------------------- | -------------------------------------------------------------------------- |
| `SKPORT_PROFILES_JSON` | A JSON array of your profiles (see below)                                  |
| `DISCORD_WEBHOOK`      | Your Discord webhook URL (optional — leave unset to disable notifications) |
| `MY_DISCORD_ID`        | Your Discord user ID for `@mention`s (optional)                            |
| `DISCORD_NOTIFY`       | `"true"` (default when unset) or `"false"`                                 |

`SKPORT_PROFILES_JSON` is a single-line JSON array. For one account:

```json
[{"SK_OAUTH_CRED_KEY":"...","gameId":"123456789","server":"2","language":"en","accountName":"Player1"}]
```

`SK_TOKEN_CACHE_KEY` may be omitted entirely — the script refreshes it automatically via `SK_OAUTH_CRED_KEY` on every run. For multiple accounts, add more objects to the array — each becomes one profile.

> **Tip:** Generate the JSON from your local `config.json` with `python -c "import json;print(json.dumps(json.load(open('config.json'))['profiles']))"`, or answer a few prompts with `python gh_setup.py` (prints the `SKPORT_PROFILES_JSON` value and the optional Discord secrets).

### 2. Variables

| Variable           | Value                                                          |
| ------------------ | -------------------------------------------------------------- |
| `LAST_SIGNIN_DATE` | *(optional, auto-managed)* leave unset or empty on first setup |

The workflow maintains this automatically — see [github-actions.md](github-actions.md#variables).

### 3. Run

Push the workflow file (already included at `.github/workflows/checkin.yaml`) and either:

- wait for the schedule (00:00 / 06:00 / 12:00 / 18:00 UTC), or
- go to **Actions → Skport Daily Auto Check-in → Run workflow** to trigger a manual run (it forces execution by default)

### 4. Verify

Check the run logs or your Discord channel. A successful run reports per-profile results, e.g.:

```
Check-in completed for Player1
Endfield: ✅ ...
```

---

## Environment variables (alternative to config.json)

Every `config.json` key has an environment variable equivalent. `--config-source` selects the source(s):

| Config key       | Env var                | Notes                                         |
| ---------------- | ---------------------- | --------------------------------------------- |
| `profiles`       | `SKPORT_PROFILES_JSON` | JSON array of profile objects                 |
| `discordNotify`  | `DISCORD_NOTIFY`       | `"true"` / `"false"`                          |
| `myDiscordID`    | `MY_DISCORD_ID`        |                                               |
| `discordWebhook` | `DISCORD_WEBHOOK`      |                                               |
| `lastSigninDate` | `LAST_SIGNIN_DATE`     | **Actions only** — not read from JSON configs |

```bash
SKPORT_PROFILES_JSON='[{"SK_OAUTH_CRED_KEY":"...","gameId":"...","server":"2"}]' \
python main.py --config-source env
```
