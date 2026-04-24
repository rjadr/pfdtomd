import re
from typing import List, Sequence

def is_numbered_list(text: str) -> bool:
    return bool(re.match(r"^[\s]*[\d]+[.][\s]", text))

def is_bullet_list(text: str) -> bool:
    # Port of isListItemCharacter: handles -, •, –
    return bool(re.match(r"^[\s]*[-•–][\s]", text))

def word_match_score(str1: str, str2: str) -> float:
    w1 = set(normalize_for_match(str1).split())
    w2 = set(normalize_for_match(str2).split())
    if not w1 or not w2: return 0.0
    return len(w1.intersection(w2)) / max(len(w1), len(w2))

def normalize_for_match(text: str) -> str:
    """Deep Port of normalizedCharCodeArray + isDigit logic."""
    # Replace non-breaking spaces (160) with standard spaces
    text = text.replace('\xa0', ' ')
    # Uppercase and strip everything except alphanumeric
    return re.sub(r'[^A-Z0-9]', '', text.upper())

# ─── v0.6 additions ──────────────────────────────────────────────────────────

def token_overlap(a: str, b: str) -> float:
    """Token-level Jaccard overlap (case-insensitive, punctuation stripped)."""
    def _tokens(s: str) -> set:
        return set(re.sub(r'[^\w\s]', '', s.lower()).split())
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta), len(tb))

def is_numeric_sequence(items: Sequence[str]) -> bool:
    """Return True if items form a consecutive integer sequence (1,2,3 or 2,3,4)."""
    nums = []
    for s in items:
        m = re.match(r'^(\d+)', s.strip())
        if m:
            nums.append(int(m.group(1)))
        else:
            return False
    if len(nums) < 2:
        return False
    return all(nums[i] == nums[i - 1] + 1 for i in range(1, len(nums)))

def fuzzy_contains(needle: str, haystack: str, min_ratio: float = 0.8) -> bool:
    """Return True if needle's tokens are substantially covered by haystack."""
    return token_overlap(needle, haystack) >= min_ratio

def classify_token(text: str) -> str:
    """Classify a text token into a broad category string.

    Returns one of: 'numeric', 'alpha', 'mixed', 'punct', 'empty'.
    Useful for distinguishing table cells from prose.
    """
    stripped = text.strip()
    if not stripped:
        return 'empty'
    if re.match(r'^[\d.,\-+%/]+$', stripped):
        return 'numeric'
    if re.match(r'^[A-Za-z]+$', stripped):
        return 'alpha'
    if re.match(r'^[\W]+$', stripped):
        return 'punct'
    return 'mixed'
