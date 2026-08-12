"""Generate GitHub secret values for the Skport check-in workflow.

Run:  python gh_setup.py
Paste the printed values into:
  Settings -> Secrets and variables -> Actions -> Secrets
The workflow only needs SKPORT_PROFILES_JSON; DISCORD_* are optional.
"""
import json

profiles = []
n = 1
while True:
    print(f"\n--- Account {n} ---")
    cred = input("SK_OAUTH_CRED_KEY (required): ").strip()
    if not cred:
        if n == 1:
            print("Nothing entered; exiting.")
            raise SystemExit(1)
        break
    game_id = input(
        "gameId (required, from the sk-game-role header): ").strip()
    profile = {
        "SK_OAUTH_CRED_KEY": cred,
        "gameId": game_id,
        "server": input("server [2]: ").strip() or "2",
        "language": input("language [en]: ").strip() or "en",
        "accountName": input(f"accountName [Account {n}]: ").strip() or f"Account {n}",
    }
    discord_id = input(
        "myDiscordID (optional, per-account @mention): ").strip()
    if discord_id:
        profile["myDiscordID"] = discord_id
    profiles.append(profile)
    n += 1
    if input("Add another account? [y/N]: ").strip().lower() != "y":
        break

print("\n===== SKPORT_PROFILES_JSON (paste this as a secret) =====")
print(json.dumps(profiles, ensure_ascii=False, separators=(",", ":")))

webhook = input("\nDiscord webhook URL (optional, empty to skip): ").strip()
print(f"\n===== Other secrets (optional) =====")
print(
    f"DISCORD_WEBHOOK: {webhook or '<leave unset to disable notifications>'}")
discord_id = input("Default Discord user ID (optional): ").strip()
print(f"MY_DISCORD_ID: {discord_id or '<leave unset>'}")
print("DISCORD_NOTIFY: <leave unset — defaults to 'true'>")
