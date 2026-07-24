# Personal monitor GCP infrastructure

이 디렉터리는 `local-social-native-wlk-0720` 프로젝트에 개인 모니터 전용 VM과
백업 버킷을 준비한다. 기존 Cloud Run 서비스는 조회 기준선에만 포함되며 변경하지 않는다.

## 1. 읽기 전용 점검

```bash
bash infra/gcp/preflight.sh
```

`preflight.sh`는 계정, 결제, 예산 조회 권한, Compute API 상태, CPU 쿼터, 두 후보
존의 `e2-medium`, 동일 이름 VM, 기존 Cloud Run 서비스 이름을 JSON으로 출력한다.
API를 활성화하거나 리소스를 변경하지 않는다. Compute API가 꺼져 있으면 관련 값은
`unknown_until_enabled`가 되고 종료 코드는 1이다.

## 2. 실행 체크포인트

실제 적용은 비용과 외부 상태를 바꾸므로 운영자 승인 후에만 실행한다. 코드 생성이나
테스트 완료는 이 체크포인트를 대신하지 않는다.

```bash
export PERSONAL_MONITOR_PROVISION_CONFIRM=local-social-native-wlk-0720/personal-monitor-1
bash infra/gcp/provision.sh --apply
```

스크립트는 월 50,000원 예산을 먼저 검증하거나 생성한 다음 API, 서비스 계정,
US 멀티 리전 BigQuery `billing_monitor` 데이터셋, 백업 버킷, 방화벽, VM을 순서대로
준비한다. VM 서비스 계정에는 BigQuery job 실행·조회 역할을 부여하고 VM OAuth
scope는 `cloud-platform`으로 제한 위임한다. 같은 설정이 이미 있으면 재사용하고,
동일 이름의 리소스가 다른 설정이면 중단한다.

데이터셋 생성 뒤 Google Cloud 콘솔에서 **Cloud Billing Standard usage export**의
대상으로 `local-social-native-wlk-0720.billing_monitor`를 한 번 선택해야 한다.
결제 내보내기 설정은 이 스크립트가 대신 변경하지 않는다.

생성되는 VM은 IAP 대역의 SSH만 허용하고 공개 애플리케이션 포트는 열지 않는다.
`startup.sh`는 Docker와 호스트 디렉터리만 준비하며 자격증명, Codex 로그인 정보,
애플리케이션 설정을 만들거나 서비스를 시작하지 않는다.

## 3. 변경 금지 범위

- 기존 Cloud Run 서비스와 그 revision/configuration
- 기존 Artifact Registry와 데이터 저장소
- 기존 임대주택 QStash 일정과 GitHub Actions workflow

배포, 로그인, 백업 복구, shadow/cutover 절차는 후속 운영 문서의 별도 체크포인트에서
진행한다.
