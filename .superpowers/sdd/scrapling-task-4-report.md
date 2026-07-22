# Scrapling Phase 2 Task 4 Report

Task 4 implementation commit: `72c0ac9ecd9b69d8d6713a86ea581a9bccc4b7ea`

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
- Final focused adapter/backend suite: 170 passed.

## Policy closure

- Production adapters accept only the exact concrete bounded client/backend types. There is
  no public test transport factory or production transport bypass; tests install
  `httpx.MockTransport` and fake backend delegation only through test-local monkeypatching.
- The Scrapling backend and policy client prove that they use the same validated proxy with
  a process-keyed HMAC identity whose representation never exposes the proxy or credentials.
- The mandatory proxy is the origin-egress enforcement boundary. Untrusted Scrapling
  `primary_ip` and httpx proxy-socket peer metadata are ignored rather than treated as
  origin attestation.
- Every HTTP request, including official/robots httpx traffic and Scrapling curl traffic,
  shares one process-global four-slot gate. Async acquisition does not block the event loop,
  cancellation releases acquired policy-client slots, and Scrapling executor slots remain
  held until orphaned worker calls actually finish.
- Official decoded bodies are checked before each accumulator extension. Scrapling curl
  uses a bounded `content_callback` that rejects the first excess chunk before retaining it,
  carries collected bytes into normalization, and maps direct or wrapped callback overflow
  to a safe POLICY error. Browser bodies remain bounded after rendering.
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
  context managers. Exit receives the real exception triple, cleanup always runs, failures
  map safely, and cleanup cannot replace an original `CancelledError`.
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

- focused adapter/backend tests — 170 passed in 1.53s;
- full `pytest -q` — 641 passed in 4.17s;
- `ruff check src tests` — all checks passed;
- `ruff format --check src tests` — 93 files already formatted;
- `compileall -q src` — exit 0;
- `git diff --check` — exit 0.
