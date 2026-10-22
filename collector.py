"""
증분 댓글 수집기

- 2시간 단위 경계(KST 00·02·…·22시)를 기준으로, 직전 경계(cutoff) 이전에 작성된 댓글 중
  지난 실행 이후 새로 달린 댓글만 가져온다.
- commentNo 는 시간순으로 증가하므로 state.json 의 last_comment_no 를 기준점으로 쓴다.
  cron 실행이 늦어져도 기준점 기준이라 누락·중복이 없다.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import naver_client

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
SLOT_HOURS = 2
MAX_PAGES = 100  # 최대 100페이지(약 1만 건)
PAGE_DELAY = 0.7

COMMENT_FIELDS = [
    "comment_no", "reg_time", "window", "contents",
    "sympathy", "antipathy", "reply_count",
    "sentiment", "category", "keyword", "collected_slot",
]
SNAPSHOT_FIELDS = [
    "slot", "collected_at", "naver_total", "naver_exposed", "deleted",
    "new_comments", "gender", "age",
]


# ---------- 시간 ----------

def now_kst() -> datetime:
    return datetime.now(KST)


def floor_slot(dt: datetime, hours: int = SLOT_HOURS) -> datetime:
    """dt 를 2시간 경계로 내림 (14:37 → 14:00, 15:10 → 14:00)"""
    dt = dt.astimezone(KST)
    return dt.replace(hour=dt.hour - dt.hour % hours, minute=0, second=0, microsecond=0)


def parse_reg_time(value: str) -> datetime:
    # 예: 2026-10-22T15:31:17+0900
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z").astimezone(KST)


def fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


# ---------- 저장소 ----------

def article_dir(data_root: str, article_id: str) -> str:
    path = os.path.join(data_root, article_id)
    os.makedirs(path, exist_ok=True)
    return path


def load_state(path: str) -> dict:
    fp = os.path.join(path, "state.json")
    if not os.path.exists(fp):
        return {}
    with open(fp, encoding="utf-8") as f:
        return json.load(f)


def save_state(path: str, state: dict) -> None:
    with open(os.path.join(path, "state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def load_comments(path: str) -> list[dict]:
    fp = os.path.join(path, "comments.csv")
    if not os.path.exists(fp):
        return []
    with open(fp, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in ("sympathy", "antipathy", "reply_count"):
            r[k] = int(r.get(k) or 0)
    return rows


def save_comments(path: str, rows: list[dict]) -> None:
    rows = sorted(rows, key=lambda r: int(r["comment_no"]))
    with open(os.path.join(path, "comments.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COMMENT_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def load_snapshots(path: str) -> list[dict]:
    fp = os.path.join(path, "snapshots.csv")
    if not os.path.exists(fp):
        return []
    with open(fp, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def append_snapshot(path: str, snap: dict) -> None:
    """슬롯당 1행. 같은 슬롯을 다시 돌리면 메타는 최신값으로, 신규 건수는 합산한다."""
    snapshots = load_snapshots(path)
    if snapshots and snapshots[-1]["slot"] == snap["slot"]:
        snap["new_comments"] = int(snapshots[-1].get("new_comments") or 0) + int(snap["new_comments"])
        snapshots[-1] = snap
    else:
        snapshots.append(snap)
    with open(os.path.join(path, "snapshots.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SNAPSHOT_FIELDS)
        w.writeheader()
        w.writerows(snapshots)


# ---------- 수집 ----------

def _to_row(c: dict, reg: datetime, slot: datetime) -> dict:
    return {
        "comment_no": str(c.get("commentNo")),
        "reg_time": fmt(reg) + reg.strftime(":%S"),
        "window": fmt(floor_slot(reg)),
        "contents": (c.get("contents") or "").strip().replace("\r", " "),
        "sympathy": int(c.get("sympathyCount") or 0),
        "antipathy": int(c.get("antipathyCount") or 0),
        "reply_count": int(c.get("replyCount") or 0),
        "sentiment": "",
        "category": "",
        "keyword": "",
        "collected_slot": fmt(slot),
    }


def collect_new(session, oid, aid, sid, last_no: int, cutoff: datetime):
    """
    최신순으로 페이지를 넘기며 last_no 보다 큰 댓글 중 cutoff 이전 작성분만 모은다.
    반환: (새 댓글 rows, 새 기준점 commentNo, page1 result)
    """
    rows, seen, cursor, first = [], set(), None, None
    new_last_no = last_no
    reached = False
    for page in range(1, MAX_PAGES + 1):
        result = naver_client.fetch_page(session, oid, aid, sid, page, cursor, sort="NEW")
        if page == 1:
            first = result
        comments = result.get("commentList") or []
        fresh = 0
        for c in comments:
            no = int(c.get("commentNo") or 0)
            if no in seen:
                continue
            seen.add(no)
            fresh += 1
            if no <= last_no:
                reached = True
                break
            reg = parse_reg_time(c["regTime"])
            if reg >= cutoff:
                continue  # 다음 경계에서 수집
            new_last_no = max(new_last_no, no)
            text = (c.get("contents") or "").strip()
            if c.get("deleted") or not text:
                continue
            rows.append(_to_row(c, reg, cutoff))
        logger.info("  page %d: 응답 %d건, 신규 누적 %d건", page, len(comments), len(rows))
        # 기준점 도달, 다음 페이지 없음, 같은 댓글만 반복되면 종료
        if reached or not comments or fresh == 0:
            break
        cursor = (result.get("morePage") or {}).get("next")
        if not cursor:
            break
        time.sleep(PAGE_DELAY)
    return rows, new_last_no, first or {}


def fetch_favorites(session, oid, aid, sid) -> dict[str, dict]:
    """공감순 상위 100건의 최신 공감/비공감/답글 수"""
    result = naver_client.fetch_page(session, oid, aid, sid, 1, sort="FAVORITE")
    out = {}
    for c in result.get("commentList") or []:
        out[str(c.get("commentNo"))] = {
            "sympathy": int(c.get("sympathyCount") or 0),
            "antipathy": int(c.get("antipathyCount") or 0),
            "reply_count": int(c.get("replyCount") or 0),
        }
    return out


def snapshot_from(result: dict, slot: datetime, collected_at: datetime, new_count: int) -> dict:
    count = result.get("count") or {}
    graph = result.get("graph") or {}
    return {
        "slot": fmt(slot),
        "collected_at": fmt(collected_at),
        "naver_total": count.get("comment", ""),
        "naver_exposed": count.get("exposeCount", ""),
        "deleted": (count.get("delCommentByUser") or 0) + (count.get("delCommentByMon") or 0),
        "new_comments": new_count,
        "gender": json.dumps(graph.get("gender"), ensure_ascii=False) if graph.get("gender") else "",
        "age": json.dumps([{"age": a.get("age"), "value": a.get("value")} for a in graph.get("old") or []],
                          ensure_ascii=False) if graph.get("old") else "",
    }


def run_collection(article: dict, path: str, now: datetime) -> dict:
    """기사 1건 증분 수집. 반환: 실행 결과 요약"""
    oid, aid, sid = naver_client.parse_url(article["url"])
    session = naver_client.make_session(oid, aid, sid)
    state = load_state(path)
    cutoff = floor_slot(now)

    if not state.get("article_meta"):
        try:
            state["article_meta"] = naver_client.fetch_article_meta(session, oid, aid)
        except Exception as e:  # 메타는 부가 정보라 실패해도 진행
            logger.warning("  기사 메타 조회 실패: %s", e)

    last_no = int(state.get("last_comment_no") or 0)
    rows = load_comments(path)
    known = {r["comment_no"]: r for r in rows}

    new_rows, new_last_no, first = collect_new(session, oid, aid, sid, last_no, cutoff)
    added = 0
    for r in new_rows:
        if r["comment_no"] not in known:
            known[r["comment_no"]] = r
            added += 1

    # 공감 수 갱신 (오래된 댓글의 공감 변화 반영)
    try:
        time.sleep(PAGE_DELAY)
        for no, counts in fetch_favorites(session, oid, aid, sid).items():
            if no in known:
                known[no].update(counts)
    except Exception as e:
        logger.warning("  공감순 갱신 실패: %s", e)

    # 댓글을 먼저 저장한 뒤 기준점을 옮긴다 (분석 단계에서 실패해도 수집분은 보존)
    rows = list(known.values())
    save_comments(path, rows)
    append_snapshot(path, snapshot_from(first, cutoff, now, added))

    state.update({
        "last_comment_no": str(new_last_no),
        "last_cutoff": fmt(cutoff),
        "last_run_at": fmt(now),
        "last_success_at": fmt(now),
        "last_error": None,
    })
    save_state(path, state)
    logger.info("  신규 %d건 (누적 %d건, 기준점 %s, cutoff %s)", added, len(rows), new_last_no, fmt(cutoff))
    return {"rows": rows, "added": added, "state": state}
