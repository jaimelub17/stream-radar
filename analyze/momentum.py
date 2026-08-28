"""Breakout Watch: momentum scoring + ignition alerts over collected snapshots.

Reads the snapshot partitions and writes reports/breakout_watch.md (human)
plus reports/breakout_watch.csv (full per-game feature dump, the future
training table). Designed to answer one question: which games are about to
blow up - the PEAK / Big Walk pattern - before they peak?

Signals, per game in the latest snapshot:
  - viewer / channel log-growth vs ~6h and ~24h reference snapshots
    (channel growth is the organic-breadth signal: many streamers adopting
    a game beats one big channel raiding it)
  - chart-entry bonus when a game was absent from a reference snapshot
    (viral hits usually enter from nowhere)
  - rank climb on the Twitch chart
  - breadth bonus when channels grow at least as fast as viewers
  - IGNITION: a top-decile-reach streamer (reach = their max observed
    viewers across all history) currently playing a game outside the
    established top 20 - the "big streamer touches small game" event that
    can cause a blowup; flagged before growth is visible
  - Steam confirmation: daily-peak growth for mapped games

Score = weighted sum of the above. The weights are transparent priors
(v0.1), NOT fitted coefficients: once weeks of history accumulate, they
get replaced by backtested ones (label: "enters Twitch top 20 within 7
days"). Every component is reported so any score can be explained.
"""

from __future__ import annotations

import csv
import math
import statistics
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAP_DIR = ROOT / "data" / "snapshots"
CATALOG_DIR = ROOT / "data" / "catalog"
REPORTS_DIR = ROOT / "reports"

WEIGHTS = {
    "viewer_growth_short": 2.0,   # log-growth vs ~6h ref
    "viewer_growth_day": 1.0,     # log-growth vs ~24h ref
    "channel_growth_short": 1.5,
    "channel_growth_day": 0.75,
    "rank_climb": 0.04,           # per chart position gained vs day ref
    "breadth_bonus": 0.5,         # channels growing >= viewers (both up)
    "entry_short": 1.5,           # not on the chart ~6h ago, charted now
    "entry_day": 0.75,
    "ignition": 1.0,              # per top-decile streamer on the game (cap 2)
    "steam_growth": 0.5,          # log-growth of Steam daily peak
}
MIN_VIEWERS = 1500        # noise floor for the leaderboard
TOP_ESTABLISHED = 20      # ignition only matters outside this rank
SHORT_HOURS = 6
DAY_HOURS = 24
HISTORY_MONTHS = 2        # trailing monthly partitions to read; growth windows need
                          # days, and streamer reach is better recent than eternal


def load_table(table: str) -> list[dict]:
    rows: list[dict] = []
    folder = SNAP_DIR / table
    if folder.exists():
        for part in sorted(folder.glob("*.csv"))[-HISTORY_MONTHS:]:
            with part.open(newline="", encoding="utf-8") as f:
                rows.extend(csv.DictReader(f))
    return rows


def md_escape(text: str) -> str:
    """Keep game names from breaking Markdown table rows."""
    return text.replace("|", "\\|")


def load_catalog(name: str) -> list[dict]:
    path = CATALOG_DIR / name
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def parse_hour(hour: str) -> datetime:
    return datetime.strptime(hour, "%Y-%m-%dT%H")


def nearest_ref(hours: list[str], latest: str, back_hours: int) -> str | None:
    """The snapshot closest to (latest - back_hours), excluding latest itself."""
    latest_dt = parse_hour(latest)
    candidates = [h for h in hours if h != latest]
    if not candidates:
        return None
    return min(candidates, key=lambda h: abs((latest_dt - parse_hour(h)).total_seconds() - back_hours * 3600))


def log_growth(now: float, ref: float) -> float | None:
    if now > 0 and ref > 0:
        return math.log(now / ref)
    return None


def fmt_pct(ratio_log: float | None) -> str:
    if ratio_log is None:
        return "-"
    return f"{(math.exp(ratio_log) - 1) * 100:+.0f}%"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    tg = load_table("twitch_top_games")
    gs = load_table("twitch_game_streams")
    ts = load_table("twitch_top_streams")
    sp = load_table("steam_player_counts")
    if not tg:
        print("no snapshots yet")
        return 1

    hours = sorted({r["snapshot_hour_utc"] for r in tg})
    latest = hours[-1]
    ref_short = nearest_ref(hours, latest, SHORT_HOURS)
    ref_day = nearest_ref(hours, latest, DAY_HOURS)
    if ref_day == ref_short:
        ref_day = None  # one reference can't serve both windows

    tg_by = {(r["snapshot_hour_utc"], r["game_id"]): r for r in tg}
    gs_by = {(r["snapshot_hour_utc"], r["game_id"]): r for r in gs}
    charted = {h: {r["game_id"] for r in tg if r["snapshot_hour_utc"] == h} for h in hours}

    # mapping: manual overrides win over IGDB
    igdb_to_app: dict[str, list[str]] = {}
    for r in load_catalog("igdb_steam_map.csv"):
        if r["steam_appid"]:
            igdb_to_app.setdefault(r["igdb_id"], []).append(r["steam_appid"])
    manual = {r["twitch_game_id"]: r["steam_appid"] for r in load_catalog("manual_map.csv")}

    def steam_appids(game: dict) -> list[str]:
        appids = list(igdb_to_app.get(game["igdb_id"], []))
        override = manual.get(game["game_id"])
        if override:
            appids = [override] + [a for a in appids if a != override]
        return appids

    steam_peak: dict[tuple[str, str], int] = {}
    for r in sp:
        if r["peak_in_game"]:
            steam_peak[(r["snapshot_hour_utc"], r["appid"])] = int(r["peak_in_game"])

    # streamer reach = their max observed viewers anywhere in history
    reach: dict[str, int] = {}
    for r in ts:
        if r["viewer_count"]:
            login = r["user_login"]
            reach[login] = max(reach.get(login, 0), int(r["viewer_count"]))
    reach_threshold = (statistics.quantiles(reach.values(), n=10)[8]
                       if len(reach) >= 10 else max(reach.values(), default=0) + 1)
    top_streams_latest: dict[str, list[dict]] = {}
    for r in ts:
        if r["snapshot_hour_utc"] == latest:
            top_streams_latest.setdefault(r["game_id"], []).append(r)

    rows_out: list[dict] = []
    for game_id in charted[latest]:
        game = tg_by[(latest, game_id)]
        agg = gs_by.get((latest, game_id))
        is_game = bool(game["igdb_id"] and game["igdb_id"] != "0") or game_id in manual
        viewers = int(agg["viewers_top100"]) if agg else 0
        channels = int(agg["channels_top100"]) if agg else 0
        rank = int(game["rank"])

        comp: dict[str, float] = {}
        flags: list[str] = []
        deltas: dict[str, float | None] = {"v_short": None, "v_day": None, "c_short": None}

        for label, ref in (("short", ref_short), ("day", ref_day)):
            if ref is None:
                continue
            if game_id not in charted[ref]:
                comp[f"entry_{label}"] = WEIGHTS[f"entry_{label}"]
                if "ENTRY" not in flags:
                    flags.append("ENTRY")
                continue
            ref_agg = gs_by.get((ref, game_id))
            if not (agg and ref_agg):
                continue
            vg = log_growth(viewers, int(ref_agg["viewers_top100"]))
            cg = log_growth(channels, int(ref_agg["channels_top100"]))
            if vg is not None:
                comp[f"viewer_growth_{label}"] = WEIGHTS[f"viewer_growth_{label}"] * vg
                deltas[f"v_{label}"] = vg
            if cg is not None:
                comp[f"channel_growth_{label}"] = WEIGHTS[f"channel_growth_{label}"] * cg
                if label == "short":
                    deltas["c_short"] = cg
            if label == "short" and vg is not None and cg is not None and cg >= vg > 0:
                comp["breadth_bonus"] = WEIGHTS["breadth_bonus"]
                flags.append("BREADTH")

        rank_ref = ref_day or ref_short
        if rank_ref and game_id in charted[rank_ref]:
            climb = int(tg_by[(rank_ref, game_id)]["rank"]) - rank
            if climb:
                comp["rank_climb"] = WEIGHTS["rank_climb"] * climb

        igniters = []
        if rank > TOP_ESTABLISHED:
            for s in top_streams_latest.get(game_id, []):
                if reach.get(s["user_login"], 0) >= reach_threshold:
                    igniters.append(s["user_login"])
        if igniters:
            comp["ignition"] = WEIGHTS["ignition"] * min(len(igniters), 2)
            flags.append("IGNITION:" + ",".join(igniters[:2]))

        appids = steam_appids(game)
        if appids and rank_ref:
            now_peak = sum(steam_peak.get((latest, a), 0) for a in appids)
            ref_peak = sum(steam_peak.get((rank_ref, a), 0) for a in appids)
            sg = log_growth(now_peak, ref_peak)
            if sg is not None:
                comp["steam_growth"] = WEIGHTS["steam_growth"] * sg

        rows_out.append({
            "snapshot_hour_utc": latest,
            "game_id": game_id,
            "name": game["name"],
            "is_game": is_game,
            "rank": rank,
            "viewers_top100": viewers,
            "channels_top100": channels,
            "share_top1": agg["share_top1"] if agg else "",
            "v_short": deltas["v_short"], "v_day": deltas["v_day"], "c_short": deltas["c_short"],
            "flags": " ".join(flags),
            "score": round(sum(comp.values()), 3),
            "components": ";".join(f"{k}={v:+.2f}" for k, v in sorted(comp.items())),
            "steam_appid": (appids or [""])[0],
            "_appids": appids,
        })

    board = sorted(
        (r for r in rows_out if r["is_game"] and r["viewers_top100"] >= MIN_VIEWERS),
        key=lambda r: r["score"], reverse=True)

    # ignition alerts: big streamer dominating a small game right now
    alerts = []
    for r in rows_out:
        if not r["is_game"] or r["rank"] <= TOP_ESTABLISHED:
            continue
        for s in top_streams_latest.get(r["game_id"], []):
            login = s["user_login"]
            share = float(r["share_top1"]) if r["share_top1"] else 0.0
            if s["position"] == "1" and reach.get(login, 0) >= reach_threshold and share >= 0.5:
                alerts.append((r["name"], login, reach[login], int(s["viewer_count"]), share, r["rank"]))
    alerts.sort(key=lambda a: a[3], reverse=True)

    # watched vs played extremes (latest hour, mapped + on the Steam chart)
    ratios = []
    for r in rows_out:
        # sum all mapped appids (editions), matching the steam_growth component
        peak = sum(steam_peak.get((latest, a), 0) for a in r["_appids"])
        if peak and r["viewers_top100"]:
            ratios.append((round(r["viewers_top100"] / peak, 3), r["name"], r["viewers_top100"], peak))
    ratios.sort(reverse=True)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    span_h = (parse_hour(latest) - parse_hour(hours[0])).total_seconds() / 3600
    lines = [
        "# Breakout Watch",
        "",
        f"Generated from snapshot `{latest}` (UTC). History: {len(hours)} snapshots spanning {span_h:.0f}h.",
        f"Reference windows: short = `{ref_short}`, day = `{ref_day}`.",
        "",
        "## Leaderboard (momentum score v0.1)",
        "",
        "| # | game | score | rank | viewers | Δ6h | Δ24h | channels | chΔ6h | top1 share | flags |",
        "|---|------|-------|------|---------|-----|------|----------|-------|-----------|-------|",
    ]
    for i, r in enumerate(board[:15], start=1):
        lines.append(
            f"| {i} | {md_escape(r['name'])} | {r['score']:+.2f} | {r['rank']} | {r['viewers_top100']:,} "
            f"| {fmt_pct(r['v_short'])} | {fmt_pct(r['v_day'])} | {r['channels_top100']} "
            f"| {fmt_pct(r['c_short'])} | {r['share_top1']} | {r['flags']} |")
    lines += [
        "",
        "## Ignition alerts",
        "",
        "Big-reach streamers currently dominating a game outside the top 20 — the",
        "\"one streamer could make it blow up\" event, flagged before growth shows.",
        "",
    ]
    if alerts:
        lines += ["| game | streamer | their max reach | viewers now | share of game | game rank |",
                  "|------|----------|----------------|-------------|--------------|-----------|"]
        lines += [f"| {md_escape(n)} | {md_escape(s)} | {rc:,} | {v:,} | {sh:.0%} | {rk} |" for n, s, rc, v, sh, rk in alerts[:10]]
    else:
        lines.append("*(none this snapshot)*")
    lines += [
        "",
        "## Watched vs played (Twitch viewers ÷ Steam daily peak)",
        "",
        "| ratio | game | twitch viewers | steam peak |",
        "|-------|------|----------------|------------|",
    ]
    for ratio, name, v, p in ratios[:5] + ([] if len(ratios) <= 10 else ratios[-5:]):
        lines.append(f"| {ratio} | {md_escape(name)} | {v:,} | {p:,} |")
    lines += [
        "",
        "---",
        "",
        "**Read honestly:** score weights are transparent priors (v0.1), not fitted",
        "coefficients — they get backtested and replaced once weeks of history exist",
        "(label: *enters the Twitch top 20 within 7 days*). Component breakdown for",
        "every game is in `breakout_watch.csv`. Growth windows shorten-fallback when",
        "history is young.",
        "",
    ]
    (REPORTS_DIR / "breakout_watch.md").write_text("\n".join(lines), encoding="utf-8")

    fields = ["snapshot_hour_utc", "game_id", "name", "is_game", "rank", "viewers_top100",
              "channels_top100", "share_top1", "v_short", "v_day", "c_short", "flags",
              "score", "components", "steam_appid"]
    with (REPORTS_DIR / "breakout_watch.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows({k: ("" if r[k] is None else r[k]) for k in fields} for r in sorted(
            rows_out, key=lambda r: r["score"], reverse=True))

    print(f"breakout_watch: {len(board)} scored games, {len(alerts)} ignition alerts "
          f"(history {len(hours)} snapshots / {span_h:.0f}h)")
    for i, r in enumerate(board[:10], start=1):
        print(f"  {i:2}. {r['score']:+.2f}  {r['name']}  (rank {r['rank']}, {r['viewers_top100']:,} viewers) {r['flags']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
