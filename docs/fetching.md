---
parent: /reference.html
title: Download limits and retries
---

# How fetch behaves

- It tries four times, waiting 5, 10 and 20 s between attempts, on 429, 500, 502, 503,
  504, network errors and a body cut short. A `Retry-After` header, in seconds or as a
  date, raises the wait to at most 120 s. Any other error status, and a certificate that does
  not verify, fails at once.
- One source's download has a `timeout_s` budget, 300 s unless set, including retries and waits.
  It is checked before each attempt and after each body read. Each socket operation times
  out after the smaller of 30 s and the budget left at the start of that attempt.
  DNS lookup and HTTP header parsing can exceed the budget, so it is not a hard time limit.
  An optional archive request has the separate timeout below.
- Fetch downloads one source at a time, in manifest order, so a host receives at most
  one source request at a time, including redirects and retries.
- Every source is tried. When some fail, the others are still written, and the run
  exits 3 with one `error:` line per failed source.
- A second fetch in the same project, while one runs, exits at once (a lock on
  `data/raw/.lock`).
- Each line it prints says `new` or `unchanged` against the last version in the log. It says
  `relogged` for the same bytes that came in another way, such as fetched after a `record` or
  under a new `url` or `vintage`. After `new` or `relogged`, run `build`.
- For a source with `archive = true`, the Internet Archive request times out after 120 s
  per connect and read. When it fails, fetch prints a warning, leaves `archive_url` empty,
  and still succeeds. Save Page Now answers once the snapshot is taken.
  Its limit is five captures of one URL per day across all users. Past that it answers
  HTTP 429 with no `Retry-After`, and that fetch records no snapshot.

[Use sourceledger safely](secure-use.md) covers the checks on redirects, compression and keys.
[How it works](architecture.md#fetch) has the steps and the crash safety.
