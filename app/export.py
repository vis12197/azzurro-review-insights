"""Export stored reviews to data/sample_reviews.csv (no raw JSON, no personal data)."""
from . import db, insights

if __name__ == "__main__":
    df = insights.load_reviews(db.connect())
    df["complaints"] = df["complaints"].map("; ".join)
    df["praise"] = df["praise"].map("; ".join)
    out = db.ROOT / "data" / "sample_reviews.csv"
    df.to_csv(out, index=False)
    print(f"wrote {len(df)} reviews to {out}")
