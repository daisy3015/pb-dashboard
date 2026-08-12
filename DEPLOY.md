# pb-dashboard 배포 — 팀 전체 실시간 조회

`dashboard.py` 를 인터넷에 올려 팀원들이 각자 브라우저로 최신 노션 데이터를 보게 하는 절차입니다.

---

## 0. 먼저 — 이건 공개하면 안 되는 데이터입니다

대시보드에는 이런 게 들어 있습니다.

- 제조원가 (`4,400원(▼80원)`), 발주금액 (`330,983,400원`)
- 단가 인상 협상 내용 (`20,000 → 23,900원/set`)
- 제조사 이관 검토, 미출시 신제품 파이프라인

**URL 만 알면 누구나 볼 수 있는 상태로 두면 안 됩니다.** `robots.txt` 는 검색엔진에 "색인하지 말아 달라" 고 부탁하는 것일 뿐, 접근을 막지 못합니다.

그래서 `dashboard.py` 는 **외부 주소(`0.0.0.0`)로 열리는데 비밀번호가 없으면 아예 시작하지 않습니다.**

```
중단: 외부에 열려 있는데 DASHBOARD_PASSWORD 가 설정되지 않았습니다.
```

아래 절차는 이 비밀번호를 설정하는 단계를 포함합니다.

---

## 1. 노션 토큰 준비

1. https://www.notion.so/profile/integrations → **New integration**
2. 만들어진 **Internal Integration Secret** (`ntn_...`) 복사
3. 노션에서 `상품기획 - 신제품` 데이터베이스 열기 → 우측 상단 **⋯ → 연결(Connections)** → 방금 만든 인테그레이션 추가

> 3번을 빼먹으면 조회가 0건으로 나오고 대시보드가 "노션 항목이 급감했습니다" 배너를 띄웁니다.

**토큰을 저장소에 커밋하지 마세요.** 아래 절차는 전부 플랫폼의 비밀 변수(secret)로 넣습니다.

---

## 2. Render 배포 — 무료 (권장)

Render 는 2026년 8월 현재 무료 웹 서비스를 유지하고 있습니다.
저장소 최상단에 `render.yaml` 이 이미 들어 있어 대부분 자동으로 잡힙니다.

1. 이 저장소에 `dashboard.py` · `render.yaml` 을 push
2. https://dashboard.render.com → **New → Blueprint** → `daisy3015/pb-dashboard` 선택
3. Render 가 `render.yaml` 을 읽고 서비스를 제안합니다 → **Apply**
4. 배포 중 `NOTION_TOKEN` 값을 물어봅니다 → 1단계에서 복사한 `ntn_...` 붙여넣기
5. 배포가 끝나면 **Environment** 탭에서 `DASHBOARD_PASSWORD` 값을 확인 (Render 가 자동 생성해 둡니다). 이걸 팀에 공유합니다
6. `https://pb-dashboard-xxxx.onrender.com` 접속 → 브라우저가 아이디/비밀번호를 물어봄 → `pb` / 위 비밀번호

### 알아둘 것

| 항목 | 내용 |
|---|---|
| 콜드 스타트 | **15분간 아무도 안 들어오면 잠들고, 다음 접속 때 깨어나는 데 약 1분** 걸립니다 |
| 월 사용시간 | 워크스페이스당 **750 시간/월**. 초과하면 그 달 남은 기간 정지됩니다 |
| 자동 재배포 | `main` 에 push 하면 자동으로 다시 배포됩니다 |
| HTTPS | 자동으로 붙습니다 (비밀번호가 평문으로 안 날아감) |

> 아침 첫 접속 1분이 거슬리면 UptimeRobot 등으로 **업무시간에만** 5분 간격 핑을 걸어두세요.
> 24시간 계속 깨워두면 월 744시간이라 750시간 한도에 아슬아슬하게 붙습니다 — 권하지 않습니다.

---

## 3. Fly.io 배포 — 유료 (콜드 스타트가 싫을 때)

Fly.io 는 **신규 계정 무료 요금제를 없앴습니다.** Pay-as-you-go 로 가장 작은 머신이 월 $2~3 수준입니다.
그 대신 지연이 짧고(도쿄 리전) 잠들었다 깨는 속도가 Render 보다 빠릅니다.

```bash
brew install flyctl          # 또는 https://fly.io/docs/flyctl/install/
fly auth login
fly launch --no-deploy       # fly.toml 이 이미 있으니 덮어쓰지 않도록 주의

fly secrets set NOTION_TOKEN='ntn_...'
fly secrets set DASHBOARD_USER='pb'
fly secrets set DASHBOARD_PASSWORD="$(python3 -c 'import secrets;print(secrets.token_urlsafe(18))')"

fly deploy
fly open
```

`fly.toml` 의 `auto_stop_machines = "stop"` 때문에 트래픽이 없으면 머신이 멈춰 요금이 거의 안 나옵니다.

---

## 4. 무엇이 실시간이고 무엇이 아닌가

| 데이터 | 건수 | 갱신 방식 |
|---|---|---|
| **노션** (개발 파이프라인) | 11 | **실시간** — 페이지 열 때마다 API 조회 (최소 간격 120초) |
| 구글시트 (제품 스펙) | 55 | 저장소의 `build/sheet.json` — push 해야 반영 |
| 슬랙 (발주) | 62 | 저장소의 `build/orders.json` — push 해야 반영 |

즉 **기존 일일 갱신 작업이 없어지지는 않습니다.** 다만 노션 부분은 자동이 되고,
`main` 에 push 하면 Render 가 알아서 재배포하므로 "빌드해서 index.html 올리기" 단계는 사라집니다.

시트·발주까지 실시간으로 만들 수 있습니다(구글 서비스 계정 + 슬랙 봇 토큰). 다만 그러면
사람이 오타·매칭 오류를 잡는 지점이 사라지므로, 발주 쪽은 지금처럼 스냅샷으로 두는 편이 안전합니다.

---

## 5. 운영 메모

- **비밀번호 공유**: 슬랙 DM 이나 1Password 등으로. 공개 채널에 올리지 마세요
- **퇴사자 발생 시**: `DASHBOARD_PASSWORD` 만 바꾸고 재배포하면 됩니다
- **토큰 유출 의심 시**: 노션 인테그레이션 페이지에서 secret 을 재발급하고 플랫폼 변수만 교체
- **더 엄격하게 가려면**: Cloudflare Access 를 앞에 두면 회사 구글 계정으로만 열게 할 수 있습니다 (무료 50인)
- **깃허브 페이지는?** 계속 두면 **옛 스냅샷이 인증 없이 공개된 채로 남습니다.** 실시간 서버로 옮기면
  `daisy3015.github.io/pb-dashboard` 는 저장소 Settings → Pages 에서 끄는 게 좋습니다

---

## 6. 배포 전 로컬 확인

```bash
export NOTION_TOKEN='ntn_...'
python3 dashboard.py --once        # 노션 연결·건수만 확인하고 종료
python3 dashboard.py               # http://127.0.0.1:8000 (로컬은 비밀번호 없이 열림)
```

`--once` 가 `총 항목 65건 (시트 54 / 노션 11)` 처럼 나오면 배포해도 됩니다.
