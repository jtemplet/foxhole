// ============================================================
// Gmail Inbox Zero Categorization Agent
// Classifies unread emails using Claude AI and applies labels.
// ============================================================

const ANTHROPIC_API_URL = 'https://api.anthropic.com/v1/messages';
const MODEL = 'claude-haiku-4-5-20251001';
const LABEL_PREFIX = '';
const VALID_LABELS = [
  'urgent', 'action needed', 'follow-up', 'meeting', 'awaiting-reply',
  'payment', 'info', 'newsletter', 'marketing', 'notification', 'estimates',
  'other'
];
const MAX_BODY_LENGTH = 500;
const CACHE_THRESHOLD = 5;
const CACHE_KEY = 'SENDER_CACHE';

/**
 * Main entry point — called by time-driven trigger every 2 minutes.
 * Finds unread, unscanned emails and classifies them.
 */
function classifyNewEmails() {
  const apiKey = PropertiesService.getScriptProperties().getProperty('ANTHROPIC_API_KEY');
  if (!apiKey) {
    console.error('ANTHROPIC_API_KEY not set in Script Properties.');
    return;
  }

  // Ensure all labels exist
  ensureLabelsExist();

  const excludes = VALID_LABELS.map(l => '-label:' + l.replace(/\s+/g, '-')).join(' ');
  const threads = GmailApp.search('is:unread ' + excludes, 0, 20);

  if (threads.length === 0) {
    console.log('No new unread emails to classify.');
    return;
  }

  console.log('Found ' + threads.length + ' unread threads to classify.');

  for (const thread of threads) {
    try {
      const messages = thread.getMessages();
      const latest = messages[messages.length - 1];

      const emailData = {
        from: latest.getFrom(),
        subject: latest.getSubject(),
        body: (latest.getPlainBody() || '').substring(0, MAX_BODY_LENGTH)
      };

      const senderEmail = extractEmail(emailData.from);
      const cached = getCachedLabels(senderEmail);

      let labels;
      if (cached) {
        labels = cached;
        console.log('Cache hit: "' + emailData.subject + '" (' + senderEmail + ') → [' + labels.join(', ') + ']');
      } else {
        labels = callClaude(apiKey, emailData);
        updateSenderCache(senderEmail, labels);
        console.log('Classified: "' + emailData.subject + '" → [' + labels.join(', ') + ']');
      }

      applyLabels(thread, labels);

    } catch (error) {
      console.error('Error classifying thread "' + thread.getFirstMessageSubject() + '": ' + error.message);
      // Skip this email — it will be retried on the next run
    }
  }
}

/**
 * Calls the Claude API to classify an email.
 * Returns an array of label strings.
 */
function callClaude(apiKey, emailData) {
  const systemPrompt = `You are an email classifier. Given the email metadata below, assign 1-2 labels from this exact list:

  urgent, action needed, follow-up, meeting, awaiting-reply, payment, info, newsletter, marketing, notification, estimates, other

  Respond with ONLY a JSON array of label strings. No explanation, no markdown.
  Example: ["action needed", "payment"]

  Classification rules:
  - "urgent": ONLY when the email contains an explicit deadline, describes an active incident, or is a clear escalation. Do NOT apply just because the sender used words like "important", "ASAP", or "reminder".
  Urgent means real consequences if not handled within hours.
  - "action needed": The sender expects you to DO something — reply, approve, fill out a form, make a decision, review something.
  - "follow-up": Worth revisiting later but no immediate action. Status updates to check back on.
  - "meeting": Calendar invites, scheduling requests, agendas, Zoom/Teams links, rescheduling.
  - "awaiting-reply": You sent or need to send a response and are waiting to hear back. Ball is in someone else's court.
  - "payment": Invoices, receipts, billing, subscription renewals, bank alerts, expense reports.
  - "info": Informational with no action needed. Team announcements, project updates, CC'd threads.
  - "newsletter": Subscribed editorial content — Substack, digests, curated roundups, RSS-to-email.
  - "marketing": Unsolicited promotional content — sales outreach, product announcements, cold emails, discount offers.
  - "notification": Automated system alerts — GitHub, CI/CD, shipping tracking, app alerts, password resets.
  - "estimates": Quotes, bids, estimates, or proposals from contractors, vendors, or service providers for project work.
  - "other": Use when the email does not clearly fit any of the above categories.

  When in doubt between two labels, prefer the more actionable one.
  An email can have at most 2 labels. Most emails should have exactly 1.
  Every email MUST receive at least one label.`;

  const userMessage = `From: ${emailData.from}\nSubject: ${emailData.subject}\nPreview: ${emailData.body}`;

  const payload = {
    model: MODEL,
    max_tokens: 64,
    system: systemPrompt,
    messages: [{ role: 'user', content: userMessage }]
  };

  const options = {
    method: 'post',
    contentType: 'application/json',
    headers: {
      'x-api-key': apiKey,
      'anthropic-version': '2023-06-01'
    },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true
  };

  const response = UrlFetchApp.fetch(ANTHROPIC_API_URL, options);
  const status = response.getResponseCode();

  if (status !== 200) {
    throw new Error('Claude API returned status ' + status + ': ' + response.getContentText());
  }

  const result = JSON.parse(response.getContentText());
  const text = result.content[0].text.trim();

  // Parse JSON array from response
  let labels;
  try {
    labels = JSON.parse(text);
  } catch (e) {
    // Try to extract JSON array if model wrapped it in markdown
    const match = text.match(/\[.*\]/s);
    if (match) {
      labels = JSON.parse(match[0]);
    } else {
      throw new Error('Could not parse Claude response: ' + text);
    }
  }

  // Validate labels, fall back to "other" if none survive filtering
  const validated = labels
    .filter(label => VALID_LABELS.includes(label))
    .slice(0, 2);

  return validated.length > 0 ? validated : ['other'];
}

/**
 * Applies Gmail labels to a thread.
 */
function applyLabels(thread, labels) {
  for (const labelName of labels) {
    const fullName = LABEL_PREFIX + labelName;
    const label = GmailApp.getUserLabelByName(fullName);
    if (label) {
      thread.addLabel(label);
    }
  }
}

/**
 * Creates all required Gmail labels if they don't already exist.
 * Run this once manually or let it run on first trigger execution.
 */
function ensureLabelsExist() {
    const LABEL_COLORS = {
      'urgent':         { textColor: '#ffffff', backgroundColor: '#cc3a21' },
      'action needed':  { textColor: '#ffffff', backgroundColor: '#fb4c2f' },
      'follow-up':      { textColor: '#000000', backgroundColor: '#fce8b3' },
      'meeting':        { textColor: '#ffffff', backgroundColor: '#4a86e8' },
      'awaiting-reply': { textColor: '#ffffff', backgroundColor: '#a479e2' },
      'payment':        { textColor: '#ffffff', backgroundColor: '#16a766' },
      'info':            { textColor: '#ffffff', backgroundColor: '#999999' },
      'newsletter':     { textColor: '#ffffff', backgroundColor: '#653e9b' },
      'marketing':      { textColor: '#ffffff', backgroundColor: '#e07798' },
      'notification':   { textColor: '#ffffff', backgroundColor: '#b6cff5' },
      'estimates':      { textColor: '#ffffff', backgroundColor: '#16a766' },
      'other':          { textColor: '#ffffff', backgroundColor: '#c2c2c2' },
    };

    const allLabelNames = VALID_LABELS.map(l => LABEL_PREFIX + l);
    const existingLabels = Gmail.Users.Labels.list('me').labels;
    const existingNames = new Set(existingLabels.map(l => l.name));

    for (const labelName of allLabelNames) {
      const baseName = labelName.replace(LABEL_PREFIX, '');
      const color = LABEL_COLORS[baseName];

      if (!existingNames.has(labelName)) {
        try {
          const labelResource = {
            name: labelName,
            labelListVisibility: 'labelShow',
            messageListVisibility: 'show',
          };
          if (color) {
            labelResource.color = color;
          }
          Gmail.Users.Labels.create(labelResource, 'me');
          console.log('Created label: ' + labelName);
        } catch (e) {
          console.log('Skipped (already exists): ' + labelName);
        }
      } else if (color) {
        const existing = existingLabels.find(l => l.name === labelName);
        Gmail.Users.Labels.patch({ color: color }, 'me', existing.id);
        console.log('Updated color: ' + labelName);
      }
    }
  }

// ============================================================
// Sender Cache
// ============================================================

/**
 * Extracts the email address from a "Display Name <email>" string.
 */
function extractEmail(fromField) {
  const match = fromField.match(/<([^>]+)>/);
  return (match ? match[1] : fromField).toLowerCase().trim();
}

/**
 * Returns cached labels for a sender if the cache has enough confidence,
 * or null if the sender isn't cached or hasn't hit the threshold yet.
 */
function getCachedLabels(senderEmail) {
  const cache = JSON.parse(PropertiesService.getScriptProperties().getProperty(CACHE_KEY) || '{}');
  const entry = cache[senderEmail];
  if (entry && entry.hits >= CACHE_THRESHOLD) {
    return entry.labels;
  }
  return null;
}

/**
 * Updates the sender cache after a Claude classification.
 * Increments hits if labels match, resets if they changed.
 */
function updateSenderCache(senderEmail, labels) {
  const props = PropertiesService.getScriptProperties();
  const cache = JSON.parse(props.getProperty(CACHE_KEY) || '{}');
  const entry = cache[senderEmail];
  const labelsKey = JSON.stringify(labels.slice().sort());

  if (entry && JSON.stringify(entry.labels.slice().sort()) === labelsKey) {
    entry.hits += 1;
  } else {
    cache[senderEmail] = { labels: labels, hits: 1 };
  }

  props.setProperty(CACHE_KEY, JSON.stringify(cache));
}

/**
 * Clears the entire sender cache. Run after prompt changes.
 */
function clearSenderCache() {
  PropertiesService.getScriptProperties().deleteProperty(CACHE_KEY);
  console.log('Sender cache cleared.');
}

/**
 * Removes a single sender from the cache.
 * Usage: removeSenderFromCache('noreply@github.com')
 */
function removeSenderFromCache(email) {
  const props = PropertiesService.getScriptProperties();
  const cache = JSON.parse(props.getProperty(CACHE_KEY) || '{}');
  const key = email.toLowerCase().trim();
  if (cache[key]) {
    delete cache[key];
    props.setProperty(CACHE_KEY, JSON.stringify(cache));
    console.log('Removed from cache: ' + key);
  } else {
    console.log('Not in cache: ' + key);
  }
}

/**
 * Prints the current sender cache contents for debugging.
 */
function printSenderCache() {
  const cache = JSON.parse(PropertiesService.getScriptProperties().getProperty(CACHE_KEY) || '{}');
  const entries = Object.entries(cache);
  console.log('Sender cache: ' + entries.length + ' entries\n');
  for (const [sender, entry] of entries) {
    const status = entry.hits >= CACHE_THRESHOLD ? 'ACTIVE' : 'learning';
    console.log(sender + ' → [' + entry.labels.join(', ') + '] (' + entry.hits + ' hits, ' + status + ')');
  }
}

/**
 * Manual test function — classifies the 5 most recent unread emails.
 * Run this from the Apps Script editor to verify the system works.
 */
function testClassification() {
  const apiKey = PropertiesService.getScriptProperties().getProperty('ANTHROPIC_API_KEY');
  if (!apiKey) {
    console.error('ANTHROPIC_API_KEY not set. Go to Project Settings > Script Properties.');
    return;
  }

  const threads = GmailApp.search('is:unread', 0, 5);
  console.log('Testing with ' + threads.length + ' threads...\n');

  for (const thread of threads) {
    const messages = thread.getMessages();
    const latest = messages[messages.length - 1];

    const emailData = {
      from: latest.getFrom(),
      subject: latest.getSubject(),
      body: (latest.getPlainBody() || '').substring(0, MAX_BODY_LENGTH)
    };

    try {
      const labels = callClaude(apiKey, emailData);
      console.log('Subject: ' + emailData.subject);
      console.log('From: ' + emailData.from);
      console.log('Labels: [' + labels.join(', ') + ']\n');
    } catch (error) {
      console.log('ERROR on "' + emailData.subject + '": ' + error.message + '\n');
    }
  }

  console.log('Test complete. No labels were applied — this is a dry run.');
}

