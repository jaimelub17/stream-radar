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

---

## Step 2 — Review hardening + monthly partitions (2026-08-28)

**Goal:** fix all five findings from the high-effort code review before the collector runs unattended — two confirmed cron-killers, two data-integrity landmines, one unbounded-growth design flaw.

**What:**
- A failed per-game `/streams` fetch now skips that game and continues (the game keeps its rank row, just no aggregates that hour); the run fails only if *every* fetch failed. Previously one permanent 404 mid-loop threw away all 100 games' data for the snapshot.
- Token requests go through the same retry/backoff as every other network call (`request_json` refactor). Bad credentials still fail fast (401 is permanent by design).
- `upsert_csv` drops duplicate keys *within* a batch (first occurrence wins, with a warning) — defense against upstream APIs repeating entries, which Steam's featured lists actually did on 2026-08-05.
- Existing rows are projected onto the current fieldnames on rewrite: column adds backfill empty for old rows; removals/renames no longer crash old partitions. Schema evolution is additive-preferred, documented in the module docstring.
- Snapshots moved to monthly partitions `data/snapshots/<table>/<YYYY-MM>.csv`, bounding per-run rewrite cost to one month instead of all history forever. August data migrated in place.

**Verify:**
- Synthetic tests (scratchpad, against the real `upsert_csv`): intra-batch dedupe first-wins with warning; same-hour replace; column removal doesn't crash; column add backfills empty — ALL PASS.
- Live run (22s), landing in the same UTC hour as Step 1's runs: partition files written, 0 duplicate keys across all four tables, growth only by union-of-observations (102 games / 104 Steam apps seen within hour 2026-08-28T01 across four runs — the same chart-rotation mechanism as Step 1's Corsair Cove case, now at scale).

---

## Step 3 — Self-refreshing via GitHub Actions (2026-08-28)

**Goal:** history must accumulate without a PC being on. GitHub Actions runs the collector every 2 hours and commits what it collects — the public repo becomes a self-updating dataset.

**What:**
- `.github/workflows/collect.yml`: cron `17 */2 * * *` (minute offset because GitHub delays top-of-hour crons), plus manual `workflow_dispatch`. Ubuntu runner, Python 3.12, collector credentials from encrypted repo secrets, then `data/` committed as github-actions[bot] (skips empty commits, rebases before push). `concurrency: collect` prevents overlapping runs.
- Public repo `github.com/jaimelub17/stream-radar`; README badge shows collect status. Twitch credentials uploaded via GitHub's encrypted-secrets API (libsodium sealed box against the repo public key), never through the browser UI.

**Verify (2026-08-28):**
- Repo created through the GitHub API using the machine's stored git credential (gh CLI isn't installed); all commits pushed including the workflow file (the stored token carries the `workflow` scope, without which that push is rejected).
- Both secrets uploaded via the encrypted-secrets API → `created`; `workflow_dispatch` accepted (HTTP 204).
- First cloud run: **success** (actions/runs/33133516905). The bot committed `snapshot 2026-08-28T01:39Z`.
- That run landed *inside the same UTC hour* as the local Step 1/2 runs — so the runner upserted hour 01 in place (801 insertions / 786 deletions) and the pulled table still has 0 duplicate keys. Cloud and local collectors colliding on one snapshot hour and producing a clean table is the cross-machine idempotency proof.
- Cron fires every 2 hours at :17 UTC from here on; the repo now grows on its own.

---

## Step 4 — IGDB mapping + first watched-vs-played join (2026-08-28)

**Goal:** connect Twitch categories to Steam appids so the viewership and playerbase series can join — the watched-vs-played ratio needs it, and so does every cross-platform model after it.

**What:**
- `update_igdb_map()` in the collector: IGDB `external_games` queried in batches of <=100 ids with the same Twitch app token (IGDB is Twitch-operated), filtered to `external_game_source = 1` (Steam). Catalog semantics: each igdb_id queried once ever; games with no Steam release recorded with an empty appid so they aren't re-queried; multi-appid games (editions) keep all rows.
- `data/catalog/manual_map.csv`: hand-kept twitch_game_id -> steam_appid overrides for categories Twitch carries no igdb_id for. Manual rows win over IGDB on conflict.
- Probe before code: verified the post-2024 field name `external_game_source` works, and spot-checked GTA V -> 271590, Rust -> 252490, World of Warcraft -> no Steam entry (Battle.net). All correct.

**Verify (snapshot 2026-08-28T05):**
- 84 igdb_ids queried: 63 map to Steam appids, 21 legitimately don't (launcher exclusives, non-games). 91 map rows total — the extras are multi-edition games, kept by design.
- With overrides, 65/100 of the hour's Twitch top-100 carry a Steam appid; 38 also sit in Steam's top-100 chart.
- The first ratio table validates the project thesis immediately: Escape from Tarkov at watch/play **1.381** (more Twitch viewers than the game's daily Steam peak — a "watch game", and top1_share 0.80 says one streamer is carrying it) versus CS2 at **0.019** and Dota 2 at **0.021** (played, not watched). Both extremes exist only via the manual map.

**Real issues caught:**
1. **Twitch's igdb_id coverage fails exactly where it hurts most.** 16 categories in the hour lack an igdb_id; most are non-games (Just Chatting, Music, Slots), but CS2 — filed under the legacy "Counter-Strike" category (32399) — and Dota 2 (29595) are in that set. Steam's two biggest games would have silently missed every join. Lesson: never assume a platform's own foreign keys cover the head of the distribution; verify against the entities that matter most.
2. **Local runs race the cron.** A local collect ran while remote state was assumed current; with bot commits possible at any :17 odd hour, partition CSVs can rebase-conflict. Practice adopted: pull before any local run; if a conflict threatens, discard local data rows, pull, re-collect — idempotent upserts make the re-run free (~23s).
3. **The 2-hour cron hasn't fired yet** (03:17 and 05:17 produced no runs; workflow state is "active" and the manual dispatch was green). Consistent with GitHub's known new-repo scheduler lag / load-shedding of short-interval crons. Watching the 07:17 UTC slot before changing anything.

---

## Step 5 — Breakout Watch: momentum leaderboard + ignition alerts (2026-08-28)

**Goal:** the first predictive surface — rank which games are blowing up next (the PEAK / Big Walk pattern), with streamer influence modeled explicitly, since one big streamer picking up a small game is how many blowups start.

**What:**
- `analyze/momentum.py` → `reports/breakout_watch.md` + `.csv` (per-game feature/component dump — the future training table). Momentum score v0.1 = weighted sum of: viewer and channel log-growth over ~6h / ~24h windows (nearest-snapshot fallback while history is young), chart-entry bonuses, rank climb, a breadth bonus when channels grow at least as fast as viewers (organic adoption beats one channel raiding), Steam daily-peak confirmation for mapped games, and **IGNITION** — a top-decile-reach streamer (reach = their max viewers observed anywhere in history) currently on a game outside the top 20.
- **Ignition alerts** section: big-reach streamer holding ≥50% of a small game right now — flagged *before* any growth exists.
- Weights are transparent priors, deliberately not fitted; the plan of record is backtested weights against the label "enters the Twitch top 20 within 7 days" once weeks of history accumulate.
- Wired into the Actions workflow after collection (`git add data reports`) — the repo recommits a fresh leaderboard every cycle.

**Verify:**
- Hand-recomputed the #1 score from raw rows — Rust +3.53: viewers 5,731→16,753 (log 1.073 × 2.0 = +2.15), channels 100→99 (−0.02), rank 52→17 (35 × 0.04 = +1.40), Steam flat (+0.00). Sum matches the reported score exactly.
- The mechanisms fired on real cases on day one: Waterpark Simulator at #2 with ENTRY + IGNITION (vanillamace = 88% of its viewers); alerts also caught moonmoon at 96% of Darkest Dungeon (rank 26). Rust itself shows share_top1 0.33→0.71 alongside its surge — the report exposes concentration so the *quality* of growth is readable, not just the quantity.
- Day-0 honesty, logged as such: with 2 snapshots 4h apart, ENTRY flags are partly chart-rotation noise (uniform +1.50 block) and the 24h window is empty; both self-resolve as history accumulates.
