import re


JAPANESE_RE = re.compile(r"[ぁ-ゟァ-ヿ㐀-鿿]")
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def needs_japanese_rewrite(text):
    """Flag English-only output or substantial English prose; URLs are ignored."""
    visible = URL_RE.sub("", (text or "")).strip()
    if len(visible) < 4 or JAPANESE_RE.search(visible):
        for line in visible.splitlines():
            if (
                len(line) >= 60
                and not JAPANESE_RE.search(line)
                and len(re.findall(r"[A-Za-z]{3,}", line)) >= 8
            ):
                return True
        japanese_count = len(JAPANESE_RE.findall(visible))
        latin_letters = len(re.findall(r"[A-Za-z]", visible))
        latin_words = len(re.findall(r"[A-Za-z]{3,}", visible))
        if latin_words >= 15 and latin_letters > japanese_count * 3:
            return True
        return False
    return bool(re.search(r"[A-Za-z]{3,}", visible))


def infer_research_notification_mode(text, model_mode=None):
    """Resolve common Japanese notification nuances deterministically."""
    text = text or ""
    if re.search(r"(詳しく|詳細|全部|全文|省略せず).{0,12}(通知|教え|送)", text):
        return "detailed_result"
    if re.search(r"(終わっ|終わり|完了|済ん).{0,12}(通知|教え|知らせ)", text):
        return "completion_only"
    if re.search(r"(結果|内容|要点).{0,12}(通知|教え|送)", text):
        return "result_summary"
    if "教えて" in text or "通知" in text:
        return "result_summary"
    if model_mode in {"completion_only", "result_summary", "detailed_result"}:
        return model_mode
    return "result_summary"
