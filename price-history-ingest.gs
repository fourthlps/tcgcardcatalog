/**
 * Price History Raw ingest endpoint.
 *
 * Standalone Apps Script project ("TCG Price History Ingest"), opens the
 * TCGPROJECTONEPIECE spreadsheet by ID. Deployed as a Web App so the
 * GitHub Action running update_prices.py can POST daily price rows here
 * without needing a Google service account.
 *
 * Append-only by design: every write goes to getLastRow()+1, so a bad
 * request can never overwrite a previous day's history. Rows are written
 * exactly as sent -- this script does not invent or adjust prices itself;
 * that "real price or skip" decision is made in update_prices.py.
 *
 * Duplicate-date protection (added 2026-06-25): update_prices.py calls this
 * endpoint with {action: "checkDate", date: "YYYY-MM-DD"} once per run,
 * before sending any actual rows, to ask "does Price History Raw already
 * have a snapshot for this date?". This is read-only -- it never writes --
 * so the actual stop-the-run decision and the exact-N-rows append both stay
 * single-responsibility: this script answers "does it exist", the Python
 * script decides what to do about it.
 *
 * Copy of this file lives at TCG-Platform/price-history-ingest.gs for
 * reference. The deployed copy lives in the "TCG Price History Ingest"
 * Apps Script project at script.google.com -- that is the one that
 * actually runs.
 */

var SHEET_NAME = 'Price History Raw';
var RUN_LOG_SHEET_NAME = 'Run Log';
var WIDE_SHEET_NAME = 'Price History Wide';
var SPREADSHEET_ID = '17BoWE7nAvAvjzwmDIaJQFce1MtvflonEKyxCVhq9EOQ'; // TCGPROJECTONEPIECE

// Price History Wide: fixed metadata columns before the date columns begin.
// NEVER reorder these -- column positions are referenced by index in the code.
//   Col 1 (A): No.
//   Col 2 (B): Game
//   Col 3 (C): Card ID
//   Col 4 (D): Card Name
//   Col 5 (E): Set
//   Col 6 (F): Rarity
//   Col 7 (G): Currency
//   Col 8+ (H+): Date columns  (YYYY-MM-DD, one column per day, appended right)
var WIDE_META_COLS = 7;
var WIDE_HEADERS = ['No.', 'Game', 'Card ID', 'Card Name', 'Set', 'Rarity', 'Currency'];

// Columns for the Run Log sheet, in order. Any new column must be appended
// here AND to RUN_LOG_HEADERS below -- never insert in the middle, as that
// would shift existing data.
var RUN_LOG_HEADERS = [
  'run_date',
  'run_timestamp_utc',
  'status',
  'source',
  'total_sets',
  'successful_sets',
  'failed_sets',
  'total_cards',
  'cards_with_price',
  'cards_missing_price',
  'history_rows_written',
  'trend_gainers_count',
  'trend_losers_count',
  'snapshot_saved',
  'json_updated',
  'wide_sheet_status',
  'duration_seconds',
  'error_message',
  'github_run_id',
  'notes'
];

// Set after first deploy: Project Settings > Script Properties > SHARED_SECRET.
// Never hardcode the secret in this file -- it's pasted into a script bound
// to a spreadsheet Varit owns, and Script Properties keep it out of any
// future "view source" / sharing of this .gs file.
function getSharedSecret() {
  return PropertiesService.getScriptProperties().getProperty('SHARED_SECRET');
}

function jsonOut(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function toRow(r) {
  return [
    r.date || '',
    r.card_id || '',
    r.card_name || '',
    r.set_code || '',
    (r.market_price === undefined || r.market_price === null) ? '' : r.market_price,
    r.currency || '',
    r.source || '',
    r.source_url || '',
    r.confidence || '',
    r.timestamp || ''
  ];
}

// Column A is written as a plain "YYYY-MM-DD" string in toRow(), but
// depending on the spreadsheet's locale settings, Sheets can silently
// auto-convert a value that *looks* like a date into a real Date object on
// write. getRange().getValues() then hands that cell back as a JS Date
// instead of the original string. Normalize both shapes back to
// "YYYY-MM-DD" before comparing, so the duplicate-date check is correct
// either way instead of silently never matching.
function normalizeDateCell(val) {
  if (val instanceof Date) {
    return Utilities.formatDate(val, Session.getScriptTimeZone(), 'yyyy-MM-dd');
  }
  return String(val).trim();
}

// Returns the Run Log sheet, creating it with a frozen header row if it
// doesn't exist yet. Safe to call on every runLog request.
function getOrCreateRunLogSheet(ss) {
  var sheet = ss.getSheetByName(RUN_LOG_SHEET_NAME);
  if (!sheet) {
    sheet = ss.insertSheet(RUN_LOG_SHEET_NAME);
    var headerRange = sheet.getRange(1, 1, 1, RUN_LOG_HEADERS.length);
    headerRange.setValues([RUN_LOG_HEADERS]);
    headerRange.setFontWeight('bold');
    headerRange.setBackground('#f3f3f3');
    sheet.setFrozenRows(1);
    // Set sensible column widths for the most-read columns
    sheet.setColumnWidth(1, 90);   // run_date
    sheet.setColumnWidth(2, 165);  // run_timestamp_utc
    sheet.setColumnWidth(3, 70);   // status
    sheet.setColumnWidth(17, 300); // error_message
    sheet.setColumnWidth(19, 220); // notes
  }
  return sheet;
}

function toRunLogRow(r) {
  return [
    r.run_date            || '',
    r.run_timestamp_utc   || '',
    r.status              || '',
    r.source              || '',
    r.total_sets          !== undefined ? r.total_sets : '',
    r.successful_sets     !== undefined ? r.successful_sets : '',
    r.failed_sets         !== undefined ? r.failed_sets : '',
    r.total_cards         !== undefined ? r.total_cards : '',
    r.cards_with_price    !== undefined ? r.cards_with_price : '',
    r.cards_missing_price !== undefined ? r.cards_missing_price : '',
    r.history_rows_written !== undefined ? r.history_rows_written : '',
    r.trend_gainers_count !== undefined ? r.trend_gainers_count : '',
    r.trend_losers_count  !== undefined ? r.trend_losers_count : '',
    r.snapshot_saved      !== undefined ? String(r.snapshot_saved) : '',
    r.json_updated        !== undefined ? String(r.json_updated) : '',
    r.wide_sheet_status   || '',
    r.duration_seconds    !== undefined ? r.duration_seconds : '',
    r.error_message       || '',
    r.github_run_id       || '',
    r.notes               || ''
  ];
}

// ---------------------------------------------------------------------------
// Price History Wide sheet
// ---------------------------------------------------------------------------

function getOrCreateWideSheet(ss) {
  var sheet = ss.getSheetByName(WIDE_SHEET_NAME);
  if (!sheet) {
    sheet = ss.insertSheet(WIDE_SHEET_NAME);
    var hdrRange = sheet.getRange(1, 1, 1, WIDE_HEADERS.length);
    hdrRange.setValues([WIDE_HEADERS]);
    hdrRange.setFontWeight('bold').setBackground('#cfe2f3'); // light blue to distinguish from Run Log
    sheet.setFrozenRows(1);
    sheet.setFrozenColumns(3); // freeze No., Game, Card ID so scrolling right keeps identity columns visible
    sheet.setColumnWidth(1, 45);   // No.
    sheet.setColumnWidth(2, 75);   // Game
    sheet.setColumnWidth(3, 85);   // Card ID
    sheet.setColumnWidth(4, 200);  // Card Name
    sheet.setColumnWidth(5, 70);   // Set
    sheet.setColumnWidth(6, 60);   // Rarity
    sheet.setColumnWidth(7, 65);   // Currency
  }
  return sheet;
}

// Find the 0-based column index of `date` in the header row, or -1 if absent.
// Handles both plain string headers and Sheets-auto-converted Date objects.
function findDateColIdx(headerRow, date) {
  for (var h = WIDE_META_COLS; h < headerRow.length; h++) {
    var cell = headerRow[h];
    if (cell instanceof Date) {
      cell = Utilities.formatDate(cell, 'UTC', 'yyyy-MM-dd');
    }
    if (String(cell).trim() === date) return h;
  }
  return -1;
}

function handleUpdateWide(ss, data, dryRun) {
  var date     = data.date;     // "YYYY-MM-DD"
  var game     = data.game;     // "onepiece"
  var currency = data.currency; // "USD"
  var incoming = data.rows;     // [{card_id, card_name, set_code, rarity, market_price}]

  if (!date || !game || !Array.isArray(incoming) || incoming.length === 0) {
    return { ok: false, error: 'date, game, and a non-empty rows array are required' };
  }

  if (dryRun) {
    return { ok: true, dryRun: true, action: 'updateWide',
             wouldProcess: incoming.length, date: date, game: game };
  }

  var sheet   = getOrCreateWideSheet(ss);
  var lastRow = sheet.getLastRow();              // 0 if truly empty, 1 if only header
  var lastCol = Math.max(sheet.getLastColumn(), WIDE_META_COLS);

  // Single batch-read of the entire sheet (one API call)
  var allData = lastRow >= 1
    ? sheet.getRange(1, 1, lastRow, lastCol).getValues()
    : [WIDE_HEADERS.slice()];

  var headerRow = allData[0];

  // Find or register the date column
  var dateColIdx = findDateColIdx(headerRow, date); // 0-based
  var isNewCol   = (dateColIdx === -1);
  if (isNewCol) {
    dateColIdx = Math.max(headerRow.length, WIDE_META_COLS); // append right
  }
  var dateColNum = dateColIdx + 1; // 1-based for getRange

  // Build rowMap: "game::set_code::card_id" -> sheet row number (1-based)
  // Indices (0-based): [0]=No. [1]=Game [2]=CardID [3]=Name [4]=Set [5]=Rarity [6]=Currency
  var rowMap = {};
  for (var r = 1; r < allData.length; r++) {
    var rd = allData[r];
    var g  = String(rd[1] || '').trim();
    var id = String(rd[2] || '').trim();
    var s  = String(rd[4] || '').trim();
    if (id) rowMap[g + '::' + s + '::' + id] = r + 1; // sheet row = array index + 1
  }

  // Partition incoming into updates (existing rows) and inserts (new rows)
  var priceByRow = {}; // sheetRowNum -> price  (for existing rows)
  var toInsert   = []; // full row arrays for new cards
  var cardsUpdated = 0;
  var cardsAdded   = 0;

  for (var i = 0; i < incoming.length; i++) {
    var card = incoming[i];
    var key  = game + '::' + card.set_code + '::' + card.card_id;
    if (rowMap.hasOwnProperty(key)) {
      priceByRow[rowMap[key]] = card.market_price;
      cardsUpdated++;
    } else {
      // Build a full row array wide enough to reach the date column
      var rowArr = new Array(dateColNum).fill('');
      rowArr[0] = 0;                  // No. -- assigned below
      rowArr[1] = game;
      rowArr[2] = card.card_id;
      rowArr[3] = card.card_name;
      rowArr[4] = card.set_code;
      rowArr[5] = card.rarity;
      rowArr[6] = currency;
      rowArr[dateColIdx] = card.market_price;
      toInsert.push(rowArr);
      cardsAdded++;
    }
  }

  // --- Write 1: date column header (if new column) ---
  if (isNewCol) {
    sheet.getRange(1, dateColNum).setValue(date);
    // Keep header style consistent: bold + light blue background
    sheet.getRange(1, dateColNum)
         .setFontWeight('bold')
         .setBackground('#cfe2f3')
         .setNumberFormat('@'); // store as text so Sheets doesn't reformat YYYY-MM-DD
    sheet.setColumnWidth(dateColNum, 90); // date columns are 90px wide
  }

  // --- Write 2: update price column for all existing data rows in one shot ---
  // We write the ENTIRE data column (rows 2..lastRow) as a single setValues call.
  // For rows not in priceByRow we preserve the current cell value (or blank for
  // a new column). This avoids N individual setValue calls.
  if (lastRow >= 2) {
    var colData = [];
    for (var rowNum = 2; rowNum <= lastRow; rowNum++) {
      if (priceByRow.hasOwnProperty(rowNum)) {
        colData.push([priceByRow[rowNum]]);
      } else {
        // Preserve existing value (blank for new column, current for existing column)
        var existing = (allData[rowNum - 1] && dateColIdx < allData[rowNum - 1].length)
          ? allData[rowNum - 1][dateColIdx]
          : '';
        colData.push([existing === undefined ? '' : existing]);
      }
    }
    sheet.getRange(2, dateColNum, colData.length, 1).setValues(colData);
  }

  // --- Write 3: append new card rows in one shot ---
  if (toInsert.length > 0) {
    var insertStart = lastRow + 1;
    if (insertStart < 2) insertStart = 2; // guard against empty sheet edge case
    var existingCount = Math.max(lastRow - 1, 0); // data rows already in sheet
    // Assign sequential No. values starting after existing rows
    for (var n = 0; n < toInsert.length; n++) {
      toInsert[n][0] = existingCount + n + 1;
    }
    sheet.getRange(insertStart, 1, toInsert.length, toInsert[0].length)
         .setValues(toInsert);
    lastRow = insertStart + toInsert.length - 1;
  }

  return {
    ok: true,
    action: 'updateWide',
    date: date,
    game: game,
    isNewDateColumn: isNewCol,
    dateColumnNumber: dateColNum,
    cardsUpdated: cardsUpdated,
    cardsAdded: cardsAdded,
    totalDataRows: lastRow - 1
  };
}

function doPost(e) {
  try {
    var secret = getSharedSecret();
    if (!secret) {
      return jsonOut({ ok: false, error: 'SHARED_SECRET not configured on this deployment' });
    }

    var body = JSON.parse(e.postData.contents);

    if (body.secret !== secret) {
      return jsonOut({ ok: false, error: 'unauthorized' });
    }

    // Read-only duplicate-date check. Must be handled before the "rows"
    // validation below, since a checkDate request has no rows -- it's just
    // "does this date already exist?". Never writes anything.
    if (body.action === 'checkDate') {
      var checkDate = body.date;
      if (!checkDate) {
        return jsonOut({ ok: false, error: 'date required for checkDate' });
      }

      var checkSheet = SpreadsheetApp.openById(SPREADSHEET_ID).getSheetByName(SHEET_NAME);
      if (!checkSheet) {
        return jsonOut({ ok: false, error: 'Sheet "' + SHEET_NAME + '" not found' });
      }

      var lastRow = checkSheet.getLastRow();
      var exists = false;
      if (lastRow >= 2) {
        var dateCells = checkSheet.getRange(2, 1, lastRow - 1, 1).getValues();
        for (var i = 0; i < dateCells.length; i++) {
          if (normalizeDateCell(dateCells[i][0]) === checkDate) {
            exists = true;
            break;
          }
        }
      }
      return jsonOut({ ok: true, action: 'checkDate', date: checkDate, exists: exists });
    }

    // Admin run log: append one row per pipeline run.
    // This is append-only (same guarantee as Price History Raw) and is
    // written even on failure runs so the admin sheet always has a record.
    // A dryRun flag is respected here too so test runs don't pollute the log.
    if (body.action === 'runLog') {
      var logData = body.data;
      if (!logData || typeof logData !== 'object') {
        return jsonOut({ ok: false, error: 'data object required for runLog' });
      }

      if (body.dryRun) {
        return jsonOut({ ok: true, dryRun: true, action: 'runLog', wouldAppend: toRunLogRow(logData) });
      }

      var ss = SpreadsheetApp.openById(SPREADSHEET_ID);
      var logSheet = getOrCreateRunLogSheet(ss);
      var newRow = toRunLogRow(logData);
      var nextRow = logSheet.getLastRow() + 1;
      logSheet.getRange(nextRow, 1, 1, newRow.length).setValues([newRow]);

      // Colour the status cell: green for success, orange for partial, red for failed
      var statusCell = logSheet.getRange(nextRow, 3);
      var statusVal = String(logData.status || '').toLowerCase();
      if (statusVal === 'success') {
        statusCell.setBackground('#d9ead3');
      } else if (statusVal === 'partial') {
        statusCell.setBackground('#fce5cd');
      } else if (statusVal === 'failed') {
        statusCell.setBackground('#f4cccc');
      }

      return jsonOut({ ok: true, action: 'runLog', appendedRow: nextRow });
    }

    // Price History Wide: pivot today's prices into the wide-format admin sheet.
    // One card = one row, one date = one column. Append-right for new dates;
    // update the date column in place for re-runs on the same day.
    if (body.action === 'updateWide') {
      var wideResult = handleUpdateWide(
        SpreadsheetApp.openById(SPREADSHEET_ID),
        body.data || {},
        !!body.dryRun
      );
      return jsonOut(wideResult);
    }

    var rows = body.rows;
    if (!Array.isArray(rows) || rows.length === 0) {
      return jsonOut({ ok: false, error: 'no rows provided' });
    }

    var values = rows.map(toRow);

    // Dry run: validate shape/auth, write nothing. Used to test the
    // pipeline end-to-end without ever touching Price History Raw.
    if (body.dryRun) {
      return jsonOut({ ok: true, dryRun: true, wouldAppend: values.length, sample: values.slice(0, 3) });
    }

    var sheet = SpreadsheetApp.openById(SPREADSHEET_ID).getSheetByName(SHEET_NAME);
    if (!sheet) {
      return jsonOut({ ok: false, error: 'Sheet "' + SHEET_NAME + '" not found' });
    }

    var startRow = sheet.getLastRow() + 1;
    sheet.getRange(startRow, 1, values.length, 10).setValues(values);

    return jsonOut({ ok: true, appended: values.length, startRow: startRow });
  } catch (err) {
    return jsonOut({ ok: false, error: String(err) });
  }
}

function doGet(e) {
  return jsonOut({ ok: true, message: 'Price History Raw ingest endpoint is alive' });
}
