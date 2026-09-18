/**
 * Local AI Notifier 用 Google Calendar Webhook
 *
 * スクリプトプロパティに次を設定してください。
 *   CALENDAR_WEBHOOK_SECRET: ローカル設定ツールが表示する秘密キー
 */

const AI_TASK_LIST_NAME = 'AI Inbox';
const TASKS_SIGNAL_URL_PROPERTY = 'TASKS_SIGNAL_URL';
const TASKS_SIGNAL_STATE_PROPERTY = 'TASKS_SIGNAL_STATE';
const TASKS_SIGNAL_LAST_SENT_PROPERTY = 'TASKS_SIGNAL_LAST_SENT';
const TASKS_SIGNAL_RETRY_MS = 5 * 60 * 1000;
const TASKS_LAST_POLL_PROPERTY = 'TASKS_LAST_POLL';
const TASKS_POLL_STATS_PROPERTY = 'TASKS_POLL_STATS';
const TASKS_POLL_COOLDOWN_MS = 15 * 1000;
const TASKS_POLL_TRIGGER_COUNT = 2;
const TASKS_TRIGGER_INSTALL_STAGGER_MS = 15 * 1000;
const INGEST_REQUEST_PROPERTY_PREFIX = 'INGEST_';
const INGEST_DEDUPE_MS = 10 * 60 * 1000;
const INGEST_TITLE_MAX = 40;
const AI_TASK_LIST_ID_PROPERTY = 'AI_TASK_LIST_ID';
const SIGNAL_MAX_ATTEMPTS = 3;
const SIGNAL_RETRY_DELAY_MS = 600;

function doPost(e) {
  let lock = null;
  try {
    const request = JSON.parse((e.postData && e.postData.contents) || '{}');
    const expected = PropertiesService.getScriptProperties()
      .getProperty('CALENDAR_WEBHOOK_SECRET');

    if (!expected || !constantTimeEquals_(request.secret || '', expected)) {
      return jsonResponse_({ok: false, error: '認証に失敗しました'});
    }

    // 設定ツールが接続とカレンダー権限を確認するために使用します。
    if (request.action === 'ping') {
      CalendarApp.getDefaultCalendar().getId();
      return jsonResponse_({ok: true, status: 'ready'});
    }

    if (request.action === 'upcoming') {
      const rangeStart = new Date(request.range_start);
      const rangeEnd = new Date(request.range_end);
      if (
        isNaN(rangeStart.getTime()) ||
        isNaN(rangeEnd.getTime()) ||
        rangeEnd.getTime() <= rangeStart.getTime()
      ) {
        throw new Error('予定の取得範囲を解釈できません');
      }
      const timeZone = Session.getScriptTimeZone() || 'Asia/Tokyo';
      const events = CalendarApp.getDefaultCalendar()
        .getEvents(rangeStart, rangeEnd)
        .map(function(event) {
          const allDay = event.isAllDayEvent();
          return {
            id: event.getId(),
            title: event.getTitle(),
            all_day: allDay,
            start: allDay
              ? Utilities.formatDate(event.getAllDayStartDate(), timeZone, 'yyyy-MM-dd')
              : event.getStartTime().toISOString(),
            end: allDay
              ? Utilities.formatDate(event.getAllDayEndDate(), timeZone, 'yyyy-MM-dd')
              : event.getEndTime().toISOString(),
            location: event.getLocation() || '',
          };
        })
        .sort(function(left, right) {
          return String(left.start).localeCompare(String(right.start));
        });
      return jsonResponse_({ok: true, events: events});
    }

    if (request.action === 'tasks_list') {
      const tasks = listPendingAiTasksAcrossLists_();
      return jsonResponse_({ok: true, tasks: tasks});
    }

    if (request.action === 'tasks_signal_configure') {
      const signalUrl = String(request.signal_url || '').trim();
      if (!/^https:\/\//i.test(signalUrl)) {
        throw new Error('起動信号URLはHTTPSで指定してください');
      }
      PropertiesService.getScriptProperties()
        .setProperty(TASKS_SIGNAL_URL_PROPERTY, signalUrl.replace(/\/+$/, ''));
      installTaskPollingTrigger_();
      return jsonResponse_({ok: true, status: 'configured'});
    }

    if (request.action === 'tasks_polling_status') {
      const triggers = ScriptApp.getProjectTriggers().filter(function(trigger) {
        return trigger.getHandlerFunction() === 'pollTasksAndSignal';
      });
      const properties = PropertiesService.getScriptProperties();
      let stats = {};
      try {
        stats = JSON.parse(
          properties.getProperty(TASKS_POLL_STATS_PROPERTY) || '{}'
        );
      } catch (_error) {
        stats = {};
      }
      return jsonResponse_({
        ok: true,
        trigger_count: triggers.length,
        cooldown_ms: TASKS_POLL_COOLDOWN_MS,
        signal_url_configured: Boolean(
          properties.getProperty(TASKS_SIGNAL_URL_PROPERTY)
        ),
        stats: stats,
      });
    }

    if (request.action === 'tasks_complete') {
      const taskId = String(request.task_id || '').trim();
      if (!taskId) throw new Error('完了にするタスクIDがありません');
      const requestedListId = String(request.task_list_id || '').trim();
      const taskListId = requestedListId || ensureAiInbox_().id;
      const task = Tasks.Tasks.get(taskListId, taskId);
      if (task.status !== 'completed') {
        Tasks.Tasks.patch(
          {status: 'completed', completed: new Date().toISOString()},
          taskListId,
          taskId
        );
      }
      return jsonResponse_({ok: true, task_id: taskId, status: 'completed'});
    }

    // 音声入力アプリ（Automateなど）から直接1件を取り込みます。
    // Gemini を経由しないので原文がそのまま届き、取り込んだ時点で
    // ローカルへ合図を送るため、1分ポーリングの待ち時間が発生しません。
    if (request.action === 'tasks_ingest') {
      const rawText = String(request.text || '').trim();
      if (!rawText) {
        return jsonResponse_({ok: false, error: '本文がありません'});
      }

      const properties = PropertiesService.getScriptProperties();
      const requestId = String(request.request_id || '').trim();
      let dedupeKey = null;
      if (requestId) {
        // A retry from a flaky phone connection must not create a second task.
        dedupeKey = INGEST_REQUEST_PROPERTY_PREFIX + sha256Hex_(requestId);
        const previous = properties.getProperty(dedupeKey);
        if (previous) {
          const record = JSON.parse(previous);
          if (Date.now() - record.at < INGEST_DEDUPE_MS) {
            return jsonResponse_({
              ok: true,
              task_id: record.task_id,
              duplicate: true,
            });
          }
        }
      }

      const taskListId = aiInboxListId_(properties);
      const title = String(request.title || '').trim()
        || rawText.slice(0, INGEST_TITLE_MAX);
      const created = Tasks.Tasks.insert({title: title, notes: rawText}, taskListId);

      if (dedupeKey) {
        properties.setProperty(
          dedupeKey,
          JSON.stringify({task_id: created.id, at: Date.now()})
        );
      }

      // Signal immediately instead of waiting for the next poll. The task is
      // already stored, so a signal failure only costs latency, never data.
      let signal = {ok: false, status: 0, error: 'not_attempted'};
      try {
        signal = sendTasksSignalDirect_(properties);
      } catch (signalError) {
        signal = {ok: false, status: -1, error: String(signalError).slice(0, 200)};
      }
      return jsonResponse_({
        ok: true,
        task_id: created.id,
        task_list_id: taskListId,
        signalled: signal.ok,
        signal_status: signal.status,
        signal_error: signal.error || null,
        signal_attempts: signal.attempts || 1,
        duplicate: false,
      });
    }

    if (request.action === 'calendar_delete') {
      const targetNoteId = String(request.source_note_id || '').trim();
      const properties = PropertiesService.getScriptProperties();
      const jobKey = 'event_' + sha256Hex_(targetNoteId);
      const eventId = String(request.event_id || '') || properties.getProperty(jobKey);
      if (!eventId) {
        return jsonResponse_({ok: true, deleted: false, reason: 'not_found'});
      }
      try {
        const event = CalendarApp.getDefaultCalendar().getEventById(eventId);
        if (event) event.deleteEvent();
      } catch (deleteError) {
        // An event the user already removed by hand is still a success here.
        if (!String(deleteError).match(/not found|見つかりません/i)) throw deleteError;
      }
      properties.deleteProperty(jobKey);
      return jsonResponse_({ok: true, deleted: true, event_id: eventId});
    }

    const noteId = String(request.source_note_id || '').trim();
    const title = String(request.event_title || '').trim();
    if (!noteId || !title || !request.event_start) {
      return jsonResponse_({ok: false, error: '必須項目が不足しています'});
    }

    lock = LockService.getScriptLock();
    if (!lock.tryLock(30000)) {
      throw new Error('別の予定を処理中です。後でもう一度実行してください');
    }

    const properties = PropertiesService.getScriptProperties();
    const jobKey = 'event_' + sha256Hex_(noteId);
    const existingEventId = properties.getProperty(jobKey);
    if (existingEventId) {
      return jsonResponse_({
        ok: true,
        event_id: existingEventId,
        duplicate: true,
      });
    }

    const calendar = CalendarApp.getDefaultCalendar();
    const options = {};
    if (request.location) options.location = String(request.location);
    if (request.description) options.description = String(request.description);

    let event;
    if (request.all_day) {
      const start = parseDateOnly_(request.event_start);
      const end = request.event_end
        ? parseDateOnly_(request.event_end)
        : addDays_(start, 1);
      if (end.getTime() <= start.getTime()) {
        throw new Error('終了日は開始日より後である必要があります');
      }
      event = calendar.createAllDayEvent(title, start, end, options);
    } else {
      const start = new Date(request.event_start);
      const end = request.event_end
        ? new Date(request.event_end)
        : new Date(start.getTime() + 60 * 60 * 1000);
      if (isNaN(start.getTime()) || isNaN(end.getTime())) {
        throw new Error('予定日時を解釈できません');
      }
      if (end.getTime() <= start.getTime()) {
        throw new Error('終了時刻は開始時刻より後である必要があります');
      }
      event = calendar.createEvent(title, start, end, options);
    }

    event.setTag('sourceNoteId', noteId);
    properties.setProperty(jobKey, event.getId());
    return jsonResponse_({
      ok: true,
      event_id: event.getId(),
      duplicate: false,
    });
  } catch (error) {
    return jsonResponse_({ok: false, error: String(error.message || error)});
  } finally {
    if (lock && lock.hasLock()) lock.releaseLock();
  }
}

// エディタ上で一度実行し、カレンダー権限を許可してください。
function authorizeCalendar() {
  const calendar = CalendarApp.getDefaultCalendar();
  console.log('認証済みカレンダー: ' + calendar.getName());
}

// Tasks APIを有効化した後に一度実行してください。
// 「AI Inbox」リストがなければ作成し、Tasks権限を許可します。
function authorizeTasksAndCreateInbox() {
  const taskList = ensureAiInbox_();
  console.log('AI Inbox準備完了: ' + taskList.title);
}

/**
 * Google側でAI Inboxを監視し、未処理タスクがあればローカルPCへ起動合図を送ります。
 * この関数は1分トリガーから呼ばれ、検索やAI処理は一切行いません。
 */
function pollTasksAndSignal() {
  const startedAt = Date.now();
  const properties = PropertiesService.getScriptProperties();
  if (!properties.getProperty(TASKS_SIGNAL_URL_PROPERTY)) return;

  const lock = LockService.getScriptLock();
  if (!lock.tryLock(100)) return;
  let didPoll = false;
  try {
    const now = Date.now();
    const lastPoll = Number(
      properties.getProperty(TASKS_LAST_POLL_PROPERTY) || '0'
    );
    if (now - lastPoll < TASKS_POLL_COOLDOWN_MS) return;
    properties.setProperty(TASKS_LAST_POLL_PROPERTY, String(now));
    didPoll = true;
    pollTasksAndSignalUnlocked_();
  } finally {
    if (didPoll) {
      recordTasksPollStats_(properties, Date.now() - startedAt);
    }
    lock.releaseLock();
  }
}

/**
 * 取り込み直後に使う軽量シグナル。
 * pollTasksAndSignalUnlocked_ と違い、全タスクリストの再走査を行いません。
 * 新しいタスクを作った直後なので「未処理がある」ことは自明で、
 * 走査を挟むと応答が数十秒に伸びて呼び出し側がタイムアウトします。
 */
function sendTasksSignalDirect_(properties) {
  const signalUrl = properties.getProperty(TASKS_SIGNAL_URL_PROPERTY);
  if (!signalUrl) {
    return {ok: false, status: 0, error: 'signal_url_not_configured'};
  }

  // ntfy.sh throttles by source IP, and Apps Script egress addresses are
  // shared with every other script, so a rejection here is expected to be
  // transient. Retry briefly rather than falling back to the slow poll.
  let status = 0;
  let lastError = '';
  for (let attempt = 0; attempt < SIGNAL_MAX_ATTEMPTS; attempt++) {
    try {
      const response = UrlFetchApp.fetch(signalUrl, {
        method: 'post',
        contentType: 'text/plain; charset=utf-8',
        payload: 'tasks_changed',
        headers: {
          'Title': 'AI Inbox signal',
          'Priority': '3',
        },
        muteHttpExceptions: true,
      });
      status = response.getResponseCode();
      if (status >= 200 && status < 300) {
        // 状態ハッシュは意図的に更新しません。算出には避けたい全走査が必要で、
        // 次回ポーリングが一度だけ重複合図を送るほうが安上がりです。
        // 重複合図は処理済みタスクを再走査するだけで副作用はありません。
        properties.setProperty(TASKS_SIGNAL_LAST_SENT_PROPERTY, String(Date.now()));
        return {ok: true, status: status, attempts: attempt + 1};
      }
      lastError = String(response.getContentText() || '').slice(0, 200);
    } catch (fetchError) {
      status = -1;
      lastError = String(fetchError).slice(0, 200);
    }
    if (attempt + 1 < SIGNAL_MAX_ATTEMPTS) {
      Utilities.sleep(SIGNAL_RETRY_DELAY_MS * (attempt + 1));
    }
  }
  return {
    ok: false,
    status: status,
    error: lastError || 'signal_failed',
    attempts: SIGNAL_MAX_ATTEMPTS,
  };
}

/** AI Inbox のリストIDをキャッシュし、毎回の全リスト走査を避けます。 */
function aiInboxListId_(properties) {
  const cached = properties.getProperty(AI_TASK_LIST_ID_PROPERTY);
  if (cached) return cached;
  const taskList = ensureAiInbox_();
  properties.setProperty(AI_TASK_LIST_ID_PROPERTY, taskList.id);
  return taskList.id;
}

function pollTasksAndSignalUnlocked_() {
  const properties = PropertiesService.getScriptProperties();
  const signalUrl = properties.getProperty(TASKS_SIGNAL_URL_PROPERTY);
  if (!signalUrl) return;

  const tasks = listPendingAiTasksAcrossLists_();
  if (!tasks.length) {
    properties.deleteProperty(TASKS_SIGNAL_STATE_PROPERTY);
    properties.deleteProperty(TASKS_SIGNAL_LAST_SENT_PROPERTY);
    return;
  }

  const state = sha256Hex_(tasks.map(function(task) {
    return task.task_list_id + ':' + task.id + ':' + task.updated;
  }).sort().join('|'));
  const previousState = properties.getProperty(TASKS_SIGNAL_STATE_PROPERTY) || '';
  const lastSent = Number(
    properties.getProperty(TASKS_SIGNAL_LAST_SENT_PROPERTY) || '0'
  );
  const now = Date.now();

  // 新規・更新時はすぐ送信。受信漏れに備え、未処理の間は5分ごとに再送します。
  if (state === previousState && now - lastSent < TASKS_SIGNAL_RETRY_MS) return;

  const response = UrlFetchApp.fetch(signalUrl, {
    method: 'post',
    contentType: 'text/plain; charset=utf-8',
    payload: 'tasks_changed',
    headers: {
      'Title': 'AI Inbox signal',
      'Priority': '3',
    },
    muteHttpExceptions: true,
  });
  const status = response.getResponseCode();
  if (status < 200 || status >= 300) {
    throw new Error('起動信号の送信に失敗しました: HTTP ' + status);
  }
  properties.setProperty(TASKS_SIGNAL_STATE_PROPERTY, state);
  properties.setProperty(TASKS_SIGNAL_LAST_SENT_PROPERTY, String(now));
}

function recordTasksPollStats_(properties, elapsedMs) {
  const timeZone = Session.getScriptTimeZone() || 'Asia/Tokyo';
  const date = Utilities.formatDate(new Date(), timeZone, 'yyyy-MM-dd');
  let stats = {};
  try {
    stats = JSON.parse(properties.getProperty(TASKS_POLL_STATS_PROPERTY) || '{}');
  } catch (_error) {
    stats = {};
  }
  if (stats.date !== date) {
    stats = {date: date, count: 0, runtime_ms: 0, max_runtime_ms: 0};
  }
  stats.count += 1;
  stats.runtime_ms += elapsedMs;
  stats.max_runtime_ms = Math.max(stats.max_runtime_ms || 0, elapsedMs);
  properties.setProperty(TASKS_POLL_STATS_PROPERTY, JSON.stringify(stats));
}

/** エディタから一度実行すれば、権限確認と1分トリガー作成を行えます。 */
function authorizeTasksPolling() {
  ensureAiInbox_();
  UrlFetchApp.getRequest('https://ntfy.sh/');
  installTaskPollingTrigger_();
  console.log('Google Tasksの1分監視トリガーを作成しました');
}

function installTaskPollingTrigger_() {
  ScriptApp.getProjectTriggers().forEach(function(trigger) {
    if (trigger.getHandlerFunction() === 'pollTasksAndSignal') {
      ScriptApp.deleteTrigger(trigger);
    }
  });
  for (let i = 0; i < TASKS_POLL_TRIGGER_COUNT; i++) {
    ScriptApp.newTrigger('pollTasksAndSignal')
      .timeBased()
      .everyMinutes(1)
      .create();
    if (i + 1 < TASKS_POLL_TRIGGER_COUNT) {
      // Installation-only delay; recurring polling never sleeps.
      Utilities.sleep(TASKS_TRIGGER_INSTALL_STAGGER_MS);
    }
  }
}

function listPendingAiTasks_(taskListId) {
  let pageToken = null;
  const tasks = [];
  do {
    const response = Tasks.Tasks.list(taskListId, {
      maxResults: 100,
      pageToken: pageToken,
      showCompleted: false,
      showDeleted: false,
      showHidden: false,
    });
    (response.items || []).forEach(function(task) {
      if (task.status === 'needsAction' && !task.deleted && !task.hidden) {
        tasks.push({
          id: task.id,
          title: task.title || '',
          notes: task.notes || '',
          updated: task.updated || '',
        });
      }
    });
    pageToken = response.nextPageToken || null;
  } while (pageToken);
  return tasks;
}

function listTaskLists_() {
  let pageToken = null;
  const taskLists = [];
  do {
    const response = Tasks.Tasklists.list({
      maxResults: 100,
      pageToken: pageToken,
    });
    Array.prototype.push.apply(taskLists, response.items || []);
    pageToken = response.nextPageToken || null;
  } while (pageToken);
  return taskLists;
}

function isExplicitAiTask_(title, notes) {
  const titleMarker = /^\s*\[\s*AI\s*\]/i.test(String(title || ''));
  const body = String(notes || '').trim();
  const markerPattern = '(?:(?:AI|ＡＩ)\\s*(?:メ[モムマ]|め[もむま])|(?:あい|アイ|えーあい|エーアイ|えいあい|エイアイ)\\s*(?:メ[モムマ]|め[もむま]))';
  const startMarker = new RegExp(
    '^[\\s、。,:：・\\-]*' + markerPattern,
    'i'
  ).test(body);
  const bodyWithoutTransport = body.replace(
    /[\s、。,:：・\-]*(?:(?:Google|グーグル|ぐーぐる)\s*)?(?:Tasks?|タスクス?|タスク|たすくす?|たすく).{0,18}?(?:保存|ほぞん|追加|ついか|登録|とうろく|なんとか).{0,10}$/i,
    ''
  ).trim();
  const endMarker = new RegExp(
    markerPattern + '[\\s、。,:：・\\-]*$',
    'i'
  ).test(bodyWithoutTransport);
  return titleMarker || (startMarker && endMarker);
}

function listPendingAiTasksAcrossLists_() {
  const tasks = [];
  listTaskLists_().forEach(function(taskList) {
    const dedicatedInbox = taskList.title === AI_TASK_LIST_NAME;
    listPendingAiTasks_(taskList.id).forEach(function(task) {
      if (!dedicatedInbox && !isExplicitAiTask_(task.title, task.notes)) return;
      task.task_list_id = taskList.id;
      task.task_list_title = taskList.title || '';
      task.dedicated_inbox = dedicatedInbox;
      tasks.push(task);
    });
  });
  return tasks;
}

function ensureAiInbox_() {
  let pageToken = null;
  do {
    const response = Tasks.Tasklists.list({
      maxResults: 100,
      pageToken: pageToken,
    });
    const lists = response.items || [];
    for (let i = 0; i < lists.length; i++) {
      if (lists[i].title === AI_TASK_LIST_NAME) return lists[i];
    }
    pageToken = response.nextPageToken || null;
  } while (pageToken);
  return Tasks.Tasklists.insert({title: AI_TASK_LIST_NAME});
}

function parseDateOnly_(value) {
  const match = String(value).match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!match) throw new Error('日付を解釈できません');
  return new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
}

function addDays_(date, days) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate() + days);
}

function sha256Hex_(value) {
  return Utilities.computeDigest(
    Utilities.DigestAlgorithm.SHA_256,
    value,
    Utilities.Charset.UTF_8
  ).map(function(byte) {
    const unsigned = byte < 0 ? byte + 256 : byte;
    return unsigned.toString(16).padStart(2, '0');
  }).join('');
}

function constantTimeEquals_(left, right) {
  left = String(left);
  right = String(right);
  let difference = left.length ^ right.length;
  const length = Math.max(left.length, right.length);
  for (let i = 0; i < length; i++) {
    difference |= (left.charCodeAt(i) || 0) ^ (right.charCodeAt(i) || 0);
  }
  return difference === 0;
}

function jsonResponse_(value) {
  return ContentService
    .createTextOutput(JSON.stringify(value))
    .setMimeType(ContentService.MimeType.JSON);
}
