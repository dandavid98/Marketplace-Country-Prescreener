"""Keyword matching that sees through disguised spellings, plus optional typo slack.

Three layers (the New scan page explains the same thing in plain English):

1. Exact -- every keyword word appears in the text (accents ignored). This is
   the tool's original rule, so plain matches behave exactly as before.
2. Disguises -- ALWAYS on for keyword searches and never counted as a typo:
   3->e 0->o 4/@->a 5/$->s 7->t 8->b 1/|/!->i or l, lookalike letters from other
   alphabets (Cyrillic/Greek), fancy/full-width letters, dots/dashes/spaces
   inside a word, invisible zero-width characters, stretched letters (eee).
3. Fuzzy -- only when the user turns it on: up to N changed / missing / extra /
   swapped letters, N from the strictness slider and the word's length.

Safety rails: disguise/fuzzy matches must start at the beginning of a word, a
match that jumps a gap (space/punctuation) must also end at a word end (so
"Ti Deco" never reads as "tide"), a standalone number after a space is never
glued on ("Planter, 5" is not "planters"), and short words get no typo slack.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

SHORT_WORD_MAX = 4            # words this short get disguise matching only, no typo slack
DEFAULT_STRICTNESS = 85
MAX_DISGUISED_SEARCHES = 5

# letter -> the digit sellers swap in. Drives both decoding and search spellings.
LEET_SWAPS = {"e": "3", "o": "0", "a": "4", "i": "1", "s": "5"}

# Stand-in character -> the letters it can mean. The character itself still
# matches too, so a keyword like "409" keeps matching "409".
_STANDINS = {
    **{digit: letter for letter, digit in LEET_SWAPS.items()},
    "1": "il", "@": "a", "$": "s", "7": "t", "8": "b", "9": "g", "6": "bg",
    "2": "z", "|": "il", "!": "i", "€": "e",
}

# Lookalike letters from other alphabets (after lowercasing) -> Latin letters.
_HOMOGLYPHS = {
    # Cyrillic
    "а": "a", "в": "b", "е": "e", "ё": "e", "к": "k", "м": "m", "н": "h", "о": "o",
    "р": "p", "с": "c", "т": "t", "у": "y", "х": "x", "і": "i", "ј": "j", "ѕ": "s",
    "ԁ": "d", "ԛ": "q", "ԝ": "w", "ү": "y",
    # Greek
    "α": "a", "β": "b", "ε": "e", "η": "hn", "ι": "i", "κ": "k", "μ": "mu", "ν": "nv",
    "ο": "o", "ρ": "p", "τ": "t", "υ": "yu", "χ": "x", "ω": "w", "ζ": "z",
}

_KIND_RANK = ("exact", "disguised", "fuzzy")
_PHONETIC_SWAPS = (("ph", "f"), ("f", "ph"), ("ck", "k"), ("z", "s"), ("s", "z"), ("c", "k"), ("k", "c"))


@dataclass(frozen=True)
class KeywordMatch:
    """Why a keyword matched. kind: "exact" | "disguised" | "fuzzy"."""

    kind: str
    details: tuple[str, ...] = ()
    where: str = "title"

    def describe(self) -> str:
        return "" if self.kind == "exact" else f"{self.where} has " + "; ".join(self.details)


def _fold(text: str) -> str:
    """Lowercase and strip accents ("Jabón" -> "jabon").

    NFD, not NFKD: full-width / fancy letters must NOT fold here, so they land
    in the disguise layer and get flagged instead of passing as exact.
    """
    return "".join(c for c in unicodedata.normalize("NFD", text.lower()) if not unicodedata.combining(c))


def legacy_contains(keyword: str, text: str) -> bool:
    """The tool's original rule: every 2+ char [a-z0-9] chunk of the keyword is in the text.

    Always tried first, so nothing that matched before fuzzy matching existed
    can stop matching (e.g. garbled titles like "H√§agen-Dazs").
    """
    text_lower = text.lower()
    return all(w in text_lower for w in re.findall(r"[a-z0-9]+", keyword.lower()) if len(w) > 1)


def keyword_words(keyword: str) -> list[str]:
    """The keyword's significant words: folded, lookalikes mapped, stretches collapsed."""
    text = "".join(_HOMOGLYPHS.get(c, c)[0] for c in unicodedata.normalize("NFKC", _fold(keyword)))
    words = (re.sub(r"(.)\1\1+", r"\1", w) for w in re.findall(r"[^\W_]+", text))
    return [w for w in words if len(w) > 1]


def allowed_edits(word: str, strictness: int | None) -> int:
    """Letter differences allowed for `word`. None = fuzzy off (disguises only)."""
    if strictness is None or len(word) <= SHORT_WORD_MAX:
        return 0
    s = min(max(int(strictness), 0), 100)
    return min(len(word), (len(word) * (100 - s) + 50) // 100)


# Token = (letters it can stand for, first and last index in the original text).
# letters == "" marks a gap (a run of spaces/punctuation).
_Token = tuple[str, int, int]


def _decode(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    for i, ch in enumerate(text):
        for c in unicodedata.normalize("NFKD", ch).lower():
            if unicodedata.category(c) in ("Mn", "Cf"):      # accents, zero-width/invisible chars
                continue
            if c in _STANDINS:
                tokens.append((c + _STANDINS[c], i, i))
            elif c in _HOMOGLYPHS:
                tokens.append((_HOMOGLYPHS[c], i, i))
            elif c.isalnum():
                tokens.append((c, i, i))
            elif tokens and not tokens[-1][0]:
                tokens[-1] = ("", tokens[-1][1], i)          # extend the current gap
            else:
                tokens.append(("", i, i))
    # stretched letters: a run of 3+ of the same letter reads as one ("eee" -> "e")
    out: list[_Token] = []
    i = 0
    while i < len(tokens):
        j = i
        while j < len(tokens) and tokens[j][0] and tokens[j][0] == tokens[i][0]:
            j += 1
        if j - i >= 3:
            out.append((tokens[i][0], tokens[i][1], tokens[j - 1][2]))
            i = j
        else:
            out.append(tokens[i])
            i += 1
    return out


def _number_pieces(tokens: list[_Token], text: str) -> set[int]:
    """Start indices of all-digit pieces sitting after a space ("Planter, 5").

    A disguised word may be split by dots/dashes ("F.3.b.r.3.z.3") but a
    standalone number after whitespace is just a number, never part of a word.
    """
    blocked: set[int] = set()
    for s in range(1, len(tokens)):
        gap = tokens[s - 1]
        if gap[0] or not tokens[s][0] or not any(ch.isspace() for ch in text[gap[1]:gap[2] + 1]):
            continue
        k = s
        while k < len(tokens) and tokens[k][0] and text[tokens[k][1]].isdigit():
            k += 1
        if k == len(tokens) or not tokens[k][0]:
            blocked.add(s)
    return blocked


def _best_hit(word: str, tokens: list[_Token], max_edits: int, blocked: set[int]) -> tuple[int, int, int] | None:
    """Fewest-edit match of `word` starting at a word start: (edits, first, last) or None."""
    n = len(word)
    best: tuple[int, bool, int, int, int] | None = None   # (edits, stops mid-word, |length diff|, first, last)
    for s, (letters, _, _) in enumerate(tokens):
        if not letters or (s and tokens[s - 1][0]):
            continue
        seg: list[tuple[str, int, bool, bool]] = []    # (letters, last idx, crossed a gap, ends a word)
        crossed = False
        k = s
        while k < len(tokens) and len(seg) < n + max_edits:
            if not tokens[k][0]:
                crossed = True
            elif crossed and k in blocked:
                break
            else:
                ends_word = k + 1 == len(tokens) or not tokens[k + 1][0]
                seg.append((tokens[k][0], tokens[k][2], crossed, ends_word))
            k += 1
        m = len(seg)
        d = [[0] * (m + 1) for _ in range(n + 1)]
        d[0] = list(range(m + 1))
        for i in range(1, n + 1):
            d[i][0] = i
            for j in range(1, m + 1):
                cost = 0 if word[i - 1] in seg[j - 1][0] else 1
                d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
                if i > 1 and j > 1 and word[i - 1] in seg[j - 2][0] and word[i - 2] in seg[j - 1][0]:
                    d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)      # swapped neighbours
        for j in range(1, m + 1):
            _, last, crossed_gap, ends_word = seg[j - 1]
            if d[n][j] > max_edits or (crossed_gap and not ends_word):
                continue
            cand = (d[n][j], not ends_word, abs(j - n), tokens[s][1], last)
            if best is None or cand[:3] < best[:3]:
                best = cand
    return (best[0], best[3], best[4]) if best else None


def match_keyword(keyword: str, text: str, strictness: int | None = None) -> KeywordMatch | None:
    """Match every keyword word against `text`. None = no match.

    Disguises are always decoded; `strictness` (0-100) adds typo slack, None = off.
    """
    words = keyword_words(keyword)
    if not words or (text and legacy_contains(keyword, text)):
        return KeywordMatch("exact")
    if not text:
        return None
    folded = _fold(text)
    tokens: list[_Token] | None = None
    blocked: set[int] = set()
    kinds: list[str] = []
    details: list[str] = []
    for word in words:
        if word in folded:
            kinds.append("exact")
            continue
        if tokens is None:
            tokens = _decode(text)
            blocked = _number_pieces(tokens, text)
        hit = _best_hit(word, tokens, allowed_edits(word, strictness), blocked)
        if hit is None:
            return None
        edits, first, last = hit
        found = text[first:last + 1]
        if edits:
            kinds.append("fuzzy")
            details.append(f"{found!r} ≈ {word!r} ({edits} letter{'s' if edits > 1 else ''} different)")
        else:
            kinds.append("disguised")
            details.append(f"{found!r} = disguised {word!r}")
    return KeywordMatch(max(kinds, key=_KIND_RANK.index), tuple(details))


def disguised_spellings(keyword: str, limit: int = MAX_DISGUISED_SEARCHES) -> list[str]:
    """Likely circumvention spellings of a keyword, to run as extra Walmart searches.

    Only the keyword's longest word is disguised, e.g. febreze -> f3breze,
    f3br3z3, feb reze, phebreze, febreeze.
    """
    words = keyword.lower().split()
    if not words:
        return []
    idx = max(range(len(words)), key=lambda i: len(words[i]))
    w = words[idx]
    if len(w) < 3:
        return []
    swappable = [i for i, c in enumerate(w) if i and c in LEET_SWAPS]
    vowels = [i for i, c in enumerate(w) if i and c in "aeiou"]
    candidates: list[str] = []
    if swappable:
        i = swappable[0]
        candidates.append(w[:i] + LEET_SWAPS[w[i]] + w[i + 1:])
        candidates.append(w[0] + "".join(LEET_SWAPS.get(c, c) for c in w[1:]))
    candidates.append(f"{w[:len(w) // 2]} {w[len(w) // 2:]}")
    for old, new in _PHONETIC_SWAPS:
        pos = w.rfind(old)
        if pos != -1:
            candidates.append(w[:pos] + new + w[pos + len(old):])
            break
    if vowels:
        i = vowels[len(vowels) // 2]
        candidates.append(w[:i + 1] + w[i:])
    unique = [c for c in dict.fromkeys(candidates) if c != w][:limit]
    return [" ".join(words[:idx] + [c] + words[idx + 1:]) for c in unique]
