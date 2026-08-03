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

- 마지막 갱신: 2026-08-04 KST
- 저장소: `kim8796/rental-housing-monitor`
- 기본 브랜치: `main`
- GCP 결제 동기화 복구 전 `main`: `dc61626` (PR #15)
- 운영 배포 코드 기준: `6a36d15` (`codex/fix-monitor-semantic-accuracy`)
- 최종 통합: PR #13 `fix: enable production URL discovery`, PR #14
  `test: verify safe server upgrade runbook`, PR #15
  `fix: send detailed rental alerts`, PR #16 `fix: restore GCP billing sync`
  및 PR #17 `fix: preserve monitor request semantics`
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
- 사이트 이름과 게시판·키워드만 말하면 Codex가 공개 웹에서 후보 URL을 찾고
  사용자 확인 뒤 등록하는 URL 없는 생성 흐름이 구현됐다. URL이 있는 기존 요청은
  검색 없이 처리하고, URL 없는 신규 등록 요청에서만 Codex 실시간 웹 검색을 켠다.
- 최소 확인 간격은 15분이다. Telegram에 계정 비밀번호, API key, token을 보내지
  않는다. 로그인 사이트는 별도 인증 프로필 등록이 먼저 필요하다.
- 요청을 받으면 페이지를 시험 수집한 뒤 이름, 대상, 현재 예시값, 조건, 시간대,
  확인 주기, Scrapling 수집 방식, robots.txt, 로그인 필요 여부를 미리 보여준다.
  사용자가 `등록` 버튼을 눌러야 실제 모니터가 생성되며 `수정`과 `취소`도 가능하다.
- URL 대신 구체적인 사이트명·게시판명을 받으면 공식 후보를 URL 정책과 Scrapling
  실제 접속으로 검증한다. 후보가 여러 개면 Telegram 선택 버튼을 표시하며 최종
  등록 확인 때 사용자별 URL 별칭을 schema migration 8에 저장한다.
- URL 없는 실서비스 요청은 Codex 웹 검색, Scrapling 실제 접속, AI spec 작성,
  `등록/수정/취소` 미리보기까지 검증됐다. 테스트가 만든 pending action은 즉시
  폐기되어 운영 모니터 수는 바뀌지 않았다.
- PR #17 기준 전체 회귀 테스트 `5350 passed`, Ruff와 `git diff --check`가
  통과했다.

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
- Compose wrapper: `/usr/local/sbin/personal-monitor-compose`
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
- 이 항목은 당시 상태 기록이다. 첫 BigQuery 동기화와 백업 복구는 아래
  `2026-07-25 운영 복구`에서 완료됐다.
- 구현 배포와 PR #7 병합 직전 전체 테스트 결과는 `5310 passed`, Ruff 통과다.

## 2026-07-25 운영 복구

- 16:34 KST 기준 `904b141`을 VM에 배포했고 systemd와 세 Compose 컨테이너,
  SQLite 무결성 및 heartbeat가 정상이다.
- 결제 동기화 실패에는 두 원인이 있었다.
  - Standard usage export의 세 금액 필드가 `FLOAT`인데 파서가 `NUMERIC`만
    허용했다.
  - VM 서비스 계정에 `billing_monitor` 데이터셋 `READER` ACL이 없었다.
- 파서를 실제 schema에 맞추고 데이터셋에 최소 읽기 ACL을 부여했다.
  `infra/gcp/provision.sh`도 allowlist가 필요한
  `bq add-iam-policy-binding --dataset` 대신 기존 ACL과 etag를 보존하는
  `bq update --update_mode=UPDATE_ACL` 방식을 사용한다.
- 16:34 KST 수동 동기화가 성공했고 최신 billing snapshot은
  `source=bigquery`, 잔액 ₩455,145.36, 98.85%다. 아직 export 사용행이 없어
  프로젝트별 사용액은 0건이며, 다음 자동 동기화는 매일 12:10 KST다.
- 배포 이후 `billing_iteration_failed`는 0건이다. 12:20 자동 Telegram 요약은
  다음 예약 실행 때 별도 확인한다.
- 백업 실패 원인은 앱의 `compose.yaml`이 group-writable mode `0664`로 배포돼
  백업 스크립트의 보안 검사를 통과하지 못한 것이었다.
- 배포 파일을 서비스 계정 소유 `0644`로 바로잡고 배포 runbook에 mode/owner
  검사를 추가했다.
- 16:34 KST 백업 서비스가 `success/0`, `backup-status.json`이 `status=ok`로
  끝났고 GCS에 `daily/2026-07-25T073428Z.tar.age` 객체가 생성됐다.
- 백업 timer는 active이며 다음 예약은 2026-07-26 03:10 KST다.
- 복구 및 재발 방지 변경 후 전체 테스트 `5310 passed`, Ruff, `bash -n`,
  `git diff --check`가 통과했다.

## 2026-07-26 URL 탐색 운영 배포

- 03:03 KST에 코드 커밋 `ea35de8`을 VM에 배포했다. 새 릴리스 디렉터리에서
  이미지를 먼저 빌드하고 network 없는 일회성 컨테이너로 migration을 적용한 뒤
  앱 디렉터리를 교체했다. 이전 앱과 이미지는 rollback 자산으로 보존했다.
- URL 없는 등록 흐름의 운영 장애 원인은 세 가지였다.
  - Codex CLI 0.144.1의 `web_search` 이벤트가 서로 다른 값의 중복 `id` 키를
    보냈다. 일반 JSON의 중복 키 거부는 유지하고, 크기가 제한된 해당 이벤트의
    문자열 `id`만 허용했다.
  - Scrapling `adaptive=True`가 읽기 전용 monitor 컨테이너에서 기본 SQLite
    저장소를 만들려 했다. 프로젝트의 암호화 adaptive 저장소는 별도로 있으므로
    일반 HTTP fetch에서는 Scrapling 기본 adaptive 저장소를 끈다.
  - `MonitorSpec.extract.fields`의 동적 객체가 Codex Structured Outputs의 strict
    schema에서 거부됐다. AI transport에서 명명된 필드 배열로 변환하고 도메인
    경계에서 다시 매핑으로 복원하며 중복 이름과 최대 50개 제한을 검증한다.
- 실제 운영 내부 라우터에
  `서울주택도시공사 임대주택 모집공고 게시판에서 강남구 공고가 나오면 알려줘`
  요청을 보냈다. `SH 임대주택 강남구 모집공고 알림` 미리보기와
  `등록/수정/취소` 버튼이 생성됐고 pending action은 `0→0`, cleanup 오류는
  0건이었다. 실제 모니터는 등록하지 않았다.
- 배포 후 `personal-monitor.service=active`, Compose 서비스 3개,
  DB `quick_check=ok`, migration 8, 활성 모니터 1개, heartbeat 54초,
  최근 operator/service 오류 0건, Codex `Logged in using ChatGPT`를 확인했다.
- 결제 최신 snapshot은 `source=bigquery`, 잔액 ₩455,145.36,
  종료일 2026-10-08이다.
- 03:07 KST 암호화 백업이 `status=ok`로 완료됐고
  `daily/2026-07-25T180735Z.tar.age` GCS 객체(338,200 bytes)를 확인했다.
- 롤백 이미지는 유지하고 Docker build cache만 정리해 약 5.47GB를 회수했다.
  `/srv/personal-monitor` 디스크 여유는 약 16GB, 사용률은 67%다.
- 안전한 재배포 절차는 `docs/operations/gcp-deploy.md`의
  `기존 서버의 안전한 업그레이드`에 기록했다. PR #14는 초기 배포의 Compose
  wrapper 강제와 업그레이드의 격리 build·network 없는 migration을 각각
  runbook 테스트로 고정했다.

## 2026-07-27 임대주택 상세 알림 수정

- 12:13 KST 예약 실행에서 실제 신규 공고
  `오산시 행복주택 입주자격완화 예비입주자 모집(2026.07.27)`을 찾았지만
  Telegram에는 내부 이벤트명 `new_item`과 LH 목록 URL만 표시됐다.
- 원인은 공통 `render_payload`가 관측 item을 버리고 monitor의 `target_url`만
  사용한 것이었다. `new_item` 판정 자체는 신규 항목이라는 의미로 정확했다.
- 고정 `python_plugin/rental_housing` adapter의 신규 공고만 기존
  `format_announcement`로 렌더링한다. 이제 제목, 기관, 지역, 주택 유형, 대상,
  공고일, 접수 기간과 `selectWrtancInfo.do` 상세 URL을 보낸다.
- 상세 URL은 허용 도메인, scheme, port, credential 및 민감 query 이름을 다시
  검증한다. 일반 Scrapling 모니터는 선언된 매칭 필드의 현재값을 길이 제한과
  민감값 제거 후 표시하고, 신규 항목은 선언된 제목·이름을 우선 표시한다. 이벤트
  종류는 `신규 항목`, `값 변경`, `키워드 일치` 같은 한국어로 표시한다.
- 과거에 이미 전달된 임대주택 import outbox payload는 레거시 형식으로 동결했다.
  새 렌더러가 바뀌어도 기존 DB를 다시 import할 때 충돌하지 않는다.
- 최종 커밋·배포 직전 전체 테스트는 `5337 passed`, Ruff와
  `git diff --check`가 통과했다. 사용자의 운영 방식에 따라 앞으로 테스트는 구현
  도중 반복하지 않고 코드 변경을 모두 마친 뒤 커밋·배포 직전에 한 번 실행한다.
- 코드 커밋 `da94ec3`을 rollback 가능한 디렉터리 교체 방식으로 VM에 배포했다.
  2026-07-27 배포 직후 `personal-monitor.service=active`, Compose 서비스 3개,
  DB `quick_check=ok`, migration 8, 활성 모니터 1개, 미전송 outbox 0개와 최근
  서비스 오류 0개를 확인했다. 다음 실행은 2026-07-28 12:13 KST다.
- 배포 후에는 사용자 지시에 따라 별도 수집·메시지 전송 테스트를 실행하지 않았다.
  따라서 중복 Telegram 알림은 발생하지 않았다.

## 2026-08-03 GCP 결제 동기화 복구

- 결제 스케줄러는 매일 12:10 KST에 실행됐지만 2026-07-27부터 2026-08-03까지
  8회 연속 `BigQueryBillingError`가 발생했다. 마지막 정상 DB snapshot은
  2026-07-26 12:10 KST였고 Telegram 12:20 요약은 이 오래된 값을 반복했다.
- BigQuery export 자체와 VM IAM은 정상이었다. 2026-08-03 진단 당시 테이블은
  9,397행이고 최신 export는 22:18 KST였다.
- 첫 원인은 Cloud Billing의 `FLOAT` 합계가
  `47382.38245100002`처럼 부동소수 오차를 포함하는데 파서가 정확한 마이크로원
  정수만 허용한 것이었다. 쿼리에서 각 금액을 소수점 6자리로 반올림한
  BigQuery `NUMERIC`으로 변환한 뒤 합산하도록 바꿨다.
- 두 번째 원인은 export가 비어 있던 첫 성공 시점에 기준 누적 사용액을 0으로
  고정한 것이었다. 늦은 과거 데이터 백필이 들어오면 기준일 이전 사용액까지 다시
  차감될 수 있었다. 이제 매 동기화에서 콘솔 기준시각 이전 누적 사용액도 함께
  계산하고 최신 기준점으로 저장해 과거 백필을 자동 상쇄한다. DB migration이나
  수동 기준값 수정은 필요하지 않다.
- snapshot이 24시간 이상 오래되면 Telegram 상태·요약에
  `사용량 동기화가 24시간 이상 지연되었습니다` 경고를 표시한다.
- 첫 배포 이미지가 Scrapling import 단계에서 재시작했다. 코드 원인이 아니라
  재빌드 중 `apify-fingerprint-datapoints 0.13.0→0.14.0`,
  `cssselect 1.4.0→1.5.0`, `curl-cffi 0.15.0→0.16.0`이 자동 선택된 의존성
  드리프트였다. 즉시 이전 이미지로 rollback한 뒤 Scrapling 0.4.12와 검증된
  browser runtime 버전을 `pyproject.toml`에 고정했다.
- 최종 커밋·재배포 직전 전체 테스트 `5339 passed`, Ruff와
  `git diff --check`, 실제 BigQuery 현재 쿼리·파싱이 통과했다.
- 코드 커밋 `6198eb8`을 격리 build, network 없는 migration, 원자적 디렉터리
  교체 방식으로 배포했다. 운영 교체 전 새 이미지의 Scrapling import를 3회
  확인하고 교체 후 30초 동안 monitor 재시작 횟수 0을 확인했다.
- 23:48 KST 수동 동기화 결과는 잔액 `₩413,734.38 / ₩460,418.00 (89.86%)`,
  사용 `₩46,683.62`, 최근 7일 일평균 약 `₩3,666.30`, 예상 소진일
  2026-11-24, 만료일 2026-10-08이다. 8월 프로젝트 사용액은
  `Local Social Native: ₩11,600.39`다.
- 배포 후 `personal-monitor.service=active`, Compose 서비스 3개,
  monitor restart 0, DB `quick_check=ok`, migration 8, heartbeat 약 35초,
  미전송 outbox 0개, 신규 오류 0개, Codex `Logged in using ChatGPT`를 확인했다.
- 23:49 KST 암호화 백업이 `status=ok`로 완료됐고
  `daily/2026-08-03T144931Z.tar.age` GCS 객체(348,440 bytes)를 확인했다.
  실패 이미지와 재생성 가능한 build cache만 제거해 약 3.46GB를 회수했으며 서버
  디스크 사용률은 79%다. 정상 운영 이미지와 rollback 자산은 보존했다.

## 2026-08-04 모니터 의미 정확도 개선

- LH 공공데이터의 `CLSG_DT`를 실제 접수 종료일로 매핑한다. 대상 문구는 공고명에
  `청년`, `신혼`, `신생아`가 명시된 경우에만 `공고명 기준`으로 표시하고, 그 외에는
  `공식 공고문 신청자격 확인`으로 표시해 신청 자격을 추측하지 않는다.
- SH 제목은 공백을 제거한 뒤 동호선정, 서류심사·제출, 예비당첨자 발표 등 모집 뒤
  후속 운영 게시물을 신규 공고에서 제외한다. 기존 행복주택·국민임대·신혼부부
  매입임대 범위는 유지하며 청년안심주택 등 신규 공급유형은 이번 변경에 포함하지
  않았다.
- Scrapling 후보 승인 경계에서 시험 추출 0건과 `min_items=0`을 거부한다. GPT가
  만든 키워드, 상태값, 숫자 기준·비교 방향, 변경·신규 규칙이 사용자의 조건 문장에
  근거하지 않으면 최대 3회 재시도 후 pending action 없이 실패한다.
- 등록 미리보기는 `지정 키워드` 대신 실제 키워드를 안전하게 보여준다. 일반
  Telegram 알림은 선언된 매칭 필드의 현재값을 표시하며 URL query, credential 형태,
  제어문자와 길이 초과 값은 기존 출력 경계에서 제거한다.
- 전체 검증은 `5350 passed`, Ruff와 `git diff --check` 통과다. 기능 커밋
  `6a36d15`을 PR #17 브랜치에 push했다.
- 00:55 KST에 `6a36d15`을 격리 build, Scrapling import 3회, network 없는 migration,
  원자적 디렉터리 교체 방식으로 VM에 배포했다. 이전 앱 디렉터리와
  `personal-monitor:rollback-6a36d15` 이미지는 보존했다.
- 배포 후 `personal-monitor.service=active`, Compose 서비스 3개, restart 0,
  DB `quick_check=ok`, migration 8, heartbeat 34초, 활성 모니터 1개, 미전송 outbox
  0개, 미소비 pending action 0개, 최근 실패 run 0개, 최근 journal 오류 0개와
  Codex `Logged in using ChatGPT`를 확인했다.
- Telegram 전송 없이 운영 이미지에서 실제 임대 수집을 다시 실행했다. 총 44건
  (LH 39, GH 5), LH·SH·GH 상태 모두 `ok`, 경고 0건이었다. LH 39건 모두 공식
  마감일이 들어갔고 대상은 공고명 근거 10건, 공고문 확인 필요 29건으로 나뉘었다.
- 최신 결제 snapshot은 00:13 KST `source=bigquery`, 잔액 ₩413,734.38,
  최근 일평균 ₩2,926.62, 예상 소진일 2026-12-24다. 12:10 자동 동기화는 아직
  도래하지 않았으므로 아래 다음 행동에서 별도 확인한다.
- 00:56 KST 암호화 백업이 `status=ok`로 완료됐고
  `daily/2026-08-03T155611Z.tar.age` GCS 객체(348,440 bytes)를 확인했다. build
  cache 2.94GB를 정리한 뒤 디스크 사용률은 84%, 여유는 약 7.7GB다.
- 01:42 KST 병합 후 읽기 전용 종합 smoke test를 추가로 실행했다. 로컬 전체 테스트
  `5350 passed`, Ruff와 `git diff --check`가 다시 통과했다. 운영 Codex worker는
  `등록된 모니터 보여줘`를 `list`로 분류했고 confidence는 0.99였다.
- 메모리 DB와 실제 `example.com` 문서를 사용한 전체 planner 경로가
  `keyword_match`, 예시 1건, `키워드 포함: Example Domain` 미리보기를 만들었다.
  실제 Scrapling 관측값으로 만든 알림도 `일치 필드: title`,
  `현재값: Example Domain`을 표시했다. AI에 가상 HTML을 주고 다른 실제 URL을
  긁게 한 첫 진단 시도는 의도대로 0건 승인 거부됐으며, 같은 실제 문서를 사용하는
  제품 경로 검증과 구분했다.
- 같은 테스트에서 임대 실수집은 44건(LH 39, GH 5), 세 기관 상태 `ok`, 경고
  0건이었다. 종료 후에도 운영 active 모니터 1개, 미전송 outbox 0개, 미소비 pending
  action 0개, 최근 실패 run 0개, 컨테이너 restart 0, DB `quick_check=ok`, 최근
  journal 오류 0개였으므로 Telegram 전송과 운영 상태 변화는 없었다.

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

코드 변경을 모두 마친 뒤 커밋·배포 직전에 변경 범위에 맞는 테스트를 한 번
실행하고, 완료를 보고하기 직전에 운영 서비스·DB·로그를 각각 확인한다. 배포 절차는
`docs/operations/gcp-deploy.md`를 따른다.

## 다음 세션의 첫 행동

1. 2026-08-04 12:10 KST 자동 결제 동기화와 12:20 Telegram 요약이 성공하고
   `billing_iteration_failed`가 다시 생기지 않는지 확인한다.
2. Upstash QStash schedule의 실제 pause 상태와 임대주택 중복 실행 여부를 확인한다.
3. 사용자가 Telegram에서 첫 URL 없는 모니터를 실제 등록하면 첫 예약 실행 결과와
   중복 알림 방지를 확인한다.
4. Scrapling runtime 버전을 올릴 때는 자동 범위 확장 대신 새 이미지 import와
   실제 HTTP·브라우저 수집을 검증한 뒤 고정 버전을 의도적으로 갱신한다.
