"""Post-process classification output without changing automation decisions."""

from output_policy import needs_japanese_rewrite


TOP_LEVEL_TEXT_FIELDS = (
    "summary",
    "notification_text",
    "event_title",
    "event_location",
    "event_description",
    "persistent_task_text",
)


def classification_needs_japanese_rewrite(result):
    values = [result.get(field) for field in TOP_LEVEL_TEXT_FIELDS]
    values.extend(result.get("missing_information") or [])
    for action in result.get("actions") or []:
        values.append(action.get("objective"))
        values.extend(action.get("requested_items") or [])
    return any(needs_japanese_rewrite(value) for value in values if value)


def merge_rewritten_text(result, rewritten):
    """Merge only user-facing prose so the LLM cannot alter routing fields."""
    for field in TOP_LEVEL_TEXT_FIELDS:
        if field in rewritten:
            result[field] = rewritten[field]

    missing = rewritten.get("missing_information")
    original_missing = result.get("missing_information") or []
    if isinstance(missing, list) and len(missing) == len(original_missing):
        result["missing_information"] = missing

    original_actions = result.get("actions") or []
    rewritten_actions = rewritten.get("actions") or []
    if len(original_actions) == len(rewritten_actions):
        for original, replacement in zip(original_actions, rewritten_actions):
            if "objective" in replacement:
                original["objective"] = replacement["objective"]
            requested_items = replacement.get("requested_items")
            original_items = original.get("requested_items") or []
            if isinstance(requested_items, list) and len(requested_items) == len(original_items):
                original["requested_items"] = requested_items
    return result


def rewrite_classification_output(result, rewriter):
    if not classification_needs_japanese_rewrite(result):
        return result
    return merge_rewritten_text(result, rewriter(result))
