"""Data for verification-code extraction gates: anchor phrases + stopwords.
Data, not logic — extend these lists without touching parser code.
"""
from __future__ import annotations

# Anchor phrases (gate 2): the verification code token must immediately follow
# one of these phrases in the email. Matched case-insensitively. Order matters
# only in that the FIRST anchor that appears in the email wins.
DEFAULT_ANCHORS: list[str] = [
    "copy and paste this code",
    "security code",
    "verification code",
    "your code is",
]

# Stopwords (gate 2): a candidate code token equal (case-insensitively) to one
# of these is REJECTED — it is an ordinary English/email word, not a code.
# This guards the widened alphanumeric code regex from grabbing words like
# "please", "application", "ready", "field", "thank", "hello".
STOPWORDS: frozenset[str] = frozenset({
    "a", "about", "above", "account", "act", "address", "after", "again",
    "all", "also", "always", "an", "and", "any", "application", "applications",
    "are", "as", "ashby", "ask", "at", "away", "back", "be",
    "because", "been", "below", "best", "big", "black", "book", "both",
    "build", "but", "by", "came", "can", "candidate", "careers", "change",
    "click", "code", "come", "complete", "completed", "confirm", "confirmed", "continue",
    "copy", "could", "country", "day", "dear", "did", "do", "door",
    "each", "early", "email", "enough", "enter", "even", "every", "expire",
    "expires", "face", "family", "fast", "few", "field", "first", "five",
    "follow", "for", "found", "friend", "from", "get", "give", "go",
    "good", "great", "greenhouse", "had", "half", "has", "have", "he",
    "hello", "her", "here", "hi", "high", "him", "his", "hour",
    "house", "how", "idea", "if", "in", "information", "into", "is",
    "it", "its", "just", "keep", "kind", "know", "last", "learn",
    "less", "let", "lever", "life", "light", "like", "list", "look",
    "make", "me", "mean", "men", "minutes", "money", "morning", "most",
    "mother", "much", "my", "name", "near", "need", "never", "new",
    "next", "no", "not", "now", "number", "of", "off", "old",
    "on", "once", "one", "only", "open", "or", "order", "other",
    "our", "out", "over", "page", "part", "password", "paste", "people",
    "person", "picture", "please", "point", "position", "provided", "ready", "receive",
    "received", "regards", "said", "say", "security", "see", "self", "she",
    "short", "should", "show", "sign", "simple", "since", "sincerely", "six",
    "so", "some", "state", "still", "submit", "submitted", "submitting", "such",
    "sure", "take", "talk", "team", "tell", "ten", "than", "thank",
    "thanks", "that", "the", "their", "them", "then", "there", "these",
    "they", "think", "this", "though", "three", "time", "to", "together",
    "told", "too", "top", "true", "try", "two", "until", "up",
    "us", "use", "using", "verification", "verified", "verify", "want", "was",
    "way", "we", "well", "went", "were", "west", "what", "when",
    "where", "which", "who", "whole", "why", "will", "with", "work",
    "world", "would", "year", "you", "young", "your",
})
