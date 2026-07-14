# Property + Overlay Search

Local dashboard that searches realestate.com.au listings by suburb/price/land size,
and cross-checks each result against Victorian planning overlays (VicPlan) —
bushfire, flood, heritage, vegetation, etc.

## Status

- **Overlay lookup (VIC): working and tested live.** Uses two free, public,
  no-auth ArcGIS REST services behind the official VicPlan map — a geocoder
  (address → lat/lon) and the `Vicplan_PlanningSchemeOverlays` MapServer's
  `identify` endpoint (point → every overlay intersecting it). See
  `backend/overlays.py`.
- **Listing scrape: unresolved, needs testing on your machine.** realestate.com.au
  has no public API and runs **Kasada** bot-protection, which blocks a plain
  automated browser outright (confirmed while building this — got a
  `window.KPSDK` challenge page + HTTP 429, not listings). The scraper
  (`backend/scraper.py`) now uses `patchright` (an anti-detection Playwright
  fork) in **headed** mode (a visible Chrome window opens — this is required,
  not optional, for the anti-detection patches to work) with a persistent
  browser profile so cookies/session survive between runs. This is the
  standard approach people use to get through Kasada, but I could not verify
  it end-to-end from the environment this was built in — no display was
  available there, and cloud/sandbox IPs are exactly what Kasada's IP
  reputation checks flag. **You need to try a real search from your own Mac
  on your home network to know if it gets through.**

## How it works

- **Listings**: `backend/scraper.py` drives a real, visible Chromium browser
  to load realestate.com.au search pages and extracts the JSON embedded in
  the page (`window.ArgonautExchange`). If it hits a bot-challenge page it
  waits 15s (giving you a chance to manually solve a CAPTCHA in the visible
  window if one appears) before giving up on that run. This is inherently
  fragile — REA can change page structure or bot-detection at any time. Keep
  search volume modest (small "max pages", don't run huge batches back to
  back) to reduce block/ban risk.
- **Overlays**: `backend/overlays.py` calls the VicPlan ArcGIS services
  described above. Solid and unlikely to break.
- Only Victoria is wired up for now. Other states would need their own
  planning-portal lookup added to `overlays.py` (each state runs its own
  spatial service under a different URL/schema).

## Setup

```bash
cd ~/Desktop/realestate-tool
python3 -m venv venv
source venv/bin/activate
pip install -r backend/requirements.txt
patchright install chromium
```

## Running it

```bash
cd ~/Desktop/realestate-tool
source venv/bin/activate
uvicorn main:app --app-dir backend --port 8000
```

Then open http://127.0.0.1:8000 in your browser, enter a suburb, price
range, and land size range, and hit Search.

**A separate Chrome window will pop open and navigate itself** — this is the
scraper working, not a bug. Leave it alone while it runs. If it ever shows a
CAPTCHA/"verify you're human" page, solve it manually in that window; the
scraper waits ~15s specifically to give you that chance, and the session
gets saved to `.browser-profile/` so you shouldn't have to solve it again
for a while.

A search takes 20-60+ seconds because it's driving a real browser
page-by-page and then doing a geocode + overlay lookup per result.

## If the scrape still doesn't work

If you consistently get 0 results / a permanent block page even after
solving any CAPTCHA, Kasada is winning and a bare browser-automation
approach isn't going to be reliable for this site. Realistic fallbacks,
in order of effort:
1. Try running with a real installed Chrome instead of the bundled
   Chromium — in `scraper.py`'s `launch_persistent_context` call, add
   `channel="chrome"` (requires Chrome installed on your Mac).
2. Switch to a paid scraping API that already handles Kasada for this
   exact site (e.g. Apify's realestate.com.au scrapers) — usage-based
   pricing, would replace `scraper.py`'s browser automation with a simple
   API call.
3. Switch to parsing realestate.com.au's own saved-search email alerts
   instead of live scraping — zero ban risk, not truly live/on-demand.

## Known limitations / next steps

- Land size is sometimes missing on listings (agents don't always supply
  it) — those are excluded when a land size filter is set.
- The scraper's JSON extraction is defensive (scans for anything shaped
  like a listing) rather than hard-coded to one exact path, to survive
  minor REA site changes — but a larger redesign of their page will still
  break it and require updating `scraper.py`.
- No caching/persistence yet — every search re-scrapes live. If this gets
  used a lot, worth adding a local cache (e.g. SQLite) keyed by search
  params + a TTL, both to speed up repeat searches and reduce load on
  realestate.com.au.
- Only VIC overlays are implemented. Extending to other states means
  finding each state's equivalent spatial/planning API and adding a
  matching lookup function.
