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
const TASKS_POLL_TRIGGER_COUNT = 1;
const TASKS_POLL_INTERVAL_MINUTES = 1;
const TASKS_POLL_INTERVAL_PROPERTY = 'TASKS_POLL_INTERVAL_MINUTES';
const TASKS_POLL_COUNT_PROPERTY = 'TASKS_POLL_TRIGGER_COUNT';
const ALLOWED_POLL_INTERVALS = [1, 5, 10, 15, 30];
// 合図のfetchがハングしたとき、次のトリガーが即座に再試行して
// 実行が積み上がるのを防ぎます。
const TASKS_SIGNAL_ATTEMPT_PROPERTY = 'TASKS_SIGNAL_LAST_ATTEMPT';
const TASKS_SIGNAL_ATTEMPT_COOLDOWN_MS = 90 * 1000;
const TASKS_TRIGGER_INSTALL_STAGGER_MS = 15 * 1000;
// 1リクエスト1プロパティだと件数が際限なく増え、Propertiesの500KB上限に
// 当たった時点で「登録後の記録」が失敗して二重登録を招きます。
// 台帳を1プロパティにまとめ、件数と期限の両方で上限を掛けます。
const INGEST_LEDGER_PROPERTY = 'INGEST_LEDGER';
const INGEST_DEDUPE_MS = 24 * 60 * 60 * 1000;
const INGEST_LEDGER_MAX_ENTRIES = 200;
const INGEST_LOCK_WAIT_MS = 8 * 1000;
const INGEST_TITLE_MAX = 40;
const AI_TASK_LIST_ID_PROPERTY = 'AI_TASK_LIST_ID';
const TASK_LISTS_CACHE_PROPERTY = 'TASK_LISTS_CACHE';
const TASK_LISTS_CACHE_MS = 30 * 60 * 1000;

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

    // ポーリング頻度を再デプロイなしで変更します。合図は呼び出し側が送るため、
    // ポーリングはGemini経由の保険として頻度を落とせます。
    if (request.action === 'tasks_polling_configure') {
      const properties = PropertiesService.getScriptProperties();
      if (request.interval_minutes !== undefined) {
        const interval = Number(request.interval_minutes);
        if (ALLOWED_POLL_INTERVALS.indexOf(interval) === -1) {
          throw new Error(
            '間隔は ' + ALLOWED_POLL_INTERVALS.join('/') + ' 分のいずれかです'
          );
        }
        properties.setProperty(TASKS_POLL_INTERVAL_PROPERTY, String(interval));
      }
      if (request.trigger_count !== undefined) {
        const count = Number(request.trigger_count);
        if (!(count >= 0 && count <= 6)) {
          throw new Error('トリガー本数は0〜6で指定してください');
        }
        properties.setProperty(TASKS_POLL_COUNT_PROPERTY, String(count));
      }
      const applied = installTaskPollingTrigger_();
      return jsonResponse_({
        ok: true,
        interval_minutes: applied.interval,
        trigger_count: applied.count,
      });
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
        interval_minutes: pollingSettings_().interval,
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

      // The dedupe check, the insert and the record have to be one unit.
      // Without the lock two retries both read "no record", both insert, and
      // both answer duplicate=false - which is exactly what a phone does when
      // the first response is slow.
      // Well under Google's frontend cutoff. Waiting longer does not produce a
      // JSON answer, it produces an HTML error page, which the caller cannot
      // tell apart from a protocol change. Failing fast lets the client retry
      // with the same request_id, which the ledger makes safe.
      lock = LockService.getScriptLock();
      if (!lock.tryLock(INGEST_LOCK_WAIT_MS)) {
        return jsonResponse_({
          ok: false,
          error: '取り込みが混み合っています。同じrequest_idで再送してください',
          retryable: true,
        });
      }

      const properties = PropertiesService.getScriptProperties();
      const requestId = String(request.request_id || '').trim();
      const ledger = readIngestLedger_(properties);

      if (requestId) {
        const previous = ledger.entries[requestId];
        if (previous && previous.task_id) {
          return jsonResponse_({
            ok: true,
            task_id: previous.task_id,
            task_list_id: previous.list_id || null,
            duplicate: true,
          });
        }
        if (previous && !previous.task_id) {
          // A previous attempt inserted but died before recording the id.
          // Adopt the matching task instead of creating a second one.
          const adopted = findRecentTaskByContent_(
            previous.list_id || aiInboxListId_(properties), rawText
          );
          if (adopted) {
            ledger.entries[requestId] = {
              task_id: adopted, list_id: previous.list_id, at: Date.now(),
            };
            writeIngestLedger_(properties, ledger);
            return jsonResponse_({
              ok: true, task_id: adopted, duplicate: true, recovered: true,
            });
          }
        }
      }

      const taskListId = aiInboxListId_(properties);
      if (requestId) {
        // Reserve before inserting, so a crash between insert and record is
        // recoverable rather than invisible.
        ledger.entries[requestId] = {task_id: null, list_id: taskListId, at: Date.now()};
        writeIngestLedger_(properties, ledger);
      }

      const title = String(request.title || '').trim()
        || rawText.slice(0, INGEST_TITLE_MAX);
      const created = Tasks.Tasks.insert({title: title, notes: rawText}, taskListId);

      if (requestId) {
        ledger.entries[requestId] = {
          task_id: created.id, list_id: taskListId, at: Date.now(),
        };
        writeIngestLedger_(properties, ledger);
      }

      // 既定では合図を送りません。Google の送信元から ntfy への接続は
      // 断続的に数十秒ハングし、その待ち時間が呼び出し側のタイムアウトに
      // なります。合図はスマホ／PC から直接送るほうが速く確実です。
      // ntfy へ到達できない呼び出し元だけ signal:true を指定してください。
      let signal = {ok: false, status: 0, error: 'skipped_by_default'};
      if (request.signal === true) {
        try {
          signal = sendTasksSignalDirect_(properties);
        } catch (signalError) {
          signal = {ok: false, status: -1, error: String(signalError).slice(0, 200)};
        }
      }
      return jsonResponse_({
        ok: true,
        task_id: created.id,
        task_list_id: taskListId,
        signalled: signal.ok,
        signal_status: signal.status,
        signal_error: signal.error || null,
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
    if (!lock.tryLock(INGEST_LOCK_WAIT_MS)) {
      // Same reason as the ingest branch: a long wait returns HTML, not JSON.
      // The worker keeps the job queued and will come back to it.
      return jsonResponse_({
        ok: false,
        error: '別の処理と競合しました。後で再試行されます',
        retryable: true,
      });
    }

    const properties = PropertiesService.getScriptProperties();
    const jobKey = 'event_' + sha256Hex_(noteId);
    const existingEventId = properties.getProperty(jobKey);
    const calendar = CalendarApp.getDefaultCalendar();

    // An existing key means either a retry of the same request or a
    // correction. Returning the stored id for both is what let
    // "さっきの予定を10時に" succeed locally while Google kept the old time.
    if (existingEventId) {
      let existing = null;
      try {
        existing = calendar.getEventById(existingEventId);
      } catch (lookupError) {
        existing = null;
      }
      if (existing) {
        const changed = applyEventFields_(existing, request, title);
        return jsonResponse_({
          ok: true,
          event_id: existingEventId,
          duplicate: !changed,
          updated: changed,
        });
      }
      // Removed by hand on the Google side. Fall through and create it again.
      properties.deleteProperty(jobKey);
    }

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

/** 取り込み済みリクエストの台帳を読み、期限切れを落とします。 */
function readIngestLedger_(properties) {
  let ledger = {entries: {}};
  try {
    const raw = properties.getProperty(INGEST_LEDGER_PROPERTY);
    if (raw) ledger = JSON.parse(raw);
  } catch (parseError) {
    ledger = {entries: {}};
  }
  if (!ledger.entries) ledger.entries = {};

  const cutoff = Date.now() - INGEST_DEDUPE_MS;
  Object.keys(ledger.entries).forEach(function(key) {
    const entry = ledger.entries[key];
    if (!entry || !entry.at || entry.at < cutoff) delete ledger.entries[key];
  });
  return ledger;
}

/** 台帳を保存します。件数が上限を超えたら古い順に落とします。 */
function writeIngestLedger_(properties, ledger) {
  const keys = Object.keys(ledger.entries);
  if (keys.length > INGEST_LEDGER_MAX_ENTRIES) {
    keys.sort(function(a, b) {
      return (ledger.entries[a].at || 0) - (ledger.entries[b].at || 0);
    });
    keys.slice(0, keys.length - INGEST_LEDGER_MAX_ENTRIES).forEach(function(key) {
      delete ledger.entries[key];
    });
  }
  properties.setProperty(INGEST_LEDGER_PROPERTY, JSON.stringify(ledger));
}

/**
 * 予約済みだがIDを記録できなかったリクエストの実体を探します。
 * 挿入後・記録前にスクリプトが落ちた場合の回収経路です。
 */
function findRecentTaskByContent_(taskListId, rawText) {
  if (!taskListId) return null;
  const cutoff = new Date(Date.now() - INGEST_DEDUPE_MS).toISOString();
  let pageToken = null;
  do {
    const response = Tasks.Tasks.list(taskListId, {
      maxResults: 100,
      showCompleted: false,
      updatedMin: cutoff,
      pageToken: pageToken,
    });
    const items = response.items || [];
    for (let i = 0; i < items.length; i++) {
      if (String(items[i].notes || '') === rawText) return items[i].id;
    }
    pageToken = response.nextPageToken || null;
  } while (pageToken);
  return null;
}

/**
 * 既存イベントを要求どおりの内容へ揃えます。
 * 実際に変更した場合だけ true を返すので、呼び出し側は
 * 「再送による重複」と「訂正による更新」を区別できます。
 */
function applyEventFields_(event, request, title) {
  let changed = false;

  if (event.getTitle() !== title) {
    event.setTitle(title);
    changed = true;
  }

  if (request.all_day) {
    const start = parseDateOnly_(request.event_start);
    const end = request.event_end
      ? parseDateOnly_(request.event_end)
      : addDays_(start, 1);
    if (end.getTime() <= start.getTime()) {
      throw new Error('終了日は開始日より後である必要があります');
    }
    if (
      !event.isAllDayEvent()
      || event.getAllDayStartDate().getTime() !== start.getTime()
      || event.getAllDayEndDate().getTime() !== end.getTime()
    ) {
      event.setAllDayDates(start, end);
      changed = true;
    }
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
    if (
      event.isAllDayEvent()
      || event.getStartTime().getTime() !== start.getTime()
      || event.getEndTime().getTime() !== end.getTime()
    ) {
      event.setTime(start, end);
      changed = true;
    }
  }

  const location = request.location ? String(request.location) : '';
  if (event.getLocation() !== location) {
    event.setLocation(location);
    changed = true;
  }
  const description = request.description ? String(request.description) : '';
  if (event.getDescription() !== description) {
    event.setDescription(description);
    changed = true;
  }
  return changed;
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

  // The lock covers only the claim. Holding it across the listing and the
  // ntfy fetch made every poll block the web app on the same script lock, so
  // an ingest arriving mid-poll waited past Google's frontend timeout and the
  // caller got an HTML error page with HTTP 200 instead of JSON.
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(100)) return;
  let claimed = false;
  try {
    const now = Date.now();
    const lastPoll = Number(
      properties.getProperty(TASKS_LAST_POLL_PROPERTY) || '0'
    );
    if (now - lastPoll >= TASKS_POLL_COOLDOWN_MS) {
      properties.setProperty(TASKS_LAST_POLL_PROPERTY, String(now));
      claimed = true;
    }
  } finally {
    lock.releaseLock();
  }
  if (!claimed) return;

  try {
    pollTasksAndSignalUnlocked_();
  } finally {
    recordTasksPollStats_(properties, Date.now() - startedAt);
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
    const status = response.getResponseCode();
    if (status >= 200 && status < 300) {
      // 状態ハッシュは意図的に更新しません。算出には全走査が必要で、
      // 次回ポーリングが一度だけ重複合図を送るほうが安上がりです。
      properties.setProperty(TASKS_SIGNAL_LAST_SENT_PROPERTY, String(Date.now()));
      return {ok: true, status: status};
    }
    return {
      ok: false,
      status: status,
      error: String(response.getContentText() || '').slice(0, 200),
    };
  } catch (fetchError) {
    return {ok: false, status: -1, error: String(fetchError).slice(0, 200)};
  }
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

  // Googleの送信元からntfyへのfetchは数十秒ハングすることがあります。
  // 記録を試行の「前」に置かないと、ハング中に次のトリガーが同じfetchを
  // 積み増し、スクリプト全体が飽和してping応答すら返らなくなります。
  const lastAttempt = Number(
    properties.getProperty(TASKS_SIGNAL_ATTEMPT_PROPERTY) || '0'
  );
  if (now - lastAttempt < TASKS_SIGNAL_ATTEMPT_COOLDOWN_MS) return;
  properties.setProperty(TASKS_SIGNAL_ATTEMPT_PROPERTY, String(now));

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

function pollingSettings_() {
  const properties = PropertiesService.getScriptProperties();
  let interval = Number(
    properties.getProperty(TASKS_POLL_INTERVAL_PROPERTY)
      || TASKS_POLL_INTERVAL_MINUTES
  );
  if (ALLOWED_POLL_INTERVALS.indexOf(interval) === -1) {
    interval = TASKS_POLL_INTERVAL_MINUTES;
  }
  let count = Number(
    properties.getProperty(TASKS_POLL_COUNT_PROPERTY) || TASKS_POLL_TRIGGER_COUNT
  );
  if (!(count >= 0 && count <= 6)) count = TASKS_POLL_TRIGGER_COUNT;
  return {interval: interval, count: count};
}

function installTaskPollingTrigger_() {
  ScriptApp.getProjectTriggers().forEach(function(trigger) {
    if (trigger.getHandlerFunction() === 'pollTasksAndSignal') {
      ScriptApp.deleteTrigger(trigger);
    }
  });
  const settings = pollingSettings_();
  for (let i = 0; i < settings.count; i++) {
    ScriptApp.newTrigger('pollTasksAndSignal')
      .timeBased()
      .everyMinutes(settings.interval)
      .create();
    if (i + 1 < settings.count) {
      // Installation-only delay; recurring polling never sleeps.
      Utilities.sleep(TASKS_TRIGGER_INSTALL_STAGGER_MS);
    }
  }
  return settings;
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
  // Task lists change rarely, and the poll runs on a timer, so re-enumerating
  // them every time is pure daily-quota spend.
  const properties = PropertiesService.getScriptProperties();
  try {
    const cached = JSON.parse(
      properties.getProperty(TASK_LISTS_CACHE_PROPERTY) || 'null'
    );
    if (cached && Date.now() - cached.at < TASK_LISTS_CACHE_MS && cached.items) {
      return cached.items;
    }
  } catch (parseError) {
    // Fall through and refresh.
  }

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

  properties.setProperty(
    TASK_LISTS_CACHE_PROPERTY,
    JSON.stringify({
      at: Date.now(),
      items: taskLists.map(function(list) {
        return {id: list.id, title: list.title};
      }),
    })
  );
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
