"""Turn stored reviews into the numbers and sentences the dashboard shows.

All dates are handled in Sydney time and weeks run Monday to Sunday, because that is how the
hotels' ops team thinks about "this week".
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from . import classify, db

TZ = "Australia/Sydney"
MIN_SAMPLE = 3  # below this many reviews, a weekly average is flagged as unreliable


def load_reviews(conn=None) -> pd.DataFrame:
    conn = conn or db.connect()
    df = pd.read_sql_query("SELECT * FROM reviews", conn)
    if df.empty:
        return df
    text_cols = ["title", "positive_text", "negative_text", "hotel_reply", "room_type",
                 "traveller_type", "guest_country"]
    df[text_cols] = df[text_cols].astype(object).where(df[text_cols].notna(), None)
    df["reviewed_at"] = pd.to_datetime(df["reviewed_at"], utc=True).dt.tz_convert(TZ)
    df["date"] = df["reviewed_at"].dt.date
    df["week_start"] = (df["reviewed_at"].dt.normalize()
                        - pd.to_timedelta(df["reviewed_at"].dt.weekday, unit="D")).dt.date
    df["sentiment"] = df["score"].map(classify.sentiment)
    df["has_text"] = ~(df["positive_text"].map(classify.is_non_answer)
                       & df["negative_text"].map(classify.is_non_answer))
    # Topics are only assigned to English reviews that actually say something.
    df["taggable"] = df["has_text"] & df["review_lang"].map(classify.is_taggable_language)
    empty = {"complaints": [], "praise": []}
    tags = [classify.classify(p, n, t) if ok else empty for p, n, t, ok in
            zip(df["positive_text"], df["negative_text"], df["title"], df["taggable"])]
    df["complaints"] = [t["complaints"] for t in tags]
    df["praise"] = [t["praise"] for t in tags]
    return df.drop(columns=["raw_json"]).sort_values("reviewed_at", ascending=False)


def week_bounds(today: date) -> tuple[date, date, date]:
    """(start of this week, start of last week, today). Weeks start Monday."""
    this_start = today - timedelta(days=today.weekday())
    return this_start, this_start - timedelta(days=7), today


def in_range(df: pd.DataFrame, start: date, end_inclusive: date) -> pd.DataFrame:
    return df[(df["date"] >= start) & (df["date"] <= end_inclusive)]


def avg(df: pd.DataFrame) -> float | None:
    return round(float(df["score"].mean()), 2) if len(df) else None


def week_comparison(df: pd.DataFrame, today: date) -> dict:
    this_start, last_start, _ = week_bounds(today)
    this_wk = in_range(df, this_start, today)
    last_wk = in_range(df, last_start, this_start - timedelta(days=1))
    a, b = avg(this_wk), avg(last_wk)
    return {
        "this_start": this_start, "last_start": last_start,
        "this_avg": a, "this_n": len(this_wk), "last_avg": b, "last_n": len(last_wk),
        "delta": round(a - b, 2) if a is not None and b is not None else None,
        "low_sample": len(this_wk) < MIN_SAMPLE or len(last_wk) < MIN_SAMPLE,
    }


def property_table(df: pd.DataFrame, today: date, properties: list[str]) -> pd.DataFrame:
    this_start, last_start, _ = week_bounds(today)
    rows = []
    for name in properties:
        p = df[df["property_name"] == name]
        tw = in_range(p, this_start, today)
        lw = in_range(p, last_start, this_start - timedelta(days=1))
        d90 = in_range(p, today - timedelta(days=89), today)
        neg90 = d90[d90["sentiment"] == "Negative"]
        top = top_topics(d90, "complaints", base="all", n=1)
        rows.append({
            "Property": name,
            "This week": avg(tw), "Reviews this week": len(tw),
            "Last week": avg(lw), "Reviews last week": len(lw),
            "Change": (round(avg(tw) - avg(lw), 2) if len(tw) and len(lw) else None),
            "Last 90 days": avg(d90), "Reviews (90d)": len(d90),
            "% negative (90d)": round(100 * len(neg90) / len(d90)) if len(d90) else None,
            "Top complaint (90d)": top[0][0] if top else "-",
        })
    return pd.DataFrame(rows)


def top_topics(df: pd.DataFrame, kind: str = "complaints", base: str = "negative",
               n: int = 8) -> list[tuple[str, int, float]]:
    """[(topic, count, share%)] among reviews we can tag (English, with comments).

    base='negative' -> share of those negative reviews; 'all' -> share of all of them.
    """
    pool = df[df["taggable"]]
    if base == "negative":
        pool = pool[pool["sentiment"] == "Negative"]
    if pool.empty:
        return []
    counts = pool[kind].explode().dropna().value_counts()
    return [(t, int(c), round(100 * c / len(pool))) for t, c in counts.head(n).items()]


def headline_insights(df: pd.DataFrame, start: date, end: date, label: str) -> list[str]:
    """Plain-English sentences for the top of the dashboard."""
    period = in_range(df, start, end)
    out: list[str] = []
    if period.empty:
        return [f"No reviews were published {label}."]
    neg = period[period["sentiment"] == "Negative"]
    out.append(
        f"{len(period)} reviews {label}, average {avg(period):.1f}/10. "
        f"{len(neg)} negative (below {classify.NEGATIVE_MAX:.0f}), "
        f"{(period['sentiment'] == 'Positive').sum()} positive (8 or above)."
    )
    tagged = period[period["taggable"]]
    neg_tagged = tagged[tagged["sentiment"] == "Negative"]
    topics = top_topics(period, "complaints", base="negative", n=2)
    if len(neg_tagged) and topics:
        t, c, share = topics[0]
        out.append(f"{share}% of negative reviews {label} mentioned {t.lower()} issues "
                   f"({c} of {len(neg_tagged)} negative reviews with English comments).")
        if len(topics) > 1:
            t2, c2, s2 = topics[1]
            out.append(f"Next most common: {t2.lower()} ({s2}%, {c2} of {len(neg_tagged)}).")
    all_c = top_topics(period, "complaints", base="all", n=1)
    if all_c:
        t, c, share = all_c[0]
        out.append(f"Across all reviews, the most common thing guests disliked was "
                   f"{t.lower()} ({c} of {len(tagged)} reviews with English comments, {share}%).")
    praise = top_topics(period, "praise", base="all", n=1)
    if praise:
        t, c, share = praise[0]
        out.append(f"Most praised: {t.lower()} ({share}% of reviews).")
    if 0 < len(neg_tagged) < MIN_SAMPLE:
        out.append(f"Note: only {len(neg_tagged)} negative review(s) with English comments {label}, "
                   f"so percentages can swing a lot. Read the reviews before acting.")
    untagged = int((period["has_text"] & ~period["taggable"]).sum())
    no_text = int((~period["has_text"]).sum())
    out.append(f"Coverage: topics come from {len(tagged)} reviews with English comments. "
               f"{untagged} reviews in other languages and {no_text} score-only reviews "
               f"count in the averages but not in the topics.")
    return out


def weekly_trend(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby(["week_start", "property_name"]).agg(
        avg_score=("score", "mean"), reviews=("score", "size")).reset_index()
    g["avg_score"] = g["avg_score"].round(2)
    return g


def weekly_sentiment(df: pd.DataFrame) -> pd.DataFrame:
    return (df.groupby(["week_start", "sentiment"]).size().reset_index(name="reviews"))


def topic_trend(df: pd.DataFrame, kind: str = "complaints") -> pd.DataFrame:
    """Monthly count of reviews mentioning each topic (weekly is too sparse for 4 small hotels)."""
    x = df[["reviewed_at", kind]].explode(kind).dropna()
    if x.empty:
        return pd.DataFrame(columns=["month", "topic", "reviews"])
    x["month"] = x["reviewed_at"].dt.tz_localize(None).dt.to_period("M").dt.to_timestamp()
    return x.groupby(["month", kind]).size().reset_index(name="reviews").rename(columns={kind: "topic"})


def last_runs(conn=None) -> pd.DataFrame:
    conn = conn or db.connect()
    return pd.read_sql_query(
        """SELECT property_slug, MAX(finished_at) AS finished_at, status, method, fetched, inserted,
                  total_on_site, message
           FROM scrape_runs WHERE id IN (SELECT MAX(id) FROM scrape_runs GROUP BY property_slug)
           GROUP BY property_slug""", conn)


def category_scores(conn=None) -> pd.DataFrame:
    conn = conn or db.connect()
    return pd.read_sql_query(
        """SELECT property_slug, category, score FROM category_scores
           WHERE (property_slug, captured_at) IN
                 (SELECT property_slug, MAX(captured_at) FROM category_scores GROUP BY property_slug)""",
        conn)
