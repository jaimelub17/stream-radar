# Stream Radar

Which game is Twitch about to crown next? A data-science project that watches live Twitch viewership and Steam player counts, and learns to call breakout games **before** they peak.

Streamers and video creators constantly ask "what should I cover next?" — by the time a game tops the charts, that window is gone. Stream Radar snapshots the live rankings every couple of hours and turns its own accumulated history into predictive signals.

## The science (what makes this more than a dashboard)

- **Breakout prediction.** Label: *does a game enter the Twitch top 20 by viewers within the next 7 days?* Features from collected history: viewer/channel momentum, rank velocity, Steam player growth, concentration shifts. Models are backtested on held-out weeks with calibration reported honestly.
- **Organic vs. manufactured growth.** Viewer *concentration* (top-1 / top-10 streamer share) separates a sponsored mega-streamer spike from hundreds of small channels adopting a game organically. Hypothesis to test: breadth leads durable breakouts, concentration spikes decay.
- **Watched vs. played.** Twitch viewers ÷ Steam players. Esports run high (watched, not played), cozy games run low (played, not watched) — and a rising ratio is a candidate leading indicator of content-driven booms.

## How it works

```
Twitch Helix API + Steam charts ──► ingest/collect.py (every ~2h, scheduled)
                                        │
                                        ▼
                          data/ (append-only snapshots, versioned in git)
                                        │
                                        ▼
                          models: momentum baselines → backtested classifier
                                        │
                                        ▼
                          breakout leaderboard + findings write-ups
```

Snapshots are keyed by UTC hour and idempotent (a retry replaces that hour's rows, never duplicates them). Full stream lists are aggregated at collect time — viewers, channels, concentration shares, language spread, top-3 streamers per game — because the per-stream firehose is ephemeral bulk; the aggregates are the record.

## Data sources

| Source | Signal | Access |
|---|---|---|
| Twitch Helix `/games/top` | Top 100 categories by live viewers | Free app token |
| Twitch Helix `/streams` | Per-game viewers, channels, concentration, languages, top streamers | Free app token |
| Steam `GetMostPlayedGames` | Top 100 games by players (rank, last-week rank, daily peak) | Free, keyless |
| IGDB `external_games` (planned) | Twitch game ↔ Steam appid mapping | Same Twitch token |

## Running it locally

```
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env   (fill in your Twitch app credentials)
run.cmd
```

## Notes

Not affiliated with Twitch, Valve, or IGDB. Public APIs, polite rate limiting, research/portfolio use.
