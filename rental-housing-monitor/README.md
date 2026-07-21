# 서울·경기 LH·SH·GH 임대주택 공고 모니터

매일 LH, SH, GH의 공식 공고만 확인해 서울특별시와 경기도의 다음 모집공고를 Telegram으로 전송하는 Python 프로젝트입니다.

- 행복주택
- 국민임대
- 신혼부부·신생아 대상 매입임대

공고 고유번호 또는 공식 URL 기반 키를 SQLite에 저장하며, Telegram 전송 성공 기록이 없는 공고만 신규로 보냅니다. 정상 수집 결과가 0건이면 `오늘은 신규 공고가 없습니다.`를 보냅니다. 한 기관의 수집이나 파싱이 실패하면 나머지 기관은 계속 처리하고 실패 기관·단계·원인을 별도 메시지로 알립니다.

## 공식 데이터 소스

- LH: [한국토지주택공사 분양임대공고문 조회 서비스](https://www.data.go.kr/data/15058530/openapi.do)
- SH: [SH 인터넷청약시스템 주택임대 공고](https://www.i-sh.co.kr/app/lay2/program/S1T294C297/www/brd/m_247/list.do?multi_itm_seq=2)
- GH: [GH 임대주택 청약공고](https://apply.gh.or.kr/sb/sr/sr7150/selectPbancRentHouseList.do), [GH 매입임대 청약공고](https://apply.gh.or.kr/sb/sr/sr7155/selectPbancRentHouseList.do)

검색 결과나 비공식 재게시 사이트는 사용하지 않습니다. LH는 서울·경기, 임대주택·주거복지 조합을 90일 범위로 조회하며 SH와 GH는 최신 공식 목록의 모집 후보 상세 페이지를 확인합니다.

GH 서버는 일부 OpenSSL 클라이언트가 요구하는 중간 인증서 체인을 보내지 않고 제한된 TLS cipher만 허용합니다. 인증서 검증을 끄지 않기 위해 인증서 AIA가 지정한 공식 Sectigo 중간 인증서를 패키지에 포함하고, GH 연결에만 TLS 1.2와 해당 cipher를 적용합니다.

## 준비

Python 3.12 이상이 필요합니다.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
```

### 공공데이터포털 키

1. 공공데이터포털에서 `한국토지주택공사_분양임대공고문 조회 서비스` 활용을 신청합니다.
2. 발급 화면의 **일반 인증키(Decoding)** 값을 `DATA_GO_KR_SERVICE_KEY`에 넣습니다. HTTP 클라이언트가 요청 시 인코딩하므로 Encoding 키를 다시 넣지 않습니다.

### Telegram

1. BotFather에서 bot을 만들고 token을 발급받습니다.
2. bot과 대화를 시작하거나 대상 그룹에 bot을 추가합니다.
3. Telegram `getUpdates` 응답 등으로 chat ID를 확인합니다.
4. `.env`에 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`를 넣습니다.

기존에 실행 중인 chatbot이 같은 Telegram bot/chat을 사용한다면 서버의 Secret 관리 화면에서 두 값을 복사해 재사용할 수 있습니다. Git 저장소에는 보통 Secret **이름만** 남고 값은 조회할 수 없습니다. token을 Git 커밋, 이슈, 로그, 채팅에 붙여 넣지 마세요.

`.env` 예시:

```dotenv
DATA_GO_KR_SERVICE_KEY=공공데이터포털_Decoding_키
TELEGRAM_BOT_TOKEN=BotFather_token
TELEGRAM_CHAT_ID=대상_chat_id
TELEGRAM_DELIVERY_TARGET=telegram-default
DATABASE_PATH=data/announcements.db
LOG_PATH=logs/monitor.log
```

`.env`와 DB·로그 파일은 Git에서 제외됩니다.

## 실행

```bash
python -m rental_monitor
```

첫 실행에서는 공식 소스의 현재 조회 범위에서 조건에 맞는 공고를 모두 신규로 취급합니다. 전송이 성공한 뒤에만 delivery가 기록됩니다. 전송 도중 실패한 공고는 다음 실행에서 다시 시도됩니다.

수집 오류는 콘솔과 `logs/monitor.log`에 UTC 시각으로 남습니다. API key와 Telegram token 값은 로그에 기록하지 않습니다.

## 테스트와 정적 검사

테스트는 외부 사이트를 호출하지 않고 공식 응답 구조를 최소화한 JSON/HTML fixture를 사용합니다.

```bash
python -m pytest -q
python -m ruff check .
python -m compileall -q src
```

SH/GH가 HTML 구조를 변경해 목록 표, 공고 ID, 필수 상세 필드를 찾지 못하면 `ParserStructureError`가 발생합니다. 실행기는 기관명을 포함해 Telegram으로 알리고 Actions 로그 artifact를 남깁니다.

## GitHub Actions

저장소 `Settings → Secrets and variables → Actions`에 다음 Repository secrets를 등록합니다.

- `DATA_GO_KR_SERVICE_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

워크플로는 `.github/workflows/rental-housing-monitor.yml`에 있으며 다음을 수행합니다.

- cron `13 3 * * *`: UTC 03:13, 한국시간 매일 12:13
- `workflow_dispatch`: 수동 실행
- `concurrency`: DB를 동시에 갱신하는 실행 차단
- `contents: write`: 전용 `data` 브랜치에 SQLite 저장
- `if: always()`: 성공·실패와 무관하게 로그 artifact 업로드

첫 실행 때 `data` 브랜치가 없으면 자동 생성합니다. 이후 매 실행마다 `rental-housing-monitor/data/announcements.db`만 포함하는 새로운 단일 스냅샷 커밋으로 `data` 브랜치를 교체합니다. 과거 DB 커밋은 보존하지 않으므로 장기간 운영해도 접근 가능한 Git 이력이 누적되지 않습니다. `force-with-lease`가 예상하지 못한 동시 갱신을 감지하면 기존 상태를 덮어쓰지 않고 실행을 실패시킵니다.

Repository 또는 Organization 정책이 Actions의 쓰기 권한을 제한한다면 `Settings → Actions → General → Workflow permissions`에서 Read and write permissions를 허용해야 합니다.

## 저장 구조

SQLite는 다음 상태를 관리합니다.

- `announcements`: 정규화 공고와 최초·마지막 발견 시각
- `deliveries`: 공고·chat별 성공 전송 시각과 Telegram message ID
- `runs`: 실행 결과, 신규 수, 기관별 상태

`announcements`와 `deliveries`는 중복 전송 방지를 위해 계속 보존합니다. `runs`는 최근 90일만 유지하며 종료 시 SQLite `VACUUM`으로 사용하지 않는 공간을 회수합니다. `deliveries`에는 실제 Telegram chat ID 대신 비식별 키 `telegram-default`가 저장됩니다.

기관 고유번호가 있으면 `기관:고유번호`, 없으면 추적 파라미터를 제거한 공식 URL의 SHA-256을 고유 키로 사용합니다.

## 운영 시 확인할 점

- `오늘은 신규 공고가 없습니다.`는 세 기관이 모두 정상 수집됐을 때만 전송됩니다.
- 일부 기관이 실패하면 `정상 처리`와 실패 기관을 포함한 경고가 전송됩니다.
- Actions 실패 시 해당 실행의 `rental-housing-monitor-log-*` artifact를 확인합니다.
- Telegram bot 또는 chat을 바꿔도 기본 비식별 delivery 키가 같으면 과거 공고를 다시 보내지 않습니다. 전체 공고를 새로 받고 싶을 때만 `TELEGRAM_DELIVERY_TARGET`을 새로운 값으로 변경합니다.
