import os
import json
import sqlite3
import sys
import requests
from ddgs import DDGS
from datetime import datetime

from action_log import (
    get_action,
    record_action,
    recent_actions,
    run_pending_calendar_deletes,
    undo_action,
)
from ai_memo import is_ready_to_trash, parse_ai_memo, trash_processed_ai_memo
from automation_config import OLLAMA_THINK, VAGUE_TIMES
from calendar_worker import delete_calendar_event
from classification_output import rewrite_classification_output
from intent_fallback import apply_intent_fallback
from instance_lock import SingleInstanceLock
from inbox_client import get_inbox_client
from output_policy import infer_research_notification_mode
from persistent_reminders import (
    add_task as add_persistent_task,
    complete_group,
    complete_task as complete_persistent_task,
    create_notify_now_command,
    list_active_groups,
    list_active_tasks as list_active_persistent_tasks,
    normalize_task_text,
)
from report_notifier import send_execution_report
from project_paths import DB_PATH, RUNTIME_DIR, ensure_runtime_directories
from reminder_worker import dispatch_persistent_now, send_notification
from failure_notifier import notify_processing_failure
from state_store import (
    cancel_reminders,
    cancel_web_monitors,
    claim_note,
    ensure_schema,
    mark_note_failed as store_mark_note_failed,
    mark_note_processed,
    mark_note_waiting_downstream,
    note_content_hash,
    is_generated_note,
    item_id_for,
    note_revision,
    list_pending_reminders,
    list_active_web_monitors,
    save_structured_item,
    supersede_orphan_items,
    upsert_wake_job,
    upsert_research_job,
    upsert_web_monitor,
    utc_now,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(errors="backslashreplace")

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen3:14b"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
if not NTFY_TOPIC:
    raise RuntimeError("NTFY_TOPIC is required")


def mark_note_failed(conn, note_id, content_hash, error):
    """Persist the retry state, then notify the user without masking the error."""
    store_mark_note_failed(conn, note_id, content_hash, error)
    notify_processing_failure(
        conn,
        NTFY_TOPIC,
        "AIメモの受付・分類",
        note_id,
        error,
        retrying=True,
    )



# =========================================================
# 共通: OllamaにJSONを返させる
# =========================================================

def ask_ollama(system_prompt, user_data, schema):
    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "system",
                "content": system_prompt
            },
            {
                "role": "user",
                "content": (
                    user_data
                    if isinstance(user_data, str)
                    else json.dumps(user_data, ensure_ascii=False)
                )
            }
        ],
        "format": schema,
        "keep_alive": "30m",
        "think": OLLAMA_THINK,
        "stream": False,
        "options": {
            "temperature": 0.1
        }
    }

    response = requests.post(
        OLLAMA_URL,
        json=payload,
        timeout=120
    )

    response.raise_for_status()

    content = response.json()["message"]["content"]

    return json.loads(content)


# =========================================================
# 1. Keepメモの意図分類
# =========================================================


ITEM_SCHEMA = {   'type': 'object',
    'properties': {   'intent': {   'type': 'string',
                                    'enum': [   'memo',
                                                'todo',
                                                'web_monitor',
                                                'web_monitor_manage',
                                                'reminder_manage',
                                                'calendar',
                                                'reminder',
                                                'persistent_reminder',
                                                'wake_briefing',
                                                'research',
                                                'unknown',
                                                'correction']},
                      'summary': {'type': 'string'},
                      'notification_text': {'type': ['string', 'null']},
                      'event_title': {'type': ['string', 'null']},
                      'event_start': {'type': ['string', 'null']},
                      'event_end': {'type': ['string', 'null']},
                      'all_day': {'type': 'boolean'},
                      'event_location': {'type': ['string', 'null']},
                      'event_description': {'type': ['string', 'null']},
                      'actionable': {'type': 'boolean'},
                      'calendar_ready': {'type': 'boolean'},
                      'needs_target_resolution': {'type': 'boolean'},
                      'needs_confirmation': {'type': 'boolean'},
                      'missing_information': {   'type': 'array',
                                                 'items': {'type': 'string'}},
                      'scheduled_at': {'type': ['string', 'null']},
                      'recurrence': {'type': ['string', 'null']},
                      'persistent_reminder_action': {   'type': ['string', 'null'],
                                                        'enum': [   'add',
                                                                    'complete',
                                                                    'notify_now',
                                                                    None]},
                      'persistent_task_text': {'type': ['string', 'null']},
                      'persistent_target_id': {'type': ['integer', 'null']},
                      'web_monitor_action': {   'type': ['string', 'null'],
                                                'enum': ['list', 'delete', None]},
                      'web_monitor_target_ids': {   'type': 'array',
                                                    'items': {'type': 'integer'}},
                      'reminder_manage_action': {   'type': ['string', 'null'],
                                                    'enum': ['list', 'cancel', None]},
                      'reminder_target_ids': {   'type': 'array',
                                                 'items': {'type': 'string'}},
                      'actions': {   'type': 'array',
                                     'items': {   'type': 'object',
                                                  'properties': {   'type': {   'type': 'string',
                                                                                'enum': [   'research',
                                                                                            'save_memo',
                                                                                            'notify']},
                                                                    'objective': {   'type': [   'string',
                                                                                                 'null']},
                                                                    'requested_items': {   'type': 'array',
                                                                                           'items': {   'type': 'string'}},
                                                                    'when': {   'type': 'string',
                                                                                'enum': [   'now',
                                                                                            'after_previous',
                                                                                            'at_time']},
                                                                    'execute_at': {   'type': [   'string',
                                                                                                  'null']},
                                                                    'scheduled_at': {   'type': [   'string',
                                                                                                    'null']},
                                                                    'notification_mode': {   'type': [   'string',
                                                                                                         'null'],
                                                                                             'enum': [   'completion_only',
                                                                                                         'result_summary',
                                                                                                         'detailed_result',
                                                                                                         None]}},
                                                  'required': [   'type',
                                                                  'objective',
                                                                  'requested_items',
                                                                  'when',
                                                                  'execute_at',
                                                                  'scheduled_at',
                                                                  'notification_mode']}},
                      'persistent_group': {'type': ['string', 'null']},
                      'correction_target': {'type': ['string', 'null']},
                      'correction_action': {   'type': ['string', 'null'],
                                               'enum': [   'reschedule',
                                                           'rewrite',
                                                           'cancel',
                                                           None]}},
    'required': [   'intent',
                    'summary',
                    'notification_text',
                    'event_title',
                    'event_start',
                    'event_end',
                    'all_day',
                    'event_location',
                    'event_description',
                    'actionable',
                    'calendar_ready',
                    'needs_target_resolution',
                    'needs_confirmation',
                    'missing_information',
                    'scheduled_at',
                    'recurrence',
                    'persistent_reminder_action',
                    'persistent_task_text',
                    'persistent_target_id',
                    'web_monitor_action',
                    'web_monitor_target_ids',
                    'reminder_manage_action',
                    'reminder_target_ids',
                    'actions',
                    'persistent_group',
                    'correction_target',
                    'correction_action']}


CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {"type": "array", "items": ITEM_SCHEMA, "minItems": 1}
    },
    "required": ["items"],
}


CLASSIFY_PROMPT = """
You classify Japanese speech-to-text requests for a personal local automation
system. Return only the requested JSON schema.

OUTPUT SHAPE
Return {"items": [...]}. One spoken note often contains several unrelated
requests, for example "明日9時に歯医者、あと牛乳買うのリマインドして、それと
第74回の登録開始も見張っといて". Emit one item per distinct request, in the order
they were spoken. Emit exactly one item when the note contains one request.
Never merge two unrelated requests into one item and never drop one.

NEVER ASK, ALWAYS DECIDE
The user speaks into a phone and will not answer follow-up questions. Choosing
the most reasonable interpretation is always better than refusing to decide.
- Never use needs_confirmation to stall. Set it only as extra information.
- Prefer a concrete intent over "unknown". Use "unknown" only when the text
  carries no request at all.
- If a date or time is partly unclear, still choose the most likely intent and
  leave the unresolved field null. Downstream code reroutes the item to a form
  that can run without that field, so a null is safe and a refusal is not.
- Tolerate speech-recognition noise. Repair obvious mishearings from context
  instead of treating them as unknown.

Intents:
- reminder: the user explicitly asks to be notified/reminded at a time.
- persistent_reminder: manages a locally saved task that has no specific due
  date/time and should remain active until the user says it is finished. It also
  covers commands to notify all currently saved persistent reminder tasks now.
- wake_briefing: the user reports that they have just woken up and wants the
  automatic wake-up briefing. Recognize natural Japanese variants such as
  "起きた", "今起きた", "起床した", "目が覚めた", and "おはよう" when they
  clearly refer to the user's current wake-up. Tolerate small speech-recognition
  or spelling variations, but do not classify unrelated uses of 起きた such as
  an incident or event occurring.
- calendar: a concrete event with a sufficiently clear date/time, but not a notification request.
- todo: an action, check, investigation, or task without a concrete schedulable date/time.
- web_monitor: asks to keep watching until a web page, registration, update, or change appears.
- web_monitor_manage: lists or deletes already saved web-monitor tasks.
- reminder_manage: lists or cancels date/time-specific reminders that have not
  fired yet.
- research: asks to investigate information now or at a specified future time and
  return a finite result. This is different from continuously watching for a change.
- memo: information or an idea to retain, with no action request.
- correction: the user is fixing something they just said, such as
  "さっきのリマインダー9時じゃなくて10時" or "今の予定やっぱりなし". Match the
  request against recent_actions and put that entry's token in correction_target.
- unknown: only when the text carries no request at all.

Rules:
- ユーザー向けの文章は必ず日本語で生成すること。
- Every user-facing natural-language value MUST be written in Japanese. This
  includes summary, notification_text, missing_information, action objectives,
  requested_items, and reasons. Keep official names, product names, and URLs in
  their original form when appropriate, but write all explanations in Japanese.
- Never invent a date, time, place, person, URL, deadline, or action.
- Prefer todo over memo when the user clearly wants an action.
- Do not turn an uncertain date into calendar.
- For calendar, separate the event title from scheduling and command language.
  event_title must contain only the natural Japanese name of the event, such as
  "田中さんと会う" or "高校の同期の飲み会". Never include date/time
  phrases such as "明後日" or "9月20日", or commands such as "カレンダーに
  追加して" in event_title.
- Resolve relative calendar dates such as "明日" and "明後日" from
  current_datetime. If no clock time is stated, use all_day=true and event_start
  as YYYY-MM-DD. If a clock time is stated, use all_day=false and event_start as
  ISO 8601 with +09:00. event_end may be null when the user gives no end.
- When only a month and day are given for a future plan, resolve the next upcoming
  occurrence from current_datetime without putting the date into event_title.
- calendar_ready is true only when event_title and event_start are safely resolved.
  Never invent a missing date. Preserve an explicitly stated location and
  description in their dedicated fields and omit them from event_title when they
  are clearly separable; otherwise use null. For non-calendar intents, all calendar fields
  are null and all_day is false.
- For reminder, set scheduled_at to an ISO 8601 datetime with +09:00 only when it can
  be safely resolved from current_datetime. Otherwise set it to null and describe
  the missing information.
- Keep reminder and persistent_reminder strictly separate. A request such as
  "明日の午前4時に洗濯をするって通知して" is reminder because it specifies a
  delivery time. A request such as "バイト先にお菓子を買うって後でリマインドして"
  is persistent_reminder/add because it has no concrete time and asks to keep
  reminding the user.
- For persistent_reminder/add, persistent_task_text must contain only the task the
  user wants to see in notifications. Remove wrappers such as "後で", "通知して",
  "リマインドして", and quotation fillers, without changing the task meaning.
  Set persistent_reminder_action="add" and persistent_target_id=null.
- For persistent_reminder/complete, recognize natural variants such as "終わった",
  "完了した", "済んだ", "もうやった", and "リマインドから消して". Compare the
  request with active_persistent_tasks and set persistent_target_id only when one
  task is a safe semantic match. Set persistent_task_text to the completed task's
  core content. Never complete an unrelated or ambiguous task.
- When the user asks with natural variants such as "今リマインド通知して",
  "今のリマインダーを送って", or "保存中のタスクを今教えて", use
  persistent_reminder/notify_now. This means notify every active saved task now,
  as a separate notification. Task fields and target id are null.
- For non-persistent_reminder intents, persistent_reminder_action,
  persistent_task_text, and persistent_target_id must all be null.
- For web_monitor_manage/list, recognize requests such as "Web監視を教えて",
  "監視中のものを確認", and "今残っている監視一覧". Set
  web_monitor_action="list" and web_monitor_target_ids=[].
- For web_monitor_manage/delete, recognize "削除", "消して", "止めて",
  "監視終了", and similar natural variants. Compare the request with
  active_web_monitors and include an id only when it is a safe semantic match.
  When the user explicitly says all/every monitor, include every active id.
  Never delete all for an ambiguous request. If no safe match exists, return an
  empty id list and do not ask a follow-up question; downstream reports no match.
- For web_monitor (new registration), web_monitor_action must be null and target
  ids must be empty. For every intent other than web_monitor_manage, these fields
  must also be null and empty.
- For reminder_manage/list, recognize requests such as "日時指定リマインダーの一覧",
  "予約中の通知を教えて", and "これから鳴るリマインダーを確認". Set
  reminder_manage_action="list" and reminder_target_ids=[].
- For reminder_manage/cancel, recognize "取消", "キャンセル", "消して", and
  similar variants. Compare against active_scheduled_reminders. Include a note_id
  only for a safe semantic/time match. Include every id only when the user clearly
  says all/every reminder. Never cancel all from an ambiguous request. If no safe
  match exists, return an empty list; downstream reports no match without asking.
- For reminder (new registration), reminder_manage_action must be null and target
  ids must be empty. For every intent other than reminder_manage, these fields
  must also be null and empty.
- For wake_briefing, do not invent content. Set scheduled_at=null, all calendar
  fields null, persistent-reminder fields null, and actions=[]. The downstream
  wake worker obtains weather, calendar events, and saved reminder tasks.
- Resolve relative times such as "20分後" from current_datetime. Resolve vague
  day parts such as "明日の朝" with vague_time_defaults. An explicit clock time
  always overrides a vague-time default.
- For reminder, notification_text is the short, natural Japanese content the user
  actually wants to see. Remove the delivery command and scheduling wrapper such
  as "明日の午前4時に", "通知して", "リマインドして", and
  quotation fillers such as "って" when they only instruct this system. Preserve
  the task meaning. Example: "明日の午前4時に部屋の掃除をするって通知して"
  becomes notification_text "部屋の掃除をする". For non-reminders use null.
- scheduled_at must be null for every non-reminder intent.
- recurrence is null for one-time reminders. For repeating reminders use only
  daily or weekly:MO/TU/WE/TH/FR/SA/SU.
- For research, return an ordered actions list. The first action is research.
  Add save_memo when the result should be saved to Keep. Add notify when the result
  should be sent. Use execute_at for when research starts and scheduled_at for when
  its result is notified. Keep these two times separate.
- On a notify action, set notification_mode to completion_only when the user says
  to notify only when the research finishes; result_summary when they ask to
  notify/tell the result (also the default for an ambiguous "通知して"); and
  detailed_result only when they explicitly request details, everything, or no
  omission. Other actions use null.
- actions must be [] for non-research intents.
- summary and missing_information must be Japanese.
- persistent_group groups saved tasks that belong to one running list, so they
  arrive as a single notification instead of several. Use a short Japanese noun
  such as "買い物" or "持ち物" when the task clearly belongs to such a list, for
  example "牛乳買うのリマインドして" -> persistent_group "買い物". Use null when
  the task does not belong to a recurring list. Existing groups are supplied in
  active_persistent_groups; reuse an existing name instead of inventing a synonym.
- When the user says a whole group is finished, for example "買い物終わった",
  use persistent_reminder/complete with persistent_task_text set to the group
  name and persistent_target_id null.
- For correction, set correction_target to the token of the matching entry in
  recent_actions and correction_action to one of:
  reschedule (a new time; put it in scheduled_at),
  rewrite (new wording; put it in summary and notification_text),
  cancel (undo it entirely).
  Match on meaning and recency together: "さっきの" means the newest entry that
  fits the description. If nothing matches safely, set correction_target null and
  classify the note as whatever it describes on its own.
- For every intent other than correction, correction_target and correction_action
  are null. For every intent other than persistent_reminder, persistent_group is
  null.
"""


AUTOMATION_GATE_SCHEMA = {
    "type": "object",
    "properties": {
        "should_process": {"type": "boolean"},
        "reason": {"type": "string"}
    },
    "required": ["should_process", "reason"]
}


AUTOMATION_GATE_PROMPT = """
You are a conservative gate protecting ordinary Google Keep notes.
The note has no explicit [AI] or edge AIメモ marker.
Set should_process=true only when the text clearly asks for automation, such as:
- notify/remind at a time
- create or retain a concrete task or calendar event
- add, complete, or immediately notify a persistent reminder task
- report waking up or request the automatic wake-up briefing
- continuously monitor a website/event/change
- list, stop, or delete saved web-monitor tasks
- list or cancel pending date/time reminders
- perform a one-time investigation and return, save, or notify the result
Ambiguous notes, reference material, diary entries, ideas, passwords, ordinary lists,
and text that merely mentions automation must be false. Never infer an unstated request.
Write reason in Japanese.
All user-facing natural-language output must be Japanese.
"""


JAPANESE_REWRITE_PROMPT = """
Rewrite every user-facing prose field that is not already Japanese into natural
Japanese. Preserve every fact, number, date, proper noun, product name, URL, array
length, null value, and all routing or automation fields exactly. Do not add facts.
Return the same JSON structure using the supplied schema.
ユーザー向けの文章は必ず日本語で生成してください。
"""


def ensure_classification_japanese(result):
    return rewrite_classification_output(
        result,
        lambda value: ask_ollama(
            JAPANESE_REWRITE_PROMPT,
            value,
            CLASSIFY_SCHEMA,
        ),
    )


def should_process_unmarked(text):
    return ask_ollama(
        AUTOMATION_GATE_PROMPT,
        text,
        AUTOMATION_GATE_SCHEMA,
    )


def classify_note(
    text,
    active_persistent_tasks=None,
    active_web_monitors=None,
    active_scheduled_reminders=None,
):
    result = ask_ollama(
        CLASSIFY_PROMPT,
        {
            "current_datetime": datetime.now().astimezone().isoformat(),
            "timezone": "Asia/Tokyo",
            "vague_time_defaults": VAGUE_TIMES,
            "active_persistent_tasks": [
                {"id": task_id, "task_text": task_text}
                for task_id, task_text in (active_persistent_tasks or [])
            ],
            "active_web_monitors": active_web_monitors or [],
            "active_scheduled_reminders": active_scheduled_reminders or [],
            "text": text,
        },
        CLASSIFY_SCHEMA
    )
    return ensure_classification_japanese(result)


def execute_web_monitor_management(conn, result, topic, sender=send_notification):
    action = result.get("web_monitor_action")
    if action == "list":
        active = list_active_web_monitors(conn)
        if active:
            lines = [f"{index}. {item['request_text']}" for index, item in enumerate(active, 1)]
            message = "現在のWeb監視タスクです。\n\n" + "\n".join(lines)
        else:
            message = "現在、Web監視タスクはありません。"
        sender(topic, message, title="Web監視タスク一覧")
        return active

    if action == "delete":
        cancelled = cancel_web_monitors(
            conn,
            result.get("web_monitor_target_ids") or [],
        )
        if cancelled:
            lines = [f"・{item['request_text']}" for item in cancelled]
            message = "次のWeb監視タスクを停止しました。\n\n" + "\n".join(lines)
        else:
            message = "指定内容に一致する有効なWeb監視タスクはありませんでした。"
        sender(topic, message, title="Web監視タスク更新")
        return cancelled

    raise ValueError("Web監視タスクの操作を判定できませんでした")


def _format_reminder_time(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.strftime("%Y年%m月%d日 %H:%M")
        return parsed.astimezone().strftime("%Y年%m月%d日 %H:%M")
    except (AttributeError, TypeError, ValueError):
        return str(value or "日時不明")


def execute_reminder_management(conn, result, topic, sender=send_notification):
    action = result.get("reminder_manage_action")
    if action == "list":
        active = list_pending_reminders(conn)
        if active:
            lines = [
                f"{index}. {_format_reminder_time(item['scheduled_at'])}  {item['summary']}"
                for index, item in enumerate(active, 1)
            ]
            message = "現在の日時指定リマインダーです。\n\n" + "\n".join(lines)
        else:
            message = "現在、日時指定リマインダーはありません。"
        sender(topic, message, title="日時指定リマインダー一覧")
        return active

    if action == "cancel":
        cancelled = cancel_reminders(
            conn,
            result.get("reminder_target_ids") or [],
        )
        if cancelled:
            lines = [
                f"・{_format_reminder_time(item['scheduled_at'])}  {item['summary']}"
                for item in cancelled
            ]
            message = "次のリマインダーを取り消しました。\n\n" + "\n".join(lines)
        else:
            message = "指定内容に一致する未通知のリマインダーはありませんでした。"
        sender(topic, message, title="リマインダー取消")
        return cancelled

    raise ValueError("日時指定リマインダーの操作を判定できませんでした")


def build_research_plan(result, original_request=""):
    actions = result.get("actions") or []
    research = next(
        (action for action in actions if action.get("type") == "research"),
        None,
    )
    if research is None:
        research = {
            "objective": result["summary"],
            "requested_items": [],
            "execute_at": None,
        }
    save_to_keep = any(action.get("type") == "save_memo" for action in actions)
    if not save_to_keep and (
        "Keep" in original_request
        or "メモに残" in original_request
        or "メモに書" in original_request
    ):
        save_to_keep = True
    notify = next(
        (action for action in actions if action.get("type") == "notify"),
        None,
    )
    notify_mode = "none"
    notify_at = None
    if notify:
        notify_at = notify.get("scheduled_at")
        notify_mode = "at_time" if notify_at else "after_completion"
    elif "通知" in original_request or "教えて" in original_request:
        notify_mode = "after_completion"
    notification_content_mode = infer_research_notification_mode(
        original_request,
        notify.get("notification_mode") if notify else None,
    )
    objective = (research.get("objective") or result["summary"]).strip()
    return {
        "objective": objective,
        "query": objective,
        "requested_items": research.get("requested_items") or [],
        "execute_at": research.get("execute_at"),
        "save_to_keep": save_to_keep,
        "notify_mode": notify_mode,
        "notify_at": notify_at,
        "notification_content_mode": notification_content_mode,
    }


# =========================================================
# 2. Web監視対象の検索・選定
# =========================================================

RESOLVE_SCHEMA = {
    "type": "object",
    "properties": {
        "target_found": {
            "type": "boolean"
        },
        "target_result_id": {
            "type": ["integer", "null"]
        },
        "monitor_result_ids": {
            "type": "array",
            "items": {
                "type": "integer"
            }
        },
        "reason": {
            "type": "string"
        }
    },
    "required": [
        "target_found",
        "target_result_id",
        "monitor_result_ids",
        "reason"
    ]
}


RESOLVE_PROMPT = """
あなたはWeb監視システムの監視対象選定担当です。
ユーザー向けの自然言語は、固有名詞を除き必ず日本語にしてください。

ユーザーの依頼とWeb検索結果を読み、
目的そのものを満たすページが既に存在するか判定してください。

重要ルール:

- 検索結果に存在しないURLを作らない。
- URLではなく検索結果idだけを返す。
- 公式サイトを最優先する。
- 類似イベントや過去回を目的ページと誤認しない。
- 「発表申込」と「参加登録」は必ず区別する。

特に重要:
- ユーザーが「第73回」を求めている場合、
  第72回、第71回など過去回の個別イベントページは
  targetにもmonitor候補にも選ばない。
- 目的ページがまだ無い場合、
  過去回の個別ページではなく、
  主催団体の公式トップページ、
  年間開催予定ページ、
  公式イベント一覧など、
  将来の新規イベント情報が掲載されるページを優先する。
- 適切な固定監視ページが無ければ
  monitor_result_ids は空配列でもよい。
  後続システムがWeb検索を定期的にやり直すため、
  無理に監視URLを選ぶ必要はない。

target_found:
ユーザーの目的そのものを満たすページが既に存在する場合true。

target_result_id:
target_found=trueなら、そのページの検索結果id。
存在しなければnull。

monitor_result_ids:
目的ページがまだ存在しない場合、
将来の公開を補助的に検出するために監視する価値がある
安定した公式ページのid。

reason:
日本語で簡潔に説明する。
"""


def resolve_web_monitor(request_text):
    search_query = request_text + " 公式"

    print(f"Web検索中: {search_query}")

    results = list(
        DDGS().text(
            search_query,
            max_results=8
        )
    )

    if not results:
        return {
            "search_query": search_query,
            "target_found": False,
            "found_url": None,
            "monitor_urls": [],
            "reason": "検索結果なし"
        }

    candidates = []

    for i, r in enumerate(results):
        candidates.append({
            "id": i,
            "title": r.get("title", ""),
            "url": r.get("href", ""),
            "description": r.get("body", "")
        })

    decision = ask_ollama(
        RESOLVE_PROMPT,
        {
            "request": request_text,
            "search_results": candidates
        },
        RESOLVE_SCHEMA
    )

    found_url = None
    monitor_urls = []

    if decision["target_found"]:
        idx = decision["target_result_id"]

        if (
            idx is not None
            and 0 <= idx < len(candidates)
        ):
            found_url = candidates[idx]["url"]

    else:
        for idx in decision["monitor_result_ids"]:
            if 0 <= idx < len(candidates):
                monitor_urls.append(
                    candidates[idx]["url"]
                )

    return {
        "search_query": search_query,
        "target_found": decision["target_found"],
        "found_url": found_url,
        "monitor_urls": monitor_urls,
        "reason": decision["reason"]
    }


# =========================================================
# 3. 分類結果の正規化
# =========================================================

def normalize_items(result):
    """Accept both the items envelope and a bare legacy classification."""
    if isinstance(result, dict) and isinstance(result.get("items"), list):
        items = [item for item in result["items"] if isinstance(item, dict)]
    elif isinstance(result, dict):
        items = [result]
    else:
        items = []
    return [item for item in items if item.get("intent")]


def classify_note(
    text,
    active_persistent_tasks=None,
    active_web_monitors=None,
    active_scheduled_reminders=None,
    active_persistent_groups=None,
    recent=None,
):
    result = ask_ollama(
        CLASSIFY_PROMPT,
        {
            "current_datetime": datetime.now().astimezone().isoformat(),
            "timezone": "Asia/Tokyo",
            "vague_time_defaults": VAGUE_TIMES,
            "active_persistent_tasks": [
                {"id": task_id, "task_text": task_text}
                for task_id, task_text in (active_persistent_tasks or [])
            ],
            "active_persistent_groups": active_persistent_groups or [],
            "active_web_monitors": active_web_monitors or [],
            "active_scheduled_reminders": active_scheduled_reminders or [],
            "recent_actions": recent or [],
            "text": text,
        },
        CLASSIFY_SCHEMA,
    )
    return ensure_classification_japanese(result)


def format_detail(item):
    """Short, scannable context for the execution report."""
    intent = item.get("intent")
    if intent == "reminder" and item.get("scheduled_at"):
        return _format_reminder_time(item["scheduled_at"])
    if intent == "calendar" and item.get("event_start"):
        if item.get("all_day"):
            return _format_reminder_time(item["event_start"]).split(" ")[0]
        return _format_reminder_time(item["event_start"])
    if intent == "persistent_reminder" and item.get("persistent_group"):
        return item["persistent_group"]
    return None


def report_summary(item):
    if item.get("intent") == "reminder":
        return item.get("notification_text") or item.get("summary") or ""
    if item.get("intent") == "calendar":
        return item.get("event_title") or item.get("summary") or ""
    if item.get("intent") == "persistent_reminder":
        return item.get("persistent_task_text") or item.get("summary") or ""
    return item.get("summary") or ""


# =========================================================
# 4. 訂正
# =========================================================

def apply_correction(conn, item, pending_calendar_deletes=None, now=None):
    """Rewrite or undo something the user just registered.

    Voice input is corrected immediately after it is spoken, so this path has to
    work from a vague reference like "さっきの" rather than an exact identifier.
    """
    now = now or utc_now()
    token = (item.get("correction_target") or "").strip()
    action = item.get("correction_action")
    if not token:
        raise ValueError("訂正の対象を特定できませんでした")
    target = get_action(conn, token)
    if target is None:
        raise ValueError("訂正の対象が見つかりませんでした")

    if action == "cancel":
        ok, message = undo_action(conn, token, pending_calendar_deletes)
        if not ok:
            raise ValueError(message)
        return {"summary": message, "detail": None, "target_key": target["target_key"]}

    item_id = target["target_key"]
    kind = target["kind"]
    if target["undone"]:
        raise ValueError(f"すでに取り消された操作は訂正できません: {target['summary']}")

    if action == "reschedule":
        scheduled_at = item.get("scheduled_at")
        if not scheduled_at:
            raise ValueError("変更後の日時を読み取れませんでした")
        if kind == "reminder":
            cursor = conn.execute("""
                UPDATE reminders
                SET scheduled_at = ?, status = 'pending', notified_at = NULL,
                    last_error = NULL, updated_at = ?
                WHERE note_id = ? AND status IN ('pending', 'waiting_information',
                                                 'notified', 'sending')
            """, (scheduled_at, now, item_id))
        elif kind == "calendar":
            cursor = conn.execute("""
                UPDATE calendar_jobs
                SET event_start = ?, status = 'pending', calendar_event_id = NULL,
                    last_error = NULL, updated_at = ?
                WHERE note_id = ? AND status != 'cancelled'
            """, (scheduled_at, now, item_id))
        else:
            raise ValueError("この操作は日時を変更できません")
        # Reporting success for an UPDATE that matched nothing would tell the
        # user their correction landed when the row was already gone.
        if cursor.rowcount == 0:
            raise ValueError(f"変更対象が見つかりませんでした: {target['summary']}")
        return {
            "summary": f"{target['summary']}",
            "detail": _format_reminder_time(scheduled_at),
            "target_key": item_id,
        }

    if action == "rewrite":
        new_text = item.get("notification_text") or item.get("summary")
        if not new_text:
            raise ValueError("変更後の内容を読み取れませんでした")
        if kind == "reminder":
            cursor = conn.execute(
                "UPDATE reminders SET summary = ?, updated_at = ? WHERE note_id = ?",
                (new_text, now, item_id),
            )
        elif kind == "persistent_reminder":
            cursor = conn.execute("""
                UPDATE persistent_reminders
                SET task_text = ?, normalized_text = ?, updated_at = ?
                WHERE source_note_id = ? AND status = 'active'
            """, (new_text, normalize_task_text(new_text), now, item_id))
        elif kind == "todo":
            cursor = conn.execute(
                "UPDATE todos SET summary = ?, updated_at = ? WHERE note_id = ?",
                (new_text, now, item_id),
            )
        elif kind == "memo":
            cursor = conn.execute(
                "UPDATE memos SET summary = ?, updated_at = ? WHERE note_id = ?",
                (new_text, now, item_id),
            )
        elif kind == "calendar":
            cursor = conn.execute(
                "UPDATE calendar_jobs SET event_title = ?, summary = ?, "
                "status = 'pending', updated_at = ? WHERE note_id = ?",
                (new_text, new_text, now, item_id),
            )
        else:
            raise ValueError("この操作は内容を変更できません")
        if cursor.rowcount == 0:
            raise ValueError(f"変更対象が見つかりませんでした: {target['summary']}")
        return {"summary": new_text, "detail": None, "target_key": item_id}

    raise ValueError("訂正の種類を判定できませんでした")


# =========================================================
# 5. 1アイテムの実行
# =========================================================

# Intents that publish their own notification, so the execution report skips them.
SELF_REPORTING_INTENTS = {"web_monitor_manage", "reminder_manage"}
# Intents whose real work happens in a later worker in the same cycle.
DOWNSTREAM_INTENTS = {"wake_briefing", "research"}


def process_item(conn, cur, item, item_id, source_note_id, ai_text, inbox_source,
                 ai_memo_explicit, pending_calendar_deletes=None):
    """Persist one work item. Returns (report_entry, needs_downstream)."""
    item, fallback_reason = apply_intent_fallback(item)
    intent = item["intent"]

    if intent == "correction":
        outcome = apply_correction(conn, item, pending_calendar_deletes)
        # Log the correction too. Without it, recent_actions keeps offering the
        # pre-correction wording, so a second "さっきの" resolves against text
        # the user already replaced.
        record_action(
            conn,
            source_note_id=source_note_id,
            item_id=item_id,
            kind="correction",
            summary=outcome["summary"],
            detail=outcome["detail"],
            note_revision=note_revision(conn, source_note_id),
            target_key=outcome.get("target_key") or item_id,
        )
        return {
            "kind": "correction",
            "summary": outcome["summary"],
            "detail": outcome["detail"],
            "fallback_reason": None,
            "token": None,
        }, False

    cur.execute("""
    INSERT OR REPLACE INTO ai_results (
        note_id, title, original_text, intent, summary, actionable,
        calendar_ready, needs_target_resolution, needs_confirmation,
        missing_information, processed_at, scheduled_at, automation_source,
        recurrence, actions, notification_text, event_title, event_start,
        event_end, all_day, event_location, event_description,
        persistent_reminder_action, persistent_task_text, persistent_target_id
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        item_id,
        item.get("title") or "",
        ai_text,
        intent,
        item.get("summary") or "",
        int(bool(item.get("actionable"))),
        int(bool(item.get("calendar_ready"))),
        int(bool(item.get("needs_target_resolution"))),
        int(bool(item.get("needs_confirmation"))),
        json.dumps(item.get("missing_information") or [], ensure_ascii=False),
        datetime.now().isoformat(),
        item.get("scheduled_at"),
        (
            "google_tasks"
            if inbox_source == "google_tasks"
            else ("explicit_ai_memo" if ai_memo_explicit else "inferred_automation")
        ),
        item.get("recurrence"),
        json.dumps(item.get("actions") or [], ensure_ascii=False),
        item.get("notification_text"),
        item.get("event_title"),
        item.get("event_start"),
        item.get("event_end"),
        int(bool(item.get("all_day"))),
        item.get("event_location"),
        item.get("event_description"),
        item.get("persistent_reminder_action"),
        item.get("persistent_task_text"),
        item.get("persistent_target_id"),
    ))

    save_structured_item(
        cur,
        item_id,
        item.get("title") or "",
        ai_text,
        {
            "intent": intent,
            "summary": item.get("summary") or "",
            "actionable": bool(item.get("actionable")),
            "calendar_ready": bool(item.get("calendar_ready")),
            "needs_target_resolution": bool(item.get("needs_target_resolution")),
            "needs_confirmation": bool(item.get("needs_confirmation")),
            "missing_information": item.get("missing_information") or [],
            "scheduled_at": item.get("scheduled_at"),
            "recurrence": item.get("recurrence"),
            "notification_text": item.get("notification_text"),
            "event_title": item.get("event_title"),
            "event_start": item.get("event_start"),
            "event_end": item.get("event_end"),
            "all_day": bool(item.get("all_day")),
            "event_location": item.get("event_location"),
            "event_description": item.get("event_description"),
        },
        source_note_id=source_note_id,
    )

    target_key = item_id
    if intent == "persistent_reminder":
        action = item.get("persistent_reminder_action")
        if action == "add":
            task_id = add_persistent_task(
                conn,
                item_id,
                item.get("persistent_task_text"),
                group_name=item.get("persistent_group"),
            )
            # add_task deduplicates by wording and may hand back a row created
            # by an earlier note. Undo has to reach that row, not this item id.
            owner = conn.execute(
                "SELECT source_note_id FROM persistent_reminders WHERE id = ?",
                (task_id,),
            ).fetchone()
            if owner:
                target_key = owner[0]
        elif action == "complete":
            cleared = complete_group(
                conn, item.get("persistent_task_text"), command_note_id=item_id
            )
            if not cleared:
                complete_persistent_task(
                    conn,
                    item_id,
                    target_task_id=item.get("persistent_target_id"),
                    task_text=item.get("persistent_task_text"),
                )
        elif action == "notify_now":
            create_notify_now_command(conn, item_id)

    elif intent == "web_monitor":
        resolved = resolve_web_monitor(ai_text)
        upsert_web_monitor(
            cur,
            item_id,
            item.get("summary") or "",
            resolved,
            source_note_id=source_note_id,
        )

    elif intent == "wake_briefing":
        upsert_wake_job(cur, item_id, source_note_id=source_note_id)

    elif intent == "research":
        upsert_research_job(cur, item_id, build_research_plan(item, ai_text))

    if intent in SELF_REPORTING_INTENTS:
        return None, False

    token = record_action(
        conn,
        source_note_id=source_note_id,
        item_id=item_id,
        kind=intent,
        summary=report_summary(item),
        detail=format_detail(item),
        fallback_reason=fallback_reason,
        note_revision=note_revision(conn, source_note_id),
        target_key=target_key,
    )
    return {
        "kind": intent,
        "summary": report_summary(item),
        "detail": format_detail(item),
        "fallback_reason": fallback_reason,
        "token": token,
    }, intent in DOWNSTREAM_INTENTS or (
        intent == "calendar" and bool(item.get("calendar_ready"))
    )


def run_side_effects(conn, item, item_id):
    """Network work that must happen after the item's state is committed."""
    intent = item.get("intent")
    if intent == "web_monitor_manage":
        execute_web_monitor_management(conn, item, NTFY_TOPIC)
    elif intent == "reminder_manage":
        execute_reminder_management(conn, item, NTFY_TOPIC)
    elif (
        intent == "persistent_reminder"
        and item.get("persistent_reminder_action") == "notify_now"
    ):
        dispatch_persistent_now(conn, NTFY_TOPIC, item_id)


# =========================================================
# 6. 1メモの処理
# =========================================================

def process_note(conn, cur, keep, note, inbox_source):
    """Returns True when the note produced work."""
    title = note.title or ""
    text = note.text or ""
    if (
        inbox_source == "google_keep"
        and (title.lstrip().startswith("[AI結果]") or is_generated_note(cur, note.id))
    ):
        return False

    content_hash = note_content_hash(title, text)
    if not claim_note(conn, note.id, content_hash):
        return False

    full_text = ""
    if title:
        full_text += f"タイトル: {title}\n"
    if text:
        full_text += f"本文: {text}"
    full_text = full_text.strip()

    ai_memo = parse_ai_memo(title, text)
    ai_text = ai_memo.text_for_ai if ai_memo.explicit else full_text
    trusted_inbox_item = ai_memo.explicit or (
        inbox_source == "google_tasks"
        and getattr(note, "is_dedicated_inbox", False)
    )
    if inbox_source == "google_tasks" and not ai_memo.explicit:
        ai_text = (text or title).strip()

    if not full_text:
        mark_note_processed(conn, note.id, content_hash)
        return False

    if not trusted_inbox_item:
        try:
            gate = should_process_unmarked(full_text)
        except Exception as error:
            mark_note_failed(conn, note.id, content_hash, error)
            return False
        if not gate["should_process"]:
            mark_note_processed(conn, note.id, content_hash)
            return False

    print()
    print("=" * 60)
    print(full_text)
    print("\nAIで意図判定中...")

    try:
        classification = classify_note(
            ai_text,
            list_active_persistent_tasks(conn),
            list_active_web_monitors(conn),
            list_pending_reminders(conn),
            list_active_groups(conn),
            recent_actions(conn),
        )
    except Exception as error:
        print("分類失敗:", error)
        mark_note_failed(conn, note.id, content_hash, error)
        return False

    items = normalize_items(classification)
    if not items:
        # An empty classification is still a note the user spoke, so keep it
        # rather than discarding it silently.
        items = [{"intent": "unknown", "summary": ai_text[:200]}]

    print("\n分類結果:", json.dumps(items, ensure_ascii=False, indent=2))

    for item in items:
        item.setdefault("title", title)

    item_ids = [item_id_for(note.id, index) for index in range(len(items))]
    entries = []
    needs_downstream = False

    # The classification projection is written as one unit so a failure halfway
    # through a multi-item note cannot leave a partial interpretation behind.
    pending_calendar_deletes = []
    conn.execute("BEGIN IMMEDIATE")
    try:
        supersede_orphan_items(cur, note.id, item_ids)
        for item, item_id in zip(items, item_ids):
            entry, downstream = process_item(
                conn, cur, item, item_id, note.id, ai_text, inbox_source,
                ai_memo.explicit, pending_calendar_deletes,
            )
            if entry:
                entries.append(entry)
            needs_downstream = needs_downstream or downstream
        conn.execute("COMMIT")
    except Exception as error:
        conn.execute("ROLLBACK")
        print("処理失敗:", error)
        mark_note_failed(conn, note.id, content_hash, error)
        return False

    # Side effects run after the commit: they send notifications and delete
    # remote calendar events, and a delivery failure must not roll back state
    # the user can already see.
    for error in run_pending_calendar_deletes(
        pending_calendar_deletes, delete_calendar_event
    ):
        print("カレンダーからの削除に失敗:", error)
        notify_processing_failure(
            conn, NTFY_TOPIC, "カレンダー予定の取り消し", note.id, error, retrying=False,
        )

    for item, item_id in zip(items, item_ids):
        try:
            run_side_effects(conn, item, item_id)
        except Exception as error:
            print("通知処理に失敗:", error)
            notify_processing_failure(
                conn, NTFY_TOPIC, "AIメモの実行", item_id, error, retrying=False,
            )

    if entries:
        try:
            send_execution_report(NTFY_TOPIC, entries)
        except Exception as error:
            print("実行報告の送信に失敗:", error)
            notify_processing_failure(
                conn, NTFY_TOPIC, "実行報告の通知", note.id, error, retrying=False,
            )

    if needs_downstream:
        mark_note_waiting_downstream(conn, note.id, content_hash)
        return True

    if trusted_inbox_item and all(is_ready_to_trash(item) for item in items):
        try:
            trash_processed_ai_memo(keep, note)
        except Exception as error:
            mark_note_failed(conn, note.id, content_hash, error)
            return False

    mark_note_processed(conn, note.id, content_hash)
    return True


# =========================================================
# 7. エントリーポイント
# =========================================================

def main():
    ensure_runtime_directories()
    instance_lock = SingleInstanceLock(str(RUNTIME_DIR / "keep_watcher.lock"))
    if not instance_lock.acquire():
        print("Keep watcher is already running; this invocation will exit.")
        return 0

    print("AI Inbox に接続中...")
    keep, inbox_source = get_inbox_client()
    print(f"AI Inbox source: {inbox_source}")

    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    cur = conn.cursor()
    try:
        ensure_schema(conn)
        new_count = 0
        for note in keep.all():
            if getattr(note, "trashed", False):
                continue
            if process_note(conn, cur, keep, note, inbox_source):
                new_count += 1
    finally:
        conn.close()

    print()
    print("=" * 60)
    if new_count == 0:
        print("新しいメモはありません")
    else:
        print(f"{new_count}件のメモを処理しました")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
