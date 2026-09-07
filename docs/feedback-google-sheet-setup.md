# Wiring the per-card feedback form to a Google Sheet

The "⚑ Report an issue" form on each card can POST every report straight into a
Google Sheet you own — one row per report, no emails, free, and unlimited. This
uses a small **Google Apps Script Web App** as the endpoint. ~5 minutes to set up.

## Steps

1. Create a new **Google Sheet** (this is where reports will land). Name it e.g. `RetaBase Feedback`.
2. In the sheet: **Extensions → Apps Script**. Delete any starter code.
3. Paste the script below, then **Save** (disk icon).
4. **Deploy → New deployment**. Click the gear → **Web app**. Set:
   - **Description:** RetaBase feedback
   - **Execute as:** Me (your account)
   - **Who has access:** **Anyone**  ← required so the site can post anonymously
5. Click **Deploy**. Google will ask you to **authorize** the script (it needs permission
   to write to your sheet) — approve it. You may see an "unverified app" warning; it's your
   own script, so choose **Advanced → Go to … (unsafe)** and allow.
6. Copy the **Web app URL** — it ends in `/exec`. **Send me that URL** and I'll drop it into
   `config/feedback.json`, rebuild a preview, and you can file a real test report to confirm
   the row appears in your sheet.

## The script

Paste your **Sheet ID** into the first line. It's the long string in your sheet's URL:
`https://docs.google.com/spreadsheets/d/`**`THIS_PART`**`/edit`. Binding by ID makes the
script work whether or not it's bound to the sheet, and the built-in health check lets us
verify the write path without guessing.

```javascript
// RetaBase feedback -> Google Sheet (one row per report).
var SHEET_ID = "PASTE_YOUR_SHEET_ID_HERE";   // from the sheet URL (/spreadsheets/d/<THIS>/edit)
var SHEET_NAME = "Feedback";
var HEADERS = [
  "ts", "report_id", "pmid", "molecule", "molecule_id", "title",
  "aspects_text", "suggested", "note",
  "current_evidence_level", "current_rigor", "current_directness", "current_rank",
  "evidence_class", "page", "schema"
];

function _sheet() {
  var ss = SHEET_ID ? SpreadsheetApp.openById(SHEET_ID) : SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getSheetByName(SHEET_NAME) || ss.insertSheet(SHEET_NAME);
  if (sh.getLastRow() === 0) sh.appendRow(HEADERS);   // header row, once
  return sh;
}

function _append(data) {
  var sh = _sheet();
  sh.appendRow(HEADERS.map(function (h) {
    var v = data[h];
    if (h === "aspects_text" && !v && data.aspects) v = [].concat(data.aspects).join("; ");
    return v == null ? "" : v;
  }));
}

function doPost(e) {
  try {
    _append(JSON.parse(e.postData.contents));
    return ContentService.createTextOutput(JSON.stringify({ success: true }))
      .setMimeType(ContentService.MimeType.JSON);
  } catch (err) {
    return ContentService.createTextOutput(JSON.stringify({ success: false, error: String(err) }))
      .setMimeType(ContentService.MimeType.JSON);
  }
}

// Health check: visiting <exec-url>?ping=1 writes a test row and reports success/error,
// so the sheet-write path can be verified end-to-end from a browser (or by us).
function doGet(e) {
  try {
    if (e && e.parameter && e.parameter.ping) {
      _append({ ts: new Date().toISOString(), report_id: "ping", pmid: "PING",
                molecule: "health-check", aspects_text: "ping", note: "doGet health check" });
      return ContentService.createTextOutput(JSON.stringify({ success: true, wrote: "ping row" }))
        .setMimeType(ContentService.MimeType.JSON);
    }
    return ContentService.createTextOutput(JSON.stringify({ success: true, status: "alive" }))
      .setMimeType(ContentService.MimeType.JSON);
  } catch (err) {
    return ContentService.createTextOutput(JSON.stringify({ success: false, error: String(err) }))
      .setMimeType(ContentService.MimeType.JSON);
  }
}
```

### After pasting: re-deploy the SAME URL

Because a deployment already exists, don't make a new one (that changes the URL). Instead:
**Deploy → Manage deployments → (pencil/edit icon) → Version: New version → Deploy.** Approve
any re-authorization prompt. The `/exec` URL stays the same.

### Verify

Open this in a browser (or tell me and I'll check): your `/exec` URL with `?ping=1` on the end.
If it returns `{"success":true,"wrote":"ping row"}` and a **PING** row appears in the sheet, the
write path works — any remaining issue is just the browser, which the real deployed site handles.
If it returns `{"success":false,"error":...}`, the error text tells us exactly what to fix.

## What you get for QC

Each report is a row keyed by paper (`pmid`), with `aspects_text` like
`evidence_level; rigor` so you can filter the sheet to "every report flagging the
evidence level," plus a snapshot of what the site showed at report time
(`current_evidence_level`, `current_rigor`, `current_directness`, `current_rank`) so a
reviewer can tell whether it has since changed. Confirmed corrections become rows in a
`config/overrides.csv` (keyed by PMID) that the build applies on top of the rules — the
human-review override loop.

## Notes

- **Re-deploying:** if you edit the script later, use **Deploy → Manage deployments →
  (edit) → New version** so the same `/exec` URL keeps working. A brand-new deployment
  gives a new URL (which would need updating in the config).
- **Spam:** anyone can POST to the endpoint. The form has no login by design (low friction).
  If spam appears, we can add a honeypot field and/or a simple shared token check in the
  script. Start simple; harden only if needed.
- **Privacy:** no personal data is collected — only the paper, the flagged aspects, and any
  note the reporter chooses to add.
