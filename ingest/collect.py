"""Snapshot collector for Stream Radar.

Each run captures one intraday snapshot (keyed by UTC hour) of:

  1. Twitch top 100 game categories by live viewers   (Helix /games/top)
  2. Per-game live-stream aggregates: total viewers, channel count,
     concentration (top-1 / top-10 share), language spread, and the
     top 3 streamers                                   (Helix /streams)
  3. Steam top 100 most-played chart                   (keyless)

Outputs:

  data/raw/<hour>/*.json                    verbatim small API responses
  data/snapshots/<table>/<YYYY-MM>.csv      monthly partitions, one row per
                                            entity per snapshot hour
  data/catalog/igdb_steam_map.csv           IGDB game id -> Steam appid
                                            (catalog: queried once per game)
  data/catalog/manual_map.csv               hand-maintained twitch_game_id ->
                                            steam_appid overrides for games
                                            Twitch carries no igdb_id for

Tables: twitch_top_games, twitch_game_streams, twitch_top_streams,
steam_player_counts.

Design notes:
- Full stream lists (~100 games x 100 streams, every run) are ephemeral
  bulk, so they are aggregated at collect time; the tidy tables ARE the
  record for streams.
- Runs are idempotent per UTC hour: a retry inside the same hour replaces
  that hour's rows instead of duplicating them, and duplicate keys within
  one batch are dropped (first occurrence wins) so an upstream API
  repeating an entry cannot corrupt the table.
- Schema evolution is additive: new columns backfill as empty for old
  rows. Removing or renaming a column silently drops the old values on
  the next rewrite of that partition - migrate by hand if they matter.
- A failed per-game stream fetch skips that game and the run continues;
  the run only fails if every fetch failed.

Twitch credentials (free): register an app at dev.twitch.tv/console/apps,
then put TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET in .env (see .env.example)
or the process environment.
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

IGDB_BASE = "https://api.igdb.com/v4"
IGDB_STEAM_SOURCE = 1  # external_games source id for Steam
IGDB_MAP_PATH = ROOT / "data" / "catalog" / "igdb_steam_map.csv"
IGDB_REQUERY_EMPTY_DAYS = 7  # no-Steam verdicts expire: pre-release games gain a store page later

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "stream-radar (portfolio research project)"


# ---------------------------------------------------------------- http

def request_json(method: str, url: str, *, params: dict | None = None, data: dict | str | None = None,
                 headers: dict | None = None, attempts: int = 4, base_wait: float = 5.0):
    """Request with retry/backoff; 4xx other than 429 is permanent -> fail fast.
    (Lesson from the Steam collector: unretryable 404s once burned 7 minutes.)"""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            resp = SESSION.request(method, url, params=params, data=data, headers=headers, timeout=30)
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


def get_json(url: str, params: dict | None = None, headers: dict | None = None):
    return request_json("GET", url, params=params, headers=headers)


# ---------------------------------------------------------------- storage

def dump_raw(hour_dir: Path, name: str, payload) -> None:
    hour_dir.mkdir(parents=True, exist_ok=True)
    (hour_dir / name).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def upsert_csv(table: str, month: str, fieldnames: list[str], key_fields: list[str],
               new_rows: list[dict]) -> tuple[int, int]:
    """Upsert rows into the table's monthly partition file.

    - Duplicate keys within new_rows are dropped (first occurrence wins).
    - Existing rows whose key matches an incoming row are replaced.
    - Existing rows are projected onto the current fieldnames, so a later
      column addition never crashes old partitions (removed columns are
      dropped on rewrite - see module docstring).
    Returns (rows_written, partition_total)."""
    deduped: list[dict] = []
    seen: set[tuple] = set()
    for r in new_rows:
        key = tuple(str(r[k]) for k in key_fields)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    if len(deduped) < len(new_rows):
        print(f"  WARNING: {table}: dropped {len(new_rows) - len(deduped)} duplicate-key rows within this batch")

    path = SNAP_DIR / table / f"{month}.csv"
    existing: list[dict] = []
    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            existing = list(csv.DictReader(f))
    kept = [
        {k: r.get(k, "") for k in fieldnames}
        for r in existing
        if tuple(str(r.get(k, "")) for k in key_fields) not in seen
    ]
    rows = kept + [{k: r.get(k, "") for k in fieldnames} for r in deduped]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return len(deduped), len(rows)


# ---------------------------------------------------------------- twitch

class TwitchClient:
    """App-token (client credentials) Helix client for public data."""

    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token: str | None = None

    def _authenticate(self) -> None:
        payload = request_json(
            "POST", TOKEN_URL,
            data={"client_id": self.client_id, "client_secret": self.client_secret,
                  "grant_type": "client_credentials"},
        )
        self.token = payload["access_token"]

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

    def igdb(self, endpoint: str, body: str) -> list:
        """Query the IGDB API (operated by Twitch - same credentials/token).
        body is an APIcalypse query string."""
        if self.token is None:
            self._authenticate()
        headers = {"Client-Id": self.client_id, "Authorization": f"Bearer {self.token}"}
        try:
            return request_json("POST", f"{IGDB_BASE}/{endpoint}", data=body, headers=headers)
        except RuntimeError as exc:
            if "HTTP 401" in str(exc):
                self._authenticate()
                headers["Authorization"] = f"Bearer {self.token}"
                return request_json("POST", f"{IGDB_BASE}/{endpoint}", data=body, headers=headers)
            raise


def collect_twitch(twitch: TwitchClient, hour: str, month: str, hour_dir: Path, sleep_s: float) -> list[str]:
    payload = twitch.get("/games/top", {"first": 100})
    dump_raw(hour_dir, "twitch_top_games.json", payload)
    games = payload.get("data", [])

    game_rows: list[dict] = []
    stream_agg_rows: list[dict] = []
    top_stream_rows: list[dict] = []
    failures: list[str] = []

    for rank, game in enumerate(games, start=1):
        game_id = game["id"]
        game_rows.append(
            {"snapshot_hour_utc": hour, "rank": rank, "game_id": game_id,
             "igdb_id": game.get("igdb_id", ""), "name": game.get("name", "")}
        )
        try:
            streams = twitch.get("/streams", {"game_id": game_id, "first": 100})
        except RuntimeError as exc:
            failures.append(f"{game.get('name', game_id)}: {exc}")
            time.sleep(sleep_s)
            continue
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

    if failures:
        print(f"  WARNING: {len(failures)} of {len(games)} stream fetches failed; skipped those games:")
        for line in failures[:5]:
            print(f"    {line}")
        if len(failures) == len(games):
            raise RuntimeError("every stream fetch failed - aborting so the run is marked failed")

    written, total_rows = upsert_csv(
        "twitch_top_games", month,
        ["snapshot_hour_utc", "rank", "game_id", "igdb_id", "name"],
        ["snapshot_hour_utc", "game_id"],
        game_rows,
    )
    print(f"twitch_top_games: {written} rows for {hour} (partition {month} now {total_rows})")
    written, total_rows = upsert_csv(
        "twitch_game_streams", month,
        ["snapshot_hour_utc", "game_id", "viewers_top100", "channels_top100",
         "truncated", "share_top1", "share_top10", "n_languages"],
        ["snapshot_hour_utc", "game_id"],
        stream_agg_rows,
    )
    print(f"twitch_game_streams: {written} rows (partition {month} now {total_rows})")
    written, total_rows = upsert_csv(
        "twitch_top_streams", month,
        ["snapshot_hour_utc", "game_id", "position", "user_login", "viewer_count", "language", "started_at"],
        ["snapshot_hour_utc", "game_id", "position"],
        top_stream_rows,
    )
    print(f"twitch_top_streams: {written} rows (partition {month} now {total_rows})")
    return [r["igdb_id"] for r in game_rows]


def update_igdb_map(twitch: TwitchClient, igdb_ids: list[str]) -> None:
    """Map IGDB game ids to Steam appids via IGDB external_games.

    Catalog-style, not a snapshot: only ids not yet in the map are queried.
    Games with no Steam release are recorded with an empty steam_appid so
    they aren't re-queried every run - but an empty verdict expires after
    IGDB_REQUERY_EMPTY_DAYS while the game still charts, because a
    pre-release game (the viral-launch cohort) gains its Steam page later.
    A game can map to several appids (editions); all are kept."""
    wanted = sorted({i for i in igdb_ids if i and i != "0" and str(i).isdigit()}, key=int)
    rows: list[dict] = []
    if IGDB_MAP_PATH.exists():
        with IGDB_MAP_PATH.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    known = {r["igdb_id"] for r in rows}
    mapped = {r["igdb_id"] for r in rows if r["steam_appid"]}
    cutoff = datetime.now(timezone.utc).timestamp() - IGDB_REQUERY_EMPTY_DAYS * 86400
    stale_empty: set[str] = set()
    for r in rows:
        if r["igdb_id"] in mapped or r["igdb_id"] not in set(wanted):
            continue
        try:
            fetched = datetime.fromisoformat(r["fetched_at_utc"]).timestamp()
        except ValueError:
            fetched = 0.0
        if fetched < cutoff:
            stale_empty.add(r["igdb_id"])
    missing = [i for i in wanted if i not in known] + sorted(stale_empty, key=int)
    if not missing:
        print(f"igdb_map: no new ids (map covers {len(known)} games)")
        return
    rows = [r for r in rows if r["igdb_id"] not in stale_empty]  # expired verdicts get replaced
    now_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    found: dict[str, list[str]] = {}
    batches = [missing[i:i + 100] for i in range(0, len(missing), 100)]
    while batches:
        batch = batches.pop()
        body = (f"fields game,uid,external_game_source; "
                f"where game = ({','.join(batch)}) & external_game_source = {IGDB_STEAM_SOURCE}; limit 500;")
        result = twitch.igdb("external_games", body)
        if len(result) >= 500 and len(batch) > 1:
            # response hit the row cap: rows past it were silently dropped, and a
            # game whose rows all fell past the cap would be mislabeled "no Steam"
            mid = len(batch) // 2
            batches += [batch[:mid], batch[mid:]]
            print(f"  WARNING: igdb batch of {len(batch)} hit the 500-row cap - splitting")
            continue
        for entry in result:
            found.setdefault(str(entry.get("game", "")), []).append(str(entry.get("uid", "")).strip())
        time.sleep(0.3)  # IGDB allows 4 req/s
    with_steam = 0
    for igdb_id in missing:
        uids = sorted(set(found.get(igdb_id, [])))
        if uids:
            with_steam += 1
            for uid in uids:
                if not uid.isdigit():
                    print(f"  WARNING: igdb {igdb_id}: non-numeric Steam uid '{uid}'")
                rows.append({"igdb_id": igdb_id, "steam_appid": uid, "fetched_at_utc": now_utc})
        else:
            rows.append({"igdb_id": igdb_id, "steam_appid": "", "fetched_at_utc": now_utc})
    IGDB_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    with IGDB_MAP_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["igdb_id", "steam_appid", "fetched_at_utc"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"igdb_map: queried {len(missing)} new ids, {with_steam} have Steam appids "
          f"(map now covers {len(known) + len(missing)} games)")


# ---------------------------------------------------------------- steam

def collect_steam(hour: str, month: str, hour_dir: Path) -> None:
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
        "steam_player_counts", month,
        ["snapshot_hour_utc", "appid", "rank", "last_week_rank", "peak_in_game"],
        ["snapshot_hour_utc", "appid"],
        rows,
    )
    print(f"steam_player_counts: {written} rows for {hour} (partition {month} now {total_rows})")


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
    month = hour[:7]
    hour_dir = RAW_DIR / hour
    print(f"Collecting snapshot {hour} (UTC)")

    collect_steam(hour, month, hour_dir)

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
        twitch = TwitchClient(client_id, client_secret)
        igdb_ids = collect_twitch(twitch, hour, month, hour_dir, args.twitch_sleep)
        try:
            update_igdb_map(twitch, igdb_ids)
        except Exception as exc:  # best-effort enrichment must never cost the snapshot
            print(f"  WARNING: igdb map update failed, continuing without it: {exc!r}")

    print(f"Done in {time.monotonic() - started:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
