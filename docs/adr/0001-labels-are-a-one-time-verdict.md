# Labels are a one-time verdict, not a running state

A Thread leaves the search set the moment it carries any Label, because the
query built in `classifyNewEmails()` excludes every Category label. So Foxhole
judges each Thread exactly once, on the conversation as it stood at first sight,
and never looks again. A Thread that was `info` in March is still `info` in
September, however it turned out.

We chose this over re-classifying a labeled Thread whenever new mail lands on it.
Re-classification is more truthful, but it costs a Claude call per new message on
every Thread already labeled, and it would need a real "already seen" marker,
since Labels could no longer serve as one. An earlier version had such a marker
and it was removed in `74f78d1`; we are not bringing it back.

The cost is accepted deliberately: Foxhole exists to sort a new inbox quickly,
not to keep a permanent, accurate index of every conversation.
