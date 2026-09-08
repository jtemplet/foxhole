# Foxhole

Foxhole reads unclassified mail in one Gmail inbox, asks Claude what kind of
mail each conversation is, and records the answer as Gmail labels. It runs
unattended on a timer and never writes, sends, or deletes mail.

## Language

**Thread**:
A conversation in Gmail, and the only thing Foxhole ever classifies. Judged as
a whole: every message in it is evidence, not just the newest one.
_Avoid_: Email, conversation, chain

**Message**:
One piece of mail inside a Thread. Foxhole reads messages but never labels one
on its own, because a label always belongs to the whole Thread.
_Avoid_: Email, mail

**Category**:
One of the twelve names Claude may return: `urgent`, `action needed`,
`follow-up`, `meeting`, `awaiting-reply`, `payment`, `info`, `newsletter`,
`marketing`, `notification`, `estimates`, `other`. A Thread gets one or two,
never zero.
_Avoid_: Tag, class, type

**Label**:
The Gmail label that carries a Category onto a Thread. One label exists per
Category, each with its own color. "Label" names the Gmail artifact; "Category"
names the meaning it carries.
_Avoid_: Using "label" for the meaning as well as the artifact

**Verdict**:
Claude's answer for one Thread: the one or two Categories it chose. A Verdict is
reached once and never revised. A Thread that turns urgent after it was called
`info` stays `info`.
_Avoid_: Classification, result, decision

**Classified**:
A Thread that carries at least one Label. There is no separate marker for this;
the Labels are the record. An unclassified Thread is one with no Label at all.
_Avoid_: Scanned, processed, seen, handled

**Sender**:
The email address a Thread's mail came from, lowercased, with any display name
stripped. Two people behind one address are one Sender; one person writing from
two addresses is two Senders.
_Avoid_: Correspondent, contact, from

**Settled Sender**:
A Sender that has drawn the same Verdict enough times in a row to be treated as
predictable. Mail from a Settled Sender is labeled from the remembered Verdict
without asking Claude. A Sender stops being settled the moment a Verdict differs.
_Avoid_: Trusted sender, known sender, cached sender
