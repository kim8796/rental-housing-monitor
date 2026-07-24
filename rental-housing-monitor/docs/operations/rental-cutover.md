# 임대주택 7일 shadow, 전환, 롤백

현재 QStash schedule `rental-housing-monitor-daily`과 GitHub Actions가 7일 shadow가
끝날 때까지 production이다. `.github/workflows/rental-housing-monitor.yml`과 원격
`data` 브랜치는 수정, force-push, 삭제하지 않는다.

새 importer는 임대주택 monitor를 active로 만든다. 따라서 import 직전부터 cutover
직전까지 `personal-monitor.service`를 정지·비활성화해야 한다. shadow 명령은
`NullDeliverySender`를 고정 사용하므로 Telegram 전송 및 일반 runner 영속화를 하지
않는다. QStash는 이 기간에 계속 매일 12:13 Asia/Seoul에 실행한다.

## 1. 최신 data 브랜치 DB 전달

각 shadow 날짜에는 QStash의 GitHub Actions 실행이 끝난 뒤 로컬 관리용 체크아웃에서
최신 DB를 가져온다.

```bash
git fetch origin data
git show origin/data:data/announcements.db > /tmp/legacy-announcements.db
sqlite3 /tmp/legacy-announcements.db 'PRAGMA integrity_check;'
gcloud compute scp /tmp/legacy-announcements.db \
  personal-monitor-1:/tmp/legacy-announcements.db \
  --project=local-social-native-wlk-0720 \
  --zone=asia-northeast3-a \
  --tunnel-through-iap
```

VM zone이 b이면 zone만 바꾼다. IAP SSH로 접속해 one-shot 명령이 실행 중이 아님을
확인하고 고정 경로에 설치한다.

```bash
sudo install -o 10001 -g 10001 -m 0600 /tmp/legacy-announcements.db \
  /srv/personal-monitor/db/legacy-announcements.db
sudo rm -f /tmp/legacy-announcements.db
```

## 2. 서비스 정지와 최초 import

QStash를 아직 Pause하지 않는다. 새 scheduler만 멈춘다.

```bash
sudo systemctl stop personal-monitor.service
sudo systemctl disable personal-monitor.service
cd /srv/personal-monitor/app
sudo docker compose --env-file /srv/personal-monitor/.env ps
```

먼저 dry-run JSON을 보관하고 target DB가 변하지 않았는지 확인한다. owner 숫자는
컨테이너의 기존 허용 사용자 환경값에서 조합되며 화면에 출력하지 않는다.

```bash
sudo docker compose --env-file /srv/personal-monitor/.env run --rm --no-deps monitor \
  /bin/sh -ceu 'exec personal-monitor migration import-rental \
  --source /srv/personal-monitor/db/legacy-announcements.db \
  --database /srv/personal-monitor/db/monitor.db \
  --owner "telegram-user:${PERSONAL_MONITOR_TELEGRAM_USER_ID}" \
  --target telegram-main \
  --dry-run'
```

`status`가 `validated`, `dry_run`이 true인지 검토한 뒤 동일 입력을 실제로 import한다.

```bash
sudo docker compose --env-file /srv/personal-monitor/.env run --rm --no-deps monitor \
  /bin/sh -ceu 'exec personal-monitor migration import-rental \
  --source /srv/personal-monitor/db/legacy-announcements.db \
  --database /srv/personal-monitor/db/monitor.db \
  --owner "telegram-user:${PERSONAL_MONITOR_TELEGRAM_USER_ID}" \
  --target telegram-main'
```

출력은 `status=complete`, `import_complete=true`여야 한다. 같은 명령을 한 번 더 실행해
새 imported row 수가 0인 idempotency도 확인한다. 이 시점에도
`personal-monitor.service`는 시작하지 않는다.

## 3. 7일 연속 shadow

매일 `1. 최신 data 브랜치 DB 전달`을 다시 수행한다. 아래 각 `RUN_DATE=YYYY-MM-DD`를
그날의 실제 Asia/Seoul 날짜로 바꾼 뒤 실행한다. 실행 전에 egress proxy만 올리고,
끝나면 정지한다. 출력의 `matched`가 true여야 하며 하루 누락이나 mismatch가 생기면
연속 일수는 다시 시작한다.

Day 1:

```bash
RUN_DATE=YYYY-MM-DD
sudo docker compose --env-file /srv/personal-monitor/.env up -d egress-proxy
sudo docker compose --env-file /srv/personal-monitor/.env run --rm --no-deps monitor \
  personal-monitor migration shadow-run \
  --source /srv/personal-monitor/db/legacy-announcements.db \
  --database /srv/personal-monitor/db/monitor.db \
  --run-date "$RUN_DATE"
sudo docker compose --env-file /srv/personal-monitor/.env stop egress-proxy
```

Day 2:

```bash
RUN_DATE=YYYY-MM-DD
sudo docker compose --env-file /srv/personal-monitor/.env up -d egress-proxy
sudo docker compose --env-file /srv/personal-monitor/.env run --rm --no-deps monitor \
  personal-monitor migration shadow-run \
  --source /srv/personal-monitor/db/legacy-announcements.db \
  --database /srv/personal-monitor/db/monitor.db \
  --run-date "$RUN_DATE"
sudo docker compose --env-file /srv/personal-monitor/.env stop egress-proxy
```

Day 3:

```bash
RUN_DATE=YYYY-MM-DD
sudo docker compose --env-file /srv/personal-monitor/.env up -d egress-proxy
sudo docker compose --env-file /srv/personal-monitor/.env run --rm --no-deps monitor \
  personal-monitor migration shadow-run \
  --source /srv/personal-monitor/db/legacy-announcements.db \
  --database /srv/personal-monitor/db/monitor.db \
  --run-date "$RUN_DATE"
sudo docker compose --env-file /srv/personal-monitor/.env stop egress-proxy
```

Day 4:

```bash
RUN_DATE=YYYY-MM-DD
sudo docker compose --env-file /srv/personal-monitor/.env up -d egress-proxy
sudo docker compose --env-file /srv/personal-monitor/.env run --rm --no-deps monitor \
  personal-monitor migration shadow-run \
  --source /srv/personal-monitor/db/legacy-announcements.db \
  --database /srv/personal-monitor/db/monitor.db \
  --run-date "$RUN_DATE"
sudo docker compose --env-file /srv/personal-monitor/.env stop egress-proxy
```

Day 5:

```bash
RUN_DATE=YYYY-MM-DD
sudo docker compose --env-file /srv/personal-monitor/.env up -d egress-proxy
sudo docker compose --env-file /srv/personal-monitor/.env run --rm --no-deps monitor \
  personal-monitor migration shadow-run \
  --source /srv/personal-monitor/db/legacy-announcements.db \
  --database /srv/personal-monitor/db/monitor.db \
  --run-date "$RUN_DATE"
sudo docker compose --env-file /srv/personal-monitor/.env stop egress-proxy
```

Day 6:

```bash
RUN_DATE=YYYY-MM-DD
sudo docker compose --env-file /srv/personal-monitor/.env up -d egress-proxy
sudo docker compose --env-file /srv/personal-monitor/.env run --rm --no-deps monitor \
  personal-monitor migration shadow-run \
  --source /srv/personal-monitor/db/legacy-announcements.db \
  --database /srv/personal-monitor/db/monitor.db \
  --run-date "$RUN_DATE"
sudo docker compose --env-file /srv/personal-monitor/.env stop egress-proxy
```

Day 7:

```bash
RUN_DATE=YYYY-MM-DD
sudo docker compose --env-file /srv/personal-monitor/.env up -d egress-proxy
sudo docker compose --env-file /srv/personal-monitor/.env run --rm --no-deps monitor \
  personal-monitor migration shadow-run \
  --source /srv/personal-monitor/db/legacy-announcements.db \
  --database /srv/personal-monitor/db/monitor.db \
  --run-date "$RUN_DATE"
sudo docker compose --env-file /srv/personal-monitor/.env stop egress-proxy
```

Day 7 결과가 일치한 뒤 duplicate probe와 최종 gate를 실행한다. probe 역시
`NullDeliverySender`를 사용한다.

```bash
sudo docker compose --env-file /srv/personal-monitor/.env up -d egress-proxy
sudo docker compose --env-file /srv/personal-monitor/.env run --rm --no-deps monitor \
  personal-monitor migration duplicate-probe \
  --database /srv/personal-monitor/db/monitor.db \
  --monitor rental-housing-seoul-gyeonggi
sudo docker compose --env-file /srv/personal-monitor/.env run --rm --no-deps monitor \
  personal-monitor migration status \
  --database /srv/personal-monitor/db/monitor.db
sudo docker compose --env-file /srv/personal-monitor/.env stop egress-proxy
```

status JSON은 `consecutive_matches=7`, `unresolved_differences=0`,
`state_imported=true`, `duplicate_probe_passed=true`, `"cutover_ready":true`를 모두
만족해야 한다. 하나라도 다르면 cutover하지 않는다.

## 4. Cutover

GitHub Actions 실행 중인 job이 없는지 확인한다. Upstash QStash 콘솔에서 Schedules →
`rental-housing-monitor-daily` → **Pause**를 누른다. 즉시 schedule 상세를 다시
조회해 `isPaused=true`인지 확인한다. rollback 기간에는 schedule을 삭제하지 않는다.

VM에서 한 번만 실제 전송 run을 수행한다.

```bash
cd /srv/personal-monitor/app
sudo docker compose --env-file /srv/personal-monitor/.env up -d egress-proxy
sudo docker compose --env-file /srv/personal-monitor/.env run --rm --no-deps monitor \
  personal-monitor run-once \
  --database /srv/personal-monitor/db/monitor.db \
  --monitor rental-housing-seoul-gyeonggi \
  --delivery enabled
```

종료 코드 0, 최신 `runs.status`, outbox와 delivery 결과, Telegram 수신을 각각 확인한다.
성공한 경우 기본 서비스를 다시 활성화한다.

```bash
sudo systemctl enable personal-monitor.service
sudo systemctl start personal-monitor.service
sudo systemctl status personal-monitor.service --no-pager
sudo docker compose --env-file /srv/personal-monitor/.env ps
```

다음 12:13 Asia/Seoul 예약과 Telegram 자연어 명령을 관찰한다. QStash schedule 삭제는
별도의 안정화·승인 단계이며, 여기서는 pause 상태로 보존한다.

## 5. Rollback

새 서비스나 첫 production run에 문제가 있으면 먼저 새 scheduler와 Compose를
중지한다.

```bash
sudo systemctl stop personal-monitor.service
sudo systemctl disable personal-monitor.service
cd /srv/personal-monitor/app
sudo docker compose --env-file /srv/personal-monitor/.env stop --timeout 90
```

그 다음 Upstash QStash 콘솔에서 같은 schedule을 **Resume**하고 상세를 다시 조회해
`isPaused=false`인지 확인한다. 다음 GitHub Actions `workflow_dispatch`가 정상
실행되는지 확인한다. `.github/workflows/rental-housing-monitor.yml`과 `data 브랜치`는
계속 기존 rollback 자산으로 두며 새 서버가 수정하지 않는다.

첫 production run이 일부 Telegram 메시지를 이미 전송한 뒤 rollback하는 경우에는
즉시 QStash를 재개하기 전에 delivery 기록과 대상 공고를 비교한다. 이 수동 경계의
중복 가능성을 숨기지 말고 운영 기록에 남긴다.
