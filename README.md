# 🛰 SENTINEL — Daily Brief Automation Pipeline

**AKA IND Technologies** · Automated OSINT intelligence brief, delivered to Notion daily at 00:05 UTC.

---

## What It Does

Every day at midnight UTC, this pipeline:

1. **Hits OpenSky Network** — pulls live ADS-B state vectors (same feed as FlightRadar24)
2. **Hits GPSJam.org** — pulls real GPS interference data (daily CSV)
3. **Classifies everything** — military vs ISR vs commercial, emergency squawks, high-intensity jamming
4. **Generates a structured brief** — Notion-flavored Markdown with tables, callouts, toggles
5. **Posts a new dated page** to your Notion workspace automatically

Falls back to simulation if either API is unavailable (rate-limited, down, etc.)

---

## Setup — 5 Minutes

### Step 1 — Clone & connect to GitHub

```bash
git clone https://github.com/YOUR_USERNAME/sentinel-pipeline.git
cd sentinel-pipeline
```

### Step 2 — Create Notion Integration

1. Go to **[notion.so/my-integrations](https://www.notion.so/my-integrations)**
2. Click **+ New integration**
3. Name it `SENTINEL Pipeline`
4. Copy the **Internal Integration Token** (starts with `secret_...`)
5. Open your Notion workspace, go to the parent page where you want briefs to live
6. Click `•••` → **Add connections** → select `SENTINEL Pipeline`
7. Copy the page ID from the URL:
   `notion.so/YOUR-WORKSPACE/Page-Title-**THIS-IS-THE-ID**`

### Step 3 — Add GitHub Secrets

In your GitHub repo: **Settings → Secrets and variables → Actions → New repository secret**

| Secret name | Value |
|---|---|
| `NOTION_API_KEY` | `secret_xxxxxxxxxxxx` (from Step 2) |
| `NOTION_PARENT_ID` | Page ID where briefs are created (from Step 2) |
| `AIS_API_KEY` | WebSocket API key from [aisstream.io](https://aisstream.io) (free registration) |
| `CELESTRAK_API_KEY` | API key from [celestrak.org](https://celestrak.org) |
| `ACLED_API_KEY` | Access key from [acleddata.com](https://acleddata.com) |

### Step 4 — Push & activate

```bash
git add .
git commit -m "init: SENTINEL daily brief pipeline"
git push origin main
```

GitHub Actions will now fire every day at **00:05 UTC** automatically.

---

## Manual Run

**From GitHub Actions UI:**
- Go to **Actions → SENTINEL Daily Intelligence Brief → Run workflow**
- Optionally set a date override or enable dry-run mode

**From CLI:**

```bash
# Install dependencies
pip install -r requirements.txt

# Dry run (prints output, does NOT post to Notion)
python sentinel_brief.py --dry-run

# Run for a specific date
python sentinel_brief.py --date 2026-03-06

# Full run (posts to Notion)
NOTION_API_KEY=secret_xxx NOTION_PARENT_ID=abc123 python sentinel_brief.py

# Save raw data alongside posting
python sentinel_brief.py --json-out data/2026-03-06.json
```

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `NOTION_API_KEY` | Yes | Notion internal integration token |
| `NOTION_PARENT_ID` | Yes | Parent page/database ID for new briefs |
| `AIS_API_KEY` | No | aisstream.io WebSocket key — enables live maritime AIS tracking |
| `CELESTRAK_API_KEY` | No | CelesTrak API key — enables live satellite TLE data |
| `ACLED_API_KEY` | No | ACLED access key — enables live armed conflict incident data |

---

## Data Sources

| Domain | Source | Live? | Notes |
|---|---|---|---|
| ✈ Aircraft | OpenSky Network | ✅ Live | Same ADS-B feed as FlightRadar24. Anonymous API: ~1 req/10s |
| 📡 GPS Jamming | GPSJam.org | ✅ Live | Daily CSV. Derived from NACp anomalies in ADS-B messages |
| ⛴ Maritime | AIS (simulated) | ⚠ Simulated | Register free at aisstream.io to activate |
| 🛰 Satellites | CelesTrak GP/OMM | ✅ Live | Queries resource & military groups. Falls back to simulated if unavailable |
| 💥 Incidents | ACLED (simulated) | ⚠ Simulated | Plug in ACLED API key to activate |

---

## Enabling Live AIS (Optional)

1. Register at [aisstream.io](https://aisstream.io) (free)
2. Add `AIS_API_KEY=your_key` to GitHub Secrets
3. Uncomment the `fetch_ais()` function in `sentinel_brief.py`

---

## Architecture

```
GitHub Actions (cron: 00:05 UTC)
        │
        ▼
sentinel_brief.py
        │
        ├── fetch_opensky()    → OpenSky ADS-B API → classify aircraft
        ├── fetch_gpsjam()     → GPSJam.org CSV    → parse jamming zones
        ├── fetch_celestrak()  → CelesTrak GP API  → satellite orbital data
        │
        ▼
generate_brief()               → Notion-flavored Markdown
        │
        ▼
post_to_notion()               → Notion API → new dated page
```

---

## File Structure

```
sentinel-pipeline/
├── .github/
│   └── workflows/
│       └── daily_brief.yml    ← GitHub Actions schedule
├── data/                      ← Raw JSON output (auto-created)
├── sentinel_brief.py          ← Main pipeline script
├── requirements.txt
└── README.md
```

---

## Custom Domain (GitHub Pages)

The SENTINEL dashboard is served via GitHub Pages at **https://sentinel.akaind.ca/**.

### DNS Configuration

In your DNS provider for `akaind.ca`, add (or update) the following record:

| Type | Host / Name | Value / Target | TTL |
|---|---|---|---|
| `CNAME` | `sentinel` | `aaakaind.github.io` | 3600 (or default) |

**Step-by-step:**

1. Log in to your DNS provider (e.g. Cloudflare, Namecheap, GoDaddy, Route 53)
2. Navigate to the DNS records for `akaind.ca`
3. If a record for `sentinel` already exists, **update** it — otherwise create a new one
4. Set type to `CNAME`, host/name to `sentinel`, value/target to `aaakaind.github.io`
5. Save and wait for propagation (typically 5–30 minutes, up to 48 hours)

### Verify DNS

```bash
dig sentinel.akaind.ca +short
# Expected output: aaakaind.github.io. (and a GitHub Pages IP)
```

### Enable HTTPS in GitHub Pages

1. Go to **Settings → Pages** in this repository
2. Under **Custom domain**, confirm it shows `sentinel.akaind.ca`
3. Check **Enforce HTTPS** (available once DNS propagates and the TLS certificate is issued)

> **Note:** The `CNAME` file in this repository tells GitHub Pages which custom domain to use. Do not remove or rename it.

---

## Troubleshooting

**OpenSky returning 429** — Rate limited. The fallback simulation activates automatically. Add authenticated credentials to `OPENSKY_USER` / `OPENSKY_PASS` env vars for higher rate limits.

**Notion API 401** — Invalid or expired API key. Re-generate at notion.so/my-integrations.

**Notion API 404** — Parent page ID incorrect, or integration not connected to that page. Check Step 2 in setup.

**Brief not appearing** — Check Actions tab in GitHub. Green checkmark = success. Red = see logs.

---

*SENTINEL v5 · AKA IND Technologies · OSINT UNCLASSIFIED*
