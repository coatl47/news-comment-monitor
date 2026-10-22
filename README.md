# 네이버 뉴스 댓글 여론 모니터링

지정한 네이버 뉴스 기사의 댓글을 **2시간마다(KST 00·02·…·22시) 새 댓글만** 모아서 OpenRouter(gpt-4o-mini)로 감성·주제를 분류하고, GitHub Pages 정적 대시보드로 보여줍니다.

## 구성

| 파일 | 역할 |
|---|---|
| `dashboard_config.json` | 대시보드 제목, 모니터링할 기사 목록 |
| `naver_client.py` | 네이버 댓글 API 래퍼 (`naver_comments.py` 기반) |
| `collector.py` | 증분 수집: 마지막으로 받은 `commentNo` 이후, 직전 2시간 경계 이전에 작성된 댓글만 저장 |
| `analyzer.py` | 미분류·실패 댓글만 OpenRouter 로 배치 분류 (긍정/중립/부정/광고 + 주제 + 키워드) |
| `aggregator.py` | 대시보드용 `summary.json`, `index.json` 생성 |
| `update_job.py` | 위 단계를 순서대로 실행 (GitHub Actions·로컬 공용) |
| `prompt/prompt_base.txt` | 분류 프롬프트 (`{article_title}`, `{categories}` 치환) |
| `index.html` | 대시보드 (Plotly, PapaParse) |
| `data/<기사 id>/` | `comments.csv`(댓글+분류), `snapshots.csv`(수집 시점별 총 댓글·삭제·성별/연령), `state.json`, `summary.json` |

### 2시간 경계 수집 방식
- 14:05에 실행되면 기준 시각(cutoff)은 14:00입니다. 14:00 이전에 작성된 새 댓글만 저장하고, 14:00 이후 댓글은 16시 회차로 넘깁니다. 그래서 차트의 각 2시간 구간이 항상 꽉 찬 상태로 집계됩니다.
- 기준점이 시간이 아니라 `commentNo`라서 cron 실행이 늦어지거나 한 번 빠져도 누락·중복이 생기지 않습니다.
- 매 회차마다 공감순 상위 100건의 공감/비공감 수도 다시 받아 대표 댓글 선정에 반영합니다.

### 자동 마감
- 기사 게재 후 `close_after_days`(기본 20일)가 지난 뒤 첫 2시간 경계에서 마감합니다. 예: 10/22 06:00:40 게재 → 11/11 08:00 마감.
- 마감 회차는 마감 시각까지만 댓글을 모으고 `state.json` 에 `closed: true` 를 남깁니다. 실행이 늦어져도 마감 시각 이후 댓글은 넣지 않습니다.
- 마감 후에는 수집·분류를 하지 않고, 대시보드에 "마감" 표시만 합니다. 다시 열려면 `state.json` 의 `closed` 를 지우고 `close_after_days` 를 늘리세요.

## 기사 추가하기

`dashboard_config.json` 의 `articles` 에 항목을 추가합니다.

```json
{
  "id": "pension_marriage_20261022",
  "close_after_days": 20,
  "tab_label": "국민연금 낡은 구조",
  "url": "https://n.news.naver.com/article/001/0016392780?ntype=RANKING",
  "categories": ["성별·혼인 형평성", "제도 개선(1인 1연금)", "연금 재정·고갈", "노후 불안", "정부·정책 평가"],
  "prompt_file": "prompt/prompt_base.txt",
  "enabled": true,
  "collect_enabled": true
}
```

- `id`: 영문·숫자·밑줄. `data/<id>/` 폴더 이름이 됩니다.
- `categories`: AI가 고를 주제 목록. 여기에 없는 값은 "기타"로 처리됩니다.
- `close_after_days`: 생략하면 최상위 값(기본 20)을 씁니다. `0` 이면 마감하지 않습니다.
- `collect_enabled: false`: 수집·분석을 멈추고 대시보드에 표시만 합니다. `enabled: false`이면 탭에서도 숨깁니다.
- 제목·언론사·게재 시각은 첫 수집 때 기사 페이지에서 자동으로 채웁니다. 직접 지정하려면 `title`, `press`, `published_at` 를 넣으세요.

## GitHub 배포

1. GitHub에 저장소를 만들고 push 합니다. 무료 플랜에서 Pages를 쓰려면 저장소가 public이어야 합니다.
2. **Settings → Secrets and variables → Actions** 에 `OPENROUTER_API_KEY` 를 등록합니다.
3. **Settings → Actions → General → Workflow permissions** 를 *Read and write* 로 설정합니다.
4. **Settings → Pages** 에서 Source를 *Deploy from a branch*, `main` / `(root)` 로 설정합니다.
5. **Actions → Auto Update Naver Comment Data → Run workflow** 로 한 번 수동 실행해서 네이버 수집이 되는지 확인합니다.
   이후에는 2시간마다(KST 짝수 정시 5분) 자동 실행됩니다. 수동 실행으로 이미 처리한 구간이면 커밋 없이 바로 끝납니다(`--skip-if-current`).
   GitHub 예약 실행은 수십 분씩 늦거나 한 회차를 건너뛸 수 있습니다. 건너뛴 구간의 댓글은 다음 회차에 함께 수집되므로 누락은 없습니다.

> GitHub Actions는 해외 IP라서 네이버가 막을 수 있습니다. 실패하면 대시보드 탭에 "⚠ 수집 지연" 이 표시되고, `data/<id>/state.json` 의 `last_error` 에 원인이 남습니다. 이때는 아래처럼 Mac에서 같은 작업을 돌리면 됩니다.

## 로컬 실행

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
echo 'OPENROUTER_API_KEY=sk-or-...' > .env      # 없으면 '미분석'으로 남고 다음 실행 때 분류됨

.venv/bin/python update_job.py                  # 전체 기사
.venv/bin/python update_job.py --article <id>   # 특정 기사만
.venv/bin/python update_job.py --no-analyze     # 수집만
.venv/bin/python update_job.py --skip-if-current  # 이번 구간을 이미 처리했으면 종료

python3 -m http.server 8000                      # http://localhost:8000 에서 대시보드 확인
```

### Mac에서 2시간마다 실행 (Actions 차단 시 대안)

`~/Library/LaunchAgents/com.news-comment-monitor.plist` 에 짝수 정시 5분마다 아래 명령을 실행하도록 등록합니다.

```bash
cd <저장소 경로> && git pull --rebase && .venv/bin/python update_job.py && \
  git add data && (git diff --staged --quiet || (git commit -m "Auto-update data" && git push))
```

## 환경 변수

| 이름 | 기본값 | 설명 |
|---|---|---|
| `OPENROUTER_API_KEY` | — | 없으면 분류를 건너뜀 |
| `OPENROUTER_MODEL` | `openai/gpt-4o-mini` | |
| `OPENROUTER_BATCH_SIZE` | 20 | 요청당 댓글 수 |
| `OPENROUTER_WORKERS` | 4 | 동시 요청 수 |
| `OPENROUTER_MAX_PER_RUN` | 3000 | 회차당 최대 분류 건수 (최신 댓글 우선, 나머지는 다음 회차) |

## 주의
- 네이버 비공식 댓글 엔드포인트를 사용합니다. 소량, 개인 분석 용도로만 쓰세요.
- 작성자 닉네임/ID는 수집하지 않습니다. 답글과 삭제된 댓글은 분석에서 제외합니다.
- 성별·연령 분포는 네이버가 제공할 때만 표시됩니다. 댓글이 적은 기사는 제공되지 않습니다.
