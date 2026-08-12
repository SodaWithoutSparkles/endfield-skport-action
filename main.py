import argparse
import datetime
import hashlib
import hmac
import json
import logging
import os
import sys
import time
from typing import Optional
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
    """Structured request failure: sanitized message plus HTTP/business codes."""

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
    """Skport signature: HMAC-SHA256 over path + query/body + timestamp +
    compact JSON of {platform, timestamp, dId, vName}, then MD5 of the hex digest."""
    string_to_sign = path + ((query or "") if method ==
                             "GET" else (body or ""))
    if "timestamp" in headers:
        string_to_sign += str(headers["timestamp"])

    # Key order and dId defaulting are part of the signature; do not reorder.
    header_obj = {key: headers.get(key, "") for key in ("platform", "timestamp", "dId", "vName")
                  if key in headers or key == "dId"}
    string_to_sign += json.dumps(header_obj, separators=(',', ':'))

    key = token.encode('utf-8') if isinstance(token, str) else token
    hmac_hex = hmac.new(key, string_to_sign.encode(
        'utf-8'), hashlib.sha256).hexdigest()
    return hashlib.md5(hmac_hex.encode('utf-8')).hexdigest()


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

    Mentions are attached only here — the messages main() logs never contain
    Discord user IDs.
    """
    if _is_unset(webhook_url):
        return

    lines = []
    for message, discord_id in results:
        if discord_id and not _is_unset(discord_id) and "\nEndfield: " in message:
            message = message.replace(
                "\nEndfield: ", f"\nEndfield: <@{discord_id}> ", 1)
        lines.append(message)
    post_webhook(webhook_url, "\n\n".join(lines))


def _handle_error(account_name: str, error: RequestError, profile: Profile, action: str) -> tuple[str, bool]:
    """Builds the failure message shared by the status check and check-in paths.

    `action` prefixes the message so a failed GET is distinguishable from a
    failed POST. The Discord @mention is attached later by notify_user().
    """
    safe_text = _sanitize(
        error.message, (profile.oauth_cred, profile.token, profile.game_id,))
    return (f"{action} failed for {account_name}\n"
            f"Endfield: {safe_text}"), False


def _generate_headers(profile: Profile) -> dict:
    """Builds the shared request headers with a fresh timestamp.

    Callers add the 'sign' header per request, so a retry after a token
    refresh never reuses a stale `timestamp` in the signature.
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

    Headers are regenerated per call, so a retry after a token refresh never
    reuses a stale `timestamp`. Returns (response_json, error); one is None.
    """
    headers = _generate_headers(profile)
    headers['sign'] = generate_sign(path, method, headers, '', '', token)

    try:
        req = urllib.request.Request(
            ENDFIELD_HOST + path, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            status_code = response.getcode()
            raw_text = response.read().decode('utf-8')
    except OSError as e:
        # HTTPError carries a status code; other OSErrors are network problems.
        if isinstance(e, urllib.error.HTTPError):
            return None, RequestError(message=f"HTTP Error {e.code}", status=e.code)
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
    """GETs a fresh token; raises RuntimeError on any failure."""
    response_json, error = _request_json(
        profile, 'GET', profile.token, path=REFRESH_PATH)
    if error:
        raise RuntimeError(f"Refresh failed: {error.message}")
    if response_json.get("code") != 0:
        raise RuntimeError(
            f"Refresh failed: code={response_json.get('code')}, "
            f"message={response_json.get('message') or response_json.get('msg')}")
    token = (response_json.get("data") or {}).get("token")
    if not token:
        raise RuntimeError(
            f"Refresh succeeded but no token was returned: {response_json!r}")
    logger.info("Successfully refreshed SK_TOKEN_CACHE_KEY")
    return token


def persist_refreshed_token(profile: Profile, new_token: str) -> bool:
    """Writes the refreshed SK_TOKEN_CACHE_KEY back into config.json.

    Only the profile whose SK_OAUTH_CRED_KEY matches is touched. Returns False
    (no-op) when config.json is absent or the credential isn't found there.
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
    return None if msg == "OK" else RequestError(message=msg, code=code)


def _request_with_refresh(profile: Profile, method: str, token: Optional[str]) -> tuple[dict | None, RequestError | None, str]:
    """
    Performs a request, refreshing the token once if the server rejects it.
    Args:
        profile: The Profile to use for the request.
        method: 'GET' or 'POST'.
        token: The current SK_TOKEN_CACHE_KEY; may be None or empty, in which case a refresh is attempted immediately.
    Returns:
        (response_json, error, token)
    where one of response_json or error is None, and token is the latest SK_TOKEN_CACHE_KEY.
    """
    no_token = _is_unset(token)
    if no_token:
        logger.info(
            "No SK_TOKEN_CACHE_KEY configured; refreshing via SK_OAUTH_CRED_KEY")
    else:
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
        prefix = ("No token configured and refresh failed" if no_token
                  else "Token expired and refresh failed")
        advice = ("Re-copy SK_OAUTH_CRED_KEY from DevTools." if no_token
                  else "Re-copy both keys from DevTools.")
        return None, RequestError(
            f"{prefix} — SK_OAUTH_CRED_KEY has expired or changed. {advice}"
            f"{details_suffix}"
        ), token

    persist_refreshed_token(profile, new_token)

    if no_token:
        logger.info("Proceeding with refreshed token")
    else:
        logger.info("Retrying request with refreshed token")
    response_json, error = _request_json(profile, method, new_token)
    if _token_expired(error, response_json):
        return None, RequestError(
            "Token expired even after refresh — SK_OAUTH_CRED_KEY and "
            "SK_TOKEN_CACHE_KEY are out of sync. Re-copy both keys from DevTools."), new_token
    return response_json, error, new_token


def _checked_request(profile: Profile, method: str, token: str) -> tuple[dict | None, RequestError | None, str]:
    """Request with 401-refresh, also surfacing server-level errors."""
    response_json, error, token = _request_with_refresh(profile, method, token)
    if error is None:
        error = _server_error(response_json)
    return response_json, error, token


def check_attendance_status(profile: Profile, token: str) -> tuple[bool | None, int, RequestError | None, str]:
    """GETs the attendance calendar to see if today's reward is already claimed.

    Returns (already_signed_in, days_done, error, token).
    """
    response_json, error, token = _checked_request(profile, 'GET', token)
    if error:
        return None, 0, error, token

    data = response_json.get("data") or {}
    days_done = sum(1 for day in data.get("calendar") or []
                    if isinstance(day, dict) and day.get("done"))
    return bool(data.get("hasToday", False)), days_done, None, token


def do_checkin(profile: Profile, token: str) -> tuple[str, bool]:
    """POSTs the check-in (a 401 refreshes the token once); returns (message, success)."""
    response_json, error, _ = _checked_request(profile, 'POST', token)
    # The server answers a duplicate claim with HTTP 403; treat it as success
    # so a race between the status check and the POST doesn't fail the run.
    if error is not None and error.status == 403:
        return (f"Check-in skipped for {profile.account_name} — already signed in "
                f"(server rejected the POST with HTTP 403)\n"
                f"Endfield: ✅ Already checked in today."), True
    if error:
        return _handle_error(profile.account_name, error, profile, "Check-in")

    msg = response_json.get("message") or response_json.get("msg") or "OK"
    return (f"Check-in completed for {profile.account_name}\n"
            f"Endfield: {msg}"), True


def checkin_flow(profile: Profile, index: int, global_discord_id: str) -> tuple[str, bool, str]:
    """Runs the check-in flow for one profile: status check, then POST if needed.

    Fails closed: a failed status check aborts the check-in, mirroring normal
    user behaviour (never POST twice in one day).

    Returns:
        Tuple of (message, success, discord_id).
    """
    my_discord_id = profile.discord_id or global_discord_id

    # Missing credentials is a config gap: skip without failing the run.
    # SK_TOKEN_CACHE_KEY is optional — it is refreshed via SK_OAUTH_CRED_KEY.
    if _is_unset(profile.oauth_cred) or _is_unset(profile.game_id):
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


def _normalize_config(data: dict) -> dict:
    """Extracts the known keys from a raw config dict, validating profiles."""
    profiles = data.get("profiles", [])
    if not isinstance(profiles, list) or not all(isinstance(p, dict) for p in profiles):
        raise ValueError("'profiles' must be an array of objects")
    return {
        "profiles": profiles,
        "discordNotify": str(data.get("discordNotify", True)).lower() == "true",
        "myDiscordID": data.get("myDiscordID", ""),
        "discordWebhook": data.get("discordWebhook", ""),
    }


def _load_config_from_env() -> dict:
    """Loads config from environment variables. Raises on malformed values."""
    raw = os.getenv("SKPORT_PROFILES_JSON", "").strip()
    profiles = []
    if raw:
        try:
            profiles = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"SKPORT_PROFILES_JSON is not valid JSON: {e}") from e
    config = _normalize_config({"profiles": profiles})
    config.update({
        "discordNotify": os.getenv("DISCORD_NOTIFY", "true").lower() == "true",
        "myDiscordID": os.getenv("MY_DISCORD_ID", ""),
        "discordWebhook": os.getenv("DISCORD_WEBHOOK", ""),
        "lastSigninDate": os.getenv("LAST_SIGNIN_DATE", ""),
    })
    return config


def _load_config_from_json() -> dict:
    """Loads config from config.json; a missing file yields empty values."""
    try:
        return _normalize_config(_read_json_file(CONFIG_PATH))
    except FileNotFoundError:
        return {}


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


def _gh_outputs(**kwargs):
    """Writes several GITHUB_OUTPUT entries at once (no-op outside Actions)."""
    for key, value in kwargs.items():
        write_gh_output(key, value)


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
        _gh_outputs(executed="false", run_status="CONFIG_ERROR")
        sys.exit(1)

    today_utc = datetime.datetime.now(
        datetime.timezone.utc).strftime('%Y-%m-%d')
    last_signin = config.get("lastSigninDate", "")

    # Local same-day dedup, only when LAST_SIGNIN_DATE is provided (GitHub
    # Actions). --force bypasses this; the server-side hasToday check still applies.
    if not args.force and last_signin == today_utc:
        logger.info(
            f"Already completed sign-in for today ({today_utc}). Skipping execution.")
        _gh_outputs(executed="false", run_status="SKIPPED",
                    today_date=today_utc)
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

    results = []
    all_success = True

    for idx, profile in enumerate(profiles):
        if idx > 0:
            time.sleep(1)  # Throttle requests between profiles
        results.append(checkin_flow(profile, idx, global_discord_id))
        all_success &= results[-1][1]

    skport_resp = "\n\n".join(msg for msg, _ in results)

    # Output to stdout (log-safe: no Discord IDs or mention markup here)
    logger.info(skport_resp)

    # Trigger Discord webhook; mentions are attached only by notify_user
    if discord_notify and discord_webhook:
        notify_user(discord_webhook, results)

    # Export outputs for the GitHub Actions runner (no-op when unset)
    _gh_outputs(executed="true", today_date=today_utc,
                run_status="SUCCESS" if all_success else "FAILED")

    # Any failed profile marks the run as failed for cron/CI
    if not all_success:
        sys.exit(1)


if __name__ == "__main__":
    main()
