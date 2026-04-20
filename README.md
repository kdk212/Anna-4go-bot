# Policy Digest Telegram Bot

매일 오전 9시에 중앙정부 정책 관련 사설, 칼럼, 논평, 분석 기사를 모아 텔레그램으로 보냅니다.

## Files

- `policy_digest_telegram.py`: RSS 수집, 필터링, 텔레그램 발송 스크립트
- `run_policy_digest.bat`: Windows 작업 스케줄러에서 실행할 배치 파일
- `.env`: 텔레그램 토큰과 채팅 ID 설정 파일
- `policy_digest_state.json`: 이미 보낸 링크를 기록해 중복 발송을 줄이는 파일
- `logs/policy_digest.log`: 실행 로그

## Manual Test

```powershell
$env:DRY_RUN='1'; python .\policy_digest_telegram.py
```

실제로 텔레그램으로 보내려면:

```powershell
python .\policy_digest_telegram.py
```

## Schedule

Windows 작업 스케줄러 작업 이름은 `PolicyDigestTelegram`입니다.
현재 설정은 매일 오전 9시 실행입니다.

## GitHub Actions

PC가 꺼져 있어도 보내려면 GitHub Actions를 사용합니다.

등록해야 하는 repository secrets:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID` 개인방과 그룹방에 함께 보내려면 쉼표로 구분합니다. 예: `694726450,-5135282922`

워크플로 파일은 `.github/workflows/policy-digest.yml`입니다.
실행 시간은 매일 `00:00 UTC`, 한국시간 오전 9시입니다.
