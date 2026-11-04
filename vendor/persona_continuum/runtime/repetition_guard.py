"""Name the words a persona keeps reusing across its own recent replies.

Habitual phrases belong to a voice, but a model that reads its own last few
replies copies them until every reply opens or ends the same way.  This looks
only at the persona's own replies, so it never flags a word the user repeats.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence

WINDOW = 5
_MIN_EDGE_HITS = 3
_MAX_NAMED = 3
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_EDGE_CHARS = re.compile(r"[\w\u4e00-\u9fff]+")
# Function words every reply may share; repeating them is not a tic.
_COMMON = frozenset(
    {
        "我们", "你们", "他们", "什么", "没有", "一个", "这个", "那个", "就是", "可以",
        "知道", "觉得", "不是", "还是", "自己", "现在", "时候", "怎么", "因为", "所以",
        "但是", "如果", "已经", "然后", "一下", "这样", "那样", "真的", "还有", "一样",
        "不会", "不要", "我的", "你的",
    }
)


def _edges(text: str) -> tuple[str, str]:
    words = "".join(_EDGE_CHARS.findall(text))
    return words[:2], words[-2:]


def _phrases(text: str) -> set[str]:
    grams: set[str] = set()
    for run in _CJK_RUN.findall(text):
        for size in (2, 3, 4):
            grams.update(run[index : index + size] for index in range(len(run) - size + 1))
    return grams - _COMMON


def repetition_notice(replies: Sequence[str]) -> str:
    """Return a one-line nudge, or ``""`` when the recent replies vary enough."""

    recent = [text.strip() for text in replies[-WINDOW:] if text and text.strip()]
    if len(recent) < _MIN_EDGE_HITS:
        return ""
    named: list[str] = []
    for position in (0, 1):
        edge, hits = Counter(_edges(text)[position] for text in recent).most_common(1)[0]
        if len(edge) == 2 and edge not in _COMMON and hits >= _MIN_EDGE_HITS:
            named.append(edge)
    counts = Counter(gram for text in recent for gram in _phrases(text))
    # A phrase anywhere in a reply is a looser signal than an edge, so it must
    # recur in all but one reply (3 of 3, 3 of 4, 4 of 5).
    min_phrase_hits = max(_MIN_EDGE_HITS, len(recent) - 1)
    frequent = [gram for gram, hits in counts.items() if hits >= min_phrase_hits]
    # Keep the longest form: "在忙啥" rather than also "在忙" and "忙啥".
    for gram in sorted(frequent, key=len, reverse=True):
        if not any(gram in kept for kept in named):
            named.append(gram)
    named = [item for item in named if not any(item != o and item in o for o in named)]
    if not named:
        return ""
    quoted = "、".join(f"「{item}」" for item in named[:_MAX_NAMED])
    return (
        f"Your last {len(recent)} replies kept reusing {quoted}. That is a habit, not a "
        "signature: this time say it another way or leave it out, unless the moment "
        "truly calls for it."
    )


__all__ = ["repetition_notice"]
