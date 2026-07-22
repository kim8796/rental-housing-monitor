# Scrapling Phase 2 Task 4 Report

Task 4 implementation commit: `72c0ac9ecd9b69d8d6713a86ea581a9bccc4b7ea`

First review-closure commit: `19e90882429086d8fb4264d698cf011411653c9a`

## Result

Implemented the policy-bound Scrapling source adapter, fixed-method official JSON adapter,
and closed default adapter registry. The implementation does not include recovery,
credential storage, or browser-profile storage from Tasks 5-7.

## TDD evidence

- Initial Task 4 RED/GREEN work covered registry closure, the official adapter, Scrapling
  strategy selection, redirects, robots, retries, profiles, and normalization.
- Review-boundary RED: 6 focused failures proved the public test factories, subclass
  acceptance, proxy mismatch, unsafe browser history, and reliance on untrusted peer
  metadata. GREEN: 6 passed.
- Streaming-bound RED: 4 of 5 focused cases failed because the curl callback collector and
  body handoff were absent (the official gzip case was already bounded). GREEN: 5 passed.
- Shared-gate RED: policy-only and mixed policy/Scrapling runs both peaked at 8 requests.
  GREEN: 2 passed with both peaks capped at 4.
- Profile RED: 4 failures showed sync/async exits received no exception triple and cleanup
  could mask cancellation. GREEN: 4 passed.
- Official strictness RED: 13 failures covered invalid terminal statuses, malformed JSON
  media types, and date-form Retry-After. GREEN: 13 passed.
- Robots status RED: 17 failures showed missing retry propagation and fail-open treatment of
  malformed/policy statuses. GREEN: 21 passed across both adapters.
- Second sealing RED: 4 failures proved custom production fetcher/gate injection, mutable
  backend execution state, mutable client proxy identity, and the absence of a single-proxy
  composition path.
- Second boundary RED: 13 failures proved decoded gzip iteration, permissive Scrapling
  1xx/304 handling, gate wait outside the official deadline, profile cleanup skipped for
  system exceptions, accepted empty proxy ports, and missing low-level use-time sealing.
- Factory-provenance RED: rebinding adapter/client/backend policy state together bypassed
  the first use-time comparison; a later RED proved the WeakKey registry was exposed as a
  module-global object. Both were closed before the focused run.
- Final focused adapters/backend/security suite: 300 passed.

## Second review finding closure

1. **Production sealing and same-proxy provenance:** `ScraplingBackend` no longer accepts
   fetcher or gate arguments. The Scrapling adapter accepts one proxy URL and internally
   creates both exact components from one integrity-checked policy object. Client/backend
   state is frozen and slotted; current fetchers, gates, timeouts, policy integrity, and
   shared factory provenance are checked immediately before use. The original provenance
   lives in a closure-private weak registry, so low-level field rebinding cannot forge it,
   collected adapters are not retained, and new instances cannot inherit old provenance.
2. **Official decompression bound:** official responses reject every non-identity or
   ambiguous Content-Encoding before body iteration, then stream `aiter_raw()` chunks and
   check their size before extending the accumulator. The gzip-bomb regression proves the
   decoded iterator is never entered and the safe result is POLICY.
3. **Scrapling terminal statuses:** after the five navigation redirect statuses, all static
   and browser responses must be 2xx. Focused 1xx, 199, and 304 regressions fail closed.
4. **True official total deadline:** one outer 30-second timeout now encloses global-gate
   acquisition, client entry, send, raw streaming, response close, and client exit. A
   test-only constant monkeypatch proves a saturated gate times out without a production
   deadline-bypass parameter.
5. **BaseException profile cleanup:** sync and async exits receive the real exception triple
   for ordinary exceptions, cancellation, `KeyboardInterrupt`, and `SystemExit`. Cleanup
   always runs after successful entry, while original cancellation/system exceptions win
   over cleanup failures.
6. **Empty proxy ports:** proxy syntax validation rejects an explicit empty port both with
   an empty path and `/`, after stripping any userinfo from the authority check.

## Policy closure

- Production Scrapling composition owns its exact concrete bounded client/backend types.
  There is no public test transport, fetcher, or gate injection path; tests install raw
  `httpx.MockTransport` streams and fake backend delegation only through test-local seams.
- The Scrapling backend and policy client share one validated proxy policy with a
  process-keyed HMAC integrity value whose representation never exposes the proxy or
  credentials. Frozen state plus closure-held use-time provenance rejects ordinary and
  low-level component replacement.
- The mandatory proxy is the origin-egress enforcement boundary. Untrusted Scrapling
  `primary_ip` and httpx proxy-socket peer metadata are ignored rather than treated as
  origin attestation.
- Every HTTP request, including official/robots httpx traffic and Scrapling curl traffic,
  shares one process-global four-slot gate. Async acquisition does not block the event loop,
  cancellation releases acquired policy-client slots, and Scrapling executor slots remain
  held until orphaned worker calls actually finish.
- Official responses require identity encoding and raw chunks are checked before each
  accumulator extension. Scrapling curl uses a bounded `content_callback` that rejects the
  first excess chunk before retaining it, carries collected bytes into normalization, and
  maps direct or wrapped callback overflow to a safe POLICY error. Browser bodies remain
  bounded after rendering.
- HTTP redirects remain manual and are fully preflighted before every destination request.
  Dynamic and stealthy responses with any redirect history are rejected because the
  browser backend cannot preflight each hop; a changed final URL without history is also
  rejected.
- Robots navigation redirects are manual, same-origin, loop/cap checked, and rate-limited.
  A 404/410 is modeled as absence/fetch failure. A 429/5xx is also fail-open, but its bounded
  Retry-After deadline is passed to the source limiter. Other terminal statuses fail closed.
- Auto escalation is typed: only `FailureCode.REQUIRED_CONTENT_ABSENT` advances HTTP to
  dynamic, and only `FetchError.detected_interstitial` advances dynamic to stealthy.
  Explicit strategies never escalate.
- Browser profile materialization is browser-only and supports synchronous and asynchronous
  context managers. Exit receives the real exception triple, cleanup always runs after
  entry, failures map safely, and cleanup cannot replace an original cancellation,
  `KeyboardInterrupt`, or `SystemExit`.
- Official JSON accepts only navigation redirects or 2xx finals, validates JSON media types
  with RFC token grammar (including non-empty structured `+json` suffixes), and computes
  both delta and HTTP-date Retry-After values from an injected timezone-aware clock.
- Registry resolution remains exact for Scrapling, `official_api/json_get`, and copied
  explicit Python-plugin allowlists. Rejected references are not included in errors.

All adapter tests use injected DNS and test-local transports; no test reaches a public
network.

## Task-plan dependency exception

`personal_monitor.scraping.extractor` still imports `scrapling.Selector`. Task 3 explicitly
required that selector implementation, so removing the runtime dependency in this Task 4
review would contradict the accepted plan and expand scope. The official adapter itself
does not import Scrapling directly; replacing the shared extractor dependency belongs to a
later plan item.

## Final verification

Executed from `rental-housing-monitor` with the repository virtual environment:

- focused adapters/backend/security tests — 300 passed in 1.74s;
- full `pytest -q` — 664 passed in 4.52s;
- `ruff check src tests` — all checks passed;
- `ruff format --check src tests` — 93 files already formatted;
- `compileall -q src` — exit 0;
- `git diff --check` — exit 0.
