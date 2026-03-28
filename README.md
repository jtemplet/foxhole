# Foxhole

An automated Gmail agent that classifies every incoming email and applies labels using Claude AI. Runs in Google's cloud via Apps Script — your Mac can be off, no servers to maintain, costs under $1/month.

*"Stay focused while chaos flies overhead."*

## How It Works

```
Gmail Inbox
    │
    ▼
Google Apps Script (trigger every 2 min)
    │  GmailApp.search("is:unread -label:scanned")
    │
    ▼
For each unprocessed thread:
    │  Extract: sender, subject, first 500 chars
    │
    ▼
Claude API (Haiku 4.5)
    │  Returns: JSON array of labels
    │
    ▼
Apply Gmail labels + mark as scanned
```

Every 2 minutes, the agent finds unread emails that haven't been classified yet, sends the metadata to Claude Haiku, and applies the returned labels. A `scanned` label prevents re-processing.

## Labels

| Label | When Applied |
|-------|-------------|
| `urgent` | Explicit time pressure — deadlines, incidents, escalations |
| `action needed` | Sender expects you to do something concrete |
| `follow-up` | Worth revisiting later, no immediate action |
| `meeting` | Calendar and scheduling related |
| `awaiting-reply` | Waiting on someone else's response |
| `payment` | Invoices, receipts, billing, financial alerts |
| `info` | Informational, no action needed |
| `newsletter` | Subscribed content you opted into |
| `marketing` | Unsolicited promotional content |
| `notification` | Automated system alerts |
| `estimates` | Quotes, bids, or proposals from vendors |

Each label is color-coded in Gmail automatically.

## Setup

### 1. Get a Claude API Key

Go to [console.anthropic.com](https://console.anthropic.com), navigate to API Keys, and create one.

### 2. Create the Apps Script Project

1. Go to [script.google.com](https://script.google.com)
2. Click **New Project**, name it `Foxhole`
3. Delete the default code and paste the contents of `foxhole.js`

### 3. Enable the Advanced Gmail Service

1. In the Apps Script editor, click **Services** (+ icon) in the left sidebar
2. Find **Gmail API** and click **Add**

### 4. Add Your API Key

1. Click the gear icon (**Project Settings**)
2. Under **Script Properties**, add:
   - Property: `ANTHROPIC_API_KEY`
   - Value: your Claude API key

### 5. Test

1. Select `testClassification` from the function dropdown and click **Run**
2. Authorize Gmail access when prompted
3. Check the execution log — this is a dry run, no labels are applied

### 6. Go Live

1. Select `classifyNewEmails` and click **Run** to verify labels are applied correctly
2. Go to **Triggers** (clock icon) and add a trigger:
   - Function: `classifyNewEmails`
   - Event source: Time-driven
   - Type: Minutes timer
   - Interval: Every 2 minutes

### 7. Optional: Gmail Filters

For full Inbox Zero, create Gmail filters to auto-archive low-priority labels:

- `newsletter` → Skip Inbox
- `marketing` → Skip Inbox
- `notification` → Skip Inbox

## Cost

| Emails/day | Monthly cost |
|-----------|-------------|
| ~50 | ~$0.15 |
| ~100 | ~$0.30 |
| ~300 | ~$0.90 |

Apps Script hosting and Gmail API access are free. The only cost is Claude API usage.

## Customization

The classification prompt is in the `callClaude()` function. Edit it to:

- Add or remove label categories
- Adjust classification strictness
- Add domain-specific rules (e.g., always label emails from `@yourcompany.com` as `info`)

Update `VALID_LABELS` and `LABEL_COLORS` in `ensureLabelsExist()` to match any prompt changes.

## License

MIT
