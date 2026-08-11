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

logger = logging.getLogger(__name__)

# --- Constants -----------------------------------------------------------
# Single source of truth for the endpoint: URL is derived from the path used in signing.
ENDFIELD_HOST = 'https://zonai.skport.com'
ENDFIELD_PATH = '/web/v1/game/endfield/attendance'
URL_ENDFIELD = ENDFIELD_HOST + ENDFIELD_PATH

# server platform id (meaning unknown; matches reference script)
PLATFORM = '3'
SK_GAME_ROLE_PREFIX = '3'   # role prefix meaning unknown; hardcoded to match server
VERSION_NAME = '1.0.0'
REQUEST_TIMEOUT_SECONDS = 10
# Discord hard limit is 2000; leave headroom for the truncation note
DISCORD_CONTENT_CAP = 1950
PLACEHOLDER = 'REPLACE_ME'  # marks unconfigured values in example configs

# Server response codes mapped to human-readable explanations.
# Codes not listed here fall back to the raw message returned by the server.
KNOWN_CODES = {
    10000: '⚠️ Token expired! Please update SK_TOKEN_CACHE_KEY in your config.',
}

CONFIG_PATH = os.path.join(os.path.dirname(
    os.path.abspath(__file__)), 'config.json')


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
        logger.warning(f'Failed to send Discord notification: {err}')


def _handle_error(account_name: str, my_discord_id: str, error_text: str, secrets: tuple) -> tuple[str, bool]:
    """Builds the shared failure message for the status check and check-in paths."""
    safe_text = _sanitize(error_text, secrets)
    return (f"Check-in failed for {account_name}\n"
            f"Endfield: {discord_ping(my_discord_id)}{safe_text}"), False


def _handle_check_status_error(account_name: str, my_discord_id: str, error_text: str, secrets: tuple) -> tuple[str, bool]:
    """Error handler for the attendance status check (GET)."""
    return _handle_error(account_name, my_discord_id, error_text, secrets)


def _handle_checkin_error(account_name: str, my_discord_id: str, error_text: str, secrets: tuple) -> tuple[str, bool]:
    """Error handler for the check-in POST."""
    return _handle_error(account_name, my_discord_id, error_text, secrets)


def _generate_headers(sk_oauth_cred: str, game_id: str, server: str, language: str) -> dict:
    """Builds the shared request headers. The 'sign' header is added per request."""
    return {
        'Accept': '*/*',
        'Content-Type': 'application/json',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:153.0) Gecko/20100101 Firefox/153.0',
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
        'cred': sk_oauth_cred,
        'sk-game-role': f"{SK_GAME_ROLE_PREFIX}_{game_id}_{server}",
        'sk-language': language,
        'timestamp': str(int(time.time())),
    }


def _request_json(headers: dict, method: str, token: str) -> tuple[dict | None, str]:
    """Signs the headers and performs the request against the attendance endpoint.

    Returns (response_json, error_text):
    - response_json: parsed response object, or None when the request failed
    - error_text: '' on success, otherwise a description of the failure
    """
    signed_headers = dict(headers)
    signed_headers['sign'] = generate_sign(
        ENDFIELD_PATH, method, signed_headers, '', '', token)

    try:
        req = urllib.request.Request(
            URL_ENDFIELD, headers=signed_headers, method=method)
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            status_code = response.getcode()
            raw_text = response.read().decode('utf-8')
    except urllib.error.HTTPError as e:
        return None, f"HTTP Error {e.code}"
    except urllib.error.URLError as e:
        return None, f"Network Error: {e.reason}"
    except Exception as e:
        return None, f"Execution Error: {e}"

    if status_code < 200 or status_code >= 300:
        return None, f"HTTP Error {status_code}"

    try:
        response_json = json.loads(raw_text)
    except json.JSONDecodeError:
        return None, "Invalid JSON response from server."
    if not isinstance(response_json, dict):
        return None, "Unexpected response from server."
    return response_json, ""


def check_attendence_status(headers: dict, token: str) -> tuple[bool | None, int, str]:
    """GETs the attendance calendar to see if today's reward is already claimed.

    Returns (already_signed_in, days_done, error_text):
    - already_signed_in: True/False, or None when the check failed
    - days_done: number of claimed days from the calendar
    - error_text: '' on success, otherwise a description of the failure
    """
    response_json, error = _request_json(headers, 'GET', token)
    if error:
        return None, 0, error

    code = response_json.get("code")
    if code in KNOWN_CODES:
        return None, 0, KNOWN_CODES[code]

    msg = response_json.get("message") or response_json.get(
        "msg") or "Unknown status"
    if msg != "OK":
        return None, 0, msg

    data = response_json.get("data") or {}
    calendar = data.get("calendar") or []
    days_done = sum(1 for day in calendar if isinstance(
        day, dict) and day.get("done"))
    return bool(data.get("hasToday", False)), days_done, ""


def do_checkin(headers: dict, token: str, account_name: str, my_discord_id: str, secrets: tuple) -> tuple[str, bool]:
    """POSTs the check-in and returns (message, success)."""
    response_json, error = _request_json(headers, 'POST', token)
    if error:
        return _handle_checkin_error(account_name, my_discord_id, error, secrets)

    code = response_json.get("code")
    if code in KNOWN_CODES:
        return _handle_checkin_error(account_name, my_discord_id, KNOWN_CODES[code], secrets)

    msg = response_json.get("message") or response_json.get(
        "msg") or "Unknown status"
    is_error = msg != "OK"
    response_msg = (f"Check-in completed for {account_name}\n"
                    f"Endfield: {discord_ping(my_discord_id) if is_error else ''}{msg}")
    return response_msg, not is_error


def checkin_flow(profile: dict, index: int, global_discord_id: str) -> tuple[str, bool]:
    """Runs the full check-in flow for one profile: status check, then POST if needed.

    Fails closed: a failed status check aborts the check-in, mirroring normal
    user behaviour (never POST twice in one day).
    """
    sk_oauth_cred = profile.get("SK_OAUTH_CRED_KEY", "")
    sk_token_cache = profile.get("SK_TOKEN_CACHE_KEY", "")
    game_id = profile.get("id", "")
    server = profile.get("server", "2")
    language = profile.get("language", "en")
    account_name = profile.get("accountName", f"Account {index + 1}")
    my_discord_id = profile.get("myDiscordID", global_discord_id)

    if _is_unset(sk_oauth_cred) or _is_unset(sk_token_cache) or _is_unset(game_id):
        return f"[Profile {index + 1}] Skip: Missing configuration credentials.", False

    secrets = (sk_oauth_cred, sk_token_cache)
    headers = _generate_headers(sk_oauth_cred, game_id, server, language)

    already_signed_in, days_done, status_error = check_attendence_status(
        headers, sk_token_cache)
    if status_error:
        return _handle_check_status_error(account_name, my_discord_id, status_error, secrets)
    if already_signed_in:
        day_word = "day" if days_done == 1 else "days"
        return (f"Check-in skipped for {account_name} — already signed in today "
                f"({days_done} {day_word} claimed)\n"
                f"Endfield: {discord_ping(my_discord_id)}✅ Already checked in today."), True

    logger.debug(f"[Profile {index + 1}] Generated sign for POST")
    return do_checkin(headers, sk_token_cache, account_name, my_discord_id, secrets)


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
        "discord_notify": os.getenv("DISCORD_NOTIFY", "true").lower() == "true",
        "myDiscordID": os.getenv("MY_DISCORD_ID", ""),
        "discordWebhook": os.getenv("DISCORD_WEBHOOK", ""),
        "last_signin_date": os.getenv("LAST_SIGNIN_DATE", ""),
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
        "discord_notify": str(data.get("discord_notify", True)).lower() == "true",
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
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )

    parser = argparse.ArgumentParser(description="Skport Auto-Sign Tool")
    parser.add_argument("--config-source", choices=["env", "json", "both"],
                        default="both",
                        help="Config source: env, json, or both with env overriding json (default: both)")
    parser.add_argument("--force", action="store_true",
                        help="Bypass daily check-in deduplication check")
    args = parser.parse_args()

    try:
        config = load_config(args.config_source)
    except (ValueError, FileNotFoundError) as err:
        logger.error(f"Configuration error: {err}")
        write_gh_output("executed", "false")
        write_gh_output("run_status", "CONFIG_ERROR")
        sys.exit(1)

    today_utc = datetime.datetime.now(
        datetime.timezone.utc).strftime('%Y-%m-%d')
    last_signin = config.get("last_signin_date", "")

    # Same-day dedup only applies when LAST_SIGNIN_DATE is provided (env / GitHub
    # Actions, where the workflow persists it). JSON configs skip this entirely:
    # the server-side hasToday check in checkin_flow is the real guard.
    if not args.force and last_signin == today_utc:
        logger.info(
            f"Already completed sign-in for today ({today_utc}). Skipping execution.")
        write_gh_output("executed", "false")
        write_gh_output("run_status", "SKIPPED")
        write_gh_output("today_date", today_utc)
        return

    profiles = config["profiles"]
    discord_notify = config.get("discord_notify", True)
    global_discord_id = config.get("myDiscordID", "")
    discord_webhook = config.get("discordWebhook", "")

    # Notifications need a webhook; disable silently-missing or placeholder config
    if discord_notify and _is_unset(discord_webhook):
        logger.warning(
            "discord_notify is enabled but no webhook URL is configured; notifications disabled.")
        discord_notify = False

    messages = []
    all_success = True

    for idx, profile in enumerate(profiles):
        if idx > 0:
            time.sleep(1)  # Throttle requests between profiles
        msg, success = checkin_flow(profile, idx, global_discord_id)
        messages.append(msg)
        if not success:
            all_success = False

    skport_resp = "\n\n".join(messages)

    # Output to stdout
    logger.info(skport_resp)

    # Trigger Discord webhook
    if discord_notify and discord_webhook:
        post_webhook(discord_webhook, skport_resp)

    # Export outputs for GitHub Actions runner (no-op when GITHUB_OUTPUT is unset)
    write_gh_output("executed", "true")
    write_gh_output("today_date", today_utc)
    write_gh_output("run_status", "SUCCESS" if all_success else "FAILED")

    # Any failed profile marks the run as failed for cron/CI
    if not all_success:
        sys.exit(1)


if __name__ == "__main__":
    main()
