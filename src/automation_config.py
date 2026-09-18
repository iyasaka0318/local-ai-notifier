import os


OLLAMA_THINK = os.environ.get("AI_OLLAMA_THINK", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

VAGUE_TIMES = {
    "morning": os.environ.get("AI_TIME_MORNING", "08:00"),
    "afternoon": os.environ.get("AI_TIME_AFTERNOON", "15:00"),
    "evening": os.environ.get("AI_TIME_EVENING", "18:00"),
    "night": os.environ.get("AI_TIME_NIGHT", "20:00"),
}

MAX_RESEARCH_RESULTS = 12
MAX_RESEARCH_PAGES = 3
MAX_PAGE_BYTES = 1_000_000
MAX_PAGE_TEXT_CHARS = 8_000

CALENDAR_TIMEZONE = os.environ.get("AI_CALENDAR_TIMEZONE", "Asia/Tokyo")
CALENDAR_DEFAULT_MINUTES = int(
    os.environ.get("AI_CALENDAR_DEFAULT_MINUTES", "60")
)
