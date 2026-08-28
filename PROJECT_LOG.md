# Stream Radar — Project Log

Build journal. One entry per step: what was done, why, how it was verified. A step isn't done until it's verified.

## Conventions

- **Snapshot key** is the UTC hour (`snapshot_hour_utc`, e.g. `2026-08-27T18`) — intraday cadence, and local runs line up with future scheduled runs.
- **Snapshots** (`data/snapshots/*.csv`) are append-only tidy tables keyed by hour + entity; re-running within the same hour updates rows in place and never duplicates keys.
- **Raw zone** (`data/raw/<hour>/`) keeps verbatim responses for the *small* payloads (top-games lists, Steam chart). Per-stream lists are deliberately NOT kept raw: ~10k stream records per snapshot is ephemeral bulk, so they are aggregated at collect time (totals, concentration shares, language spread, top-3 streamers) and the tidy tables are the record. This is a conscious tradeoff, logged here so it can be defended.
- Data is committed to git: the repo is the historical database.
- Carried over from the Steam collector (previous iteration of this project): 4xx-other-than-429 is permanent → fail fast (that lesson once cost 7 minutes of pointless retries).

## Glossary

- **Category** — Twitch's unit for "what is being streamed"; mostly games, but also Just Chatting, Music, etc. Non-game categories stay in the data (they're competition for attention) and get flagged downstream via IGDB mapping.
- **Concentration (share_top1 / share_top10)** — fraction of a game's viewers watching its single biggest / ten biggest live channels. High = a few big streamers carry it; low = broad organic adoption.
- **Watched-vs-played ratio** — Twitch viewers ÷ Steam concurrent players for the same game (needs the IGDB appid mapping).
- **Breakout** (working definition, to be pinned down with data) — a game entering the Twitch top 20 by viewers within a 7-day horizon.

---

## Step 1 — Collector + first live snapshot (started 2026-08-27)

**Goal:** Twitch-first snapshot collector running end to end, because every model in the roadmap trains on history we collect ourselves — the collector must start before anything else matters.

**What:**
- `ingest/collect.py`: one run = one snapshot. Twitch Helix via app token (client-credentials): top-100 games, then per-game `/streams` page (100 streams) reduced to aggregates + top-3 streamers. Steam most-played chart (keyless) rides along in the same snapshot for the watched-vs-played join. Idempotent-per-hour upserts, retry/backoff with 4xx fast-fail, 0.15s Helix spacing (app bucket allows 800 req/min; a run uses ~102).
- `.env.example` / `python-dotenv` for the Twitch credentials (gitignored `.env`).

**Verify (first live snapshots, hour 2026-08-28T01 UTC, three runs):**
- Full run in 23s (~102 Helix calls): 100 twitch_top_games / 100 twitch_game_streams / 296 twitch_top_streams / 100 steam_player_counts rows. 0 duplicate keys in all four tables.
- Live idempotency proof, twice: a `--steam-only` test, the first full run, and the post-fix rerun all landed inside the same UTC hour — every table held its row count with rows updated in place (NIGHT-RUNNERS PROLOGUE's rank even moved 17→16 between runs and was cleanly replaced).
- Board sanity: top 5 = Just Chatting, How to Fish, GTA V, World of Warcraft, League of Legends. "How to Fish" at #2 is a viral moment in progress — the target phenomenon, visible on day one.
- `igdb_id` present for 82/100 categories; the missing 18 are the non-game categories (Just Chatting etc.) → free preview of the is_game flag before IGDB mapping exists.
- Concentration shares: 0 sanity violations (all in (0,1], share_top10 ≥ share_top1); 0 zero-viewer games.
- 296 top-stream rows instead of 300 → chased, and it's a finding: two games had exactly ONE live channel each — 'NIGHT-RUNNERS PROLOGUE' (rank 16, 29,024 viewers, share_top1 = 1.0) and 'Yeah! Wow! Hey!' (rank 91, 2,726 viewers). A single mega-streamer can carry a niche game into the top 20: the concentration archetype the organic-vs-manufactured hypothesis is about, captured within minutes of first data.

**Real issues caught:**
1. **Helix's pagination cursor doesn't mean more data.** `truncated` was first defined as "cursor present" and came back True for 100/100 games — including the two with a single live channel, which is impossible. Twitch returns a cursor even on exhausted result sets. Redefined as "full page returned" (`len(data) == 100`): now 37/100 — and therefore for 63 of 100 games, `viewers_top100` is the *complete* live viewer count, not a truncated proxy. For the 37 full-page games (big categories), viewers and concentration shares are top-100-of-category metrics: consistently defined, comparable across games and time, but undercounts of the long tail — acceptable for modeling, documented here for honesty.
