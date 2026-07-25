# 프로젝트 인수인계

이 문서는 새 Codex 세션이나 다른 AI가 프로젝트의 현재 상태를 빠르게 파악하기 위한
단일 인수인계 문서다. 작업 시작 전에 이 문서와 `git status -sb`, 최신 `git log`를
함께 확인한다. 비밀값, Telegram 숫자 ID, 결제 계정 ID, 인증 토큰은 이 파일에 쓰지
않는다.

## 종료 시 갱신 규칙

모든 개발·배포 작업의 마지막에는 같은 PR 또는 커밋에서 이 문서를 갱신한다.

1. `마지막 갱신`과 기준 커밋을 바꾼다.
2. 완료한 작업과 실제 운영 반영 여부를 기록한다.
3. 운영 상태를 확인했다면 확인 시각과 근거 명령을 기록한다.
4. 미완료 작업, 위험, 다음 세션의 첫 행동을 구체적으로 남긴다.
5. 이미 끝난 항목은 `다음 할 일`에서 제거한다.
6. 추측은 사실처럼 쓰지 말고 `확인 필요`로 표시한다.

## 현재 요약

- 마지막 갱신: 2026-07-25 KST
- 저장소: `kim8796/rental-housing-monitor`
- 기본 브랜치: `main`
- 최근 확인한 `main`: `4b00a8f` (PR #9 Telegram 요청 형식 문서화)
- 최근 기능 기준: `24f55a2` (PR #7 GCP 크레딧 모니터 병합)
- 서비스 성격: 현재 개인용이며, 사용자·소유자 경계를 유지해 향후 다중 사용자
  서비스로 확장 가능하게 설계한다.
- 사용자 개발 방식: 사용자가 직접 코딩하기보다 Codex에 자연어로 요청한다. 기능뿐
  아니라 운영과 사용 편의성을 함께 챙긴다.

## 시스템 구성

### 1. 기존 임대주택 모니터

- LH·SH·GH 공식 소스에서 서울·경기 임대주택 공고를 수집한다.
- GitHub Actions workflow:
  `.github/workflows/rental-housing-monitor.yml`
- QStash schedule `rental-housing-monitor-daily`가 매일 12:13 KST에
  `workflow_dispatch`를 호출한다.
- 실행 상태와 중복 방지 DB는 원격 `data` 브랜치에 단일 SQLite 스냅샷으로
  저장한다.
- 2026-07-24 12:13 KST 실행은 성공했다. 당시 workflow 기준 SHA는
  `472154a3c17a7460b5ece0623122ece21234f963`이었다.

### 2. 개인 모니터 플랫폼

- Telegram 자연어 요청을 받아 모니터를 조회·생성·수정·중지·재개한다.
- 스크래핑 런타임은 Scrapling 기반이며 정적 HTTP, 동적 브라우저, 적응형 selector
  경로를 분리한다.
- 자연어 의도 분석은 VM의 `codex-worker`가 ChatGPT Pro의 Codex device login을
  사용한다. OpenAI API key 기반 과금은 사용하지 않는다.
- 상태 저장은 SQLite, 실행은 Docker Compose, 호스트 관리는 systemd다.
- 소유자 경계, 승인 미리보기, outbox, 감사·운영 이벤트 구조를 유지한다.

Telegram에서 새 모니터를 등록할 때는 JSON이나 slash command가 아니라 아래 요소를
한 메시지에 자연어로 보낸다.

```text
[공개 http(s) URL]
여기서 [감시할 항목]이 [알림 조건]이 되면 알려줘.
[확인 주기]마다 확인해줘.
```

- 필수: 대상 URL, 감시 항목 또는 변화, 알림 조건
- 선택: 확인 주기. 생략하면 planner가 안전한 기본 주기를 제안한다.
- 예: `https://example.com/products 가격이 10만원 이하가 되면 30분마다 확인해서 알려줘`
- 예: `https://example.com/stock 재고가 입고로 바뀌면 알려줘`
- 예: `https://example.com/notices 새 글 중 청년 키워드가 있으면 평일 오전 9시에 알려줘`
- 현재 의도 분석은 대화 기록 전체가 아니라 현재 메시지를 기준으로 하므로, 추가
  설명을 보낼 때도 URL과 조건을 포함한 완전한 요청을 다시 보내는 편이 안전하다.
- 최소 확인 간격은 15분이다. Telegram에 계정 비밀번호, API key, token을 보내지
  않는다. 로그인 사이트는 별도 인증 프로필 등록이 먼저 필요하다.
- 요청을 받으면 페이지를 시험 수집한 뒤 이름, 대상, 현재 예시값, 조건, 시간대,
  확인 주기, Scrapling 수집 방식, robots.txt, 로그인 필요 여부를 미리 보여준다.
  사용자가 `등록` 버튼을 눌러야 실제 모니터가 생성되며 `수정`과 `취소`도 가능하다.

### 3. GCP 크레딧 모니터

- 콘솔 기준값과 Cloud Billing Standard usage BigQuery export를 결합한다.
- Telegram에서 다음과 같은 자연어를 직접 인식한다.
  `구글 클라우드 무료 크레딧 얼마나 남았어?`
- 매일 12:10 KST에 동기화하고 12:20 KST에 잔액·프로젝트 사용액 요약을 보낸다.
- 잔액 30%·10%·5%, 만료 30일·7일·1일, 예상 30일 이내 소진을 알린다.
- 잔액 10% 이하에서는 서버 이전 준비 필요와 백업·재연결 체크리스트를 함께
  표시한다. 사용자 승인 없이 서버를 자동 이전하지 않는다.

## 운영 환경

- GCP 작업 전 `gcloud auth list`로 사용자가 지정한 활성 계정인지 확인한다. 계정
  주소는 공개 저장소에 기록하지 않는다.
- 프로젝트: `local-social-native-wlk-0720`
- VM: `personal-monitor-1`
- zone: `asia-northeast3-a`
- 접속: 외부 IP 직접 SSH가 아니라 IAP를 사용한다.
- 앱 경로: `/srv/personal-monitor/app`
- 환경 파일: `/srv/personal-monitor/.env`
- DB: `/srv/personal-monitor/db/monitor.db`
- systemd: `personal-monitor.service`
- Compose wrapper: `/usr/local/bin/personal-monitor-compose`
- BigQuery dataset: US 멀티 리전 `billing_monitor`
- 백업 버킷 이름과 복구 절차:
  `docs/operations/backup-restore.md`

접속 예:

```bash
gcloud compute ssh personal-monitor-1 \
  --project=local-social-native-wlk-0720 \
  --zone=asia-northeast3-a \
  --tunnel-through-iap
```

비밀정보는 Git이나 이 문서에 없으며 VM의 root 전용 `.env`, master key, Docker의
Codex login volume에 있다. 서버 이전 시 `.env`, Codex 로그인, VM IAM, BigQuery,
Telegram 연결을 다시 확인한다.

## 2026-07-24 확인된 운영 상태

- VM과 `personal-monitor.service`는 실행 중이다.
- `monitor`, `codex-worker`, `egress-proxy` 컨테이너가 모두 실행 중이다.
- 개인 모니터 DB schema migration은 1~7까지 적용됐다.
- `rental-housing-seoul-gyeonggi` 모니터가 `active`이고 다음 실행은
  2026-07-25 12:13 KST다.
- VM 컨테이너 안에서 GCP metadata token 취득과 크레딧 자연어 route를 검증했다.
- 무료 크레딧 콘솔 기준값:
  - 원래 값: ₩460,418.00
  - 남은 값: ₩455,145.36
  - 기준 시각: 2026-07-24 21:10 KST
  - 종료일: 2026-10-08
- Cloud Billing Standard usage export가 활성화됐다.
- `billing_monitor`에 `gcp_billing_export_v1_*` 테이블 생성까지 확인했다.
- 첫 BigQuery 기반 일일 동기화는 아직 확인하지 않았다. 현재 최신 DB snapshot
  source는 `console`이다.
- 구현 배포와 PR #7 병합 직전 전체 테스트 결과는 `5310 passed`, Ruff 통과다.

## 2026-07-25 운영 점검

- 16:09 KST 기준 VM, systemd, 세 Compose 컨테이너와 DB heartbeat는 정상이다.
- 12:10 KST 크레딧 동기화는 예약대로 시작했지만 `BigQueryBillingError`로 실패했다.
- BigQuery SQL과 권한은 정상이며 조회 결과도 반환된다.
- 원인은 Standard usage export의 `month_cost`, `promotion_consumed`,
  `recent_7d_consumed` schema가 `FLOAT`인데
  `src/personal_monitor/billing/bigquery.py`가 `NUMERIC`만 허용하는 형식 불일치다.
- 최신 billing snapshot은 여전히 2026-07-24 21:10 KST의 `console` 기준값이다.
  12:20 요약이 실제 Telegram에 전달됐는지는 별도로 검증하지 않았다.
- `backup_failed` 운영 이벤트가 2026-07-24 03:10 KST 이후 반복됐고,
  `backup-status.json`은 그 시각 이후 갱신되지 않았다. 크레딧 파서 수정과 별개로
  백업 실패 원인을 조사해야 한다.

## 중요한 운영 경계

- 2026-07-24 QStash 실행 성공과 GCP의 임대주택 모니터 `active` 상태가 모두
  확인됐다. QStash가 실제로 pause됐는지는 이번 확인에서 검증하지 못했다.
- 다음 12:13 KST 전에 Upstash에서
  `rental-housing-monitor-daily`의 `isPaused`를 반드시 확인한다.
- 완전 전환을 유지하려면 QStash를 pause하고 삭제하지 않은 채 rollback 자산으로
  보존한다. pause가 아니면 두 실행 경로가 동시에 동작할 위험이 있다.
- GitHub workflow와 `data` 브랜치는 rollback 확인 전까지 삭제하거나 강제
  변경하지 않는다.
- 결제 크레딧이 10% 이하라고 알리더라도 자동 서버 이전은 금지한다.
- `local-social-api` Cloud Run 서비스는 이 프로젝트 배포의 변경 대상이 아니다.
- 로그나 예외에 API key, Telegram token, Codex 인증 정보가 포함되지 않게 한다.

## 저장소 지도

- `src/rental_monitor/`: 기존 LH·SH·GH 수집과 Telegram 전송
- `src/personal_monitor/`: 개인 모니터 플랫폼
- `src/personal_monitor/billing/`: GCP 크레딧 모델·BigQuery·알림
- `src/personal_monitor/control/`: Telegram 자연어 의도와 승인 흐름
- `src/personal_monitor/adapters/`: Scrapling 및 사이트 adapter
- `compose.yaml`: VM 컨테이너 구성
- `infra/gcp/`: VM·IAM·BigQuery 프로비저닝
- `docs/operations/gcp-deploy.md`: 배포와 상태 점검
- `docs/operations/rental-cutover.md`: QStash shadow·전환·롤백
- `docs/operations/backup-restore.md`: 암호화 백업·복구 검증
- `tests/`: 외부 호출 없이 동작하는 테스트와 fixture

## 개발과 검증

작업 디렉터리는 저장소 내부의 `rental-housing-monitor/`다. Python 3.12 이상과
`uv`를 사용한다.

```bash
uv run pytest
uv run ruff check .
git diff --check
```

배포 전에는 변경 범위에 맞는 테스트를 먼저 실행하고, 완료를 보고하기 직전에
운영 서비스·DB·로그를 각각 확인한다. 배포 절차는
`docs/operations/gcp-deploy.md`를 따른다.

## 다음 세션의 첫 행동

1. `git status -sb`, `git log -5 --oneline`으로 이 문서 이후 변경을 확인한다.
2. BigQuery 결과의 안전한 `FLOAT` 파싱을 테스트 우선으로 수정하고 VM에 배포한다.
3. 수동 동기화 또는 다음 12:10 KST 실행으로 최신 billing snapshot이
   `source=bigquery`인지 확인한다.
4. 반복된 `backup_failed`의 실제 오류와 GCS 최신 객체를 확인해 별도로 복구한다.
5. Upstash QStash schedule의 실제 pause 상태와 임대주택 중복 실행 여부를 확인한다.
