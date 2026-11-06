#!/usr/bin/env python3
"""
WoW: Forever progress tracker.

Every run checks each character in characters.json against Blizzard's
Profile API, compares with the last check (state.json), and posts one
Discord message with everything that changed since then:

  - level-ups (and who was first to max level)
  - professions learned, and profession skill milestones
  - dungeons cleared for the first time
  - dungeons cleared for the first time (final boss killed, see dungeons.json)
  - raid bosses killed for the first time (from boss-kill statistics)
  - achievements earned
  - gear: every new epic/legendary equipped, and a character's first blue
  - mounts: new mounts (mounts are account-wide, so posted once per friend)

Once a week (Sunday evening by default) the first run after the recap time
also posts a weekly recap: a leaderboard with each character's progress
since the last recap.

Anything a character already had when it was first tracked is recorded
quietly, so only new progress gets posted. All posts are silent (no
notifications). Uses only the Python standard library.

Environment variables:
  BLIZZARD_CLIENT_ID      Battle.net API client ID
  BLIZZARD_CLIENT_SECRET  Battle.net API client secret
  DISCORD_WEBHOOK_URL     Discord channel webhook URL
  PROFILE_NAMESPACE       optional override, default profile-forever-{region}

Usage:
  python3 tracker.py                 normal run
  python3 tracker.py --dry-run       print what would be posted, save nothing
  python3 tracker.py --test-webhook  send a test message to Discord and exit
  python3 tracker.py --recap-now     post the weekly recap now (from saved data)
"""

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "characters.json"
STATE_PATH = ROOT / "state.json"
DUNGEONS_PATH = ROOT / "dungeons.json"
USER_AGENT = "WoWForeverTracker/1.2"
DEFAULT_COLOR = 0x5865F2
RECAP_COLOR = 0xF1C40F

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

QUALITY_RANK = {"POOR": 0, "COMMON": 1, "UNCOMMON": 2, "RARE": 3, "EPIC": 4, "LEGENDARY": 5, "ARTIFACT": 6}
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

DEFAULT_SETTINGS = {
    "max_level": 60,
    "track_levels": True,
    "track_professions": True,
    "profession_milestones": [75, 150, 225, 300],
    "track_dungeons": True,
    "track_raids": True,
    "track_achievements": True,
    "track_gear": True,
    "track_mounts": True,
    "weekly_recap": True,
    "recap_day": "sunday",
    "recap_hour": 19,
    "timezone": "America/Los_Angeles",
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


def parse_equipment(body):
    """{item id: {"name", "quality", "slot"}} for blue-or-better items currently equipped."""
    if not isinstance(body, dict):
        return None
    out = {}
    for it in body.get("equipped_items") or []:
        quality = ((it.get("quality") or {}).get("type") or "").upper()
        if QUALITY_RANK.get(quality, 0) < QUALITY_RANK["RARE"]:
            continue
        item_id = str((it.get("item") or {}).get("id") or it.get("name"))
        out[item_id] = {
            "name": it.get("name") or name_of(it) or "an item",
            "quality": quality,
            "slot": (it.get("slot") or {}).get("name") or "",
        }
    return out


def parse_mounts(body):
    """{mount id: name} for mounts the account has collected."""
    if not isinstance(body, dict):
        return None
    out = {}
    for m in body.get("mounts") or []:
        mount = m.get("mount") or {}
        if mount.get("id") is not None:
            out[str(mount["id"])] = name_of(mount) or "a mount"
    return out


BOSS_KILL_STAT = re.compile(r"^(?P<boss>.+?)\s+kills\s*\((?P<instance>.+)\)\s*$", re.IGNORECASE)


def parse_boss_kills(body):
    """
    {statistic id: {"boss", "instance", "count"}} for every boss the character
    has killed at least once, read from the "Boss Kills" statistics
    (e.g. "Bazzalan kills (Ragefire Chasm)"). Forever doesn't fill in the
    dungeon/raid encounter data, but it does keep these kill counts.
    """
    if not isinstance(body, dict):
        return None
    out = {}

    def walk(categories, in_boss_kills=False):
        for cat in categories or []:
            boss_cat = in_boss_kills or "boss kills" in (name_of(cat) or "").lower()
            for stat in cat.get("statistics") or []:
                qty = stat.get("quantity") or 0
                match = BOSS_KILL_STAT.match(name_of(stat) or "")
                if qty > 0 and match and (boss_cat or "kills (" in (name_of(stat) or "").lower()):
                    out[str(stat.get("id"))] = {
                        "boss": match.group("boss").strip(),
                        "instance": match.group("instance").strip(),
                        "count": int(qty),
                    }
            walk(cat.get("sub_categories"), boss_cat)

    walk(body.get("categories"))
    return out


def norm(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


class Dungeons:
    """Final bosses from dungeons.json: a final-boss kill counts as a dungeon clear."""

    def __init__(self, data=None):
        data = data or {}
        self.final = {}      # normalized final boss name -> dungeon name
        self.unknown = set()  # normalized dungeon names whose final boss isn't known yet
        self.known = set()   # normalized names of every listed dungeon
        for d in data.get("dungeons") or []:
            self.known.add(norm(d["name"]))
            if d.get("final_boss"):
                self.final[norm(d["final_boss"])] = d["name"]
            else:
                self.unknown.add(norm(d["name"]))
        self.ignore = {str(i) for i in data.get("ignore_stat_ids") or []}

    def classify(self, kill):
        """('clear', dungeon) | ('boss', None) to post a boss kill | (None, None) to stay quiet."""
        dungeon = self.final.get(norm(kill["boss"]))
        if dungeon:
            return "clear", dungeon
        instance = norm(kill["instance"])
        if instance in self.unknown or any(instance and (instance in k or k in instance) for k in self.unknown):
            return "boss", None   # new dungeon with an unknown final boss: show every boss
        if instance in self.known or any(instance and (instance in k or k in instance) for k in self.known):
            return None, None     # a known dungeon's earlier boss: not reliable in Forever, skip
        return "boss", None       # raids and anything unlisted


DUNGEONS = Dungeons()


def cleared_dungeons(snap):
    """Names of dungeons this character has cleared (killed the final boss)."""
    out = set()
    for sid, kill in (snap.get("boss_kills") or {}).items():
        kind, dungeon = DUNGEONS.classify(kill)
        if kind == "clear" and sid not in DUNGEONS.ignore:
            out.add(dungeon)
    return sorted(out)


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
        if settings["track_dungeons"] or settings["track_raids"]:
            snap["boss_kills"] = sub("/achievements/statistics", parse_boss_kills)
        if settings["track_gear"]:
            snap["_equipped"] = sub("/equipment", parse_equipment)
        if settings["track_mounts"]:
            snap["_mounts"] = sub("/collections/mounts", parse_mounts)
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

    # Boss kills, from the boss-kill statistics. A dungeon's final boss posts
    # as a clear; raid bosses (and bosses in new dungeons whose final boss
    # isn't known yet) post as first kills. Mislabeled stats are skipped.
    new_k = new.get("boss_kills")
    if new_k is not None:
        old_k = old.get("boss_kills") or {}
        already = set(cleared_dungeons(old))
        for sid in sorted(new_k, key=lambda k: (new_k[k]["instance"], new_k[k]["boss"])):
            if sid in old_k or sid in DUNGEONS.ignore:
                continue
            kind, dungeon = DUNGEONS.classify(new_k[sid])
            if kind == "clear" and dungeon not in already:
                lines.append(f"🗝️ Cleared **{dungeon}** for the first time")
                already.add(dungeon)
            elif kind == "boss":
                lines.append(f"⚔️ First kill: **{new_k[sid]['boss']}** ({new_k[sid]['instance']})")

    # Achievements
    old_a, new_a = old.get("achievements"), new.get("achievements")
    if old_a is not None and new_a is not None:
        for aid, name in sorted(new_a.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0):
            if aid not in old_a:
                lines.append(f"🎖️ {name}")

    # Gear: every new epic/legendary, and the character's first blue.
    # "gear" in the snapshot is every blue+ item ever seen equipped, so
    # swapping an item off and back on doesn't post it twice.
    old_g, equipped = old.get("gear"), new.get("_equipped")
    if old_g is not None and equipped is not None:
        had_blue = bool(old_g)
        for item_id, it in sorted(equipped.items(), key=lambda kv: -QUALITY_RANK.get(kv[1]["quality"], 0)):
            if item_id in old_g:
                continue
            slot = f" ({it['slot']})" if it["slot"] else ""
            if QUALITY_RANK.get(it["quality"], 0) >= QUALITY_RANK["EPIC"]:
                icon = "🟠" if it["quality"] == "LEGENDARY" else "🟣"
                lines.append(f"{icon} Equipped **{it['name']}**{slot}")
            elif not had_blue:
                lines.append(f"🔵 First blue: **{it['name']}**{slot}")
                had_blue = True

    return lines


def merge_gear(old, new):
    """Fold this run's equipped blue+ items into the character's 'ever seen' list."""
    seen = dict((old or {}).get("gear") or {})
    equipped = new.pop("_equipped", None)
    if equipped is None:
        if old and old.get("gear") is not None:
            new["gear"] = old["gear"]
        return
    for item_id, it in equipped.items():
        seen.setdefault(item_id, {"name": it["name"], "quality": it["quality"]})
    new["gear"] = seen


def keep_baselines(old, new):
    """If a feature failed to load this run, keep last run's data for it."""
    for field in ("professions", "dungeons", "raids", "achievements", "boss_kills"):
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


# ---------------------------------------------------------------- Weekly recap

def local_zone(settings):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(settings["timezone"])
    except Exception:
        return timezone(timedelta(hours=-8))


def last_recap_time(now, settings):
    """The most recent recap moment (e.g. last Sunday 7 pm local) at or before now."""
    zone = local_zone(settings)
    local = now.astimezone(zone)
    target_day = DAYS.index(settings["recap_day"].strip().lower())
    days_back = (local.weekday() - target_day) % 7
    moment = (local - timedelta(days=days_back)).replace(
        hour=int(settings["recap_hour"]), minute=0, second=0, microsecond=0)
    if moment > local:
        moment -= timedelta(days=7)
    return moment


def recap_summary(snap):
    """The numbers a weekly recap compares."""
    return {
        "level": snap.get("level") or 0,
        "dungeons": sorted(set(cleared_dungeons(snap)) |
                           {k for k, v in (snap.get("dungeons") or {}).items() if v.get("cleared")}),
        "bosses": sorted(set(
            [f"{i}|{b}" for i, v in (snap.get("raids") or {}).items() for b in v.get("bosses", [])]
            + [f"{v['instance']}|{v['boss']}" for sid, v in (snap.get("boss_kills") or {}).items()
               if sid not in DUNGEONS.ignore and DUNGEONS.classify(v)[0] == "boss"])),
        "epics": sorted(k for k, v in (snap.get("gear") or {}).items()
                        if QUALITY_RANK.get(v.get("quality"), 0) >= QUALITY_RANK["EPIC"]),
        "professions": dict(snap.get("professions") or {}),
    }


def build_recap(characters, state, settings, week_start):
    weekly = state.setdefault("weekly", {})
    baseline = weekly.setdefault("baseline", {})
    rows, total_levels = [], 0

    for c, key in characters:
        snap = state["characters"].get(key)
        if not snap:
            continue
        now_s = recap_summary(snap)
        base = baseline.get(key) or now_s
        gained = now_s["level"] - base["level"]
        total_levels += max(gained, 0)

        extras = []
        new_dungeons = len(set(now_s["dungeons"]) - set(base["dungeons"]))
        new_bosses = len(set(now_s["bosses"]) - set(base["bosses"]))
        new_epics = len(set(now_s["epics"]) - set(base["epics"]))
        if new_dungeons:
            extras.append(f"🗝️ {new_dungeons} dungeon{'s' * (new_dungeons > 1)}")
        if new_bosses:
            extras.append(f"⚔️ {new_bosses} boss{'es' * (new_bosses > 1)}")
        if new_epics:
            extras.append(f"🟣 {new_epics} epic{'s' * (new_epics > 1)}")
        profs = sorted(now_s["professions"].items(), key=lambda kv: -kv[1])
        prof_text = ", ".join(f"{p} {s}" for p, s in profs if s > 1)[:120]

        line = f"**{display_name(c)}**: Lv {now_s['level']}"
        line += f" (+{gained})" if gained > 0 else " (no change)"
        if extras:
            line += " · " + " · ".join(extras)
        if prof_text:
            line += f"\n  {prof_text}"
        rows.append((now_s["level"], gained, line))

    if not rows:
        return None
    rows.sort(key=lambda r: (-r[1], -r[0]))  # most levels gained this week first
    medals = ["🥇", "🥈", "🥉"]
    body = "\n".join(
        f"{medals[i] if i < 3 and r[1] > 0 else '•'} {r[2]}" for i, r in enumerate(rows))
    return {
        "title": f"📅 Weekly recap: week ending {week_start.strftime('%b %-d')}",
        "description": body[:4000],
        "color": RECAP_COLOR,
        "footer": {"text": f"Group total: +{total_levels} level{'s' * (total_levels != 1)} this week"},
    }


def reset_recap_baseline(characters, state, when):
    weekly = state.setdefault("weekly", {})
    weekly["baseline"] = {key: recap_summary(state["characters"][key])
                          for _, key in characters if key in state["characters"]}
    weekly["last_recap"] = when.astimezone(timezone.utc).isoformat()


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
    parser.add_argument("--recap-now", action="store_true", help="also post the weekly recap on this run")
    args = parser.parse_args()

    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

    if args.test_webhook:
        if not webhook:
            sys.exit("DISCORD_WEBHOOK_URL is not set.")
        post_to_discord(webhook, [{
            "title": "Tracker connected ✅",
            "description": "Progress updates will be posted in this channel when something changes.",
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
    global DUNGEONS
    DUNGEONS = Dungeons(load_json(DUNGEONS_PATH, {}))
    default_region = config.get("region", "us").lower()

    state = load_json(STATE_PATH, {"characters": {}, "group": {}})
    state.setdefault("characters", {})
    state.setdefault("group", {})
    owners = state.setdefault("owners", {})
    weekly = state.setdefault("weekly", {})
    weekly.setdefault("baseline", {})

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

    keyed = []
    for c, (snap, err) in zip(characters, results):
        key = f"{c.get('region', default_region).lower()}:{char_slug(c)}"
        keyed.append((c, key))
        if err:
            print(f"[warn] {display_name(c)}: {err}")
            continue
        if snap is None:
            unavailable.append(display_name(c))
            continue

        old = state["characters"].get(key)
        lines = []
        if old is None:
            newly_tracked.append(f"{display_name(c)}: level {snap.get('level') or '?'}")
        else:
            lines = find_changes(key, old, snap, settings, state["group"])

        # Mounts are account-wide: track them per friend, so a new mount is
        # posted once (on whichever character saw it first), not once per alt.
        mounts = snap.pop("_mounts", None)
        if mounts is not None:
            owner_key = (c.get("owner") or key).lower()
            known = owners.setdefault(owner_key, {}).get("mounts")
            if known is None or old is None:
                owners[owner_key]["mounts"] = {**(known or {}), **mounts}
            else:
                for mid, mname in sorted(mounts.items(), key=lambda kv: kv[1]):
                    if mid not in known:
                        lines.append(f"🐎 New mount: **{mname}**")
                        known[mid] = mname

        if lines:
            embeds.append(character_embed(c, snap, lines))
        merge_gear(old, snap)
        state["characters"][key] = keep_baselines(old, snap)
        weekly["baseline"].setdefault(key, recap_summary(state["characters"][key]))

    if unavailable:
        print("[info] No Blizzard data yet for: " + ", ".join(unavailable))
        print("       (They show up after the character logs into the world once.)")

    if newly_tracked:
        embeds.insert(0, {
            "title": "Now tracking",
            "description": "\n".join(f"• {n}" for n in newly_tracked),
            "color": DEFAULT_COLOR,
        })

    # Weekly recap: posted by the first run after the recap time.
    now = datetime.now(timezone.utc)
    recap_due_at = last_recap_time(now, settings)
    last = weekly.get("last_recap")
    if last is None and not args.recap_now:
        # First run with recaps enabled: start counting from now, recap next week.
        weekly["last_recap"] = now.isoformat()
    elif settings["weekly_recap"] and (args.recap_now or datetime.fromisoformat(last) < recap_due_at):
        recap = build_recap(keyed, state, settings, recap_due_at if not args.recap_now else now.astimezone(local_zone(settings)))
        if recap:
            embeds.append(recap)
        reset_recap_baseline(keyed, state, now)

    if args.dry_run:
        print(json.dumps(embeds, indent=2, ensure_ascii=False) if embeds else "[dry run] Nothing to post.")
        return

    if embeds:
        post_to_discord(webhook, embeds)
        print(f"Posted {len(embeds)} update(s).")
    else:
        print("No changes since the last check.")

    # Saved only after posting succeeds, so a failed post is retried next run.
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")


if __name__ == "__main__":
    main()
