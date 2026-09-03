# Artistern Radar

A self-updating static page that sweeps the art press, the MENA/Iranian art
magazines, and the region's institutions once a day, and puts everything on one
page for the Artistern content team to check each morning.

No build step, no framework, no server. A GitHub Action runs a Python script,
the script writes `data.json`, and `index.html` reads that file in the browser.

---

## How it works

```
scripts/feeds_config.json     the source registry — the only file you normally edit
        │
        ▼
scripts/build_digest.py       fetches every feed, filters, writes ↓
        │
        ▼
data.json                     the digest, committed back by the Action
        │
        ▼
index.html                    fetches data.json client-side and renders it
```

`.github/workflows/daily-digest.yml` runs the script at **05:00 UTC** daily,
on demand via **Run workflow** (`workflow_dispatch`), and whenever the config or
script changes on `main`. It commits `data.json` **only if the content changed** —
a run where nothing new was published makes no commit, so the history stays
meaningful.

### Rules the build applies

| Rule | Value | Why |
|---|---|---|
| Rolling window | **180 days** | The regional institutions publish every 2–6 months. At 30 days none of them reach the digest and the page is nothing but the daily Western art wires. |
| Cap per feed | **8 items** | Stops a daily publisher (The Art Newspaper) burying a monthly one (Barjeel). |
| Fetch isolation | per feed | One feed timing out never stops the others. Every failure lands in `errors`. |
| Retries | 2, backing off | Absorbs a transient blip without hiding a genuinely dead feed. |
| Undated items | fallback only | Kept only when a feed gives us nothing dated at all. |
| Quiet sources | newest item kept | A source that publishes every few months stays visible, dated and demoted, instead of disappearing. |

### What the page shows

- **Regional signal** — every item across all categories that names a MENA or
  Iranian place, institution or subject, lifted to the top. The general art
  wires publish mostly Western news; without this the regional story is buried.
- **Categories** → sources → items, newest first.
- **Quiet sources** — reachable feeds with nothing inside the window, showing
  their most recent post and its age.
- **Also worth checking manually** — every source with no usable feed, and the
  reason it is there. Nothing is silently dropped.
- **Social Media Directory** — static. Instagram and X have no reliable free
  feed API, so these are never fetched, scraped, or invented.
- **Feed status log** — the `errors` array, rendered. Failures are visible so
  they can be fixed, not hidden.

`MENA` and `needs translation` tags are applied automatically from the item text.

---

## Source coverage

Every source on the brief is accounted for on the page — either fetched
automatically or listed under *Also worth checking manually* with the reason.

- **21** sources have a working feed and are fetched (13 delivering today).
- **42** have no usable feed and are listed for manual checking.

The most common reasons a source cannot be automated:

- **Artlogic galleries** (The Third Line, Ayyam, Athr, IVDE, Dastan, L'Atelier 21,
  Loft Art) publish a `/feed/` that is an **artwork-inventory image feed** —
  undated entries linking to CDN images, not news. It parses as valid RSS, which
  makes it a trap: fetching it would flood the digest with untitled artworks.
- **Auction houses** (Christie's, Sotheby's, Bonhams) have retired public RSS.
- **Institutions** (Sharjah Art Foundation, Mathaf, Sursock, MACAAL, Louvre Abu
  Dhabi, Ithra) render news as pages only, or sit behind bot protection.
- **Jameel Arts Centre** serves a `/feed/` that is permanently empty.

---

## Editing sources

Everything lives in **`scripts/feeds_config.json`**. You should not need to touch
the Python or the HTML to add, fix or retire a source.

### Add a feed

```jsonc
{
  "name": "Gallery Name",                    // shown as the source heading
  "url":  "https://example.com/feed/",       // the RSS/Atom URL itself
  "category": "Galleries",                   // must match one in category_order
  "site": "https://example.com/",            // where the heading links
  "lang": "fa"                               // optional: fa/ar/fr → translation tag
}
```

### Fix a feed that started failing

1. Open the **Feed status log** on the page, or the workflow run's job summary —
   both name the source, the URL tried, and the exact error.
2. Find the real feed URL. In order, try: view-source on the site and search for
   `application/rss+xml`, then `/feed/`, `/feed`, `/rss`, `/rss.xml`, `/feed.xml`,
   `/atom.xml`, `/index.xml`.
3. Update `url` in `feeds_config.json`, commit, then **Actions → Daily digest →
   Run workflow** to rebuild immediately.

### Retire a source to the manual list

Move it from `feeds` to `manual_sources` and say why — the reason is rendered on
the page, so the next person does not re-investigate it:

```jsonc
{ "name": "Gallery Name", "url": "https://example.com/",
  "category": "Galleries", "reason": "No feed; all candidate paths return 404." }
```

### Gate a noisy whole-site feed

When a publisher offers only one feed for the whole title, add `"filter_terms": "art"`
to keep just the art coverage. The preset is deliberately strict — loose words
like *design*, *culture* and *collection* let luxury-brand PR through.

### Change the window or the per-feed cap

Both are in `scripts/feeds_config.json` — no Python edit needed:

```jsonc
"window_days": 180,
"max_items_per_feed": 8,
```

> **Why 180 and not 30.** The regional institutions publish on a 2–6 month
> cadence — Delfina ~56 days, Kamel Lazaar ~77, 1-54 ~96, Barjeel ~176. At a
> 30-day window every one of them falls out of the digest and the page becomes
> the daily Western art wires with a MENA banner on top. At 180 days they are
> all in: 13 of 21 feeds deliver instead of 9, and 78 items instead of 55.
> The per-feed cap of 8 is what stops the wider window turning into a wall of
> The Art Newspaper. Lower the window if the page starts feeling stale rather
> than broad.

---

## Running it locally

```bash
pip install feedparser
python3 scripts/build_digest.py      # writes data.json, prints a per-feed report
python3 -m http.server 8000          # then open http://localhost:8000
```

Opening `index.html` straight off disk will **not** work — browsers block
`fetch` on `file://`. Serve the folder.

The script always exits `0`. A broken feed is data for the status log, not a
reason to fail the build and take the other twenty feeds down with it.

---

## Deployment

GitHub Pages, served from `main` at the repository root. Nothing to build.

**Settings → Pages → Source: Deploy from a branch → `main` / `/ (root)`.**

For the Action to commit `data.json`, **Settings → Actions → General → Workflow
permissions** must be set to **Read and write permissions**.
