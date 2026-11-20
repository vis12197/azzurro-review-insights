"""Tests for the parts that don't need a browser: validation, dedup, classification, metrics."""
from datetime import date

import pytest

from app import classify, collector, db, insights

PROP = {"slug": "test-hotel", "name": "Test Hotel", "url": "https://example.invalid"}


def card(review_id="abc123", score=8.0, ts=1795060642, pos="Great location", neg="Room was dirty",
         **extra):
    c = {
        "reviewUrl": review_id, "reviewScore": score, "reviewedDate": ts,
        "textDetails": {"title": "Ok stay", "positiveText": pos, "negativeText": neg, "lang": "en"},
        "bookingDetails": {"checkinDate": "2026-10-23", "checkoutDate": "2026-10-24", "numNights": 1,
                           "roomType": {"name": "Double Room"}},
        "guestDetails": {"username": "Jane", "avatarUrl": "http://x", "countryName": "Australia",
                         "guestTypeTranslation": "Couple"},
        "partnerReply": None, "helpfulVotesCount": 0,
    }
    c.update(extra)
    return c


# ---------------------------------------------------------------- validation

def test_normalise_strips_personal_data():
    rec = collector.normalise_card(card(), PROP, "2026-11-20T00:00:00+00:00")
    assert "Jane" not in rec["raw_json"] and "avatarUrl" not in rec["raw_json"]
    assert rec["guest_country"] == "Australia"
    assert rec["review_id"] == "test-hotel:abc123"


@pytest.mark.parametrize("bad", [
    {"reviewUrl": None}, {"reviewScore": 0}, {"reviewScore": 11}, {"reviewScore": "9"},
    {"reviewedDate": None}, {"reviewedDate": 4102444800},  # year 2100
])
def test_normalise_rejects_bad_records(bad):
    with pytest.raises(ValueError):
        collector.normalise_card(card(**bad), PROP, "2026-11-20T00:00:00+00:00")


def test_schema_change_detected():
    with pytest.raises(collector.SchemaChanged):
        collector.parse_review_list({"data": {"somethingElse": {}}})
    with pytest.raises(collector.SchemaChanged):
        collector.parse_review_list({"errors": [{"message": "PersistedQueryNotFound"}]})


# ---------------------------------------------------------------- dedup / updates

def test_upsert_deduplicates_and_updates(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    seen = "2026-11-20T00:00:00+00:00"
    r1 = collector.normalise_card(card(), PROP, seen)
    assert db.upsert_review(conn, r1) == "inserted"
    assert db.upsert_review(conn, r1) == "unchanged"
    edited = collector.normalise_card(card(partnerReply={"reply": "Sorry, fixed"}), PROP,
                                      "2026-11-21T00:00:00+00:00")
    assert db.upsert_review(conn, edited) == "updated"
    rows = conn.execute("SELECT hotel_reply, first_seen_at FROM reviews").fetchall()
    assert len(rows) == 1
    assert rows[0]["hotel_reply"] == "Sorry, fixed"
    assert rows[0]["first_seen_at"] == seen  # first-seen date is preserved


# ---------------------------------------------------------------- classification

def test_sentiment_bands():
    assert classify.sentiment(10) == "Positive"
    assert classify.sentiment(8) == "Positive"
    assert classify.sentiment(7.9) == "Mixed"
    assert classify.sentiment(6) == "Mixed"
    assert classify.sentiment(5.9) == "Negative"


@pytest.mark.parametrize("text,expected", [
    ("The bathroom was dirty and smelled of mould", "Cleanliness"),
    ("Receptionist was rude to us", "Staff"),
    ("Very noisy at night, could hear the pub", "Noise"),
    ("We had to wait an hour for check-in", "Check-in"),
    ("No lift and the wifi kept dropping", "Facilities"),
    ("Overpriced for what you get", "Value for money"),
    ("Mattress was old and the pillows were flat", "Room condition"),
    ("Great location, close to the station", "Location"),
])
def test_topics(text, expected):
    assert expected in classify.topics_in(text)


@pytest.mark.parametrize("text", ["Nothing", "nothing.", "N/A", "None", "No complaints",
                                  "Nothing really", "-", "all good", ""])
def test_non_answers_are_not_complaints(text):
    assert classify.topics_in(text) == []


def test_toilet_door_code_is_not_check_in():
    assert "Check-in" not in classify.topics_in("We needed to enter some codes to open the toilets")
    assert "Check-in" in classify.topics_in("Check in was by zoom call, no one at the desk")


def test_only_english_is_tagged():
    assert classify.is_taggable_language("en")
    assert not classify.is_taggable_language("fr")
    assert not classify.is_taggable_language(None)


def test_complaint_vs_praise_split():
    out = classify.classify("Staff were lovely", "Room was filthy")
    assert out["praise"] == ["Staff"]
    assert "Cleanliness" in out["complaints"] and "Staff" not in out["complaints"]


# ---------------------------------------------------------------- metrics

def test_week_bounds_monday_start():
    this_start, last_start, _ = insights.week_bounds(date(2026, 11, 20))  # a Friday
    assert this_start == date(2026, 11, 16)
    assert last_start == date(2026, 11, 9)


def test_week_comparison(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    # Tue 17 Nov (this week) score 9; Tue 10 Nov (last week) score 5
    from datetime import datetime, timezone
    def ts(y, m, d):  # midday UTC = late evening Sydney, same calendar day
        return int(datetime(y, m, d, 2, tzinfo=timezone.utc).timestamp())
    for rid, score, ts in [("a", 9, ts(2026, 11, 17)), ("b", 5, ts(2026, 11, 10))]:
        db.upsert_review(conn, collector.normalise_card(card(review_id=rid, score=score, ts=ts),
                                                        PROP, "2026-11-20T00:00:00+00:00"))
    conn.commit()
    df = insights.load_reviews(conn)
    wk = insights.week_comparison(df, date(2026, 11, 20))
    assert wk["this_avg"] == 9 and wk["last_avg"] == 5 and wk["delta"] == 4
    assert wk["low_sample"] is True


def test_collector_lock_blocks_second_run():
    held = db.acquire_collector_lock()
    try:
        assert held is not None
        assert db.acquire_collector_lock() is None  # second collector is refused
        assert db.collector_running() is True
    finally:
        held.close()
    assert db.collector_running() is False


def test_round1_rounds_halves_up():
    assert insights.round1(4.25) == 4.3
    assert insights.round1(4.24) == 4.2
    assert insights.round1(6.05) == 6.1


def test_busy_is_not_location():
    assert "Location" not in classify.topics_in("Bathrooms were busy in the morning")
    assert "Location" in classify.topics_in("The bus stop is right outside")
