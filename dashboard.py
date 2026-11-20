"""Azzurro Hotels guest review dashboard.  Run:  streamlit run dashboard.py"""
from __future__ import annotations

import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

from app import classify, db, insights

ROOT = Path(__file__).resolve().parent
st.set_page_config(page_title="Azzurro Review Insights", page_icon="🏨", layout="wide")

SENT_COLOURS = {"Positive": "#2e7d32", "Mixed": "#f9a825", "Negative": "#c62828"}


@st.cache_data(ttl=60)
def load():
    conn = db.connect()
    return insights.load_reviews(conn), insights.last_runs(conn), insights.category_scores(conn)


collecting = db.collector_running()
if st.session_state.get("refresh_started") and not collecting:
    # The refresh this user started has finished: drop cached data so the page shows it.
    st.session_state["refresh_started"] = False
    st.cache_data.clear()
    st.toast("Reviews updated from Booking.com", icon="✅")

df_all, runs, cats = load()

st.title("Guest Review Insights")
st.caption("Booking.com reviews for the four Azzurro Hotels properties. "
           "Scores are out of 10. Weeks run Monday to Sunday, Sydney time.")

if df_all.empty:
    st.warning("No reviews collected yet. Click **Refresh reviews** in the sidebar, "
               "or run `python -m app.collector` in a terminal.")
    st.stop()

properties = sorted(df_all["property_name"].unique())
today = pd.Timestamp.now(tz=insights.TZ).date()

# ------------------------------------------------------------------ sidebar filters
with st.sidebar:
    st.header("Filters")
    chosen_props = st.multiselect("Properties", properties, default=properties)
    preset = st.radio("Date range", ["Last 4 weeks", "Last 3 months", "Last 12 months",
                                     "All time", "Custom"], index=1)
    min_d = df_all["date"].min()
    if preset == "Custom":
        rng = st.date_input("From / to", (today - timedelta(days=30), today),
                            min_value=min_d, max_value=today)
        start, end = (rng if isinstance(rng, tuple) and len(rng) == 2 else (rng[0], rng[0]))
    else:
        days = {"Last 4 weeks": 28, "Last 3 months": 91, "Last 12 months": 365}.get(preset)
        start, end = (today - timedelta(days=days - 1) if days else min_d), today

    st.divider()
    st.subheader("Data freshness")
    def fmt(ts):
        ts = pd.to_datetime(ts, errors="coerce", utc=True)
        return "never" if pd.isna(ts) else ts.tz_convert(insights.TZ).strftime("%d %b %H:%M")

    name_by_slug = dict(zip(df_all["property_slug"], df_all["property_name"]))
    for _, r in runs.iterrows():
        icon = {"ok": "🟢", "partial": "🟠", "failed": "🔴"}.get(r["last_status"], "⚪")
        st.caption(f"{icon} {name_by_slug.get(r['property_slug'], r['property_slug'])}: "
                   f"updated {fmt(r['last_ok_at'])}")
        if r["last_status"] in ("partial", "failed") and r["last_message"]:
            st.caption(f"↳ last attempt: {r['last_message'][:120]}")
    if collecting:
        st.info("⏳ Checking Booking.com for new reviews. This usually takes under a minute; "
                "you can keep using the dashboard meanwhile.")
        st.button("Check if finished")  # any click reruns the page, which picks up the result
    elif st.button("🔄 Refresh reviews", help="Check Booking.com for new reviews (runs in the background)"):
        # Run in the background so the page stays usable. The collector's own lock stops
        # a second run starting if someone clicks twice.
        subprocess.Popen([sys.executable, "-m", "app.collector"], cwd=ROOT,
                         stdout=open(ROOT / "data" / "collector.log", "a"), stderr=subprocess.STDOUT,
                         start_new_session=True)
        st.session_state["refresh_started"] = True
        time.sleep(1.5)  # give the collector a moment to take its lock before we redraw
        st.rerun()

if not chosen_props:
    st.info("Pick at least one property in the sidebar.")
    st.stop()

df_props = df_all[df_all["property_name"].isin(chosen_props)]
df = insights.in_range(df_props, start, end)

# ------------------------------------------------------------------ this week vs last week
wk = insights.week_comparison(df_props, today)
c1, c2, c3, c4 = st.columns(4)
c1.metric(f"Avg rating this week (from {wk['this_start']:%d %b})",
          f"{wk['this_avg']:.1f}" if wk["this_avg"] is not None else "No reviews",
          f"{wk['delta']:+.1f} vs last week" if wk["delta"] is not None else None)
c2.metric("Avg rating last week", f"{wk['last_avg']:.1f}" if wk["last_avg"] is not None else "No reviews")
c3.metric("Reviews this week / last week", f"{wk['this_n']} / {wk['last_n']}")
c4.metric(f"Avg rating, selected period", f"{insights.avg(df):.1f}" if len(df) else "-",
          help=f"{start:%d %b %Y} to {end:%d %b %Y}, {len(df)} reviews")
if wk["low_sample"]:
    st.caption("⚠️ Fewer than 3 reviews in one of the weeks. A single review can move the "
               "weekly average a lot, so compare with the 90-day figure below.")

# ------------------------------------------------------------------ headline insights
st.subheader("What guests are saying")
tab_week, tab_period = st.tabs(["This week", "Selected period"])
with tab_week:
    for s in insights.headline_insights(df_props, wk["this_start"], today, "this week"):
        st.markdown(f"- {s}")
with tab_period:
    for s in insights.headline_insights(df_props, start, end, "in the selected period"):
        st.markdown(f"- {s}")

# ------------------------------------------------------------------ property breakdown
st.subheader("By property")
table = insights.property_table(df_all, today, chosen_props)
st.dataframe(
    table, hide_index=True, width="stretch",
    column_config={
        "This week": st.column_config.NumberColumn(format="%.1f"),
        "Last week": st.column_config.NumberColumn(format="%.1f"),
        "Change": st.column_config.NumberColumn(format="%+.1f"),
        "Last 90 days": st.column_config.NumberColumn(format="%.1f"),
        "% negative (90d)": st.column_config.NumberColumn(format="%d%%"),
    },
)
if not cats.empty:
    with st.expander("Booking.com's own category scores (all-time, as shown on Booking)"):
        name_by_slug = dict(zip(df_all["property_slug"], df_all["property_name"]))
        piv = cats.assign(Property=cats["property_slug"].map(name_by_slug)) \
                  .pivot_table(index="Property", columns="category", values="score").round(1)
        st.dataframe(piv.loc[[p for p in chosen_props if p in piv.index]], width="stretch")

# ------------------------------------------------------------------ trends
st.subheader("Trends")
t1, t2 = st.columns(2)
with t1:
    wt = insights.weekly_trend(df)
    if len(wt):
        fig = px.line(wt, x="week_start", y="avg_score", color="property_name", markers=True,
                      hover_data={"reviews": True},
                      labels={"week_start": "Week starting", "avg_score": "Avg rating",
                              "property_name": "Property"},
                      title="Average rating by week")
        fig.update_yaxes(range=[0, 10.5])
        st.plotly_chart(fig, width="stretch")
with t2:
    ws = insights.weekly_sentiment(df)
    if len(ws):
        fig = px.bar(ws, x="week_start", y="reviews", color="sentiment",
                     color_discrete_map=SENT_COLOURS,
                     category_orders={"sentiment": ["Positive", "Mixed", "Negative"]},
                     labels={"week_start": "Week starting", "reviews": "Reviews", "sentiment": ""},
                     title="Positive vs negative reviews by week")
        st.plotly_chart(fig, width="stretch")
st.caption("Positive = score 8 or above. Mixed = 6 to 7.9. Negative = below 6.")

# ------------------------------------------------------------------ topics
st.subheader("Recurring topics")
k1, k2 = st.columns(2)
with k1:
    neg_topics = insights.top_topics(df, "complaints", base="negative", n=8)
    n_neg = int(((df["sentiment"] == "Negative") & df["taggable"]).sum())
    if neg_topics:
        tdf = pd.DataFrame(neg_topics, columns=["Topic", "Reviews", "Share %"])
        fig = px.bar(tdf, x="Share %", y="Topic", orientation="h", text="Share %",
                     title=f"What negative reviews complain about ({n_neg} with English comments)",
                     color_discrete_sequence=["#c62828"])
        fig.update_layout(yaxis={"categoryorder": "total ascending"})
        st.plotly_chart(fig, width="stretch")
    else:
        st.info("No negative reviews in this period.")
with k2:
    pos_topics = insights.top_topics(df, "praise", base="all", n=8)
    if pos_topics:
        tdf = pd.DataFrame(pos_topics, columns=["Topic", "Reviews", "Share %"])
        fig = px.bar(tdf, x="Share %", y="Topic", orientation="h", text="Share %",
                     title=f"What guests praise ({int(df['taggable'].sum())} reviews with English comments)",
                     color_discrete_sequence=["#2e7d32"])
        fig.update_layout(yaxis={"categoryorder": "total ascending"})
        st.plotly_chart(fig, width="stretch")
tt = insights.topic_trend(df, "complaints")
if len(tt):
    # A heatmap stays readable with 8 topics over many months; 8 overlapping lines did not.
    grid = tt.pivot_table(index="topic", columns="month", values="reviews", fill_value=0)
    grid = grid.loc[grid.sum(axis=1).sort_values(ascending=False).index]  # biggest problem on top
    grid.columns = [m.strftime("%b %Y") for m in grid.columns]  # plain labels, one per column
    fig = px.imshow(grid, aspect="auto", color_continuous_scale="Reds", text_auto=len(grid.columns) <= 18,
                    labels={"x": "Month", "y": "", "color": "Reviews"},
                    title="Complaints by topic per month (darker = more reviews mentioning it)")
    fig.update_xaxes(type="category")
    fig.update_layout(height=120 + 32 * len(grid))
    st.plotly_chart(fig, width="stretch")
st.caption("Topics are found by keywords in the guest's 'liked' and 'disliked' boxes. A topic in the "
           "'disliked' box counts as a complaint even when the overall score is high. "
           "See the README for how this works and where it can be wrong.")

# ------------------------------------------------------------------ review feed
st.subheader("Reviews")
f1, f2, f3, f4 = st.columns([1, 1, 1, 2])
sent_f = f1.multiselect("Sentiment", ["Positive", "Mixed", "Negative"],
                        default=["Positive", "Mixed", "Negative"])
topic_f = f2.selectbox("Complaint topic", ["Any"] + list(classify.TOPICS))
sort_f = f3.selectbox("Sort", ["Newest first", "Lowest score first", "Highest score first"])
search = f4.text_input("Search text", placeholder="e.g. shower, aircon, receptionist")

feed = df[df["sentiment"].isin(sent_f)]
if topic_f != "Any":
    feed = feed[feed["complaints"].apply(lambda t: topic_f in t)]
if search:
    s = search.lower()
    blob = (feed["title"].fillna("") + " " + feed["positive_text"].fillna("") + " "
            + feed["negative_text"].fillna("")).str.lower()
    feed = feed[blob.str.contains(s, regex=False)]
sort_col, sort_asc = {"Newest first": ("reviewed_at", False),
                      "Lowest score first": ("score", True),
                      "Highest score first": ("score", False)}[sort_f]
feed = feed.sort_values(sort_col, ascending=sort_asc)

st.caption(f"{insights.plural(len(feed), 'review')} match{'es' if len(feed) == 1 else ''}. "
           f"Showing up to 50.")
csv = feed.drop(columns=["complaints", "praise"]).to_csv(index=False).encode()
st.download_button("Download these reviews (CSV)", csv, "reviews.csv", "text/csv")

for _, r in feed.head(50).iterrows():
    colour = SENT_COLOURS[r["sentiment"]]
    with st.container(border=True):
        h1, h2 = st.columns([5, 1])
        h1.markdown(f"**{r['title'] or '(no title)'}**  \n"
                    f"<span style='color:grey'>{r['property_name']} · {r['reviewed_at']:%d %b %Y} · "
                    f"{r['traveller_type'] or ''} · {r['guest_country'] or ''} · "
                    f"{r['room_type'] or ''}</span>", unsafe_allow_html=True)
        h2.markdown(f"<div style='text-align:right;font-size:1.6em;font-weight:700;color:{colour}'>"
                    f"{r['score']:g}</div>", unsafe_allow_html=True)
        if r["positive_text"] and not classify.is_non_answer(r["positive_text"]):
            st.markdown(f"👍 {r['positive_text']}")
        if r["negative_text"] and not classify.is_non_answer(r["negative_text"]):
            st.markdown(f"👎 {r['negative_text']}")
        tags = [f"🔴 {t}" for t in r["complaints"]] + [f"🟢 {t}" for t in r["praise"]]
        if tags:
            st.caption("  ".join(tags))
        elif r["has_text"] and not r["taggable"]:
            st.caption(f"Not topic-tagged: written in '{r['review_lang']}'. Counts in averages only.")
        if r["hotel_reply"]:
            st.caption(f"Hotel replied: {r['hotel_reply'][:300]}")
