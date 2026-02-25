import re

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
