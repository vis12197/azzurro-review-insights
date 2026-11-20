# Azzurro Hotels: Booking.com Review Insights

Collects public Booking.com guest reviews for the four Azzurro properties, stores them in
SQLite, tags them with operational topics, and shows everything in a dashboard built for
operations staff (no technical knowledge needed).

| Property | Booking.com page |
|---|---|
| Olympic Hotel Paddington | `olympic-paddington` |
| Potts Point | `venus-potts-point-sydney` |
| Central Sydney | `venus-surry-hills` |
| Darling Harbour | `chateau-de-venus` |

---

## Quick start

Requires Python 3.11+ (tested on 3.12, macOS).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium        # one-off, downloads the browser used for collection
```

**Open the dashboard** (the repo already includes collected data in `data/reviews.db`):

```bash
streamlit run dashboard.py
```

Then open http://localhost:8501.

**Collect new reviews:**

```bash
python -m app.collector            # incremental: stops when it reaches reviews it already has
python -m app.collector --full     # rescan everything (picks up edited reviews / new hotel replies)
python -m app.collector --property olympic-paddington --headful   # one hotel, visible browser
```

Staff can also click **Refresh reviews** in the dashboard sidebar. It runs the same command in the
background (the page stays usable) and the sidebar shows when each hotel last updated. A file lock
(`data/collector.lock`) means only one collection runs at a time, however many times it's clicked
or if cron overlaps.
For daily updates, schedule it, e.g. with cron:

```
0 6 * * * cd /path/to/azzurro-review-insights && .venv/bin/python -m app.collector >> data/collector.log 2>&1
```

**Run tests:** `pytest -q`

---

## Architecture

```
config/properties.json      the 4 hotels (slug, display name, URL); add a hotel here
app/collector.py            Playwright collector: open page -> capture ReviewList call -> page through it
app/db.py                   SQLite schema, upsert/dedup, run log
app/classify.py             sentiment bands + topic keyword rules
app/insights.py             weekly averages, comparisons, topic shares, plain-English sentences
dashboard.py                Streamlit UI
data/reviews.db             collected data (SQLite)
data/sample_reviews.csv     same data as CSV for quick inspection
tests/test_core.py          validation, dedup, classification and metric tests
```

```
Booking.com page ──(headless Chromium)──> ReviewList GraphQL (JSON)
        │                                        │
        └─ fallback: rendered review cards       ▼
                                   validate ─> upsert into SQLite ─> scrape_runs log
                                                          │
                                         insights.py (pandas) ─> Streamlit dashboard
```

**Why these choices**
- **Python + Playwright**: Booking needs a real browser (see below). Playwright is the most
  reliable way to drive one headless.
- **SQLite**: four hotels produce a few thousand reviews; a single file needs no server, is easy to
  ship as sample data, and can be swapped for Postgres later with the same schema.
- **Streamlit**: fastest route to a clean, interactive UI that non-technical staff can use, in the
  same language as the rest of the code, so one person can maintain it.

---

## How review collection works

Booking.com has no public review API, and a plain HTTP request to a hotel or review page returns an
**AWS WAF JavaScript challenge** instead of content (the legacy `reviewlist.html` endpoint now
returns 404). So:

1. **Open the real page in headless Chromium.** The browser runs the WAF check the same way a
   guest's browser would. No CAPTCHA solving, no stealth plugins, no logins or cookies are used.
2. **Capture Booking's own review request.** The page loads reviews from
   `/dml/graphql` (operation `ReviewList`). The collector listens for that request (clicking
   "Read all reviews" to trigger it) and records its URL, headers and body.
3. **Replay it from inside the page** with `sorter: NEWEST_FIRST` and paging (`skip`/`limit=25`),
   using the page's own `fetch`, so it carries the same session the browser already has.
   Because the request is captured live on every run, changes to Booking's persisted-query hash
   or headers are picked up automatically instead of being hard-coded.
4. **Fallback:** if the GraphQL request can't be captured (e.g. a page redesign), the collector
   reads the review cards rendered on the page. That gets fewer reviews, so the run is logged as
   `partial` and shown in orange in the dashboard's "Data freshness" panel.

**Fields stored:** score, title, liked / disliked text, language, publish date, check-in/out dates,
nights, room type, traveller type, guest country, hotel reply, helpful votes, plus the full source
record (`raw_json`) so data can be re-parsed later without re-scraping. Booking's own category
scores (Cleanliness, Staff, Value...) are stored per run as well.

**Privacy:** guest names and avatar URLs are dropped before anything is saved. Country and
traveller type are kept because they are useful and not identifying on their own.

### Duplicates and updates
- Each review is keyed by Booking's own review id (`reviewUrl`) prefixed with the property slug.
  Re-running never creates duplicates.
- A review already stored is **updated in place** if anything changed (a guest edit, or the hotel
  adding a reply); `first_seen_at` is preserved and `last_seen_at` refreshed.
- **Incremental mode** (default) reads newest first and stops at the first page that contains no
  new reviews. `--full` rescans everything.

### Errors and data quality
- Each request is retried up to 4 times with exponential backoff; after the second failure the
  page is reloaded to get a fresh WAF token.
- A random 1.5 to 3.5 s pause between pages keeps the load on Booking low.
- One hotel failing never stops the others; the outcome of every run (fetched / new / updated /
  rejected / total shown on Booking / error message) is written to the `scrape_runs` table and
  shown in the dashboard sidebar.
- Every record is validated: it must have an id, a score between 1 and 10, and a publish date that
  is not in the future. Bad records are counted as `rejected`, not stored.
- If the response shape changes (`data.reviewListFrontend.reviewCard` missing, GraphQL errors),
  the collector raises `SchemaChanged` immediately rather than retrying or storing garbage.
- The run log records Booking's own total review count, so a gap between "on site" and "stored"
  is easy to spot.

---

## How the insights work

**Sentiment** uses the guest's own score, not text analysis:
Positive is 8 or above, Mixed is 6 to 7.9, Negative is below 6. A guest's number is more reliable
than a model guessing tone from two short sentences. The thresholds live in `app/classify.py`.

**Topics** (Cleanliness, Check-in, Staff, Noise, Facilities, Location, Room condition, Value for
money) come from keyword rules. The key point is that Booking asks guests two separate questions,
"What did you like?" and "What didn't you like?":
- a topic found in the **disliked** box counts as a **complaint**;
- a topic found in the **liked** box counts as **praise**.

That split does most of the work a sentiment model would otherwise have to guess at, and means a
9/10 review that says "no lift" still shows up as a Facilities complaint. Non-answers in the
disliked box ("nothing", "n/a", "all good") are ignored. The dashboard shows each review's tags,
so staff can see why something was counted.

**Example output** (from the dashboard's "What guests are saying" panel):
> 22% of negative reviews this week mentioned facilities issues (2 of 9 negative reviews with English comments).

**Weeks** run Monday to Sunday in Sydney time. "This week" is Monday up to today.

### Accuracy check
I read a random sample of 40 English complaints next to their tags. Most tags were right; I'd
put precision at roughly **75 to 80%**. The main errors were broad words ("walk", "nearby",
"sleep", "code") firing on the wrong topic; those rules were tightened. Recall is lower for
complaints phrased without a keyword ("very bad communication" isn't tagged).

---

## Known limitations and assumptions

- **Topics only cover English reviews.** About half of the reviews with text are in other
  languages (French, German, Spanish, Japanese...). They count in every average and in the
  positive/negative trend, but get no topic tags rather than wrong ones. The dashboard states this
  coverage next to every topic figure. Next step: classify with an LLM (e.g. Claude), which handles
  any language and phrasing, keeping the keyword rules as a cheap, auditable baseline.
- **About 40% of reviews are score-only** (no text), so topic shares are based on the reviewers who
  wrote something.
- **Small weekly samples.** Each hotel gets a handful of reviews a week, so weekly averages swing.
  The dashboard flags weeks with fewer than 3 reviews and shows the figures for the selected date
  range (default: last 3 months) above them for comparison.
- **Keyword rules** miss sarcasm, negation ("not dirty at all" in the disliked box is rare but
  possible) and misspellings.
- **Depends on Booking's front end.** If Booking changes the `ReviewList` operation or blocks
  headless browsers, collection falls back to the DOM, then fails loudly in the run log. It does not
  silently return wrong data. Booking's terms restrict automated access; for production use, the
  hotels' own Booking.com extranet (via a channel manager or Booking's Connectivity Partner
  programme) is the sanctioned source of the same reviews, and this collector could be replaced by
  that feed with the rest of the app unchanged.
- **Review dates** are the date Booking published the review, not the stay date (both are stored).
- The **Refresh** button runs the collector inside the dashboard process; fine for a single office
  machine, but a hosted version should run collection as a scheduled job instead.
