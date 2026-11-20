"""Sentiment bands and operational topic tagging.

Method (deliberately simple and explainable to ops staff):

* Sentiment comes from the guest's own score, not from text analysis. Booking scores are 1-10:
      Positive  >= 8.0      Mixed  6.0 - 7.9      Negative  < 6.0
  The guest's number is a more reliable signal than any model guessing tone from short text.

* Topics come from keyword rules applied separately to the two boxes Booking asks guests to fill:
  "What did you like" (positive_text) and "What didn't you like" (negative_text).
  A topic found in the "didn't like" box is a complaint; in the "liked" box it is praise.
  That split does most of the work a sentiment model would otherwise have to guess at.

* Non-answers in the complaint box ("nothing", "n/a", "all good") are ignored, so they
  don't count as complaints.

Known limits are listed in the README (sarcasm, non-English text, misspellings, etc.).
"""
from __future__ import annotations

import re

POSITIVE_MIN = 8.0
NEGATIVE_MAX = 6.0  # strictly below this is negative


def sentiment(score: float) -> str:
    if score >= POSITIVE_MIN:
        return "Positive"
    if score < NEGATIVE_MAX:
        return "Negative"
    return "Mixed"


# Each topic: list of regex fragments, matched on word boundaries, case-insensitive.
TOPICS: dict[str, list[str]] = {
    "Cleanliness": [
        r"clean\w*", r"dirt\w*", r"dust\w*", r"filth\w*", r"stain\w*", r"mou?ld\w*", r"smell\w*",
        r"smelly", r"stink\w*", r"odou?r\w*", r"hair(s)?", r"cockroach\w*", r"roach\w*", r"bugs?",
        r"bed ?bugs?", r"insects?", r"spotless", r"hygien\w*", r"musty", r"grime", r"grubby",
        r"unclean", r"housekeep\w*", r"sheets? (?:were|was) (?:dirty|stained)",
    ],
    "Check-in": [
        r"check[\s-]?in", r"check[\s-]?out", r"arriv\w*", r"key ?(?:card|code|box)?s?",
        r"(?:door|access|entry|pin|room) codes?", r"zoom", r"video call", r"kiosk", r"robo\w*",
        r"self[\s-]?check", r"reception hours", r"late arrival", r"early check", r"instructions?",
        r"wait(?:ed|ing)? (?:for|at)", r"let us in", r"lock(?:ed)? out",
    ],
    "Staff": [
        r"staff", r"reception\w*", r"receptionist\w*", r"manager\w*", r"host\w*", r"owner\w*",
        r"employee\w*", r"rude\w*", r"friendly", r"unfriendly", r"helpful", r"unhelpful",
        r"polite", r"impolite", r"welcom\w*", r"service", r"attitude", r"customer service",
    ],
    "Noise": [
        r"nois\w*", r"loud\w*", r"quiet\w*", r"sound\w*", r"thin walls?", r"traffic", r"party",
        r"parties", r"music", r"pub", r"bar next", r"could hear", r"hear(?:d)? everything",
        r"construction", r"kept (?:me|us) (?:up|awake)", r"woke (?:me|us) up",
    ],
    "Facilities": [
        r"lift", r"elevator", r"stairs?", r"wi-?fi", r"internet", r"kitchen\w*", r"laundry",
        r"parking", r"car ?park", r"air ?con\w*", r"aircon", r"a/?c", r"heat\w*", r"fan",
        r"tv", r"television", r"fridge", r"kettle", r"microwave", r"breakfast", r"pool",
        r"luggage", r"storage", r"common (?:area|room)", r"facilit\w*", r"amenit\w*",
        r"shared bathroom", r"toilets?", r"showers?", r"hot water",
    ],
    "Location": [
        r"location", r"located", r"position\w*", r"close to", r"walking distance", r"distance",
        r"station", r"train", r"bus(?:es)?", r"bus stop", r"transport", r"cbd",
        r"neighbou?rhood", r"restaurants?", r"cafes?", r"shops?", r"stadium", r"beach",
        r"central\w*", r"convenien\w*",
    ],
    "Room condition": [
        r"beds?", r"mattress\w*", r"pillows?", r"sheets?", r"linen", r"towels?",
        r"bathroom\w*", r"window\w*", r"curtains?", r"furniture", r"broken", r"old",
        r"dated", r"tired", r"run[\s-]?down", r"small", r"tiny", r"cramped", r"spacious",
        r"comfy", r"comfortable", r"uncomfortable", r"renovat\w*", r"maintenance", r"leak\w*",
        r"damp", r"carpet\w*", r"paint",
    ],
    "Value for money": [
        r"value", r"price\w*", r"pric(?:ey|y)", r"expensive", r"cheap\w*", r"overpriced",
        r"worth", r"money", r"cost\w*", r"afford\w*", r"budget", r"paid", r"pay",
        r"charg\w*", r"fees?", r"refund\w*", r"deposit",
    ],
}

_COMPILED = {
    topic: re.compile(r"\b(?:" + "|".join(parts) + r")\b", re.IGNORECASE)
    for topic, parts in TOPICS.items()
}

_NON_ANSWER = re.compile(
    r"^\s*(?:nothing|none|nil|n/?a|na|no|nope|-+|\.+|all good|everything(?: was)? (?:good|great|fine|perfect)"
    r"|nothing (?:really|at all|to complain about|to report|much)?|no complaints?|not applicable)"
    r"[\s.!]*$",
    re.IGNORECASE,
)


def is_non_answer(text: str | None) -> bool:
    if not isinstance(text, str):
        return True
    return len(text.strip()) < 2 or bool(_NON_ANSWER.match(text))


def topics_in(text: str | None) -> list[str]:
    if is_non_answer(text):
        return []
    return [t for t, rx in _COMPILED.items() if rx.search(text)]


def is_taggable_language(lang: str | None) -> bool:
    """Keyword rules are English-only. Other languages are left untagged rather than mis-tagged."""
    return isinstance(lang, str) and lang.lower().startswith("en")


def classify(positive_text: str | None, negative_text: str | None, title: str | None = None) -> dict:
    """Return {'complaints': [...topics], 'praise': [...topics]}.

    The review title is used only when both boxes are empty (some guests write only a title),
    and then only as a complaint source if the score says the stay was poor (handled by caller).
    """
    complaints = topics_in(negative_text)
    praise = topics_in(positive_text)
    return {"complaints": complaints, "praise": praise}


def matched_terms(text: str | None, topic: str) -> list[str]:
    """Which words triggered a topic. Shown in the UI so staff can see why a review was tagged."""
    if not text:
        return []
    return sorted({m.group(0).lower() for m in _COMPILED[topic].finditer(text)})
