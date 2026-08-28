"""Snapshot collector for Stream Radar.

Each run captures one intraday snapshot (keyed by UTC hour) of:

  1. Twitch top 100 game categories by live viewers   (Helix /games/top)
  2. Per-game live-stream aggregates: total viewers, channel count,
     concentration (top-1 / top-10 share), language spread, and the
     top 3 streamers                                   (Helix /streams)
  3. Steam top 100 most-played chart                   (keyless)

Outputs:

  data/raw/<hour>/twitch_top_games.json   verbatim Helix response
  data/snapshots/twitch_top_games.csv     one row per game per snapshot
  data/snapshots/twitch_game_streams.csv  per-game aggregates per snapshot
  data/snapshots/twitch_top_streams.csv   top-3 streamers per game per snapshot
  data/snapshots/steam_player_counts.csv  one row per app per snapshot

Design note: full stream lists (~100 games x 100 streams, every run) are
ephemeral bulk, so unlike the raw-everything pattern they are aggregated
at collect time; the tidy tables ARE the record for streams. Runs are
idempotent per UTC hour: a retry inside the same hour replaces that
hour's rows instead of duplicating them.

Twitch credentials (free): register an app at dev.twitch.tv/console/apps,
then put TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET in .env (see .env.example).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"
SNAP_DIR = ROOT / "data" / "snapshots"

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
HELIX_BASE = "https://api.twitch.tv/helix"
STEAM_MOST_PLAYED_URL = "https://api.steampowered.com/ISteamChartsService/GetMostPlayedGames/v1/"

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "stream-radar (portfolio research project)"


# ---------------------------------------------------------------- shared io

def dump_raw(hour_dir: Path, name: str, payload) -> None:
    hour_dir.mkdir(parents=True, exist_ok=True)
    (hour_dir / name).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def upsert_csv(path: Path, fieldnames: list[str], key_fields: list[str], new_rows: list[dict]) -> tuple[int, int]:
    """Append-only table; rows whose key matches an incoming row are replaced,
    so a retry within the same snapshot hour never duplicates keys."""
    existing: list[dict] = []
    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            existing = list(csv.DictReader(f))
    new_keys = {tuple(str(r[k]) for k in key_fields) for r in new_rows}
    kept = [r for r in existing if tuple(str(r.get(k, "")) for k in key_fields) not in new_keys]
    rows = kept + [{k: r.get(k, "") for k in fieldnames} for r in new_rows]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return len(new_rows), len(rows)


def get_json(url: str, params: dict | None = None, headers: dict | None = None,
             attempts: int = 4, base_wait: float = 5.0):
    """GET with retry/backoff; 4xx other than 429 is permanent -> fail fast.
    (Lesson from the Steam collector: unretryable 404s once burned 7 minutes.)"""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            resp = SESSION.get(url, params=params, headers=headers, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                raise RuntimeError(f"HTTP {resp.status_code} (permanent) from {url}: {resp.text[:200]}")
            if attempt < attempts:
                wait = base_wait * attempt
                print(f"  HTTP {resp.status_code} - retry {attempt}/{attempts} in {wait:.0f}s")
                time.sleep(wait)
                continue
            last_error = RuntimeError(f"HTTP {resp.status_code}")
        except requests.RequestException as exc:
            last_error = exc
            if attempt == attempts:
                break
            wait = base_wait * attempt
            print(f"  {exc.__class__.__name__} - retry {attempt}/{attempts} in {wait:.0f}s")
            time.sleep(wait)
    raise RuntimeError(f"gave up on {url}: {last_error}")


# ---------------------------------------------------------------- twitch

class TwitchClient:
    """App-token (client credentials) Helix client for public data."""

    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token: str | None = None

    def _authenticate(self) -> None:
        resp = SESSION.post(
            TOKEN_URL,
            data={"client_id": self.client_id, "client_secret": self.client_secret,
                  "grant_type": "client_credentials"},
            timeout=30,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Twitch auth failed (HTTP {resp.status_code}): {resp.text[:200]}")
        self.token = resp.json()["access_token"]

    def get(self, path: str, params: dict) -> dict:
        if self.token is None:
            self._authenticate()
        headers = {"Client-Id": self.client_id, "Authorization": f"Bearer {self.token}"}
        try:
            return get_json(HELIX_BASE + path, params=params, headers=headers)
        except RuntimeError as exc:
            if "HTTP 401" in str(exc):  # token expired mid-run - refresh once
                self._authenticate()
                headers["Authorization"] = f"Bearer {self.token}"
                return get_json(HELIX_BASE + path, params=params, headers=headers)
            raise


def collect_twitch(twitch: TwitchClient, hour: str, hour_dir: Path, sleep_s: float) -> None:
    payload = twitch.get("/games/top", {"first": 100})
    dump_raw(hour_dir, "twitch_top_games.json", payload)
    games = payload.get("data", [])

    game_rows: list[dict] = []
    stream_agg_rows: list[dict] = []
    top_stream_rows: list[dict] = []

    for rank, game in enumerate(games, start=1):
        game_id = game["id"]
        game_rows.append(
            {"snapshot_hour_utc": hour, "rank": rank, "game_id": game_id,
             "igdb_id": game.get("igdb_id", ""), "name": game.get("name", "")}
        )
        streams = twitch.get("/streams", {"game_id": game_id, "first": 100})
        data = streams.get("data", [])
        viewers = [s.get("viewer_count", 0) for s in data]
        total = sum(viewers)
        stream_agg_rows.append(
            {
                "snapshot_hour_utc": hour,
                "game_id": game_id,
                "viewers_top100": total,
                "channels_top100": len(data),
                # full page => true channel count may exceed 100. NOT the pagination
                # cursor: Helix returns one even on exhausted result sets.
                "truncated": len(data) == 100,
                "share_top1": round(viewers[0] / total, 4) if total else "",
                "share_top10": round(sum(viewers[:10]) / total, 4) if total else "",
                "n_languages": len({s.get("language", "") for s in data}),
            }
        )
        for pos, s in enumerate(data[:3], start=1):
            top_stream_rows.append(
                {
                    "snapshot_hour_utc": hour,
                    "game_id": game_id,
                    "position": pos,
                    "user_login": s.get("user_login", ""),
                    "viewer_count": s.get("viewer_count", ""),
                    "language": s.get("language", ""),
                    "started_at": s.get("started_at", ""),
                }
            )
        time.sleep(sleep_s)

    written, total_rows = upsert_csv(
        SNAP_DIR / "twitch_top_games.csv",
        ["snapshot_hour_utc", "rank", "game_id", "igdb_id", "name"],
        ["snapshot_hour_utc", "game_id"],
        game_rows,
    )
    print(f"twitch_top_games: {written} rows for {hour} (table now {total_rows})")
    written, total_rows = upsert_csv(
        SNAP_DIR / "twitch_game_streams.csv",
        ["snapshot_hour_utc", "game_id", "viewers_top100", "channels_top100",
         "truncated", "share_top1", "share_top10", "n_languages"],
        ["snapshot_hour_utc", "game_id"],
        stream_agg_rows,
    )
    print(f"twitch_game_streams: {written} rows (table now {total_rows})")
    written, total_rows = upsert_csv(
        SNAP_DIR / "twitch_top_streams.csv",
        ["snapshot_hour_utc", "game_id", "position", "user_login", "viewer_count", "language", "started_at"],
        ["snapshot_hour_utc", "game_id", "position"],
        top_stream_rows,
    )
    print(f"twitch_top_streams: {written} rows (table now {total_rows})")


# ---------------------------------------------------------------- steam

def collect_steam(hour: str, hour_dir: Path) -> None:
    payload = get_json(STEAM_MOST_PLAYED_URL)
    dump_raw(hour_dir, "steam_most_played.json", payload)
    ranks = payload["response"]["ranks"]
    rows = [
        {
            "snapshot_hour_utc": hour,
            "appid": r["appid"],
            "rank": r.get("rank", ""),
            "last_week_rank": r.get("last_week_rank", ""),
            "peak_in_game": r.get("peak_in_game", ""),
        }
        for r in ranks
    ]
    written, total_rows = upsert_csv(
        SNAP_DIR / "steam_player_counts.csv",
        ["snapshot_hour_utc", "appid", "rank", "last_week_rank", "peak_in_game"],
        ["snapshot_hour_utc", "appid"],
        rows,
    )
    print(f"steam_player_counts: {written} rows for {hour} (table now {total_rows})")


# ---------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description="Collect one Stream Radar snapshot.")
    parser.add_argument("--steam-only", action="store_true", help="skip Twitch (no credentials needed)")
    parser.add_argument("--twitch-sleep", type=float, default=0.15,
                        help="seconds between Helix calls (app bucket allows 800/min)")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    load_dotenv(ROOT / ".env")
    started = time.monotonic()
    hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
    hour_dir = RAW_DIR / hour
    print(f"Collecting snapshot {hour} (UTC)")

    collect_steam(hour, hour_dir)

    if args.steam_only:
        print("(--steam-only: Twitch skipped)")
    else:
        client_id = os.environ.get("TWITCH_CLIENT_ID", "")
        client_secret = os.environ.get("TWITCH_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            print("ERROR: TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET not set.")
            print("Register a (free) app at https://dev.twitch.tv/console/apps,")
            print("then copy .env.example to .env and fill both values in.")
            return 2
        collect_twitch(TwitchClient(client_id, client_secret), hour, hour_dir, args.twitch_sleep)

    print(f"Done in {time.monotonic() - started:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
