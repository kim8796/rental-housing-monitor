# GCP 배포와 호스트 운영

이 문서는 `personal-monitor-1`을 처음 준비하는 운영 절차다. 인프라 생성은
`infra/gcp/README.md`의 별도 실행 체크포인트를 통과한 뒤에만 진행한다. 기존
Cloud Run 서비스 `local-social-api`는 이 절차의 변경 대상이 아니다.

## 1. 소스 전송과 IAP 접속

배포할 커밋을 검토한 로컬 체크아웃에서 소스 아카이브를 만든다. `.env`, DB, Codex
로그인 상태와 Git 메타데이터는 아카이브에 포함되지 않는다.

```bash
git status --short
git -C "$(git rev-parse --show-toplevel)" archive \
  --format=tar.gz \
  --output=/tmp/personal-monitor-src.tar.gz \
  HEAD:rental-housing-monitor
chmod 0600 /tmp/personal-monitor-src.tar.gz
shasum -a 256 /tmp/personal-monitor-src.tar.gz
gcloud compute scp /tmp/personal-monitor-src.tar.gz personal-monitor-1:/tmp/personal-monitor-src.tar.gz \
  --project=local-social-native-wlk-0720 \
  --zone=asia-northeast3-a \
  --tunnel-through-iap
gcloud compute ssh personal-monitor-1 \
  --project=local-social-native-wlk-0720 \
  --zone=asia-northeast3-a \
  --tunnel-through-iap
```

VM이 `asia-northeast3-b`에 만들어졌다면 두 명령의 zone만 `asia-northeast3-b`로
바꾼다. VM 안에서는 첫 배포 대상이 비어 있는지 확인한 후 서비스 UID로 압축을 푼다.

```bash
set -Eeuo pipefail
sudo test -z "$(sudo find /srv/personal-monitor/app -mindepth 1 -maxdepth 1 -print -quit)"
sudo chown 10001:10001 /tmp/personal-monitor-src.tar.gz
sudo chmod 0600 /tmp/personal-monitor-src.tar.gz
sudo -u personal-monitor sh -c 'umask 022; exec tar -xzf /tmp/personal-monitor-src.tar.gz -C /srv/personal-monitor/app'
sudo rm -f /tmp/personal-monitor-src.tar.gz
sudo install -o root -g root -m 0755 /srv/personal-monitor/app/deploy/personal-monitor-compose /usr/local/sbin/personal-monitor-compose
```

## 2. 비밀정보 준비

값은 비밀번호 관리자에서 VM의 `sudoedit` 화면으로 직접 옮긴다. Git/Codex 채팅,
이슈, 명령 인자, 셸 히스토리에 값을 붙여 넣지 않는다. 다음 두 파일이 이미 있으면
생성 명령을 다시 실행하지 말고 먼저 기존 파일을 조사한다.

```bash
sudo test ! -e /srv/personal-monitor/.env
sudo install -o root -g root -m 0600 /dev/null /srv/personal-monitor/.env
sudoedit /srv/personal-monitor/.env
sudo chown root:root /srv/personal-monitor/.env
sudo chmod 0600 /srv/personal-monitor/.env

sudo test ! -e /etc/personal-monitor/master.key
sudo sh -c 'umask 077; openssl rand 32 > /etc/personal-monitor/master.key'
sudo chown 10001:10001 /etc/personal-monitor/master.key
sudo chmod 0600 /etc/personal-monitor/master.key
```

`.env`에는 아래 이름만 채운다. `AGE_RECIPIENT`는 오프서버에서 `age-keygen`으로
만든 identity의 공개 recipient이고, identity 파일 자체는 VM에 복사하지 않는다.

```dotenv
PERSONAL_MONITOR_TELEGRAM_BOT_TOKEN=<비밀번호 관리자에서 직접 입력>
PERSONAL_MONITOR_TELEGRAM_USER_ID=<허용할 Telegram 사용자 숫자 ID>
PERSONAL_MONITOR_TELEGRAM_COMMAND_CHAT_ID=<명령 채팅 숫자 ID>
PERSONAL_MONITOR_TELEGRAM_DELIVERY_CHAT_ID=<알림 채팅 숫자 ID>
PERSONAL_MONITOR_DATA_GO_KR_SERVICE_KEY=<공공데이터포털 Decoding 키>
AGE_RECIPIENT=<오프서버 identity의 공개 recipient>
PERSONAL_MONITOR_BACKUP_BUCKET=gs://local-social-native-wlk-0720-personal-monitor-backups
```

ChatGPT Pro의 Codex 로그인을 사용하므로 `OPENAI_API_KEY`와 `CODEX_API_KEY` 항목은
서비스 환경에서 제거한다. 두 API 키를 빈 값으로 두는 대신 행 자체를 만들지 않는다.
마스터 키는 정확히 32바이트이며 잃어버리거나 다시 만들면 기존 vault를 복호화할 수
없다.

## 3. 빌드, DB 초기화, Codex 로그인

먼저 이미지를 만들고 빈 DB의 스키마를 초기화한다.

```bash
sudo personal-monitor-compose build
sudo personal-monitor-compose run --rm --no-deps monitor \
  personal-monitor database init --path /srv/personal-monitor/db/monitor.db
```

인증되지 않은 `codex-worker`는 의도적으로 시작을 거부한다. 따라서 먼저 일회성
컨테이너에서 사용자 본인이 `codex login --device-auth`를 완료한 뒤 worker를
시작한다. 로그인 정보는 `codex-home` Docker volume에 남고 백업 아카이브에는
포함되지 않는다.

```bash
sudo personal-monitor-compose run --rm --no-deps --entrypoint codex \
  -e CODEX_HOME=/srv/personal-monitor/codex-home \
  -e HOME=/srv/personal-monitor/codex-home codex-worker \
  login --device-auth
sudo personal-monitor-compose run --rm --no-deps --entrypoint codex \
  -e CODEX_HOME=/srv/personal-monitor/codex-home \
  -e HOME=/srv/personal-monitor/codex-home codex-worker \
  login status
sudo personal-monitor-compose up -d codex-worker
sudo personal-monitor-compose exec -T \
  -e CODEX_HOME=/srv/personal-monitor/codex-home \
  -e HOME=/srv/personal-monitor/codex-home codex-worker \
  codex login status
sudo personal-monitor-compose stop --timeout 90
```

상태 명령이 성공한 뒤 systemd 파일을 설치하고 기본 서비스를 시작한다.

```bash
sudo install -o root -g root -m 0644 \
  /srv/personal-monitor/app/deploy/systemd/personal-monitor.service \
  /etc/systemd/system/personal-monitor.service
sudo install -o root -g root -m 0644 \
  /srv/personal-monitor/app/deploy/systemd/personal-monitor-backup.service \
  /etc/systemd/system/personal-monitor-backup.service
sudo install -o root -g root -m 0644 \
  /srv/personal-monitor/app/deploy/systemd/personal-monitor-backup.timer \
  /etc/systemd/system/personal-monitor-backup.timer
sudo install -o root -g root -m 0644 \
  /srv/personal-monitor/app/deploy/systemd/personal-monitor-verify.service \
  /etc/systemd/system/personal-monitor-verify.service
sudo install -o root -g root -m 0644 \
  /srv/personal-monitor/app/deploy/systemd/personal-monitor-verify.timer \
  /etc/systemd/system/personal-monitor-verify.timer
sudo systemctl daemon-reload
sudo systemctl enable --now personal-monitor.service
sudo systemctl enable --now personal-monitor-backup.timer
sudo systemctl enable --now personal-monitor-verify.timer
```

임대주택 상태를 import하기 전의 일반 서비스 smoke test까지만 여기서 수행한다.
`docs/operations/rental-cutover.md`는 import 직전에 이 서비스를 다시 정지시켜 7일
shadow 중 예약 전송이 발생하지 않게 한다.

## 4. 서로 분리된 상태 점검

한 명령의 성공으로 전체가 정상이라고 간주하지 않는다. 아래 항목을 각각 확인한다.

Compose와 systemd:

```bash
sudo systemctl status personal-monitor.service --no-pager
sudo personal-monitor-compose ps
sudo journalctl -u personal-monitor.service --since today --no-pager
```

DB 무결성:

```bash
sudo personal-monitor-compose exec -T monitor \
  personal-monitor database integrity-check --path /srv/personal-monitor/db/monitor.db
```

heartbeat 시각과 scheduler의 다음 실행:

```bash
sudo sqlite3 /srv/personal-monitor/db/monitor.db \
  "SELECT checked_at, unixepoch('now')-unixepoch(checked_at) AS heartbeat_age_seconds FROM health_write_probe WHERE singleton=1;"
sudo sqlite3 /srv/personal-monitor/db/monitor.db \
  "SELECT id,status,next_run_at FROM monitors ORDER BY id;"
```

디스크 여유:

```bash
df -h /srv/personal-monitor
```

Telegram poll은 서비스 메모리에만 정확한 시각을 보유한다. `health_write_probe`가
최근이고 아래 `telegram_poll` 오류가 현재 6시간 창에 없으면 heartbeat가 90초
stale 경계를 통과한 것이다. 오류가 있으면 서비스 재시작으로 감추지 말고 네트워크와
bot 충돌을 먼저 조사한다.

```bash
sudo sqlite3 /srv/personal-monitor/db/monitor.db \
  "SELECT created_at,json_extract(payload_json,'$.code') FROM operator_events WHERE json_extract(payload_json,'$.code') LIKE 'telegram_poll_%' ORDER BY created_at DESC LIMIT 5;"
```

Codex 인증:

```bash
sudo personal-monitor-compose exec -T \
  -e CODEX_HOME=/srv/personal-monitor/codex-home \
  -e HOME=/srv/personal-monitor/codex-home codex-worker \
  codex login status
```

마지막 로컬 백업 상태와 GCS 객체:

```bash
sudo cat /srv/personal-monitor/logs/backup-status.json
gcloud storage objects list \
  gs://local-social-native-wlk-0720-personal-monitor-backups/daily/ \
  --sort-by='~name' \
  --limit=1 \
  --format='table(name,generation,updateTime)'
```

`backup-status.json`은 `status=ok`와 최근 UTC `updated_at`이어야 한다. 백업 및 복구
검증의 상세 절차는 `docs/operations/backup-restore.md`를 따른다.
