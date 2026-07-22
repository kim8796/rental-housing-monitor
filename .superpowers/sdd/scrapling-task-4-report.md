# Scrapling Phase 2 Task 4 Report

Base commit: `c33d27c43e0dc355cdcc4f0d35d0cf074dabcacd`

## Result

Implemented the policy-bound Scrapling source adapter, fixed-method official JSON adapter,
and closed default adapter registry. The implementation does not include recovery,
credential storage, or browser-profile storage from Tasks 5-7.

## TDD evidence

- Initial adapter collection RED: three planned modules failed with three expected
  `ModuleNotFoundError: personal_monitor.adapters` errors.
- Import skeleton GREEN: 3 tests passed.
- Registry RED: 2 failures because the registry had no constructor; GREEN: 2 passed.
- Official adapter RED: missing bounded client/public constants; GREEN: 8 passed before
  later redirect/retry coverage was added.
- Scrapling adapter RED: 20 behavioral failures because the adapter had no constructor;
  GREEN: 20 passed.
- Peer metadata RED: normalized documents dropped `primary_ip`; GREEN: 1 passed.
- Completion-review RED: three focused failures proved unsealed backend injection,
  proxy-peer misclassification, and unanchored browser history; GREEN: 3 passed.
- Final focused adapter suite: 46 passed.

## Policy closure

- Every strategy attempt validates URL/DNS, acquires the host limiter, validates and
  fetches same-origin robots through the required proxy, rate-limits the source request,
  validates redirect/history/final destinations and trustworthy origin peer metadata,
  then extracts and independently validates observations.
- Scrapling production construction accepts only the sealed default backend with its
  proxy-preserving fetchers and process-wide gates. Tests use the explicit `for_test`
  construction path.
- Robots redirects are manual, validated before request, same-origin, loop/cap checked,
  and rate-limited. Genuine robots fetch failures use
  `RobotsPolicy.from_fetch_failure()`; explicit Disallow remains a policy error.
- HTTP redirects are manual, relative locations are resolved by the bounded normalizer,
  loops and the sixth redirect are rejected before fetching another destination, and the
  full URL/rate/robots chain is rerun for each accepted destination.
- Browser histories must start at the approved request, contain unique validated entries,
  and terminate in a separately validated final URL. Scrapling `primary_ip` is retained
  for origin-peer validation. httpx `network_stream.server_addr` is deliberately ignored
  because it identifies the mandatory proxy socket rather than an attested origin peer.
- Auto escalation is typed: only `FailureCode.REQUIRED_CONTENT_ABSENT` advances HTTP to
  dynamic; detector exceptions/non-booleans close as internal failures; only
  `FetchError.detected_interstitial` advances dynamic to stealthy. Explicit strategies
  never escalate.
- Only transient network failures retry, with at most three total attempts and exact
  1/4-second inter-attempt sleeps. Cancellation is preserved, no final sleep occurs, and
  finite non-negative Retry-After is passed to the host limiter.
- Browser profile materialization supports synchronous and asynchronous context managers,
  is used only on browser paths, and maps missing/materialization failures to safe
  authentication errors without exposing references or paths.
- Official JSON uses a required-proxy concrete client, GET only, fixed User-Agent,
  `Accept: application/json`, and `Accept-Encoding: identity`; it enforces 10-second
  connect/30-second total bounds, manual five-redirect policy, 10 MiB decompressed-body
  limit, JSON media/status rules, robots/rate/URL policy, and imports no Scrapling runtime.
- Registry resolution is exact for Scrapling, `official_api/json_get`, and copied explicit
  Python-plugin allowlists. Rejected references are not included in errors.

All adapter tests use injected DNS plus `httpx.MockTransport`; no test reaches a public
network.

## Final verification

Executed from `rental-housing-monitor` with the repository virtual environment:

- `python -m pytest tests/personal_monitor/adapters -q` — 46 passed in 0.55s.
- `python -m pytest -q` — 590 passed in 4.12s.
- `python -m ruff check .` — all checks passed.
- `python -m ruff format --check .` — 89 files already formatted.
- `python -m compileall -q src` — exit 0.
- `git diff --check` — exit 0.

The completion review's mandatory-proxy, proxy-peer, browser-chain, and acceptance-test
findings were resolved before these gates.
