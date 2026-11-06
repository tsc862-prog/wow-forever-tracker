# WoW: Forever progress tracker

Every 30 minutes, checks your group's WoW: Forever characters and posts one Discord update with everything that changed since the last check:

- ⬆️ Every level-up (🏆 first in the group to 60, ⭐ everyone else at 60)
- 📜 Professions learned, 🛠️ profession skill milestones (75 / 150 / 225 / 300)
- 🗝️ Dungeons cleared for the first time (the final boss listed in `dungeons.json`)
- ⚔️ First kill of each raid boss
- 🎖️ Achievements earned
- 🟣 Every new epic equipped, and 🔵 each character's first blue
- 🐎 New mounts (once per friend, since mounts are account-wide)
- 📅 A weekly recap leaderboard every Sunday evening (first run after 7 pm Pacific)

If nothing changed, it posts nothing. Each character with news gets its own card, colored by class.

Runs free on GitHub Actions. No server, nothing to install.

## Setup (about 15 minutes)

### 1. Discord webhook
In your Discord server: **channel settings → Integrations → Webhooks → New Webhook**. Pick the channel, then **Copy Webhook URL**.

### 2. Blizzard API keys
Go to **develop.battle.net**, sign in, open **API Access**, and create a client. Any name works; the redirect URL can be blank or `http://localhost`. Copy the **Client ID** and **Client Secret**.

### 3. GitHub repo
1. Create a new repository and upload everything in this folder, including the `.github` folder.
2. Go to **Settings → Secrets and variables → Actions → New repository secret** and add:
   - `BLIZZARD_CLIENT_ID`
   - `BLIZZARD_CLIENT_SECRET`
   - `DISCORD_WEBHOOK_URL`
3. Go to **Settings → Actions → General → Workflow permissions** and choose **Read and write permissions** (so it can save progress).

A **public** repo is simplest: unlimited free minutes, and your keys stay hidden in Secrets. A private repo also works (about 1,440 of its 2,000 free minutes a month).

### 4. Your group's characters
Edit `characters.json`. List each friend once with all their characters. Forever names have two parts: the first goes in `name`, the second in `surname`.

```json
"friends": [
  {
    "name": "Tim",
    "characters": [
      { "name": "Erlyn", "surname": "Farthing" },
      { "name": "Bramble", "surname": "Ashvale" }
    ]
  },
  {
    "name": "Mike",
    "characters": [
      { "name": "Korra", "surname": "Ironfen" }
    ]
  }
]
```

Set `"region"` to `us` or `eu`. To add an alt later, just add a line; it's picked up on the next run.

### 5. Test it
**Actions tab → WoW Forever tracker → Run workflow**, tick **Only send a test message**, and run. A "Tracker connected ✅" message should appear in Discord.

Then run it once more without the box ticked. The first check posts a "Now tracking" list and quietly records what everyone already has; from then on it only posts new progress.

## Settings (`characters.json`)

| Setting | What it does |
|---|---|
| `track_levels`, `track_professions`, `track_dungeons`, `track_raids`, `track_achievements` | Set any to `false` to stop posting that kind of update |
| `profession_milestones` | Skill levels that get announced |
| `track_gear`, `track_mounts`, `weekly_recap` | Set to `false` to turn off gear, mount or recap posts |
| `recap_day`, `recap_hour`, `timezone` | When the weekly recap is posted |
| `max_level` | Level cap (60) |

## If nothing shows up

Character lookup for Forever wasn't live on Blizzard's API before launch. If the run log says **"No Blizzard data yet for: …"**, that's why: the tracker keeps checking and starts posting as soon as Blizzard turns it on. Nothing to fix on your end, unless a name is misspelled.

Each feature is checked separately. If Blizzard offers levels but not, say, dungeon data for Forever, you still get level-ups, and dungeon posts start on their own once that data appears (without spamming old clears).

If Blizzard ends up using a different namespace than `profile-forever-us`, add a repository **variable** (not secret) called `PROFILE_NAMESPACE` with the right value. If a character needs a different URL path, add `"api_path": "/profile/wow/character/..."` to that character's line.

## Notes
- Times aren't exact: GitHub can delay scheduled runs by several minutes when busy.
- In a public repo, GitHub turns off schedules after 60 days with no repo activity. The tracker's own commits usually keep it active, but if it stops, re-enable it from the Actions tab.
- Running locally: `python3 tracker.py --dry-run` (with the three variables set) prints what it would post without posting.
