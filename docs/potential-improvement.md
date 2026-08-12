# Potential Improvements

Findings from analyzing third-party sign-in scripts (e.g. [from kaihuang1122](https://github.com/kaihuang1122/skport-auto-sign/blob/main/src/main-discord.gs), an
Apps Script implementation covering both Arknights and Endfield) that we did
**not** implement — and why. Mostly notes for a future refactor.

## The `sk-game-role` header is fully discoverable at runtime

The header format is:

```
{gameId}_{roleId}_{serverId}
```

where the first segment is **not** a "platform" — it is the game ID:

| Game      | Prefix | Meaning of the rest                                |
| --------- | ------ | -------------------------------------------------- |
| Arknights | `1`    | `{uid}_{1}` — account uid, server hardcoded to `1` |
| Endfield  | `3`    | `{roleId}_{serverId}` — per-role, per-server       |

Both segments currently scraped by hand in this project (`gameId`, `server` in
config.json) are returned by a single signed request:

```
GET https://zonai.skport.com/api/v1/game/player/binding
```

Response shape (observed in the reference script):

```jsonc
{
  "code": 0,
  "data": {
    "list": [
      {
        "appCode": "arknights",
        "bindingList": [
          { "uid": "...", "nickName": "..." }            // server is always 1
        ]
      },
      {
        "appCode": "endfield",
        "bindingList": [
          {
            "roles": [
              {
                "roleId": "...",
                "serverId": "...",
                "serverName": "...",
                "nickName": "..."
              }
            ]
          }
        ]
      }
    ]
  }
}
```

The reference script signs this GET with the same `generate_sign` machinery we
already have, and builds each attendance task as
`3_{roleId}_{serverId}` — identical to what `docs/setup.md` tells users to read
out of DevTools. **The author of that script never implemented the obvious
follow-up: dropping the config fields entirely and auto-discovering roles.**
We could do the same: one extra signed GET per run, then check in to every role
under the account (multiple servers/characters included) with zero manual
configuration.

### Why we didn't implement it

- `gameId` / `server` already work; auto-discovery changes the config contract
  (breaking change for existing users, GitHub Actions secrets, `gh_setup.py`).
- The binding endpoint response was only observed via the reference script, not
  captured directly from our own DevTools session — worth verifying before
  committing to it.

## Arknights support is untested — do not port it

The reference script also handles Arknights check-ins
(`POST /api/v1/game/attendance`, role `1_{uid}_1`). We deliberately did **not**
port it:

- We have no Arknights account to test against, and the reference script's
  Arknights path is itself unverified by us.
- Rule: **don't write code we can't test.** If Arknights support is ever added,
  it should be driven by real DevTools captures and golden vectors like the
  Endfield path, not by porting a third-party script.

## Side notes for future work

- The reference script bootstraps a full session from a single login cookie:
  `as.gryphline.com/user/oauth2/v2/grant` (`appCode: "6eb76d4e13aa36e6"`,
  `type: 0`) → `POST /api/v1/user/auth/generate_cred_by_code`
  (`{code, kind: 1}`) → `cred` + `token`. Could replace the two-key
  `SK_OAUTH_CRED_KEY` / `SK_TOKEN_CACHE_KEY` setup one day.
- Their binding/refresh calls use `/api/v1/...` paths while our captured
  traffic used `/web/v1/...`; both appear to work. Prefer whatever we capture
  ourselves.
- The sign algorithm in their script matches our `generate_sign` exactly
  (path + query/body + timestamp + `{platform,timestamp,dId,vName}` JSON,
  HMAC-SHA256 hex, then MD5) — good independent confirmation of
  `tests/test_sign.py` golden vectors.
