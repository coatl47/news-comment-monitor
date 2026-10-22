"""
OpenRouter 댓글 감성·주제 분류기

- 아직 분류되지 않았거나(빈 값, 미분석) 실패한(오류) 댓글만 배치로 보낸다.
- 환경변수: OPENROUTER_API_KEY, OPENROUTER_MODEL, OPENROUTER_BATCH_SIZE,
  OPENROUTER_TIMEOUT, OPENROUTER_MAX_PER_RUN, OPENROUTER_WORKERS
"""
from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

SENTIMENTS = ("긍정", "부정", "중립", "광고")
PENDING = ("", "미분석", "오류")
DEFAULT_CATEGORY = "기타"
MAX_TEXT = 300


def get_positive_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if not raw_value:
        return default
    try:
        value = int(raw_value)
        if value <= 0:
            raise ValueError
        return value
    except ValueError:
        logger.warning("%s 값이 양의 정수가 아니어서 기본값 %s를 사용합니다: %s", name, default, raw_value)
        return default


def normalize_sentiment_label(sentiment: str | None) -> str:
    value = (sentiment or "").strip()
    if value in SENTIMENTS:
        return value
    if value in ("스팸", "홍보"):
        return "광고"
    return "중립"


def normalize_category_label(category: str | None, categories: list[str]) -> str:
    value = (category or "").strip()
    if value in categories:
        return value
    # 공백·표기 차이 보정 ("기금 고갈" → "기금고갈")
    compact = value.replace(" ", "")
    for c in categories:
        if c.replace(" ", "") == compact:
            return c
    return DEFAULT_CATEGORY


def load_prompt(article: dict, root: str) -> str:
    prompt_file = article.get("prompt_file") or "prompt/prompt_base.txt"
    with open(os.path.join(root, prompt_file), encoding="utf-8") as f:
        template = f.read()
    categories = list(article.get("categories") or [])
    return template.format(
        article_title=article.get("title") or article.get("tab_label") or "",
        categories=", ".join(categories + [DEFAULT_CATEGORY]),
    )


def _client():
    from openai import OpenAI
    return OpenAI(
        api_key=os.getenv("OPENROUTER_API_KEY"),
        base_url="https://openrouter.ai/api/v1",
        timeout=get_positive_int_env("OPENROUTER_TIMEOUT", 60),
    )


def _classify_batch(client, model: str, system_prompt: str, batch: list[dict], categories: list[str]):
    items = [{"id": i + 1, "text": r["contents"][:MAX_TEXT]} for i, r in enumerate(batch)]
    try:
        resp = client.chat.completions.create(
            model=model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps({"comments": items}, ensure_ascii=False)},
            ],
        )
        content = resp.choices[0].message.content or ""
        content = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
        results = {int(x["id"]): x for x in json.loads(content).get("results", []) if "id" in x}
    except Exception as e:
        logger.warning("  배치 분류 실패 (%d건): %s", len(batch), e)
        for r in batch:
            r["sentiment"] = "오류"
        return 0

    done = 0
    for i, r in enumerate(batch):
        x = results.get(i + 1)
        if not x:
            r["sentiment"] = "오류"
            continue
        r["sentiment"] = normalize_sentiment_label(x.get("sentiment"))
        r["category"] = normalize_category_label(x.get("category"), categories)
        r["keyword"] = (x.get("keyword") or "").strip()[:20]
        done += 1
    return done


def analyze(rows: list[dict], article: dict, root: str) -> dict:
    """rows 를 제자리에서 분류한다. 반환: {"analyzed": n, "failed": n, "skipped": n}"""
    pending = [r for r in rows if (r.get("sentiment") or "") in PENDING]
    if not pending:
        return {"analyzed": 0, "failed": 0, "skipped": 0}

    if not os.getenv("OPENROUTER_API_KEY"):
        logger.warning("  OPENROUTER_API_KEY 가 없어 %d건을 미분석으로 둡니다.", len(pending))
        for r in pending:
            r["sentiment"] = "미분석"
        return {"analyzed": 0, "failed": 0, "skipped": len(pending)}

    # 최신 댓글부터 처리 (첫 실행에서 상한에 걸려도 최근 여론이 먼저 채워지도록)
    pending.sort(key=lambda r: int(r["comment_no"]), reverse=True)
    max_per_run = get_positive_int_env("OPENROUTER_MAX_PER_RUN", 3000)
    skipped = pending[max_per_run:]
    for r in skipped:
        r["sentiment"] = "미분석"
    pending = pending[:max_per_run]

    model = os.getenv("OPENROUTER_MODEL") or "openai/gpt-4o-mini"
    size = get_positive_int_env("OPENROUTER_BATCH_SIZE", 20)
    workers = get_positive_int_env("OPENROUTER_WORKERS", 4)
    categories = list(article.get("categories") or [])
    system_prompt = load_prompt(article, root)
    client = _client()

    batches = [pending[i:i + size] for i in range(0, len(pending), size)]
    logger.info("  AI 분류: %d건, %d배치 (%s)", len(pending), len(batches), model)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        done = sum(pool.map(lambda b: _classify_batch(client, model, system_prompt, b, categories), batches))
    return {"analyzed": done, "failed": len(pending) - done, "skipped": len(skipped)}
