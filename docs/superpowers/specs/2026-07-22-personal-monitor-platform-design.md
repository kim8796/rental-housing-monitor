# 개인용 범용 모니터링 플랫폼 설계

## 목표

현재의 서울·경기 임대주택 모니터를 첫 번째 도메인 플러그인으로 유지하면서, 사용자가 Telegram에서 URL과 자연어 조건을 보내면 공고, 채용, 상품 가격·재고, 예약, 부동산 매물, 로그인 필요 사이트 등 다양한 대상을 등록하고 알림받을 수 있는 개인용 모니터링 플랫폼으로 확장한다.

초기 시스템은 한 명이 사용하지만 사용자, 모니터, 구독과 전달 대상을 분리한 데이터 모델을 사용한다. 이후 다중 사용자 서비스로 발전할 때 수집기와 규칙을 다시 작성하지 않고 인증, 과금, 작업 큐와 저장소만 확장할 수 있어야 한다.

## 핵심 결정

- 사용자 인터페이스는 Telegram 자연어 대화가 기본이다. `/monitors` 같은 명령은 장애 시 사용할 보조 수단으로만 둔다.
- Scrapling을 웹 수집·파싱의 기본 엔진으로 적극 채용한다.
- Codex는 신규 모니터 설정 생성과 손상된 설정 복구에만 사용한다. 승인된 모니터의 정기 실행은 AI 없이 결정론적으로 동작한다.
- 기본 AI 라우팅은 GPT-5.6 Terra와 `medium` effort다. 두 번의 생성·검증 시도에 실패한 복잡한 페이지 복구만 GPT-5.6 Sol과 `high` effort로 승격한다.
- Python 플러그인 설계와 보안 검토는 개발 세션에서 GPT-5.6 Sol과 `xhigh` effort를 사용한다. 운영 중인 Telegram 봇은 코드를 생성·배포하지 않는다.
- 개인용 배포는 기존 Google Cloud 프로젝트 `local-social-native-wlk-0720`의 별도 Compute Engine VM을 사용한다. 기존 `local-social-api` Cloud Run 서비스는 변경하거나 모니터 작업과 결합하지 않는다.
- 애플리케이션은 Docker Compose, 파일 기반 설정과 이식 가능한 백업 형식을 사용해 향후 Lightsail 또는 다른 VPS로 이전할 수 있게 한다.

## 범위

### 1단계 범위

- Telegram 사용자 한 명의 자연어 모니터 등록, 조회, 수정, 일시정지, 재개와 삭제
- 공개 API, 정적 HTML, JavaScript 렌더링 페이지와 일반적인 봇 차단 페이지 수집
- 제목, 링크, 텍스트, 숫자, 가격, 날짜, 재고 상태와 반복 목록 추출
- 새 항목, 값 변경, 임계값 도달, 상태 변경과 키워드 일치 알림
- 로그인 세션을 사용하는 사이트의 수집 프로필 저장
- 모니터별 일정, 실행 기록, 이전 관측값, 중복 없는 Telegram 전달
- 구조 변경 감지, 안전 정지, AI 기반 복구 제안과 사용자 재승인
- 기존 LH·SH·GH 임대주택 모니터의 무중단 이전
- Google Cloud VM 배포, 암호화 백업과 다른 VPS로의 복원 절차

### 1단계 비대상

- 공개 회원가입, 결제와 요금제
- 여러 사용자의 동시 브라우저 세션과 사용량 제한
- 웹 관리 화면
- 운영 Telegram 요청만으로 Python 코드를 자동 작성·배포하는 기능
- CAPTCHA 자동 해결을 성공 조건으로 보장하는 기능
- 사이트 이용약관이나 접근 통제를 우회하도록 보장하는 기능

## 아키텍처

시스템은 제어 경로와 실행 경로를 분리한다.

```text
제어 경로 — 사용자 요청이 있을 때만 AI 사용

Telegram 자연어
    → 사용자 인증
    → Intent Router (Terra/medium)
    → Scrapling Probe
    → MonitorSpec 제안
    → 결정론적 검증과 미리보기
    → Telegram 사용자 승인
    → Versioned Monitor Registry

실행 경로 — AI 호출 없음

Scheduler
    → Source Adapter
    → Extractor
    → Validator
    → Observation Store
    → Diff/Rule Engine
    → Delivery Outbox
    → Telegram
```

구성 단위는 다음 책임만 가진다.

- `TelegramGateway`: 업데이트 수신, 본인 확인, 대화 상태, 미리보기와 확인 버튼, 결과 전송
- `IntentRouter`: 자연어를 등록, 조회, 변경, 일시정지, 재개, 삭제, 상태 확인 중 하나의 구조화된 의도로 변환
- `MonitorPlanner`: Scrapling 결과와 사용자 조건으로 `MonitorSpec` 후보를 생성하고 설명 가능한 미리보기를 구성
- `MonitorRegistry`: 승인된 설정의 현재 버전과 과거 버전을 저장하고 원자적으로 활성 버전을 교체
- `Scheduler`: 만기 작업을 선택하고 모니터별 단일 실행 lease를 발급
- `SourceAdapter`: 공식 API, Scrapling 또는 Python 플러그인 중 승인된 수집 방식을 실행
- `Extractor`: 원시 응답에서 선언된 필드를 추출해 정규화된 관측값 생성
- `Validator`: 타입, 필수 필드, 값 범위, 항목 수와 도메인 같은 불변조건 검사
- `DiffEngine`: 마지막 승인된 관측값과 현재 관측값의 변화 계산
- `RuleEngine`: 신규, 변화, 임계값, 상태와 키워드 조건 평가
- `DeliveryOutbox`: 알림을 먼저 영속화하고 Telegram 성공 후 전달 기록을 확정해 중복과 유실 방지
- `RecoveryManager`: 실패 분류, 제한된 재시도, adaptive 후보 탐색, AI 복구와 재승인 조정
- `CredentialVault`: 로그인 쿠키, 브라우저 프로필 키와 외부 서비스 비밀을 모니터 설정과 분리

각 단위는 프로토콜로 연결하며 Telegram, SQLite, Scrapling이나 Codex 구현을 도메인 모델에서 직접 참조하지 않는다.

## Telegram 자연어 경험

사용자는 같은 Telegram 봇에게 다음처럼 요청한다.

```text
https://example.com/product/123
이 상품이 10만 원 아래가 되면 알려줘
```

봇은 URL, 현재 추출값, 조건, 확인 주기와 선택한 수집 방식을 보여주고 `등록`, `수정`, `취소` 버튼을 제공한다. `등록` 승인 전에는 일정이나 활성 설정을 저장하지 않는다.

사용자가 확인 주기를 말하지 않으면 Planner는 6시간마다 확인을 제안한다. 개인용 1단계의 최소 주기는 15분이며, 더 짧은 주기는 등록하지 않는다. 제안된 주기와 시간대는 반드시 미리보기에 표시하고 사용자가 승인해야 한다. 동일 시각 요청이 몰리지 않도록 실제 실행에는 최대 2분의 결정론적 jitter를 적용하되, 기존 임대주택 모니터의 한국시간 12:13 실행은 그대로 유지한다.

자연어 관리는 다음 표현을 포함해야 한다.

- `지금 모니터링 중인 거 보여줘`
- `아까 등록한 상품 감시 잠깐 꺼줘`
- `확인 주기를 하루에 한 번으로 바꿔줘`
- `임대주택 알림은 두고 가격 알림만 삭제해줘`

조회는 즉시 실행할 수 있다. 등록, 조건 변경, 일정 변경, 자동 복구 적용과 삭제는 변경 요약과 확인 버튼을 먼저 보여준다. 대상이 둘 이상이거나 의도가 모호하면 추측하지 않고 구분에 필요한 가장 작은 질문 하나를 한다. 확인 요청에는 10분 만료 시간과 요청자 Telegram user ID를 묶은 일회성 토큰을 사용한다.

개인용 단계에서는 환경설정에 등록된 Telegram `user_id`와 command `chat_id`가 모두 일치하는 요청만 명령으로 처리한다. 알림 목적지 `chat_id`와 명령 채팅은 같은 값일 수 있지만 권한과 전달 목적을 구분해 별도 저장한다.

long polling은 같은 bot token에서 하나의 소비자만 허용하므로 전용 bot token을 기본으로 사용한다. 기존 token을 재사용하려면 기존 시스템이 `sendMessage`만 사용하고 webhook이나 다른 `getUpdates` 소비자가 없음을 먼저 확인해야 한다.

## AI 사용과 인증

운영 서버는 Codex CLI를 ChatGPT 계정으로 로그인해 ChatGPT Pro의 Codex 사용 한도를 소비한다. Platform API 키는 기본 경로에서 사용하지 않는다. 구현은 공식 비대화형 `codex exec --output-schema` 경로를 사용하며, 애플리케이션은 Unix socket을 통해 격리된 Codex worker에 구조화 요청만 전달한다.

- 정상 시작 시 `codex login status`로 ChatGPT 인증인지 확인한다.
- `OPENAI_API_KEY` 또는 `CODEX_API_KEY`가 설정된 경우 Pro 사용 경로와 혼동하지 않도록 AI 등록 기능을 시작하지 않고 운영자에게 알린다.
- 기본 모델은 GPT-5.6 Terra, effort는 `medium`이다.
- 동일 입력에 대한 생성과 검증을 최대 두 번 수행한 뒤에도 실패하면 GPT-5.6 Sol, `high`로 한 번만 승격한다.
- 승격도 실패하면 모니터를 만들지 않고 검증 실패 이유와 수동 플러그인이 필요하다는 사실을 Telegram으로 알린다.
- 정기 실행, 비교, 조건 평가와 알림 작성에는 Codex를 호출하지 않는다.

Codex는 운영 중 다음 권한만 가진다.

- 제공된 URL을 제한된 Scrapling 도구로 읽기
- 허용된 JSON Schema에 맞는 의도와 `MonitorSpec` 후보 출력
- 실패한 설정과 검증 결과를 바탕으로 새 설정 버전 제안

Codex worker는 빈 read-only 작업 디렉터리에서 실행하고 운영 DB, Git 저장소, Docker socket, Telegram token, 수집 자격정보 volume을 마운트하지 않는다. command execution, 파일 변경, MCP 또는 web search 이벤트가 한 번이라도 나타난 실행 결과는 폐기한다. 복잡한 Python 플러그인은 이 저장소의 별도 개발 작업으로 작성하고 테스트·리뷰·배포한다.

## MonitorSpec 계약

승인된 선언형 모니터는 최소 다음 정보를 가진다.

```json
{
  "schema_version": 1,
  "owner_id": "telegram-user:123456789",
  "name": "상품 가격 감시",
  "target_url": "https://example.com/product/123",
  "source_adapter": "scrapling",
  "adapter_ref": null,
  "fetch_strategy": "auto",
  "schedule": "0 * * * *",
  "timezone": "Asia/Seoul",
  "extract": {
    "item_scope": "main",
    "fields": {
      "title": {"selector": "h1", "type": "text", "required": true},
      "price": {"selector": ".price", "type": "krw", "required": true}
    }
  },
  "validators": {
    "min_items": 1,
    "max_items": 1,
    "allowed_link_domains": ["example.com"]
  },
  "rules": [
    {"kind": "numeric_threshold", "field": "price", "operator": "lte", "value": 100000}
  ],
  "notify_on_no_change": false,
  "auth_profile_ref": null
}
```

`source_adapter`는 `official_api`, `scrapling`, `python_plugin` 중 하나다. `official_api`와 `python_plugin`은 임의 모듈 경로가 아닌 배포 코드에 등록된 allowlist 키를 `adapter_ref`로 요구하고, `scrapling`은 `adapter_ref=null`만 허용한다. Scrapling의 `fetch_strategy`는 `auto`, `http`, `dynamic`, `stealthy` 중 하나이며 `auto`는 HTTP 요청부터 시작해 필요할 때만 브라우저 방식으로 승격한다.

선택자는 실행 가능한 코드가 아니라 CSS/XPath와 제한된 텍스트·정규식 연산만 허용한다. 조건도 등록된 연산자 집합으로 표현하며 Python 표현식이나 셸 문자열을 저장하지 않는다. 모든 rule의 field는 `extract.fields`에 선언되어야 하고 숫자 임계값은 숫자형, keyword는 text, status literal은 선언형 scalar type과 호환되어야 한다. 어댑터 관측값도 선언되지 않은 field를 하나라도 포함하면 HTML·token 문자열을 포함해 diff와 저장 전에 거부한다. 모든 변경은 새 `monitor_versions` 행을 만들고 승인된 버전만 활성화한다.

## Scrapling 사용 방식

Scrapling은 다음 순서로 사용한다.

1. 공식 API가 있으면 HTML보다 공식 API 어댑터를 우선한다.
2. 공개 HTML은 `Fetcher`로 요청한다.
3. 필수 내용이 JavaScript 이후 나타나는 경우 `DynamicFetcher`를 사용한다.
4. 일반 브라우저로 접근할 수 있으나 탐지 때문에 실패하는 경우에만 `StealthyFetcher`를 사용한다.
5. 세션이 필요한 사이트는 모니터별 브라우저 프로필을 연결한다.

브라우저 워커 동시성은 초기값 1로 제한하고 HTTP 수집은 최대 4개까지 병렬 실행한다. 같은 호스트에는 기본 10초 간격을 두고 `Retry-After`가 더 길면 그 값을 따른다. HTTP 수집은 연결 10초·전체 30초, 브라우저 수집은 전체 90초, redirect 5회, 압축 해제 후 응답 본문 10MiB를 기본 상한으로 한다. 1단계의 범용 선언형 모니터는 HTML과 JSON만 본문으로 처리하고 그 밖의 바이너리는 내려받지 않는다. 사이트별 어댑터는 테스트와 사용자 승인을 거쳐 더 엄격한 한도를 설정할 수 있다.

모니터 생성 probe 전에 robots.txt를 읽고 허용 여부와 확인 시각을 기록한다. 명시적으로 금지된 경로는 `policy` 오류로 중단하며 개인용 1단계에는 이를 무시하는 설정을 제공하지 않는다. robots.txt가 없거나 일시적으로 조회되지 않는 경우에도 이용약관과 접근 통제를 우회할 권한이 생기는 것은 아니며, 반복 접근은 위의 호스트별 속도 제한을 따른다.

adaptive 기능은 요소 특징을 저장하고 구조 변경 시 후보를 찾는 보조 수단이다. adaptive 후보가 기존 선택자를 대신했다고 해서 바로 정상 결과로 인정하지 않는다. 후보 결과가 필수 validator를 모두 통과해도 모니터를 `needs_review`로 전환하고 새 설정 미리보기를 사용자에게 승인받는다. 승인되지 않은 추출 결과로 일반 알림을 전송하지 않는다.

## 상태와 저장

개인용 단계는 단일 프로세스와 영속 디스크를 전제로 SQLite WAL을 사용한다. 저장 계층은 repository 프로토콜 뒤에 두어 다중 사용자 단계에서 PostgreSQL로 교체한다.

주요 테이블은 다음과 같다.

- `users`: 사용자, Telegram user ID, 상태와 생성 시각
- `delivery_targets`: Telegram chat ID를 포함한 사용자별 알림 목적지
- `monitors`: 소유자, 이름, 현재 상태, 활성 버전, 다음 실행 시각
- `monitor_versions`: 승인된 전체 `MonitorSpec`, 생성 주체, 승인자와 승인 시각
- `observations`: 정규화 관측값, 콘텐츠 해시, 최초·마지막 발견 시각
- `runs`: 모니터 실행, 단계, 수집 전략, 상태, 시작·종료 시각과 오류 분류
- `deliveries`: 알림 고유 키, 목적지, Telegram message ID와 성공 시각
- `outbox`: 아직 전달되지 않은 알림과 재시도 상태
- `pending_actions`: Telegram 확인 대기 작업, 만료 시각과 일회성 토큰 해시
- `credential_refs`: 모니터와 암호화된 자격정보의 논리적 연결

모니터 상태는 `active`, `paused_user`, `paused_auth`, `needs_review`, `disabled` 중 하나다. 사용자 요청은 직접 `active`와 `paused_user` 사이를 전환한다. 사용자 상태 변경과 soft delete는 owner ID를 필수로 받고 SQL mutation predicate에서도 owner를 검사한다. 인증 만료는 `paused_auth`, 구조 또는 검증 실패는 `needs_review`로 전환한다. 삭제는 즉시 물리 삭제하지 않고 `disabled`와 삭제 시각을 기록하며 30일 뒤 관련 설정과 관측값을 정리한다. 삭제 취소는 이 30일 안에만 허용한다.

온라인 observation/outbox 생성은 lease generation과 worker를 검사하는 하나의 atomic unit-of-work로만 수행한다. complete batch는 snapshot을 교체하지만 warning이 있는 partial batch는 실제로 관측된 item만 upsert하고 absent item을 삭제하거나 `last_seen_at`을 갱신하지 않는다. rule 계산을 위한 old-item merge는 runner 메모리 안에서만 수행하며 merged old item을 저장 unit에 다시 넘기지 않는다. Snapshot과 enqueue를 각각 수행하는 unfenced public API는 제공하지 않는다. 향후 offline importer가 필요하면 모든 서비스가 중지된 bootstrap 전용 경로로만 추가한다.

정상 수집의 원시 응답은 추출 후 저장하지 않는다. 구조·검증 실패를 분석할 때만 script, style, hidden 요소와 Secret을 제거한 DOM 조각을 암호화해 최대 7일 보관한다. `runs`는 90일, 성공한 `deliveries`는 180일 보존하고, 활성 모니터의 정규화된 `observations`는 모니터가 삭제될 때까지 유지한다. 이 값은 개인용 1단계의 고정 기본값이며 다중 사용자 단계에서 사용자별 정책으로 바꾼다.

Scrapling adaptive 저장소, SQLite DB, 암호화된 브라우저 프로필 vault와 로그는 `/srv/personal-monitor/` 아래의 분리된 Docker volume에 둔다. 실행 중 필요한 브라우저 프로필 평문은 tmpfs에만 materialize하고 실행 종료 시 다시 암호화한 뒤 삭제한다. Google Persistent Disk의 기본 암호화를 사용하고 자격정보는 애플리케이션 키로 추가 암호화한다. 백업에는 평문 Secret을 넣지 않는다.

## 실행, 비교와 중복 방지

Scheduler는 `next_run_at`이 지난 활성 모니터에 lease를 발급한다. 같은 모니터는 동시에 두 번 실행하지 않으며 lease가 만료된 비정상 실행만 재개할 수 있다.

lease는 worker ID뿐 아니라 모니터마다 단조 증가하는 generation을 포함한다. Scheduler가
반환한 `(monitor_id, generation)`을 실행 시작, 상태 전이, 관측값·outbox 원자 커밋, 실행
종료와 lease 해제까지 그대로 전달하고 모든 변경이 worker와 generation을 함께 비교한다.
lease가 만료되어 다른 worker가 회수하면 이전 worker는 늦게 끝나더라도 관측값, outbox,
상태와 다음 실행 시각을 변경할 수 없다. 새 모니터는 생성 트랜잭션에서 생성 시각을
`next_run_at`으로 저장해 즉시 Scheduler가 선택할 수 있다.

수집 결과는 먼저 validator를 통과해야 관측값이 된다. 관측값은 모니터 ID, 안정적인 항목 ID와 내용 해시로 식별한다. 공식 ID가 있으면 우선 사용하고 없으면 정규화 URL과 핵심 필드에서 결정론적 ID를 만든다.

완전한 수집 결과는 최소·최대 항목 수, 필수 필드, 선언된 scalar 타입, 유한 숫자와 URL
필드의 정확한 허용 host를 모두 검증한다. 경고가 있는 부분 결과는 최대 항목 수와 제공된
항목의 필드·타입·host 안전성은 그대로 검증하지만 최소 항목 수보다 적을 수 있다. 부분
결과에 없는 기존 항목은 삭제하지 않고, 빈 부분 결과도 기존 snapshot 전체를 보존한다.
경고 알림은 고정된 안전 payload만 사용하며 monitor 시간대의 날짜로 중복 제거한다.

알림 조건이 충족되면 알림을 `outbox`에 먼저 기록한다. Telegram 전송 성공 후에만 `deliveries`를 확정한다. 전송 도중 실패하면 같은 outbox 항목을 재시도하므로 알림이 영구 누락되지 않는다. Telegram message ID가 이미 있는 전달은 다시 보내지 않는다.

관측값과 outbox를 함께 기록하는 트랜잭션은 모든 delivery target의 소유자가 monitor
소유자와 같은지 확인한다. 설정 버전 승인자와 활성화 요청자도 monitor 소유자여야 한다.
정기 실행의 신뢰된 내부 ID 조회와 사용자 권한이 필요한 제어 경로 API는 구분한다.

범용 모니터의 기본값은 변경이 없을 때 침묵하는 것이다. 기존 임대주택 모니터는 현재 동작을 보존하기 위해 `notify_on_no_change=true`를 사용해 모든 기관이 정상인 날에만 신규 없음 메시지를 보낸다.

## 실패 처리와 자동 복구

오류는 다음처럼 분류한다.

- `transient_network`: 타임아웃, 연결 단절, HTTP 429와 5xx. 최대 3회 지수 백오프 재시도
- `authentication`: 401, 403, 로그인 화면 또는 세션 만료. `paused_auth`로 전환하고 재로그인 요청
- `structure`: 선택자 미일치, 필수 필드 없음, 예상 항목 수 위반. 일반 알림을 중단하고 `needs_review`로 전환
- `validation`: 타입, 값 범위, 링크 도메인 또는 날짜 검증 실패. 관측값을 저장하지 않고 `needs_review`로 전환
- `policy`: robots.txt, 접근 정책 또는 명시적 차단 때문에 수집하지 않기로 결정. 재시도 없이 운영자에게 설명
- `delivery`: Telegram 오류. 수집 상태는 유지하고 outbox만 재시도
- `internal`: 예상하지 못한 애플리케이션 오류. 해당 모니터를 격리하고 다른 모니터는 계속 실행

구조 실패 시 RecoveryManager는 동일 응답을 한 번 다시 파싱하고 Scrapling adaptive 후보를 찾는다. 새 후보 또는 새 선택자 설정은 현재 버전을 덮어쓰지 않고 다음 버전 후보로 저장한다. Terra/medium 복구가 두 번 실패한 경우에만 Sol/high로 한 번 승격한다. 사용자 승인 전에는 모니터를 재개하지 않는다.

모든 오류 메시지는 모니터 이름, 실패 단계, 마지막 정상 시각, 자동 재시도 여부와 사용자에게 필요한 행동을 포함한다. URL query, 쿠키, 토큰, 원문 자격정보와 전체 HTML은 Telegram이나 일반 로그에 남기지 않는다.

## 보안 경계

### Telegram과 변경 권한

- 허용된 Telegram user ID 외 요청은 처리하지 않고 보안 로그만 남긴다.
- 등록·변경·삭제·자동 복구 적용은 요청자에게만 유효한 만료형 확인을 요구한다.
- Telegram 메시지 내용만으로 셸, Git, Docker 또는 DB 관리 명령을 실행하지 않는다.

### URL과 SSRF 방어

- `http`와 `https`만 허용한다.
- URL 사용자정보, 로컬 파일, `localhost`, loopback, RFC1918 사설망, link-local, 멀티캐스트와 Google metadata 주소를 차단한다.
- 최초 DNS 결과와 실제 연결 주소를 모두 검사하고 redirect마다 같은 검사를 반복한다.
- 허용하지 않은 포트와 한도를 넘는 redirect, 응답 크기와 다운로드를 차단한다.
- 사용자 URL을 처리하는 Scrapling 요청은 private/reserved 목적지를 거부하는 전용 egress proxy를 통과시켜 브라우저 내부 redirect와 하위 리소스에도 같은 네트워크 정책을 적용한다.

### 프롬프트 인젝션 방어

- 웹페이지는 지시가 아니라 신뢰할 수 없는 데이터로 취급한다.
- 숨김 요소, 주석, 스크립트와 스타일은 AI 입력에서 제거한다.
- Codex에는 필요한 DOM 조각과 검증 결과만 전달한다.
- 모델 출력은 폐쇄형 JSON Schema로 검증하며 알 수 없는 필드와 실행 가능한 문자열을 거부한다.
- 모델 제안은 사용자 승인과 deterministic validator를 통과해야 활성화된다.

### 자격정보

- ChatGPT 인증 파일, Telegram token, 세션 쿠키와 암호화 키는 Git, 이미지, 로그와 DB 평문에 저장하지 않는다.
- Codex worker socket은 두 컨테이너가 공유하는 전용 서비스 UID 소유의 `0700` 디렉터리에 `0600` private Unix socket으로만 바인딩한다. 다른 UID와 호스트 사용자는 접근할 수 없고 public TCP port는 열지 않는다.
- 서버에 Platform API 키를 기본 설정으로 두지 않는다.
- Google Cloud 서비스 계정은 백업에 필요한 최소 버킷 권한만 가진다.
- 자격정보 vault의 무작위 master key는 전용 서비스 UID만 읽을 수 있는 데이터 volume 밖의 mode `0600` secret 파일에 둔다. 백업 묶음에는 이 키를 포함하되 운영자가 서버 밖에 보관하는 `age` 공개키로 묶음 전체를 암호화하므로, GCS 객체만으로는 자격정보를 복호화할 수 없다.
- ChatGPT device-code 로그인과 로그인 세션 bootstrap은 Telegram으로 받지 않고 운영자가 IAP SSH 관리 세션에서 수행한다.
- 브라우저 로그인이 필요한 경우 headed browser 화면은 loopback에만 바인딩하고 IAP 터널로 일시 노출한다. 사용자가 직접 로그인을 마치면 프로필을 암호화해 저장하고 일시 화면 서비스를 종료한다.

## Google Cloud 배포

기존 프로젝트의 조사 결과는 다음과 같다.

- 기존 실행은 Compute Engine이 아니라 서울 `asia-northeast3`의 Cloud Run 서비스 `local-social-api`다.
- 기존 서비스는 1 vCPU, 1GiB 메모리, 최소 인스턴스 0, 최대 인스턴스 20이다.
- Compute Engine API는 비활성화되어 있으며 현재 VM, 디스크와 스냅샷이 없다.
- 프로젝트 결제는 활성 상태이고 운영 계정은 프로젝트 Owner다.

구현 단계에서 먼저 Compute Engine API를 활성화하고 서울 리전 quota와 `e2-medium` 재고를 읽기 전용으로 확인한다. quota가 충분할 때만 다음 VM을 만든다.

- 프로젝트: `local-social-native-wlk-0720`
- 리전/존: `asia-northeast3-a` 우선, 재고가 없으면 `asia-northeast3-b`
- 머신: `e2-medium`, 2 vCPU, 4GB RAM, x86-64
- OS: Ubuntu 24.04 LTS
- 디스크: 50GB `pd-balanced`
- 실행: Docker Compose
- 데이터 루트: `/srv/personal-monitor`
- 관리 접속: OS Login과 IAP SSH
- 공개 인바운드 포트: 없음
- Telegram: long polling
- 백업: 전용 GCS 버킷으로 일일 암호화 백업, 7개 일간·4개 주간 보존

월 비용과 프로모션 크레딧 사용량을 위한 예산 알림을 설정한다. 프로모션 크레딧 잔액은 Google Cloud Billing 콘솔에서 확인한다. 기존 `local-social-api`, Artifact Registry, 외부 DB와 S3 설정은 변경하지 않는다.

## 이식성과 서버 이전

GCP 전용 SDK는 배포·백업 스크립트 바깥에서 사용하지 않는다. 애플리케이션은 Docker Compose와 표준 파일·DB 인터페이스만 요구한다.

이전 가능한 묶음은 다음과 같다.

- 버전이 고정된 Docker 이미지와 `compose.yaml`
- Secret 값이 없는 환경설정 템플릿
- SQLite 일관성 백업 또는 이후 PostgreSQL dump
- Scrapling adaptive 데이터
- 암호화된 브라우저 프로필과 자격정보 백업

새 서버에서 복원 검증을 마친 뒤 기존 Scheduler와 Telegram bot을 중지하고, 새 서버에서 단일 인스턴스로 시작한다. long polling을 사용하므로 DNS나 webhook URL 전환은 필요 없다. Telegram `getUpdates` 충돌을 막기 위해 두 bot process를 동시에 실행하지 않는다.

## 기존 임대주택 모니터 이전

기존 LH, SH와 GH 수집기는 `python_plugin` SourceAdapter로 감싸고 현재 필터, 공고 모델, deduplication과 Telegram 형식을 보존한다. LH 공식 JSON API는 Scrapling으로 교체하지 않는다. SH와 GH는 기존 파서를 우선 유지하고 Scrapling 파서 도입은 동일 fixture와 실사이트 shadow 결과가 일치할 때 별도 변경으로 수행한다.

이전 순서는 다음과 같다.

1. 새 플랫폼에 임대주택 플러그인과 기존 fixture 테스트를 이식한다.
2. 매일 한국시간 12:13에 `workflow_dispatch`를 호출하는 현재 QStash/GitHub Actions 운영을 유지한 채 새 서버에서 Telegram 전송 없는 shadow 실행을 7일 연속 수행한다.
3. 기관별 공고 ID, 필터 결과, 실패 상태와 실행 시각을 비교한다.
4. 기존 `data` 브랜치 SQLite에서 공고와 성공 전달 이력을 새 저장소로 가져온다.
5. 마지막 shadow 결과가 일치한 뒤 QStash schedule을 중지한다.
6. 새 서버에서 한 번 수동 실행해 중복 알림이 없는지 확인한다.
7. 정상 확인 후 새 Scheduler를 활성화한다.

7일 동안 한 번이라도 의미 있는 불일치가 있으면 기간을 초기화하고 수정 후 다시 7일을 검증한다. 전환 전에는 기존 워크플로와 data 브랜치를 삭제하지 않는다.

## 관측성과 운영

- JSON 구조 로그에 monitor ID, run ID, 단계, 수집 전략, 소요 시간, 재시도와 오류 분류를 기록한다.
- Secret, Telegram 원문, 쿠키와 전체 HTML은 로그하지 않는다.
- 최근 실행과 다음 실행, 연속 실패 횟수와 마지막 정상 관측 시각을 자연어로 조회할 수 있다.
- 시스템 자체 장애, 디스크 부족, 백업 실패, Codex 인증 만료와 Telegram 장기 실패는 운영 Telegram chat으로 알린다.
- 상태 확인은 DB 쓰기, Scheduler heartbeat, 디스크 여유, Telegram 연결과 Codex 로그인 상태를 분리해 표시한다.
- 매주 한 번 백업 복원 smoke test를 별도 임시 경로에서 실행한다.

## 테스트 전략

### 단위 테스트

- 자연어 의도의 구조 검증과 확인 필요 여부
- MonitorSpec JSON Schema, 버전과 금지 필드
- 숫자·가격·날짜·링크 정규화
- 신규, 변경, 임계값과 상태 규칙
- 안정적인 항목 ID와 콘텐츠 해시
- lease, outbox, delivery idempotency와 만료형 확인 토큰
- URL 정규화, SSRF 차단, redirect와 DNS 재검증
- 실패 분류와 상태 전이

### 수집기 계약 테스트

- 기존 LH JSON과 SH/GH HTML fixture
- 정적 HTML, 반복 목록, 상세 페이지, 가격과 재고 fixture
- JavaScript 렌더링과 로그인 상태를 최소화한 브라우저 fixture
- 정상 빈 결과와 구조 변경의 구분
- Scrapling adaptive 후보가 승인 없이 활성화되지 않음
- 공식 API 우선과 Fetcher 단계적 승격

### AI 평가

- 고정된 자연어 요청 집합을 등록, 조회, 변경, 정지, 재개, 삭제로 정확히 분류
- 한국어 지시와 URL에서 예상 MonitorSpec 생성
- 모호한 요청에서 추측 대신 최소 질문 생성
- 페이지 내 프롬프트 인젝션 문구 무시
- schema 밖 필드와 실행 가능한 표현 거부
- Terra 실패 조건에서만 Sol로 승격

AI 평가에서는 모델 문장 일치를 요구하지 않고 구조화 출력과 deterministic validator 통과 여부를 검사한다.

### 통합과 배포 테스트

- Telegram update → 미리보기 → 확인 → 등록 → 수동 실행 → outbox 전달 전체 흐름
- Telegram 실패 뒤 재시도와 중복 방지
- Docker Compose 재시작 후 DB, adaptive 데이터와 브라우저 프로필 유지
- Codex ChatGPT 로그인과 API 키 오설정 감지
- GCS 백업 생성, 새 디렉터리 복원과 체크섬 검증
- 기존 임대주택 DB import 후 중복 전송 없음
- VM 방화벽에서 공개 인바운드 포트가 없음

CI는 외부 사이트와 실제 Telegram을 호출하지 않는다. 실사이트 검사는 별도 수동 canary로 실행하며 알림 전송을 비활성화한다.

## 다중 사용자 확장 경로

다중 사용자 전환 시에도 `MonitorSpec`, SourceAdapter, Extractor, Validator, RuleEngine과 관측값 계약은 유지한다.

변경되는 부분은 다음과 같다.

- SQLite를 PostgreSQL로 교체
- 단일 Scheduler를 분산 작업 큐와 여러 worker로 교체
- Telegram allowlist를 사용자 가입·인증과 tenant 권한으로 교체
- 사용자별 수집 동시성, 실행량과 보존 정책 추가
- 개인 ChatGPT Pro 인증을 제거하고 서비스용 AI 과금·quota 체계 도입
- 자격정보를 tenant별 KMS envelope encryption으로 분리
- 필요하면 Telegram과 동일한 제어 API 위에 웹 관리 화면 추가

개인용 단계에서도 모든 핵심 레코드에 `owner_id`를 두고 repository 프로토콜을 사용하므로 이 전환은 수집기 재작성 없이 가능해야 한다.

`MonitorSpec`은 strict/frozen 입력 경계이며 extract fields는 읽기 전용 mapping, rules,
keywords와 허용 domain은 tuple로 보관한다. JSON enum 입력과 JSON Schema/model dump 왕복은
유지하며 NaN/Infinity 임계값은 영속화 전에 거부한다. SQLite migration은 순서가 있는
registry로 적용하고 binary가 지원하는 것보다 높은 기록 버전은 거부한다. Shadow 배포
전까지 v1은 변경 가능하지만 첫 shadow 배포 이후에는 v1을 동결하고 후속 변경은 새
migration으로만 추가한다.

## 완료 기준

- Telegram 자연어로 공개 정적 페이지 모니터를 생성하고 미리보기 승인 후 알림받을 수 있다.
- 정기 실행에서는 GPT 호출이 발생하지 않는다.
- 구조 변경과 검증 실패가 잘못된 일반 알림으로 이어지지 않는다.
- 동적 페이지와 한 개의 로그인 세션 모니터가 Docker 재시작 후에도 동작한다.
- 허용되지 않은 Telegram 사용자, SSRF URL과 페이지 프롬프트 인젝션을 차단한다.
- 기존 임대주택 모니터가 7일 shadow 비교와 DB import 뒤 중복 없이 새 서버로 전환된다.
- GCS 백업을 빈 새 서버에 복원해 동일한 활성 모니터와 마지막 관측값을 재현할 수 있다.
- 기존 `local-social-api` Cloud Run 서비스에는 변경이나 성능 회귀가 없다.

## 참고 자료

- Scrapling: <https://github.com/D4Vinci/Scrapling>
- Scrapling adaptive scraping: <https://scrapling.readthedocs.io/en/latest/parsing/adaptive.html>
- Scrapling fetcher 선택: <https://scrapling.readthedocs.io/en/latest/fetching/choosing.html>
- Codex 인증: <https://learn.chatgpt.com/docs/auth>
- Codex 요금과 사용 한도: <https://learn.chatgpt.com/docs/pricing>
- GPT-5.6 prompting 및 effort: <https://developers.openai.com/api/docs/guides/prompt-guidance-gpt-5p6.md>
- Cloud Run 인스턴스 특성: <https://docs.cloud.google.com/run/docs/overview/what-is-cloud-run>
- Compute Engine E2 사양: <https://docs.cloud.google.com/compute/docs/general-purpose-machines>
- Google Cloud 서울 리전: <https://docs.cloud.google.com/compute/docs/regions-zones>
