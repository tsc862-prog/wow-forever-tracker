#!/usr/bin/env python3
"""
Checks which parts of Blizzard's API work for WoW: Forever right now.

Usage (normally run from the GitHub Actions "probe" option):
  python3 probe.py "Firstname Secondname" [region]

Prints a report of each endpoint with its HTTP status and, when it works,
which fields come back. It never prints your keys.
"""

import os
import sys
import urllib.parse

import tracker as t


def show(label, status, body=None, note=""):
    mark = "✅" if status == 200 else "❌"
    keys = ""
    if status == 200 and isinstance(body, dict):
        keys = "fields: " + ", ".join(k for k in body if k != "_links")[:300]
    print(f"{mark} {status}  {label}  {keys}{('  ' + note) if note else ''}")


def get(region, path, token, ns):
    return t.api_get(region, path, token, ns)


def main():
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        sys.exit('Give a character name, e.g. python3 probe.py "Erlyn Farthing"')
    parts = sys.argv[1].split()
    char = {"name": parts[0], "surname": " ".join(parts[1:]) or None}
    region = (sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else "us").lower()

    token = t.get_token(os.environ["BLIZZARD_CLIENT_ID"], os.environ["BLIZZARD_CLIENT_SECRET"])
    print("✅ Got a Battle.net token\n")

    # --- Namespaces
    print("== Namespaces ==")
    s, realms = get(region, "/data/wow/realm/index", token, f"dynamic-forever-{region}")
    show(f"dynamic-forever-{region} realm index", s, realms)
    realm_list = (realms or {}).get("realms") or [] if s == 200 else []
    for r in realm_list[:20]:
        print(f"      realm: {t.name_of(r)}  slug={r.get('slug')}  id={r.get('id')}")
    s, b = get(region, "/data/wow/playable-class/index", token, f"static-forever-{region}")
    show(f"static-forever-{region} classes", s, b)

    # --- Character lookup
    slug = t.char_slug(char)
    ns = f"profile-forever-{region}"
    print(f"\n== Character lookup: {sys.argv[1]} ({ns}) ==")
    paths = [f"/profile/wow/character/{slug}"]
    for r in realm_list:
        if r.get("slug"):
            paths.append(f"/profile/wow/character/{r['slug']}/{slug}")
            paths.append(f"/profile/wow/character/{r['slug']}/{urllib.parse.quote(char['name'].lower())}")
    working = None
    for p in dict.fromkeys(paths):
        s, b = get(region, p, token, ns)
        show(p, s, b)
        if s == 200 and working is None:
            working = p
            print(f"      level={b.get('level')} class={t.name_of(b.get('character_class'))} "
                  f"race={t.name_of(b.get('race'))}")

    if not working:
        print("\n❌ No public character lookup works yet. Things to try:")
        print("   - Make sure the character has logged in at least once since launch.")
        print("   - Check the name spelling (both parts).")
        print("   - Blizzard may not have turned it on yet. Re-run later.")
        return

    # --- Detail endpoints the tracker uses
    print(f"\n== Detail endpoints (from {working}) ==")
    for suffix, label in [
        ("/professions", "professions"),
        ("/encounters", "encounters summary"),
        ("/encounters/dungeons", "dungeons"),
        ("/encounters/raids", "raids"),
        ("/achievements", "achievements"),
        ("/equipment", "equipment"),
        ("/statistics", "statistics"),
        ("/reputations", "reputations"),
        ("/collections/mounts", "mounts"),
        ("/collections", "collections index"),
    ]:
        s, b = get(region, working + suffix, token, ns)
        if s == 200 and suffix in ("/encounters/dungeons", "/achievements") and os.environ.get("PROBE_RAW"):
            import json as _j
            print("      RAW " + suffix + ": " + _j.dumps(b)[:3500])
        if s == 200 and suffix in ("/equipment", "/collections/mounts"):
            import json as _json
            items = (b or {}).get("equipped_items") or (b or {}).get("mounts") or []
            print(f"      {len(items)} entries; first two:")
            for it in items[:2]:
                print("      " + _json.dumps(it)[:600])
        note = ""
        if s == 200 and suffix == "/professions":
            note = str(t.parse_professions(b))
        elif s == 200 and suffix.startswith("/encounters"):
            parsed = t.parse_encounters(b) or {}
            note = f"{len(parsed)} instances"
        elif s == 200 and suffix == "/achievements":
            note = f"{len(t.parse_achievements(b) or {})} completed"
        show(label, s, b, note)

    if os.environ.get("PROBE_RAW"):
        print("\n== Statistics with a value ==")
        s, b = get(region, working + "/achievements/statistics", token, ns)
        print(f"   HTTP {s}")
        def walk(cats, path=""):
            for cat in cats or []:
                here = f"{path}/{t.name_of(cat)}"
                for st in cat.get("statistics") or []:
                    if st.get("quantity") or os.environ.get("PROBE_ALL_STATS"):
                        print(f"   {here} :: {t.name_of(st) or st.get('description')} = {st.get('quantity')}  (id {st.get('id')})")
                walk(cat.get("sub_categories"), here)
        if s == 200 and isinstance(b, dict):
            walk(b.get("categories"))
        print("\n== All achievements (done or in progress) ==")
        s, b = get(region, working + "/achievements", token, ns)
        for a in (b or {}).get("achievements") or []:
            print(f"   {'DONE' if a.get('completed_timestamp') else '....'} {a.get('id')} {t.name_of(a.get('achievement'))}")
        for cp in (b or {}).get("category_progress") or []:
            print(f"   category: {t.name_of(cp.get('category'))} quantity={cp.get('quantity')} points={cp.get('points')}")
        for ev in (b or {}).get("recent_events") or []:
            print(f"   recent: {t.name_of(ev.get('achievement'))} at {ev.get('timestamp')}")

    if os.environ.get("PROBE_RAW"):
        import json as _j
        def strip(o):
            if isinstance(o, dict):
                return {k: strip(v) for k, v in o.items() if k not in ("_links", "key", "media", "href", "character")}
            if isinstance(o, list):
                return [strip(v) for v in o]
            return o
        print("\n== Full inventory ==")
        s0, prof = get(region, working, token, ns)
        flat = {k: v for k, v in (prof or {}).items() if not (isinstance(v, dict) and set(v) <= {"href"})}
        print("PROFILE: " + _j.dumps(strip(flat))[:2500])
        for suffix in ("/reputations", "/titles", "/pvp-summary", "/specializations", "/collections/pets",
                       "/collections/toys", "/collections/heirlooms", "/collections/transmogs", "/hunter-pets",
                       "/appearance", "/quests/completed", "/quests", "/character-media", "/mythic-keystone-profile",
                       "/soulbinds", "/status"):
            s1, b1 = get(region, working + suffix, token, ns)
            body = _j.dumps(strip(b1))
            print(f"{suffix} [{s1}] ({len(body)} chars): {body[:1200]}")

    print(f"\n✅ Character lookup works. If the path above isn't the realmless one, "
          f"add  \"api_path\": \"{working}\"  to that character in characters.json.")


if __name__ == "__main__":
    main()
