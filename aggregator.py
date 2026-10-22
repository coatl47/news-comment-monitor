"""
대시보드용 집계: data/<article_id>/summary.json, data/index.json
"""
from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from collector import KST, SLOT_HOURS, closes_at, fmt, load_snapshots

SENTIMENT_ORDER = ["긍정", "중립", "부정", "기타"]
OPINION = ("긍정", "중립", "부정")
RECENT_LIMIT = 300
TOP_KEYWORDS = 20
SNIPPET = 80


def bucket(sentiment: str) -> str:
    """광고·미분석·오류 → 기타"""
    return sentiment if sentiment in OPINION else "기타"


def ratio(part: int, whole: int):
    return round(part / whole * 100, 1) if whole else None


def parse_dt(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=KST)


def window_label(start: datetime) -> str:
    end = start + timedelta(hours=SLOT_HOURS)
    return f"{start.month}/{start.day} {start.hour:02d}–{end.hour:02d}시"


def comment_view(r: dict, full: bool = False) -> dict:
    text = r["contents"]
    return {
        "no": r["comment_no"],
        "time": r["reg_time"][:16],
        "text": text if full else (text[:SNIPPET] + ("…" if len(text) > SNIPPET else "")),
        "sentiment": r.get("sentiment") or "미분석",
        "category": r.get("category") or "",
        "keyword": r.get("keyword") or "",
        "sympathy": r["sympathy"],
        "antipathy": r["antipathy"],
        "reply": r["reply_count"],
    }


def counts_of(rows) -> dict:
    c = Counter(bucket(r.get("sentiment") or "") for r in rows)
    return {k: c.get(k, 0) for k in SENTIMENT_ORDER}


def neg_ratio(counts: dict):
    return ratio(counts["부정"], sum(counts[k] for k in OPINION))


def build_timeline(rows: list[dict], cutoff: datetime) -> list[dict]:
    by_window = defaultdict(list)
    for r in rows:
        by_window[r["window"]].append(r)
    if not by_window:
        return []
    start = parse_dt(min(by_window))
    out, cumulative, t = [], 0, start
    while t < cutoff:
        key = fmt(t)
        items = by_window.get(key, [])
        cumulative += len(items)
        counts = counts_of(items)
        top = max((r for r in items if r.get("sentiment") != "광고"),
                  key=lambda r: r["sympathy"], default=None)
        out.append({
            "window": key,
            "label": window_label(t),
            "total": len(items),
            "cumulative": cumulative,
            **counts,
            "neg_ratio": neg_ratio(counts),
            "pos_ratio": ratio(counts["긍정"], sum(counts[k] for k in OPINION)),
            "top": comment_view(top) if top else None,
        })
        t += timedelta(hours=SLOT_HOURS)
    return out


def build_demographics(snapshots: list[dict]):
    for s in reversed(snapshots):
        if s.get("gender") or s.get("age"):
            return {
                "as_of": s["collected_at"],
                "gender": json.loads(s["gender"]) if s.get("gender") else None,
                "age": json.loads(s["age"]) if s.get("age") else None,
            }
    return None


def build_representative(rows: list[dict], categories: list[str]) -> dict:
    ranked = sorted((r for r in rows if r.get("sentiment") in OPINION),
                    key=lambda r: (r["sympathy"], -r["antipathy"]), reverse=True)
    top = ranked[:5]
    shown = {r["comment_no"] for r in top}  # TOP 5 에 나온 댓글은 주제별에서 빼고 다음 순위로 채운다
    by_category = {}
    for cat in categories + ["기타"]:
        bucket_rows = [r for r in ranked if r.get("category") == cat and r["comment_no"] not in shown]
        entry = {s: [comment_view(r, full=True) for r in bucket_rows if r["sentiment"] == s][:2]
                 for s in ("긍정", "부정")}
        if entry["긍정"] or entry["부정"]:
            by_category[cat] = entry
    return {"top": [comment_view(r, full=True) for r in top], "by_category": by_category}


def build_summary(article: dict, rows: list[dict], state: dict, path: str, now: datetime) -> dict:
    cutoff = parse_dt(state["last_cutoff"]) if state.get("last_cutoff") else None
    snapshots = load_snapshots(path)
    latest = snapshots[-1] if snapshots else {}
    timeline = build_timeline(rows, cutoff) if cutoff else []
    last_w = timeline[-1] if timeline else None
    prev_w = timeline[-2] if len(timeline) > 1 else None

    counts = counts_of(rows)
    sym = Counter()
    for r in rows:
        sym[bucket(r.get("sentiment") or "")] += r["sympathy"]
    sym_counts = {k: sym.get(k, 0) for k in SENTIMENT_ORDER}

    categories = list(article.get("categories") or [])
    cat_counts = defaultdict(lambda: {k: 0 for k in SENTIMENT_ORDER})
    for r in rows:
        if r.get("sentiment") in OPINION:
            cat_counts[r.get("category") or "기타"][r["sentiment"]] += 1

    kw = defaultdict(lambda: {"count": 0, **{k: 0 for k in SENTIMENT_ORDER}})
    for r in rows:
        k = (r.get("keyword") or "").strip()
        if k and r.get("sentiment") in OPINION:
            kw[k]["count"] += 1
            kw[k][r["sentiment"]] += 1
    keywords = sorted(({"keyword": k, **v} for k, v in kw.items()), key=lambda x: -x["count"])[:TOP_KEYWORDS]

    deleted_prev = int(snapshots[-2]["deleted"] or 0) if len(snapshots) > 1 else None
    meta = state.get("article_meta") or {}
    close = closes_at(article, state)
    closed = bool(state.get("closed"))
    sentiment_state = Counter(r.get("sentiment") or "미분석" for r in rows)

    return {
        "article": {
            "id": article["id"],
            "tab_label": article.get("tab_label") or article["id"],
            "title": article.get("title") or meta.get("title") or "",
            "press": article.get("press") or meta.get("press") or "",
            "published_at": article.get("published_at") or meta.get("published_at") or "",
            "url": article["url"],
            "collect_enabled": article.get("collect_enabled", True),
            "closed": closed,
            "closes_at": fmt(close) if close else None,
        },
        "generated_at": fmt(now),
        "cutoff": state.get("last_cutoff"),
        "next_update": fmt(cutoff + timedelta(hours=SLOT_HOURS)) if cutoff and not closed else None,
        "status": {
            "last_success_at": state.get("last_success_at"),
            "last_error": state.get("last_error"),
            "last_error_at": state.get("last_error_at"),
        },
        "kpi": {
            "naver_total": int(latest["naver_total"]) if latest.get("naver_total") else None,
            "deleted": int(latest["deleted"]) if latest.get("deleted") else 0,
            "deleted_delta": (int(latest["deleted"] or 0) - deleted_prev) if deleted_prev is not None else None,
            "collected": len(rows),
            "last_window_label": last_w["label"] if last_w else None,
            "last_window_new": last_w["total"] if last_w else 0,
            "prev_window_new": prev_w["total"] if prev_w else None,
            "neg_ratio_last": last_w["neg_ratio"] if last_w else None,
            "neg_ratio_prev": prev_w["neg_ratio"] if prev_w else None,
            "neg_ratio_all": neg_ratio(counts),
            "neg_ratio_sympathy": neg_ratio(sym_counts),
            "ads": sentiment_state.get("광고", 0),
            "unanalyzed": sum(sentiment_state.get(k, 0) for k in ("미분석", "오류", "")),
        },
        "timeline": timeline,
        "snapshots": [{"slot": s["slot"], "collected_at": s["collected_at"],
                       "naver_total": int(s["naver_total"]) if s.get("naver_total") else None,
                       "deleted": int(s["deleted"] or 0)} for s in snapshots],
        "sentiment": {"count": counts, "sympathy": sym_counts},
        "category": {c: cat_counts[c] for c in sorted(cat_counts, key=lambda c: -sum(cat_counts[c].values()))},
        "demographics": build_demographics(snapshots),
        "keywords": keywords,
        "representative": build_representative(rows, categories),
        "recent": [comment_view(r, full=True)
                   for r in sorted(rows, key=lambda r: int(r["comment_no"]), reverse=True)[:RECENT_LIMIT]],
    }


def write_summary(path: str, summary: dict) -> None:
    with open(os.path.join(path, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)


def write_index(data_root: str, config: dict, summaries: list[dict], now: datetime) -> None:
    """종합 탭용 경량 인덱스"""
    articles = []
    for s in summaries:
        articles.append({
            **s["article"],
            "cutoff": s["cutoff"],
            "next_update": s["next_update"],
            "status": s["status"],
            "kpi": s["kpi"],
            "sentiment": s["sentiment"],
            "timeline": [{k: w[k] for k in ("window", "label", "total", "cumulative", "neg_ratio")}
                         for w in s["timeline"]],
        })
    index = {
        "dashboard_title": config.get("dashboard_title", "네이버 뉴스 댓글 여론 모니터링"),
        "generated_at": fmt(now),
        "slot_hours": SLOT_HOURS,
        "articles": articles,
    }
    with open(os.path.join(data_root, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=1)
