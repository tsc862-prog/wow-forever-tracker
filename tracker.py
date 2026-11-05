#!/usr/bin/env python3
"""
WoW: Forever progress tracker.

Every run checks each character in characters.json against Blizzard's
Profile API, compares with the last check (state.json), and posts one
Discord message with everything that changed since then:

  - level-ups (and who was first to max level)
  - professions learned, and profession skill milestones
  - dungeons cleared for the first time
  - raid bosses killed for the first time, and full raid clears
  - achievements earned

Anything a character already had when it was first tracked is recorded
quietly, so only new progress gets posted. Uses only the Python
standard library.

Environment variables:
  BLIZZARD_CLIENT_ID      Battle.net API client ID
  BLIZZARD_CLIENT_SECRET  Battle.net API client secret
  DISCORD_WEBHOOK_URL     Discord channel webhook URL
  PROFILE_NAMESPACE       optional override, default profile-forever-{region}

Usage:
  python3 tracker.py                 normal run
  python3 tracker.py --dry-run       print what would be posted, save nothing
  python3 tracker.py --test-webhook  send a test message to Discord and exit
"""

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "characters.json"
STATE_PATH = ROOT / "state.json"
USER_AGENT = "WoWForeverTracker/1.1"
DEFAULT_COLOR = 0x5865F2

CLASS_COLORS = {
    "Druid": 0xFF7C0A,
    "Hunter": 0xAAD372,
    "Mage": 0x3FC7EB,
    "Paladin": 0xF48CBA,
    "Priest": 0xFFFFFF,
    "Rogue": 0xFFF468,
    "Shaman": 0x0070DD,
    "Warlock": 0x8788EE,
    "Warrior": 0xC69B6D,
}

DEFAULT_SETTINGS = {
    "max_level": 60,
    "track_levels": True,
    "track_professions": True,
    "profession_milestones": [75, 150, 225, 300],
    "track_dungeons": True,
    "track_raids": True,
    "track_achievements": True,
}


# ---------------------------------------------------------------- HTTP

def http(method, url, headers=None, data=None, timeout=20):
    """Return (status, parsed JSON or None). Network failures raise."""
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"User-Agent": USER_AGENT, **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return resp.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as err:
        body = err.read()
        try:
            parsed = json.loads(body) if body else None
        except ValueError:
            parsed = None
        return err.code, parsed


# ---------------------------------------------------------------- Blizzard

def get_token(client_id, client_secret):
    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    status, body = http(
        "POST",
        "https://oauth.battle.net/token",
        {
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        b"grant_type=client_credentials",
    )
    if status != 200 or not body or "access_token" not in body:
        sys.exit(f"Could not get a Battle.net token (HTTP {status}). "
                 "Check BLIZZARD_CLIENT_ID and BLIZZARD_CLIENT_SECRET.")
    return body["access_token"]


def api_get(region, path, token, namespace):
    query = urllib.parse.urlencode({"namespace": namespace, "locale": "en_US"})
    url = f"https://{region}.api.blizzard.com{path}?{query}"
    return http("GET", url, {"Authorization": f"Bearer {token}"})


def name_of(obj):
    """Blizzard objects carry a name that is a string, or a locale map."""
    if not isinstance(obj, dict):
        return None
    name = obj.get("name")
    if isinstance(name, dict):
        return name.get("en_US") or next(iter(name.values()), None)
    return name


def char_slug(c):
    parts = [c["name"]] + ([c["surname"]] if c.get("surname") else [])
    return "-".join(urllib.parse.quote(p.strip().lower(), safe="") for p in parts)


def candidate_paths(c):
    """Paths to try, most likely first. 'api_path' in the config overrides."""
    if c.get("api_path"):
        return [c["api_path"]]
    slug = char_slug(c)
    paths = [f"/profile/wow/character/{slug}"]  # Forever's realmless form
    if c.get("realm"):
        realm = c["realm"].strip().lower().replace("'", "").replace(" ", "-")
        paths.append(f"/profile/wow/character/{realm}/{slug}")
    return paths


# ---------------------------------------------------------------- Parsing
# Each parser turns an API response into a plain dict that is saved in
# state.json and compared run to run. A parser returns None when the data
# isn't available, so that feature is skipped rather than misreported.

def parse_professions(body):
    """{profession name: highest skill} for primary and secondary professions."""
    if not isinstance(body, dict):
        return None
    out = {}
    for group in ("primaries", "secondaries"):
        for item in body.get(group) or []:
            name = name_of(item.get("profession"))
            if not name:
                continue
            skills = [item.get("skill_points")]  # flat (Classic-style) shape
            skills += [t.get("skill_points") for t in item.get("tiers") or []]  # tiered shape
            skills = [s for s in skills if isinstance(s, int)]
            out[name] = max(skills) if skills else 0
    return out


def parse_encounters(body):
    """
    {instance name: {"cleared": bool, "bosses": [names killed]}} from an
    encounters response. Modes (normal/heroic) are merged: an instance counts
    as cleared if any mode is fully done, and a boss counts if killed in any mode.
    """
    if not isinstance(body, dict):
        return None
    out = {}
    for exp in body.get("expansions") or []:
        for inst in exp.get("instances") or []:
            name = name_of(inst.get("instance"))
            if not name:
                continue
            entry = out.setdefault(name, {"cleared": False, "bosses": set()})
            for mode in inst.get("modes") or []:
                progress = mode.get("progress") or {}
                done, total = progress.get("completed_count"), progress.get("total_count")
                status = ((mode.get("status") or {}).get("type") or "").upper()
                if status == "COMPLETE" or (isinstance(done, int) and isinstance(total, int) and total > 0 and done >= total):
                    entry["cleared"] = True
                for enc in progress.get("encounters") or []:
                    if (enc.get("completed_count") or 0) > 0:
                        boss = name_of(enc.get("encounter"))
                        if boss:
                            entry["bosses"].add(boss)
    return {k: {"cleared": v["cleared"], "bosses": sorted(v["bosses"])} for k, v in out.items()}


def parse_achievements(body):
    """{achievement id: name} for completed achievements."""
    if not isinstance(body, dict):
        return None
    done = {}
    for item in body.get("achievements") or []:
        if item.get("completed_timestamp"):
            done[str(item.get("id"))] = name_of(item.get("achievement")) or "an achievement"
    return done


def fetch_snapshot(c, region, token, namespace, settings):
    """Return the character's current snapshot, or None if Blizzard has no data."""
    for path in candidate_paths(c):
        status, body = api_get(region, path, token, namespace)
        if status in (401, 403):
            raise RuntimeError(
                f"Blizzard refused the request (HTTP {status}) for {path} in "
                f"namespace {namespace}. The namespace may be wrong or not public yet."
            )
        if status != 200 or not isinstance(body, dict):
            continue

        snap = {
            "level": body.get("level"),
            "class": name_of(body.get("character_class")),
            "race": name_of(body.get("race")),
        }

        def sub(suffix, parser):
            s, b = api_get(region, f"{path}{suffix}", token, namespace)
            return parser(b) if s == 200 else None

        if settings["track_professions"]:
            snap["professions"] = sub("/professions", parse_professions)
        if settings["track_dungeons"]:
            snap["dungeons"] = sub("/encounters/dungeons", parse_encounters)
        if settings["track_raids"]:
            snap["raids"] = sub("/encounters/raids", parse_encounters)
        if settings["track_achievements"]:
            snap["achievements"] = sub("/achievements", parse_achievements)
        return snap
    return None


# ---------------------------------------------------------------- Changes

def display_name(c):
    full = " ".join(p for p in (c["name"], c.get("surname")) if p)
    return f"{full} ({c['owner']})" if c.get("owner") else full


def find_changes(key, old, new, settings, group):
    """Lines describing what changed between two snapshots."""
    lines = []
    max_level = settings["max_level"]

    # Levels
    level, old_level = new.get("level") or 0, old.get("level") or 0
    if settings["track_levels"] and level > old_level:
        if level >= max_level > old_level and not group.get("first_to_max"):
            group["first_to_max"] = key
            lines.append(f"🏆 **First in the group to hit {max_level}!**")
        elif level >= max_level > old_level:
            lines.append(f"⭐ Hit **{max_level}**!")
        elif level - old_level == 1:
            lines.append(f"⬆️ Level **{level}**")
        else:
            lines.append(f"⬆️ Level **{old_level} → {level}**")

    # Professions
    old_p, new_p = old.get("professions"), new.get("professions")
    if old_p is not None and new_p is not None:
        for prof, skill in sorted(new_p.items()):
            if prof not in old_p:
                lines.append(f"📜 Learned **{prof}**")
                continue
            crossed = [m for m in settings["profession_milestones"] if old_p[prof] < m <= skill]
            if crossed:
                lines.append(f"🛠️ **{prof}** reached {max(crossed)} skill")

    # Dungeons
    old_d, new_d = old.get("dungeons"), new.get("dungeons")
    if old_d is not None and new_d is not None:
        for inst, info in sorted(new_d.items()):
            if info["cleared"] and not (old_d.get(inst) or {}).get("cleared"):
                lines.append(f"🗝️ Cleared **{inst}** for the first time")

    # Raids
    old_r, new_r = old.get("raids"), new.get("raids")
    if old_r is not None and new_r is not None:
        for inst, info in sorted(new_r.items()):
            before = old_r.get(inst) or {"cleared": False, "bosses": []}
            if info["cleared"] and not before["cleared"]:
                lines.append(f"🐉 Full clear of **{inst}**!")
            else:
                for boss in info["bosses"]:
                    if boss not in before["bosses"]:
                        lines.append(f"⚔️ First kill: **{boss}** ({inst})")

    # Achievements
    old_a, new_a = old.get("achievements"), new.get("achievements")
    if old_a is not None and new_a is not None:
        for aid, name in sorted(new_a.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0):
            if aid not in old_a:
                lines.append(f"🎖️ {name}")

    return lines


def keep_baselines(old, new):
    """If a feature failed to load this run, keep last run's data for it."""
    for field in ("professions", "dungeons", "raids", "achievements"):
        if new.get(field) is None and old and old.get(field) is not None:
            new[field] = old[field]
    return new


def character_embed(c, snap, lines):
    if len(lines) > 25:  # keep long catch-up posts readable
        lines = lines[:24] + [f"…and {len(lines) - 24} more"]
    describe = " ".join(str(x) for x in (snap.get("race"), snap.get("class")) if x)
    return {
        "title": display_name(c),
        "description": "\n".join(lines),
        "color": CLASS_COLORS.get(snap.get("class"), DEFAULT_COLOR),
        "footer": {"text": f"Level {snap.get('level') or '?'} {describe}".strip()},
    }


# ---------------------------------------------------------------- Discord

def post_to_discord(webhook, embeds, content=None):
    for i in range(0, len(embeds), 10):  # Discord allows 10 embeds per message
        payload = {
            "username": "WoW: Forever Tracker",
            "embeds": embeds[i:i + 10],
            "flags": 4096,                      # @silent: no push/desktop alerts for anyone
            "allowed_mentions": {"parse": []},  # never ping users, roles or @everyone
        }
        if content and i == 0:
            payload["content"] = content
        status, body = http("POST", webhook, {"Content-Type": "application/json"},
                            json.dumps(payload).encode())
        if status not in (200, 204):
            raise RuntimeError(f"Discord webhook failed (HTTP {status}): {body}")


# ---------------------------------------------------------------- Main

def load_json(path, default):
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def load_characters(config):
    """
    Characters can be listed grouped by friend:
      "friends": [{"name": "Tim", "characters": [{"name": "...", "surname": "..."}]}]
    or as a flat list with an "owner" on each:
      "characters": [{"owner": "Tim", "name": "...", "surname": "..."}]
    Both can be used together.
    """
    chars = []
    for friend in config.get("friends") or []:
        for c in friend.get("characters") or []:
            chars.append({"owner": friend.get("name"), **c})
    chars += config.get("characters") or []
    return [c for c in chars if c.get("name")]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="print messages instead of posting; save nothing")
    parser.add_argument("--test-webhook", action="store_true", help="send a test message to Discord and exit")
    args = parser.parse_args()

    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

    if args.test_webhook:
        if not webhook:
            sys.exit("DISCORD_WEBHOOK_URL is not set.")
        post_to_discord(webhook, [{
            "title": "Tracker connected ✅",
            "description": "Progress updates will be posted in this channel every 30 minutes when something changes.",
            "color": DEFAULT_COLOR,
        }])
        print("Test message sent.")
        return

    client_id = os.environ.get("BLIZZARD_CLIENT_ID", "").strip()
    client_secret = os.environ.get("BLIZZARD_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        sys.exit("BLIZZARD_CLIENT_ID and BLIZZARD_CLIENT_SECRET must be set.")
    if not webhook and not args.dry_run:
        sys.exit("DISCORD_WEBHOOK_URL must be set (or use --dry-run).")

    config = load_json(CONFIG_PATH, None) or {}
    characters = load_characters(config)
    if not characters:
        sys.exit("characters.json is missing or has no characters.")
    settings = {**DEFAULT_SETTINGS, **config.get("settings", {})}
    default_region = config.get("region", "us").lower()

    state = load_json(STATE_PATH, {"characters": {}, "group": {}})
    state.setdefault("characters", {})
    state.setdefault("group", {})

    token = get_token(client_id, client_secret)
    embeds, newly_tracked, unavailable = [], [], []

    def check(c):
        region = c.get("region", default_region).lower()
        namespace = os.environ.get("PROFILE_NAMESPACE") or f"profile-forever-{region}"
        try:
            return fetch_snapshot(c, region, token, namespace, settings), None
        except Exception as err:  # one bad character shouldn't stop the rest
            return None, err

    # Fetch in parallel so a run stays well under a minute; results keep config order.
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(check, characters))

    for c, (snap, err) in zip(characters, results):
        key = f"{c.get('region', default_region).lower()}:{char_slug(c)}"
        if err:
            print(f"[warn] {display_name(c)}: {err}")
            continue
        if snap is None:
            unavailable.append(display_name(c))
            continue

        old = state["characters"].get(key)
        if old is None:
            newly_tracked.append(f"{display_name(c)}: level {snap.get('level') or '?'}")
        else:
            lines = find_changes(key, old, snap, settings, state["group"])
            if lines:
                embeds.append(character_embed(c, snap, lines))
        state["characters"][key] = keep_baselines(old, snap)

    if unavailable:
        print("[info] No Blizzard data yet for: " + ", ".join(unavailable))
        print("       (Character lookup may not be live for Forever yet, or the name is misspelled.)")

    if newly_tracked:
        embeds.insert(0, {
            "title": "Now tracking",
            "description": "\n".join(f"• {n}" for n in newly_tracked),
            "color": DEFAULT_COLOR,
        })

    if args.dry_run:
        print(json.dumps(embeds, indent=2, ensure_ascii=False) if embeds else "[dry run] Nothing to post.")
        return

    if embeds:
        post_to_discord(webhook, embeds)
        print(f"Posted updates for {len(embeds)} character(s).")
    else:
        print("No changes since the last check.")

    # Saved only after posting succeeds, so a failed post is retried next run.
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")


if __name__ == "__main__":
    main()
