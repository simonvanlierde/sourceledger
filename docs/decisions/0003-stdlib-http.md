# Keep HTTP fetching in the standard library

- Status: proposed
- Date: 2026-11-04

## Context

sourceledger uses `urllib.request` and fetches one source at a time.
Its core has no runtime dependencies. The [standard library client](https://docs.python.org/3/library/urllib.request.html)
also handles proxy settings from the environment and local file inputs.

The fetcher in [`ledger.py`](../../src/sourceledger/ledger.py) adds behavior that a
replacement must preserve:

- Check the connected peer before sending a redirected request from a public host.
  Through a proxy, the proxy resolves the destination and this check does not apply.
- Drop credentials when a redirect changes scheme, host or port.
- Close redirect responses without reading their bodies.
- Bound downloaded bytes and check a shared time budget between attempts and body reads.
  DNS lookup and HTTP header parsing can exceed that budget; it is not a hard deadline.
- Keep the publisher's bytes intact for hashing and retry transient failures with bounded waits.

A replacement must still preserve these rules. Connection pooling and concurrent
downloads remain optimizations to evaluate against measured workloads.

## Decision

Keep `urllib.request` and the existing fetcher checks. Adding a core dependency does
not currently solve a missing feature or remove enough project-specific behavior.

## Alternatives

- [urllib3](https://urllib3.readthedocs.io/en/stable/): pooling and retry helpers are useful, but don't justify a core dependency for the current fetcher.
- [Requests](https://requests.readthedocs.io/en/latest/): sessions and automatic connection reuse would still leave the fetcher's project-specific checks to maintain.
- [HTTPX](https://www.python-httpx.org/): async APIs and HTTP/2 support exceed the current fetcher's needs.
- [Niquests](https://niquests.readthedocs.io/en/latest/): HTTP/2 and HTTP/3 support exceed the current fetcher's needs.

## Consequences

The core keeps its dependency-free installation. We maintain retries, redirect
checks and body handling ourselves. Any transport change must preserve the existing
fetch behavior, proxy support and supported inputs. Test fixtures may be adapted
when those contracts remain covered.

## Revisit when

Revisit when representative workloads show that connection reuse or another
transport feature would materially improve fetching. Compare candidates against
the existing safety checks, proxy behavior and supported inputs.
