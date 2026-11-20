"""Collect Booking.com reviews for the configured properties.

How it works (see README "Review collection" for the reasoning):
  1. Open the public property page in a real (headless) Chromium via Playwright. Booking sits
     behind an AWS WAF JavaScript check, so plain HTTP requests get a 202/404 challenge page;
     a real browser runs that check the same way a guest's browser does.
  2. Booking's own front end loads reviews from its GraphQL endpoint (operation "ReviewList").
     We listen for that request, then replay it from inside the same page with different
     paging/sort variables. Because we capture the request live on every run, a change to
     Booking's persisted-query hash or required headers is picked up automatically.
  3. If the GraphQL request can't be captured (page redesign), fall back to reading the
     review cards from the page DOM. That gives fewer reviews, and the run is marked "partial".

Every record is validated before it is stored; rejects are counted on the run log, not stored.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import random
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import Page, async_playwright

from . import db

log = logging.getLogger("collector")

ROOT = Path(__file__).resolve().parent.parent
PROPERTIES_FILE = ROOT / "config" / "properties.json"

PAGE_SIZE = 25
MAX_RETRIES = 4
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
# Headers the browser sets itself; fetch() refuses or ignores them.
_SKIP_HEADERS = {"content-length", "cookie", "host", "origin", "referer", "user-agent",
                 "accept-encoding", "connection"}

CATEGORY_NAMES = {
    "hotel_staff": "Staff", "hotel_services": "Facilities", "hotel_clean": "Cleanliness",
    "hotel_comfort": "Comfort", "hotel_value": "Value for money", "hotel_location": "Location",
    "hotel_free_wifi": "Free WiFi",
}


class SchemaChanged(Exception):
    """Booking's response no longer has the shape we expect."""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_properties() -> list[dict]:
    return json.loads(PROPERTIES_FILE.read_text())


# --------------------------------------------------------------------------- normalisation

def _strip_personal(card: dict) -> dict:
    """Drop guest name and avatar before storing. We only need country and traveller type."""
    card = json.loads(json.dumps(card))
    gd = card.get("guestDetails") or {}
    for k in ("username", "avatarUrl", "avatarColor"):
        gd.pop(k, None)
    card.pop("editUrl", None)
    return card


def _reply_text(reply) -> str | None:
    if not reply:
        return None
    if isinstance(reply, dict):
        return reply.get("reply") or reply.get("text") or json.dumps(reply, ensure_ascii=False)
    return str(reply)


def normalise_card(card: dict, prop: dict, seen_at: str) -> dict:
    """Turn one GraphQL ReviewCard into a DB row. Raises ValueError if it fails validation."""
    review_id = card.get("reviewUrl")
    score = card.get("reviewScore")
    ts = card.get("reviewedDate")
    if not review_id:
        raise ValueError("missing review id")
    if not isinstance(score, (int, float)) or not 1 <= score <= 10:
        raise ValueError(f"score out of range: {score!r}")
    if not isinstance(ts, (int, float)) or ts <= 0:
        raise ValueError(f"bad reviewedDate: {ts!r}")
    reviewed = datetime.fromtimestamp(ts, tz=timezone.utc)
    if reviewed > datetime.now(timezone.utc):
        raise ValueError("review dated in the future")

    text = card.get("textDetails") or {}
    booking = card.get("bookingDetails") or {}
    guest = card.get("guestDetails") or {}
    clean = _strip_personal(card)
    return {
        "review_id": f"{prop['slug']}:{review_id}",
        "property_slug": prop["slug"],
        "property_name": prop["name"],
        "score": float(score),
        "title": (text.get("title") or "").strip() or None,
        "positive_text": (text.get("positiveText") or "").strip() or None,
        "negative_text": (text.get("negativeText") or "").strip() or None,
        "review_lang": text.get("lang"),
        "reviewed_at": reviewed.isoformat(),
        "checkin_date": booking.get("checkinDate"),
        "checkout_date": booking.get("checkoutDate"),
        "num_nights": booking.get("numNights"),
        "room_type": (booking.get("roomType") or {}).get("name"),
        "traveller_type": guest.get("guestTypeTranslation"),
        "guest_country": guest.get("countryName"),
        "hotel_reply": _reply_text(card.get("partnerReply")),
        "helpful_votes": card.get("helpfulVotesCount") or 0,
        "first_seen_at": seen_at,
        "last_seen_at": seen_at,
        "raw_json": db.dumps(clean),
    }


def parse_review_list(payload: dict) -> dict:
    """Validate the GraphQL response shape and return the reviewListFrontend object."""
    if payload.get("errors"):
        raise SchemaChanged(f"GraphQL errors: {payload['errors'][:1]}")
    rl = (payload.get("data") or {}).get("reviewListFrontend")
    if not isinstance(rl, dict) or "reviewCard" not in rl:
        raise SchemaChanged("response has no data.reviewListFrontend.reviewCard")
    if rl.get("reviewCard") is not None and not isinstance(rl["reviewCard"], list):
        raise SchemaChanged("reviewCard is not a list")
    return rl


# --------------------------------------------------------------------------- browser work

class PropertySession:
    """One browser page on one property, able to replay the ReviewList GraphQL call."""

    def __init__(self, page: Page, prop: dict):
        self.page = page
        self.prop = prop
        self.template: dict | None = None  # {"url", "headers", "body"}

    async def open(self) -> None:
        captured: asyncio.Future = asyncio.get_event_loop().create_future()

        def on_request(req):
            if req.method == "POST" and "/dml/graphql" in req.url and not captured.done():
                body = req.post_data or ""
                if '"operationName":"ReviewList"' in body:
                    captured.set_result({"url": req.url, "headers": req.headers, "body": body})

        self.page.on("request", on_request)
        resp = await self.page.goto(self.prop["url"], wait_until="domcontentloaded", timeout=60000)
        log.info("%s: page status %s", self.prop["slug"], resp.status if resp else "?")
        await self.page.wait_for_timeout(4000)  # let the WAF check + app hydrate

        # The review list loads lazily; opening "Read all reviews" (or scrolling) triggers it.
        for _ in range(3):
            if captured.done():
                break
            for sel in ('[data-testid="fr-read-all-reviews"]',
                        '[data-testid="review-score-read-all"]',
                        '[data-testid="Property-Header-Nav-Tab-Trigger-reviews"]'):
                el = self.page.locator(sel).first
                if await el.count():
                    try:
                        await el.click(timeout=5000)
                        break
                    except Exception:  # noqa: BLE001 - overlay/cookie banner, try next
                        continue
            else:
                await self.page.mouse.wheel(0, 3000)
            try:
                await asyncio.wait_for(asyncio.shield(captured), timeout=10)
            except asyncio.TimeoutError:
                pass
        self.page.remove_listener("request", on_request)
        if captured.done():
            t = captured.result()
            t["headers"] = {k: v for k, v in t["headers"].items()
                            if k.lower() not in _SKIP_HEADERS and not k.startswith(":")}
            self.template = t

    async def fetch_page(self, skip: int, limit: int = PAGE_SIZE) -> dict:
        assert self.template
        body = json.loads(self.template["body"])
        inp = body["variables"]["input"]
        inp.update({"skip": skip, "limit": limit, "sorter": "NEWEST_FIRST"})
        inp["filters"] = {"text": ""}
        result = await self.page.evaluate(
            """async ({url, headers, body}) => {
                const r = await fetch(url, {method: 'POST', headers, body, credentials: 'include'});
                return {status: r.status, text: await r.text()};
            }""",
            {"url": self.template["url"], "headers": self.template["headers"], "body": json.dumps(body)},
        )
        if result["status"] != 200:
            raise RuntimeError(f"HTTP {result['status']}")
        try:
            payload = json.loads(result["text"])
        except json.JSONDecodeError as e:
            raise RuntimeError("non-JSON response (probably a WAF challenge page)") from e
        return parse_review_list(payload)

    async def dom_fallback(self) -> list[dict]:
        """Last resort: read whatever review cards are rendered on the page."""
        cards = await self.page.evaluate(
            """() => [...document.querySelectorAll('[data-testid="review-card"]')].map(c => {
                const q = s => (c.querySelector(`[data-testid="${s}"]`) || {}).innerText || null;
                return {score: q('review-score'), title: q('review-title'), pos: q('review-positive-text'),
                        neg: q('review-negative-text'), date: q('review-date'), room: q('review-room-name'),
                        traveller: q('review-traveler-type')};
            })"""
        )
        out = []
        for c in cards:
            key = hashlib.sha1(json.dumps(c, sort_keys=True).encode()).hexdigest()[:16]
            out.append({"_dom": True, "key": key, **c})
        return out


async def with_retries(fn, what: str, session: PropertySession):
    delay = 2.0
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return await fn()
        except SchemaChanged:
            raise  # retrying won't help; surface it
        except Exception as e:  # noqa: BLE001
            if attempt == MAX_RETRIES:
                raise
            log.warning("%s failed (attempt %d/%d): %s - retrying in %.0fs",
                        what, attempt, MAX_RETRIES, e, delay)
            await asyncio.sleep(delay + random.random())
            delay *= 2
            if attempt == 2:  # token may have expired: reload the page and recapture
                await session.open()


def _dom_to_record(c: dict, prop: dict, seen_at: str) -> dict:
    try:
        score = float((c.get("score") or "").split()[-1].replace(",", "."))
    except (ValueError, IndexError):
        raise ValueError("unreadable score in DOM card")
    if not 1 <= score <= 10:
        raise ValueError("score out of range")
    reviewed = None
    for fmt in ("Reviewed: %d %B %Y", "%d %B %Y"):
        try:
            reviewed = datetime.strptime((c.get("date") or "").strip(), fmt).replace(tzinfo=timezone.utc)
            break
        except ValueError:
            pass
    if reviewed is None:
        raise ValueError("unreadable date in DOM card")
    return {
        "review_id": f"{prop['slug']}:dom-{c['key']}", "property_slug": prop["slug"],
        "property_name": prop["name"], "score": score, "title": c.get("title"),
        "positive_text": c.get("pos"), "negative_text": c.get("neg"), "review_lang": None,
        "reviewed_at": reviewed.isoformat(), "checkin_date": None, "checkout_date": None,
        "num_nights": None, "room_type": c.get("room"), "traveller_type": c.get("traveller"),
        "guest_country": None, "hotel_reply": None, "helpful_votes": 0,
        "first_seen_at": seen_at, "last_seen_at": seen_at, "raw_json": db.dumps(c),
    }


async def collect_property(browser, conn, prop: dict, full: bool, max_pages: int) -> dict:
    started = now_iso()
    run_id = db.start_run(conn, prop["slug"], started)
    stats = {"fetched": 0, "inserted": 0, "updated": 0, "rejected": 0}
    ctx = await browser.new_context(locale="en-GB", user_agent=USER_AGENT,
                                    viewport={"width": 1400, "height": 900},
                                    timezone_id="Australia/Sydney")
    page = await ctx.new_page()
    session = PropertySession(page, prop)
    known = db.known_ids(conn, prop["slug"])
    status, method, message, total = "ok", "graphql", None, None
    try:
        await with_retries(session.open, f"{prop['slug']} open", session)
        if session.template is None:
            # ---- degraded path
            method, status = "dom-fallback", "partial"
            message = "ReviewList request not seen; read rendered cards only"
            log.warning("%s: %s", prop["slug"], message)
            for c in await session.dom_fallback():
                stats["fetched"] += 1
                try:
                    result = db.upsert_review(conn, _dom_to_record(c, prop, started))
                except ValueError:
                    stats["rejected"] += 1
                    continue
                if result in ("inserted", "updated"):
                    stats[result] += 1
            conn.commit()
        else:
            skip, pages = 0, 0
            while pages < max_pages:
                rl = await with_retries(lambda: session.fetch_page(skip), f"{prop['slug']} skip={skip}", session)
                if pages == 0:
                    total = rl.get("reviewsCount")
                    scores = {CATEGORY_NAMES.get(s["name"], s.get("translation") or s["name"]): s["value"]
                              for s in rl.get("ratingScores") or [] if s.get("value") is not None}
                    db.save_category_scores(conn, prop["slug"], scores, started[:10])
                cards = rl.get("reviewCard") or []
                new_on_page = 0
                for card in cards:
                    stats["fetched"] += 1
                    try:
                        rec = normalise_card(card, prop, started)
                    except ValueError as e:
                        stats["rejected"] += 1
                        log.warning("%s: rejected review: %s", prop["slug"], e)
                        continue
                    if rec["review_id"] not in known:
                        new_on_page += 1
                    result = db.upsert_review(conn, rec)
                    if result in ("inserted", "updated"):
                        stats[result] += 1
                conn.commit()
                pages += 1
                skip += len(cards)
                log.info("%s: page %d, %d cards, %d new (total on site %s)",
                         prop["slug"], pages, len(cards), new_on_page, total)
                if len(cards) < PAGE_SIZE or (total is not None and skip >= total):
                    break
                # Incremental stop: this page had nothing new AND we already hold as many reviews
                # as Booking says exist. The second check means an interrupted first run gets
                # backfilled next time instead of stopping at the newest known review.
                stored = len(known) + stats["inserted"]
                if not full and known and new_on_page == 0 and total is not None and stored >= total:
                    log.info("%s: up to date (%d stored, %d on site) - stopping",
                             prop["slug"], stored, total)
                    break
                await asyncio.sleep(1.5 + random.random() * 2)  # be polite
    except Exception as e:  # noqa: BLE001 - one property failing must not stop the others
        status, message = ("partial" if stats["fetched"] else "failed"), f"{type(e).__name__}: {e}"
        log.error("%s: %s", prop["slug"], message)
    finally:
        await ctx.close()
        db.finish_run(conn, run_id, finished_at=now_iso(), status=status, method=method,
                      total_on_site=total, message=message, **stats)
    return {"property": prop["slug"], "status": status, **stats, "total_on_site": total}


async def collect(full: bool = False, only: list[str] | None = None, headful: bool = False,
                  max_pages: int = 200, db_path=db.DEFAULT_DB) -> list[dict]:
    props = [p for p in load_properties() if not only or p["slug"] in only]
    conn = db.connect(db_path)
    results = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not headful)
        try:
            for prop in props:
                results.append(await collect_property(browser, conn, prop, full, max_pages))
        finally:
            await browser.close()
    conn.close()
    return results


def main() -> None:
    ap = argparse.ArgumentParser(description="Collect Booking.com reviews into data/reviews.db")
    ap.add_argument("--full", action="store_true", help="rescan every page, not just new reviews")
    ap.add_argument("--property", action="append", help="limit to one slug (repeatable)")
    ap.add_argument("--headful", action="store_true", help="show the browser window (debugging)")
    ap.add_argument("--max-pages", type=int, default=200)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    results = asyncio.run(collect(args.full, args.property, args.headful, args.max_pages))
    print("\nSummary")
    for r in results:
        print(f"  {r['property']:<28} {r['status']:<8} fetched={r['fetched']:<4} new={r['inserted']:<4} "
              f"updated={r['updated']:<4} rejected={r['rejected']:<3} on_site={r['total_on_site']}")
    if any(r["status"] == "failed" for r in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
