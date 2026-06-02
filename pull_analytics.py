#!/usr/bin/env python3
"""
YouTube Analytics → Dashboard JSON
Pulls data from YouTube Analytics + Data APIs, writes data.json in the
schema the Community Homes Ohio dashboard expects. Runs monthly via GitHub Actions.

Requires environment variables:
  YT_CLIENT_ID       — OAuth client ID
  YT_CLIENT_SECRET   — OAuth client secret
  YT_REFRESH_TOKEN   — OAuth refresh token (one-time setup via OAuth Playground)
  YT_CHANNEL_ID      — (optional) channel ID, defaults to Community Homes Ohio
"""
import os
import sys
import json
import datetime
import traceback

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ---------- CONFIG ----------
CHANNEL_ID = os.environ.get("YT_CHANNEL_ID", "UCJiMGnm9J9TBQKCyh9sFTPA")
# Note: YouTube Analytics API requires `channel==MINE` for channel-level reports.
# Explicit channel IDs require content-owner credentials, which a regular OAuth user lacks.
# CHANNEL_ID is used only for Data API lookups (channel stats, uploads playlist).
ERA_SPLIT_YEAR = 2025
ERA_SPLIT_MONTH = 9          # September 2025 — music era begins
NUM_TOP_VIDEOS = 15
TREND_MONTHS = 16
OUTPUT_PATH = "data.json"

REQUIRED_ENV = ["YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN"]


# ---------- AUTH ----------
def check_env():
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        print(f"ERROR: Missing required environment variables: {missing}")
        sys.exit(1)


def get_credentials():
    return Credentials(
        token=None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=[
            "https://www.googleapis.com/auth/yt-analytics.readonly",
            "https://www.googleapis.com/auth/youtube.readonly",
        ],
    )


# ---------- HELPERS ----------
def month_label(year, month):
    return datetime.date(year, month, 1).strftime("%b %y")


def first_of_month(d):
    return d.replace(day=1)


def add_months(d, n):
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    return datetime.date(y, m, 1)


def safe(label, fn, default=None):
    """Run an API call defensively; log full error and continue on failure."""
    try:
        return fn()
    except HttpError as e:
        # Get the full response body, not just the status — Google sends useful detail there
        body = ""
        try:
            body = e.content.decode("utf-8") if e.content else ""
        except Exception:
            body = str(e)
        print(f"  ⚠ [{label}] HttpError {e.resp.status if hasattr(e, 'resp') else '?'}: {body[:500]}")
    except Exception as e:
        print(f"  ⚠ [{label}] {type(e).__name__}: {e}")
        traceback.print_exc()
    return default


def pct_change(now, prev):
    if not prev:
        return 0
    return round(((now - prev) / abs(prev)) * 100)


# ---------- PULL FUNCTIONS ----------
def pull_kpis(analytics, today):
    """Last 365 days of channel KPIs vs prior 365 days."""
    print("Pulling KPIs (last 365 days)...")
    end = today
    start = today - datetime.timedelta(days=365)
    prev_end = start - datetime.timedelta(days=1)
    prev_start = prev_end - datetime.timedelta(days=365)

    cur = analytics.reports().query(
        ids="channel==MINE",
        startDate=start.isoformat(),
        endDate=end.isoformat(),
        metrics="views,estimatedMinutesWatched,subscribersGained,subscribersLost,averageViewDuration,averageViewPercentage",
    ).execute()
    cur_row = cur["rows"][0] if cur.get("rows") else [0] * 6
    views, mins, sg, sl, avg_dur, avg_pct = cur_row

    prev = analytics.reports().query(
        ids="channel==MINE",
        startDate=prev_start.isoformat(),
        endDate=prev_end.isoformat(),
        metrics="views,estimatedMinutesWatched,subscribersGained,subscribersLost",
    ).execute()
    prev_row = prev["rows"][0] if prev.get("rows") else [0] * 4
    p_views, p_mins, p_sg, p_sl = prev_row

    return {
        "views": int(views),
        "watch_hours": round(mins / 60, 1),
        "sub_change": int(sg - sl),
        "avg_view_duration_seconds": round(avg_dur, 1) if avg_dur else 0,
        "avg_view_percentage": round(avg_pct, 1) if avg_pct else 0,
        "views_yoy_pct": pct_change(views, p_views),
        "watch_yoy_pct": pct_change(mins, p_mins),
        "sub_yoy_pct": pct_change(sg - sl, p_sg - p_sl),
    }


def pull_monthly_trends(analytics, today, months=16):
    """Pull daily views/watch and aggregate to monthly buckets.

    We use dimension=day rather than dimension=month because the API rejects
    monthly queries when the end date doesn't fall on a recognized boundary.
    Aggregating in Python is straightforward and avoids that quirk.
    """
    print(f"Pulling monthly trends ({months} months)...")
    start = add_months(first_of_month(today), -months)
    end = first_of_month(today) - datetime.timedelta(days=1)

    res = analytics.reports().query(
        ids="channel==MINE",
        startDate=start.isoformat(),
        endDate=end.isoformat(),
        metrics="views,estimatedMinutesWatched",
        dimensions="day",
        sort="day",
        maxResults=10000,
    ).execute()

    by_month = {}
    for row in res.get("rows", []):
        ymd = row[0]  # YYYY-MM-DD
        year, month, _ = ymd.split("-")
        key = (int(year), int(month))
        bucket = by_month.setdefault(key, {"views": 0, "watch_min": 0})
        bucket["views"] += int(row[1])
        bucket["watch_min"] += float(row[2])

    trends = []
    cursor = start
    for _ in range(months):
        key = (cursor.year, cursor.month)
        m = by_month.get(key, {"views": 0, "watch_min": 0})
        trends.append({
            "month": month_label(cursor.year, cursor.month),
            "views": m["views"],
            "watch": round(m["watch_min"] / 60, 1),
            "uploads": 0,  # filled in by pull_monthly_uploads
        })
        cursor = add_months(cursor, 1)
    return trends


def pull_monthly_uploads(data_api, channel_id, trends):
    """Count videos uploaded each month using the channel's uploads playlist."""
    print("Pulling upload cadence...")
    # Get uploads playlist ID
    ch = data_api.channels().list(part="contentDetails", id=channel_id).execute()
    if not ch.get("items"):
        return trends
    uploads_pid = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]

    # Paginate through the uploads playlist
    counts = {}
    page_token = None
    fetched = 0
    while True:
        res = data_api.playlistItems().list(
            part="snippet",
            playlistId=uploads_pid,
            maxResults=50,
            pageToken=page_token,
        ).execute()
        for item in res.get("items", []):
            pub = item["snippet"]["publishedAt"]
            try:
                d = datetime.datetime.fromisoformat(pub.replace("Z", "+00:00"))
                key = (d.year, d.month)
                counts[key] = counts.get(key, 0) + 1
                fetched += 1
            except Exception:
                pass
        page_token = res.get("nextPageToken")
        if not page_token:
            break
        # Safety: stop after a lot of pages (channel has 899+ videos, paginate them all)
        if fetched > 2000:
            print(f"  Stopped paginating at {fetched} videos")
            break

    # Backfill trends with upload counts
    for t in trends:
        # Parse "Sep 25" → (2025, 9)
        parts = t["month"].split()
        if len(parts) == 2:
            try:
                mon_num = datetime.datetime.strptime(parts[0], "%b").month
                yr_num = 2000 + int(parts[1])
                t["uploads"] = counts.get((yr_num, mon_num), 0)
            except ValueError:
                pass
    return trends


def pull_subscriber_timeline(analytics, today, total_subs, months=16):
    """Build a cumulative subscriber timeline from daily deltas.

    Same rationale as monthly_trends: use dimension=day and aggregate.
    """
    print("Pulling subscriber timeline...")
    if total_subs is None:
        return []
    start = add_months(first_of_month(today), -months)
    end = first_of_month(today) - datetime.timedelta(days=1)

    res = analytics.reports().query(
        ids="channel==MINE",
        startDate=start.isoformat(),
        endDate=end.isoformat(),
        metrics="subscribersGained,subscribersLost",
        dimensions="day",
        sort="day",
        maxResults=10000,
    ).execute()

    # Aggregate daily deltas into monthly buckets
    by_month = {}
    for row in res.get("rows", []):
        ymd = row[0]  # YYYY-MM-DD
        year, month, _ = ymd.split("-")
        key = (int(year), int(month))
        bucket = by_month.setdefault(key, {"gained": 0, "lost": 0})
        bucket["gained"] += int(row[1])
        bucket["lost"] += int(row[2])

    deltas = []
    cursor = start
    for _ in range(months):
        key = (cursor.year, cursor.month)
        b = by_month.get(key, {"gained": 0, "lost": 0})
        deltas.append((month_label(cursor.year, cursor.month), b["gained"] - b["lost"]))
        cursor = add_months(cursor, 1)

    # Walk backward from current total to compute monthly snapshots
    timeline_rev = []
    cumulative = total_subs
    for label, delta in reversed(deltas):
        timeline_rev.append({"month": label, "value": cumulative})
        cumulative -= delta
    timeline = list(reversed(timeline_rev))

    # Subsample to ~7 points so the chart isn't crowded
    if len(timeline) > 7:
        step = max(1, len(timeline) // 7)
        timeline = [timeline[i] for i in range(0, len(timeline), step)][:7]
    return timeline


def pull_top_videos(analytics, data_api, today, n=15):
    print(f"Pulling top {n} videos...")
    end = today
    start = today - datetime.timedelta(days=365)

    res = analytics.reports().query(
        ids="channel==MINE",
        startDate=start.isoformat(),
        endDate=end.isoformat(),
        metrics="views,averageViewDuration,averageViewPercentage",
        dimensions="video",
        sort="-views",
        maxResults=n,
    ).execute()

    rows = res.get("rows", [])
    if not rows:
        return []

    video_ids = [r[0] for r in rows]
    meta = pull_video_metadata(data_api, video_ids)

    out = []
    for r in rows:
        vid, views, avg_dur, avg_pct = r
        m = meta.get(vid, {"title": vid, "publishedAt": ""})
        try:
            pub = datetime.datetime.fromisoformat(m["publishedAt"].replace("Z", "+00:00")).date()
            era = "post" if pub >= datetime.date(ERA_SPLIT_YEAR, ERA_SPLIT_MONTH, 1) else "pre"
            date_str = pub.strftime("%b ") + str(pub.day) + ", " + str(pub.year)
        except Exception:
            era = "post"
            date_str = ""

        avg_dur_s = float(avg_dur) if avg_dur else 0
        dur_m = int(avg_dur_s // 60)
        dur_s = int(avg_dur_s % 60)
        dur_str = f"{dur_m}:{dur_s:02d}" if avg_dur_s > 0 else "—"

        out.append({
            "title": m["title"],
            "era": era,
            "date": date_str,
            "views": int(views),
            "dur": dur_str,
            "ret": round(float(avg_pct), 1) if avg_pct else None,
        })
    return out


def pull_video_metadata(data_api, video_ids):
    out = {}
    if not video_ids:
        return out
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        res = data_api.videos().list(part="snippet", id=",".join(batch)).execute()
        for item in res.get("items", []):
            out[item["id"]] = {
                "title": item["snippet"]["title"],
                "publishedAt": item["snippet"]["publishedAt"],
            }
    return out


def pull_traffic_sources(analytics, today):
    print("Pulling traffic sources...")
    end = today
    start = today - datetime.timedelta(days=365)

    res = analytics.reports().query(
        ids="channel==MINE",
        startDate=start.isoformat(),
        endDate=end.isoformat(),
        metrics="views",
        dimensions="insightTrafficSourceType",
        sort="-views",
        maxResults=10,
    ).execute()

    rows = res.get("rows", [])
    total = sum(int(r[1]) for r in rows) or 1

    label_map = {
        "EXT_URL": "External Sites",
        "YT_SEARCH": "YouTube Search",
        "BROWSE": "Browse Features",
        "PLAYLIST": "Playlists",
        "SUGGESTED_VIDEO": "Suggested",
        "RELATED_VIDEO": "Suggested",
        "NOTIFICATION": "Notifications",
        "YT_CHANNEL": "Channel Pages",
        "YT_OTHER_PAGE": "YouTube Other",
        "NO_LINK_OTHER": "Direct / Unknown",
        "PROMOTED": "Ads",
        "END_SCREEN": "End Screens",
        "ANNOTATION": "Cards",
        "SUBSCRIBER": "Subscriptions Feed",
        "HASHTAGS": "Hashtags",
    }

    # Combine duplicate labels (e.g. RELATED_VIDEO + SUGGESTED_VIDEO → Suggested)
    merged = {}
    for r in rows:
        source = r[0]
        views = int(r[1])
        name = label_map.get(source, source.replace("_", " ").title())
        merged[name] = merged.get(name, 0) + views

    out = []
    for name, views in sorted(merged.items(), key=lambda x: -x[1]):
        pct = round((views / total) * 100, 1)
        if pct >= 0.5:  # filter tiny sources
            out.append({"name": name, "pct": pct})
    return out[:7]


def pull_retention_top(top_videos):
    """Use top videos' averageViewPercentage as retention proxy."""
    out = []
    for v in top_videos[:6]:
        if v.get("ret") is not None:
            # Shorten the title for the chart label
            short = v["title"].split(" – ")[0].split(",")[0]
            short = short.split(":")[0].strip()
            if len(short) > 34:
                short = short[:32] + "…"
            out.append({"name": short, "pct": v["ret"]})
    return out


def get_channel_stats(data_api, channel_id):
    res = data_api.channels().list(part="statistics", id=channel_id).execute()
    if res.get("items"):
        s = res["items"][0]["statistics"]
        return {
            "subs": int(s.get("subscriberCount", 0)),
            "videos": int(s.get("videoCount", 0)),
            "views_total": int(s.get("viewCount", 0)),
        }
    return None


def compute_era_split(trends):
    """Find the index where Music Era begins."""
    for i, t in enumerate(trends):
        parts = t["month"].split()
        if len(parts) != 2:
            continue
        try:
            mon = datetime.datetime.strptime(parts[0], "%b").month
            yr = 2000 + int(parts[1])
            if (yr, mon) >= (ERA_SPLIT_YEAR, ERA_SPLIT_MONTH):
                return i
        except ValueError:
            pass
    return len(trends)


# ---------- MAIN ----------
def main():
    check_env()
    print(f"YouTube Analytics Pull — {datetime.date.today()}")
    print(f"Data API channel: {CHANNEL_ID}")
    print(f"Analytics scope:  channel==MINE (resolves to OAuth account's primary channel)")
    print(f"")

    creds = get_credentials()
    analytics = build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)
    data_api = build("youtube", "v3", credentials=creds, cache_discovery=False)

    today = datetime.date.today()

    stats = safe("channel_stats", lambda: get_channel_stats(data_api, CHANNEL_ID), {})
    kpis = safe("kpis", lambda: pull_kpis(analytics, today), {})
    trends = safe("trends", lambda: pull_monthly_trends(analytics, today, TREND_MONTHS), [])
    trends = safe("uploads", lambda: pull_monthly_uploads(data_api, CHANNEL_ID, trends), trends)
    top_videos = safe("top_videos", lambda: pull_top_videos(analytics, data_api, today, NUM_TOP_VIDEOS), [])
    traffic = safe("traffic", lambda: pull_traffic_sources(analytics, today), [])
    retention = pull_retention_top(top_videos) if top_videos else []
    subs_timeline = safe(
        "subs_timeline",
        lambda: pull_subscriber_timeline(analytics, today, stats.get("subs") if stats else None, TREND_MONTHS),
        [],
    )

    output = {
        "_generated": today.isoformat(),
        "_generator": "github-actions",

        "totalVideos": f"{stats['videos']}+" if stats and stats.get("videos") else "899+",
        "totalSubs": stats.get("subs") if stats else 217,
        "genDate": today.isoformat(),
        "displayDate": today.strftime("%b ") + str(today.day) + ", " + str(today.year),

        "kpiViews": kpis.get("views", 0),
        "kpiWatch": kpis.get("watch_hours", 0),
        "kpiWatchYoY": kpis.get("watch_yoy_pct", 0),
        "kpiSubChange": kpis.get("sub_change", 0),
        "kpiSubYoY": kpis.get("sub_yoy_pct", 0),

        # Thumbnail impressions / CTR are not exposed via the public Analytics API.
        # These values must be set manually via the dashboard editor when needed.
        # The script preserves prior values if present.
        "kpiImpressions": None,
        "kpiImpYoY": None,
        "kpiCTR": None,

        "eraSplit": compute_era_split(trends),
        "trends": trends,
        "subs": subs_timeline,
        "videos": top_videos,
        "traffic": traffic,
        "retention": retention,
    }

    # Preserve manually-set impressions/CTR from prior run, if data.json exists
    if os.path.exists(OUTPUT_PATH):
        try:
            with open(OUTPUT_PATH) as f:
                prior = json.load(f)
            for k in ("kpiImpressions", "kpiImpYoY", "kpiCTR"):
                if output[k] is None and prior.get(k) is not None:
                    output[k] = prior[k]
        except Exception as e:
            print(f"  Could not load prior data.json: {e}")

    # Final fallbacks for manual-only fields
    if output["kpiImpressions"] is None:
        output["kpiImpressions"] = 130000
    if output["kpiImpYoY"] is None:
        output["kpiImpYoY"] = 0
    if output["kpiCTR"] is None:
        output["kpiCTR"] = 2.5

    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\n✓ Wrote {OUTPUT_PATH}")
    print(f"  Subscribers: {output['totalSubs']}")
    print(f"  Videos: {output['totalVideos']}")
    print(f"  Views (365d): {output['kpiViews']:,}")
    print(f"  Watch (365d): {output['kpiWatch']} hrs")
    print(f"  Trends: {len(trends)} months")
    print(f"  Top videos: {len(top_videos)}")
    print(f"  Traffic sources: {len(traffic)}")


if __name__ == "__main__":
    main()
