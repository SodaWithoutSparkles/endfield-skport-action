import argparse
import datetime
import hashlib
import hmac
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)


USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:153.0) Gecko/20100101 Firefox/153.0'
REQUEST_TIMEOUT_SECONDS = 10

# Paths observed from devtools
ENDFIELD_HOST = 'https://zonai.skport.com'
ENDFIELD_PATH = '/web/v1/game/endfield/attendance'
REFRESH_PATH = '/web/v1/auth/refresh'
# Magic constants observed from devtools
PLATFORM = '3'
SK_GAME_ROLE_PREFIX = '3'
VERSION_NAME = '1.0.0'

# Discord hard limit is 2000; leave headroom for the truncation note
DISCORD_CONTENT_CAP = 1950
PLACEHOLDER = 'REPLACE_ME'  # marks unconfigured values in example configs

# Server response codes mapped to human-readable explanations.
# Codes not listed here fall back to the raw message returned by the server.
KNOWN_CODES = {
    10000: '⚠️ Token expired — re-copy SK_OAUTH_CRED_KEY and SK_TOKEN_CACHE_KEY from DevTools.',
}

CONFIG_PATH = os.path.join(os.path.dirname(
    os.path.abspath(__file__)), 'config.json')


@dataclass
class RequestError:
    """Structured request failure — replaces stringly-typed error text.

    - message: human-readable description (sanitized before reaching logs/Discord)
    - status: HTTP status code when the server answered
    - code: server business code from the JSON payload, when present
    """

    message: str
    status: int | None = None
    code: int | None = None


@dataclass
class Profile:
    """Per-account configuration. Secrets keep their original env-style names."""

    oauth_cred: str = ""
    token: str = ""
    game_id: str = ""
    server: str = "2"
    language: str = "en"
    account_name: str = ""
    discord_id: str = ""

    @classmethod
    def from_dict(cls, data: dict, index: int) -> "Profile":
        """Parses one profile object from config.json / SKPORT_PROFILES_JSON."""
        return cls(
            oauth_cred=data.get("SK_OAUTH_CRED_KEY", ""),
            token=data.get("SK_TOKEN_CACHE_KEY", ""),
            game_id=str(data.get("gameId", "")),
            server=str(data.get("server", "2")),
            language=data.get("language", "en"),
            account_name=data.get("accountName") or f"Account {index + 1}",
            discord_id=data.get("myDiscordID", ""),
        )


def write_gh_output(key: str, value: str):
    """Appends outputs to GITHUB_OUTPUT file for downstream GitHub Action steps."""
    gh_output = os.getenv('GITHUB_OUTPUT')
    if gh_output:
        with open(gh_output, 'a', encoding='utf-8') as f:
            f.write(f"{key}={value}\n")


def generate_sign(path: str, method: str, headers: dict, query: str, body: str, token: str) -> str:
    """Generates the Skport HMAC-SHA256 + MD5 double-hash signature."""
    string_to_sign = path
    if method == "GET":
        string_to_sign += (query or "")
    else:
        string_to_sign += (body or "")

    if "timestamp" in headers:
        string_to_sign += str(headers["timestamp"])

    header_obj = {}
    for key in ["platform", "timestamp", "dId", "vName"]:
        if key in headers:
            header_obj[key] = headers[key]
        elif key == "dId":
            header_obj[key] = ""

    string_to_sign += json.dumps(header_obj, separators=(',', ':'))

    token_bytes = token.encode('utf-8') if isinstance(token, str) else token
    hmac_bytes = hmac.new(token_bytes, string_to_sign.encode(
        'utf-8'), hashlib.sha256).digest()
    hmac_hex = hmac_bytes.hex()

    md5_bytes = hashlib.md5(hmac_hex.encode('utf-8')).digest()
    return md5_bytes.hex()


def discord_ping(my_discord_id: str) -> str:
    return f"<@{my_discord_id}> " if my_discord_id and not _is_unset(my_discord_id) else ""


def _sanitize(text: str, secrets: tuple) -> str:
    """Redacts secret values from error text before it reaches logs or Discord."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, '[REDACTED]')
    return text


def post_webhook(webhook_url: str, data: str):
    """Sends check-in results to Discord, staying under the 2000-char content limit."""
    if _is_unset(webhook_url):
        return

    content = data[:DISCORD_CONTENT_CAP] + \
        "\n...[truncated]" if len(data) > DISCORD_CONTENT_CAP else data
    payload = json.dumps({
        'username': 'Skport Endfield Auto-Sign',
        'avatar_url': 'https://i.imgur.com/TguAOiA.png',
        'content': content
    }).encode('utf-8')

    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0'
        },
        method='POST'
    )

    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            pass
    except Exception as err:
        # The webhook URL carries a secret token; never let it reach the logs.
        logger.warning(
            f'Failed to send Discord notification: {_sanitize(str(err), (webhook_url,))}')


def notify_user(webhook_url: str, results: list[tuple[str, str]]) -> None:
    """Sends per-profile results to Discord, attaching each profile's @mention.

    Discord-specific concerns (mentions) live only here — the messages that
    main() logs never contain Discord user IDs.
    """
    if _is_unset(webhook_url):
        return

    lines = []
    for message, discord_id in results:
        ping = discord_ping(discord_id)
        if ping and "\nEndfield: " in message:
            # Every flow message carries an "Endfield:" line; drop the mention
            # in right after it so Discord sees the same format as before.
            message = message.replace(
                "\nEndfield: ", f"\nEndfield: {ping}", 1)
        lines.append(message)
    post_webhook(webhook_url, "\n\n".join(lines))


def _handle_error(account_name: str, error: RequestError, profile: Profile, action: str) -> tuple[str, bool]:
    """Builds the failure message shared by the status check and check-in paths.

    `action` prefixes the message ("Status check" / "Check-in") so a failed
    GET is distinguishable from a failed POST. The message is log-safe — the
    Discord @mention is attached later by notify_user().
    """
    safe_text = _sanitize(
        error.message, (profile.oauth_cred, profile.token, profile.game_id,))
    return (f"{action} failed for {account_name}\n"
            f"Endfield: {safe_text}"), False


def _generate_headers(profile: Profile) -> dict:
    """Builds the shared request headers with a fresh timestamp.

    Callers add the 'sign' header per request. Generating a fresh header set
    per request means a retry after a token refresh never reuses a stale
    `timestamp` in the signature.
    """
    return {
        'Accept': '*/*',
        'Content-Type': 'application/json',
        'User-Agent': USER_AGENT,
        'Referer': 'https://game.skport.com/',
        'platform': PLATFORM,
        'vName': VERSION_NAME,
        'Origin': 'https://game.skport.com',
        'Connection': 'keep-alive',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-site',
        'Priority': 'u=0',
        'TE': 'trailers',
        'cred': profile.oauth_cred,
        'sk-game-role': f"{SK_GAME_ROLE_PREFIX}_{profile.game_id}_{profile.server}",
        'sk-language': profile.language,
        'timestamp': str(int(time.time())),
    }


def _request_json(profile: Profile, method: str, token: str, path: str = ENDFIELD_PATH) -> tuple[dict | None, RequestError | None]:
    """Signs a fresh header set and performs the request against `path`.

    Headers (and therefore the `timestamp` used in signing) are regenerated
    on every call, so a retry after a token refresh never reuses a stale
    timestamp.

    Returns (response_json, error):
    - response_json: parsed response object, or None when the request failed
    - error: RequestError describing the failure, or None on success
    """
    headers = _generate_headers(profile)
    headers['sign'] = generate_sign(path, method, headers, '', '', token)

    try:
        req = urllib.request.Request(
            ENDFIELD_HOST + path, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            status_code = response.getcode()
            raw_text = response.read().decode('utf-8')
    except urllib.error.HTTPError as e:
        return None, RequestError(message=f"HTTP Error {e.code}", status=e.code)
    except urllib.error.URLError as e:
        return None, RequestError(message=f"Network Error: {e.reason}")
    except OSError as e:
        # Socket errors and timeouts (URLError above is itself an OSError).
        # Anything else is a programming bug and should crash loudly.
        return None, RequestError(message=f"Network Error: {e}")

    if status_code < 200 or status_code >= 300:
        return None, RequestError(
            message=f"HTTP Error {status_code}", status=status_code)

    try:
        response_json = json.loads(raw_text)
    except json.JSONDecodeError:
        return None, RequestError(message="Invalid JSON response from server.")
    if not isinstance(response_json, dict):
        return None, RequestError(message="Unexpected response from server.")
    return response_json, None


def refresh_cache_token(profile: Profile) -> str:
    """Refreshes the SK token and returns the new token.

    Reuses the standard header generation, including a `sign` header.
    Emperically observed to not verify or require a `sign`, 
    including to match behaviour observed from devtools.
    """
    headers = _generate_headers(profile)
    headers['sign'] = generate_sign(
        REFRESH_PATH, 'GET', headers, '', '', profile.token)

    req = urllib.request.Request(
        ENDFIELD_HOST + REFRESH_PATH,
        headers=headers,
        method='GET',
    )

    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            raw_text = response.read().decode('utf-8')
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        raise RuntimeError(f"Refresh HTTP {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Refresh network error: {e.reason}") from e
    except OSError as e:
        raise RuntimeError(f"Refresh network error: {e}") from e

    try:
        response_json = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Refresh returned invalid JSON: {raw_text}") from e

    if not isinstance(response_json, dict):
        raise RuntimeError(f"Unexpected refresh response: {response_json!r}")

    if response_json.get("code") != 0:
        raise RuntimeError(
            f"Refresh failed: code={response_json.get('code')}, "
            f"message={response_json.get('message') or response_json.get('msg')}"
        )

    token = (response_json.get("data") or {}).get("token")
    if not token:
        raise RuntimeError(
            f"Refresh succeeded but no token was returned: {response_json!r}")

    logger.info("Successfully refreshed SK_TOKEN_CACHE_KEY")
    return token


def persist_refreshed_token(profile: Profile, new_token: str) -> bool:
    """Writes the refreshed SK_TOKEN_CACHE_KEY back into config.json.

    Only the profile whose SK_OAUTH_CRED_KEY matches is touched, so env-driven
    profiles never overwrite a different local profile. Returns False (no-op)
    when config.json is absent or the credential isn't found there.
    """
    if not os.path.exists(CONFIG_PATH):
        return False
    try:
        data = _read_json_file(CONFIG_PATH)
    except (FileNotFoundError, ValueError) as e:
        logger.warning(f"Could not persist refreshed token: {e}")
        return False

    for entry in data.get("profiles", []):
        if isinstance(entry, dict) and entry.get("SK_OAUTH_CRED_KEY") == profile.oauth_cred:
            entry["SK_TOKEN_CACHE_KEY"] = new_token
            break
    else:
        return False

    tmp_path = CONFIG_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
    os.replace(tmp_path, CONFIG_PATH)
    logger.info("Persisted refreshed SK_TOKEN_CACHE_KEY to config.json")
    return True


def _token_expired(error: RequestError | None, response_json: dict | None) -> bool:
    """True when the server rejected the request because the token expired."""
    return (error is not None and error.status == 401) or (
        isinstance(response_json, dict) and response_json.get("code") == 10000)


def _server_error(response_json: dict) -> RequestError | None:
    """Builds a RequestError when the server payload itself carries a failure."""
    code = response_json.get("code")
    if code in KNOWN_CODES:
        return RequestError(message=KNOWN_CODES[code], code=code)
    msg = response_json.get("message") or response_json.get(
        "msg") or "Unknown status"
    if msg != "OK":
        return RequestError(message=msg, code=code)
    return None


def _out_of_sync_error() -> RequestError:
    """Error for a fresh token the server still rejects (both keys out of sync)."""
    return RequestError(
        "Token expired even after refresh — SK_OAUTH_CRED_KEY and "
        "SK_TOKEN_CACHE_KEY are out of sync. Re-copy both keys from DevTools."
    )


def _request_with_refresh(profile: Profile, method: str, token: str) -> tuple[dict | None, RequestError | None, str]:
    """Performs the request, treating the cache token as valid.

    Refreshing is strictly a 401 handler: when the server rejects the token
    (HTTP 401 or business code 10000), the token is refreshed once and the
    request is retried with the fresh token. The refreshed token is persisted
    back into config.json when present, and threaded through the return value
    so subsequent requests reuse it.

    A failed refresh means SK_OAUTH_CRED_KEY has expired or changed; a refresh
    that succeeds but still gets rejected means the two keys are out of sync.
    Both cases tell the user to re-copy both keys from DevTools.

    Returns (response_json, error, token):
    - response_json: parsed response object, or None when the request failed
    - error: RequestError describing the failure, or None on success
    - token: the (possibly refreshed) token, for reuse in subsequent requests
    """
    response_json, error = _request_json(profile, method, token)
    if not _token_expired(error, response_json):
        return response_json, error, token

    logger.info(
        "Token expired (HTTP 401 or code 10000); refreshing SK_TOKEN_CACHE_KEY")
    try:
        new_token = refresh_cache_token(profile)
    except RuntimeError as e:
        details = _sanitize(
            str(e), (profile.oauth_cred, profile.token, profile.game_id))
        details_suffix = f" Details: {details}" if details else ""
        return None, RequestError(
            "Token expired and refresh failed — SK_OAUTH_CRED_KEY has expired or "
            f"changed. Re-copy both keys from DevTools.{details_suffix}"
        ), token

    persist_refreshed_token(profile, new_token)

    logger.info("Retrying request with refreshed token")
    response_json, error = _request_json(profile, method, new_token)
    if _token_expired(error, response_json):
        return None, _out_of_sync_error(), new_token
    return response_json, error, new_token


def check_attendance_status(profile: Profile, token: str) -> tuple[bool | None, int, RequestError | None, str]:
    """GETs the attendance calendar to see if today's reward is already claimed.

    The cache token is assumed valid; a 401 triggers a one-shot refresh and
    retry inside _request_with_refresh.

    Returns (already_signed_in, days_done, error, token):
    - already_signed_in: True/False, or None when the check failed
    - days_done: number of claimed days from the calendar
    - error: RequestError on failure, or None on success
    - token: the (possibly refreshed) token, for the subsequent POST
    """
    response_json, error, token = _request_with_refresh(profile, 'GET', token)
    if error:
        return None, 0, error, token

    server_error = _server_error(response_json)
    if server_error:
        return None, 0, server_error, token

    data = response_json.get("data") or {}
    calendar = data.get("calendar") or []
    days_done = sum(1 for day in calendar if isinstance(
        day, dict) and day.get("done"))
    return bool(data.get("hasToday", False)), days_done, None, token


def do_checkin(profile: Profile, token: str) -> tuple[str, bool]:
    """POSTs the check-in (a 401 refreshes the token once); returns (message, success)."""
    response_json, error, _ = _request_with_refresh(profile, 'POST', token)
    # The server answers a duplicate claim with HTTP 403; treat it as success
    # so a race between the status check and the POST doesn't fail the run.
    if error is not None and error.status == 403:
        return (f"Check-in skipped for {profile.account_name} — already signed in "
                f"(server rejected the POST with HTTP 403)\n"
                f"Endfield: ✅ Already checked in today."), True
    if error:
        return _handle_error(profile.account_name, error, profile, "Check-in")

    server_error = _server_error(response_json)
    if server_error:
        return _handle_error(profile.account_name, server_error, profile, "Check-in")

    msg = response_json.get("message") or response_json.get("msg") or "OK"
    return (f"Check-in completed for {profile.account_name}\n"
            f"Endfield: {msg}"), True


def checkin_flow(profile: Profile, index: int, global_discord_id: str) -> tuple[str, bool, str]:
    """Runs the check-in flow for one profile: status check, then POST if needed.

    SK_TOKEN_CACHE_KEY is assumed valid; refreshing is strictly a 401 handler
    inside _request_with_refresh (refresh once, retry with the fresh token). A
    failed refresh means SK_OAUTH_CRED_KEY has expired or changed: the profile
    fails and the user is told to re-copy both keys. The status check may
    refresh the token mid-flow; the fresh token is threaded through to the POST.

    Fails closed: a failed status check aborts the check-in, mirroring normal
    user behaviour (never POST twice in one day).

    Return:
        A tuple of (message, success, discord_id).
    """
    my_discord_id = profile.discord_id or global_discord_id

    # A profile without credentials is a configuration gap, not a failure:
    # skip it without failing the run (other profiles may be fully configured).
    if _is_unset(profile.oauth_cred) or _is_unset(profile.token) or _is_unset(profile.game_id):
        return f"[Profile {index + 1}] Skip: Missing configuration credentials.", True, my_discord_id

    already_signed_in, days_done, status_error, token = check_attendance_status(
        profile, profile.token)
    if status_error:
        msg, ok = _handle_error(
            profile.account_name, status_error, profile, "Status check")
        return msg, ok, my_discord_id
    if already_signed_in:
        day_word = "day" if days_done == 1 else "days"
        return (f"Check-in skipped for {profile.account_name} — already signed in today "
                f"({days_done} {day_word} claimed)\n"
                f"Endfield: ✅ Already checked in today."), True, my_discord_id

    logger.debug(f"[Profile {index + 1}] Generated sign for POST")
    msg, ok = do_checkin(profile, token)
    return msg, ok, my_discord_id


def _is_unset(value) -> bool:
    """True when a value is None, empty, or the REPLACE_ME placeholder."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().startswith(PLACEHOLDER) or value.strip() == ""
    return False


def _read_json_file(path: str) -> dict:
    """Reads a JSON config file. Raises FileNotFoundError or ValueError."""
    with open(path, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"Could not parse '{path}': {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"Config file '{path}' must contain a JSON object")
    return data


def _load_config_from_env() -> dict:
    """Loads config from environment variables. Raises on malformed values."""
    profiles_raw = os.getenv("SKPORT_PROFILES_JSON", "")
    if profiles_raw.strip() == "":
        profiles = []
    else:
        try:
            profiles = json.loads(profiles_raw)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"SKPORT_PROFILES_JSON is not valid JSON: {e}") from e
        if not isinstance(profiles, list):
            raise ValueError("SKPORT_PROFILES_JSON must be a JSON array")
        if not all(isinstance(p, dict) for p in profiles):
            raise ValueError(
                "SKPORT_PROFILES_JSON must be an array of objects")

    return {
        "profiles": profiles,
        "discordNotify": os.getenv("DISCORD_NOTIFY", "true").lower() == "true",
        "myDiscordID": os.getenv("MY_DISCORD_ID", ""),
        "discordWebhook": os.getenv("DISCORD_WEBHOOK", ""),
        "lastSigninDate": os.getenv("LAST_SIGNIN_DATE", ""),
    }


def _load_config_from_json() -> dict:
    """Loads config from config.json. A missing file yields empty values; malformed content raises."""
    try:
        data = _read_json_file(CONFIG_PATH)
    except FileNotFoundError:
        return {}

    profiles = data.get("profiles", [])
    if not isinstance(profiles, list):
        raise ValueError("'profiles' in config.json must be an array")
    if not all(isinstance(p, dict) for p in profiles):
        raise ValueError(
            "'profiles' in config.json must be an array of objects")

    return {
        "profiles": profiles,
        "discordNotify": str(data.get("discordNotify", True)).lower() == "true",
        "myDiscordID": data.get("myDiscordID", ""),
        "discordWebhook": data.get("discordWebhook", ""),
    }


def load_config(source: str) -> dict:
    """Loads and merges configuration from 'env' and/or 'json' sources.

    Environment variables override config.json values. Raises ValueError when
    the merged config has no usable profiles.
    """
    config = {}
    if source in ("json", "both"):
        config.update(_load_config_from_json())
    if source in ("env", "both"):
        env_config = _load_config_from_env()
        for key, value in env_config.items():
            if value not in (None, "", []):
                config[key] = value

    if not config.get("profiles"):
        raise ValueError(
            "No profiles configured. Set SKPORT_PROFILES_JSON or the 'profiles' key in config.json.")
    return config


def main():
    parser = argparse.ArgumentParser(description="Skport Auto-Sign Tool")
    parser.add_argument("--config-source", choices=["env", "json", "both"],
                        default="both",
                        help="Config source: env, json, or both with env overriding json (default: both)")
    parser.add_argument("--force", action="store_true",
                        help="Bypass only the local last_signin_date dedup check; "
                             "the server-side hasToday check still applies")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )

    try:
        config = load_config(args.config_source)
    except (ValueError, FileNotFoundError) as err:
        logger.error(f"Configuration error: {err}")
        write_gh_output("executed", "false")
        write_gh_output("run_status", "CONFIG_ERROR")
        sys.exit(1)

    today_utc = datetime.datetime.now(
        datetime.timezone.utc).strftime('%Y-%m-%d')
    last_signin = config.get("lastSigninDate", "")

    # Same-day dedup only applies when LAST_SIGNIN_DATE is provided (env / GitHub
    # Actions, where the workflow persists it). JSON configs skip this entirely:
    # the server-side hasToday check in checkin_flow is the real guard.
    # --force bypasses only this local check; the server still refuses a
    # duplicate claim (hasToday / HTTP 403).
    if not args.force and last_signin == today_utc:
        logger.info(
            f"Already completed sign-in for today ({today_utc}). Skipping execution.")
        write_gh_output("executed", "false")
        write_gh_output("run_status", "SKIPPED")
        write_gh_output("today_date", today_utc)
        return

    profiles = [Profile.from_dict(p, idx)
                for idx, p in enumerate(config["profiles"])]
    discord_notify = config.get("discordNotify", True)
    global_discord_id = config.get("myDiscordID", "")
    discord_webhook = config.get("discordWebhook", "")

    # Notifications need a webhook; disable silently-missing or placeholder config
    if discord_notify and _is_unset(discord_webhook):
        logger.warning(
            "discord_notify is enabled but no webhook URL is configured; notifications disabled.")
        discord_notify = False

    messages = []
    results = []
    all_success = True

    for idx, profile in enumerate(profiles):
        if idx > 0:
            time.sleep(1)  # Throttle requests between profiles
        msg, success, discord_id = checkin_flow(
            profile, idx, global_discord_id)
        messages.append(msg)
        results.append((msg, discord_id))
        if not success:
            all_success = False

    skport_resp = "\n\n".join(messages)

    # Output to stdout (log-safe: no Discord IDs or mention markup here)
    logger.info(skport_resp)

    # Trigger Discord webhook; mentions are attached only by notify_user
    if discord_notify and discord_webhook:
        notify_user(discord_webhook, results)

    # Export outputs for GitHub Actions runner (no-op when GITHUB_OUTPUT is unset)
    write_gh_output("executed", "true")
    write_gh_output("today_date", today_utc)
    write_gh_output("run_status", "SUCCESS" if all_success else "FAILED")

    # Any failed profile marks the run as failed for cron/CI
    if not all_success:
        sys.exit(1)


if __name__ == "__main__":
    main()
