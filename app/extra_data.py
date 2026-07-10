from __future__ import annotations

import json
import os
import re
import time
import html as html_lib
from datetime import date, datetime
from typing import Any
from urllib.parse import urljoin

import requests
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, and_, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from .core import (
    Base,
    CostCalculation,
    GeneratedFile,
    IngredientLine,
    ProductRequest,
    RecipeDraft,
    ScreeningFinding,
    ToolRun,
    api_error,
    as_json,
    build_process_plan,
    build_recipe_risk_basis,
    current_visitor_id,
    db_session,
    engine,
    from_json,
    json_payload,
    load_owned_generated_file_or_404,
    load_owned_request_or_404,
    make_hash,
    now_utc,
    query_int,
    record_tool_run,
)
from .price_crawler import (
    PriceObservation,
    PriceSyncError,
    TrendSnapshot,
    collect_public_crawl_prices,
    collect_kamis_prices,
    env_int,
    start_periodic_price_sync,
)


PRICE_SYNC_SCHEDULER_STARTED = False
TREND_CACHE_TTL_SECONDS = 600
TREND_REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0 CBT-TrendCrawler"}
TREND_CACHE: dict[str, Any] = {"expires_at": 0.0, "items": [], "mode": "empty"}
TREND_SOURCES = [
    {
        "name": "KATI",
        "url": "https://www.kati.net/board/exportNewsList.do",
        "kind": "kati",
    },
    {
        "name": "aTFIS",
        "url": "https://www.atfis.or.kr/home/board/FB0002.do",
        "kind": "atfis",
    },
    {
        "name": "식품저널",
        "url": "https://www.foodnews.co.kr/news/articleList.html?sc_section_code=S1N1&view_type=sm",
        "kind": "foodnews",
    },
]
TREND_KEYWORDS = ["저당", "단백질", "간편식", "HMR", "K푸드", "수출", "비건", "고령친화", "푸드테크", "온라인", "냉동", "소스", "음료", "디저트"]
TREND_BLOCKED_TITLES = [
    "카카오톡",
    "로그인",
    "회원가입",
    "검색",
    "전체",
    "기사",
    "뉴스",
    "구독",
    "메뉴",
    "바로가기",
    "개인정보",
]
TREND_BLOCKED_BODY_PARTS = [
    "유료회원전용기사",
    "로그인 또는 회원가입을 해주세요.",
    "좋아요 0",
    "댓글 0",
    "PREV",
    "NEXT",
    "목록",
]
TREND_IMAGE_SKIP_WORDS = (
    "logo",
    "icon",
    "banner",
    "share",
    "sns",
    "common",
    "favicon",
    "blank",
    "opentype",
)
TREND_IMAGE_PREFER_WORDS = (
    "crosseditor",
    "image.do?file=",
    "imgview.do",
    "/board/",
    "/editor/",
    "/upload/",
    "thumb",
    "thumbnail",
)


class DocumentTemplate(Base):
    __tablename__ = "document_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    doc_type: Mapped[str] = mapped_column(String(40), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    template_name: Mapped[str] = mapped_column(String(120))
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class DocumentRenderJob(Base):
    __tablename__ = "document_render_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    doc_type: Mapped[str] = mapped_column(String(40), index=True)
    source_id: Mapped[int] = mapped_column(Integer, index=True)
    template_id: Mapped[int | None] = mapped_column(ForeignKey("document_templates.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(40), index=True, default="queued")
    file_id: Mapped[int | None] = mapped_column(ForeignKey("generated_files.id"), nullable=True)
    error_code: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: now_utc())


class DocumentAuditLog(Base):
    __tablename__ = "document_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("generated_files.id"), index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True, default=1)
    action: Mapped[str] = mapped_column(String(40), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: now_utc())


class RecipeEvaluation(Base):
    __tablename__ = "recipe_evaluations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recipe_id: Mapped[int] = mapped_column(ForeignKey("recipe_drafts.id"), index=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("product_requests.id"), index=True)
    manufacturability_score: Mapped[float] = mapped_column(Float)
    nutrition_estimate: Mapped[str] = mapped_column(Text)
    claim_feasibility: Mapped[str] = mapped_column(String(40))
    allergen_risk: Mapped[str] = mapped_column(String(40))
    process_risk: Mapped[str] = mapped_column(String(40))
    cost_score: Mapped[float] = mapped_column(Float)
    required_tests: Mapped[str] = mapped_column(Text)
    revision_suggestions: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: now_utc())


class NutritionReference(Base):
    __tablename__ = "nutrition_references"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    food_name: Mapped[str] = mapped_column(String(160), index=True)
    category: Mapped[str] = mapped_column(String(80), index=True)
    calories_kcal: Mapped[float] = mapped_column(Float)
    protein_g: Mapped[float] = mapped_column(Float)
    sugar_g: Mapped[float] = mapped_column(Float)
    sodium_mg: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(80), default="manual_reference")
    checked_at: Mapped[str] = mapped_column(String(20), default="")


class IngredientPriceIndex(Base):
    __tablename__ = "ingredient_price_indexes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ingredient_name: Mapped[str] = mapped_column(String(120), index=True)
    source: Mapped[str] = mapped_column(String(60), index=True)
    price: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(30))
    normalized_price_kg: Mapped[float] = mapped_column(Float)
    market: Mapped[str] = mapped_column(String(80), default="")
    grade: Mapped[str] = mapped_column(String(80), default="")
    observed_at: Mapped[str] = mapped_column(String(20), index=True)
    status: Mapped[str] = mapped_column(String(40), index=True, default="manual_input")
    stale: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    source_url: Mapped[str] = mapped_column(Text, default="")
    trend_5y_change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    trend_5y_low_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    trend_5y_high_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    trend_5y_points: Mapped[int] = mapped_column(Integer, default=0)


class FxRate(Base):
    __tablename__ = "fx_rates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    currency: Mapped[str] = mapped_column(String(10), index=True)
    base_currency: Mapped[str] = mapped_column(String(10), default="KRW")
    rate: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(40), default="manual")
    rate_date: Mapped[str] = mapped_column(String(20), index=True)
    stale_flag: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    fx_buffer_rate: Mapped[float] = mapped_column(Float, default=0.03)


class PriceSyncRun(Base):
    __tablename__ = "price_sync_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(60), index=True)
    status: Mapped[str] = mapped_column(String(40), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: now_utc())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_code: Mapped[str] = mapped_column(String(80), default="")
    summary: Mapped[str] = mapped_column(Text, default="")


class RecipeEvaluationCreate(BaseModel):
    include_cost: bool = True


class PriceCreate(BaseModel):
    ingredient_name: str
    price: float = Field(..., ge=0)
    unit: str = "kg"
    normalized_price_kg: float = Field(..., ge=0)
    source: str = "manual_input"
    market: str = ""
    grade: str = ""
    observed_at: str | None = None
    status: str = "manual_input"
    source_url: str = ""


class FxRateCreate(BaseModel):
    currency: str = "USD"
    rate: float = Field(..., gt=0)
    source: str = "manual"
    rate_date: str | None = None
    stale_flag: bool = False
    fx_buffer_rate: float = 0.03


def clean_anchor_text(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value)
    text = html_lib.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def clean_article_text(value: str) -> str:
    text = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", value)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|section|article|tr|h\d)>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = text.replace("\xa0", " ").replace("\u200b", " ")
    lines = []
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip(" -•·\t")
        if line:
            lines.append(line)
    return re.sub(r"\n{2,}", "\n", "\n".join(lines)).strip()


def collapse_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def is_trend_title(text: str) -> bool:
    if len(text) < 8 or len(text) > 90:
        return False
    if any(word == text or text.startswith(word) for word in TREND_BLOCKED_TITLES):
        return False
    return bool(re.search(r"[가-힣A-Za-z]", text))


def trend_tags(title: str) -> list[str]:
    tags = [keyword for keyword in TREND_KEYWORDS if keyword.lower() in title.lower()]
    if not tags:
        tags = ["식산업"]
    return tags[:4]


def trend_column(title: str, tags: list[str]) -> str:
    tag = tags[0] if tags else "식산업"
    playbook = {
        "저당": "저당 포지션이면 영양성분 근거와 실제 제형 안정성을 먼저 확인해야 합니다.",
        "단백질": "단백질 강조 제품은 원료 수급과 알레르기 교차오염 관리가 핵심입니다.",
        "간편식": "간편식은 냉장·냉동 물류와 판매 포장 단가를 같이 봐야 수익성이 맞습니다.",
        "HMR": "HMR은 조리 공정과 콜드체인 운영 범위를 초기에 분리해 점검해야 합니다.",
        "K푸드": "K푸드는 수출국 표시 기준과 현지 유통 파트너 요건을 동시에 확인해야 합니다.",
        "수출": "수출형 제품은 국내 제조 적합성과 해외 라벨 규정을 병행 검토해야 합니다.",
        "비건": "비건은 원료 증빙과 교차오염 차단 공정이 먼저 확보되어야 합니다.",
        "고령친화": "고령친화 식품은 물성 검증과 섭취 안전성 자료 준비가 선행되어야 합니다.",
        "푸드테크": "푸드테크 이슈는 설비 전환성과 자동화 비용을 먼저 따져야 합니다.",
        "소스": "소스는 당도, pH, 충진 규격을 한 번에 묶어 검증해야 견적 오차가 줄어듭니다.",
        "음료": "음료는 충전·살균 설비와 용기 적합도를 함께 봐야 생산성이 맞습니다.",
        "디저트": "디저트는 시즌성 MOQ와 개별 포장 허용 범위를 먼저 체크해야 합니다.",
    }
    return playbook.get(tag, "식품 뉴스는 공정, 포장, 증빙 질문으로 쪼개서 실제 사업 판단으로 연결해야 합니다.")


def truncate_text(value: str, limit: int) -> tuple[str, bool]:
    text = collapse_text(value)
    if len(text) <= limit:
        return text, False
    shortened = text[: limit + 1]
    if " " in shortened:
        shortened = shortened.rsplit(" ", 1)[0]
    return shortened.rstrip(" ,.;:/"), True


def lead_sentences(value: str, *, max_chars: int, max_sentences: int = 2) -> str:
    text = collapse_text(value)
    if not text:
        return ""
    sentences = [
        part.strip()
        for part in re.split(r"(?<=[.!?])\s+|(?<=[다요]\.)\s+", text)
        if part.strip()
    ]
    if not sentences:
        return truncate_text(text, max_chars)[0]
    selected: list[str] = []
    for sentence in sentences:
        candidate = " ".join([*selected, sentence]).strip()
        if selected and len(candidate) > max_chars:
            break
        if not selected and len(sentence) > max_chars:
            return truncate_text(sentence, max_chars)[0]
        selected.append(sentence)
        if len(selected) >= max_sentences or len(candidate) >= max_chars:
            break
    return truncate_text(" ".join(selected), max_chars)[0]


def normalize_article_text(title: str, value: str) -> str:
    text = collapse_text(value)
    if not text:
        return ""
    if title and text.startswith(title):
        text = text[len(title) :].lstrip(" :-·•")
    for blocked in TREND_BLOCKED_BODY_PARTS:
        text = text.replace(blocked, " ")
    text = collapse_text(text)
    return "" if text == title else text


def build_short_column(title: str, article_text: str, tags: list[str]) -> str:
    playbook_line = trend_column(title, tags)
    summary = lead_sentences(article_text, max_chars=120, max_sentences=1)
    if not summary:
        return playbook_line
    return truncate_text(f"{summary} {playbook_line}", 220)[0]


def build_trend_item(
    *,
    title: str,
    source: str,
    source_url: str,
    tags: list[str],
    article_text: str,
    published_at: str = "",
    image_url: str = "",
) -> dict[str, Any]:
    normalized_article = normalize_article_text(title, article_text)
    article_preview, preview_truncated = truncate_text(normalized_article, 280)
    return {
        "title": title,
        "source": source,
        "source_url": source_url,
        "published_at": published_at,
        "tags": tags,
        "column": build_short_column(title, normalized_article, tags),
        "article_preview": article_preview,
        "article_preview_truncated": preview_truncated,
        "image_url": image_url,
    }


def fetch_trend_page(url: str) -> str:
    last_error: requests.RequestException | None = None
    for attempt in range(2):
        try:
            response = requests.get(url, timeout=(5, 20), headers=TREND_REQUEST_HEADERS)
            response.raise_for_status()
            if not response.encoding or response.encoding.lower() == "iso-8859-1":
                response.encoding = response.apparent_encoding or response.encoding
            return response.text
        except requests.RequestException as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(1.0)
                continue
            raise
    if last_error is not None:
        raise last_error
    raise requests.RequestException("trend_fetch_failed")


def extract_meta_image_url(page_html: str, base_url: str) -> str:
    for pattern in (
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']twitter:image(?::src)?["\'][^>]+content=["\']([^"\']+)["\']',
    ):
        match = re.search(pattern, page_html, flags=re.IGNORECASE)
        if match:
            return urljoin(base_url, html_lib.unescape(match.group(1)))
    return ""


def image_rank(url: str) -> int:
    lower = url.lower()
    if any(word in lower for word in TREND_IMAGE_SKIP_WORDS):
        return -1
    score = 0
    if lower.endswith((".jpg", ".jpeg", ".png", ".webp")):
        score += 1
    if any(word in lower for word in TREND_IMAGE_PREFER_WORDS):
        score += 5
    return score


def extract_image_url(page_html: str, base_url: str) -> str:
    candidates: list[tuple[int, str]] = []
    meta_image = extract_meta_image_url(page_html, base_url)
    if meta_image:
        candidates.append((image_rank(meta_image), meta_image))
    for raw_src in re.findall(r"<img[^>]+src=[\"']([^\"']+)[\"']", page_html, flags=re.IGNORECASE):
        resolved = urljoin(base_url, html_lib.unescape(raw_src))
        candidates.append((image_rank(resolved), resolved))
    if not candidates:
        return ""
    best_score, best_url = max(candidates, key=lambda item: item[0])
    return best_url if best_score >= 0 else ""


def dedupe_trend_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for item in items:
        key = (item["source"], item["title"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def extract_foodnews_items(source: dict[str, str], limit: int) -> list[dict[str, Any]]:
    page_html = fetch_trend_page(source["url"])
    items: list[dict[str, Any]] = []
    pattern = re.compile(
        r'<li>\s*<h4 class="titles">\s*<a href="([^"]+)"[^>]*>(.*?)</a>\s*</h4>.*?'
        r'<em class="info category">\s*(.*?)\s*</em>.*?'
        r'<em class="info dated">\s*(.*?)\s*</em>',
        flags=re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(page_html):
        title = clean_anchor_text(match.group(2))
        if not is_trend_title(title):
            continue
        category = clean_anchor_text(match.group(3))
        published_at = collapse_text(match.group(4))
        tags = trend_tags(f"{title} {category}".strip())
        items.append(
            build_trend_item(
                title=title,
                source=source["name"],
                source_url=urljoin(source["url"], html_lib.unescape(match.group(1))),
                published_at=published_at,
                tags=tags,
                article_text="",
            )
        )
        if len(items) >= limit:
            break
    return items


def parse_atfis_title(raw_text: str) -> str:
    text = collapse_text(re.sub(r"\b20\d{2}-\d{2}-\d{2}\b", " ", raw_text))
    if " - " in text:
        text = text.split(" - ")[-1].strip()
    if " 뉴스레터 " in text:
        text = text.split(" 뉴스레터 ", 1)[0].strip()
    return text


def extract_atfis_detail_text(page_html: str, fallback_title: str) -> str:
    text = clean_article_text(page_html)
    subject_match = re.search(r"주제\s*:\s*(.*?)(?:좋아요|첨부파일|댓글|PREV|NEXT|목록)", text)
    if subject_match:
        subject = collapse_text(subject_match.group(1))
        return f"{subject} 관련 월간 식품산업 트렌드 분석 자료입니다."
    return fallback_title


def extract_atfis_items(source: dict[str, str], limit: int) -> list[dict[str, Any]]:
    page_html = fetch_trend_page(source["url"])
    items: list[dict[str, Any]] = []
    pattern = re.compile(
        r'<a href="([^"]*act=read[^"]*)"[^>]*>(.*?)</a>',
        flags=re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(page_html):
        anchor_text = clean_anchor_text(match.group(2))
        published_match = re.search(r"(20\d{2}-\d{2}-\d{2})", anchor_text)
        published_at = published_match.group(1) if published_match else ""
        title = parse_atfis_title(anchor_text)
        if not is_trend_title(title):
            continue
        source_url = urljoin(source["url"], html_lib.unescape(match.group(1)))
        image_url = ""
        article_text = f"월간 식품산업 트렌드 리포트: {title}. 관련 원문과 첨부 자료를 확인할 수 있습니다."
        try:
            detail_html = fetch_trend_page(source_url)
            image_url = extract_image_url(detail_html, source_url)
            extracted_text = extract_atfis_detail_text(detail_html, title)
            if extracted_text and extracted_text != title:
                article_text = extracted_text
        except requests.RequestException:
            pass
        items.append(
            build_trend_item(
                title=title,
                source=source["name"],
                source_url=source_url,
                published_at=published_at,
                tags=trend_tags(title),
                article_text=article_text,
                image_url=image_url,
            )
        )
        if len(items) >= limit:
            break
    return items


def extract_kati_items(source: dict[str, str], limit: int) -> list[dict[str, Any]]:
    page_html = fetch_trend_page(source["url"])
    items: list[dict[str, Any]] = []
    pattern = re.compile(
        r'<li>\s*<a href="([^"]*exportNewsView\.do[^"]*)"[^>]*>.*?'
        r'<span class="fs-15 ff-ngb">\s*(.*?)\s*</span>.*?'
        r'<span class="option-area">.*?(\d{4}-\d{2}-\d{2}).*?</span>.*?'
        r'<span class="board-cont fs-13">\s*(.*?)\s*</span>',
        flags=re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(page_html):
        title = clean_anchor_text(match.group(2))
        if not is_trend_title(title):
            continue
        source_url = urljoin(source["url"], html_lib.unescape(match.group(1)))
        body_text = clean_article_text(match.group(4))
        items.append(
            build_trend_item(
                title=title,
                source=source["name"],
                source_url=source_url,
                published_at=match.group(3),
                tags=trend_tags(f"{title} {body_text}"),
                article_text=body_text,
            )
        )
        if len(items) >= limit:
            break
    return items


def crawl_source_items(source: dict[str, str], limit: int) -> list[dict[str, Any]]:
    kind = source.get("kind", "")
    if kind == "kati":
        return extract_kati_items(source, limit)
    if kind == "atfis":
        return extract_atfis_items(source, limit)
    if kind == "foodnews":
        return extract_foodnews_items(source, limit)
    return []


def fallback_trends() -> list[dict[str, Any]]:
    titles = [
        "저당·고단백 제품은 표시 기준과 분석 성적서가 입찰 조건으로 이동",
        "HMR과 냉동 간편식은 콜드체인 가능 업체 선별이 견적 편차를 좌우",
        "K푸드 수출형 소스는 살균 조건과 현지 라벨 검토가 초기 병목",
        "비건·대체식은 원료 증빙과 교차오염 관리가 컨택 질문의 중심",
        "고령친화 케어푸드는 물성 검증과 소량 파일럿 생산 수요가 증가",
        "디저트·베이커리는 시즌 한정 MOQ와 개별포장 수율 확인이 중요",
    ]
    return [
        build_trend_item(
            title=title,
            source="CBT fallback",
            source_url="",
            tags=trend_tags(title),
            article_text=title,
        )
        for title in titles
    ]


def crawl_industry_trends(limit: int = 8, refresh: bool = False) -> dict[str, Any]:
    now = time.time()
    if not refresh and TREND_CACHE["expires_at"] > now and TREND_CACHE["items"]:
        return {"mode": TREND_CACHE["mode"], "items": TREND_CACHE["items"][:limit], "cached": True}

    items: list[dict[str, Any]] = []
    for source in TREND_SOURCES:
        source_limit = 3 if source.get("kind") == "kati" else 2
        try:
            items.extend(crawl_source_items(source, source_limit))
        except (requests.RequestException, ValueError):
            continue
        items = dedupe_trend_items(items)
        if len(items) >= limit:
            break

    mode = "crawled" if items else "fallback"
    if not items:
        items = fallback_trends()
    TREND_CACHE.update({"expires_at": now + TREND_CACHE_TTL_SECONDS, "items": items, "mode": mode})
    return {"mode": mode, "items": items[:limit], "cached": False}


def seed_extra_data(db: Session) -> None:
    if not db.scalar(select(func.count(DocumentTemplate.id))):
        db.add_all(
            [
                DocumentTemplate(doc_type="product_plan", template_name="CBT 제품 기획안 기본", active=True),
                DocumentTemplate(doc_type="sample_brief", template_name="CBT 샘플 발주안 기본", active=True),
            ]
        )

    if not db.scalar(select(func.count(NutritionReference.id))):
        db.add_all(
            [
                NutritionReference(food_name="곡물 단백질바 참고", category="건강간식", calories_kcal=380, protein_g=18, sugar_g=6, sodium_mg=180, checked_at=str(date.today())),
                NutritionReference(food_name="식이섬유 분말 참고", category="분말스틱", calories_kcal=220, protein_g=5, sugar_g=3, sodium_mg=90, checked_at=str(date.today())),
                NutritionReference(food_name="저당 매운 소스 참고", category="소스", calories_kcal=90, protein_g=2, sugar_g=5, sodium_mg=780, checked_at=str(date.today())),
            ]
        )

    if not db.scalar(select(func.count(IngredientPriceIndex.id))):
        today = str(date.today())
        db.add_all(
            [
                IngredientPriceIndex(ingredient_name="현미", source="manual_reference", price=4200, unit="kg", normalized_price_kg=4200, market="내부 참고", grade="일반", observed_at=today, status="manual_input"),
                IngredientPriceIndex(ingredient_name="대두단백", source="supplier_quote", price=9800, unit="kg", normalized_price_kg=9800, market="내부 견적", grade="식품용", observed_at=today, status="confirmed_quote"),
                IngredientPriceIndex(ingredient_name="알룰로스", source="supplier_quote", price=6200, unit="kg", normalized_price_kg=6200, market="내부 견적", grade="시럽/분말 후보", observed_at=today, status="confirmed_quote"),
                IngredientPriceIndex(ingredient_name="계란", source="public_reference", price=5800, unit="kg", normalized_price_kg=5800, market="공공 참고", grade="가공용 환산", observed_at=today, status="public_reference"),
                IngredientPriceIndex(ingredient_name="스틱필름", source="manual_input", price=32, unit="매", normalized_price_kg=0, market="포장재", grade="식품용 확인 필요", observed_at=today, status="manual_input"),
            ]
        )

    if not db.scalar(select(func.count(FxRate.id))):
        db.add_all(
            [
                FxRate(currency="USD", rate=1380.0, source="manual", rate_date=str(date.today()), stale_flag=True, fx_buffer_rate=0.03),
                FxRate(currency="EUR", rate=1500.0, source="manual", rate_date=str(date.today()), stale_flag=True, fx_buffer_rate=0.03),
            ]
        )


def ensure_extra_schema_columns() -> None:
    with engine.begin() as conn:
        rows = conn.exec_driver_sql("PRAGMA table_info(ingredient_price_indexes)").fetchall()
        existing = {row[1] for row in rows}
        additions = {
            "source_url": "TEXT DEFAULT ''",
            "trend_5y_change_pct": "FLOAT",
            "trend_5y_low_price": "FLOAT",
            "trend_5y_high_price": "FLOAT",
            "trend_5y_points": "INTEGER DEFAULT 0",
        }
        for name, ddl in additions.items():
            if name not in existing:
                conn.exec_driver_sql(f"ALTER TABLE ingredient_price_indexes ADD COLUMN {name} {ddl}")


def serialize_price(row: IngredientPriceIndex) -> dict[str, Any]:
    return {
        "id": row.id,
        "ingredient_name": row.ingredient_name,
        "source": row.source,
        "price": row.price,
        "unit": row.unit,
        "normalized_price_kg": row.normalized_price_kg,
        "market": row.market,
        "grade": row.grade,
        "observed_at": row.observed_at,
        "status": row.status,
        "stale": row.stale,
        "source_url": row.source_url,
        "trend_5y_change_pct": row.trend_5y_change_pct,
        "trend_5y_low_price": row.trend_5y_low_price,
        "trend_5y_high_price": row.trend_5y_high_price,
        "trend_5y_points": row.trend_5y_points,
    }


def serialize_fx(row: FxRate) -> dict[str, Any]:
    return {
        "id": row.id,
        "currency": row.currency,
        "base_currency": row.base_currency,
        "rate": row.rate,
        "source": row.source,
        "rate_date": row.rate_date,
        "stale_flag": row.stale_flag,
        "fx_buffer_rate": row.fx_buffer_rate,
    }


def price_identity(row: PriceObservation) -> tuple[str, str, str, str, str]:
    return (row.ingredient_name, row.source, row.market, row.grade, row.observed_at)


def apply_trend(row: IngredientPriceIndex, trend: TrendSnapshot | None) -> None:
    if trend is None:
        return
    row.trend_5y_change_pct = trend.change_pct
    row.trend_5y_low_price = trend.low_price
    row.trend_5y_high_price = trend.high_price
    row.trend_5y_points = trend.points


def upsert_price_observations(
    db: Session,
    observations: list[PriceObservation],
    trends: dict[str, TrendSnapshot],
) -> int:
    if not observations:
        return 0

    names = sorted({row.ingredient_name for row in observations})
    dates = sorted({row.observed_at for row in observations})
    sources = sorted({row.source for row in observations})
    rows = db.scalars(
        select(IngredientPriceIndex).where(
            IngredientPriceIndex.source.in_(sources),
            IngredientPriceIndex.ingredient_name.in_(names),
            IngredientPriceIndex.observed_at >= dates[0],
            IngredientPriceIndex.observed_at <= dates[-1],
        )
    ).all()
    existing = {
        (row.ingredient_name, row.source, row.market, row.grade, row.observed_at): row
        for row in rows
    }

    changed = 0
    for item in observations:
        key = price_identity(item)
        row = existing.get(key)
        if row is None:
            row = IngredientPriceIndex(
                ingredient_name=item.ingredient_name,
                source=item.source,
                price=item.price,
                unit=item.unit,
                normalized_price_kg=item.normalized_price_kg,
                market=item.market,
                grade=item.grade,
                observed_at=item.observed_at,
                status=item.status,
                source_url=item.source_url,
                stale=False,
            )
            existing[key] = row
            db.add(row)
            changed += 1
        else:
            row.price = item.price
            row.unit = item.unit
            row.normalized_price_kg = item.normalized_price_kg
            row.status = item.status
            row.source_url = item.source_url
            row.stale = False
            changed += 1
        apply_trend(row, trends.get(item.ingredient_name))
    return changed


def run_public_price_sync(db: Session, history_years: int) -> PriceSyncRun:
    run = PriceSyncRun(source="worldbank_pink_sheet", status="running")
    db.add(run)
    db.flush()
    try:
        observations, trends, summary = collect_public_crawl_prices(years=history_years)
        changed = upsert_price_observations(db, observations, trends)
        run.status = "succeeded" if observations else "skipped"
        run.summary = f"{summary}; DB 반영 {changed}건"
        run.finished_at = now_utc()
    except (PriceSyncError, requests.RequestException, ValueError, OSError) as exc:
        run.status = "failed"
        run.error_code = exc.__class__.__name__
        run.summary = str(exc)
        run.finished_at = now_utc()
    return run


def run_kamis_price_sync(db: Session, history_years: int) -> PriceSyncRun:
    run = PriceSyncRun(source="kamis_api", status="running")
    db.add(run)
    db.flush()
    try:
        observations, trends, summary = collect_kamis_prices(years=history_years)
        changed = upsert_price_observations(db, observations, trends)
        run.status = "succeeded" if observations else "skipped"
        run.summary = f"{summary}; DB 반영 {changed}건"
        run.finished_at = now_utc()
    except (PriceSyncError, requests.RequestException) as exc:
        run.status = "failed"
        run.error_code = exc.__class__.__name__
        run.summary = str(exc)
        run.finished_at = now_utc()
    return run


def run_periodic_public_sync_once() -> None:
    history_years = env_int("PRICE_SYNC_HISTORY_YEARS", 5, 1, 5)
    with db_session() as db:
        run_public_price_sync(db, history_years)


def ensure_price_sync_scheduler_started() -> None:
    global PRICE_SYNC_SCHEDULER_STARTED
    if PRICE_SYNC_SCHEDULER_STARTED:
        return
    PRICE_SYNC_SCHEDULER_STARTED = True
    start_periodic_price_sync(run_periodic_public_sync_once)


def evaluate_recipe(db: Session, recipe: RecipeDraft, include_cost: bool = True) -> RecipeEvaluation:
    findings = list(db.scalars(select(ScreeningFinding).where(ScreeningFinding.request_id == recipe.request_id)))
    ingredients = list(db.scalars(select(IngredientLine).where(IngredientLine.request_id == recipe.request_id)))
    cost = db.scalar(select(CostCalculation).where(CostCalculation.request_id == recipe.request_id).order_by(CostCalculation.id.desc()))
    red_count = sum(1 for finding in findings if finding.severity == "RED")
    yellow_count = sum(1 for finding in findings if finding.severity == "YELLOW")
    allergen_count = sum(1 for line in ingredients if line.allergen_flag)
    manufacturability = max(45, 88 - red_count * 12 - yellow_count * 3 - allergen_count * 2)
    cost_score = 80.0
    if include_cost and cost:
        cost_score = max(35, min(95, 95 - max(0, cost.unit_cost - 900) / 40))
    nutrition = {
        "calories_kcal": "유사 식품 참고 추정",
        "protein": "고단백/단백질 문구는 분석값 필요",
        "sugar": "저당/무당 문구는 영양성분 분석값 필요",
        "sodium": "소스류는 나트륨 강조표시 기준 확인",
    }
    required_tests = ["영양성분 분석", "알레르기 교차오염 확인", "라벨 검수"]
    if red_count:
        required_tests.insert(0, "표시광고 전문가 검토")
    suggestions = ["공장 샘플 3종으로 맛/식감 비교", "원료 대체 가능 여부와 MOQ 동시 확인"]
    if cost_score < 65:
        suggestions.append("포장비 또는 샘플비를 분리 견적해 목표 원가를 재검토")
    evaluation = RecipeEvaluation(
        recipe_id=recipe.id,
        request_id=recipe.request_id,
        manufacturability_score=round(float(manufacturability), 1),
        nutrition_estimate=as_json(nutrition),
        claim_feasibility="위험" if red_count else "주의" if yellow_count else "좋음",
        allergen_risk="주의" if allergen_count else "낮음",
        process_risk="주의" if manufacturability < 70 else "좋음",
        cost_score=round(float(cost_score), 1),
        required_tests=as_json(required_tests),
        revision_suggestions=as_json(suggestions),
    )
    db.add(evaluation)
    record_tool_run(db, recipe.request_id, "recipe_evaluator", {"recipe_id": recipe.id}, f"레시피 평가 {evaluation.claim_feasibility}")
    return evaluation


def serialize_evaluation(db: Session, row: RecipeEvaluation) -> dict[str, Any]:
    req = db.get(ProductRequest, row.request_id)
    recipe = db.get(RecipeDraft, row.recipe_id)
    ingredients = list(db.scalars(select(IngredientLine).where(IngredientLine.request_id == row.request_id)))
    findings = list(db.scalars(select(ScreeningFinding).where(ScreeningFinding.request_id == row.request_id)))
    cost = db.scalar(select(CostCalculation).where(CostCalculation.request_id == row.request_id).order_by(CostCalculation.id.desc()))
    process_plan = build_process_plan(db, req) if req else None
    risk_basis = (
        build_recipe_risk_basis(
            req=req,
            recipe=recipe,
            ingredients=ingredients,
            findings=findings,
            cost=cost,
            manufacturability_score=row.manufacturability_score,
            cost_score=row.cost_score,
            claim_feasibility=row.claim_feasibility,
            process_plan=process_plan,
        )
        if req
        else {}
    )
    return {
        "id": row.id,
        "recipe_id": row.recipe_id,
        "request_id": row.request_id,
        "manufacturability_score": row.manufacturability_score,
        "nutrition_estimate": from_json(row.nutrition_estimate, {}),
        "claim_feasibility": row.claim_feasibility,
        "allergen_risk": row.allergen_risk,
        "process_risk": row.process_risk,
        "cost_score": row.cost_score,
        "required_tests": from_json(row.required_tests, []),
        "revision_suggestions": from_json(row.revision_suggestions, []),
        "risk_basis": risk_basis,
        "created_at": row.created_at.isoformat(),
    }


def register_extra_routes(app) -> None:
    Base.metadata.create_all(bind=engine)
    ensure_extra_schema_columns()
    with db_session() as db:
        seed_extra_data(db)
    ensure_price_sync_scheduler_started()

    @app.get("/api/industry-trends")
    def list_industry_trends() -> dict[str, Any]:
        from flask import request

        refresh = request.args.get("refresh", "").lower() in {"1", "true", "yes", "y"}
        limit = query_int("limit", 8, 1, 12)
        return crawl_industry_trends(limit=limit, refresh=refresh)

    @app.get("/api/ingredient-prices")
    def list_ingredient_prices() -> dict[str, Any]:
        from flask import request

        q = request.args.get("q")
        source = request.args.get("source")
        history = request.args.get("history", "").lower() in {"1", "true", "yes", "y"}
        limit = query_int("limit", 30, 1, 100)
        offset = query_int("offset", 0, 0)
        with db_session() as db:
            filters = []
            if q:
                filters.append(IngredientPriceIndex.ingredient_name.like(f"%{q}%"))
            if source:
                filters.append(IngredientPriceIndex.source == source)
            if history:
                total = db.scalar(select(func.count(IngredientPriceIndex.id)).where(*filters))
                rows = db.scalars(
                    select(IngredientPriceIndex)
                    .where(*filters)
                    .order_by(IngredientPriceIndex.observed_at.desc(), IngredientPriceIndex.ingredient_name)
                    .limit(limit)
                    .offset(offset)
                ).all()
                return {"total": total, "items": [serialize_price(row) for row in rows]}

            latest = (
                select(
                    IngredientPriceIndex.ingredient_name.label("ingredient_name"),
                    IngredientPriceIndex.source.label("source"),
                    IngredientPriceIndex.market.label("market"),
                    IngredientPriceIndex.grade.label("grade"),
                    func.max(IngredientPriceIndex.observed_at).label("observed_at"),
                )
                .where(*filters)
                .group_by(
                    IngredientPriceIndex.ingredient_name,
                    IngredientPriceIndex.source,
                    IngredientPriceIndex.market,
                    IngredientPriceIndex.grade,
                )
                .subquery()
            )
            total = db.scalar(select(func.count()).select_from(latest))
            rows = db.scalars(
                select(IngredientPriceIndex)
                .join(
                    latest,
                    and_(
                        IngredientPriceIndex.ingredient_name == latest.c.ingredient_name,
                        IngredientPriceIndex.source == latest.c.source,
                        IngredientPriceIndex.market == latest.c.market,
                        IngredientPriceIndex.grade == latest.c.grade,
                        IngredientPriceIndex.observed_at == latest.c.observed_at,
                    ),
                )
                .order_by(IngredientPriceIndex.observed_at.desc(), IngredientPriceIndex.ingredient_name)
                .limit(limit)
                .offset(offset)
            ).all()
            return {"total": total, "items": [serialize_price(row) for row in rows]}

    @app.post("/api/admin/ingredient-prices")
    def create_ingredient_price() -> dict[str, Any]:
        payload = json_payload(PriceCreate)
        with db_session() as db:
            row = IngredientPriceIndex(
                ingredient_name=payload.ingredient_name,
                source=payload.source,
                price=payload.price,
                unit=payload.unit,
                normalized_price_kg=payload.normalized_price_kg,
                market=payload.market,
                grade=payload.grade,
                observed_at=payload.observed_at or str(date.today()),
                status=payload.status,
                source_url=payload.source_url,
            )
            db.add(row)
            db.flush()
            return serialize_price(row)

    @app.get("/api/fx-rates")
    def list_fx_rates() -> dict[str, Any]:
        from flask import request

        currency = request.args.get("currency")
        with db_session() as db:
            filters = []
            if currency:
                filters.append(FxRate.currency == currency.upper())
            rows = db.scalars(select(FxRate).where(*filters).order_by(FxRate.currency, FxRate.rate_date.desc())).all()
            return {"items": [serialize_fx(row) for row in rows]}

    @app.post("/api/admin/fx-rates")
    def create_fx_rate() -> dict[str, Any]:
        payload = json_payload(FxRateCreate)
        with db_session() as db:
            row = FxRate(
                currency=payload.currency.upper(),
                rate=payload.rate,
                source=payload.source,
                rate_date=payload.rate_date or str(date.today()),
                stale_flag=payload.stale_flag,
                fx_buffer_rate=payload.fx_buffer_rate,
            )
            db.add(row)
            db.flush()
            return serialize_fx(row)

    @app.post("/api/admin/price-sync-runs")
    def create_price_sync_run() -> dict[str, Any]:
        from flask import request

        source = request.args.get("source", "worldbank_pink_sheet")
        history_years = query_int("history_years", env_int("PRICE_SYNC_HISTORY_YEARS", 5, 1, 5), 1, 5)
        with db_session() as db:
            if source in {"kamis", "kamis_api"}:
                row = run_kamis_price_sync(db, history_years)
            elif source in {"crawl", "public_crawl", "worldbank", "worldbank_pink_sheet"}:
                row = run_public_price_sync(db, history_years)
            else:
                row = PriceSyncRun(source=source, status="succeeded", finished_at=now_utc(), summary="CBT 수동 기준 데이터 확인")
                db.add(row)
            db.flush()
            return {"id": row.id, "source": row.source, "status": row.status, "summary": row.summary, "error_code": row.error_code}

    @app.get("/api/admin/price-sync-runs")
    def list_price_sync_runs() -> dict[str, Any]:
        limit = query_int("limit", 10, 1, 50)
        with db_session() as db:
            rows = db.scalars(select(PriceSyncRun).order_by(PriceSyncRun.started_at.desc()).limit(limit)).all()
            return {
                "configured": True,
                "public_crawl_source": "worldbank_pink_sheet",
                "kamis_configured": bool(os.getenv("KAMIS_CERT_KEY") and os.getenv("KAMIS_CERT_ID")),
                "items": [
                    {
                        "id": row.id,
                        "source": row.source,
                        "status": row.status,
                        "started_at": row.started_at.isoformat(),
                        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                        "error_code": row.error_code,
                        "summary": row.summary,
                    }
                    for row in rows
                ],
            }

    @app.get("/api/admin/price-sync-runs/<int:run_id>")
    def get_price_sync_run(run_id: int) -> dict[str, Any]:
        with db_session() as db:
            row = db.get(PriceSyncRun, run_id)
            if not row:
                api_error(404, "price_sync_run_not_found")
            return {"id": row.id, "source": row.source, "status": row.status, "started_at": row.started_at.isoformat(), "finished_at": row.finished_at.isoformat() if row.finished_at else None, "error_code": row.error_code, "summary": row.summary}

    @app.post("/api/recipes/<int:recipe_id>/evaluations")
    def create_recipe_evaluation(recipe_id: int) -> dict[str, Any]:
        payload = json_payload(RecipeEvaluationCreate)
        with db_session() as db:
            recipe = db.get(RecipeDraft, recipe_id)
            if not recipe:
                api_error(404, "recipe_not_found")
            load_owned_request_or_404(db, recipe.request_id)
            row = evaluate_recipe(db, recipe, include_cost=payload.include_cost)
            db.flush()
            return serialize_evaluation(db, row)

    @app.get("/api/recipes/<int:recipe_id>/evaluations/<int:evaluation_id>")
    def get_recipe_evaluation(recipe_id: int, evaluation_id: int) -> dict[str, Any]:
        with db_session() as db:
            row = db.get(RecipeEvaluation, evaluation_id)
            if not row or row.recipe_id != recipe_id:
                api_error(404, "recipe_evaluation_not_found")
            load_owned_request_or_404(db, row.request_id)
            return serialize_evaluation(db, row)

    @app.get("/api/product-requests/<int:request_id>/recipe-evaluations")
    def list_request_recipe_evaluations(request_id: int) -> dict[str, Any]:
        with db_session() as db:
            load_owned_request_or_404(db, request_id)
            rows = db.scalars(select(RecipeEvaluation).where(RecipeEvaluation.request_id == request_id).order_by(RecipeEvaluation.id.desc())).all()
            return {"items": [serialize_evaluation(db, row) for row in rows]}

    @app.get("/api/nutrition-references")
    def list_nutrition_references() -> dict[str, Any]:
        from flask import request

        q = request.args.get("q")
        with db_session() as db:
            filters = []
            if q:
                filters.append(NutritionReference.food_name.like(f"%{q}%"))
            rows = db.scalars(select(NutritionReference).where(*filters).order_by(NutritionReference.food_name).limit(30)).all()
            return {
                "items": [
                    {
                        "id": row.id,
                        "food_name": row.food_name,
                        "category": row.category,
                        "calories_kcal": row.calories_kcal,
                        "protein_g": row.protein_g,
                        "sugar_g": row.sugar_g,
                        "sodium_mg": row.sodium_mg,
                        "source": row.source,
                        "checked_at": row.checked_at,
                    }
                    for row in rows
                ]
            }

    @app.get("/api/document-audit-logs")
    def list_document_audit_logs() -> dict[str, Any]:
        limit = query_int("limit", 30, 1, 100)
        with db_session() as db:
            rows = db.scalars(select(DocumentAuditLog).order_by(DocumentAuditLog.created_at.desc()).limit(limit)).all()
            return {"items": [{"id": row.id, "file_id": row.file_id, "user_id": row.user_id, "action": row.action, "created_at": row.created_at.isoformat()} for row in rows]}

    @app.post("/api/files/<file_uid>/audit")
    def create_document_audit(file_uid: str) -> dict[str, Any]:
        from flask import request

        action = request.args.get("action", "download")
        with db_session() as db:
            current_visitor_id()
            file_row = load_owned_generated_file_or_404(db, file_uid)
            audit = DocumentAuditLog(file_id=file_row.id, action=action)
            db.add(audit)
            db.flush()
            return {"id": audit.id, "file_id": audit.file_id, "action": audit.action}
