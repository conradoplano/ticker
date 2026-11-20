"""The topic cards offered in the setup wizard, grouped by area. People can always add their own."""

AREAS = [
    ("Politics", ["Geopolitics", "German politics", "European politics", "US politics", "Elections",
                  "Security and defence"]),
    ("Economy and markets", ["Markets and central banks", "Companies and earnings", "Clearing and derivatives",
                             "Energy and commodities", "Personal finance", "Startups and venture capital"]),
    ("Tech and AI", ["AI", "Software and the web", "Cybersecurity", "Gadgets", "Tech policy"]),
    ("Science", ["Climate and environment", "Health and medicine", "Space", "Research"]),
    ("Sport", ["Football", "FIFA and football business", "Tennis", "Formula 1", "Winter sports"]),
    ("Culture", ["Books", "Film and TV", "Music", "Art and design", "Food"]),
    ("Regional", ["Switzerland", "Germany", "Hessen and Frankfurt", "Spain", "Argentina"]),
]

ALL = [name for _, names in AREAS for name in names]


def match_description(text):
    """Catalogue topics whose words appear in a free-text description (used when AI is not available)."""
    text = (text or "").lower()
    found = []
    for name in ALL:
        words = [w for w in name.lower().replace(" and ", " ").split() if len(w) > 2]
        if any(word in text for word in words):
            found.append(name)
    return found


def keywords(topic_name):
    """Words to look for in an article when ranking without AI."""
    return [w for w in topic_name.lower().replace(" and ", " ").replace(",", " ").split() if len(w) > 1]
