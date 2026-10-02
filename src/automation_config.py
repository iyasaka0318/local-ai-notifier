import os


VAGUE_TIMES = {
    "morning": os.environ.get("AI_TIME_MORNING", "08:00"),
    "afternoon": os.environ.get("AI_TIME_AFTERNOON", "15:00"),
    "evening": os.environ.get("AI_TIME_EVENING", "18:00"),
    "night": os.environ.get("AI_TIME_NIGHT", "20:00"),
}

# Sized for a 32K-token context: Japanese runs about 0.72 tokens per character
# (measured), so six pages of 4,500 characters plus the result list stay near
# 23K tokens and leave room for the answer. The server refuses, rather than
# truncates, a prompt that does not fit.
MAX_RESEARCH_QUERIES = 4
MAX_RESEARCH_RESULTS = 16
MAX_RESEARCH_PAGES = 4
MAX_FOLLOW_UP_QUERIES = 2
MAX_FOLLOW_UP_PAGES = 2
MAX_PAGE_BYTES = 1_000_000
MAX_PAGE_TEXT_CHARS = 4_500

CALENDAR_TIMEZONE = os.environ.get("AI_CALENDAR_TIMEZONE", "Asia/Tokyo")
CALENDAR_DEFAULT_MINUTES = int(
    os.environ.get("AI_CALENDAR_DEFAULT_MINUTES", "60")
)
