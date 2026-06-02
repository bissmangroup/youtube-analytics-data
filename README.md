# YouTube Analytics Data

Pulls monthly YouTube Studio analytics for the **Community Homes Ohio / Bissman Group** channel and publishes the result as `data.json` for consumption by the analytics dashboard.

- **Runs**: 1st of every month, 09:00 UTC (~5am Eastern) via GitHub Actions
- **Manual trigger**: Actions tab → "Update YouTube Analytics" → "Run workflow"
- **Output**: `data.json` (committed back to this repo)
- **Consumed by**: `youtube-analytics-dashboard.html` on SiteGround

## Architecture

```
GitHub Actions cron
        │
        ▼
pull_analytics.py
   │
   ├─► YouTube Analytics API   (views, watch time, retention, traffic)
   └─► YouTube Data API v3     (channel stats, video titles, uploads)
        │
        ▼
   data.json   (committed back to repo)
        │
        ▼
https://raw.githubusercontent.com/<owner>/<repo>/main/data.json
        │
        ▼
   Dashboard (SiteGround) fetches on page load
```

## What it pulls

| Field | Source | Notes |
|---|---|---|
| Total subscribers / video count | Data API → `channels.statistics` | Live count |
| Views, watch hours (365d) | Analytics API | + YoY % vs prior 365 days |
| Subscriber change (365d) | Analytics API | + YoY % |
| Monthly trends (16 months) | Analytics API | Views, watch hours |
| Upload count per month | Data API → uploads playlist | Backfilled into trends |
| Subscriber timeline | Computed | Walks backward from current total using monthly deltas |
| Top 15 videos | Analytics + Data API | Title, views, retention, era classification |
| Traffic sources | Analytics API → `insightTrafficSourceType` | Aggregated and labeled |
| Retention chart data | Top videos' `averageViewPercentage` | One number per top video |

## What it does NOT pull

**Thumbnail impressions and CTR** are not exposed via the public YouTube Analytics API — those numbers only appear in YouTube Studio. The script preserves prior manual values from `data.json` between runs. To update those, edit them via the dashboard's "Update Data" button.

## Setup

### 1. Google Cloud Console
1. Create (or use) a project at https://console.cloud.google.com
2. Enable **YouTube Data API v3** and **YouTube Analytics API**
3. Configure OAuth consent screen (External, Testing mode is fine if you only authorize your own account)
4. Create OAuth 2.0 credentials: **Application type = Web application**, set `https://developers.google.com/oauthplayground` as an authorized redirect URI
5. Copy the **Client ID** and **Client Secret**

### 2. Generate a refresh token
1. Go to https://developers.google.com/oauthplayground
2. Click the gear (top right) → **Use your own OAuth credentials** → paste your Client ID and Client Secret
3. In Step 1, enter these scopes in the "Input your own scopes" box:
   ```
   https://www.googleapis.com/auth/yt-analytics.readonly
   https://www.googleapis.com/auth/youtube.readonly
   ```
4. Click **Authorize APIs**, sign in with the **channel-owning Google account**, click Allow
5. In Step 2, click **Exchange authorization code for tokens**
6. Copy the **Refresh token** (NOT the access token)

### 3. GitHub repo secrets
In this repo: **Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `YT_CLIENT_ID` | from step 1 |
| `YT_CLIENT_SECRET` | from step 1 |
| `YT_REFRESH_TOKEN` | from step 2 |
| `YT_CHANNEL_ID` | `UCJiMGnm9J9TBQKCyh9sFTPA` (or your channel ID) |

### 4. First run
- Actions tab → "Update YouTube Analytics" → **Run workflow**
- Wait ~30 seconds, refresh the repo, `data.json` should appear

### 5. Wire up the dashboard
In your dashboard HTML, find the line:
```js
const REMOTE_DATA_URL = '';
```
And set it to your raw GitHub URL:
```js
const REMOTE_DATA_URL = 'https://raw.githubusercontent.com/YOUR_USER/YOUR_REPO/main/data.json';
```

## Running locally

```bash
pip install -r requirements.txt
export YT_CLIENT_ID="..."
export YT_CLIENT_SECRET="..."
export YT_REFRESH_TOKEN="..."
python pull_analytics.py
# Inspect data.json
```

## Troubleshooting

**"Missing required environment variables"** — secrets aren't set in the repo, or the workflow file isn't reading them. Verify under Settings → Secrets.

**"Token has been expired or revoked"** — refresh token died (rare — usually because the OAuth consent screen is in Testing mode and the token has aged out after 7 days, or because the password changed). Repeat step 2 above and update the `YT_REFRESH_TOKEN` secret.

**"Quota exceeded"** — unlikely (default quota is 10,000 units/day, monthly pull uses ~50). If it happens, request a quota increase in Cloud Console.

**Empty data.json** — the script failed silently on every call. Check the Actions run log for the specific error.

## Privacy note

The `data.json` file is committed to this repo. If the repo is **public**, the analytics data is public too — which matches the dashboard already being on a noindex public URL. If you want it private, make this repo private and switch the dashboard to fetch from an authenticated endpoint instead (would require a small proxy).

---

*Maintained by Bissman Group · Keller Williams Advisors*
