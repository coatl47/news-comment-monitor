"""
네이버 뉴스 댓글 API 래퍼 (naver_comments.py 기반)

주의: 네이버 비공식 댓글 엔드포인트를 사용합니다. 소량, 개인 분석 용도로만 사용하세요.
작성자 닉네임/ID는 수집하지 않습니다.
"""
from __future__ import annotations

import html
import json
import re

import requests

SECTION_TEMPLATE = {
    "100": "default_politics",
    "101": "default_economy",
    "102": "default_society",
    "103": "default_life",
    "104": "default_world",
    "105": "default_it",
}

API = "https://apis.naver.com/commentBox/cbox/web_naver_list_jsonp.json"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


class NaverApiError(RuntimeError):
    pass


def parse_url(url: str):
    """기사 URL → (언론사ID, 기사ID, 섹션ID)"""
    m = re.search(r"/article/(?:comment/)?(\d{3})/(\d{10})", url)
    if not m:
        raise ValueError("기사 URL에서 언론사ID/기사ID를 찾지 못했습니다: " + url)
    sid = re.search(r"[?&]sid=(\d+)", url)
    return m.group(1), m.group(2), (sid.group(1) if sid else "102")


def make_session(oid: str, aid: str, sid: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Referer": f"https://n.news.naver.com/mnews/article/comment/{oid}/{aid}?sid={sid}",
    })
    return s


def fetch_page(session, oid, aid, sid, page, next_cursor=None, sort="NEW"):
    """댓글 한 페이지(최대 100건). sort: NEW(최신순) / FAVORITE(공감순)"""
    params = {
        "ticket": "news",
        "templateId": SECTION_TEMPLATE.get(sid, "default_society"),
        "pool": "cbox5",
        "_callback": "cb",
        "lang": "ko",
        "country": "KR",
        "objectId": f"news{oid},{aid}",
        "pageSize": 100,
        "indexSize": 10,
        "listType": "OBJECT",
        "pageType": "more",
        "page": page,
        "initialize": "true" if page == 1 else "false",
        "followSize": 5,
        "useAltSort": "true",
        "replyPageSize": 20,
        "sort": sort,
        "includeAllStatus": "true",
    }
    if next_cursor:
        params["moreParam.direction"] = "next"
        params["moreParam.next"] = next_cursor
    r = session.get(API, params=params, timeout=15)
    r.raise_for_status()
    m = re.search(r"^[^(]*\((.*)\)\s*;?\s*$", r.text, re.S)
    if not m:
        raise NaverApiError("예상치 못한 응답 형식: " + r.text[:200])
    data = json.loads(m.group(1))
    if not data.get("success", True):
        raise NaverApiError("API 오류: " + json.dumps(data, ensure_ascii=False)[:300])
    return data.get("result", {})


def fetch_article_meta(session, oid: str, aid: str) -> dict:
    """기사 제목·언론사·게재시각 (기사 페이지 meta 태그에서 추출)"""
    r = session.get(f"https://n.news.naver.com/mnews/article/{oid}/{aid}", timeout=15)
    r.raise_for_status()
    text = r.text

    def meta(pattern):
        m = re.search(pattern, text)
        return html.unescape(m.group(1)).strip() if m else None

    return {
        "title": meta(r'<meta property="og:title" content="([^"]+)"'),
        "press": meta(r'<meta name="twitter:creator" content="([^"]+)"')
                 or meta(r'<meta property="og:article:author" content="([^"|]+)'),
        "published_at": meta(r'data-date-time="([^"]+)"'),
    }
