import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher


TITLE_PREFIX_RE = re.compile(
    r"^\s*[\[［]\s*(?:AI|ＡＩ)\s*[\]］]\s*",
    re.IGNORECASE,
)
EDGE_PUNCTUATION = r"[\s、。,:：;；・\-‐―ー「」『』（）()]*"
AI_MEMO_MARKER = r"(?:AI|ＡＩ)\s*メモ"
TRANSPORT_INSTRUCTION = (
    r"(?:(?:Google|グーグル)\s*)?"
    r"(?:Tasks?|タスクス?|タスク)"
    r"(?:の?(?:通常)?リスト)?"
    r"(?:に|へ)?"
    r"(?:(?:メモ|タスク)(?:として)?)?"
    r"(?:保存|追加|登録)"
    r"(?:して|しといて|しておいて|してください|お願い(?:します)?)?"
)

START_MARKER_RE = re.compile(
    rf"^{EDGE_PUNCTUATION}{AI_MEMO_MARKER}{EDGE_PUNCTUATION}",
    re.IGNORECASE,
)
END_MARKER_RE = re.compile(
    rf"{EDGE_PUNCTUATION}{AI_MEMO_MARKER}{EDGE_PUNCTUATION}"
    rf"(?:{TRANSPORT_INSTRUCTION})?"
    rf"{EDGE_PUNCTUATION}$",
    re.IGNORECASE,
)
TRANSPORT_SUFFIX_RE = re.compile(
    rf"{EDGE_PUNCTUATION}{TRANSPORT_INSTRUCTION}{EDGE_PUNCTUATION}$",
    re.IGNORECASE,
)

FUZZY_MARKER_VARIANTS = (
    "aiメモ",
    "aiめも",
    "aiメム",
    "aiめむ",
    "aiメマ",
    "aiめま",
    "あいメモ",
    "あいめも",
    "エーアイメモ",
    "えーあいめも",
    "エイアイメモ",
    "えいあいめも",
    "アイメモ",
    "ai memo",
)
FUZZY_MARKER_THRESHOLD = 0.72
EDGE_NOISE_RE = re.compile(r"^[\s、。,:：;；・\-‐―ー「」『』（）()]+")
TRANSPORT_CUES = (
    "task",
    "たすく",
    "タスク",
    "保存",
    "ほぞん",
    "追加",
    "ついか",
    "登録",
    "とうろく",
    "なんとか",
)
FUZZY_TRANSPORT_SUFFIX_RE = re.compile(
    r"[\s、。,:：;；・\-‐―ー]*"
    r"(?:(?:google|ぐーぐる|グーグル)\s*)?"
    r"(?:tasks?|たすくす?|タスクス?)"
    r".{0,18}?"
    r"(?:保存|ほぞん|追加|ついか|登録|とうろく|なんとか)"
    r".{0,10}$",
    re.IGNORECASE,
)


def _marker_key(value):
    normalized = unicodedata.normalize("NFKC", value).lower()
    normalized = normalized.translate(str.maketrans({
        "エ": "え",
        "ア": "あ",
        "イ": "い",
        "メ": "め",
        "モ": "も",
        "ム": "む",
        "マ": "ま",
    }))
    return re.sub(r"[\s、。,:：;；・\-‐―ー「」『』（）()]+", "", normalized)


FUZZY_MARKER_KEYS = tuple(_marker_key(value) for value in FUZZY_MARKER_VARIANTS)


def _marker_similarity(value):
    key = _marker_key(value)
    if not key:
        return 0.0
    return max(SequenceMatcher(None, key, candidate).ratio() for candidate in FUZZY_MARKER_KEYS)


def _find_fuzzy_start(text):
    leading = EDGE_NOISE_RE.match(text)
    offset = leading.end() if leading else 0
    best = None
    for length in range(3, min(10, len(text) - offset) + 1):
        score = _marker_similarity(text[offset:offset + length])
        if score >= FUZZY_MARKER_THRESHOLD and (
            best is None or score > best[0]
        ):
            best = (score, offset + length)
    if best is None:
        return None
    end = best[1]
    trailing_noise = EDGE_NOISE_RE.match(text[end:])
    if trailing_noise:
        end += trailing_noise.end()
    return end


def _looks_like_transport_tail(value):
    key = _marker_key(value)
    if not key:
        return True
    if len(key) > 32:
        return False
    return any(cue.lower() in key for cue in TRANSPORT_CUES)


def _find_fuzzy_end(text):
    search_start = max(0, len(text) - 48)
    best = None
    for start in range(search_start, len(text)):
        for length in range(3, min(10, len(text) - start) + 1):
            end = start + length
            if not _looks_like_transport_tail(text[end:]):
                continue
            score = _marker_similarity(text[start:end])
            if score >= FUZZY_MARKER_THRESHOLD and (
                best is None or score > best[0] or (
                    score == best[0] and start > best[1]
                )
            ):
                best = (score, start)
    return None if best is None else best[1]


@dataclass(frozen=True)
class AIMemo:
    explicit: bool
    confidence: str
    title_marker: bool
    start_marker: bool
    end_marker: bool
    clean_title: str
    clean_body: str

    @property
    def text_for_ai(self):
        if self.clean_body:
            return self.clean_body
        return self.clean_title


def parse_ai_memo(title, body):
    """Detect explicit AI memo markers and remove transport-only noise."""
    title = title or ""
    body = body or ""
    title_marker = TITLE_PREFIX_RE.search(title) is not None

    stripped_body = body.strip()
    start_marker = START_MARKER_RE.search(stripped_body) is not None
    end_marker = END_MARKER_RE.search(stripped_body) is not None

    fuzzy_start_end = None if start_marker else _find_fuzzy_start(stripped_body)
    fuzzy_end_start = None if end_marker else _find_fuzzy_end(stripped_body)
    accept_fuzzy = title_marker or (
        fuzzy_start_end is not None and fuzzy_end_start is not None
    )
    fuzzy_start = accept_fuzzy and fuzzy_start_end is not None
    fuzzy_end = accept_fuzzy and fuzzy_end_start is not None

    clean_title = TITLE_PREFIX_RE.sub("", title, count=1).strip()
    clean_body = stripped_body
    if start_marker:
        clean_body = START_MARKER_RE.sub("", clean_body, count=1).strip()
    elif fuzzy_start:
        clean_body = clean_body[fuzzy_start_end:].strip()
    if end_marker:
        clean_body = END_MARKER_RE.sub("", clean_body, count=1).strip()
    elif fuzzy_end:
        # Recalculate after removing a leading marker because indices changed.
        recalculated_end = _find_fuzzy_end(clean_body)
        if recalculated_end is not None:
            clean_body = clean_body[:recalculated_end].rstrip(" \t\r\n、。,:：;；・-‐―ー")

    explicit = title_marker or start_marker or end_marker or fuzzy_start or fuzzy_end

    # If Gemini kept a trailing transport command but dropped the closing
    # marker, strip it only when another explicit marker proves this is AI input.
    if explicit and not end_marker:
        clean_body = TRANSPORT_SUFFIX_RE.sub("", clean_body, count=1).strip()
        clean_body = FUZZY_TRANSPORT_SUFFIX_RE.sub("", clean_body, count=1).strip()

    any_start = start_marker or fuzzy_start
    any_end = end_marker or fuzzy_end
    strong = (title_marker and (any_start or any_end)) or (
        any_start and any_end
    )
    confidence = "strong" if strong else ("explicit" if explicit else "none")

    return AIMemo(
        explicit=explicit,
        confidence=confidence,
        title_marker=title_marker,
        start_marker=start_marker,
        end_marker=end_marker,
        clean_title=clean_title,
        clean_body=clean_body,
    )


def trash_processed_ai_memo(keep, note):
    """Move an already-processed explicit AI memo to Keep's trash."""
    note.trash()
    keep.sync()


def is_ready_to_trash(result):
    """Return true unless trashing would hide work that cannot run.

    needs_confirmation deliberately does not block here. Holding the note back
    would turn a confident guess into a chore for the user, and every ambiguous
    classification is already rerouted to a runnable intent before this point.
    """
    if result.get("intent") in (None, "", "unknown"):
        return False
    if result.get("intent") == "reminder":
        scheduled_at = result.get("scheduled_at")
        if not scheduled_at:
            return False
        try:
            parsed = datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            return False
        if parsed.tzinfo is None:
            return False
    if result.get("intent") == "calendar" and not result.get("calendar_ready"):
        return False
    if result.get("intent") == "persistent_reminder":
        action = result.get("persistent_reminder_action")
        if action not in ("add", "complete", "notify_now"):
            return False
        if action == "add" and not result.get("persistent_task_text"):
            return False
        if action == "complete" and not (
            result.get("persistent_target_id") is not None
            or result.get("persistent_task_text")
        ):
            return False
    return True
