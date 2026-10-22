"""
2시간 주기 업데이트 작업 (GitHub Actions / 로컬 공용)

    python update_job.py                    # 활성 기사 전체
    python update_job.py --article <id>     # 특정 기사만
    python update_job.py --no-analyze       # AI 분류 생략
    python update_job.py --now "2026-10-22 14:05"   # 기준 시각 지정(테스트용)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

import aggregator
import analyzer
import collector

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "dashboard_config.json")
DATA_ROOT = os.path.join(ROOT, "data")

logger = logging.getLogger("update_job")


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def process_article(article: dict, now, do_analyze: bool) -> dict:
    path = collector.article_dir(DATA_ROOT, article["id"])
    logger.info("[%s] %s", article["id"], article.get("tab_label", ""))

    if article.get("collect_enabled", True):
        try:
            rows = collector.run_collection(article, path, now)["rows"]
        except Exception as e:
            logger.exception("  수집 실패")
            state = collector.load_state(path)
            state.update({"last_error": f"{type(e).__name__}: {e}"[:300],
                          "last_error_at": collector.fmt(now),
                          "last_run_at": collector.fmt(now)})
            collector.save_state(path, state)
            rows = collector.load_comments(path)
    else:
        logger.info("  수집 중지된 기사 (collect_enabled=false)")
        rows = collector.load_comments(path)

    if do_analyze and rows:
        result = analyzer.analyze(rows, article, ROOT)
        logger.info("  분류 결과: %s", result)
        collector.save_comments(path, rows)

    state = collector.load_state(path)
    if not state.get("last_cutoff"):
        logger.warning("  수집 이력이 없어 summary 를 만들지 않습니다.")
        return None
    summary = aggregator.build_summary(article, rows, state, path, now)
    aggregator.write_summary(path, summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--article", help="이 id 의 기사만 처리")
    parser.add_argument("--no-analyze", action="store_true", help="AI 분류 생략")
    parser.add_argument("--now", help='기준 시각 (KST, "YYYY-MM-DD HH:MM")')
    args = parser.parse_args()

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")

    now = aggregator.parse_dt(args.now) if args.now else collector.now_kst()
    config = load_config()
    articles = [a for a in config.get("articles", []) if a.get("enabled", True)]
    if args.article:
        articles = [a for a in articles if a["id"] == args.article]
        if not articles:
            logger.error("활성 기사 중 id=%s 가 없습니다.", args.article)
            return 1

    logger.info("기준 시각 %s (cutoff %s), 대상 기사 %d건",
                collector.fmt(now), collector.fmt(collector.floor_slot(now)), len(articles))

    summaries = []
    for article in articles:
        summary = process_article(article, now, not args.no_analyze)
        if summary:
            summaries.append(summary)

    # --article 로 일부만 돌려도 index 는 전체 기사 기준으로 다시 만든다
    if args.article:
        summaries = []
        for a in config.get("articles", []):
            fp = os.path.join(DATA_ROOT, a["id"], "summary.json")
            if a.get("enabled", True) and os.path.exists(fp):
                with open(fp, encoding="utf-8") as f:
                    summaries.append(json.load(f))
    aggregator.write_index(DATA_ROOT, config, summaries, now)
    logger.info("완료: 기사 %d건 summary 갱신", len(summaries))
    return 0


if __name__ == "__main__":
    sys.exit(main())
