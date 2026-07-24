# 암호화 백업과 복구 검증

백업은 VM 서비스 계정으로 GCS에 올리지만 복호화 identity는 운영자의 오프서버
보관소에만 둔다. VM에는 공개 `AGE_RECIPIENT`만 있다. 따라서 VM과 버킷 권한이 함께
유출돼도 백업 평문을 바로 열 수 없다.

## 자동 실행

`personal-monitor-backup.timer`는 매일 03:10 Asia/Seoul에 실행되고 missed run을
`Persistent=true`로 보충한다. 백업 스크립트는 다음 순서를 한 번에 수행한다.

1. SQLite online backup과 허용된 adaptive/vault/config 파일만 private staging에 복사
2. `manifest/SHA256SUMS`와 DB row count 생성
3. `scripts/verify_backup.sh`로 두 번째 빈 staging 디렉터리에 복원하고
   `PRAGMA integrity_check` 실행
4. 검증된 tar만 age recipient로 암호화
5. GCS create-only 업로드와 원격 `sha256`/size metadata 재검증
6. daily 7개, weekly 4개를 generation 조건부로 유지

`personal-monitor-verify.timer`는 일요일 04:10에 같은 전체 백업 파이프라인을 한 번 더
실행한다. 이것은 암호화 전에 local staging restore를 실제로 다시 수행하고 검증된
암호화 객체도 하나 더 남긴다. VM에 없는 age identity나 `age --decrypt`는 요구하지
않는다.

수동 점검:

```bash
sudo systemctl start personal-monitor-backup.service
sudo systemctl status personal-monitor-backup.service --no-pager
sudo cat /srv/personal-monitor/logs/backup-status.json
sudo systemctl list-timers 'personal-monitor-*' --all
```

상태 파일 `/srv/personal-monitor/logs/backup-status.json`은 서비스 UID 소유, mode
0600이며 `status=ok`일 때만 성공으로 본다.

## GCS 객체와 업로드 digest 확인

객체 이름을 먼저 목록에서 고르고 generation, size, custom metadata를 읽는다.
다음 예시의 `<OBJECT_NAME>`은 출력에서 고른 정확한 이름으로 바꾼다.

```bash
gcloud storage objects list \
  gs://local-social-native-wlk-0720-personal-monitor-backups/daily/ \
  --sort-by='~name' \
  --limit=1 \
  --format='table(name,generation,updateTime)'
gcloud storage objects describe \
  gs://local-social-native-wlk-0720-personal-monitor-backups/<OBJECT_NAME> \
  --raw \
  --format='json(size,generation,metadata)'
```

복구 검증용 컴퓨터로 같은 generation의 객체를 내려받고 로컬 digest를 계산한다.

```bash
gcloud storage cp \
  gs://local-social-native-wlk-0720-personal-monitor-backups/<OBJECT_NAME> \
  /tmp/personal-monitor-restore.tar.age
sha256sum /tmp/personal-monitor-restore.tar.age
```

`sha256sum` 결과와 object metadata의 `metadata.sha256`, 파일 크기와
`metadata.size`를 각각 비교한다. 불일치하면 복호화를 시도하지 말고 객체와 업로드
로그를 보존한다.

## 오프서버 전체 복구 drill

복호화 identity를 가진 별도 임시 VM 또는 오프라인 Linux 환경에서 수행한다.
identity는 production VM에 업로드하지 않는다. 대상은 현재 사용자 소유의 새로운
빈 디렉터리여야 하며 mode 0700이어야 한다.

```bash
install -d -m 0700 /tmp/personal-monitor-restore-target
chmod 0700 /tmp/personal-monitor-restore-target
scripts/restore_personal_monitor.sh \
  /tmp/personal-monitor-restore.tar.age \
  /tmp/personal-monitor-restore-target \
  /secure/off-server/age-identity.txt
```

스크립트는 암호화 해제 후 closed manifest와 모든 SHA-256을 검사하고, 안전한 tar
항목만 추출하며 SQLite `PRAGMA integrity_check`와 주요 row count를 출력한다. 추가로
읽기 전용 확인을 할 수 있다.

```bash
sqlite3 /tmp/personal-monitor-restore-target/data/db/monitor.db \
  'PRAGMA integrity_check;'
sqlite3 /tmp/personal-monitor-restore-target/data/db/monitor.db \
  'SELECT COUNT(*) FROM monitors; SELECT COUNT(*) FROM observations; SELECT COUNT(*) FROM outbox;'
```

출력과 `manifest/backup.json`, `manifest/SHA256SUMS`를 검토한 뒤 임시 환경을
폐기한다. 이 drill에서 `/srv/personal-monitor/db`, `/srv/personal-monitor` 또는 다른
라이브 데이터 디렉터리를 대상으로 지정하지 않는다.

## 교체 VM으로 복구할 때

실제 장애 복구는 새 VM의 빈 `/srv/personal-monitor`에서만 수행한다. 기존 VM의
Compose와 timer를 먼저 정지하고, 위 오프서버 검증이 성공한 동일 generation을
선택한다. 복구 결과의 다음 항목을 운영자가 검토한 뒤에만 배치한다.

- `data/db/monitor.db`
- `data/adaptive/`
- `data/vault/`
- `secrets/master.key`
- `config/compose.yaml`

`.env`, Codex 로그인 volume, browser 진단 자료는 백업에 들어 있지 않으므로 별도
bootstrap이 필요하다. 복구 파일을 즉시 라이브 경로로 이동하거나 Compose를 자동
시작하지 않는다. `docs/operations/gcp-deploy.md`의 비밀정보, Codex 로그인, 개별
health 순서를 다시 수행한다.
