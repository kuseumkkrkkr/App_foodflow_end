from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
from xml.sax.saxutils import escape

from flask import Flask, Response, abort, jsonify, request, send_file, send_from_directory
from flask_cors import CORS
from pydantic import BaseModel, Field
from PyPDF2 import PdfReader
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    func,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker
import requests


BASE_DIR = Path(__file__).resolve().parents[1]
STATIC_DIR = BASE_DIR / "static"
DATABASE_DIR = BASE_DIR / "database"
DB_PATH = BASE_DIR / "data" / "cbt_app.db"
FACTORY_SEED = DATABASE_DIR / "korea_oem_odm_seed.csv"
RULE_SEED = DATABASE_DIR / "regulatory_screening_rules_seed.csv"
GENERATED_DIR = BASE_DIR / "generated"


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key:
            os.environ.setdefault(key, value)


load_dotenv(BASE_DIR / ".env")

SAM_BASE_URL = os.getenv("SAM_BASE_URL", "https://sam.soonsoon.ai")
SAM_API_KEY = os.getenv("SAM_API_KEY", "")
SAM_DEFAULT_MODEL = os.getenv("SAM_MODEL", "az-deepseek-v4-flash")
DEEPSEEK_MODELS = ["az-deepseek-v4-flash", "az-deepseek-v4-pro"]
AI_VENDOR_PROFILES = [
    {"key": "fast_sample", "label": "초도 대응형 ODM"},
    {"key": "cost_down", "label": "원가 최적화형 OEM"},
    {"key": "quality_gate", "label": "문서 검증형 OEM"},
    {"key": "repeat_supply", "label": "반복 납품형 B2B"},
]
DEFAULT_VIBE_AGENT_REPORT_GOAL = "입찰/컨택 근거와 레시피 위험을 상위 5개 업체 기준으로 정리"
VISITOR_HEADER_NAME = "X-Visitor-Id"
VISITOR_QUERY_PARAM = "visitor_id"
BOARD_REPLY_DELAY_SECONDS = 6
BOARD_REPLY_POLL_SECONDS = 2

PRODUCT_CASES = {
    "health_snack": {
        "label": "건강간식",
        "aliases": ["건강간식", "그래놀라", "쿠키", "바", "스낵", "프로틴바", "곡물"],
        "process": ["배합", "성형", "굽기/건조", "냉각", "개별포장"],
        "packages": ["개별포장", "파우치", "바포장", "박스"],
        "ingredients": [
            ("주원료", "귀리/현미/곡물 베이스", "제한 가능", "밀"),
            ("단백질원", "분리대두단백 또는 유청단백", "가능", "대두/우유"),
            ("감미료", "알룰로스 또는 에리스리톨", "가능", ""),
            ("식감 보완", "견과류 또는 식이섬유", "가능", "견과류"),
        ],
        "questions": ["당류/단백질 분석 지원이 가능한가", "알레르기 교차오염 관리 기준이 있는가"],
    },
    "powder_stick": {
        "label": "분말스틱",
        "aliases": ["분말", "스틱", "파우더", "쉐이크", "식이섬유", "건강식품"],
        "process": ["원료계량", "혼합", "체질", "스틱충진", "금속검출"],
        "packages": ["스틱", "파우치", "지퍼팩", "병"],
        "ingredients": [
            ("주원료", "식이섬유/단백질/곡물 분말", "제한 가능", ""),
            ("향미", "코코아/라떼/과일 향미", "가능", "우유"),
            ("감미료", "알룰로스 또는 스테비아", "가능", ""),
            ("기능성 후보", "프로바이오틱스 또는 비타민", "제한 가능", ""),
        ],
        "questions": ["일반식품과 건강기능식품 중 어느 범위가 가능한가", "스틱포 단위 표시 검수가 가능한가"],
    },
    "sauce": {
        "label": "소스",
        "aliases": ["소스", "드레싱", "양념", "육수", "시즈닝", "매운"],
        "process": ["배합", "가열/살균", "충진", "냉각", "포장"],
        "packages": ["파우치", "병", "스파우트파우치", "용기"],
        "ingredients": [
            ("베이스", "고추/간장/채소 추출 베이스", "가능", "대두/밀"),
            ("향미", "마늘/양파/향신료", "가능", ""),
            ("감미/염도", "대체당 또는 저염 설계", "가능", ""),
            ("안정화", "산도조절제 또는 점도 조절 원료", "제한 가능", ""),
        ],
        "questions": ["살균 조건과 보존 기준을 제안할 수 있는가", "파우치/병 포장재 식품용 증빙 제공이 가능한가"],
    },
    "beverage": {
        "label": "음료/액상",
        "aliases": ["음료", "주스", "티", "커피", "액상", "RTD", "드링크"],
        "process": ["배합", "여과", "살균", "충진", "라벨링"],
        "packages": ["병", "PET병", "파우치", "스파우트파우치", "팩", "캔"],
        "ingredients": [
            ("베이스", "정제수/추출액/농축액 베이스", "가능", ""),
            ("향미", "천연향 또는 과채 향미", "가능", ""),
            ("감미", "알룰로스/스테비아/설탕 후보", "가능", ""),
            ("안정화", "pH 조정 또는 침전 방지 설계", "제한 가능", ""),
        ],
        "questions": ["살균 방식과 충진 설비 범위가 맞는가", "침전/분리 안정성 테스트가 가능한가"],
    },
    "hmr_mealkit": {
        "label": "HMR/밀키트",
        "aliases": ["HMR", "밀키트", "간편식", "도시락", "냉동", "RMR"],
        "process": ["전처리", "조리", "급속냉각/냉동", "소분", "포장"],
        "packages": ["트레이", "파우치", "용기", "진공포장", "팩"],
        "ingredients": [
            ("주재료", "곡류/육류/채소 조합", "가능", "대두/밀/우유/계란"),
            ("소스", "전용 소스 또는 양념 베이스", "가능", "대두/밀"),
            ("토핑", "채소/단백질/고명", "가능", ""),
            ("보존 설계", "냉장/냉동 유통 기준", "제한 가능", ""),
        ],
        "questions": ["냉장/냉동 HACCP 라인과 콜드체인이 맞는가", "소분 포장과 표시사항 작업 범위는 어디까지인가"],
    },
    "bakery_dessert": {
        "label": "베이커리/디저트",
        "aliases": ["베이커리", "빵", "쿠키", "케이크", "디저트", "떡", "초콜릿"],
        "process": ["배합", "성형", "굽기/증숙", "냉각", "개별포장"],
        "packages": ["개별포장", "파우치", "트레이", "박스", "바포장"],
        "ingredients": [
            ("베이스", "밀가루/쌀가루/견과 베이스", "가능", "밀/견과류"),
            ("유지", "버터/식물성유지 후보", "가능", "우유"),
            ("감미", "설탕/대체당/과일 농축 후보", "가능", ""),
            ("식감", "크림/필링/토핑 후보", "가능", "우유/계란"),
        ],
        "questions": ["개별포장 후 수분 이행과 식감 유지 테스트가 가능한가", "소량 시즌 제품 MOQ가 가능한가"],
    },
    "kimchi_pickled": {
        "label": "김치/절임",
        "aliases": ["김치", "절임", "장아찌", "피클", "발효채소"],
        "process": ["원물 선별", "절임", "양념배합", "숙성", "포장"],
        "packages": ["파우치", "병", "용기", "진공포장", "팩"],
        "ingredients": [
            ("원물", "배추/무/채소 원물", "제한 가능", ""),
            ("절임", "소금/염수 조건", "가능", ""),
            ("양념", "고춧가루/마늘/젓갈 후보", "가능", "새우/생선"),
            ("숙성", "발효 온도와 기간 설계", "제한 가능", ""),
        ],
        "questions": ["발효 편차와 가스 발생 관리가 가능한가", "저염/비건 옵션의 표시 리스크를 검토할 수 있는가"],
    },
    "meat_seafood": {
        "label": "축수산 가공",
        "aliases": ["육가공", "축산", "수산", "어묵", "닭가슴살", "해산물", "HACCP"],
        "process": ["원료검수", "전처리", "가열/성형", "냉각", "냉장/냉동포장"],
        "packages": ["파우치", "진공포장", "트레이", "용기", "레토르트파우치"],
        "ingredients": [
            ("주원료", "육류/수산 원료", "제한 가능", "고기/생선"),
            ("결착/식감", "전분/단백/식이섬유 후보", "가능", "대두/밀"),
            ("소스", "시즈닝/염지/소스", "가능", "대두/밀"),
            ("유통", "냉장 또는 냉동 기준", "제한 가능", ""),
        ],
        "questions": ["축수산 HACCP 범위와 냉장/냉동 물류가 맞는가", "원산지/알레르기 표시 증빙을 제공할 수 있는가"],
    },
    "fermented": {
        "label": "발효/전통식품",
        "aliases": ["발효", "장류", "고추장", "된장", "간장", "식초", "전통식품"],
        "process": ["원료배합", "발효", "숙성", "여과/살균", "포장"],
        "packages": ["병", "파우치", "스파우트파우치", "용기", "팩"],
        "ingredients": [
            ("발효 베이스", "콩/곡류/과채 발효 베이스", "제한 가능", "대두/밀"),
            ("종균/누룩", "발효 스타터 후보", "제한 가능", ""),
            ("향미", "소금/향신/감미 보정", "가능", ""),
            ("안정화", "숙성 편차와 산도 관리", "제한 가능", ""),
        ],
        "questions": ["발효 기간과 균일도 관리 기준이 있는가", "소량 숙성 배치와 품질 기록 제공이 가능한가"],
    },
    "senior_care": {
        "label": "고령친화/케어푸드",
        "aliases": ["고령친화", "케어푸드", "연하", "실버", "저작", "부드러운"],
        "process": ["배합", "입자/물성 조정", "가열", "충진", "살균/포장"],
        "packages": ["파우치", "스파우트파우치", "컵", "병", "팩"],
        "ingredients": [
            ("주원료", "곡류/단백질/채소 베이스", "가능", "대두/우유"),
            ("물성", "점도/입자 크기 조정 소재", "제한 가능", ""),
            ("영양", "단백질/비타민/미네랄 후보", "가능", "우유"),
            ("풍미", "저염/저당 향미 보완", "가능", ""),
        ],
        "questions": ["물성 측정과 고령친화 표시 검토가 가능한가", "살균 후 식감 유지 테스트가 가능한가"],
    },
    "vegan_alt": {
        "label": "비건/대체식",
        "aliases": ["비건", "대체육", "식물성", "플랜트베이스", "대체식"],
        "process": ["원료수화", "배합", "성형", "가열", "포장"],
        "packages": ["파우치", "트레이", "개별포장", "용기", "진공포장"],
        "ingredients": [
            ("식물성 단백", "대두/완두/밀 단백 후보", "가능", "대두/밀"),
            ("식감", "식이섬유/전분/유지 조합", "가능", ""),
            ("향미", "효모추출물/향신료 후보", "가능", ""),
            ("색상", "천연색소 또는 소스 보정", "가능", ""),
        ],
        "questions": ["비건 원료 증빙과 교차오염 관리가 가능한가", "식감 구현을 위한 압출/성형 설비 범위가 맞는가"],
    },
    "rice_processed": {
        "label": "쌀/곡류 가공",
        "aliases": ["쌀", "곡류", "현미", "누룽지", "떡", "쌀과자", "라이스"],
        "process": ["원료선별", "분쇄/증숙", "성형", "건조/굽기", "포장"],
        "packages": ["개별포장", "파우치", "컵", "트레이", "박스"],
        "ingredients": [
            ("곡류 베이스", "쌀/현미/잡곡 베이스", "가능", ""),
            ("결착", "전분/시럽/식이섬유 후보", "가능", ""),
            ("향미", "소금/감미/시즈닝 후보", "가능", "대두/밀"),
            ("식감", "팽화/건조 조건", "제한 가능", ""),
        ],
        "questions": ["팽화/건조 설비와 소량 테스트가 가능한가", "바삭함 유지 포장 조건을 제안할 수 있는가"],
    },
    "noodle_pasta": {
        "label": "면/파스타",
        "aliases": ["면", "국수", "파스타", "라면", "생면", "건면", "소바"],
        "process": ["배합", "제면", "숙성/건조", "절단", "소분포장"],
        "packages": ["파우치", "트레이", "컵", "박스"],
        "ingredients": [
            ("주원료", "밀가루/쌀가루/메밀 베이스", "가능", "밀"),
            ("전분", "감자/타피오카 전분 후보", "가능", ""),
            ("풍미", "소스/스프/오일 별첨 후보", "가능", "대두/밀"),
            ("보존", "건면 또는 냉장 생면 조건", "제한 가능", ""),
        ],
        "questions": ["건면/생면 설비와 목표 유통기한이 맞는가", "소스/스프 별첨 포장까지 가능한가"],
    },
    "ready_rice_soup": {
        "label": "즉석밥/죽/스프",
        "aliases": ["즉석밥", "죽", "스프", "레토르트", "탕", "국", "컵밥"],
        "process": ["원료계량", "조리", "충진", "레토르트/살균", "검수포장"],
        "packages": ["레토르트파우치", "컵", "트레이", "용기"],
        "ingredients": [
            ("베이스", "쌀/곡물/육수 베이스", "가능", ""),
            ("부재료", "채소/육류/해산물 토핑", "제한 가능", "고기/생선"),
            ("점도", "전분/검류 물성 후보", "가능", ""),
            ("풍미", "소스/시즈닝 후보", "가능", "대두/밀"),
        ],
        "questions": ["레토르트 또는 고온살균 조건을 제안할 수 있는가", "용기 충진과 이물 검출 라인이 있는가"],
    },
    "side_dish_deli": {
        "label": "반찬/델리",
        "aliases": ["반찬", "델리", "조림", "무침", "볶음", "샐러드반찬"],
        "process": ["원물전처리", "조리", "냉각", "소분", "실링포장"],
        "packages": ["트레이", "용기", "파우치", "진공포장"],
        "ingredients": [
            ("주재료", "채소/육류/수산 원물", "제한 가능", "고기/생선"),
            ("양념", "간장/고추장/드레싱 후보", "가능", "대두/밀"),
            ("보존", "냉장 유통과 수분활성 관리", "제한 가능", ""),
            ("토핑", "견과/깨/고명 후보", "가능", "견과류"),
        ],
        "questions": ["냉장 HACCP 라인과 당일/익일 출고가 가능한가", "소분 중량 편차 관리 기준이 있는가"],
    },
    "fresh_cut_salad": {
        "label": "샐러드/신선편의",
        "aliases": ["샐러드", "신선편의", "컷채소", "과일컵", "세척채소"],
        "process": ["원물검수", "세척/살균", "절단", "탈수", "MAP포장"],
        "packages": ["트레이", "컵", "용기", "필름포장"],
        "ingredients": [
            ("원물", "채소/과일 원물", "제한 가능", ""),
            ("드레싱", "소스/드레싱 별첨 후보", "가능", "대두/우유"),
            ("토핑", "곡물/견과/치즈 후보", "가능", "견과류/우유"),
            ("유통", "냉장 콜드체인 기준", "제한 가능", ""),
        ],
        "questions": ["세척수/살균 공정 기록을 제공할 수 있는가", "MAP 또는 필름 실링 포장이 가능한가"],
    },
    "dairy_alt": {
        "label": "유제품/대체유",
        "aliases": ["우유", "요거트", "치즈", "대체유", "두유", "오트밀크", "발효유"],
        "process": ["원료배합", "균질/여과", "살균", "발효/냉각", "충진"],
        "packages": ["병", "컵", "팩", "카톤"],
        "ingredients": [
            ("베이스", "원유/두유/오트 추출액", "제한 가능", "우유/대두"),
            ("발효", "유산균/스타터 후보", "제한 가능", "우유"),
            ("향미", "과채/곡물/천연향 후보", "가능", ""),
            ("안정화", "침전/분리 방지 소재", "가능", ""),
        ],
        "questions": ["유가공 또는 식물성 음료 설비 범위가 맞는가", "냉장/상온 유통 중 어떤 기준이 가능한가"],
    },
    "coffee_tea": {
        "label": "커피/차",
        "aliases": ["커피", "차", "티", "콜드브루", "원두", "티백", "허브티"],
        "process": ["원료선별", "추출/로스팅", "혼합", "여과", "충진/포장"],
        "packages": ["스틱", "티백", "병", "캔", "파우치"],
        "ingredients": [
            ("베이스", "커피 원두/찻잎/허브 원료", "가능", ""),
            ("향미", "과일/허브/향료 후보", "가능", ""),
            ("감미", "설탕/대체당 후보", "가능", ""),
            ("기능성", "카페인/디카페인/블렌딩 기준", "제한 가능", ""),
        ],
        "questions": ["원두 로스팅 또는 추출 설비를 보유했는가", "스틱/티백/RTD 중 가능한 포장 범위는 무엇인가"],
    },
    "confectionery": {
        "label": "캔디/젤리/초콜릿",
        "aliases": ["캔디", "젤리", "구미", "초콜릿", "카라멜", "츄잉"],
        "process": ["원료용해", "배합", "성형", "냉각/건조", "개별포장"],
        "packages": ["개별포장", "파우치", "박스", "블리스터"],
        "ingredients": [
            ("당류", "설탕/올리고당/대체당 후보", "가능", ""),
            ("겔화", "젤라틴/펙틴/한천 후보", "제한 가능", ""),
            ("향미", "과일 농축액/천연향 후보", "가능", ""),
            ("코팅", "초콜릿/당의/광택 후보", "가능", "우유"),
        ],
        "questions": ["젤리/캔디 성형 몰드와 소량 배치가 가능한가", "개별포장과 표시사항 작업 범위는 어디까지인가"],
    },
    "supplement": {
        "label": "건기식/영양제",
        "aliases": ["건기식", "건강기능식품", "영양제", "정제", "캡슐", "프로바이오틱스"],
        "process": ["원료칭량", "혼합", "타정/캡슐충전", "선별", "병입/블리스터"],
        "packages": ["병", "PTP", "블리스터", "스틱", "파우치", "바이알"],
        "ingredients": [
            ("기능성 원료", "비타민/미네랄/프로바이오틱스 후보", "제한 가능", ""),
            ("부형제", "결합제/활택제 후보", "가능", ""),
            ("코팅", "장용/필름 코팅 후보", "제한 가능", ""),
            ("표시", "기능성 표시와 섭취량 기준", "제한 가능", ""),
        ],
        "questions": ["건강기능식품 GMP 인증 범위가 맞는가", "기능성 원료 인정서류와 시험성적서 제공이 가능한가"],
    },
    "baby_food": {
        "label": "영유아식",
        "aliases": ["영유아식", "이유식", "아기과자", "키즈", "어린이식", "유아간식"],
        "process": ["원료검수", "저염/저당 배합", "가열", "분쇄/충진", "살균포장"],
        "packages": ["파우치", "컵", "용기", "스틱"],
        "ingredients": [
            ("베이스", "쌀/채소/과일/단백질 원료", "제한 가능", ""),
            ("영양", "칼슘/철분/비타민 후보", "제한 가능", "우유"),
            ("식감", "입자 크기/연하 단계 기준", "제한 가능", ""),
            ("보존", "무첨가/저염 기준과 살균 조건", "제한 가능", ""),
        ],
        "questions": ["영유아 대상 표시/원료 기준을 검토할 수 있는가", "입자 크기와 단계별 물성 관리가 가능한가"],
    },
    "frozen_dessert": {
        "label": "아이스크림/냉동디저트",
        "aliases": ["아이스크림", "젤라또", "빙과", "냉동디저트", "샤베트"],
        "process": ["배합", "살균", "균질", "동결/성형", "냉동포장"],
        "packages": ["컵", "바포장", "파우치", "박스"],
        "ingredients": [
            ("베이스", "유제품/식물성 베이스", "제한 가능", "우유/대두"),
            ("감미", "설탕/대체당 후보", "가능", ""),
            ("향미", "과일/초콜릿/견과 후보", "가능", "견과류"),
            ("물성", "오버런/빙결점/안정제 후보", "제한 가능", ""),
        ],
        "questions": ["냉동 성형과 -18도 이하 보관 라인이 맞는가", "소량 컵/바 포장 테스트가 가능한가"],
    },
    "oil_seasoning": {
        "label": "식용유/조미소재",
        "aliases": ["식용유", "오일", "참기름", "들기름", "조미료", "시즈닝", "분말조미"],
        "process": ["원료정선", "착유/혼합", "여과", "충진", "라벨링"],
        "packages": ["병", "캔", "파우치", "스틱"],
        "ingredients": [
            ("베이스", "식물성 오일/분말 조미 베이스", "가능", ""),
            ("향미", "향신료/허브/천연향 후보", "가능", ""),
            ("안정화", "산패/흡습 방지 기준", "제한 가능", ""),
            ("첨가", "소금/당/아미노산 후보", "가능", ""),
        ],
        "questions": ["착유/혼합/분말 블렌딩 중 가능한 공정은 무엇인가", "산가/과산화물가 등 품질 시험을 지원하는가"],
    },
    "canned_retort": {
        "label": "캔/레토르트",
        "aliases": ["캔", "통조림", "레토르트", "파우치캔", "멸균", "상온간편식"],
        "process": ["원료전처리", "조리", "충진", "밀봉", "멸균/냉각"],
        "packages": ["캔", "레토르트파우치", "트레이", "용기"],
        "ingredients": [
            ("주재료", "육류/수산/채소/곡류 조합", "제한 가능", "고기/생선"),
            ("소스", "육수/양념/오일 후보", "가능", "대두/밀"),
            ("보존", "F0값/멸균 조건 기준", "제한 가능", ""),
            ("물성", "가열 후 식감 유지 소재", "가능", ""),
        ],
        "questions": ["상온 유통 레토르트 조건과 F0값 검증이 가능한가", "캔 또는 파우치 충진 라인의 MOQ는 얼마인가"],
    },
    "pet_food": {
        "label": "펫푸드",
        "aliases": ["펫푸드", "강아지", "고양이", "반려동물", "트릿", "사료"],
        "process": ["원료분쇄", "배합", "성형/압출", "건조", "포장"],
        "packages": ["파우치", "캔", "트레이", "박스"],
        "ingredients": [
            ("단백질", "육류/수산/식물성 단백 후보", "제한 가능", "고기/생선"),
            ("탄수화물", "곡물/전분/고구마 후보", "가능", ""),
            ("기능성", "관절/피모/장건강 후보", "제한 가능", ""),
            ("보존", "수분함량/수분활성 관리", "제한 가능", ""),
        ],
        "questions": ["펫푸드 전용 또는 교차오염 관리 라인이 있는가", "간식/습식/건식 중 가능한 제형은 무엇인가"],
    },
    "alcohol_low_no": {
        "label": "주류/논알콜",
        "aliases": ["주류", "논알콜", "무알콜", "맥주", "와인", "하이볼", "발효주"],
        "process": ["원료배합", "발효/추출", "여과", "살균", "병입/캔입"],
        "packages": ["병", "캔", "팩", "카톤"],
        "ingredients": [
            ("베이스", "맥아/과실/차/향미 베이스", "제한 가능", ""),
            ("발효", "효모/당화 조건 후보", "제한 가능", ""),
            ("향미", "홉/과일/허브 후보", "가능", ""),
            ("탄산", "탄산 주입과 용존 CO2 기준", "제한 가능", ""),
        ],
        "questions": ["주류 면허 또는 논알콜 음료 범위가 맞는가", "병/캔 충진과 탄산 안정성 테스트가 가능한가"],
    },
}

PUBLIC_PRODUCT_CASE_KEYS = tuple(PRODUCT_CASES.keys())
PUBLIC_PRODUCT_CASE_SET = set(PUBLIC_PRODUCT_CASE_KEYS)

ALL_PACKAGE_TYPES = sorted({package for meta in PRODUCT_CASES.values() for package in meta["packages"]})
MATCHABLE_PACKAGE_TYPES = sorted(ALL_PACKAGE_TYPES, key=lambda value: (-len(value), value))
PACKAGE_ALIASES = {
    "레토르트파우치": ["레토르트파우치", "레토르트 파우치"],
    "스파우트파우치": ["스파우트파우치", "스파우트 파우치"],
    "지퍼팩": ["지퍼팩", "지퍼 팩", "지퍼파우치", "지퍼 파우치"],
    "PET병": ["PET병", "PET 병", "페트병"],
    "유리병": ["유리병", "글라스병", "글라스 병"],
    "바이알": ["바이알", "vial"],
    "스틱": ["스틱포", "스틱 포"],
    "바포장": ["바포장", "바 포장"],
    "PTP": ["PTP", "ptp"],
}

SALES_TYPES = {"D2C", "공동구매", "프랜차이즈", "PB", "B2B", "B2C", "정찰제", "입찰", "샘플입찰"}

VIBE_TARGETS = {
    "solo": "1인 가구 온라인 테스트 고객",
    "family": "가족 단위 건강 간편식 구매자",
    "office": "오피스 간식/식사 대체 수요",
    "senior": "고령친화/케어푸드 수요",
    "franchise": "프랜차이즈 반복 발주 담당자",
}

VIBE_SCENES = {
    "breakfast": "아침 대용",
    "snack": "오후 간식",
    "meal": "간편 식사",
    "workout": "운동 전후 보충",
    "gift": "시즌 한정 선물/프로모션",
}

VIBE_TEXTURES = {
    "crispy": "바삭한 식감",
    "chewy": "쫀득한 식감",
    "soft": "부드러운 식감",
    "creamy": "크리미한 질감",
    "clean": "깔끔한 목넘김",
}

VIBE_PROCESS_MODES = {
    "low_sugar": "저당 설계",
    "high_protein": "고단백 설계",
    "hmr": "HMR/RMR 제조",
    "powder": "분말/스틱 충진",
    "sauce": "가열 살균 소스",
    "care": "케어푸드 물성 관리",
    "beverage": "액상 살균/충진",
    "fermented": "발효/숙성 관리",
    "frozen": "냉장/냉동 HMR",
    "baking": "베이커리 소량 생산",
    "vegan": "식물성 대체식",
}

VERIFIED_EXTRA_FACTORIES = [
    (
        "REAL-DAOOM",
        "다움",
        "건강식품/일반식품 OEM/ODM",
        "건강식품,일반식품,분말,동결건조분말,정제,캡슐,환,과립,액상,스틱젤리,사면파우치,중형스틱,미니스틱,지퍼팩,스파우트파우치,병,바이알",
        "",
        "경남 사천",
        "A",
        "https://www.daoom.co.kr/production",
        "공식 생산제형/포장 페이지에서 사면파우치, 지퍼팩, 스파우트파우치, 병, 바이알 대응 범위를 확인",
    ),
    (
        "REAL-HANKUKNEST",
        "한국네스트",
        "건강식품/액상음료 OEM/ODM",
        "건강식품,액상,음료,스틱,스탠딩파우치,사면파우치,병,이중제형,캡분말,용기,환,과립,HACCP,GMP",
        "HACCP,GMP",
        "경기 포천",
        "A",
        "https://www.nestkorea.co.kr/sub/business_1.php",
        "공식 사업소개 페이지에서 액상·음료용 스틱, 스탠딩파우치, 사면파우치, 병 포장과 HACCP/GMP를 확인",
    ),
    (
        "REAL-NATURETECH",
        "네이처텍",
        "건강기능식품/일반식품 OEM/ODM",
        "건강기능식품,일반식품,PP용기,PP바이알,액상파우치,액상스틱,젤리스틱,이중제형,정제,PTP,분말스틱,분말스파우트파우치",
        "",
        "충북 진천",
        "A",
        "https://naturetech.co.kr/contact/",
        "공식 견적문의 페이지에서 액상파우치, 액상스틱, 젤리스틱, PP바이알, 정제 PTP, 분말스파우트파우치 선택 항목을 확인",
    ),
    (
        "REAL-NEXTBIO",
        "넥스트바이오",
        "음료/커피 OEM/ODM",
        "음료,커피,티,허브,액상스틱,액상파우치,NFC주스,PET병,Glass병,건강기능식품,OEM,ODM",
        "",
        "강원 횡성",
        "A",
        "https://nextbio.co.kr/oemodm/oem",
        "공식 OEM·ODM 페이지에서 커피 액상 파우치, 액상 스틱, PET/Glass 병, NFC 주스 생산 범위를 확인",
    ),
    (
        "REAL-SOALLDAM",
        "소올담",
        "소스 OEM/ODM",
        "소스,소스충진,1회용소스,업소용소스,파우치,용기,실링,레시피,HACCP,소량,대량",
        "HACCP",
        "미확인",
        "A",
        "https://soalldam.kr/oem-odm-2/",
        "공식 OEM/ODM 페이지에서 1회용 소스와 업소용 소스 충진, 소량/대량 생산 범위를 확인",
    ),
    (
        "REAL-DONGGREEN",
        "동그린",
        "아이스크림/빙과 OEM",
        "아이스크림,빙과,빙과류 OEM,냉동디저트,컵,바포장,HACCP",
        "HACCP",
        "강원 강릉",
        "A",
        "https://eastgreen.co.kr/",
        "공식 사이트에서 빙과류 OEM 공급 및 인증서 정보를 확인",
    ),
    (
        "REAL-MARGO",
        "마고푸드랩",
        "디저트 OEM/ODM",
        "디저트,케이크,티라미수,베이글,냉동생지,냉동디저트,HACCP,OEM,ODM",
        "HACCP",
        "수도권",
        "A",
        "https://margofoodlab.com/oem.html",
        "공식 OEM 페이지에서 HACCP 자체 제조와 디저트 OEM/ODM 범위를 확인",
    ),
    (
        "REAL-ATBIOMISO",
        "ATBIO&MISO",
        "펫푸드 OEM/ODM",
        "펫푸드,펫사료,간식,ODM,OEM,시제품,제품화,파우치,트레이",
        "",
        "미확인",
        "A",
        "https://atbio-miso.co.kr/business1/",
        "공식 ODM/OEM 페이지에서 펫푸드 전 영역 토탈서비스를 확인",
    ),
    (
        "REAL-PETONE",
        "펫원",
        "펫푸드 OEM/ODM",
        "펫푸드,반려동물,사료,간식,ODM,OEM,제조생산,출하",
        "",
        "미확인",
        "A",
        "https://petone.kr/oem/",
        "공식 OEM/ODM 페이지에서 펫푸드 제조생산 협업 범위를 확인",
    ),
    (
        "REAL-OSP",
        "오에스피",
        "유기농 펫사료 제조",
        "펫푸드,유기농 펫사료,반려동물,사료,ODM,HACCP,건식",
        "HACCP",
        "충남 논산",
        "A",
        "https://www.osppetfood.com/kor/about_osp/history.html",
        "공식 연혁에서 ODM 체결 및 HACCP 인증 이력을 확인",
    ),
    (
        "REAL-JEJUBEER",
        "제주맥주",
        "주류/논알콜 OEM/ODM",
        "주류,맥주,기타주류,탄산음료,논알콜,OEM,ODM,캔,병",
        "",
        "제주",
        "A",
        "https://hanwoolnjeju.com/oemodm",
        "공식 OEM/ODM 문의 페이지에서 맥주/기타주류/논알콜 탄산음료 생산 범위를 확인",
    ),
    (
        "REAL-PLATINUM",
        "플래티넘크래프트맥주",
        "수제맥주 OEM/ODM",
        "주류,맥주,수제맥주,캔맥주,하이볼,OEM,ODM,캔",
        "",
        "서울/충북",
        "A",
        "https://www.platinumbeer.com/OEM-ODM",
        "공식 OEM/ODM 페이지에서 수제맥주 PB 및 캔 하이볼 문의 범위를 확인",
    ),
    (
        "REAL-DAEHANJUJO",
        "대한주조",
        "주류 맞춤 제조",
        "주류,막걸리,전통주,맞춤 제작,OEM,수출,병입",
        "",
        "미확인",
        "B",
        "https://www.daehanjujo.kr/main.php",
        "공식 사이트에서 1:1 맞춤 주류 제작과 OEM 문의 정보를 확인",
    ),
    (
        "REAL-BEBECOOK",
        "베베쿡",
        "영유아식 제조",
        "이유식,영유아식,채소,육류,과일,생산 시스템,HACCP,안심용기",
        "HACCP",
        "강원 춘천",
        "B",
        "https://www.bebecook.com/page/service/production",
        "공식 생산 안내에서 이유식 생산 시스템과 HACCP 정보를 확인",
    ),
    (
        "REAL-ERCOHS",
        "에르코스",
        "이유식 제조",
        "이유식,영유아식,국산 과일,채소,한우,당일 생산,파우치,용기",
        "",
        "미확인",
        "B",
        "https://www.ercohs.com/default/business/baby_food.php",
        "공식 사업 페이지에서 이유식 제조 정보를 확인",
    ),
    (
        "REAL-SANGIL",
        "상일",
        "음료 OEM/ODM",
        "음료,캔음료,병음료,펫음료,차음료,에너지드링크,ODM,OEM,캔,병",
        "",
        "경북",
        "B",
        "https://isangil.com/kr/",
        "공식 사이트에서 캔음료/병음료와 OEM/ODM 수출 제품 정보를 확인",
    ),
    (
        "REAL-DAEHOFOOD",
        "대호식품",
        "파우더 OEM/ODM",
        "아이스크림용 파우더,파우더,분말,스틱,OEM,ODM,조미소재",
        "",
        "미확인",
        "B",
        "https://daehofood.co.kr/oem",
        "공식 OEM/ODM 페이지에서 아이스크림용 파우더 제조 범위를 확인",
    ),
]

VERIFIED_COMPANY_URL_OVERRIDES = {
    "이앤에스(주)": (
        "https://enscorp.co.kr/oem-odm/",
        "공식페이지확인",
        "공식 OEM/ODM 페이지에서 스틱포, PTP, 블리스터, 병 포장 대응 범위를 확인",
    ),
    "(주)에스엘에스": (
        "https://slsltd.co.kr/oem-odm/",
        "공식페이지확인",
        "공식 OEM/ODM 페이지에서 GMP/HACCP 기반 스틱포, PTP, 블리스터, 병 포장 범위를 확인",
    ),
    "대동고려삼(주)": (
        "https://ddkorea.co.kr/wp/wp-content/uploads/2018/07/ddk_catalogue.pdf",
        "공식페이지확인",
        "공식 카탈로그에서 PTP, 병포장, 스틱포장, 삼면포장, 사면포장, 스탠딩 포장 범위를 확인",
    ),
    "케이지랩 주식회사": (
        "https://kglab.co.kr/75",
        "공식페이지확인",
        "공식 설비 페이지에서 액상 스틱파우치, 파우치, 젤리스틱, 바이알, PTP 설비를 확인",
    ),
    "네추럴웨이": (
        "https://www.naturalway.co.kr/kr/business/intro.php",
        "공식페이지확인",
        "공식 사업안내 페이지에서 병, 블리스터, 사면포, 파우치, 바이알, 이중캡, 액상/분말/젤리 스틱 포장 범위를 확인",
    ),
    "엔피케이(주)": (
        "https://npkor.co.kr/58",
        "공식페이지확인",
        "공식 생산설비 페이지에서 PTP 몰드와 대량 생산 설비를 확인",
    ),
    "푸드트리": (
        "https://ifoodtree.com/shopinfo/company.html",
        "공식사이트확인",
        "공식 회사소개 기준 영유아/케어푸드 전문 기업 정보 확인",
    ),
}


class Base(DeclarativeBase):
    pass


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    company_type: Mapped[str] = mapped_column(String(40), default="brand")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(80))
    role: Mapped[str] = mapped_column(String(30), default="operator")
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))


class Factory(Base):
    __tablename__ = "factories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    factory_code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    company_name: Mapped[str] = mapped_column(String(200), index=True)
    primary_category: Mapped[str] = mapped_column(String(160), index=True)
    product_keywords: Mapped[str] = mapped_column(Text)
    oem_signal: Mapped[bool] = mapped_column(Boolean, default=False)
    odm_signal: Mapped[bool] = mapped_column(Boolean, default=False)
    certification_signal: Mapped[str] = mapped_column(String(220), default="")
    location_signal: Mapped[str] = mapped_column(String(160), default="")
    mvp_fit: Mapped[str] = mapped_column(String(5), index=True, default="C")
    source_url: Mapped[str] = mapped_column(Text, default="")
    verification_status: Mapped[str] = mapped_column(String(40), index=True, default="미확인")
    next_action: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: now_utc())


class RegulatoryRule(Base):
    __tablename__ = "regulatory_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rule_id: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    scope: Mapped[str] = mapped_column(String(80), index=True)
    trigger_field: Mapped[str] = mapped_column(String(80), index=True)
    trigger_value: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(20), index=True)
    check_item: Mapped[str] = mapped_column(Text)
    required_evidence: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(Text)
    system_action: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ProductRequest(Base):
    __tablename__ = "product_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_uid: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    visitor_id: Mapped[str] = mapped_column(String(80), index=True, default="")
    product_case: Mapped[str] = mapped_column(String(40), index=True)
    product_case_label: Mapped[str] = mapped_column(String(40))
    raw_prompt: Mapped[str] = mapped_column(Text)
    sales_type: Mapped[str] = mapped_column(String(40), index=True)
    target_qty: Mapped[int] = mapped_column(Integer, index=True)
    qty_unit: Mapped[str] = mapped_column(String(20), default="개")
    package_type: Mapped[str] = mapped_column(String(60), default="")
    llm_model: Mapped[str] = mapped_column(String(80), default=SAM_DEFAULT_MODEL)
    claim_list: Mapped[str] = mapped_column(Text, default="[]")
    taste_tags: Mapped[str] = mapped_column(Text, default="[]")
    target_price: Mapped[str] = mapped_column(String(80), default="")
    budget_amount: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String(40), index=True, default="draft")
    is_dummy: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: now_utc(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: now_utc())

    spec: Mapped["ProductSpec"] = relationship(back_populates="request", cascade="all, delete-orphan")
    recipe: Mapped["RecipeDraft"] = relationship(back_populates="request", cascade="all, delete-orphan")


class ProductSpec(Base):
    __tablename__ = "product_specs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("product_requests.id"), unique=True, index=True)
    concept: Mapped[str] = mapped_column(Text)
    process_list: Mapped[str] = mapped_column(Text)
    package_condition: Mapped[str] = mapped_column(Text)
    storage_condition: Mapped[str] = mapped_column(String(80))
    cost_assumption: Mapped[str] = mapped_column(Text)
    validation_questions: Mapped[str] = mapped_column(Text)
    request: Mapped[ProductRequest] = relationship(back_populates="spec")


class RecipeDraft(Base):
    __tablename__ = "recipe_drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("product_requests.id"), unique=True, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    batch_size: Mapped[str] = mapped_column(String(80))
    unit_weight: Mapped[str] = mapped_column(String(80))
    yield_rate: Mapped[float] = mapped_column(Float, default=0.92)
    quality_targets: Mapped[str] = mapped_column(Text)
    request: Mapped[ProductRequest] = relationship(back_populates="recipe")


class IngredientLine(Base):
    __tablename__ = "ingredient_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("product_requests.id"), index=True)
    ingredient_role: Mapped[str] = mapped_column(String(80))
    ingredient_name: Mapped[str] = mapped_column(String(180))
    ratio_range: Mapped[str] = mapped_column(String(60))
    allergen_flag: Mapped[str] = mapped_column(String(120), default="")
    substitute_allowed: Mapped[str] = mapped_column(String(40), default="가능")


class ScreeningRun(Base):
    __tablename__ = "screening_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("product_requests.id"), index=True)
    overall_status: Mapped[str] = mapped_column(String(20), index=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: now_utc())


class ScreeningFinding(Base):
    __tablename__ = "screening_findings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    screening_run_id: Mapped[int] = mapped_column(ForeignKey("screening_runs.id"), index=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("product_requests.id"), index=True)
    rule_id: Mapped[str] = mapped_column(String(20), index=True)
    severity: Mapped[str] = mapped_column(String(20), index=True)
    message: Mapped[str] = mapped_column(Text)
    required_evidence: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(Text)


class MatchResult(Base):
    __tablename__ = "match_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("product_requests.id"), index=True)
    factory_id: Mapped[int] = mapped_column(ForeignKey("factories.id"), index=True)
    score: Mapped[float] = mapped_column(Float, index=True)
    reason: Mapped[str] = mapped_column(Text)
    confirm_questions: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="candidate", index=True)


class ProductPlan(Base):
    __tablename__ = "product_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("product_requests.id"), unique=True, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(40), index=True, default="plan_ready")
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: now_utc())


class SampleBrief(Base):
    __tablename__ = "sample_briefs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("product_requests.id"), unique=True, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(40), index=True, default="draft")
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: now_utc())


class PurchaseOrderRequest(Base):
    __tablename__ = "purchase_order_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    po_uid: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("product_requests.id"), index=True)
    status: Mapped[str] = mapped_column(String(40), index=True, default="draft")
    order_type: Mapped[str] = mapped_column(String(40), default="sample_po")
    buyer_company: Mapped[str] = mapped_column(String(160), default="")
    buyer_contact: Mapped[str] = mapped_column(String(120), default="")
    supplier_company: Mapped[str] = mapped_column(String(160), default="")
    supplier_contact: Mapped[str] = mapped_column(String(120), default="")
    order_date: Mapped[str] = mapped_column(String(20), default="")
    due_date: Mapped[str] = mapped_column(String(20), default="")
    delivery_place: Mapped[str] = mapped_column(Text, default="")
    payment_terms: Mapped[str] = mapped_column(Text, default="")
    delivery_terms: Mapped[str] = mapped_column(Text, default="")
    inspection_terms: Mapped[str] = mapped_column(Text, default="")
    vat_type: Mapped[str] = mapped_column(String(40), default="VAT 별도")
    currency: Mapped[str] = mapped_column(String(10), default="KRW")
    raw_order_form: Mapped[str] = mapped_column(Text, default="")
    line_items: Mapped[str] = mapped_column(Text, default="[]")
    subtotal: Mapped[float] = mapped_column(Float, default=0)
    vat_amount: Mapped[float] = mapped_column(Float, default=0)
    total_amount: Mapped[float] = mapped_column(Float, default=0)
    quality_terms: Mapped[str] = mapped_column(Text, default="[]")
    required_documents: Mapped[str] = mapped_column(Text, default="[]")
    risk_flags: Mapped[str] = mapped_column(Text, default="[]")
    is_dummy: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: now_utc(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: now_utc())


class ProcurementBoardPost(Base):
    __tablename__ = "procurement_board_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    post_uid: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("product_requests.id"), index=True)
    purchase_order_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_order_requests.id"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(180), index=True)
    status: Mapped[str] = mapped_column(String(30), index=True, default="published")
    summary: Mapped[str] = mapped_column(Text, default="")
    target_snapshot: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: now_utc(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: now_utc())


class ProcurementBoardBid(Base):
    __tablename__ = "procurement_board_bids"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    board_post_id: Mapped[int] = mapped_column(ForeignKey("procurement_board_posts.id"), index=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("product_requests.id"), index=True)
    factory_id: Mapped[int | None] = mapped_column(ForeignKey("factories.id"), nullable=True, index=True)
    vendor_name: Mapped[str] = mapped_column(String(160), index=True)
    vendor_profile: Mapped[str] = mapped_column(String(80), default="")
    response_status: Mapped[str] = mapped_column(String(40), index=True, default="검토중")
    response_summary: Mapped[str] = mapped_column(Text, default="")
    quote_total: Mapped[float] = mapped_column(Float, default=0)
    unit_quote: Mapped[float] = mapped_column(Float, default=0)
    brokerage_fee: Mapped[float] = mapped_column(Float, default=0)
    total_with_fee: Mapped[float] = mapped_column(Float, default=0)
    moq: Mapped[int] = mapped_column(Integer, default=0)
    lead_time_days: Mapped[int] = mapped_column(Integer, default=0)
    budget_fit: Mapped[str] = mapped_column(String(40), index=True, default="예산미입력")
    budget_gap: Mapped[float] = mapped_column(Float, default=0)
    bid_score: Mapped[float] = mapped_column(Float, index=True, default=0)
    custom_order_rules: Mapped[str] = mapped_column(Text, default="[]")
    required_documents: Mapped[str] = mapped_column(Text, default="[]")
    risk_notes: Mapped[str] = mapped_column(Text, default="[]")
    counter_offer: Mapped[str] = mapped_column(Text, default="")
    ai_reasoning: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: now_utc(), index=True)


class CostCalculation(Base):
    __tablename__ = "cost_calculations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("product_requests.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    target_qty: Mapped[int] = mapped_column(Integer)
    serving_unit: Mapped[str] = mapped_column(String(30))
    total_cost: Mapped[float] = mapped_column(Float)
    unit_cost: Mapped[float] = mapped_column(Float)
    supply_price: Mapped[float] = mapped_column(Float)
    vat_included_total: Mapped[float] = mapped_column(Float)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: now_utc())


class ToolRun(Base):
    __tablename__ = "tool_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("product_requests.id"), index=True)
    tool_name: Mapped[str] = mapped_column(String(80), index=True)
    input_hash: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(30), index=True)
    summary: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: now_utc())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_code: Mapped[str] = mapped_column(String(80), default="")


class GeneratedFile(Base):
    __tablename__ = "generated_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_uid: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("product_requests.id"), index=True)
    doc_type: Mapped[str] = mapped_column(String(40), index=True)
    storage_path: Mapped[str] = mapped_column(Text)
    checksum: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: now_utc())


engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def db_session():
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def as_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def from_json(value: str, default: Any) -> Any:
    try:
        return json.loads(value) if value else default
    except json.JSONDecodeError:
        return default


def make_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def api_error(status_code: int, detail: str) -> None:
    response = jsonify({"detail": detail})
    response.status_code = status_code
    abort(response)


def query_int(name: str, default: int, min_value: int | None = None, max_value: int | None = None) -> int:
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        value = default
    if min_value is not None:
        value = max(value, min_value)
    if max_value is not None:
        value = min(value, max_value)
    return value


def query_bool(name: str) -> bool | None:
    value = request.args.get(name)
    if value is None:
        return None
    return value.lower() in {"1", "true", "yes", "y"}


def current_visitor_id(required: bool = True) -> str:
    visitor_id = (
        request.headers.get(VISITOR_HEADER_NAME, "").strip()
        or request.args.get(VISITOR_QUERY_PARAM, "").strip()
    )
    if visitor_id:
        return visitor_id[:80]
    if required:
        api_error(400, "visitor_id_required")
    return ""


def download_url_for(file_uid: str) -> str:
    return f"/api/files/{file_uid}/download?{VISITOR_QUERY_PARAM}={quote(current_visitor_id(), safe='')}"


def document_type_label(doc_type: str) -> str:
    return {
        "product_plan": "기획안 PDF",
        "sample_brief": "발주안 PDF",
        "purchase_order": "발주요청서 PDF",
    }.get(doc_type, f"{doc_type} PDF")


def json_payload(model: type[BaseModel]) -> BaseModel:
    try:
        return model.model_validate(request.get_json(silent=True) or {})
    except Exception as exc:
        api_error(422, str(exc))


class ProductRequestCreate(BaseModel):
    raw_prompt: str = Field(..., min_length=2)
    product_case: str | None = None
    sales_type: str = "D2C"
    target_qty_text: str | None = None
    target_qty: int | None = Field(None, ge=1)
    qty_unit: str | None = None
    package_type: str | None = None
    claim_list: list[str] = Field(default_factory=list)
    answers: dict[str, str] = Field(default_factory=dict)
    process_mode: str | None = None
    ingredient_keywords: list[str] = Field(default_factory=list)
    target_price: str | None = None
    budget_text: str | None = None
    budget_amount: float | None = Field(None, ge=0)
    llm_model: str = SAM_DEFAULT_MODEL
    use_llm: bool = True
    is_dummy: bool = False
    run_full: bool = True


class ProductRequestAgentPreview(BaseModel):
    raw_prompt: str = Field(..., min_length=2)
    product_case: str | None = None
    sales_type: str | None = None
    target_qty_text: str | None = None
    target_qty: int | None = Field(None, ge=1)
    qty_unit: str | None = None
    package_type: str | None = None
    claim_list: list[str] = Field(default_factory=list)
    process_mode: str | None = None
    ingredient_keywords: list[str] = Field(default_factory=list)
    target_price: str | None = None
    budget_text: str | None = None
    budget_amount: float | None = Field(None, ge=0)
    answers: dict[str, str] = Field(default_factory=dict)
    attempt_count: int = Field(0, ge=0, le=20)
    llm_model: str = SAM_DEFAULT_MODEL
    use_llm: bool = True


class VibeCookingCompose(BaseModel):
    product_case: str = "health_snack"
    base_idea: str = Field(..., min_length=2)
    target_customer: str = "solo"
    eating_scene: str = "snack"
    texture: str = "crispy"
    process_mode: str = "low_sugar"
    key_ingredients: list[str] = Field(default_factory=list)
    avoid_ingredients: list[str] = Field(default_factory=list)
    claim_list: list[str] = Field(default_factory=list)
    sales_type: str = "D2C"
    package_type: str = "개별포장"
    target_qty_text: str = "1,000개"
    target_price: str | None = None
    budget_text: str | None = None


class VibeAgentRun(BaseModel):
    planning_goal: str = "샘플 발주 가능한 제품 기획으로 정리"
    include_revision_prompt: bool = True


class ContactSimulationRun(BaseModel):
    negotiation_mode: str = "입찰"
    include_split_order: bool = True
    preferred_contact: str = "이메일"
    budget_amount: float | None = Field(None, ge=0)


class BoardPostCreate(BaseModel):
    title: str | None = None
    summary: str | None = None
    vendor_count: int = Field(4, ge=2, le=6)
    regenerate_bids: bool = True


class BoardBidRefresh(BaseModel):
    vendor_count: int = Field(4, ge=2, le=6)


class FactoryCreate(BaseModel):
    company_name: str
    primary_category: str
    product_keywords: str
    certification_signal: str = ""
    location_signal: str = ""
    mvp_fit: str = "B"
    source_url: str = ""
    verification_status: str = "수동등록"
    notes: str = ""


class FactoryPatch(BaseModel):
    verification_status: str | None = None
    active: bool | None = None
    notes: str | None = None
    next_action: str | None = None


class CostCalculationCreate(BaseModel):
    ingredient_cost: float = Field(450, ge=0)
    packaging_cost: float = Field(120, ge=0)
    manufacturing_fee: float = Field(250, ge=0)
    sample_fee: float = Field(300000, ge=0)
    test_fee: float = Field(250000, ge=0)
    logistics_fee: float = Field(100000, ge=0)
    platform_fee: float = Field(0, ge=0)
    vat_rate: float = Field(0.1, ge=0)
    margin_target: float = Field(0.35, ge=0, lt=0.95)
    serving_unit: str = "1개"


class PurchaseOrderLineCreate(BaseModel):
    item_name: str = Field(..., min_length=1)
    specification: str = ""
    unit: str = "개"
    quantity: float = Field(..., gt=0)
    unit_price: float = Field(0, ge=0)
    requested_delivery_date: str = ""
    notes: str = ""


class PurchaseOrderCreate(BaseModel):
    order_type: str = "sample_po"
    buyer_company: str = "CBT 운영사"
    buyer_contact: str = "브랜드 담당자"
    supplier_company: str = ""
    supplier_contact: str = ""
    order_date: str = ""
    due_date: str = ""
    delivery_place: str = ""
    payment_terms: str = "세금계산서 발행 후 30일 이내 지급"
    delivery_terms: str = "납품 전 일정 확정, 운송비 포함 여부 별도 확인"
    inspection_terms: str = "입고 수량, 외관, 표시사항, 시험성적서 확인 후 검수"
    vat_type: str = "VAT 별도"
    currency: str = "KRW"
    raw_order_form: str = ""
    line_items: list[PurchaseOrderLineCreate] = Field(default_factory=list)
    quality_terms: list[str] = Field(default_factory=list)
    required_documents: list[str] = Field(default_factory=list)
    is_dummy: bool = False


class PurchaseOrderFormParse(BaseModel):
    raw_order_form: str = Field(..., min_length=2)


def detect_case(raw_prompt: str, selected_case: str | None) -> str:
    if selected_case in PRODUCT_CASES:
        return selected_case
    detected = detect_case_hint(raw_prompt)
    if detected:
        return detected
    return "health_snack"


def detect_case_hint(raw_prompt: str) -> str | None:
    text = raw_prompt.lower()
    for case_key, meta in PRODUCT_CASES.items():
        if any(alias.lower() in text for alias in meta["aliases"]):
            return case_key
    return None


def resolve_public_product_case(raw_prompt: str, selected_case: str | None) -> str | None:
    detected_case = detect_case_hint(raw_prompt)
    if selected_case:
        if selected_case not in PRODUCT_CASES:
            return None
        if detected_case in PRODUCT_CASES:
            return detected_case
        return selected_case
    if detected_case in PRODUCT_CASES:
        return detected_case
    return None


def normalize_qty(target_qty: int | None, target_qty_text: str | None) -> tuple[int, str]:
    if target_qty:
        return target_qty, "개"
    text = target_qty_text or ""
    compact = text.replace(",", "")
    match = re.search(r"(\d+(?:\.\d+)?)\s*(만|천|톤|kg|KG|포|개|병|팩)?", compact)
    if not match:
        return 1000, "개"
    number = float(match.group(1))
    unit = match.group(2) or "개"
    if unit == "만":
        return int(number * 10000), "개"
    if unit == "천":
        return int(number * 1000), "개"
    if unit == "톤":
        return int(number * 1000), "kg"
    return int(number), unit.lower() if unit.upper() == "KG" else unit


def parse_budget_amount(budget_amount: float | None, *texts: str | None) -> float:
    if budget_amount is not None:
        return float(budget_amount)
    for text in texts:
        compact = str(text or "").replace(",", "").replace(" ", "")
        if not compact:
            continue
        match = re.search(r"(\d+(?:\.\d+)?)(억|천만|백만|만|원)?", compact)
        if not match:
            continue
        value = float(match.group(1))
        unit = match.group(2) or "원"
        if unit == "억":
            value *= 100000000
        elif unit == "천만":
            value *= 10000000
        elif unit == "백만":
            value *= 1000000
        elif unit == "만":
            value *= 10000
        return round(value, 1)
    return 0.0


def normalize_claims(raw_prompt: str, claim_list: list[str]) -> list[str]:
    candidates = ["저당", "무당", "무가당", "제로슈가", "고단백", "프로틴", "식이섬유", "비건", "혈당", "장건강", "면역", "다이어트", "저염", "나트륨 감소"]
    merged = list(dict.fromkeys([*claim_list, *[word for word in candidates if word in raw_prompt]]))
    return merged


def guess_package(case_key: str, raw_prompt: str, package_type: str | None) -> str:
    if package_type:
        return package_type
    normalized_prompt = raw_prompt.lower().replace(" ", "")
    for package, aliases in PACKAGE_ALIASES.items():
        if any(alias.lower().replace(" ", "") in normalized_prompt for alias in aliases):
            return package
    for package in MATCHABLE_PACKAGE_TYPES:
        if package.lower().replace(" ", "") in normalized_prompt:
            return package
    return PRODUCT_CASES[case_key]["packages"][0]


def clean_text_items(items: list[str]) -> list[str]:
    cleaned = []
    for item in items:
        value = str(item).strip()
        if value:
            cleaned.append(value)
    return list(dict.fromkeys(cleaned))


def normalize_process_mode(
    case_key: str,
    process_mode: str | None,
    raw_prompt: str,
    claim_list: list[str],
) -> str:
    if process_mode in VIBE_PROCESS_MODES:
        return process_mode
    joined = f"{raw_prompt} {' '.join(claim_list)}".lower()
    if case_key == "powder_stick":
        return "powder"
    if case_key == "sauce":
        return "sauce"
    if any(keyword in joined for keyword in ["고단백", "protein", "단백질"]):
        return "high_protein"
    return "low_sugar"


def generate_taste_tags(
    case_key: str,
    process_mode: str | None,
    claim_list: list[str],
    ingredient_keywords: list[str],
) -> list[str]:
    mode_label = VIBE_PROCESS_MODES.get(process_mode or "", process_mode or "")
    tags = [
        case_key,
        PRODUCT_CASES[case_key]["label"],
        process_mode or "",
        mode_label,
        *claim_list[:4],
        *clean_text_items(ingredient_keywords)[:4],
    ]
    return clean_text_items(tags)


def default_public_user_id(db: Session) -> int:
    user_id = db.scalar(select(User.id).order_by(User.id).limit(1))
    if not user_id:
        api_error(500, "default_user_not_found")
    return user_id


def load_owned_request_or_404(db: Session, request_id: int, visitor_id: str | None = None) -> ProductRequest:
    owner = visitor_id or current_visitor_id()
    req = db.scalar(select(ProductRequest).where(ProductRequest.id == request_id, ProductRequest.visitor_id == owner))
    if not req:
        api_error(404, "request_not_found")
    return req


def load_owned_purchase_order_or_404(
    db: Session,
    order_id: int,
    visitor_id: str | None = None,
) -> PurchaseOrderRequest:
    owner = visitor_id or current_visitor_id()
    order = db.scalar(
        select(PurchaseOrderRequest)
        .join(ProductRequest, ProductRequest.id == PurchaseOrderRequest.request_id)
        .where(PurchaseOrderRequest.id == order_id, ProductRequest.visitor_id == owner)
    )
    if not order:
        api_error(404, "purchase_order_not_found")
    return order


def load_owned_board_post_or_404(
    db: Session,
    post_id: int,
    visitor_id: str | None = None,
) -> ProcurementBoardPost:
    owner = visitor_id or current_visitor_id()
    post = db.scalar(
        select(ProcurementBoardPost)
        .join(ProductRequest, ProductRequest.id == ProcurementBoardPost.request_id)
        .where(ProcurementBoardPost.id == post_id, ProductRequest.visitor_id == owner)
    )
    if not post:
        api_error(404, "board_post_not_found")
    return post


def load_public_board_post_or_404(db: Session, post_id: int) -> ProcurementBoardPost:
    post = db.scalar(
        select(ProcurementBoardPost)
        .join(ProductRequest, ProductRequest.id == ProcurementBoardPost.request_id)
        .where(ProcurementBoardPost.id == post_id)
    )
    if not post:
        api_error(404, "board_post_not_found")
    return post


def load_owned_generated_file_or_404(
    db: Session,
    file_uid: str,
    visitor_id: str | None = None,
) -> GeneratedFile:
    owner = visitor_id or current_visitor_id()
    generated = db.scalar(
        select(GeneratedFile)
        .join(ProductRequest, ProductRequest.id == GeneratedFile.request_id)
        .where(GeneratedFile.file_uid == file_uid, ProductRequest.visitor_id == owner)
    )
    if not generated:
        api_error(404, "file_not_found")
    return generated


def serialize_generated_file_for_owner(generated: GeneratedFile) -> dict[str, Any]:
    return {
        "file_uid": generated.file_uid,
        "doc_type": generated.doc_type,
        "label": document_type_label(generated.doc_type),
        "download_url": download_url_for(generated.file_uid),
        "created_at": generated.created_at.isoformat(),
    }


def serialize_generated_file_for_board(generated: GeneratedFile) -> dict[str, Any]:
    return {
        "file_uid": generated.file_uid,
        "doc_type": generated.doc_type,
        "label": document_type_label(generated.doc_type),
        "created_at": generated.created_at.isoformat(),
    }


def vibe_options() -> dict[str, Any]:
    public_cases = [
        {"value": key, "label": PRODUCT_CASES[key]["label"], "packages": PRODUCT_CASES[key]["packages"]}
        for key in PUBLIC_PRODUCT_CASE_KEYS
    ]
    public_package_types = sorted({package for key in PUBLIC_PRODUCT_CASE_KEYS for package in PRODUCT_CASES[key]["packages"]})
    return {
        "product_cases": public_cases,
        "package_types": public_package_types,
        "targets": [{"value": key, "label": value} for key, value in VIBE_TARGETS.items()],
        "scenes": [{"value": key, "label": value} for key, value in VIBE_SCENES.items()],
        "textures": [{"value": key, "label": value} for key, value in VIBE_TEXTURES.items()],
        "process_modes": [{"value": key, "label": value} for key, value in VIBE_PROCESS_MODES.items()],
        "sales_types": sorted(SALES_TYPES),
    }


def compose_vibe_cooking(payload: VibeCookingCompose) -> dict[str, Any]:
    case_key = payload.product_case if payload.product_case in PRODUCT_CASES else detect_case(payload.base_idea, None)
    meta = PRODUCT_CASES[case_key]
    target = VIBE_TARGETS.get(payload.target_customer, payload.target_customer)
    scene = VIBE_SCENES.get(payload.eating_scene, payload.eating_scene)
    texture = VIBE_TEXTURES.get(payload.texture, payload.texture)
    process_mode = VIBE_PROCESS_MODES.get(payload.process_mode, payload.process_mode)
    key_ingredients = clean_text_items(payload.key_ingredients)
    avoid_ingredients = clean_text_items(payload.avoid_ingredients)
    claims = clean_text_items(payload.claim_list)
    if payload.process_mode == "low_sugar":
        claims.append("저당")
    if payload.process_mode == "high_protein":
        claims.append("고단백")
    if payload.process_mode == "care":
        claims.append("섭취편의")
    claims = list(dict.fromkeys(claims))

    prompt_parts = [
        payload.base_idea.strip(),
        f"제품군은 {meta['label']}이다.",
        f"제조 방향은 {process_mode}이며 {payload.sales_type} 판매를 전제로 한다.",
    ]
    if key_ingredients:
        prompt_parts.append(f"핵심 원료 후보는 {', '.join(key_ingredients)}이다.")
    if avoid_ingredients:
        prompt_parts.append(f"제외하거나 줄일 원료는 {', '.join(avoid_ingredients)}이다.")
    if claims:
        prompt_parts.append(f"강조 문구 후보는 {', '.join(claims)}이다.")
    if payload.target_price:
        prompt_parts.append(f"목표 가격/원가는 {payload.target_price} 기준으로 검토한다.")
    if payload.budget_text:
        prompt_parts.append(f"보유 예산은 {payload.budget_text} 범위로 본다.")
    prompt_parts.append(f"{payload.sales_type} 판매를 전제로 {payload.target_qty_text} 테스트 생산과 {payload.package_type} 포장을 검토한다.")

    raw_prompt = " ".join(prompt_parts)
    request_payload = {
        "raw_prompt": raw_prompt,
        "product_case": case_key,
        "sales_type": payload.sales_type if payload.sales_type in SALES_TYPES else "D2C",
        "target_qty_text": payload.target_qty_text,
        "package_type": payload.package_type or meta["packages"][0],
        "claim_list": claims,
        "process_mode": payload.process_mode,
        "ingredient_keywords": key_ingredients,
        "taste_tags": clean_text_items([target, scene, texture, process_mode, *key_ingredients[:3]]),
        "target_price": payload.target_price or "",
        "budget_text": payload.budget_text or "",
        "run_full": True,
    }
    return {
        "vibe_card": {
            "product_case": meta["label"],
            "target_customer": target,
            "eating_scene": scene,
            "texture": texture,
            "process_mode": process_mode,
            "key_ingredients": key_ingredients,
            "avoid_ingredients": avoid_ingredients,
            "claims": claims,
        },
        "request_payload": request_payload,
        "preview_prompt": raw_prompt,
    }


def infer_sales_type(raw_prompt: str, selected: str | None) -> str:
    if selected in SALES_TYPES:
        return selected
    text = raw_prompt.lower()
    hints = [
        ("샘플입찰", "샘플입찰"),
        ("공동구매", "공동구매"),
        ("프랜차이즈", "프랜차이즈"),
        ("pb", "PB"),
        ("입찰", "입찰"),
        ("b2b", "B2B"),
        ("납품", "B2B"),
        ("도매", "B2B"),
        ("카페", "B2B"),
        ("식자재", "B2B"),
        ("b2c", "B2C"),
        ("d2c", "D2C"),
        ("자사몰", "D2C"),
        ("스마트스토어", "D2C"),
        ("온라인", "D2C"),
    ]
    for keyword, value in hints:
        if keyword in text:
            return value
    return "D2C"


def split_hint_items(text: str | None) -> list[str]:
    if not text:
        return []
    return clean_text_items(re.split(r"[\n,;/]+", text))


def normalize_answer_map(answers: dict[str, str]) -> dict[str, str]:
    return {
        str(key).strip(): str(value).strip()
        for key, value in answers.items()
        if str(key).strip() and str(value).strip()
    }


def contains_any(text: str, keywords: list[str]) -> bool:
    return any(keyword.lower() in text.lower() for keyword in keywords)


def extract_ratio_bounds(text: str) -> tuple[float, float] | None:
    values = [float(value) for value in re.findall(r"\d+(?:\.\d+)?", text or "")]
    if not values:
        return None
    if len(values) == 1:
        return values[0], values[0]
    return min(values[0], values[1]), max(values[0], values[1])


def ingredient_bucket(role: str, name: str) -> str:
    text = f"{role} {name}".lower()
    if contains_any(text, ["주원료", "베이스", "정제수", "곡물", "육류", "수산", "단백질", "protein", "농축액", "과육"]):
        return "main"
    if contains_any(text, ["감미", "당", "시럽", "알룰로스", "에리스리톨", "스테비아"]):
        return "sweetener"
    if contains_any(text, ["향미", "향", "코코아", "허브", "향신료", "과일", "라떼"]):
        return "flavor"
    if contains_any(text, ["기능", "비타민", "미네랄", "유산균", "프로바이오틱", "효소"]):
        return "functional"
    if contains_any(text, ["안정", "유화", "점증", "보존", "염", "산"]):
        return "stabilizer"
    return "sub"


def fallback_ratio_weight(role: str, name: str, product_case: str) -> float:
    bucket = ingredient_bucket(role, name)
    case_weights = {
        "sauce": {"main": 48.0, "sweetener": 10.0, "flavor": 8.0, "functional": 4.0, "stabilizer": 3.0, "sub": 6.0},
        "beverage": {"main": 55.0, "sweetener": 9.0, "flavor": 6.0, "functional": 4.0, "stabilizer": 2.0, "sub": 5.0},
        "powder_stick": {"main": 42.0, "sweetener": 12.0, "flavor": 9.0, "functional": 5.0, "stabilizer": 2.0, "sub": 6.0},
        "health_snack": {"main": 40.0, "sweetener": 8.0, "flavor": 7.0, "functional": 5.0, "stabilizer": 3.0, "sub": 8.0},
    }
    default_weights = {"main": 36.0, "sweetener": 8.0, "flavor": 7.0, "functional": 4.0, "stabilizer": 2.0, "sub": 6.0}
    return case_weights.get(product_case, default_weights).get(bucket, 6.0)


def format_ratio(ratio: float) -> str:
    return f"{ratio:.1f}%" if ratio % 1 else f"{int(ratio)}%"


def build_recipe_formula_lines(req: ProductRequest, ingredients: list[IngredientLine]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    raw_total = 0.0
    for line in ingredients:
        bounds = extract_ratio_bounds(line.ratio_range)
        if bounds:
            weight = max((bounds[0] + bounds[1]) / 2, 0.3)
            basis = f"입력 비율 {line.ratio_range}의 중앙값 기준"
        else:
            weight = fallback_ratio_weight(line.ingredient_role, line.ingredient_name, req.product_case)
            basis = "입력 비율이 없어 원료 역할 기준으로 추정"
        raw_total += weight
        rows.append(
            {
                "role": line.ingredient_role,
                "name": line.ingredient_name,
                "ratio_range": line.ratio_range,
                "allergen_flag": line.allergen_flag,
                "substitute_allowed": line.substitute_allowed,
                "bucket": ingredient_bucket(line.ingredient_role, line.ingredient_name),
                "weight": weight,
                "basis": basis,
            }
        )
    if not rows:
        return []
    ratios = [round((row["weight"] / raw_total) * 100, 1) for row in rows]
    delta = round(100 - sum(ratios), 1)
    ratios[-1] = round(max(ratios[-1] + delta, 0.1), 1)
    for row, ratio in zip(rows, ratios):
        row["estimated_ratio"] = ratio
        row["display_ratio"] = format_ratio(ratio)
        row.pop("weight", None)
    rows.sort(key=lambda row: row["estimated_ratio"], reverse=True)
    return rows


def stage_materials(stage: str, formula_lines: list[dict[str, Any]]) -> list[str]:
    stage_text = stage.lower()
    if contains_any(stage_text, ["계량", "전처리", "분쇄", "선별"]):
        return [f"{row['name']} {row['display_ratio']}" for row in formula_lines[:4]]
    if contains_any(stage_text, ["배합", "혼합"]):
        main = [row for row in formula_lines if row["bucket"] == "main"][:2]
        additives = [row for row in formula_lines if row["bucket"] in {"sweetener", "flavor", "functional", "stabilizer"}][:3]
        selected = main + additives
        return [f"{row['name']} {row['display_ratio']}" for row in selected]
    if contains_any(stage_text, ["가열", "살균", "증숙", "조리"]):
        selected = [row for row in formula_lines if row["bucket"] in {"main", "sweetener", "stabilizer"}][:4]
        return [f"{row['name']} {row['display_ratio']}" for row in selected]
    return [f"{row['name']} {row['display_ratio']}" for row in formula_lines[:3]]


def stage_action(stage: str, req: ProductRequest, formula_lines: list[dict[str, Any]]) -> tuple[str, str]:
    stage_text = stage.lower()
    top_names = ", ".join(row["name"] for row in formula_lines[:3]) or "주요 원료"
    if contains_any(stage_text, ["계량", "전처리", "분쇄", "선별"]):
        return (
            f"{top_names}를 목표 배합비에 맞춰 계량하고 입도, 해동, 세척 상태를 먼저 맞춥니다.",
            "투입 오차가 작은 전처리 원료 세트",
        )
    if contains_any(stage_text, ["배합", "혼합"]):
        return (
            "주원료를 먼저 투입한 뒤 감미·향미·기능성 원료를 순차 투입해 균일하게 혼합합니다.",
            "원료 분산이 고른 반제품 또는 배합액",
        )
    if contains_any(stage_text, ["가열", "살균", "증숙", "조리"]):
        return (
            "배합액을 가열하면서 점도, 당도, 살균 조건을 맞춰 공정 편차를 줄입니다.",
            "미생물 리스크와 점도가 안정된 가열 반제품",
        )
    if contains_any(stage_text, ["굽기", "건조"]):
        return (
            "가열 또는 건조로 목표 수분까지 낮춰 저장 안정성과 식감을 맞춥니다.",
            "수분 편차가 줄어든 완성 직전 제품",
        )
    if contains_any(stage_text, ["성형", "압출"]):
        return (
            "배합물을 단위중량 기준으로 성형해 형상과 밀도를 맞춥니다.",
            "중량 편차가 관리된 성형품",
        )
    if contains_any(stage_text, ["체질"]):
        return (
            "혼합 분말을 체질해 뭉침과 이물 가능성을 줄입니다.",
            "입도 균일성이 개선된 분말",
        )
    if contains_any(stage_text, ["냉각"]):
        return (
            "포장 전 내부 온도를 내려 응축수와 변형을 막습니다.",
            "포장 가능한 온도로 안정화된 제품",
        )
    if contains_any(stage_text, ["발효", "숙성"]):
        return (
            "시간과 온도를 관리해 풍미 형성과 수분 이동을 안정화합니다.",
            "산미 또는 숙성 풍미가 정리된 중간품",
        )
    if contains_any(stage_text, ["충진", "포장", "병입", "캔입"]):
        return (
            f"{req.package_type} 규격에 맞춰 충진하고 밀봉 후 라벨과 중량을 확인합니다.",
            "출하 가능한 포장 완제품",
        )
    if contains_any(stage_text, ["금속검출", "검수"]):
        return (
            "최종 검사로 이물, 중량, 밀봉 상태를 확인합니다.",
            "검수 기준을 통과한 출하 제품",
        )
    return (
        f"{stage} 단계에서 표준 작업서를 기준으로 제품 편차를 조정합니다.",
        "다음 공정으로 넘길 수 있는 반제품",
    )


def recipe_prediction_templates(product_case: str) -> list[tuple[str, str]]:
    return {
        "powder_stick": [
            ("분산성", "분말 입도가 맞으면 용해 속도는 안정적이지만 향미 원료 후첨이 늦으면 뭉침이 생길 수 있습니다."),
            ("풍미", "감미료와 향미 원료 비중이 높으면 초반 맛은 선명하지만 끝맛 잔향을 확인해야 합니다."),
            ("보관성", "흡습과 분말 층분리를 막기 위해 포장 내 수분 관리가 중요합니다."),
        ],
        "sauce": [
            ("점도", "가열 후 냉각 구간에서 점도가 한 번 더 올라갈 가능성이 높아 냉간 점도 확인이 필요합니다."),
            ("풍미", "당·산·향신료 밸런스가 맞으면 첫맛은 강하게 나오지만 살균 후 향 손실을 볼 수 있습니다."),
            ("안정성", "pH와 충진 온도 편차가 크면 층분리 또는 색 변화가 생길 수 있습니다."),
        ],
        "beverage": [
            ("목넘김", "액상 베이스가 가볍다면 깔끔한 음용감이 예상되지만 기능성 분말이 많으면 침전 관리가 필요합니다."),
            ("향 유지", "살균 후 휘발향이 떨어질 수 있어 향료 후첨 시점을 따로 잡는 편이 안전합니다."),
            ("안정성", "여과와 균질화가 약하면 상분리 또는 침전이 보일 수 있습니다."),
        ],
        "health_snack": [
            ("식감", "결착이 충분하면 바삭함과 씹힘이 같이 나오지만 수분이 남으면 쉽게 눅눅해질 수 있습니다."),
            ("풍미", "곡물·단백질 베이스가 강하면 건강한 인상은 분명하지만 단맛과 잔향 보정이 필요할 수 있습니다."),
            ("포장성", "개별포장 전 냉각이 부족하면 파손과 점착이 늘어날 수 있습니다."),
        ],
        "senior_care": [
            ("물성", "목표 점도 범위를 맞추면 섭취 편의성은 좋아지지만 가열 후 점도 상승폭을 재확인해야 합니다."),
            ("풍미", "부드러운 맛 설계가 가능하지만 기능성 원료 비중이 높으면 후미가 남을 수 있습니다."),
            ("안정성", "물성 편차와 분리 현상을 줄이기 위해 균질화 조건을 표준화해야 합니다."),
        ],
        "fermented": [
            ("발효 편차", "숙성 시간과 온도 관리가 맞으면 풍미는 좋아지지만 로트별 산미 편차가 커질 수 있습니다."),
            ("가스 발생", "포장 후 가스와 팽창 가능성을 같이 봐야 합니다."),
            ("안정성", "저장 중 산도 상승과 색 변화를 확인해야 합니다."),
        ],
    }.get(
        product_case,
        [
            ("식감", "배합비와 가열 조건이 맞으면 기본 품질은 확보되지만 주원료 편차에 따라 식감이 흔들릴 수 있습니다."),
            ("풍미", "주원료 풍미는 살아날 가능성이 높지만 살균·건조 이후 향 손실을 확인해야 합니다."),
            ("안정성", "포장 전 수분과 온도 관리가 부족하면 저장 중 품질 편차가 생길 수 있습니다."),
        ],
    )


def build_recipe_execution_snapshot(
    req: ProductRequest,
    spec: ProductSpec | None,
    recipe: RecipeDraft | None,
    ingredients: list[IngredientLine],
) -> dict[str, Any]:
    formula_lines = build_recipe_formula_lines(req, ingredients)
    processes = from_json(spec.process_list if spec else "", []) or PRODUCT_CASES[req.product_case]["process"]
    execution_steps = []
    for order, raw_stage in enumerate(processes, start=1):
        stage = str(raw_stage)
        action, expected_output = stage_action(stage, req, formula_lines)
        risk_level, checks = process_risk(stage, req)
        execution_steps.append(
            {
                "step": order,
                "stage": stage,
                "risk_level": risk_level,
                "input_materials": stage_materials(stage, formula_lines),
                "action": action,
                "expected_output": expected_output,
                "control_points": clean_text_items(["배합비 오차와 투입 순서를 먼저 확인합니다.", *checks])[:4],
            }
        )
    top_formula = ", ".join(f"{row['name']} {row['display_ratio']}" for row in formula_lines[:3]) or "배합 추정 데이터 없음"
    yield_text = f"{recipe.yield_rate * 100:.0f}%" if recipe else "92%"
    predicted_results = [{"title": title, "detail": detail} for title, detail in recipe_prediction_templates(req.product_case)]
    predicted_results.append(
        {
            "title": "수율",
            "detail": f"현재 초안 기준 예상 수율은 {yield_text} 수준으로 보고 충진 손실과 선별 폐기를 같이 반영해야 합니다.",
        }
    )
    return {
        "formula_lines": formula_lines,
        "execution_steps": execution_steps,
        "predicted_results": predicted_results,
        "summary": f"상위 배합 추정은 {top_formula} 기준이며 공정은 {len(execution_steps)}단계로 검토했습니다.",
    }


def score_status(score: float) -> str:
    if score >= 80:
        return "좋음"
    if score >= 65:
        return "주의"
    return "위험"


def build_recipe_risk_basis(
    req: ProductRequest,
    recipe: RecipeDraft | None,
    ingredients: list[IngredientLine],
    findings: list[ScreeningFinding],
    cost: CostCalculation | None,
    manufacturability_score: float,
    cost_score: float,
    claim_feasibility: str,
    process_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    red_findings = [finding for finding in findings if finding.severity == "RED"]
    yellow_findings = [finding for finding in findings if finding.severity == "YELLOW"]
    allergen_lines = [line for line in ingredients if line.allergen_flag]
    high_risk_lines = [line for line in (process_plan or {}).get("process_lines", []) if line.get("risk_level") == "HIGH"]

    manufacturability_evidence = []
    if high_risk_lines:
        stages = ", ".join(str(line["stage"]) for line in high_risk_lines[:3])
        manufacturability_evidence.append(f"고위험 공정 {len(high_risk_lines)}개({stages})가 있어 샘플 검증이 필요합니다.")
    if red_findings:
        manufacturability_evidence.append(f"RED 규제 {len(red_findings)}건이 있어 생산 전 증빙 확인이 필요합니다.")
    elif yellow_findings:
        manufacturability_evidence.append(f"YELLOW 규제 {len(yellow_findings)}건이 있어 라벨과 원료 증빙 확인이 필요합니다.")
    if allergen_lines:
        allergen_names = ", ".join(line.ingredient_name for line in allergen_lines[:3])
        manufacturability_evidence.append(f"알레르겐 또는 교차오염 관리 원료가 포함됩니다: {allergen_names}.")
    if recipe:
        manufacturability_evidence.append(f"초안 수율은 {recipe.yield_rate * 100:.0f}%로 잡혀 있어 재작업 여유를 크게 보긴 어렵습니다.")
    if not manufacturability_evidence:
        manufacturability_evidence.append("현재 공정과 원료 조합은 일반 OEM 라인에서 대응 가능한 범위입니다.")

    cost_evidence = []
    if cost:
        cost_evidence.append(f"모의 단위원가는 {cost.unit_cost:,.0f}원/{cost.serving_unit} 기준입니다.")
        line_items = from_json(cost.body, {}).get("line_items", [])
        top_items = sorted(
            [item for item in line_items if item.get("unit_amount") or item.get("total_amount")],
            key=lambda item: float(item.get("unit_amount") or item.get("total_amount") or 0),
            reverse=True,
        )
        if top_items:
            item = top_items[0]
            amount = float(item.get("unit_amount") or item.get("total_amount") or 0)
            suffix = "/단위" if item.get("unit_amount") else "/총액"
            cost_evidence.append(f"가장 큰 비용 항목은 {item.get('category')} {amount:,.0f}원{suffix}입니다.")
        if high_risk_lines:
            cost_evidence.append(f"고위험 공정 {len(high_risk_lines)}개가 제조비와 샘플비를 키울 수 있습니다.")
        if cost.unit_cost > 1200:
            cost_evidence.append("현재 단가 구간이 높아 MOQ 상향 또는 포장 분리가 필요할 수 있습니다.")
        elif cost.unit_cost > 900:
            cost_evidence.append("원가가 무난하진 않아 포장비와 시험비를 별도 협상하는 편이 좋습니다.")
        else:
            cost_evidence.append("현 시점 모의 원가는 초도 테스트 기준으로는 비교적 방어 가능한 구간입니다.")
    else:
        cost_evidence.append("원가 계산 전이라 제조비와 포장비 민감도 검토가 아직 없습니다.")

    claim_evidence = []
    claims = from_json(req.claim_list, [])
    if claims:
        claim_evidence.append(f"현재 확인 대상 표현은 {', '.join(claims)}입니다.")
    else:
        claim_evidence.append("강조 문구가 명확하지 않아 표시 리스크 판단 범위가 넓습니다.")
    if red_findings:
        claim_evidence.append(f"RED 항목 {len(red_findings)}건 때문에 표현 확정 전 전문가 검토가 필요합니다.")
    elif yellow_findings:
        claim_evidence.append(f"YELLOW 항목 {len(yellow_findings)}건 때문에 성적서와 원료 증빙 확인이 필요합니다.")
    else:
        claim_evidence.append("현재 스크리닝상 직접 차단 사유는 없지만 성적서 확보 전 표현 확정은 보류해야 합니다.")

    return {
        "manufacturability": {
            "status": score_status(manufacturability_score),
            "score": round(manufacturability_score, 1),
            "evidence": clean_text_items(manufacturability_evidence),
        },
        "cost": {
            "status": score_status(cost_score),
            "score": round(cost_score, 1),
            "evidence": clean_text_items(cost_evidence),
        },
        "claim": {
            "status": claim_feasibility,
            "evidence": clean_text_items(claim_evidence),
        },
    }


def build_match_report(matches: list["MatchResult"], factories: dict[int, Factory]) -> list[dict[str, Any]]:
    rows = []
    for index, match in enumerate(matches[:5], start=1):
        factory = factories.get(match.factory_id)
        if not factory:
            continue
        contact_basis = []
        if factory.certification_signal:
            contact_basis.append(factory.certification_signal)
        if factory.mvp_fit:
            contact_basis.append(f"MVP {factory.mvp_fit}")
        if factory.oem_signal or factory.odm_signal:
            contact_basis.append("OEM/ODM 대응")
        rows.append(
            {
                "rank": index,
                "company_name": factory.company_name,
                "score": match.score,
                "reason": match.reason,
                "contact_basis": ", ".join(contact_basis) if contact_basis else "제품군 적합도 추가 확인 필요",
                "confirm_questions": from_json(match.confirm_questions, [])[:3],
            }
        )
    return rows


def compose_vibe_agent_report(
    req: ProductRequest,
    planning_goal: str,
    include_revision_prompt: bool,
    spec: ProductSpec | None,
    recipe: RecipeDraft | None,
    ingredients: list[IngredientLine],
    screening: ScreeningRun | None,
    findings: list[ScreeningFinding],
    matches: list["MatchResult"],
    factories: dict[int, Factory],
    cost: CostCalculation | None,
    recipe_snapshot: dict[str, Any],
) -> dict[str, Any]:
    score = 20
    strengths: list[str] = []
    risks: list[str] = []
    actions: list[str] = []

    if spec:
        score += 15
        strengths.append("제품 컨셉, 공정, 포장 조건이 사양화되어 있습니다.")
    else:
        risks.append("바이브 쿠킹 사양이 아직 없어 공장 검토 언어가 부족합니다.")
        actions.append("전체 실행으로 사양과 레시피 초안을 먼저 생성하세요.")

    if len(ingredients) >= 3:
        score += 15
        strengths.append(f"BOM 초안이 {len(ingredients)}개 원료 역할로 분해되어 있습니다.")
    else:
        risks.append("원재료 역할이 3개 미만이라 견적 비교가 어렵습니다.")
        actions.append("주원료, 감미/향미, 기능성 원료, 포장재를 분리해 입력하세요.")

    if screening and screening.overall_status == "GREEN":
        score += 20
        strengths.append("현재 규제 플래그는 GREEN입니다.")
    elif screening and screening.overall_status == "YELLOW":
        score += 10
        risks.append("YELLOW 규제 플래그가 있어 증빙 확인 후 발주해야 합니다.")
    elif screening and screening.overall_status == "RED":
        risks.append("RED 규제 플래그가 있어 발주안 전송 전 전문가 검토가 필요합니다.")
    else:
        risks.append("규제 스크리닝이 아직 실행되지 않았습니다.")

    if matches:
        best_score = max(match.score for match in matches)
        score += 20 if best_score >= 60 else 12
        strengths.append(f"공장 후보 {len(matches)}개가 있으며 최고 적합도는 {best_score}점입니다.")
    else:
        risks.append("공장 후보가 없어 제품군, 포장, MOQ 조건을 완화해야 합니다.")
        actions.append("공장 DB에서 제품군/포장 키워드를 보강하거나 요청 조건을 단순화하세요.")

    if cost and cost.unit_cost > 0:
        score += 10
        strengths.append(f"참고 원가는 판매단위당 {cost.unit_cost:,.0f}원으로 계산되어 있습니다.")
    else:
        actions.append("원가 재계산으로 목표 판매가와 MOQ 민감도를 확인하세요.")

    red_findings = [finding for finding in findings if finding.severity == "RED"]
    yellow_findings = [finding for finding in findings if finding.severity == "YELLOW"]
    for finding in red_findings[:3]:
        risks.append(f"RED: {finding.message}")
    for finding in yellow_findings[:3]:
        risks.append(f"YELLOW: {finding.message}")

    if req.target_qty < 1000:
        risks.append("목표 수량이 낮아 샘플비와 단가가 크게 올라갈 수 있습니다.")
        actions.append("1,000개/5,000개/10,000개 MOQ 시나리오를 비교하세요.")
    if not from_json(req.claim_list, []):
        actions.append("저당, 고단백, 비건 등 검증할 강조 문구를 명확히 고르세요.")

    concept = from_json(spec.concept if spec else "", {})
    revision_prompt = ""
    if include_revision_prompt:
        revision_prompt = (
            f"{req.raw_prompt} "
            f"기획 목표는 '{planning_goal}'이다. "
            "공장 견적 전 확인이 필요한 원료 증빙, 표시 리스크, MOQ, 샘플비, 대체 원료 질문을 추가해 수정안을 만들어라."
        )

    score = max(0, min(score, 100))
    if red_findings or not matches:
        decision = "hold"
    elif score >= 75:
        decision = "send_brief"
    else:
        decision = "revise"

    top_matches = build_match_report(matches, factories)
    best_match = top_matches[0] if top_matches else None
    finding_lines = [f"{finding.severity}: {finding.message}" for finding in findings[:3]]
    executive_summary = (
        f"상위 {len(top_matches)}개 후보 중 {best_match['company_name']}가 가장 먼저 컨택할 만하며, "
        f"현재 핵심 리스크는 {finding_lines[0] if finding_lines else '원가·MOQ 협상'}입니다."
        if best_match
        else "현재 조건으로는 우선 컨택할 공장 후보가 부족합니다."
    )
    regulatory_summary = (
        "규제 스크리닝이 완료되지 않았습니다."
        if not screening
        else "RED 항목이 있어 증빙 확보 전 발주 보류가 필요합니다."
        if screening.overall_status == "RED"
        else "YELLOW 항목 중심으로 성적서와 원료 증빙을 붙이면 컨택은 가능합니다."
        if screening.overall_status == "YELLOW"
        else "현재 스크리닝상 바로 컨택 가능한 수준입니다."
    )
    bid_rationales = []
    if best_match:
        bid_rationales.append(
            f"1순위 {best_match['company_name']}는 적합도 {best_match['score']}점이며 {best_match['reason']} 사유로 우선 검토됩니다."
        )
    if cost:
        bid_rationales.append(f"현재 공급가 산정의 기준 원가는 {cost.unit_cost:,.0f}원/{cost.serving_unit}입니다.")
    if screening and screening.overall_status != "GREEN":
        bid_rationales.append("컨택 메일에는 규제 증빙 요청 문구를 함께 넣는 편이 안전합니다.")
    high_risk_steps = [
        step["stage"]
        for step in recipe_snapshot.get("execution_steps", [])
        if step.get("risk_level") == "HIGH"
    ]
    if high_risk_steps:
        bid_rationales.append(f"공정상 {', '.join(high_risk_steps[:3])} 단계는 분할발주 또는 전문라인 확인이 필요합니다.")

    return {
        "planning_goal": planning_goal,
        "decision": decision,
        "readiness_score": score,
        "headline": f"{req.product_case_label} 입찰/컨택 AI 리포트",
        "executive_summary": executive_summary,
        "fit_summary": {
            "concept": concept,
            "sales_type": req.sales_type,
            "target_qty": f"{req.target_qty:,}{req.qty_unit}",
            "package_type": req.package_type,
        },
        "top_matches": top_matches,
        "bid_contact_report": {
            "summary": executive_summary,
            "rationales": clean_text_items(bid_rationales),
        },
        "regulatory_report": {
            "status": screening.overall_status if screening else "not_run",
            "summary": regulatory_summary,
            "highlights": clean_text_items(finding_lines or ["규제 이슈가 아직 기록되지 않았습니다."]),
        },
        "recipe_report": {
            "summary": recipe_snapshot.get("summary", "레시피 초안이 아직 없습니다."),
            "formula_lines": recipe_snapshot.get("formula_lines", []),
            "execution_steps": recipe_snapshot.get("execution_steps", []),
            "predicted_results": recipe_snapshot.get("predicted_results", []),
        },
        "strengths": clean_text_items(strengths),
        "risks": clean_text_items(risks),
        "recommended_actions": clean_text_items(actions or ["상위 공장 후보 3곳에 MOQ, 샘플비, 리드타임을 확인하세요."]),
        "revision_prompt": revision_prompt,
    }


def merge_agent_prompt(raw_prompt: str, answers: dict[str, str]) -> str:
    merged = [raw_prompt.strip()]
    labels = {
        "usage_context": "추가 사용 맥락",
        "flavor_profile": "맛 방향",
        "ingredient_constraints": "필수/제외 원료",
        "price_priority": "예산/목표 단가",
        "package_detail": "포장 상세",
        "storage_condition": "보관/유통 조건",
        "claim_focus": "강조 문구",
        "success_metric": "고정 우선순위",
    }
    for key, label in labels.items():
        value = answers.get(key, "").strip()
        if value:
            merged.append(f"{label}: {value}.")
    return " ".join(part for part in merged if part).strip()


def target_qty_text_value(target_qty: int | None, qty_unit: str | None, fallback: str | None) -> str:
    if target_qty and qty_unit:
        return f"{target_qty:,}{qty_unit}"
    return (fallback or "").strip()


def format_money_text(value: float) -> str:
    return f"{int(round(value)):,}원"


def format_quantity_text(value: float) -> str:
    return f"{int(value):,}" if float(value).is_integer() else f"{value:,.1f}"


def format_currency_text(value: float, currency: str = "KRW") -> str:
    if currency == "KRW":
        return format_money_text(value)
    return f"{value:,.0f} {currency}"


def format_doc_timestamp(value: datetime | None = None) -> str:
    stamp = value or datetime.now().astimezone()
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc).astimezone()
    else:
        stamp = stamp.astimezone()
    return stamp.strftime("%Y-%m-%d %H:%M %Z")


def display_text(value: Any, empty: str = "미입력") -> str:
    if value is None:
        return empty
    text = str(value).strip()
    return text or empty


def join_display(values: list[Any], empty: str = "미입력") -> str:
    items = clean_text_items([str(value) for value in values if str(value).strip()])
    return ", ".join(items) if items else empty


def public_request_background(req: ProductRequest, concept: dict[str, Any] | None = None) -> str:
    concept = concept if isinstance(concept, dict) else {}
    target_customer = display_text(concept.get("target_customer"), f"{req.sales_type} 판매 대상")
    usage_context = display_text(concept.get("eating_scene"), "초도 개발 및 발주 검토")
    selling_point = display_text(
        concept.get("selling_point"),
        join_display(from_json(req.claim_list, []), f"{req.product_case_label} 제조 가능성 검토"),
    )
    return f"{target_customer} 기준의 {req.product_case_label} 제품으로 {usage_context}을 목표로 검토합니다. 핵심 포인트는 {selling_point}입니다."


def severity_rank(value: str) -> int:
    return {"RED": 0, "YELLOW": 1, "GREEN": 2}.get((value or "").upper(), 9)


def brief_status_label(status: str) -> str:
    mapping = {
        "ready_to_send": "발송 가능",
        "needs_review": "전문가 검토 필요",
        "draft": "작성 중",
    }
    return mapping.get(status, display_text(status))


def order_status_label(status: str) -> str:
    mapping = {
        "ready_to_send": "발송 가능",
        "needs_review": "검토 필요",
        "draft": "작성 중",
    }
    return mapping.get(status, display_text(status))


def kv_row(label: str, value: Any) -> dict[str, str]:
    return {"label": label, "value": display_text(value)}


def make_document(
    title: str,
    doc_kind: str,
    header_rows: list[dict[str, str]],
    notice: str,
    summary: str,
    sections: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "title": title,
        "doc_kind": doc_kind,
        "generated_at": format_doc_timestamp(),
        "draft_status": "검토용 초안",
        "header_rows": header_rows,
        "document_notice": notice,
        "summary": summary,
        "sections": sections,
    }


def section_paragraph(heading: str, body: str, note: str = "") -> dict[str, Any]:
    return {"heading": heading, "type": "paragraph", "body": display_text(body), "note": note}


def section_kv(heading: str, rows: list[dict[str, str]], note: str = "") -> dict[str, Any]:
    return {"heading": heading, "type": "kv", "rows": rows, "note": note}


def section_table(
    heading: str,
    columns: list[str],
    rows: list[list[Any]],
    widths: list[float] | None = None,
    note: str = "",
) -> dict[str, Any]:
    return {"heading": heading, "type": "table", "columns": columns, "rows": rows, "widths": widths or [], "note": note}


def section_list(heading: str, items: list[str], note: str = "") -> dict[str, Any]:
    return {"heading": heading, "type": "list", "items": clean_text_items(items), "note": note}


def section_grouped_list(heading: str, groups: list[dict[str, Any]], note: str = "") -> dict[str, Any]:
    normalized = []
    for group in groups:
        items = clean_text_items([str(item) for item in group.get("items", []) if str(item).strip()])
        normalized.append({"title": display_text(group.get("title"), "항목"), "items": items})
    return {"heading": heading, "type": "grouped_list", "groups": normalized, "note": note}


def doc_paragraph_markup(value: Any) -> str:
    return escape(display_text(value)).replace("\n", "<br/>")

def package_type_from_inputs(case_key: str, raw_prompt: str, package_type: str | None, answers: dict[str, str]) -> str:
    answer_hint = answers.get("package_detail", "")
    for candidate in PRODUCT_CASES[case_key]["packages"]:
        if candidate in answer_hint:
            return candidate
    return guess_package(case_key, f"{raw_prompt} {answer_hint}".strip(), package_type)


def is_explicit_text(value: str | None) -> bool:
    return bool(str(value or "").strip())


def build_agent_preview_heuristic(payload: ProductRequestAgentPreview) -> dict[str, Any]:
    answers = normalize_answer_map(payload.answers)
    enriched_prompt = merge_agent_prompt(payload.raw_prompt, answers)
    case_key = resolve_public_product_case(enriched_prompt, payload.product_case)

    def field_row(
        key: str,
        label: str,
        value: str,
        source: str,
        complete: bool,
        note: str = "",
    ) -> dict[str, Any]:
        return {
            "key": key,
            "label": label,
            "value": value,
            "source": source,
            "status": "filled" if complete else "needs_input",
            "note": note,
        }

    if not case_key:
        supported_labels = ", ".join(PRODUCT_CASES[key]["label"] for key in PUBLIC_PRODUCT_CASE_KEYS)
        return {
            "status": "unsupported_case",
            "analysis_engine": "agent_guardrail",
            "completeness_score": 18,
            "split_fields": [
                field_row(
                    "product_case",
                    "제품군",
                    display_text(payload.product_case, "미지원 또는 미확정"),
                    "직접선택" if payload.product_case else "프롬프트 추출",
                    False,
                    f"V1 지원 범위: {supported_labels}",
                )
            ],
            "reasoning_steps": [
                {
                    "title": "입력 해석",
                    "status": "complete",
                    "summary": "입력된 제품 아이디어와 선택 제품군을 MVP 지원 범위 기준으로 점검했습니다.",
                },
                {
                    "title": "지원 범위 점검",
                    "status": "needs_input",
                    "summary": f"V1 MVP는 {supported_labels}만 지원합니다.",
                },
                {
                    "title": "실행 준비",
                    "status": "needs_input",
                    "summary": "지원 제품군으로 다시 선택하거나 아이디어를 조정하면 다음 단계로 진행할 수 있습니다.",
                },
            ],
            "reasoning_cubes": [
                {
                    "title": "지원 범위 확인 중입니다",
                    "content": "입력된 아이디어와 선택 제품군을 현재 공개 MVP 지원 범위와 대조했습니다.",
                    "status": "complete",
                },
                {
                    "title": "지원 제품군을 다시 정해야 합니다",
                    "content": f"현재 공개 프리뷰는 {supported_labels} 범위만 처리할 수 있어 해당 범위 안에서 다시 선택이 필요합니다.",
                    "status": "needs_input",
                },
            ],
            "clarifying_questions": [
                {
                    "key": "product_case",
                    "label": "지원 제품군 선택",
                    "question": f"V1 MVP는 {supported_labels}만 지원합니다. 어떤 제품군으로 진행할까요?",
                    "reason": "현재 입력은 지원 범위를 벗어나거나 제품군이 모호합니다.",
                    "placeholder": "예: 건강간식, 분말스틱, 소스",
                }
            ],
            "request_payload": {
                "raw_prompt": payload.raw_prompt.strip(),
                "answers": answers,
                "product_case": payload.product_case or "",
                "sales_type": payload.sales_type or "D2C",
                "target_qty": payload.target_qty or 0,
                "qty_unit": payload.qty_unit or "",
                "target_qty_text": (payload.target_qty_text or "").strip(),
                "package_type": payload.package_type or "",
                "claim_list": clean_text_items(payload.claim_list),
                "process_mode": payload.process_mode or "",
                "ingredient_keywords": clean_text_items(payload.ingredient_keywords),
                "target_price": (payload.target_price or "").strip(),
                "budget_text": (payload.budget_text or "").strip(),
                "budget_amount": float(payload.budget_amount or 0),
                "llm_model": payload.llm_model if payload.llm_model in DEEPSEEK_MODELS else SAM_DEFAULT_MODEL,
                "use_llm": payload.use_llm,
                "is_dummy": False,
                "run_full": True,
            },
            "attempt_count": payload.attempt_count,
            "max_attempts": 6,
            "finalization_mode": "clarifying",
        }

    sales_type = infer_sales_type(
        " ".join(
            [
                enriched_prompt,
                answers.get("usage_context", ""),
                answers.get("success_metric", ""),
            ]
        ).strip(),
        payload.sales_type,
    )
    qty_seed = (payload.target_qty_text or answers.get("target_qty_text") or "").strip()
    if payload.target_qty or qty_seed:
        target_qty, qty_unit = normalize_qty(payload.target_qty, qty_seed)
        if payload.qty_unit:
            qty_unit = payload.qty_unit
    else:
        target_qty = 0
        qty_unit = payload.qty_unit or "개"
    package_type = package_type_from_inputs(
        case_key,
        enriched_prompt,
        payload.package_type,
        answers,
    )
    claims = normalize_claims(
        enriched_prompt,
        [
            *payload.claim_list,
            *split_hint_items(answers.get("claim_focus")),
        ],
    )
    process_mode = normalize_process_mode(case_key, payload.process_mode, enriched_prompt, claims)
    ingredient_keywords = clean_text_items(
        [
            *payload.ingredient_keywords,
            *split_hint_items(answers.get("ingredient_constraints")),
        ]
    )
    budget_text = (payload.budget_text or answers.get("price_priority") or "").strip()
    budget_amount = parse_budget_amount(
        payload.budget_amount,
        budget_text,
        answers.get("price_priority"),
        enriched_prompt,
    )
    target_price = (
        payload.target_price
        or answers.get("price_priority")
        or ""
    ).strip()

    prompt_text = enriched_prompt.lower()
    meta = PRODUCT_CASES[case_key]
    has_usage = payload.sales_type in SALES_TYPES or is_explicit_text(answers.get("usage_context")) or contains_any(
        prompt_text,
        [
            "공동구매",
            "프랜차이즈",
            "입찰",
            "납품",
            "자사몰",
            "온라인",
            "카페",
            "식자재",
            "스마트스토어",
        ],
    )
    has_flavor = is_explicit_text(answers.get("flavor_profile")) or contains_any(
        prompt_text,
        [
            "맛",
            "향",
            "맵",
            "달콤",
            "고소",
            "새콤",
            "담백",
            "크리미",
            "스모키",
            "후추",
            "허브",
            "갈릭",
        ],
    )
    has_constraints = is_explicit_text(answers.get("ingredient_constraints")) or contains_any(
        prompt_text,
        [
            "원료",
            "필수",
            "제외",
            "빼고",
            "넣고",
            "알레르기",
            "비건",
            "무유당",
            "설탕",
            "물엿",
            "대체당",
        ],
    )
    has_storage = is_explicit_text(answers.get("storage_condition")) or contains_any(
        prompt_text,
        [
            "상온",
            "냉장",
            "냉동",
            "유통",
            "보관",
            "살균",
            "레토르트",
            "소비기한",
        ],
    )
    has_claims = bool(claims)
    has_budget = budget_amount > 0 or is_explicit_text(target_price)
    has_package_detail = is_explicit_text(answers.get("package_detail")) or contains_any(
        prompt_text,
        ["ml", "g", "kg", "톤", "리터", "용량", "소포장", "대용량", "1회분"],
    )

    score = 30
    score += 12 if case_key in PRODUCT_CASES else 0
    score += 10 if target_qty > 0 else 0
    score += 10 if package_type else 0
    score += 10 if has_budget else 0
    score += 8 if has_usage else 0
    score += 8 if has_claims else 0
    score += 6 if has_flavor else 0
    score += 6 if has_constraints else 0
    score += 5 if has_storage else 0
    score += 5 if has_package_detail else 0
    score = max(0, min(score, 100))

    clarifying_questions: list[dict[str, Any]] = []

    def add_question(
        key: str,
        label: str,
        question: str,
        reason: str,
        placeholder: str,
    ) -> None:
        if len(clarifying_questions) >= 5:
            return
        if answers.get(key):
            return
        clarifying_questions.append(
            {
                "key": key,
                "label": label,
                "question": question,
                "reason": reason,
                "placeholder": placeholder,
            }
        )

    if not has_usage:
        add_question(
            "usage_context",
            "판매/사용 맥락",
            "이 제품을 어디에 납품하거나 어떤 채널에서 판매할 예정인가요?",
            "판매 채널과 사용처가 정해져야 MOQ, 단가, 포장 우선순위를 맞출 수 있습니다.",
            "예: 프랜차이즈 매장용 디핑 소스, 자사몰 공동구매 테스트",
        )
    if target_qty <= 0:
        add_question(
            "target_qty_text",
            "목표 수량",
            "초도 생산 수량을 어느 정도로 보고 있나요?",
            "수량이 있어야 MOQ, 견적, 샘플 단가와 맞는 공장을 추릴 수 있습니다.",
            "예: 1,000개, 5,000포, 1톤",
        )
    if not has_flavor:
        add_question(
            "flavor_profile",
            "맛 방향",
            "원하는 맛, 향, 맵기 수준을 알려 주세요.",
            "소스/음료/간식은 맛 방향이 확정되어야 원료와 공정 질문이 구체화됩니다.",
            "예: 중간 매운맛, 마늘 향 강하게, 단맛은 낮게",
        )
    if not has_constraints:
        add_question(
            "ingredient_constraints",
            "필수/제외 원료",
            "반드시 넣어야 하거나 빼야 하는 원료가 있나요?",
            "원료 제약이 빠지면 공장 견적과 규제 검토 범위가 넓어져 재작업이 늘어납니다.",
            "예: 고추장 베이스 필수, 설탕/물엿 제외, 견과 알레르기 회피",
        )
    if not has_budget:
        add_question(
            "price_priority",
            "예산/단가",
            "초도 생산에서 예산 또는 목표 단가를 어느 정도로 보고 있나요?",
            "예산이나 목표 단가가 있어야 샘플비와 생산 수량의 현실성을 같이 판단할 수 있습니다.",
            "예: 샘플비 포함 800만원 이하, 소비자가 5,900원 기준",
        )
    if not has_package_detail:
        add_question(
            "package_detail",
            "포장 상세",
            "포장 타입 외에 용량이나 1회 제공량 기준이 있나요?",
            "같은 파우치/병이어도 용량이 다르면 공정, 충진, 원가 가정이 달라집니다.",
            "예: 200g 파우치, 30ml 1회용 컵, 500ml 병",
        )
    if case_key in {"sauce", "beverage", "hmr_mealkit", "meat_seafood", "fermented", "senior_care"} and not has_storage:
        add_question(
            "storage_condition",
            "보관 조건",
            "상온, 냉장, 냉동 중 원하는 보관/유통 조건이 있나요?",
            "살균 방식과 공장 라인 적합도는 보관 조건에 크게 좌우됩니다.",
            "예: 상온 6개월, 냉장 30일, 냉동 유통 가능",
        )
    if not has_claims:
        add_question(
            "claim_focus",
            "강조 문구",
            "표시하거나 우선 검토할 강조 문구가 있나요?",
            "강조 문구가 정해져야 저당, 고단백, 비건 등 검토 포인트를 먼저 잡을 수 있습니다.",
            "예: 저당, 나트륨 감소, 고단백",
        )
    if clarifying_questions and len(clarifying_questions) < 2:
        add_question(
            "success_metric",
            "고정 우선순위",
            "초도 개발에서 절대 포기 못하는 1순위는 무엇인가요?",
            "우선순위를 알아야 원가, 맛, 규제, 리드타임 중 어디를 고정할지 정할 수 있습니다.",
            "예: 맛 유지가 1순위, 원가보다 상온 유통이 중요",
        )

    missing_labels = [item["label"] for item in clarifying_questions]
    status = "ready" if not clarifying_questions else "needs_more_input"
    forced_estimated = payload.attempt_count >= 6 and status != "ready"
    finalization_mode = "confirmed" if status == "ready" else "clarifying"
    if forced_estimated:
        status = "ready"
        finalization_mode = "estimated"
        clarifying_questions = []

    split_fields = [
        field_row(
            "product_case",
            "제품군",
            meta["label"],
            "직접선택" if payload.product_case in PUBLIC_PRODUCT_CASE_SET else "프롬프트 추출",
            True,
            ", ".join(meta["packages"][:3]),
        ),
        field_row(
            "sales_type",
            "판매 방식",
            sales_type,
            "직접선택" if payload.sales_type in SALES_TYPES else "프롬프트 추출" if has_usage else "기본값",
            has_usage,
            "MOQ/포장 우선순위 판단",
        ),
        field_row(
            "target_qty",
            "목표 수량",
            f"{target_qty:,}{qty_unit}",
            "직접입력" if payload.target_qty or payload.target_qty_text else "프롬프트 추출",
            target_qty > 0,
            "초도 생산 기준",
        ),
        field_row(
            "package_type",
            "포장 타입",
            package_type,
            "직접입력" if payload.package_type else "프롬프트 추출",
            bool(package_type),
            answers.get("package_detail", ""),
        ),
        field_row(
            "process_mode",
            "제조 방향",
            VIBE_PROCESS_MODES.get(process_mode, process_mode),
            "직접입력" if payload.process_mode in VIBE_PROCESS_MODES else "규칙 보정",
            bool(process_mode),
            "내부 표준 태그 생성 기준",
        ),
        field_row(
            "claim_list",
            "강조 문구",
            ", ".join(claims) if claims else "미입력",
            "직접입력" if payload.claim_list else "질문 반영" if answers.get("claim_focus") else "프롬프트 추출",
            has_claims,
            "표시/규제 우선 검토",
        ),
        field_row(
            "target_price",
            "목표 조건",
            target_price or "미입력",
            "직접입력" if payload.target_price else "질문 반영" if answers.get("price_priority") else "미입력",
            bool(target_price),
            "소비자가/공급가/샘플비 기준",
        ),
        field_row(
            "budget_amount",
            "예산",
            format_money_text(budget_amount) if budget_amount > 0 else "미입력",
            "직접입력" if payload.budget_amount or payload.budget_text else "프롬프트 추출" if budget_amount > 0 else "미입력",
            budget_amount > 0,
            "샘플비 포함 여부 확인",
        ),
    ]

    reasoning_steps = [
        {
            "title": "입력 해석",
            "status": "complete",
            "summary": f"'{meta['label']}' 제품군과 '{sales_type}' 판매 흐름으로 해석했습니다.",
        },
        {
            "title": "항목 분할",
            "status": "complete",
            "summary": f"수량 {target_qty:,}{qty_unit}, 포장 {package_type}, 강조 문구 {', '.join(claims) if claims else '미입력'}로 정리했습니다.",
        },
        {
            "title": "누락 점검",
            "status": "complete" if not clarifying_questions else "needs_input",
            "summary": "추가 확인 불필요" if not clarifying_questions else f"보완이 필요한 항목: {', '.join(missing_labels)}",
        },
        {
            "title": "실행 준비",
            "status": "ready" if status == "ready" else "needs_input",
            "summary": (
                "질문 한도에 도달해 현재 정보 기준의 추정 초안으로 마감합니다."
                if forced_estimated
                else "바로 규격화 + 입찰 실행 가능"
                if status == "ready"
                else f"질문 {len(clarifying_questions)}개에 답하면 최종 요청으로 이어집니다."
            ),
        },
    ]
    reasoning_cubes = [
        {
            "title": "제품 방향을 해석하는 중입니다",
            "content": f"'{meta['label']}' 제품군과 '{sales_type}' 판매 흐름을 우선 후보로 정리했습니다.",
            "status": "complete",
        },
        {
            "title": "핵심 조건을 묶는 중입니다",
            "content": f"수량 {target_qty:,}{qty_unit}, 포장 {package_type}, 강조 문구 {', '.join(claims) if claims else '미입력'}를 기준 입력으로 사용합니다.",
            "status": "complete",
        },
        {
            "title": "정보 공백을 점검하는 중입니다",
            "content": (
                "여섯 차례 보완 이후에도 일부 정보가 비어 있어 합리적 가정으로 초안을 마감합니다."
                if forced_estimated
                else "추가 질문 없이 진행 가능합니다."
                if status == "ready"
                else f"현재 부족한 항목은 {', '.join(missing_labels)}입니다."
            ),
            "status": "estimated" if forced_estimated else "complete" if status == "ready" else "needs_input",
        },
    ]
    if forced_estimated:
        score = max(score, 72)

    request_payload = {
        "raw_prompt": payload.raw_prompt.strip(),
        "answers": answers,
        "product_case": case_key,
        "sales_type": sales_type,
        "target_qty": target_qty,
        "qty_unit": qty_unit,
        "target_qty_text": target_qty_text_value(target_qty, qty_unit, payload.target_qty_text),
        "package_type": package_type,
        "claim_list": claims,
        "process_mode": process_mode,
        "ingredient_keywords": ingredient_keywords,
        "target_price": target_price,
        "budget_text": budget_text,
        "budget_amount": budget_amount,
        "llm_model": payload.llm_model if payload.llm_model in DEEPSEEK_MODELS else SAM_DEFAULT_MODEL,
        "use_llm": payload.use_llm,
        "is_dummy": False,
        "run_full": True,
    }

    return {
        "status": status,
        "analysis_engine": "heuristic_agent",
        "completeness_score": score,
        "split_fields": split_fields,
        "reasoning_steps": reasoning_steps,
        "reasoning_cubes": reasoning_cubes,
        "clarifying_questions": clarifying_questions,
        "request_payload": request_payload,
        "attempt_count": payload.attempt_count,
        "max_attempts": 6,
        "finalization_mode": finalization_mode,
    }


def update_preview_field(
    preview: dict[str, Any],
    key: str,
    *,
    value: str | None = None,
    source: str | None = None,
    note: str | None = None,
    status: str | None = None,
) -> None:
    for row in preview.get("split_fields", []):
        if row.get("key") != key:
            continue
        if value is not None:
            row["value"] = value
        if source is not None:
            row["source"] = source
        if note is not None:
            row["note"] = note
        if status is not None:
            row["status"] = status
        return


def build_reasoning_steps_from_cubes(cubes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "title": display_text(cube.get("title"), "추론 단계"),
            "status": display_text(cube.get("status"), "complete"),
            "summary": display_text(cube.get("content"), ""),
        }
        for cube in cubes
        if display_text(cube.get("title"), "") and display_text(cube.get("content"), "")
    ]


def normalize_preview_reasoning_cubes(raw: Any, fallback: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cubes: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        title = display_text(item.get("title") or item.get("추론제목"), "")
        content = display_text(item.get("content") or item.get("summary") or item.get("추론내용"), "")
        if not title or not content:
            continue
        cubes.append(
            {
                "title": title,
                "content": content,
                "status": display_text(item.get("status"), "complete"),
            }
        )
    return cubes[:5] if cubes else fallback


def normalize_preview_questions(raw: Any) -> list[dict[str, Any]]:
    allowed_keys = {
        "usage_context",
        "target_qty_text",
        "flavor_profile",
        "ingredient_constraints",
        "price_priority",
        "package_detail",
        "storage_condition",
        "claim_focus",
        "success_metric",
    }
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        key = display_text(item.get("key"), "")
        if not key or key not in allowed_keys or key in seen:
            continue
        question = display_text(item.get("question"), "")
        if not question:
            continue
        rows.append(
            {
                "key": key,
                "label": display_text(item.get("label"), key),
                "question": question,
                "reason": display_text(item.get("reason"), "추가 정보가 있으면 초안 정확도를 높일 수 있습니다."),
                "placeholder": display_text(item.get("placeholder"), "예시를 포함해 적어 주세요."),
            }
        )
        seen.add(key)
    return rows[:5]


def call_sam_agent_preview_feedback(
    payload: ProductRequestAgentPreview,
    base_preview: dict[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    if not payload.use_llm:
        return None, "LLM preview disabled"
    if not SAM_API_KEY:
        return None, "SAM_API_KEY 없음"

    model = payload.llm_model if payload.llm_model in DEEPSEEK_MODELS else SAM_DEFAULT_MODEL
    schema_hint = {
        "suggested_sales_type": "string",
        "suggested_process_mode": "string",
        "status": "ready|needs_more_input",
        "finalization_mode": "confirmed|clarifying|estimated",
        "completeness_score": 0,
        "reasoning_cubes": [
            {
                "title": "xx하는 중입니다",
                "content": "현재 판단 근거를 1~2문장으로 설명합니다.",
                "status": "complete|needs_input|estimated",
            }
        ],
        "clarifying_questions": [
            {
                "key": "usage_context",
                "label": "판매/사용 맥락",
                "question": "어디에 납품하거나 어떤 채널에서 팔 예정인가요?",
                "reason": "MOQ와 포장 우선순위를 잡기 위해 필요합니다.",
                "placeholder": "예: 프랜차이즈 매장용, 자사몰 테스트",
            }
        ],
    }
    prompt = f"""
한국 식품 OEM/ODM 기획 에이전트로서 현재 요청 프리뷰를 보완해라.

원문 요청:
{payload.raw_prompt.strip()}

사용자 추가 답변:
{json.dumps(normalize_answer_map(payload.answers), ensure_ascii=False)}

현재 구조화 초안:
{json.dumps(base_preview.get("request_payload", {}), ensure_ascii=False)}

현재 분할 입력:
{json.dumps(base_preview.get("split_fields", []), ensure_ascii=False)}

기본 질문 후보:
{json.dumps(base_preview.get("clarifying_questions", []), ensure_ascii=False)}

현재 시도 횟수: {payload.attempt_count}/6

규칙:
- 판매 방식이 비어 있거나 사용자가 모르면 적절한 채널을 하나 제안할 수 있다.
- 제조 방향은 사용자가 직접 선택하지 않았더라도 제품 특성, 포장, 보관 조건을 보고 제안할 수 있다.
- reasoning_cubes 는 3~4개로 작성하고 각 content 는 짧고 구체적으로 쓴다.
- 정보가 부실하면 clarifying_questions 를 2~4개 남긴다.
- 시도 횟수가 6 이상이면 더 이상 질문하지 말고 status=ready, finalization_mode=estimated, clarifying_questions=[] 로 반환한다.
- 출력은 설명 없이 JSON 객체만 반환한다.
- JSON 구조 힌트: {json.dumps(schema_hint, ensure_ascii=False)}
"""
    request_body = {
        "model": model,
        "stream": False,
        "temperature": 0.2,
        "max_tokens": 1600,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": "너는 한국 식품 OEM/ODM 요청서를 기획하는 에이전트다. 사용자의 빈 정보를 최소 질문으로 보완하고, 추론 흐름을 짧은 텍스트 큐브로 설명한다.",
            },
            {"role": "user", "content": prompt},
        ],
    }
    try:
        response = requests.post(
            f"{SAM_BASE_URL.rstrip('/')}/openai/v1/chat/completions",
            headers=sam_headers(),
            data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
            timeout=45,
        )
        response.raise_for_status()
        data = response.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        parsed = json.loads(content) if isinstance(content, str) else content
        if not isinstance(parsed, dict):
            return None, "SAM 응답 JSON 객체 아님"
        return parsed, f"SAM {model} 프리뷰 추론 성공"
    except Exception as exc:
        return None, f"SAM 호출 실패: {type(exc).__name__}: {exc}"


def merge_agent_preview_feedback(
    base_preview: dict[str, Any],
    feedback: dict[str, Any],
    payload: ProductRequestAgentPreview,
) -> dict[str, Any]:
    preview = {
        **base_preview,
        "analysis_engine": "llm_agent",
        "request_payload": dict(base_preview.get("request_payload", {})),
        "split_fields": [dict(row) for row in base_preview.get("split_fields", [])],
    }
    request_payload = preview["request_payload"]

    suggested_sales_type = display_text(feedback.get("suggested_sales_type"), "")
    if suggested_sales_type in SALES_TYPES:
        request_payload["sales_type"] = suggested_sales_type
        update_preview_field(
            preview,
            "sales_type",
            value=suggested_sales_type,
            source="AI 제안",
            note="사용자가 판매 방식을 비워 두어 에이전트가 우선 채널을 제안했습니다.",
            status="filled",
        )

    suggested_process_mode = display_text(feedback.get("suggested_process_mode"), "")
    if suggested_process_mode in VIBE_PROCESS_MODES:
        request_payload["process_mode"] = suggested_process_mode
        update_preview_field(
            preview,
            "process_mode",
            value=VIBE_PROCESS_MODES.get(suggested_process_mode, suggested_process_mode),
            source="AI 추론",
            note="사용자 직접 선택 없이 제품 특성 기반으로 제조 방향을 제안했습니다.",
            status="filled",
        )

    reasoning_cubes = normalize_preview_reasoning_cubes(
        feedback.get("reasoning_cubes"),
        base_preview.get("reasoning_cubes", []),
    )
    questions = normalize_preview_questions(feedback.get("clarifying_questions"))
    if payload.attempt_count < 6 and questions:
        fallback_questions = normalize_preview_questions(base_preview.get("clarifying_questions", []))
        seen = {row["key"] for row in questions}
        for row in fallback_questions:
            if len(questions) >= 5:
                break
            if row["key"] in seen:
                continue
            questions.append(row)
            seen.add(row["key"])
        if len(questions) < 2:
            questions = fallback_questions[: max(2, len(fallback_questions))]

    finalization_mode = display_text(feedback.get("finalization_mode"), "")
    status = display_text(feedback.get("status"), "")
    forced_estimated = payload.attempt_count >= 6 and (questions or status == "needs_more_input")
    if forced_estimated:
        questions = []
        status = "ready"
        finalization_mode = "estimated"
        reasoning_cubes = [
            *reasoning_cubes[:4],
            {
                "title": "가정 기반 초안을 마감하는 중입니다",
                "content": "여섯 차례 보완 이후에도 정보가 충분하지 않아 현재 입력과 업계 일반값을 기준으로 추정 초안을 완성합니다.",
                "status": "estimated",
            },
        ]

    if questions:
        preview["status"] = "needs_more_input"
        preview["clarifying_questions"] = questions[:5]
        preview["finalization_mode"] = "clarifying"
    else:
        preview["status"] = "ready"
        preview["clarifying_questions"] = []
        preview["finalization_mode"] = finalization_mode if finalization_mode in {"confirmed", "estimated"} else "confirmed"

    score = feedback.get("completeness_score")
    if isinstance(score, (int, float)):
        preview["completeness_score"] = max(
            int(base_preview.get("completeness_score", 0)),
            min(int(round(float(score))), 100),
        )
    if preview["finalization_mode"] == "estimated":
        preview["completeness_score"] = max(int(preview.get("completeness_score", 0)), 72)

    preview["reasoning_cubes"] = reasoning_cubes[:5]
    preview["reasoning_steps"] = build_reasoning_steps_from_cubes(preview["reasoning_cubes"])
    preview["attempt_count"] = payload.attempt_count
    preview["max_attempts"] = 6
    return preview


def build_agent_preview(payload: ProductRequestAgentPreview) -> dict[str, Any]:
    preview = build_agent_preview_heuristic(payload)
    if preview.get("status") == "unsupported_case":
        return preview

    if payload.sales_type not in SALES_TYPES:
        update_preview_field(
            preview,
            "sales_type",
            source="AI 제안",
            note="판매 방식이 미정이어서 에이전트가 우선 채널을 제안했습니다.",
        )
    if not payload.process_mode:
        update_preview_field(
            preview,
            "process_mode",
            source="AI 추론",
            note="사용자 직접 선택 없이 제품 특성과 포장 기준으로 제조 방향을 추론했습니다.",
        )

    feedback, llm_summary = call_sam_agent_preview_feedback(payload, preview)
    if feedback:
        return merge_agent_preview_feedback(preview, feedback, payload)

    preview["analysis_engine"] = "heuristic_agent_fallback" if payload.use_llm else "heuristic_agent"
    if payload.use_llm:
        cubes = [dict(row) for row in preview.get("reasoning_cubes", [])]
        connection_cube = {
            "title": "실시간 에이전트 응답을 확인하는 중입니다",
            "content": f"현재는 외부 LLM 응답을 받지 못해 내부 추정 흐름으로 이어갑니다. ({llm_summary})",
            "status": "complete",
        }
        if preview.get("finalization_mode") == "estimated" and cubes:
            estimated_cube = cubes[-1]
            leading_cubes = cubes[:-1]
            preview["reasoning_cubes"] = [*leading_cubes[:3], connection_cube, estimated_cube]
        else:
            preview["reasoning_cubes"] = [*cubes[:4], connection_cube]
        preview["reasoning_steps"] = build_reasoning_steps_from_cubes(preview["reasoning_cubes"])
    return preview


def register_font() -> str:
    candidates = [
        BASE_DIR / "fonts" / "NanumGothic-Regular.ttf",
        Path("C:/Windows/Fonts/malgun.ttf"),
        Path("C:/Windows/Fonts/NotoSansKR-Regular.ttf"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
    ]
    for font_path in candidates:
        if font_path.exists():
            try:
                pdfmetrics.registerFont(TTFont("Korean", str(font_path)))
                return "Korean"
            except Exception:
                continue
    return "Helvetica"


def seed_database(db: Session) -> None:
    if not db.scalar(select(func.count(User.id))):
        company = Company(name="CBT 운영사", company_type="operator")
        db.add(company)
        db.flush()
        db.add_all(
            [
                User(email="operator@example.com", name="운영자", role="admin", company_id=company.id),
                User(email="brand@example.com", name="브랜드 담당자", role="brand", company_id=company.id),
            ]
        )

    if not db.scalar(select(func.count(Factory.id))) and FACTORY_SEED.exists():
        with FACTORY_SEED.open("r", encoding="utf-8", newline="") as fp:
            for row in csv.DictReader(fp):
                db.add(
                    Factory(
                        factory_code=row["factory_id"],
                        company_name=row["company_name"],
                        primary_category=row["primary_category"],
                        product_keywords=row["product_keywords"],
                        oem_signal=row["oem_signal"] == "Y",
                        odm_signal=row["odm_signal"] == "Y",
                        certification_signal=row["certification_signal"],
                        location_signal=row["location_signal"],
                        mvp_fit=row["mvp_fit"],
                        source_url=row["source_url"],
                        verification_status=row["verification_status"],
                        next_action=row["next_action"],
                        notes=row["notes"],
                    )
                )

    if not db.scalar(select(func.count(RegulatoryRule.id))) and RULE_SEED.exists():
        with RULE_SEED.open("r", encoding="utf-8", newline="") as fp:
            for row in csv.DictReader(fp):
                db.add(RegulatoryRule(**row))

    ensure_verified_extra_factories(db)


def ensure_verified_extra_factories(db: Session) -> None:
    simulated_rows = db.scalars(
        select(Factory).where(
            (Factory.factory_code.like("SIM-%"))
            | (Factory.company_name.like("CBT %"))
            | (Factory.source_url == "internal://simulated-vendor")
        )
    ).all()
    simulated_ids = [row.id for row in simulated_rows]
    if simulated_ids:
        db.query(MatchResult).filter(MatchResult.factory_id.in_(simulated_ids)).delete(synchronize_session=False)
        for row in simulated_rows:
            db.delete(row)

    for company_name, (source_url, verification_status, notes) in VERIFIED_COMPANY_URL_OVERRIDES.items():
        factory = db.scalar(select(Factory).where(Factory.company_name == company_name))
        if factory:
            factory.source_url = source_url
            factory.verification_status = verification_status
            factory.notes = notes

    existing = set(db.scalars(select(Factory.factory_code).where(Factory.factory_code.like("REAL-%"))).all())
    for code, name, category, keywords, cert, location, fit, source_url, notes in VERIFIED_EXTRA_FACTORIES:
        if code in existing:
            continue
        db.add(
            Factory(
                factory_code=code,
                company_name=name,
                primary_category=category,
                product_keywords=keywords,
                oem_signal=True,
                odm_signal=True,
                certification_signal=cert,
                location_signal=location,
                mvp_fit=fit,
                source_url=source_url,
                verification_status="공식사이트확인",
                next_action="공식 사이트 문의 또는 B2B 상담 채널 확인",
                notes=notes,
            )
        )


def ensure_schema_columns() -> None:
    with engine.connect() as conn:
        rows = conn.exec_driver_sql("PRAGMA table_info(product_requests)").fetchall()
        columns = {row[1] for row in rows}
        if rows and "visitor_id" not in columns:
            conn.exec_driver_sql("ALTER TABLE product_requests ADD COLUMN visitor_id VARCHAR(80) DEFAULT ''")
            conn.commit()
        if rows and "llm_model" not in columns:
            conn.exec_driver_sql(f"ALTER TABLE product_requests ADD COLUMN llm_model VARCHAR(80) DEFAULT '{SAM_DEFAULT_MODEL}'")
            conn.commit()
        if rows and "target_price" not in columns:
            conn.exec_driver_sql("ALTER TABLE product_requests ADD COLUMN target_price VARCHAR(80) DEFAULT ''")
            conn.commit()
        if rows and "budget_amount" not in columns:
            conn.exec_driver_sql("ALTER TABLE product_requests ADD COLUMN budget_amount FLOAT DEFAULT 0")
            conn.commit()
        if rows:
            conn.exec_driver_sql(
                """
                UPDATE product_requests
                SET visitor_id = 'legacy-user-' || COALESCE(CAST(user_id AS TEXT), 'anonymous')
                WHERE visitor_id IS NULL OR visitor_id = ''
                """
            )
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_product_requests_visitor_created_at ON product_requests(visitor_id, created_at DESC)"
            )
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_product_requests_visitor_status_created_at ON product_requests(visitor_id, status, created_at DESC)"
            )
            conn.commit()


def record_tool_run(db: Session, request_id: int, tool_name: str, input_data: Any, summary: str, status: str = "succeeded") -> None:
    db.add(
        ToolRun(
            request_id=request_id,
            tool_name=tool_name,
            input_hash=make_hash(input_data),
            version=1,
            status=status,
            summary=summary,
            finished_at=now_utc(),
        )
    )


def sam_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if SAM_API_KEY:
        if SAM_API_KEY.startswith("sam_"):
            headers["X-API-Key"] = SAM_API_KEY
        else:
            headers["Authorization"] = f"Bearer {SAM_API_KEY}"
    return headers


def call_sam_structured(req: ProductRequest, model: str) -> tuple[dict[str, Any] | None, str]:
    if not SAM_API_KEY:
        return None, "SAM_API_KEY 없음: 규칙 기반 폴백"
    if model not in DEEPSEEK_MODELS:
        model = SAM_DEFAULT_MODEL

    schema_hint = {
        "concept": {
            "target_customer": "string",
            "eating_scene": "string",
            "selling_point": "string",
            "draft_warning": "string",
        },
        "process_list": ["string"],
        "ingredients": [
            {
                "role": "string",
                "name": "string",
                "ratio_range": "string",
                "allergen": "string",
                "substitute_allowed": "string",
            }
        ],
        "quality_targets": ["string"],
        "validation_questions": ["string"],
        "cost_assumption": {
            "test_qty": "string",
            "expected_cogs_range": "string",
            "moq_note": "string",
        },
    }
    prompt = f"""
한국 식품 OEM/ODM CBT 앱의 바이브 쿠킹 사양화 결과를 JSON으로 작성해라.
제품군: {req.product_case_label}
사용자 입력: {req.raw_prompt}
판매 방식: {req.sales_type}
목표 수량: {req.target_qty}{req.qty_unit}
포장: {req.package_type}
강조 문구: {', '.join(from_json(req.claim_list, []))}

제약:
- 실제 배합비, 소비기한, 인허가, 효능을 확정하지 말고 검토용 초안으로 둔다.
- 공장 견적에 필요한 BOM 역할, 공정, 검증 질문을 구체화한다.
- 출력은 설명 없이 JSON 객체만 반환한다.
- JSON 구조 예시는 다음 키를 따른다: {json.dumps(schema_hint, ensure_ascii=False)}
"""
    payload = {
        "model": model,
        "stream": False,
        "temperature": 0.2,
        "max_tokens": 1800,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": "너는 한국 식품 OEM/ODM 제조 발주 사양화 전문가다. 법률 판단을 확정하지 않고 확인 질문과 검토용 초안을 만든다.",
            },
            {"role": "user", "content": prompt},
        ],
    }
    try:
        response = requests.post(
            f"{SAM_BASE_URL.rstrip('/')}/openai/v1/chat/completions",
            headers=sam_headers(),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            timeout=45,
        )
        response.raise_for_status()
        data = response.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        parsed = json.loads(content) if isinstance(content, str) else content
        if not isinstance(parsed, dict):
            return None, "SAM 응답 JSON 객체 아님: 규칙 기반 폴백"
        return parsed, f"SAM {model} 사양화 성공"
    except Exception as exc:
        return None, f"SAM 호출 실패: {type(exc).__name__}: {exc}"


def build_spec_and_recipe(db: Session, req: ProductRequest) -> None:
    meta = PRODUCT_CASES[req.product_case]
    claims = from_json(req.claim_list, [])
    llm_data, llm_summary = call_sam_structured(req, req.llm_model)
    target = "예비 창업자/D2C 테스트 고객" if req.sales_type in {"D2C", "B2C", "공동구매"} else "B2B 반복 발주 담당자"
    concept = llm_data.get("concept") if llm_data else None
    if not isinstance(concept, dict):
        concept = {
        "target_customer": target,
        "eating_scene": "온라인 테스트 판매와 샘플 피드백 수집",
        "selling_point": ", ".join(claims) if claims else f"{meta['label']} 제조 가능성 검토",
        "draft_warning": "검토용 초안이며 표시/인허가 확정 판단이 아닙니다.",
        }
    process_list = llm_data.get("process_list") if llm_data else None
    if not isinstance(process_list, list) or not process_list:
        process_list = meta["process"]
    cost = llm_data.get("cost_assumption") if llm_data else None
    if not isinstance(cost, dict):
        cost = {
        "test_qty": req.target_qty,
        "expected_cogs_range": "공장 상담 전 참고: 직접원가 700~1,200원/판매단위",
        "moq_note": "공장별 MOQ, 샘플비, 리드타임 확인 필요",
        }
    questions = llm_data.get("validation_questions") if llm_data else None
    if not isinstance(questions, list) or not questions:
        questions = [*meta["questions"], "MOQ, 샘플비, 초도 생산 리드타임은 어떻게 되는가"]

    db.query(ProductSpec).filter(ProductSpec.request_id == req.id).delete()
    db.add(
        ProductSpec(
            request_id=req.id,
            concept=as_json(concept),
            process_list=as_json(process_list),
            package_condition=as_json({"package_type": req.package_type, "material_check": "식품용 포장재 증빙 필요"}),
            storage_condition="상온 가정, 제품별 수분활성/살균조건 확인",
            cost_assumption=as_json(cost),
            validation_questions=as_json(questions),
        )
    )

    db.query(RecipeDraft).filter(RecipeDraft.request_id == req.id).delete()
    unit = {
        "health_snack": "40g",
        "powder_stick": "20g",
        "sauce": "200g",
        "beverage": "250ml",
        "hmr_mealkit": "350g",
        "bakery_dessert": "60g",
        "kimchi_pickled": "500g",
        "meat_seafood": "200g",
        "fermented": "300g",
        "senior_care": "150g",
        "vegan_alt": "180g",
        "rice_processed": "45g",
    }.get(req.product_case, "100g")
    db.add(
        RecipeDraft(
            request_id=req.id,
            batch_size="테스트 배치 1kg 기준",
            unit_weight=unit,
            yield_rate=0.92,
            quality_targets=as_json(["맛/식감 샘플 3종 비교", "영양성분 분석", "보관 안정성 확인"]),
        )
    )
    db.query(IngredientLine).filter(IngredientLine.request_id == req.id).delete()
    llm_ingredients = llm_data.get("ingredients") if llm_data else None
    if isinstance(llm_ingredients, list) and llm_ingredients:
        ingredient_rows = [
            (
                str(item.get("role", "원료")),
                str(item.get("name", "")),
                str(item.get("substitute_allowed", "가능")),
                str(item.get("allergen", "")),
                str(item.get("ratio_range", "")),
            )
            for item in llm_ingredients
            if isinstance(item, dict)
        ]
    else:
        ingredient_rows = [(role, name, substitute, allergen, f"{idx * 5}~{idx * 8 + 10}%") for idx, (role, name, substitute, allergen) in enumerate(meta["ingredients"], start=1)]
    for role, name, substitute, allergen, ratio in ingredient_rows:
        db.add(
            IngredientLine(
                request_id=req.id,
                ingredient_role=role,
                ingredient_name=name,
                ratio_range=ratio or "공장 확인",
                allergen_flag=allergen,
                substitute_allowed=substitute,
            )
        )
    record_tool_run(db, req.id, "sam_deepseek_vibe_cooking", {"request": req.raw_prompt, "model": req.llm_model}, llm_summary, "succeeded" if llm_data else "skipped")
    record_tool_run(db, req.id, "vibe_cooking_spec", {"request": req.raw_prompt}, f"{meta['label']} 사양과 레시피 초안 생성")


def run_screening(db: Session, req: ProductRequest) -> ScreeningRun:
    db.query(ScreeningFinding).filter(ScreeningFinding.request_id == req.id).delete()
    db.query(ScreeningRun).filter(ScreeningRun.request_id == req.id).delete()
    run = ScreeningRun(request_id=req.id, overall_status="GREEN")
    db.add(run)
    db.flush()

    ingredient_text = " ".join(line.ingredient_name + " " + line.allergen_flag for line in db.scalars(select(IngredientLine).where(IngredientLine.request_id == req.id)))
    context = {
        "claim_list": " ".join(from_json(req.claim_list, [])) + " " + req.raw_prompt,
        "ingredient_list": ingredient_text,
        "product_case": req.product_case_label,
        "package_type": req.package_type,
        "package_material": req.package_type,
        "factory_candidate": "공장",
    }
    severities: list[str] = []
    for rule in db.scalars(select(RegulatoryRule).where(RegulatoryRule.active.is_(True))):
        target = context.get(rule.trigger_field, "")
        if not target:
            continue
        if re.search(rule.trigger_value, target):
            db.add(
                ScreeningFinding(
                    screening_run_id=run.id,
                    request_id=req.id,
                    rule_id=rule.rule_id,
                    severity=rule.severity,
                    message=rule.check_item,
                    required_evidence=rule.required_evidence,
                    source_url=rule.source_url,
                )
            )
            severities.append(rule.severity)
    run.overall_status = "RED" if "RED" in severities else "YELLOW" if "YELLOW" in severities else "GREEN"
    record_tool_run(db, req.id, "regulatory_screening", context, f"규제 스크리닝 {run.overall_status}")
    return run


def score_factory(req: ProductRequest, factory: Factory, findings: list[ScreeningFinding]) -> tuple[float, list[str]]:
    meta = PRODUCT_CASES[req.product_case]
    haystack = f"{factory.primary_category} {factory.product_keywords} {factory.certification_signal} {factory.notes}".lower()
    score = 0.0
    reasons: list[str] = []
    if any(alias.lower() in haystack for alias in meta["aliases"]):
        score += 30
        reasons.append("제품군 키워드 일치")
    if any(proc.lower().replace("/", "") in haystack.replace("/", "") for proc in meta["process"]):
        score += 15
        reasons.append("필요 공정 신호 보유")
    if req.package_type and req.package_type.lower() in haystack:
        score += 12
        reasons.append("포장 방식 신호 일치")
    if factory.mvp_fit == "A":
        score += 15
        reasons.append("초기 검증 적합도 A")
    elif factory.mvp_fit == "B":
        score += 8
    if factory.oem_signal:
        score += 6
    if factory.odm_signal:
        score += 6
    cert = factory.certification_signal.upper()
    if "HACCP" in cert:
        score += 8
        reasons.append("HACCP 신호")
    if "GMP" in cert and req.product_case == "powder_stick":
        score += 8
        reasons.append("GMP 신호")
    if factory.verification_status in {"공개정보확인", "공공DB상세확인", "공식페이지확인", "공식사이트확인"}:
        score += 5
    if req.target_qty <= 5000 and any(word in haystack for word in ["소량", "샘플", "스타트업"]):
        score += 10
        reasons.append("소량/샘플 대응 신호")
    if any(f.severity == "RED" and "알레르기" in f.message for f in findings) and "HACCP" in cert:
        score += 4
    return min(score, 100), reasons


def run_matching(db: Session, req: ProductRequest) -> list[MatchResult]:
    db.query(MatchResult).filter(MatchResult.request_id == req.id).delete()
    findings = list(db.scalars(select(ScreeningFinding).where(ScreeningFinding.request_id == req.id)))
    ranked: list[tuple[float, Factory, list[str]]] = []
    meta = PRODUCT_CASES[req.product_case]
    terms = [*meta["aliases"], req.product_case_label]
    filters = [Factory.active.is_(True), Factory.mvp_fit.in_(["A", "B"])]
    candidates = db.scalars(select(Factory).where(*filters).limit(180)).all()
    for factory in candidates:
        text = f"{factory.primary_category} {factory.product_keywords} {factory.notes}"
        if not any(term in text for term in terms):
            continue
        score, reasons = score_factory(req, factory, findings)
        if score >= 28:
            ranked.append((score, factory, reasons))
    ranked.sort(key=lambda item: item[0], reverse=True)
    results: list[MatchResult] = []
    for score, factory, reasons in ranked[:5]:
        questions = [
            "현재 입력 기준 MOQ와 샘플비를 확인해 주세요.",
            "라벨 검수와 시험성적서 대응 가능 범위를 알려 주세요.",
            *PRODUCT_CASES[req.product_case]["questions"],
        ]
        result = MatchResult(
            request_id=req.id,
            factory_id=factory.id,
            score=round(score, 1),
            reason=f"{factory.company_name}: {', '.join(reasons[:4])}",
            confirm_questions=as_json(list(dict.fromkeys(questions))),
        )
        db.add(result)
        results.append(result)
    record_tool_run(db, req.id, "factory_matcher", {"request_id": req.id}, f"공장 후보 {len(results)}개 생성")
    return results


def build_product_plan_document(
    req: ProductRequest,
    concept: dict[str, Any],
    process_list: list[Any],
    package_condition: dict[str, Any],
    storage_condition: str,
    cost_assumption: dict[str, Any],
    recipe_rows: list[dict[str, Any]],
    screening_rows: list[dict[str, Any]],
    factory_rows: list[dict[str, Any]],
    validation_questions: list[str],
    next_actions: list[str],
) -> dict[str, Any]:
    target_customer = display_text(concept.get("target_customer"))
    selling_point = display_text(concept.get("selling_point"), "제조 가능성 검토")
    package_note = display_text(package_condition.get("material_check"), "식품용 포장재 증빙 필요 여부 확인")
    budget_text = format_money_text(req.budget_amount) if req.budget_amount else "미입력"
    screening_table = [
        [display_text(row.get("severity")), display_text(row.get("message")), display_text(row.get("evidence"))]
        for row in screening_rows
    ] or [["INFO", "현재 자동 스크리닝 결과가 없습니다.", "수동 검토 필요"]]
    factory_table = [
        [
            str(index),
            display_text(row.get("factory")),
            f"{float(row.get('score') or 0):.1f}점",
            display_text(row.get("reason")),
        ]
        for index, row in enumerate(factory_rows, start=1)
    ] or [["-", "추천 후보 없음", "-", "조건 완화 또는 공장 데이터 보강이 필요합니다."]]
    recipe_table = [
        [
            display_text(row.get("role")),
            display_text(row.get("name")),
            display_text(row.get("ratio")),
            display_text(row.get("allergen"), "-"),
        ]
        for row in recipe_rows
    ] or [["미정", "원료 초안 없음", "-", "-"]]
    summary = f"{target_customer} 대상 {req.product_case_label} 제품을 {req.sales_type} 판매 흐름에 맞춰 검토한 내부 기획 초안입니다. 핵심 포인트는 {selling_point}입니다."
    return make_document(
        title=f"{req.product_case_label} 제품 기획안",
        doc_kind="product_plan",
        header_rows=[
            kv_row("요청번호", req.request_uid),
            kv_row("제품 분류", req.product_case_label),
            kv_row("판매 방식", req.sales_type),
            kv_row("문서 상태", "검토용 초안"),
        ],
        notice="식품 제품 기획 방향과 제조 가능성을 정리한 내부 검토용 문서입니다. 표시·인허가·최종 원가는 공급사와 전문가 확인 후 확정합니다.",
        summary=summary,
        sections=[
            section_paragraph("1. 기획 배경", public_request_background(req, concept)),
            section_kv(
                "2. 제품 콘셉트",
                [
                    kv_row("주요 고객", target_customer),
                    kv_row("사용 장면", concept.get("eating_scene")),
                    kv_row("핵심 판매 포인트", selling_point),
                    kv_row("검토 메모", concept.get("draft_warning")),
                ],
            ),
            section_kv(
                "3. 목표 사양 및 사업 조건",
                [
                    kv_row("목표 수량", f"{req.target_qty:,}{req.qty_unit}"),
                    kv_row("포장 형태", req.package_type),
                    kv_row("포장재 확인", package_note),
                    kv_row("보관/유통 조건", storage_condition),
                    kv_row("예상 제조 공정", join_display(process_list)),
                    kv_row("강조 문구", join_display(from_json(req.claim_list, []))),
                    kv_row("목표 가격/원가", req.target_price),
                    kv_row("보유 예산", budget_text),
                    kv_row("원가 가정", cost_assumption.get("expected_cogs_range")),
                    kv_row("MOQ 메모", cost_assumption.get("moq_note")),
                ],
            ),
            section_table(
                "4. 원료 및 배합 방향",
                ["구분", "원료 방향", "배합 가이드", "알레르기/주의"],
                recipe_table,
                widths=[0.15, 0.35, 0.22, 0.28],
            ),
            section_table(
                "5. 규제·품질 확인 포인트",
                ["등급", "확인 항목", "필요 증빙"],
                screening_table,
                widths=[0.12, 0.46, 0.42],
            ),
            section_table(
                "6. 추천 공장 요약",
                ["우선순위", "업체명", "적합도", "검토 의견"],
                factory_table,
                widths=[0.12, 0.24, 0.14, 0.50],
            ),
            section_list(
                "7. 추가 검증 질문",
                validation_questions or ["공장별 MOQ, 샘플비, 리드타임 확인"],
            ),
            section_list(
                "8. 다음 액션",
                next_actions or ["상위 후보 3곳에 샘플 가능 여부와 포장재 증빙 범위를 확인하세요."],
            ),
        ],
    )


def build_sample_brief_document(
    req: ProductRequest,
    concept: dict[str, Any],
    package_condition: dict[str, Any],
    storage_condition: str,
    manufacturing_spec: list[Any],
    bom_rows: list[dict[str, Any]],
    regulatory_rows: list[dict[str, Any]],
    factory_question_groups: list[dict[str, Any]],
    reply_fields: list[str],
    validation_questions: list[str],
    brief_status: str,
    disclaimer: str,
) -> dict[str, Any]:
    package_note = display_text(package_condition.get("material_check"), "식품용 포장재 증빙 필요 여부 확인")
    budget_text = format_money_text(req.budget_amount) if req.budget_amount else "미입력"
    bom_table = [
        [
            display_text(row.get("role")),
            display_text(row.get("name")),
            display_text(row.get("ratio")),
            display_text(row.get("allergen"), "-"),
        ]
        for row in bom_rows
    ] or [["미정", "원료 초안 없음", "-", "-"]]
    regulatory_table = [
        [display_text(row.get("severity")), display_text(row.get("message")), display_text(row.get("evidence"))]
        for row in regulatory_rows
    ] or [["INFO", "현재 자동 스크리닝 결과가 없습니다.", "수동 검토 필요"]]
    factory_groups = factory_question_groups or [{"title": "공장 후보 없음", "items": ["현재 조건으로 자동 생성된 공장 질문이 없습니다. 후보 재검색이 필요합니다."]}]
    reply_table = [[field, "공급사 기입"] for field in (reply_fields or ["가능/불가", "MOQ", "샘플비", "예상 리드타임", "필요 자료"])]
    summary = f"{req.product_case_label} 제품의 샘플 개발 가능 여부와 견적 조건을 확인하기 위한 공급사 전달용 초안입니다. 회신 전 상태는 {brief_status_label(brief_status)}입니다."
    return make_document(
        title=f"{req.product_case_label} 샘플 개발 발주안",
        doc_kind="sample_brief",
        header_rows=[
            kv_row("요청번호", req.request_uid),
            kv_row("제품 분류", req.product_case_label),
            kv_row("목표 수량", f"{req.target_qty:,}{req.qty_unit}"),
            kv_row("발송 상태", brief_status_label(brief_status)),
        ],
        notice=display_text(disclaimer, "견적 및 샘플 가능 여부 확인용 검토 초안입니다. 정식 발주서나 계약서가 아닙니다."),
        summary=summary,
        sections=[
            section_paragraph("1. 개발 요청 배경", public_request_background(req, concept)),
            section_kv(
                "2. 요청 개요",
                [
                    kv_row("판매 방식", req.sales_type),
                    kv_row("포장 형태", req.package_type),
                    kv_row("보관/유통 조건", storage_condition),
                    kv_row("강조 문구", join_display(from_json(req.claim_list, []))),
                    kv_row("핵심 판매 포인트", concept.get("selling_point")),
                    kv_row("목표 가격/원가", req.target_price),
                    kv_row("보유 예산", budget_text),
                ],
            ),
            section_kv(
                "3. 제조·포장 요구 사양",
                [
                    kv_row("예상 제조 공정", join_display(manufacturing_spec)),
                    kv_row("포장재 확인", package_note),
                    kv_row("희망 회신 범위", "가능 여부, MOQ, 샘플비, 리드타임, 필요 증빙"),
                    kv_row("내부 우선순위", "샘플 발주 가능 여부와 표시·증빙 리스크 동시 확인"),
                ],
            ),
            section_table(
                "4. BOM 초안",
                ["구분", "원료/소재", "배합 가이드", "알레르기/주의"],
                bom_table,
                widths=[0.15, 0.35, 0.22, 0.28],
            ),
            section_table(
                "5. 규제 및 증빙 확인 항목",
                ["등급", "확인 질문", "필요 증빙"],
                regulatory_table,
                widths=[0.12, 0.46, 0.42],
            ),
            section_grouped_list(
                "6. 공장별 확인 질문",
                factory_groups,
            ),
            section_table(
                "7. 공급사 회신 양식",
                ["회신 항목", "공급사 작성 메모"],
                reply_table,
                widths=[0.28, 0.72],
                note="공급사는 각 항목별 가능 여부와 조건을 간단히 기입해 회신합니다.",
            ),
            section_list(
                "8. 내부 재확인 질문",
                validation_questions or ["상위 후보 공장과 MOQ, 샘플비, 라벨 검수 범위를 다시 확인하세요."],
            ),
        ],
    )


def normalize_document_body(doc_type: str, body: dict[str, Any], req: ProductRequest, status: str = "") -> dict[str, Any]:
    if not isinstance(body, dict):
        return {}
    if body.get("sections"):
        return body
    if doc_type == "product_plan":
        product_spec = body.get("product_spec") if isinstance(body.get("product_spec"), dict) else {}
        return build_product_plan_document(
            req=req,
            concept=body.get("concept") if isinstance(body.get("concept"), dict) else {},
            process_list=product_spec.get("process") if isinstance(product_spec.get("process"), list) else [],
            package_condition={"material_check": "식품용 포장재 증빙 필요"},
            storage_condition=display_text(product_spec.get("storage"), ""),
            cost_assumption={},
            recipe_rows=body.get("recipe_direction") if isinstance(body.get("recipe_direction"), list) else [],
            screening_rows=body.get("screening") if isinstance(body.get("screening"), list) else [],
            factory_rows=body.get("factory_summary") if isinstance(body.get("factory_summary"), list) else [],
            validation_questions=[],
            next_actions=body.get("next_actions") if isinstance(body.get("next_actions"), list) else [],
        )
    if doc_type == "sample_brief":
        request_overview = body.get("request_overview") if isinstance(body.get("request_overview"), dict) else {}
        regulatory_rows = []
        for row in body.get("regulatory_questions", []) if isinstance(body.get("regulatory_questions"), list) else []:
            if isinstance(row, dict):
                question_text = display_text(row.get("question"), "")
                message, evidence = question_text, ""
                if " - " in question_text:
                    message, evidence = question_text.split(" - ", 1)
                regulatory_rows.append(
                    {
                        "severity": row.get("severity", ""),
                        "message": message,
                        "evidence": evidence,
                    }
                )
        return build_sample_brief_document(
            req=req,
            concept={"selling_point": request_overview.get("product_case", req.product_case_label)},
            package_condition={"material_check": "식품용 포장재 증빙 필요"},
            storage_condition="보관 조건 확인 필요",
            manufacturing_spec=body.get("manufacturing_spec") if isinstance(body.get("manufacturing_spec"), list) else [],
            bom_rows=body.get("bom_draft") if isinstance(body.get("bom_draft"), list) else [],
            regulatory_rows=regulatory_rows,
            factory_question_groups=[
                {
                    "title": row.get("factory", "공장"),
                    "items": row.get("questions", []),
                }
                for row in body.get("factory_questions", [])
                if isinstance(row, dict)
            ],
            reply_fields=body.get("reply_fields") if isinstance(body.get("reply_fields"), list) else [],
            validation_questions=[],
            brief_status=status,
            disclaimer=display_text(body.get("disclaimer"), ""),
        )
    return body


def build_documents(db: Session, req: ProductRequest, screening: ScreeningRun | None = None) -> None:
    if not screening:
        screening = db.scalar(select(ScreeningRun).where(ScreeningRun.request_id == req.id).order_by(ScreeningRun.id.desc()))
    spec = db.scalar(select(ProductSpec).where(ProductSpec.request_id == req.id))
    findings = sorted(
        list(db.scalars(select(ScreeningFinding).where(ScreeningFinding.request_id == req.id))),
        key=lambda item: (severity_rank(item.severity), item.message),
    )
    matches = list(db.scalars(select(MatchResult).where(MatchResult.request_id == req.id).order_by(MatchResult.score.desc()).limit(5)))
    ingredients = list(db.scalars(select(IngredientLine).where(IngredientLine.request_id == req.id)))
    factories = {f.id: f for f in db.scalars(select(Factory).where(Factory.id.in_([m.factory_id for m in matches])))} if matches else {}
    concept = from_json(spec.concept if spec else "", {})
    process_list = from_json(spec.process_list if spec else "", [])
    package_condition = from_json(spec.package_condition if spec else "", {})
    cost_assumption = from_json(spec.cost_assumption if spec else "", {})
    validation_questions = from_json(spec.validation_questions if spec else "", [])
    recipe_rows = [
        {
            "role": line.ingredient_role,
            "name": line.ingredient_name,
            "ratio": line.ratio_range,
            "allergen": line.allergen_flag,
        }
        for line in ingredients
    ]
    screening_rows = [{"severity": item.severity, "message": item.message, "evidence": item.required_evidence} for item in findings]
    factory_rows = []
    factory_question_groups = []
    for match in matches:
        factory = factories.get(match.factory_id)
        if not factory:
            continue
        reason = match.reason.split(": ", 1)[1] if ": " in match.reason else match.reason
        factory_rows.append({"factory": factory.company_name, "score": match.score, "reason": reason})
        factory_question_groups.append({"title": factory.company_name, "items": from_json(match.confirm_questions, [])})
    next_actions = ["RED/YELLOW 항목 확인", "상위 후보 3곳 MOQ/샘플비 확인", "원가계산 결과로 목표 판매가 재검토"]
    plan_body = build_product_plan_document(
        req=req,
        concept=concept,
        process_list=process_list,
        package_condition=package_condition,
        storage_condition=spec.storage_condition if spec else "",
        cost_assumption=cost_assumption,
        recipe_rows=recipe_rows,
        screening_rows=screening_rows,
        factory_rows=factory_rows,
        validation_questions=validation_questions,
        next_actions=next_actions,
    )

    brief_status = "needs_review" if screening and screening.overall_status == "RED" else "ready_to_send"
    brief_body = build_sample_brief_document(
        req=req,
        concept=concept,
        package_condition=package_condition,
        storage_condition=spec.storage_condition if spec else "",
        manufacturing_spec=process_list,
        bom_rows=recipe_rows,
        regulatory_rows=screening_rows,
        factory_question_groups=factory_question_groups,
        reply_fields=["가능/불가", "수정 제안", "MOQ", "샘플비", "예상 리드타임", "필요 자료"],
        validation_questions=validation_questions,
        brief_status=brief_status,
        disclaimer="견적 및 샘플 가능 여부 확인용 검토 초안입니다. 정식 발주서나 계약서가 아닙니다.",
    )

    db.query(ProductPlan).filter(ProductPlan.request_id == req.id).delete()
    db.query(SampleBrief).filter(SampleBrief.request_id == req.id).delete()
    db.add(ProductPlan(request_id=req.id, status="plan_ready", body=as_json(plan_body)))
    db.add(SampleBrief(request_id=req.id, status=brief_status, body=as_json(brief_body)))
    record_tool_run(db, req.id, "procurement_brief_writer", {"request_id": req.id}, f"기획안/발주안 생성: {brief_status}")


def calculate_cost(db: Session, req: ProductRequest, payload: CostCalculationCreate) -> CostCalculation:
    direct_unit = payload.ingredient_cost + payload.packaging_cost + payload.manufacturing_fee
    incidental_total = payload.sample_fee + payload.test_fee + payload.logistics_fee + payload.platform_fee
    total_cost = direct_unit * req.target_qty + incidental_total
    unit_cost = total_cost / req.target_qty
    supply_price = unit_cost / (1 - payload.margin_target)
    vat_included_total = supply_price * req.target_qty * (1 + payload.vat_rate)
    body = {
        "line_items": [
            {"category": "원재료비", "unit_amount": payload.ingredient_cost},
            {"category": "포장비", "unit_amount": payload.packaging_cost},
            {"category": "제조비", "unit_amount": payload.manufacturing_fee},
            {"category": "샘플비", "total_amount": payload.sample_fee},
            {"category": "시험비", "total_amount": payload.test_fee},
            {"category": "물류비", "total_amount": payload.logistics_fee},
        ],
        "moq_scenarios": [
            {"qty": qty, "unit_cost": round((direct_unit * qty + incidental_total) / qty, 1)}
            for qty in [1000, 5000, 10000]
        ],
        "warning": "공정, 포장, 시험, 물류 항목을 조합한 모의 원가이며 공장 견적 확정값이 아닙니다.",
    }
    calc = CostCalculation(
        request_id=req.id,
        version=1,
        target_qty=req.target_qty,
        serving_unit=payload.serving_unit,
        total_cost=round(total_cost, 1),
        unit_cost=round(unit_cost, 1),
        supply_price=round(supply_price, 1),
        vat_included_total=round(vat_included_total, 1),
        body=as_json(body),
    )
    db.add(calc)
    record_tool_run(db, req.id, "cost_calculator", payload.model_dump(), f"1식당 원가 {calc.unit_cost:,.0f}원")
    return calc


def default_serving_unit(product_case: str) -> str:
    return {
        "powder_stick": "1포",
        "sauce": "1병",
        "beverage": "1병",
        "hmr_mealkit": "1팩",
        "bakery_dessert": "1개",
        "kimchi_pickled": "1팩",
        "meat_seafood": "1팩",
        "fermented": "1병",
        "senior_care": "1팩",
        "vegan_alt": "1팩",
        "rice_processed": "1개",
    }.get(product_case, "1개")


def process_cost_weight(stage: str) -> float:
    text = stage.lower()
    weight = 1.0
    if any(word in text for word in ["살균", "가열", "굽기", "조리", "증숙"]):
        weight += 0.35
    if any(word in text for word in ["충진", "포장", "라벨", "소분"]):
        weight += 0.25
    if any(word in text for word in ["냉동", "냉각", "콜드", "숙성", "발효"]):
        weight += 0.3
    if any(word in text for word in ["선별", "검수", "계량", "전처리"]):
        weight -= 0.15
    return max(weight, 0.45)


def process_risk(stage: str, req: ProductRequest) -> tuple[str, list[str]]:
    text = f"{stage} {req.product_case_label} {req.package_type} {req.raw_prompt}"
    checks: list[str] = []
    level = "LOW"
    if any(word in text for word in ["살균", "가열", "pH", "액상", "소스", "음료"]):
        checks.append("살균 조건, pH, 수분활성 기준 확인")
        level = "MEDIUM"
    if any(word in text for word in ["충진", "포장", "파우치", "병", "스틱", "트레이"]):
        checks.append("식품용 포장재 증빙과 충진 수율 확인")
        level = "MEDIUM" if level == "LOW" else level
    if any(word in text for word in ["냉동", "냉장", "축산", "수산", "HMR", "밀키트"]):
        checks.append("콜드체인, HACCP 범위, 온도 기록 확인")
        level = "HIGH"
    if any(word in text for word in ["발효", "숙성", "김치", "장류"]):
        checks.append("발효 편차, 가스 발생, 숙성 기간 확인")
        level = "HIGH"
    if any(word in text for word in ["고령친화", "케어푸드", "물성", "연하"]):
        checks.append("물성 측정, 입자 크기, 표시 가능 범위 확인")
        level = "HIGH"
    if not checks:
        checks.append("작업 표준서와 샘플 허용오차 확인")
    return level, checks


def process_compliance_focus(stage: str, req: ProductRequest, checks: list[str]) -> tuple[str, list[str]]:
    claim_text = " ".join(str(item) for item in from_json(req.claim_list, []))
    text = f"{stage} {req.product_case_label} {req.package_type} {req.raw_prompt} {' '.join(checks)} {claim_text}".lower()
    if any(word in text for word in ["포장", "충진", "실링", "라벨", "스틱", "파우치", "병", "캔", "트레이", "용기"]):
        return "표시/포장 적합성", ["표시", "라벨", "포장", "포장재", "용기", "기구", "적합"]
    if any(word in text for word in ["살균", "가열", "멸균", "레토르트", "ph", "보존", "소스", "음료"]):
        return "공정/보존 기준", ["살균", "보존", "pH", "수분활성", "식품유형", "기준"]
    if any(word in text for word in ["냉장", "냉동", "콜드", "축산", "수산", "hmr", "밀키트", "샐러드"]):
        return "냉장/냉동 관리", ["HACCP", "온도", "냉장", "냉동", "콜드체인", "기록"]
    if any(word in text for word in ["발효", "숙성", "김치", "장류"]):
        return "발효 편차 관리", ["발효", "숙성", "보존", "식품유형", "기준"]
    if any(word in text for word in ["원료", "계량", "선별", "전처리", "혼합", "배합"]):
        return "원료/알레르기 확인", ["원료", "알레르기", "첨가물", "수입", "규격서"]
    if any(word in text for word in ["고령친화", "케어푸드", "연하", "물성"]):
        return "물성/표시 적합성", ["물성", "표시", "기준", "시험", "분석"]
    return "공장 서류/인증 점검", ["인증", "HACCP", "GMP", "행정처분", "공장", "검증"]


def recommended_certifications_for_stage(stage: str, req: ProductRequest) -> list[str]:
    claim_text = " ".join(str(item) for item in from_json(req.claim_list, []))
    text = f"{stage} {req.product_case} {req.product_case_label} {req.package_type} {req.raw_prompt} {claim_text}".lower()
    certs = ["HACCP"]
    if req.product_case in {"powder_stick", "supplement"} or any(word in text for word in ["건강기능식품", "gmp", "타정", "캡슐", "프로바이오틱스"]):
        certs.append("GMP")
    if any(word in text for word in ["음료", "유제품", "레토르트", "상온", "멸균", "캔"]):
        certs.append("FSSC22000")
    if req.product_case == "vegan_alt" or any(word in text for word in ["비건", "식물성"]):
        certs.append("비건")
    if "할랄" in text:
        certs.append("할랄")
    return list(dict.fromkeys(certs))


def normalize_certifications(value: str) -> list[str]:
    tokens: list[str] = []
    for raw in re.split(r"[,/|·\s]+", value or ""):
        token = raw.strip()
        if not token:
            continue
        upper = token.upper()
        mapped = ""
        if "HACCP" in upper:
            mapped = "HACCP"
        elif "GMP" in upper:
            mapped = "GMP"
        elif "FSSC" in upper:
            mapped = "FSSC22000"
        elif upper.startswith("ISO"):
            mapped = "ISO"
        elif token in {"비건", "할랄"}:
            mapped = token
        if mapped and mapped not in tokens:
            tokens.append(mapped)
    return tokens


def rank_process_findings(stage: str, checks: list[str], findings: list[ScreeningFinding], focus_keywords: list[str]) -> list[ScreeningFinding]:
    stage_tokens = [token for token in re.split(r"[^\w가-힣]+", f"{stage} {' '.join(checks)}") if len(token) >= 2]
    severity_score = {"RED": 3, "YELLOW": 2, "GREEN": 1}
    ranked: list[tuple[int, int, ScreeningFinding]] = []
    for finding in findings:
        text = f"{finding.rule_id} {finding.message} {finding.required_evidence}".lower()
        score = sum(3 for keyword in focus_keywords if keyword.lower() in text)
        score += sum(1 for token in stage_tokens if token.lower() in text)
        if score <= 0:
            continue
        ranked.append((score, severity_score.get(finding.severity, 0), finding))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    selected = [item[2] for item in ranked[:2]]
    if selected:
        return selected
    return sorted(findings, key=lambda item: severity_score.get(item.severity, 0), reverse=True)[:1]


def build_process_compliance_review(
    line: dict[str, Any],
    req: ProductRequest,
    findings: list[ScreeningFinding],
    candidate_factories: list[Factory],
) -> dict[str, Any]:
    checks = [str(item) for item in line.get("risk_checks", []) if str(item).strip()]
    focus_label, focus_keywords = process_compliance_focus(str(line.get("stage", "")), req, checks)
    matched_findings = rank_process_findings(str(line.get("stage", "")), checks, findings, focus_keywords)
    legal_status = "RED" if any(item.severity == "RED" for item in matched_findings) else "YELLOW" if any(item.severity == "YELLOW" for item in matched_findings) else "GREEN"
    required_certifications = recommended_certifications_for_stage(str(line.get("stage", "")), req)
    available_certifications: list[str] = []
    candidate_cover: list[dict[str, Any]] = []
    for factory in candidate_factories[:5]:
        certs = normalize_certifications(factory.certification_signal or "")
        for cert in certs:
            if cert not in available_certifications:
                available_certifications.append(cert)
        covered = [cert for cert in required_certifications if cert in certs]
        if covered:
            candidate_cover.append(
                {
                    "company_name": factory.company_name,
                    "certifications": covered,
                }
            )
    certification_gap = [cert for cert in required_certifications if cert not in available_certifications]
    certification_status = (
        "covered"
        if required_certifications and not certification_gap
        else "partial"
        if any(cert in available_certifications for cert in required_certifications)
        else "gap"
    )
    required_evidence = list(
        dict.fromkeys(
            [item.required_evidence for item in matched_findings if item.required_evidence]
            + checks
        )
    )[:3]
    return {
        "legal_focus": focus_label,
        "legal_status": legal_status,
        "legal_findings": [
            {
                "rule_id": item.rule_id,
                "severity": item.severity,
                "message": item.message,
                "required_evidence": item.required_evidence,
                "source_url": item.source_url,
            }
            for item in matched_findings
        ],
        "required_evidence": required_evidence,
        "required_certifications": required_certifications,
        "available_certifications": available_certifications,
        "certification_status": certification_status,
        "certification_gap": certification_gap,
        "candidate_factories": candidate_cover[:2],
    }


def annotate_process_plan_with_compliance(
    process_plan: dict[str, Any],
    req: ProductRequest,
    findings: list[ScreeningFinding],
    candidate_factories: list[Factory],
    overall_status: str,
) -> dict[str, Any]:
    lines: list[dict[str, Any]] = []
    required_certifications: list[str] = []
    missing_certifications: list[str] = []
    priority_stages: list[str] = []
    for raw_line in process_plan.get("process_lines", []):
        if not isinstance(raw_line, dict):
            continue
        line = dict(raw_line)
        compliance = build_process_compliance_review(line, req, findings, candidate_factories)
        line["compliance_review"] = compliance
        lines.append(line)
        for cert in compliance["required_certifications"]:
            if cert not in required_certifications:
                required_certifications.append(cert)
        for cert in compliance["certification_gap"]:
            if cert not in missing_certifications:
                missing_certifications.append(cert)
        if compliance["legal_status"] == "RED" or compliance["certification_status"] == "gap":
            priority_stages.append(str(line.get("stage", "")))
    return {
        **process_plan,
        "process_lines": lines,
        "compliance_summary": {
            "overall_status": overall_status,
            "required_certifications": required_certifications,
            "missing_certifications": missing_certifications,
            "priority_stages": priority_stages[:3],
            "summary_text": "공정 플로우마다 법률/인증 검토 결과를 묶어 확인합니다.",
        },
    }


def build_process_plan(db: Session, req: ProductRequest) -> dict[str, Any]:
    spec = db.scalar(select(ProductSpec).where(ProductSpec.request_id == req.id))
    cost = db.scalar(select(CostCalculation).where(CostCalculation.request_id == req.id).order_by(CostCalculation.id.desc()))
    processes = from_json(spec.process_list if spec else "", []) or PRODUCT_CASES[req.product_case]["process"]
    weights = [process_cost_weight(str(stage)) for stage in processes]
    weight_sum = sum(weights) or 1
    base_total = float(cost.total_cost if cost else req.target_qty * 1100)
    brokerage_rate = 0.08 if req.sales_type in {"입찰", "샘플입찰"} else 0.06
    process_lines = []
    for stage, weight in zip(processes, weights):
        level, checks = process_risk(str(stage), req)
        amount = round(base_total * (weight / weight_sum), 1)
        process_lines.append(
            {
                "stage": str(stage),
                "order_mode": "분할발주 검토" if level == "HIGH" else "통합발주 가능",
                "risk_level": level,
                "risk_checks": checks,
                "estimated_amount": amount,
                "owner_type": "전문 공정 협력사" if level == "HIGH" else "주 생산 공장",
            }
        )
    brokerage_fee = round(base_total * brokerage_rate, 1)
    projected_total = round(base_total + brokerage_fee, 1)
    budget = float(req.budget_amount or 0)
    if not budget:
        budget_status = "budget_unknown"
        budget_gap = 0.0
    else:
        budget_gap = round(budget - projected_total, 1)
        budget_status = "inside_budget" if budget_gap >= 0 else "near_budget" if projected_total <= budget * 1.1 else "over_budget"
    return {
        "serving_unit": cost.serving_unit if cost else default_serving_unit(req.product_case),
        "unit_estimate": cost.unit_cost if cost else round(base_total / max(req.target_qty, 1), 1),
        "total_estimate": base_total,
        "brokerage_fee_rate": brokerage_rate,
        "brokerage_fee": brokerage_fee,
        "projected_total": projected_total,
        "budget_amount": budget,
        "budget_status": budget_status,
        "budget_gap": budget_gap,
        "process_lines": process_lines,
        "notes": [
            "금액은 원료가 열람값이 아닌 공정/포장/시험/물류 조합 기반 모의 견적입니다.",
            "HIGH 공정은 분할발주 또는 전문 협력사 확인을 우선합니다.",
        ],
    }


def deterministic_variance(*parts: Any) -> float:
    seed = int(make_hash(parts)[:6], 16)
    return ((seed % 17) - 8) / 100


def simulate_vendor_contacts(db: Session, req: ProductRequest, payload: ContactSimulationRun) -> dict[str, Any]:
    matches = list(db.scalars(select(MatchResult).where(MatchResult.request_id == req.id).order_by(MatchResult.score.desc()).limit(5)))
    factories = {f.id: f for f in db.scalars(select(Factory).where(Factory.id.in_([m.factory_id for m in matches])))} if matches else {}
    process_plan = build_process_plan(db, req)
    budget = float(payload.budget_amount or req.budget_amount or 0)
    base_unit = max(float(process_plan["unit_estimate"]), 1.0) * 1.28
    high_risk_count = sum(1 for line in process_plan["process_lines"] if line["risk_level"] == "HIGH")
    bids = []
    for match in matches:
        factory = factories.get(match.factory_id)
        if not factory:
            continue
        variance = deterministic_variance(req.id, factory.id, req.target_qty)
        score_discount = max(-0.08, min(0.04, (72 - match.score) / 500))
        risk_factor = high_risk_count * 0.025
        unit_quote = round(base_unit * (1 + variance + score_discount + risk_factor), 1)
        quote_total = round(unit_quote * req.target_qty, 1)
        brokerage_fee = round(quote_total * process_plan["brokerage_fee_rate"], 1)
        total_with_fee = round(quote_total + brokerage_fee, 1)
        gap = round(budget - total_with_fee, 1) if budget else 0.0
        fit = "예산내" if budget and gap >= 0 else "예산초과" if budget else "예산미입력"
        lead_seed = int(make_hash([req.id, factory.factory_code])[:4], 16)
        moq = 1000 if req.target_qty <= 1000 else 3000 if req.target_qty <= 5000 else 10000
        bid_score = match.score - max(0, -gap / max(budget, 1)) * 25 - high_risk_count * 2 if budget else match.score - high_risk_count * 2
        bids.append(
            {
                "factory": serialize_factory(factory),
                "response_status": "견적 가능" if match.score >= 50 else "조건부 가능",
                "quote_total": quote_total,
                "unit_quote": unit_quote,
                "brokerage_fee": brokerage_fee,
                "total_with_fee": total_with_fee,
                "moq": moq,
                "lead_time_days": 14 + lead_seed % 21,
                "budget_fit": fit,
                "budget_gap": gap,
                "risk_notes": [line["stage"] for line in process_plan["process_lines"] if line["risk_level"] == "HIGH"][:3],
                "confirm_questions": from_json(match.confirm_questions, [])[:4],
                "bid_score": round(bid_score, 1),
            }
        )
    bids.sort(key=lambda item: item["bid_score"], reverse=True)
    best = bids[0] if bids else None
    if best:
        conclusion = {
            "decision": "우선협상" if best["budget_fit"] != "예산초과" else "조건수정",
            "recommended_vendor": best["factory"]["company_name"],
            "estimated_total": best["total_with_fee"],
            "brokerage_fee": best["brokerage_fee"],
            "budget_status": best["budget_fit"],
            "next_step": "상위 2개 업체에 샘플비, MOQ, 리드타임 확정 질문 발송",
            "rationale": f"{best['response_status']} · 입찰점수 {best['bid_score']}점 · 리드타임 {best['lead_time_days']}일",
        }
    else:
        conclusion = {
            "decision": "보류",
            "recommended_vendor": "",
            "estimated_total": 0,
            "brokerage_fee": 0,
            "budget_status": "후보없음",
            "next_step": "제품군 키워드 또는 포장 조건을 완화해 후보를 다시 조회",
            "rationale": "현재 조건으로 응답 가능한 후보가 없습니다.",
        }
    contact_message = "\n".join(
        [
            f"[{payload.negotiation_mode}] {req.product_case_label} 제품화 가능 여부 문의",
            f"제품 개요: {public_request_background(req)}",
            f"희망 수량/포장: {req.target_qty:,}{req.qty_unit} / {req.package_type}",
            f"보유 예산: {budget:,.0f}원" if budget else "보유 예산: 미입력",
            "회신 요청: 가능 여부, MOQ, 샘플비, 리드타임, 필요 증빙",
        ]
    )
    record_tool_run(db, req.id, "contact_bid_simulator", payload.model_dump(), f"모의 컨택 {len(bids)}개 응답, 결론 {conclusion['decision']}")
    return {
        "request_id": req.id,
        "negotiation_mode": payload.negotiation_mode,
        "preferred_contact": payload.preferred_contact,
        "contact_message": contact_message,
        "process_plan": process_plan if payload.include_split_order else None,
        "bids": bids,
        "final_conclusion": conclusion,
    }


def run_full_pipeline(db: Session, req: ProductRequest) -> None:
    build_spec_and_recipe(db, req)
    req.status = "spec_ready"
    req.updated_at = now_utc()
    screening = run_screening(db, req)
    req.status = "screening_ready"
    req.updated_at = now_utc()
    matches = run_matching(db, req)
    req.status = "matched" if matches else "on_hold"
    req.updated_at = now_utc()
    build_documents(db, req, screening)
    calculate_cost(db, req, CostCalculationCreate(serving_unit=default_serving_unit(req.product_case)))
    if not matches:
        req.status = "on_hold"
    elif screening.overall_status == "RED":
        req.status = "needs_review"
    else:
        req.status = "brief_ready"
    req.updated_at = now_utc()


def serialize_request(req: ProductRequest, detail: bool = False, include_private: bool = False) -> dict[str, Any]:
    idea_preview = req.raw_prompt.strip()
    for marker in [
        "추가 사용 맥락:",
        "맛 방향:",
        "필수/제외 원료:",
        "예산/목표 단가:",
        "포장 상세:",
        "보관/유통 조건:",
        "강조 문구:",
        "고정 우선순위:",
    ]:
        marker_index = idea_preview.find(marker)
        if marker_index > 0:
            idea_preview = idea_preview[:marker_index].strip()
    if len(idea_preview) > 96:
        idea_preview = f"{idea_preview[:93].rstrip()}..."

    data = {
        "id": req.id,
        "request_uid": req.request_uid,
        "product_case": req.product_case,
        "product_case_label": req.product_case_label,
        "sales_type": req.sales_type,
        "target_qty": req.target_qty,
        "qty_unit": req.qty_unit,
        "package_type": req.package_type,
        "llm_model": req.llm_model,
        "claim_list": from_json(req.claim_list, []),
        "taste_tags": from_json(req.taste_tags, []),
        "target_price": req.target_price,
        "budget_amount": req.budget_amount,
        "status": req.status,
        "is_dummy": req.is_dummy,
        "idea_preview": idea_preview,
        "created_at": req.created_at.isoformat(),
        "updated_at": req.updated_at.isoformat(),
    }
    if include_private:
        data["raw_prompt"] = req.raw_prompt if detail else req.raw_prompt[:90]
    return data


def serialize_factory(factory: Factory) -> dict[str, Any]:
    return {
        "id": factory.id,
        "factory_code": factory.factory_code,
        "company_name": factory.company_name,
        "primary_category": factory.primary_category,
        "product_keywords": factory.product_keywords,
        "certification_signal": factory.certification_signal,
        "location_signal": factory.location_signal,
        "mvp_fit": factory.mvp_fit,
        "verification_status": factory.verification_status,
        "source_url": factory.source_url,
        "notes": factory.notes,
        "active": factory.active,
    }


def default_purchase_order_lines(db: Session, req: ProductRequest) -> list[dict[str, Any]]:
    cost = db.scalar(select(CostCalculation).where(CostCalculation.request_id == req.id).order_by(CostCalculation.id.desc()))
    unit_price = cost.supply_price if cost else 0
    return [
        {
            "item_name": f"{req.product_case_label} {req.package_type} 생산",
            "specification": public_request_background(req)[:80],
            "unit": req.qty_unit,
            "quantity": req.target_qty,
            "unit_price": unit_price,
            "requested_delivery_date": "",
            "notes": "초도/샘플 발주 기준, 최종 단가는 공급사 견적서로 확정",
        }
    ]


def latest_purchase_order(db: Session, request_id: int) -> PurchaseOrderRequest | None:
    return db.scalar(select(PurchaseOrderRequest).where(PurchaseOrderRequest.request_id == request_id).order_by(PurchaseOrderRequest.created_at.desc()))


def board_post_title(req: ProductRequest) -> str:
    package = f" / {req.package_type}" if req.package_type else ""
    return f"[입찰] {req.product_case_label} {req.target_qty:,}{req.qty_unit}{package} 초도 생산"


def board_vendor_profile(factory: Factory | None, req: ProductRequest, index: int) -> dict[str, str]:
    if factory:
        certs = factory.certification_signal or ""
        if any(token in certs for token in ["GMP", "ISO", "FSSC"]):
            return AI_VENDOR_PROFILES[2]
        if factory.mvp_fit == "A":
            return AI_VENDOR_PROFILES[0]
        if req.sales_type in {"프랜차이즈", "PB", "B2B"}:
            return AI_VENDOR_PROFILES[3]
        return AI_VENDOR_PROFILES[1]
    if req.sales_type in {"프랜차이즈", "PB", "B2B"}:
        return AI_VENDOR_PROFILES[3]
    return AI_VENDOR_PROFILES[index % len(AI_VENDOR_PROFILES)]


def board_vendor_name(req: ProductRequest, factory: Factory | None, index: int) -> str:
    if factory:
        return factory.company_name
    return f"AI 모의기업 {req.product_case_label} 파트너 {index + 1}"


def board_bid_moq(req: ProductRequest, index: int) -> int:
    if req.target_qty <= 1000:
        return 1000 if index < 2 else 3000
    if req.target_qty <= 5000:
        return 3000 if index < 2 else 5000
    return 10000 if index < 2 else max(req.target_qty, 15000)


def latest_request_board_pdf(db: Session, req: ProductRequest) -> GeneratedFile | None:
    return db.scalar(
        select(GeneratedFile)
        .where(
            GeneratedFile.request_id == req.id,
            GeneratedFile.doc_type.in_(("sample_brief", "product_plan")),
            GeneratedFile.created_at >= req.created_at,
        )
        .order_by(GeneratedFile.created_at.desc())
    )


def load_request_board_pdf_or_400(db: Session, req: ProductRequest) -> GeneratedFile:
    generated = latest_request_board_pdf(db, req)
    if not generated:
        api_error(400, "board_pdf_required")
    return generated


def extract_pdf_text(storage_path: str) -> str:
    try:
        reader = PdfReader(storage_path)
    except Exception:
        return ""
    chunks: list[str] = []
    for page in reader.pages:
        try:
            chunks.append(page.extract_text() or "")
        except Exception:
            continue
    return re.sub(r"\s+", " ", " ".join(chunks)).strip()


def board_pdf_excerpt(pdf_text: str, limit: int = 260) -> str:
    text = re.sub(r"\s+", " ", pdf_text).strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}..."


def board_feasibility_status(bid_score: float, budget_fit: str) -> str:
    if bid_score >= 70 and budget_fit != "예산초과":
        return "발주 가능"
    if bid_score >= 55:
        return "조건부 가능"
    return "보완 필요"


BOARD_NEGOTIATION_OPEN_STATUSES = {"발주 가능", "조건부 가능"}


def board_bid_negotiation_ox(response_status: str) -> str:
    return "O" if str(response_status).strip() in BOARD_NEGOTIATION_OPEN_STATUSES else "X"


def board_bid_rejection_ox(response_status: str) -> str:
    return "X" if board_bid_negotiation_ox(response_status) == "O" else "O"


def summarize_board_bid_outcome(bids: list[dict[str, Any]]) -> dict[str, Any]:
    negotiation_count = 0
    rejection_count = 0
    for bid in bids:
        if board_bid_negotiation_ox(str(bid.get("response_status", "")).strip()) == "O":
            negotiation_count += 1
        else:
            rejection_count += 1
    has_proposal = negotiation_count > 0
    if has_proposal:
        proposal_status = "proposal_available"
        proposal_label = "제안 있음"
    elif bids:
        proposal_status = "rejected"
        proposal_label = "입찰 반려"
    else:
        proposal_status = "pending"
        proposal_label = "대기"
    return {
        "proposal_status": proposal_status,
        "proposal_label": proposal_label,
        "has_negotiation_offer": has_proposal,
        "negotiation_count": negotiation_count,
        "rejection_count": rejection_count,
    }


def board_vendor_persona(factory: Factory | None, profile: dict[str, str]) -> str:
    if factory:
        category = factory.primary_category or "식품 OEM/ODM"
        certs = factory.certification_signal or "기본 제조 검토"
        return f"{factory.company_name}의 {category} 영업 담당자. 인증/생산 범위를 근거로 회신하는 톤. 핵심 인증: {certs}"
    return f"{profile['label']} 페르소나로 응답하는 AI 모의기업 담당자. 확정 불가 항목은 보수적으로 안내하는 톤."


def call_sam_board_reply(
    *,
    req: ProductRequest,
    vendor_name: str,
    persona: str,
    pdf_excerpt_text: str,
    budget_fit: str,
    moq: int,
    lead_time_days: int,
    risk_notes: list[str],
    required_documents: list[str],
    fallback_status: str,
) -> dict[str, Any] | None:
    if not SAM_API_KEY:
        return None
    model = req.llm_model if req.llm_model in DEEPSEEK_MODELS else SAM_DEFAULT_MODEL
    schema_hint = {
        "response_status": "발주 가능 | 조건부 가능 | 보완 필요",
        "response_summary": "string",
        "required_documents": ["string"],
        "risk_notes": ["string"],
        "counter_offer": "string",
    }
    prompt = f"""
너는 한국 식품 OEM/ODM 업체의 실제 영업 담당자처럼 답변한다.
업체명: {vendor_name}
페르소나: {persona}
제품군: {req.product_case_label}
판매 방식: {req.sales_type}
희망 수량: {req.target_qty}{req.qty_unit}
포장: {req.package_type}
예산 적합도: {budget_fit}
MOQ 기준: {moq}{req.qty_unit}
예상 납기: {lead_time_days}일
현재 리스크: {", ".join(risk_notes) if risk_notes else "없음"}
기본 요청 서류: {", ".join(required_documents)}

게시 PDF 핵심 발췌:
{pdf_excerpt_text or "PDF 본문 추출 없음"}

제약:
- 과도한 확정 표현은 피하고, 검토 가능한 범위만 업체처럼 회신한다.
- response_status는 반드시 "발주 가능", "조건부 가능", "보완 필요" 중 하나만 사용한다.
- response_summary는 2문장 이내의 업체형 회신으로 작성한다.
- 출력은 JSON 객체만 반환한다.
- JSON 구조: {json.dumps(schema_hint, ensure_ascii=False)}
"""
    payload = {
        "model": model,
        "stream": False,
        "temperature": 0.2,
        "max_tokens": 600,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": "너는 한국 식품 OEM/ODM 업체 영업 담당자다. 발주 가능성, 필요한 서류, 리스크를 보수적으로 회신한다.",
            },
            {"role": "user", "content": prompt},
        ],
    }
    try:
        response = requests.post(
            f"{SAM_BASE_URL.rstrip('/')}/openai/v1/chat/completions",
            headers=sam_headers(),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            timeout=45,
        )
        response.raise_for_status()
        data = response.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        parsed = json.loads(content) if isinstance(content, str) else content
        if not isinstance(parsed, dict):
            return None
        status = str(parsed.get("response_status", "")).strip()
        if status not in {"발주 가능", "조건부 가능", "보완 필요"}:
            parsed["response_status"] = fallback_status
        return parsed
    except Exception:
        return None


def fallback_board_reply(
    *,
    req: ProductRequest,
    vendor_name: str,
    persona: str,
    pdf_excerpt_text: str,
    response_status: str,
    budget_fit: str,
    moq: int,
    lead_time_days: int,
    risk_notes: list[str],
) -> dict[str, Any]:
    focus = board_pdf_excerpt(pdf_excerpt_text, 90)
    opening = f"안녕하세요. {vendor_name} 영업팀입니다."
    if response_status == "발주 가능":
        summary = (
            f"{opening} 공유해주신 PDF 기준으로 {req.product_case_label} {req.package_type or '제품'} 발주는 진행 가능합니다. "
            f"{focus or '핵심 사양'} 기준으로 MOQ {moq:,}{req.qty_unit}, 납기 {lead_time_days}일 조건에서 견적 검토가 가능합니다."
        )
    elif response_status == "조건부 가능":
        summary = (
            f"{opening} PDF 상 요구사항은 검토 가능하지만 샘플 승인과 주요 스펙 확정이 먼저 필요합니다. "
            f"{focus or '핵심 사양'} 기준으로 MOQ {moq:,}{req.qty_unit} 이상에서 조건부 발주 협의가 가능합니다."
        )
    else:
        summary = (
            f"{opening} 현재 PDF 기준으로는 즉시 발주 확정이 어렵고 보완 자료 확인이 우선입니다. "
            f"{focus or '핵심 사양'}와 관련된 검증 자료가 확보되면 재산정이 가능합니다."
        )
    counter_offer = ""
    if budget_fit == "예산초과":
        counter_offer = f"예산 범위에 맞추려면 MOQ {moq:,}{req.qty_unit} 기준 재조정 또는 포장 단순화안 검토가 필요합니다."
    elif risk_notes:
        counter_offer = f"{risk_notes[0]} 구간은 샘플 생산으로 먼저 확인한 뒤 본발주 전환을 권장합니다."
    return {
        "response_status": response_status,
        "response_summary": summary,
        "required_documents": [],
        "risk_notes": risk_notes,
        "counter_offer": counter_offer,
        "ai_reasoning": f"PDF 발췌와 {persona} 페르소나를 기준으로 회신을 구성했습니다.",
    }


def build_board_post_snapshot(
    db: Session,
    req: ProductRequest,
    order: PurchaseOrderRequest | None,
    source_pdf: GeneratedFile,
) -> dict[str, Any]:
    screening = db.scalar(select(ScreeningRun).where(ScreeningRun.request_id == req.id).order_by(ScreeningRun.id.desc()))
    findings = list(db.scalars(select(ScreeningFinding).where(ScreeningFinding.request_id == req.id).order_by(ScreeningFinding.id.desc()).limit(6)))
    cost = db.scalar(select(CostCalculation).where(CostCalculation.request_id == req.id).order_by(CostCalculation.id.desc()))
    process_plan = build_process_plan(db, req)
    compliance_summary = process_plan.get("compliance_summary", {})
    matches = list(db.scalars(select(MatchResult).where(MatchResult.request_id == req.id).order_by(MatchResult.score.desc()).limit(3)))
    factories = {row.id: row for row in db.scalars(select(Factory).where(Factory.id.in_([match.factory_id for match in matches])))} if matches else {}
    required_documents = clean_text_items(
        [
            *(from_json(order.required_documents, []) if order else []),
            *[finding.required_evidence for finding in findings if finding.required_evidence],
            "견적서",
            "리드타임표",
        ]
    )
    required_certifications = clean_text_items(
        [
            *compliance_summary.get("required_certifications", []),
            *([factories[matches[0].factory_id].certification_signal] if matches and matches[0].factory_id in factories else []),
        ]
    )
    priority_checks = clean_text_items(
        [
            *compliance_summary.get("priority_stages", []),
            *[finding.message for finding in findings[:3]],
        ]
    )
    quote_rules = clean_text_items(
        [
            "회신 시 MOQ, 샘플비, 양산 단가, 리드타임을 분리해서 기재",
            "부가세, 운송비, 인쇄비 포함 여부를 반드시 명시",
            "가능한 인증/표시 검토 범위와 제외 범위를 함께 적시",
            "예산을 초과하면 수량 또는 포장 단순화 대안을 함께 제안",
            "규제 또는 표시 리스크가 있으면 필요한 증빙을 먼저 요청",
        ]
    )
    line_items = from_json(order.line_items, []) if order else default_purchase_order_lines(db, req)
    budget_text = f"{req.budget_amount:,.0f}원" if req.budget_amount else "미입력"
    pdf_text = extract_pdf_text(source_pdf.storage_path)
    top_matches = [
        {
            "company_name": factories[match.factory_id].company_name if match.factory_id in factories else "후보 업체",
            "score": match.score,
            "reason": match.reason,
        }
        for match in matches
    ]
    summary = (
        f"{req.product_case_label} {req.package_type or '기본 포장'} 초도 생산 게시입니다. "
        f"희망 수량은 {req.target_qty:,}{req.qty_unit}, 예산은 {budget_text}이며, "
        "MOQ·샘플비·증빙 범위를 분리 제안받는 조건입니다."
    )
    return {
        "product_case_label": req.product_case_label,
        "sales_type": req.sales_type,
        "target_qty": req.target_qty,
        "qty_unit": req.qty_unit,
        "package_type": req.package_type,
        "budget_amount": req.budget_amount,
        "target_price": req.target_price,
        "screening_status": screening.overall_status if screening else "not_run",
        "estimated_supply_price": cost.supply_price if cost else 0,
        "projected_total": process_plan.get("projected_total", 0),
        "brokerage_fee": process_plan.get("brokerage_fee", 0),
        "claims": from_json(req.claim_list, []),
        "required_documents": required_documents,
        "required_certifications": required_certifications,
        "priority_checks": priority_checks,
        "quote_rules": quote_rules,
        "line_items": line_items,
        "top_matches": top_matches,
        "board_summary": summary,
        "source_order_mode": "purchase_order" if order else "request_mock",
        "source_pdf": serialize_generated_file_for_board(source_pdf),
        "pdf_excerpt": board_pdf_excerpt(pdf_text),
    }


def build_board_vendor_rules(
    req: ProductRequest,
    snapshot: dict[str, Any],
    moq: int,
    lead_time_days: int,
    budget_fit: str,
    profile_label: str,
) -> list[str]:
    rules = [
        f"초도 발주는 MOQ {moq:,}{req.qty_unit} 기준으로 단가를 확정합니다.",
        f"{req.package_type or '포장'} 교정본은 생산 {max(3, lead_time_days // 4)}영업일 전 승인 기준입니다.",
        f"{profile_label} 기준으로 샘플 승인 후 본생산 전환 여부를 결정합니다.",
    ]
    required_certifications = clean_text_items(snapshot.get("required_certifications", []))
    priority_checks = clean_text_items(snapshot.get("priority_checks", []))
    if required_certifications:
        rules.append(f"필수 인증/증빙은 {required_certifications[0]} 포함 여부를 먼저 확인합니다.")
    if priority_checks:
        rules.append(f"선확인 항목은 {priority_checks[0]} 입니다.")
    if budget_fit == "예산초과":
        rules.append("예산 내 진행을 원하면 포장 단순화 또는 수량 조정안을 함께 제안합니다.")
    elif req.sales_type in {"프랜차이즈", "PB", "B2B"}:
        rules.append("반복 발주형 거래를 가정하고 월간 수요 캘린더 공유를 요청합니다.")
    return clean_text_items(rules)


def upsert_board_post(db: Session, req: ProductRequest, payload: BoardPostCreate) -> ProcurementBoardPost:
    order = latest_purchase_order(db, req.id)
    source_pdf = load_request_board_pdf_or_400(db, req)
    snapshot = build_board_post_snapshot(db, req, order, source_pdf)
    snapshot["vendor_target_count"] = payload.vendor_count
    title = (payload.title or "").strip() or board_post_title(req)
    summary = (payload.summary or "").strip() or snapshot.get("board_summary", "")
    post = db.scalar(select(ProcurementBoardPost).where(ProcurementBoardPost.request_id == req.id).order_by(ProcurementBoardPost.created_at.desc()))
    if post:
        post.purchase_order_id = order.id if order else None
        post.title = title
        post.summary = summary
        post.target_snapshot = as_json(snapshot)
        post.updated_at = now_utc()
    else:
        post = ProcurementBoardPost(
            post_uid=str(uuid.uuid4()),
            request_id=req.id,
            purchase_order_id=order.id if order else None,
            title=title,
            summary=summary,
            target_snapshot=as_json(snapshot),
            status="processing",
        )
        db.add(post)
    db.flush()
    existing_bid_count = db.scalar(select(func.count(ProcurementBoardBid.id)).where(ProcurementBoardBid.board_post_id == post.id)) or 0
    if payload.regenerate_bids or existing_bid_count == 0:
        db.query(ProcurementBoardBid).filter(ProcurementBoardBid.board_post_id == post.id).delete(synchronize_session=False)
        post.status = "processing"
        post.updated_at = now_utc()
    return post


def generate_board_bids(db: Session, post: ProcurementBoardPost, vendor_count: int) -> list[ProcurementBoardBid]:
    req = db.get(ProductRequest, post.request_id)
    if not req:
        api_error(404, "request_not_found")
    snapshot = from_json(post.target_snapshot, {})
    process_plan = build_process_plan(db, req)
    cost = db.scalar(select(CostCalculation).where(CostCalculation.request_id == req.id).order_by(CostCalculation.id.desc()))
    matches = list(db.scalars(select(MatchResult).where(MatchResult.request_id == req.id).order_by(MatchResult.score.desc()).limit(vendor_count)))
    factories = {row.id: row for row in db.scalars(select(Factory).where(Factory.id.in_([match.factory_id for match in matches])))} if matches else {}
    high_risk_stages = [line["stage"] for line in process_plan.get("process_lines", []) if line.get("risk_level") == "HIGH"][:3]
    budget = float(req.budget_amount or snapshot.get("budget_amount") or 0)
    base_unit = max(float(cost.supply_price if cost else 0), float(process_plan.get("unit_estimate") or 0) * 1.18, 1.0)
    brokerage_rate = float(process_plan.get("brokerage_fee_rate") or 0.08)
    screening_status = snapshot.get("screening_status", "not_run")
    pdf_excerpt_text = str(snapshot.get("pdf_excerpt", "")).strip()

    db.query(ProcurementBoardBid).filter(ProcurementBoardBid.board_post_id == post.id).delete(synchronize_session=False)

    rows: list[ProcurementBoardBid] = []
    candidate_count = max(vendor_count, len(matches))
    for index in range(candidate_count):
        match = matches[index] if index < len(matches) else None
        factory = factories.get(match.factory_id) if match and match.factory_id in factories else None
        profile = board_vendor_profile(factory, req, index)
        moq = board_bid_moq(req, index)
        variance = deterministic_variance(req.id, factory.id if factory else f"virtual-{index}", req.target_qty)
        score_base = float(match.score) if match else 62 - (index * 1.5)
        quality_bonus = 4 if factory and "HACCP" in (factory.certification_signal or "") else 0
        risk_penalty = len(high_risk_stages) * 0.025
        status_penalty = 0.06 if screening_status == "RED" else 0.03 if screening_status == "YELLOW" else 0.0
        unit_quote = round(base_unit * (1 + variance + risk_penalty + status_penalty - (quality_bonus / 200)), 1)
        quote_total = round(unit_quote * req.target_qty, 1)
        brokerage_fee = round(quote_total * brokerage_rate, 1)
        total_with_fee = round(quote_total + brokerage_fee, 1)
        gap = round(budget - total_with_fee, 1) if budget else 0.0
        budget_fit = "예산내" if budget and gap >= 0 else "예산초과" if budget else "예산미입력"
        lead_seed = int(make_hash([post.id, factory.factory_code if factory else f"virtual-{index}"])[:4], 16)
        lead_time_days = 12 + (lead_seed % 14) + (2 if index % 2 else 0)
        bid_score = round(score_base + quality_bonus - (max(0, -gap / max(budget, 1)) * 18 if budget else 0) - len(high_risk_stages) * 2, 1)
        base_required_documents = clean_text_items(
            [
                *snapshot.get("required_documents", []),
                "MOQ 제안서",
                "샘플 일정표",
            ]
        )
        vendor_name = board_vendor_name(req, factory, index)
        response_status = board_feasibility_status(bid_score, budget_fit)
        persona = board_vendor_persona(factory, profile)
        fallback_reply = fallback_board_reply(
            req=req,
            vendor_name=vendor_name,
            persona=persona,
            pdf_excerpt_text=pdf_excerpt_text,
            response_status=response_status,
            budget_fit=budget_fit,
            moq=moq,
            lead_time_days=lead_time_days,
            risk_notes=high_risk_stages,
        )
        sam_reply = call_sam_board_reply(
            req=req,
            vendor_name=vendor_name,
            persona=persona,
            pdf_excerpt_text=pdf_excerpt_text,
            budget_fit=budget_fit,
            moq=moq,
            lead_time_days=lead_time_days,
            risk_notes=high_risk_stages,
            required_documents=base_required_documents,
            fallback_status=response_status,
        )
        response_status = str((sam_reply or {}).get("response_status") or fallback_reply["response_status"]).strip()
        if response_status not in {"발주 가능", "조건부 가능", "보완 필요"}:
            response_status = fallback_reply["response_status"]
        response_summary = str((sam_reply or {}).get("response_summary") or fallback_reply["response_summary"]).strip()
        required_documents = clean_text_items([*base_required_documents, *((sam_reply or {}).get("required_documents") or [])])
        risk_notes = clean_text_items([*high_risk_stages, *((sam_reply or {}).get("risk_notes") or [])])
        counter_offer = str((sam_reply or {}).get("counter_offer") or fallback_reply["counter_offer"]).strip()
        row = ProcurementBoardBid(
            board_post_id=post.id,
            request_id=req.id,
            factory_id=factory.id if factory else None,
            vendor_name=vendor_name,
            vendor_profile=profile["label"],
            response_status=response_status,
            response_summary=response_summary,
            quote_total=quote_total,
            unit_quote=unit_quote,
            brokerage_fee=brokerage_fee,
            total_with_fee=total_with_fee,
            moq=moq,
            lead_time_days=lead_time_days,
            budget_fit=budget_fit,
            budget_gap=gap,
            bid_score=bid_score,
            custom_order_rules=as_json(build_board_vendor_rules(req, snapshot, moq, lead_time_days, budget_fit, profile["label"])),
            required_documents=as_json(required_documents),
            risk_notes=as_json(risk_notes),
            counter_offer=counter_offer,
            ai_reasoning=str((sam_reply or {}).get("ai_reasoning") or fallback_reply["ai_reasoning"]),
        )
        db.add(row)
        rows.append(row)

    post.status = "answered"
    post.updated_at = now_utc()
    record_tool_run(
        db,
        req.id,
        "procurement_board_ai_bids",
        {"post_id": post.id, "vendor_count": vendor_count},
        f"게시판 PDF 기반 가상 발주 답변 {len(rows)}건 생성",
    )
    return rows


def process_pending_board_posts() -> None:
    with db_session() as db:
        rows = db.scalars(
            select(ProcurementBoardPost)
            .where(ProcurementBoardPost.status == "processing")
            .order_by(ProcurementBoardPost.updated_at.asc())
            .limit(24)
        ).all()
        now = now_utc()
        for post in rows:
            if (now - post.updated_at).total_seconds() < BOARD_REPLY_DELAY_SECONDS:
                continue
            try:
                snapshot = from_json(post.target_snapshot, {})
                vendor_count = int(snapshot.get("vendor_target_count") or 4)
                generate_board_bids(db, post, vendor_count)
            except Exception:
                post.status = "failed"
                post.updated_at = now_utc()


BOARD_REPLY_WORKER: threading.Thread | None = None


def start_board_reply_worker() -> None:
    global BOARD_REPLY_WORKER
    if BOARD_REPLY_WORKER and BOARD_REPLY_WORKER.is_alive():
        return

    def _worker() -> None:
        while True:
            try:
                process_pending_board_posts()
            except Exception:
                pass
            time.sleep(BOARD_REPLY_POLL_SECONDS)

    BOARD_REPLY_WORKER = threading.Thread(
        target=_worker,
        name="board-reply-worker",
        daemon=True,
    )
    BOARD_REPLY_WORKER.start()


def serialize_board_bid(bid: ProcurementBoardBid, factory: Factory | None = None) -> dict[str, Any]:
    negotiation_ox = board_bid_negotiation_ox(bid.response_status)
    return {
        "id": bid.id,
        "board_post_id": bid.board_post_id,
        "request_id": bid.request_id,
        "factory_id": bid.factory_id,
        "vendor_name": bid.vendor_name,
        "vendor_profile": bid.vendor_profile,
        "response_status": bid.response_status,
        "response_summary": bid.response_summary,
        "quote_total": bid.quote_total,
        "unit_quote": bid.unit_quote,
        "brokerage_fee": bid.brokerage_fee,
        "total_with_fee": bid.total_with_fee,
        "moq": bid.moq,
        "lead_time_days": bid.lead_time_days,
        "budget_fit": bid.budget_fit,
        "budget_gap": bid.budget_gap,
        "bid_score": bid.bid_score,
        "custom_order_rules": from_json(bid.custom_order_rules, []),
        "required_documents": from_json(bid.required_documents, []),
        "risk_notes": from_json(bid.risk_notes, []),
        "counter_offer": bid.counter_offer,
        "ai_reasoning": bid.ai_reasoning,
        "negotiation_ox": negotiation_ox,
        "rejection_ox": board_bid_rejection_ox(bid.response_status),
        "simulated_vendor": bid.factory_id is None,
        "factory": serialize_factory(factory) if factory else None,
        "created_at": bid.created_at.isoformat(),
    }


def build_board_post_response(
    post: ProcurementBoardPost,
    req: ProductRequest,
    bids: list[ProcurementBoardBid],
    factories: dict[int, Factory],
    include_bids: bool = False,
) -> dict[str, Any]:
    viewer_id = current_visitor_id(required=False)
    snapshot = from_json(post.target_snapshot, {})
    serialized_bids = [serialize_board_bid(bid, factories.get(bid.factory_id, None) if bid.factory_id else None) for bid in bids]
    outcome = summarize_board_bid_outcome(serialized_bids)
    payload = {
        "id": post.id,
        "post_uid": post.post_uid,
        "request_id": post.request_id,
        "purchase_order_id": post.purchase_order_id,
        "title": post.title,
        "status": post.status,
        "summary": post.summary,
        "target_snapshot": snapshot,
        "source_pdf": snapshot.get("source_pdf"),
        "request": serialize_request(req),
        "owned_by_viewer": req.visitor_id == viewer_id,
        "bid_count": len(serialized_bids),
        "top_bid": serialized_bids[0] if serialized_bids else None,
        **outcome,
        "created_at": post.created_at.isoformat(),
        "updated_at": post.updated_at.isoformat(),
    }
    if include_bids:
        payload["bids"] = serialized_bids
    return payload


def get_board_post_detail(db: Session, post: ProcurementBoardPost) -> dict[str, Any]:
    req = db.get(ProductRequest, post.request_id)
    if not req:
        api_error(404, "request_not_found")
    bids = db.scalars(select(ProcurementBoardBid).where(ProcurementBoardBid.board_post_id == post.id).order_by(ProcurementBoardBid.bid_score.desc(), ProcurementBoardBid.id.asc())).all()
    factories = {row.id: row for row in db.scalars(select(Factory).where(Factory.id.in_([bid.factory_id for bid in bids if bid.factory_id])))} if bids else {}
    return build_board_post_response(post, req, bids, factories, include_bids=True)


def delete_board_records(db: Session, request_ids: list[int]) -> None:
    if not request_ids:
        return
    post_ids = list(db.scalars(select(ProcurementBoardPost.id).where(ProcurementBoardPost.request_id.in_(request_ids))))
    if post_ids:
        db.query(ProcurementBoardBid).filter(ProcurementBoardBid.board_post_id.in_(post_ids)).delete(synchronize_session=False)
    db.query(ProcurementBoardPost).filter(ProcurementBoardPost.request_id.in_(request_ids)).delete(synchronize_session=False)


def parse_float_text(value: str) -> float:
    match = re.search(r"[-+]?\d[\d,]*(?:\.\d+)?", value or "")
    return float(match.group(0).replace(",", "")) if match else 0


def parse_date_text(value: str) -> str:
    match = re.search(r"(\d{4})[.\-/년\s]+(\d{1,2})[.\-/월\s]+(\d{1,2})", value or "")
    if not match:
        return ""
    year, month, day = (int(part) for part in match.groups())
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return ""


def extract_order_field(text: str, labels: list[str]) -> str:
    joined = "|".join(re.escape(label) for label in labels)
    patterns = [
        rf"(?:^|\n)\s*(?:{joined})\s*[:：]\s*([^\n\r]+)",
        rf"(?:^|\n)\s*(?:{joined})\s+([^\n\r]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip(" \t-|")
    return ""


def split_order_list(value: str) -> list[str]:
    return [item.strip(" -\t") for item in re.split(r"[\n,;/]+", value or "") if item.strip(" -\t")]


def parse_purchase_order_form(raw_order_form: str, req: ProductRequest | None = None) -> dict[str, Any]:
    text = raw_order_form.replace("\r\n", "\n").replace("\r", "\n")
    buyer = extract_order_field(text, ["발주처", "구매자", "매입처", "Buyer", "Purchaser"])
    supplier = extract_order_field(text, ["공급처", "수주처", "납품처", "제조사", "Supplier", "Vendor"])
    order_date = parse_date_text(extract_order_field(text, ["발주일", "주문일", "Order Date", "PO Date"]))
    due_date = parse_date_text(extract_order_field(text, ["납기일", "납품일", "입고일", "Delivery Date", "Due Date"]))
    delivery_place = extract_order_field(text, ["납품장소", "배송지", "입고장소", "Delivery Place", "Ship To"])
    payment_terms = extract_order_field(text, ["결제조건", "지급조건", "Payment Terms"])
    delivery_terms = extract_order_field(text, ["납품조건", "배송조건", "Delivery Terms"])
    inspection_terms = extract_order_field(text, ["검수조건", "검사조건", "Inspection Terms"])
    vat_text = extract_order_field(text, ["VAT", "부가세", "세액"])
    vat_type = "VAT 포함" if re.search(r"포함|included|incl", vat_text, re.IGNORECASE) else "VAT 별도" if vat_text else ""
    item_name = extract_order_field(text, ["품목명", "품명", "제품명", "상품명", "Item", "Product"])
    specification = extract_order_field(text, ["규격", "사양", "Spec", "Specification"])
    quantity_text = extract_order_field(text, ["수량", "Qty", "Quantity"])
    unit_price_text = extract_order_field(text, ["단가", "Unit Price"])
    unit = extract_order_field(text, ["단위", "Unit"])
    if quantity_text and not unit:
        unit_match = re.search(r"\d[\d,]*(?:\.\d+)?\s*([가-힣A-Za-z]+)", quantity_text)
        unit = unit_match.group(1) if unit_match else ""
    quantity = parse_float_text(quantity_text)
    unit_price = parse_float_text(unit_price_text)

    if not item_name:
        for line in text.split("\n"):
            compact = line.strip()
            if not compact or re.search(r"품목|수량|단가|금액", compact) and not re.search(r"\d", compact):
                continue
            if re.search(r"\d[\d,]*\s*(개|포|팩|병|kg|KG|톤)", compact):
                item_name = re.split(r"\s{2,}|\t|,", compact)[0].strip()
                if not quantity:
                    quantity = parse_float_text(compact)
                unit_match = re.search(r"\d[\d,]*(?:\.\d+)?\s*(개|포|팩|병|kg|KG|톤)", compact)
                unit = unit or (unit_match.group(1) if unit_match else "")
                numbers = re.findall(r"\d[\d,]*(?:\.\d+)?", compact)
                if not unit_price and len(numbers) >= 2:
                    unit_price = float(numbers[-1].replace(",", ""))
                break

    fallback_item = f"{req.product_case_label} {req.package_type} 생산" if req else "품목"
    line_items = []
    if item_name or quantity or unit_price:
        line_items.append(
            {
                "item_name": item_name or fallback_item,
                "specification": specification,
                "unit": unit or (req.qty_unit if req else "개"),
                "quantity": quantity or (req.target_qty if req else 1),
                "unit_price": unit_price,
                "requested_delivery_date": due_date,
                "notes": "발주 양식 원문에서 자동 추출",
            }
        )

    return {
        "buyer_company": buyer,
        "supplier_company": supplier,
        "order_date": order_date,
        "due_date": due_date,
        "delivery_place": delivery_place,
        "payment_terms": payment_terms,
        "delivery_terms": delivery_terms,
        "inspection_terms": inspection_terms,
        "vat_type": vat_type,
        "line_items": line_items,
        "quality_terms": split_order_list(extract_order_field(text, ["품질조건", "품질 기준", "Quality Terms"])),
        "required_documents": split_order_list(extract_order_field(text, ["필수서류", "첨부서류", "Required Documents"])),
    }


def normalize_purchase_order_lines(lines: list[PurchaseOrderLineCreate], fallback: list[dict[str, Any]]) -> list[dict[str, Any]]:
    source = [line.model_dump() for line in lines] if lines else fallback
    normalized = []
    for line in source:
        quantity = float(line.get("quantity") or 0)
        unit_price = float(line.get("unit_price") or 0)
        amount = round(quantity * unit_price, 1)
        normalized.append(
            {
                "item_name": str(line.get("item_name") or "품목"),
                "specification": str(line.get("specification") or ""),
                "unit": str(line.get("unit") or "개"),
                "quantity": quantity,
                "unit_price": unit_price,
                "amount": amount,
                "requested_delivery_date": str(line.get("requested_delivery_date") or ""),
                "notes": str(line.get("notes") or ""),
            }
        )
    return normalized


def purchase_order_risk_flags(order: PurchaseOrderCreate, lines: list[dict[str, Any]]) -> list[str]:
    flags = []
    if not order.supplier_company.strip():
        flags.append("공급처 상호가 비어 있습니다.")
    if not order.delivery_place.strip():
        flags.append("납품장소가 비어 있습니다.")
    if not order.due_date.strip() and not any(line.get("requested_delivery_date") for line in lines):
        flags.append("납기일이 비어 있습니다.")
    if not order.payment_terms.strip():
        flags.append("결제조건이 비어 있습니다.")
    if "VAT" not in order.vat_type.upper():
        flags.append("VAT 포함/별도 기준을 명확히 확인하세요.")
    if any(float(line.get("unit_price") or 0) <= 0 for line in lines):
        flags.append("단가 0원 품목이 있어 견적 확정 전 상태입니다.")
    if not order.raw_order_form.strip():
        flags.append("받은 실제 발주 양식 원문/메모가 비어 있습니다.")
    return flags


def create_purchase_order_record(db: Session, req: ProductRequest, payload: PurchaseOrderCreate) -> PurchaseOrderRequest:
    parsed = parse_purchase_order_form(payload.raw_order_form, req) if payload.raw_order_form.strip() else {}
    updates = {}
    for field in ["buyer_company", "supplier_company", "order_date", "due_date", "delivery_place", "payment_terms", "delivery_terms", "inspection_terms", "vat_type"]:
        parsed_value = parsed.get(field)
        if parsed_value and not str(getattr(payload, field, "")).strip():
            updates[field] = parsed_value
    if parsed.get("quality_terms") and not payload.quality_terms:
        updates["quality_terms"] = parsed["quality_terms"]
    if parsed.get("required_documents") and not payload.required_documents:
        updates["required_documents"] = parsed["required_documents"]
    effective = payload.model_copy(update=updates) if updates else payload

    matches = list(db.scalars(select(MatchResult).where(MatchResult.request_id == req.id).order_by(MatchResult.score.desc()).limit(1)))
    supplier_company = effective.supplier_company
    if not supplier_company and matches:
        factory = db.get(Factory, matches[0].factory_id)
        supplier_company = factory.company_name if factory else ""
    parsed_lines = parsed.get("line_items") or []
    fallback_lines = parsed_lines or default_purchase_order_lines(db, req)
    lines = normalize_purchase_order_lines(effective.line_items, fallback_lines)
    subtotal = round(sum(float(line["amount"]) for line in lines), 1)
    vat_amount = round(subtotal * 0.1, 1) if effective.vat_type == "VAT 별도" else 0
    total_amount = round(subtotal + vat_amount, 1)
    quality_terms = effective.quality_terms or ["입고 수량/외관 검수", "표시사항 초안 확인", "알레르기 및 원산지 증빙 확인"]
    required_documents = effective.required_documents or ["견적서", "사업자등록증", "HACCP 등 인증서", "시험성적서 또는 원료 규격서"]
    risk_payload = effective.model_copy(update={"supplier_company": supplier_company})
    risk_flags = purchase_order_risk_flags(risk_payload, lines)
    status = "needs_review" if risk_flags else "ready_to_send"
    order = PurchaseOrderRequest(
        po_uid=str(uuid.uuid4()),
        request_id=req.id,
        status=status,
        order_type=effective.order_type,
        buyer_company=effective.buyer_company,
        buyer_contact=effective.buyer_contact,
        supplier_company=supplier_company,
        supplier_contact=effective.supplier_contact,
        order_date=effective.order_date or now_utc().date().isoformat(),
        due_date=effective.due_date,
        delivery_place=effective.delivery_place,
        payment_terms=effective.payment_terms,
        delivery_terms=effective.delivery_terms,
        inspection_terms=effective.inspection_terms,
        vat_type=effective.vat_type,
        currency=effective.currency,
        raw_order_form=effective.raw_order_form,
        line_items=as_json(lines),
        subtotal=subtotal,
        vat_amount=vat_amount,
        total_amount=total_amount,
        quality_terms=as_json(quality_terms),
        required_documents=as_json(required_documents),
        risk_flags=as_json(risk_flags),
        is_dummy=effective.is_dummy,
    )
    db.add(order)
    record_tool_run(db, req.id, "purchase_order_builder", {"request_id": req.id}, f"발주요청 {status} / {len(lines)}개 품목")
    return order


def serialize_purchase_order(order: PurchaseOrderRequest) -> dict[str, Any]:
    return {
        "id": order.id,
        "po_uid": order.po_uid,
        "request_id": order.request_id,
        "status": order.status,
        "order_type": order.order_type,
        "buyer_company": order.buyer_company,
        "buyer_contact": order.buyer_contact,
        "supplier_company": order.supplier_company,
        "supplier_contact": order.supplier_contact,
        "order_date": order.order_date,
        "due_date": order.due_date,
        "delivery_place": order.delivery_place,
        "payment_terms": order.payment_terms,
        "delivery_terms": order.delivery_terms,
        "inspection_terms": order.inspection_terms,
        "vat_type": order.vat_type,
        "currency": order.currency,
        "raw_order_form": order.raw_order_form,
        "line_items": from_json(order.line_items, []),
        "subtotal": order.subtotal,
        "vat_amount": order.vat_amount,
        "total_amount": order.total_amount,
        "quality_terms": from_json(order.quality_terms, []),
        "required_documents": from_json(order.required_documents, []),
        "risk_flags": from_json(order.risk_flags, []),
        "is_dummy": order.is_dummy,
        "created_at": order.created_at.isoformat(),
        "updated_at": order.updated_at.isoformat(),
    }


def purchase_order_document_body(order: PurchaseOrderRequest) -> dict[str, Any]:
    buyer = display_text(order.buyer_company, "발주처 미입력")
    supplier = display_text(order.supplier_company, "공급처 미입력")
    line_items = from_json(order.line_items, [])
    line_table = [
        [
            display_text(line.get("item_name")),
            display_text(line.get("specification"), "-"),
            f"{format_quantity_text(float(line.get('quantity') or 0))}{display_text(line.get('unit'), '')}",
            format_currency_text(float(line.get("unit_price") or 0), order.currency),
            format_currency_text(float(line.get("amount") or 0), order.currency),
            "\n".join(
                clean_text_items(
                    [
                        f"납기: {line.get('requested_delivery_date')}" if str(line.get("requested_delivery_date") or "").strip() else "",
                        str(line.get("notes") or ""),
                    ]
                )
            )
            or "-",
        ]
        for line in line_items
    ] or [["품목 없음", "-", "-", "-", "-", "-"]]
    quality_terms = from_json(order.quality_terms, [])
    required_documents = from_json(order.required_documents, [])
    risk_flags = from_json(order.risk_flags, [])
    summary = f"{buyer}가 {supplier}에 전달할 식품 발주요청 초안입니다. 최종 계약 조건은 공급사 견적서와 계약서 확인 후 확정합니다."
    return make_document(
        title="식품 발주요청서",
        doc_kind="purchase_order",
        header_rows=[
            kv_row("발주번호", order.po_uid),
            kv_row("문서 상태", order_status_label(order.status)),
            kv_row("발주일", order.order_date),
            kv_row("납기일", order.due_date),
        ],
        notice="공급사 확인 및 내부 검토용 발주요청서입니다. 발주 확정 전 단가, 납기, 검수 기준, 제출 서류를 최종 대조해야 합니다.",
        summary=summary,
        sections=[
            section_kv(
                "1. 거래 당사자",
                [
                    kv_row("발주처", order.buyer_company),
                    kv_row("발주 담당자", order.buyer_contact),
                    kv_row("공급처", order.supplier_company),
                    kv_row("공급 담당자", order.supplier_contact),
                ],
            ),
            section_table(
                "2. 발주 품목",
                ["품목", "규격", "수량", "단가", "금액", "납기/비고"],
                line_table,
                widths=[0.18, 0.22, 0.12, 0.14, 0.14, 0.20],
            ),
            section_table(
                "3. 금액 정리",
                ["통화", "공급가 합계", "부가세 기준", "부가세 금액", "총액"],
                [[order.currency, format_currency_text(order.subtotal, order.currency), order.vat_type, format_currency_text(order.vat_amount, order.currency), format_currency_text(order.total_amount, order.currency)]],
                widths=[0.12, 0.22, 0.18, 0.18, 0.30],
            ),
            section_kv(
                "4. 납품·검수·결제 조건",
                [
                    kv_row("납품장소", order.delivery_place),
                    kv_row("납품조건", order.delivery_terms),
                    kv_row("결제조건", order.payment_terms),
                    kv_row("검수조건", order.inspection_terms),
                ],
            ),
            section_grouped_list(
                "5. 품질 기준 및 제출 서류",
                [
                    {"title": "품질/검수 기준", "items": quality_terms or ["입고 수량, 외관, 표시사항, 보관 기준 확인"]},
                    {"title": "필수 제출 서류", "items": required_documents or ["견적서, 사업자등록증, 인증서, 시험성적서 확인"]},
                ],
            ),
            section_list(
                "6. 발주 리스크 및 확인 사항",
                risk_flags or ["현재 자동 점검 기준상 필수 누락 항목은 없지만, 최종 계약 전 재확인이 필요합니다."],
            ),
            section_paragraph(
                "7. 받은 원문 메모",
                order.raw_order_form or "받은 실제 발주 양식 원문이나 메모가 없습니다.",
            ),
        ],
    )


def build_vibe_agent_report(db: Session, req: ProductRequest, payload: VibeAgentRun) -> dict[str, Any]:
    spec = db.scalar(select(ProductSpec).where(ProductSpec.request_id == req.id))
    recipe = db.scalar(select(RecipeDraft).where(RecipeDraft.request_id == req.id))
    ingredients = list(db.scalars(select(IngredientLine).where(IngredientLine.request_id == req.id)))
    screening = db.scalar(select(ScreeningRun).where(ScreeningRun.request_id == req.id).order_by(ScreeningRun.id.desc()))
    findings = list(db.scalars(select(ScreeningFinding).where(ScreeningFinding.request_id == req.id)))
    matches = list(db.scalars(select(MatchResult).where(MatchResult.request_id == req.id).order_by(MatchResult.score.desc()).limit(5)))
    factories = {f.id: f for f in db.scalars(select(Factory).where(Factory.id.in_([m.factory_id for m in matches])))} if matches else {}
    cost = db.scalar(select(CostCalculation).where(CostCalculation.request_id == req.id).order_by(CostCalculation.id.desc()))
    recipe_snapshot = build_recipe_execution_snapshot(req, spec, recipe, ingredients)
    report = compose_vibe_agent_report(
        req=req,
        planning_goal=payload.planning_goal,
        include_revision_prompt=payload.include_revision_prompt,
        spec=spec,
        recipe=recipe,
        ingredients=ingredients,
        screening=screening,
        findings=findings,
        matches=matches,
        factories=factories,
        cost=cost,
        recipe_snapshot=recipe_snapshot,
    )
    record_tool_run(
        db,
        req.id,
        "vibe_cooking_agent",
        {"planning_goal": payload.planning_goal},
        f"기획 적합도 {report['readiness_score']}점 / {report['decision']}",
    )
    return report


def request_detail(db: Session, req: ProductRequest, include_private: bool = False) -> dict[str, Any]:
    spec = db.scalar(select(ProductSpec).where(ProductSpec.request_id == req.id))
    recipe = db.scalar(select(RecipeDraft).where(RecipeDraft.request_id == req.id))
    ingredients = list(db.scalars(select(IngredientLine).where(IngredientLine.request_id == req.id)))
    screening = db.scalar(select(ScreeningRun).where(ScreeningRun.request_id == req.id).order_by(ScreeningRun.id.desc()))
    findings = list(db.scalars(select(ScreeningFinding).where(ScreeningFinding.request_id == req.id)))
    matches = list(db.scalars(select(MatchResult).where(MatchResult.request_id == req.id).order_by(MatchResult.score.desc()).limit(5)))
    factories = {f.id: f for f in db.scalars(select(Factory).where(Factory.id.in_([m.factory_id for m in matches])))} if matches else {}
    plan = db.scalar(select(ProductPlan).where(ProductPlan.request_id == req.id))
    brief = db.scalar(select(SampleBrief).where(SampleBrief.request_id == req.id))
    cost = db.scalar(select(CostCalculation).where(CostCalculation.request_id == req.id).order_by(CostCalculation.id.desc()))
    purchase_orders = list(db.scalars(select(PurchaseOrderRequest).where(PurchaseOrderRequest.request_id == req.id).order_by(PurchaseOrderRequest.created_at.desc())))
    generated_pdfs = list(
        db.scalars(
            select(GeneratedFile)
            .where(
                GeneratedFile.request_id == req.id,
                GeneratedFile.doc_type.in_(("product_plan", "sample_brief")),
                GeneratedFile.created_at >= req.created_at,
            )
            .order_by(GeneratedFile.created_at.desc())
        )
    )
    tool_runs = list(db.scalars(select(ToolRun).where(ToolRun.request_id == req.id).order_by(ToolRun.id)))
    recipe_snapshot = build_recipe_execution_snapshot(req, spec, recipe, ingredients)
    process_plan = annotate_process_plan_with_compliance(
        build_process_plan(db, req),
        req,
        findings,
        [factories[match.factory_id] for match in matches if match.factory_id in factories],
        screening.overall_status if screening else "not_run",
    )
    agent_report = compose_vibe_agent_report(
        req=req,
        planning_goal=DEFAULT_VIBE_AGENT_REPORT_GOAL,
        include_revision_prompt=True,
        spec=spec,
        recipe=recipe,
        ingredients=ingredients,
        screening=screening,
        findings=findings,
        matches=matches,
        factories=factories,
        cost=cost,
        recipe_snapshot=recipe_snapshot,
    )
    return {
        **serialize_request(req, detail=True, include_private=include_private),
        "spec": {
            "concept": from_json(spec.concept, {}),
            "process_list": from_json(spec.process_list, []),
            "package_condition": from_json(spec.package_condition, {}),
            "storage_condition": spec.storage_condition,
            "cost_assumption": from_json(spec.cost_assumption, {}),
            "validation_questions": from_json(spec.validation_questions, []),
        }
        if spec
        else None,
        "recipe": {
            "id": recipe.id,
            "batch_size": recipe.batch_size,
            "unit_weight": recipe.unit_weight,
            "yield_rate": recipe.yield_rate,
            "quality_targets": from_json(recipe.quality_targets, []),
            "summary": recipe_snapshot["summary"],
            "formula_lines": recipe_snapshot["formula_lines"],
            "execution_steps": recipe_snapshot["execution_steps"],
            "predicted_results": recipe_snapshot["predicted_results"],
            "ingredients": [
                {
                    "role": line.ingredient_role,
                    "name": line.ingredient_name,
                    "ratio_range": line.ratio_range,
                    "allergen_flag": line.allergen_flag,
                    "substitute_allowed": line.substitute_allowed,
                }
                for line in ingredients
            ],
        }
        if recipe
        else None,
        "screening": {
            "overall_status": screening.overall_status if screening else "not_run",
            "findings": [
                {"rule_id": f.rule_id, "severity": f.severity, "message": f.message, "required_evidence": f.required_evidence, "source_url": f.source_url}
                for f in findings
            ],
        },
        "matches": [
            {
                "id": match.id,
                "score": match.score,
                "reason": match.reason,
                "status": match.status,
                "confirm_questions": from_json(match.confirm_questions, []),
                "factory": serialize_factory(factories[match.factory_id]) if match.factory_id in factories else None,
            }
            for match in matches
        ],
        "agent_report": agent_report,
        "process_plan": process_plan,
        "product_plan": {
            "id": plan.id,
            "status": plan.status,
            "body": normalize_document_body("product_plan", from_json(plan.body, {}), req, plan.status),
        }
        if plan
        else None,
        "sample_brief": {
            "id": brief.id,
            "status": brief.status,
            "body": normalize_document_body("sample_brief", from_json(brief.body, {}), req, brief.status),
        }
        if brief
        else None,
        "cost_calculation": {
            "id": cost.id,
            "target_qty": cost.target_qty,
            "serving_unit": cost.serving_unit,
            "total_cost": cost.total_cost,
            "unit_cost": cost.unit_cost,
            "supply_price": cost.supply_price,
            "vat_included_total": cost.vat_included_total,
            "body": from_json(cost.body, {}),
        }
        if cost
        else None,
        "generated_pdfs": [
            serialize_generated_file_for_owner(generated) for generated in generated_pdfs
        ],
        "purchase_orders": [serialize_purchase_order(order) for order in purchase_orders],
        "tool_runs": [
            {"tool_name": run.tool_name, "status": run.status, "summary": run.summary, "finished_at": run.finished_at.isoformat() if run.finished_at else None}
            for run in tool_runs
        ],
    }


def render_pdf(db: Session, req: ProductRequest, doc_type: str, purchase_order: PurchaseOrderRequest | None = None) -> GeneratedFile:
    detail = request_detail(db, req)
    if doc_type == "product_plan":
        data = detail["product_plan"]["body"] if detail["product_plan"] else None
    elif doc_type == "sample_brief":
        data = detail["sample_brief"]["body"] if detail["sample_brief"] else None
    elif doc_type == "purchase_order":
        order = purchase_order or db.scalar(select(PurchaseOrderRequest).where(PurchaseOrderRequest.request_id == req.id).order_by(PurchaseOrderRequest.created_at.desc()))
        data = purchase_order_document_body(order) if order else None
    else:
        data = None
    if not data:
        api_error(404, "document_not_ready")

    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    file_uid = str(uuid.uuid4())
    output = GENERATED_DIR / f"{file_uid}_{doc_type}.pdf"
    font = register_font()
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="KTitle", fontName=font, fontSize=18, leading=24, alignment=TA_CENTER, spaceAfter=8))
    styles.add(ParagraphStyle(name="KMeta", fontName=font, fontSize=8, leading=12, alignment=TA_CENTER, textColor=colors.HexColor("#5c6b61"), spaceAfter=4))
    styles.add(ParagraphStyle(name="KHeading", fontName=font, fontSize=12, leading=16, textColor=colors.HexColor("#0f5132"), spaceBefore=8, spaceAfter=4))
    styles.add(ParagraphStyle(name="KSubHeading", fontName=font, fontSize=10, leading=14, textColor=colors.HexColor("#21352b"), spaceBefore=4, spaceAfter=2))
    styles.add(ParagraphStyle(name="KBody", fontName=font, fontSize=9, leading=13, wordWrap="CJK"))
    styles.add(ParagraphStyle(name="KSummary", fontName=font, fontSize=9, leading=14, textColor=colors.HexColor("#20322a"), spaceAfter=6, wordWrap="CJK"))
    styles.add(ParagraphStyle(name="KNote", fontName=font, fontSize=8, leading=12, textColor=colors.HexColor("#66756c"), wordWrap="CJK"))
    styles.add(ParagraphStyle(name="KBullet", fontName=font, fontSize=9, leading=13, leftIndent=10, firstLineIndent=-8, wordWrap="CJK"))
    doc = SimpleDocTemplate(str(output), pagesize=A4, rightMargin=16 * mm, leftMargin=16 * mm, topMargin=15 * mm, bottomMargin=15 * mm)
    body_width = A4[0] - (32 * mm)

    def paragraph_cell(value: Any, style_name: str = "KBody", empty: str = "미입력") -> Paragraph:
        return Paragraph(escape(display_text(value, empty)).replace("\n", "<br/>"), styles[style_name])

    def ratio_widths(ratios: list[float], count: int) -> list[float]:
        if ratios and len(ratios) == count and sum(ratios) > 0:
            total = sum(ratios)
            return [body_width * (ratio / total) for ratio in ratios]
        return [body_width / max(count, 1)] * count

    def build_table(rows: list[list[Any]], col_widths: list[float], header: bool = False) -> Table:
        table = Table(rows, colWidths=col_widths, repeatRows=1 if header else 0)
        table.setStyle(
            TableStyle(
                [
                    ("FONTNAME", (0, 0), (-1, -1), font),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("LEADING", (0, 0), (-1, -1), 11),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d6ddd8")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 5),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        if header:
            table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eaf4ee")),
                        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#234935")),
                    ]
                )
            )
        return table

    story: list[Any] = [Paragraph(doc_paragraph_markup(data.get("title", "문서")), styles["KTitle"])]
    story.append(Paragraph(doc_paragraph_markup(f"생성일시 {data.get('generated_at', format_doc_timestamp())} | {data.get('draft_status', '검토용 초안')}"), styles["KMeta"]))
    story.append(Spacer(1, 3 * mm))

    header_rows = data.get("header_rows", []) if isinstance(data.get("header_rows"), list) else []
    if header_rows:
        header_table = build_table(
            [[paragraph_cell(row.get("label")), paragraph_cell(row.get("value"))] for row in header_rows if isinstance(row, dict)],
            [46 * mm, body_width - (46 * mm)],
        )
        story.append(header_table)
        story.append(Spacer(1, 3 * mm))

    notice = display_text(data.get("document_notice"), "")
    if notice:
        notice_table = Table([[paragraph_cell(notice, "KBody", "")]], colWidths=[body_width])
        notice_table.setStyle(
            TableStyle(
                [
                    ("FONTNAME", (0, 0), (-1, -1), font),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fff7e6")),
                    ("BOX", (0, 0), (-1, -1), 0.25, colors.HexColor("#e1c677")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        story.append(notice_table)
        story.append(Spacer(1, 3 * mm))

    summary = display_text(data.get("summary"), "")
    if summary:
        story.append(Paragraph(doc_paragraph_markup(summary), styles["KSummary"]))
        story.append(Spacer(1, 2 * mm))

    for section in data.get("sections", []) if isinstance(data.get("sections"), list) else []:
        if not isinstance(section, dict):
            continue
        story.append(Paragraph(doc_paragraph_markup(section.get("heading", "섹션")), styles["KHeading"]))
        note = display_text(section.get("note"), "")
        if note:
            story.append(Paragraph(doc_paragraph_markup(note), styles["KNote"]))
            story.append(Spacer(1, 1 * mm))
        section_type = section.get("type")
        if section_type == "kv":
            rows = section.get("rows", []) if isinstance(section.get("rows"), list) else []
            kv_table = build_table(
                [[paragraph_cell(row.get("label")), paragraph_cell(row.get("value"))] for row in rows if isinstance(row, dict)],
                [46 * mm, body_width - (46 * mm)],
            )
            story.append(kv_table)
        elif section_type == "table":
            columns = section.get("columns", []) if isinstance(section.get("columns"), list) else []
            rows = section.get("rows", []) if isinstance(section.get("rows"), list) else []
            table_rows = [[paragraph_cell(value, "KBody", "") for value in columns]]
            for row in rows:
                if isinstance(row, list):
                    table_rows.append([paragraph_cell(value) for value in row])
            story.append(build_table(table_rows, ratio_widths(section.get("widths", []), len(columns)), header=True))
        elif section_type == "list":
            items = section.get("items", []) if isinstance(section.get("items"), list) else []
            for item in items:
                story.append(Paragraph(f"- {doc_paragraph_markup(item)}", styles["KBullet"]))
        elif section_type == "grouped_list":
            groups = section.get("groups", []) if isinstance(section.get("groups"), list) else []
            for group in groups:
                if not isinstance(group, dict):
                    continue
                story.append(Paragraph(doc_paragraph_markup(group.get("title", "항목")), styles["KSubHeading"]))
                items = group.get("items", []) if isinstance(group.get("items"), list) else []
                for item in items:
                    story.append(Paragraph(f"- {doc_paragraph_markup(item)}", styles["KBullet"]))
        else:
            story.append(Paragraph(doc_paragraph_markup(section.get("body", "")), styles["KBody"]))
        story.append(Spacer(1, 3 * mm))

    doc.build(story)
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    generated = GeneratedFile(file_uid=file_uid, request_id=req.id, doc_type=doc_type, storage_path=str(output), checksum=checksum)
    db.add(generated)
    record_tool_run(db, req.id, "pdf_renderer", {"doc_type": doc_type}, f"{doc_type} PDF 생성")
    return generated


app = Flask(__name__, static_folder=None)
CORS(app)


def startup() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    ensure_schema_columns()
    with db_session() as db:
        seed_database(db)
    start_board_reply_worker()


startup()


@app.get("/")
def index():
    return send_from_directory(str(STATIC_DIR), "index.html")


@app.get("/<path:asset_path>")
def frontend_asset(asset_path: str):
    if asset_path.startswith("api/"):
        abort(404)
    asset_file = STATIC_DIR / asset_path
    if asset_file.is_file():
        return send_from_directory(str(STATIC_DIR), asset_path)
    return send_from_directory(str(STATIC_DIR), "index.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    with db_session() as db:
        return {
            "status": "ok",
            "db": str(DB_PATH),
            "sam_configured": bool(SAM_API_KEY),
            "default_llm_model": SAM_DEFAULT_MODEL,
            "deepseek_models": DEEPSEEK_MODELS,
            "factories": db.scalar(select(func.count(Factory.id))),
            "rules": db.scalar(select(func.count(RegulatoryRule.id))),
            "requests": db.scalar(select(func.count(ProductRequest.id))),
            "board_posts": db.scalar(select(func.count(ProcurementBoardPost.id))),
        }


@app.get("/api/llm/models")
def list_llm_models() -> dict[str, Any]:
    models = [{"alias": alias, "provider": "microsoft_foundry", "selected": alias == SAM_DEFAULT_MODEL} for alias in DEEPSEEK_MODELS]
    try:
        response = requests.get(f"{SAM_BASE_URL.rstrip('/')}/v1/models?task=chat", headers=sam_headers(), timeout=15)
        response.raise_for_status()
        catalog = response.json().get("models", [])
        deepseek = [model for model in catalog if "deepseek" in str(model.get("alias", "")).lower()]
        if deepseek:
            models = [
                {
                    "alias": model.get("alias"),
                    "provider": model.get("provider"),
                    "capabilities": model.get("capabilities", {}),
                    "selected": model.get("alias") == SAM_DEFAULT_MODEL,
                }
                for model in deepseek
            ]
    except Exception:
        pass
    return {"default": SAM_DEFAULT_MODEL, "sam_configured": bool(SAM_API_KEY), "models": models}


@app.get("/api/vibe-cooking/options")
def get_vibe_cooking_options() -> dict[str, Any]:
    return vibe_options()


@app.post("/api/vibe-cooking/compose")
def compose_vibe_cooking_request() -> dict[str, Any]:
    payload = json_payload(VibeCookingCompose)
    return compose_vibe_cooking(payload)


@app.post("/api/product-requests/agent-preview")
def preview_product_request_agent() -> dict[str, Any]:
    payload = json_payload(ProductRequestAgentPreview)
    return build_agent_preview(payload)


@app.get("/api/summary")
def summary() -> dict[str, Any]:
    visitor_id = current_visitor_id()
    with db_session() as db:
        status_rows = db.execute(
            select(ProductRequest.status, func.count(ProductRequest.id))
            .where(ProductRequest.visitor_id == visitor_id)
            .group_by(ProductRequest.status)
        ).all()
        case_rows = db.execute(
            select(ProductRequest.product_case_label, func.count(ProductRequest.id))
            .where(ProductRequest.visitor_id == visitor_id)
            .group_by(ProductRequest.product_case_label)
        ).all()
        return {
            "status_counts": dict(status_rows),
            "case_counts": dict(case_rows),
            "factory_count": db.scalar(select(func.count(Factory.id)).where(Factory.active.is_(True))),
            "red_findings": db.scalar(
                select(func.count(ScreeningFinding.id))
                .join(ProductRequest, ProductRequest.id == ScreeningFinding.request_id)
                .where(ProductRequest.visitor_id == visitor_id, ScreeningFinding.severity == "RED")
            ),
            "board_posts": db.scalar(
                select(func.count(ProcurementBoardPost.id))
                .join(ProductRequest, ProductRequest.id == ProcurementBoardPost.request_id)
                .where(ProductRequest.visitor_id == visitor_id)
            ),
        }


@app.post("/api/product-requests")
def create_product_request() -> dict[str, Any]:
    payload = json_payload(ProductRequestCreate)
    visitor_id = current_visitor_id()
    with db_session() as db:
        answers = normalize_answer_map(payload.answers)
        effective_prompt = merge_agent_prompt(payload.raw_prompt, answers)
        case_key = resolve_public_product_case(effective_prompt, payload.product_case)
        if not case_key:
            api_error(422, "unsupported_case")
        qty_seed = (payload.target_qty_text or answers.get("target_qty_text") or "").strip()
        if not payload.target_qty and not qty_seed:
            api_error(422, "target_qty_required")
        qty, qty_unit = normalize_qty(payload.target_qty, qty_seed)
        if payload.qty_unit:
            qty_unit = payload.qty_unit
        sales_type = infer_sales_type(
            " ".join([effective_prompt, answers.get("usage_context", ""), answers.get("success_metric", "")]).strip(),
            payload.sales_type,
        )
        package_type = package_type_from_inputs(case_key, effective_prompt, payload.package_type, answers)
        claims = normalize_claims(
            effective_prompt,
            [
                *payload.claim_list,
                *split_hint_items(answers.get("claim_focus")),
            ],
        )
        process_mode = normalize_process_mode(case_key, payload.process_mode, effective_prompt, claims)
        ingredient_keywords = clean_text_items(
            [
                *payload.ingredient_keywords,
                *split_hint_items(answers.get("ingredient_constraints")),
            ]
        )
        llm_model = payload.llm_model if payload.llm_model in DEEPSEEK_MODELS else SAM_DEFAULT_MODEL
        target_price = (payload.target_price or answers.get("price_priority") or "").strip()
        budget_text = (payload.budget_text or answers.get("price_priority") or "").strip()
        budget_amount = parse_budget_amount(
            payload.budget_amount,
            budget_text,
            answers.get("price_priority"),
            effective_prompt,
        )
        req = ProductRequest(
            request_uid=str(uuid.uuid4()),
            user_id=default_public_user_id(db),
            visitor_id=visitor_id,
            product_case=case_key,
            product_case_label=PRODUCT_CASES[case_key]["label"],
            raw_prompt=effective_prompt,
            sales_type=sales_type,
            target_qty=qty,
            qty_unit=qty_unit,
            package_type=package_type,
            llm_model=llm_model,
            claim_list=as_json(claims),
            taste_tags=as_json(generate_taste_tags(case_key, process_mode, claims, ingredient_keywords)),
            target_price=target_price,
            budget_amount=budget_amount,
            is_dummy=payload.is_dummy,
        )
        db.add(req)
        db.flush()
        if payload.run_full:
            run_full_pipeline(db, req)
        return request_detail(db, req)


@app.get("/api/product-requests")
def list_product_requests() -> dict[str, Any]:
    status = request.args.get("status")
    product_case = request.args.get("product_case")
    include_dummy = query_bool("include_dummy") or False
    limit = query_int("limit", 20, 1, 100)
    offset = query_int("offset", 0, 0)
    visitor_id = current_visitor_id()
    with db_session() as db:
        filters = [ProductRequest.visitor_id == visitor_id]
        if status:
            filters.append(ProductRequest.status == status)
        if product_case:
            filters.append(ProductRequest.product_case == product_case)
        if not include_dummy:
            filters.append(ProductRequest.is_dummy.is_(False))
        total = db.scalar(select(func.count(ProductRequest.id)).where(*filters))
        rows = db.scalars(select(ProductRequest).where(*filters).order_by(ProductRequest.created_at.desc()).limit(limit).offset(offset)).all()
        return {"total": total, "limit": limit, "offset": offset, "items": [serialize_request(row) for row in rows]}


@app.get("/api/product-requests/<int:request_id>")
def get_product_request(request_id: int) -> dict[str, Any]:
    with db_session() as db:
        req = load_owned_request_or_404(db, request_id)
        return request_detail(db, req)


@app.post("/api/product-requests/<int:request_id>/vibe-agent")
def run_vibe_agent(request_id: int) -> dict[str, Any]:
    payload = json_payload(VibeAgentRun)
    with db_session() as db:
        req = load_owned_request_or_404(db, request_id)
        return build_vibe_agent_report(db, req, payload)


@app.post("/api/product-requests/<int:request_id>/contact-simulation")
def run_contact_simulation(request_id: int) -> dict[str, Any]:
    payload = json_payload(ContactSimulationRun)
    with db_session() as db:
        req = load_owned_request_or_404(db, request_id)
        return simulate_vendor_contacts(db, req, payload)


@app.post("/api/product-requests/<int:request_id>/tool-runs/full")
def rerun_full(request_id: int) -> dict[str, Any]:
    with db_session() as db:
        req = load_owned_request_or_404(db, request_id)
        run_full_pipeline(db, req)
        return request_detail(db, req)


@app.post("/api/product-requests/<int:request_id>/cost-calculations")
def create_cost_calculation(request_id: int) -> dict[str, Any]:
    payload = json_payload(CostCalculationCreate)
    with db_session() as db:
        req = load_owned_request_or_404(db, request_id)
        calc = calculate_cost(db, req, payload)
        return {
            "id": calc.id,
            "unit_cost": calc.unit_cost,
            "total_cost": calc.total_cost,
            "supply_price": calc.supply_price,
            "vat_included_total": calc.vat_included_total,
            "body": from_json(calc.body, {}),
        }


@app.post("/api/product-requests/<int:request_id>/purchase-orders")
def create_purchase_order(request_id: int) -> dict[str, Any]:
    payload = json_payload(PurchaseOrderCreate)
    with db_session() as db:
        req = load_owned_request_or_404(db, request_id)
        order = create_purchase_order_record(db, req, payload)
        db.flush()
        return serialize_purchase_order(order)


@app.post("/api/product-requests/<int:request_id>/purchase-orders/parse-form")
def parse_purchase_order_form_endpoint(request_id: int) -> dict[str, Any]:
    payload = json_payload(PurchaseOrderFormParse)
    with db_session() as db:
        req = load_owned_request_or_404(db, request_id)
        parsed = parse_purchase_order_form(payload.raw_order_form, req)
        return {"parsed": parsed}


@app.get("/api/product-requests/<int:request_id>/purchase-orders")
def list_purchase_orders(request_id: int) -> dict[str, Any]:
    with db_session() as db:
        load_owned_request_or_404(db, request_id)
        rows = db.scalars(select(PurchaseOrderRequest).where(PurchaseOrderRequest.request_id == request_id).order_by(PurchaseOrderRequest.created_at.desc())).all()
        return {"items": [serialize_purchase_order(row) for row in rows]}


@app.get("/api/purchase-orders/<int:order_id>")
def get_purchase_order(order_id: int) -> dict[str, Any]:
    with db_session() as db:
        order = load_owned_purchase_order_or_404(db, order_id)
        return serialize_purchase_order(order)


@app.post("/api/purchase-orders/<int:order_id>/documents/pdf")
def create_purchase_order_pdf(order_id: int) -> dict[str, Any]:
    with db_session() as db:
        order = load_owned_purchase_order_or_404(db, order_id)
        req = load_owned_request_or_404(db, order.request_id)
        generated = render_pdf(db, req, "purchase_order", order)
        return {"file_uid": generated.file_uid, "download_url": download_url_for(generated.file_uid), "checksum": generated.checksum}


@app.post("/api/product-requests/<int:request_id>/board-posts")
def create_board_post(request_id: int) -> dict[str, Any]:
    payload = json_payload(BoardPostCreate)
    with db_session() as db:
        req = load_owned_request_or_404(db, request_id)
        post = upsert_board_post(db, req, payload)
        return get_board_post_detail(db, post)


@app.get("/api/board-posts")
def list_board_posts() -> dict[str, Any]:
    status = request.args.get("status")
    include_dummy = query_bool("include_dummy") or False
    mine_only = query_bool("mine") or False
    limit = query_int("limit", 20, 1, 100)
    offset = query_int("offset", 0, 0)
    visitor_id = current_visitor_id(required=False)
    with db_session() as db:
        filters: list[Any] = []
        if status:
            filters.append(ProcurementBoardPost.status == status)
        if not include_dummy:
            filters.append(ProductRequest.is_dummy.is_(False))
        if mine_only and visitor_id:
            filters.append(ProductRequest.visitor_id == visitor_id)
        total = db.scalar(select(func.count(ProcurementBoardPost.id)).join(ProductRequest, ProductRequest.id == ProcurementBoardPost.request_id).where(*filters))
        rows = db.scalars(
            select(ProcurementBoardPost)
            .join(ProductRequest, ProductRequest.id == ProcurementBoardPost.request_id)
            .where(*filters)
            .order_by(ProcurementBoardPost.created_at.desc())
            .limit(limit)
            .offset(offset)
        ).all()
        request_ids = [row.request_id for row in rows]
        requests_map = {row.id: row for row in db.scalars(select(ProductRequest).where(ProductRequest.id.in_(request_ids)))} if request_ids else {}
        post_ids = [row.id for row in rows]
        bid_rows = (
            db.scalars(
                select(ProcurementBoardBid)
                .where(ProcurementBoardBid.board_post_id.in_(post_ids))
                .order_by(ProcurementBoardBid.board_post_id.asc(), ProcurementBoardBid.bid_score.desc(), ProcurementBoardBid.id.asc())
            ).all()
            if post_ids
            else []
        )
        factory_ids = [bid.factory_id for bid in bid_rows if bid.factory_id]
        factories = {row.id: row for row in db.scalars(select(Factory).where(Factory.id.in_(factory_ids)))} if factory_ids else {}
        grouped: dict[int, list[ProcurementBoardBid]] = {}
        for bid in bid_rows:
            grouped.setdefault(bid.board_post_id, []).append(bid)
        items = [
            build_board_post_response(post, requests_map[post.request_id], grouped.get(post.id, []), factories, include_bids=False)
            for post in rows
            if post.request_id in requests_map
        ]
        return {"total": total, "limit": limit, "offset": offset, "items": items}


@app.get("/api/board-posts/<int:post_id>")
def get_board_post(post_id: int) -> dict[str, Any]:
    with db_session() as db:
        post = load_public_board_post_or_404(db, post_id)
        return get_board_post_detail(db, post)


@app.post("/api/board-posts/<int:post_id>/ai-bids")
def refresh_board_post_ai_bids(post_id: int) -> dict[str, Any]:
    payload = json_payload(BoardBidRefresh)
    with db_session() as db:
        post = load_owned_board_post_or_404(db, post_id)
        req = load_owned_request_or_404(db, post.request_id)
        order = latest_purchase_order(db, req.id)
        source_pdf = load_request_board_pdf_or_400(db, req)
        post.purchase_order_id = order.id if order else None
        snapshot = build_board_post_snapshot(db, req, order, source_pdf)
        snapshot["vendor_target_count"] = payload.vendor_count
        post.target_snapshot = as_json(snapshot)
        db.query(ProcurementBoardBid).filter(ProcurementBoardBid.board_post_id == post.id).delete(synchronize_session=False)
        post.status = "processing"
        post.updated_at = now_utc()
        db.flush()
        return get_board_post_detail(db, post)


@app.delete("/api/product-requests/<int:request_id>")
def delete_product_request(request_id: int) -> dict[str, Any]:
    with db_session() as db:
        req = load_owned_request_or_404(db, request_id)
        delete_board_records(db, [request_id])
        db.query(PurchaseOrderRequest).filter(PurchaseOrderRequest.request_id == request_id).delete()
        db.query(GeneratedFile).filter(GeneratedFile.request_id == request_id).delete()
        db.delete(req)
        return {"deleted": request_id}


@app.delete("/api/dummy-data")
def delete_dummy_data() -> dict[str, Any]:
    with db_session() as db:
        ids = [row.id for row in db.scalars(select(ProductRequest).where(ProductRequest.is_dummy.is_(True)))]
        order_ids = [row.id for row in db.scalars(select(PurchaseOrderRequest).where(PurchaseOrderRequest.is_dummy.is_(True)))]
        delete_board_records(db, ids)
        if ids:
            db.query(GeneratedFile).filter(GeneratedFile.request_id.in_(ids)).delete(synchronize_session=False)
        for order_id in order_ids:
            order = db.get(PurchaseOrderRequest, order_id)
            if order:
                db.delete(order)
        for request_id in ids:
            db.query(PurchaseOrderRequest).filter(PurchaseOrderRequest.request_id == request_id).delete()
            req = db.get(ProductRequest, request_id)
            if req:
                db.delete(req)
        return {"deleted": ids, "deleted_purchase_orders": order_ids, "count": len(ids)}


@app.get("/api/admin/factories")
def list_factories() -> dict[str, Any]:
    q = request.args.get("q")
    product_case = request.args.get("product_case")
    cert = request.args.get("cert")
    package_type = request.args.get("package_type")
    mvp_fit = request.args.get("mvp_fit")
    verification_status = request.args.get("verification_status")
    active = query_bool("active")
    limit = query_int("limit", 30, 1, 100)
    offset = query_int("offset", 0, 0)
    with db_session() as db:
        filters = []
        if q:
            like = f"%{q}%"
            filters.append((Factory.company_name.like(like)) | (Factory.product_keywords.like(like)) | (Factory.primary_category.like(like)))
        if verification_status:
            filters.append(Factory.verification_status == verification_status)
        if mvp_fit:
            filters.append(Factory.mvp_fit == mvp_fit.upper())
        if cert:
            filters.append(Factory.certification_signal.like(f"%{cert}%"))
        if package_type:
            filters.append(Factory.product_keywords.like(f"%{package_type}%"))
        if product_case and product_case in PRODUCT_CASES:
            case_terms = PRODUCT_CASES[product_case]["aliases"]
            term_filter = None
            for term in case_terms:
                clause = (Factory.primary_category.like(f"%{term}%")) | (Factory.product_keywords.like(f"%{term}%")) | (Factory.notes.like(f"%{term}%"))
                term_filter = clause if term_filter is None else term_filter | clause
            if term_filter is not None:
                filters.append(term_filter)
        if active is not None:
            filters.append(Factory.active.is_(active))
        total = db.scalar(select(func.count(Factory.id)).where(*filters))
        rows = db.scalars(select(Factory).where(*filters).order_by(Factory.mvp_fit, Factory.company_name).limit(limit).offset(offset)).all()
        return {"total": total, "items": [serialize_factory(row) for row in rows]}


@app.get("/api/admin/factory-filter-options")
def factory_filter_options() -> dict[str, Any]:
    with db_session() as db:
        statuses = [row[0] for row in db.execute(select(Factory.verification_status).distinct().order_by(Factory.verification_status)).all() if row[0]]
        mvp_fits = [row[0] for row in db.execute(select(Factory.mvp_fit).distinct().order_by(Factory.mvp_fit)).all() if row[0]]
        return {
            "product_cases": [{"value": key, "label": value["label"]} for key, value in PRODUCT_CASES.items()],
            "certifications": ["HACCP", "GMP", "ISO", "FSSC22000", "비건", "할랄"],
            "package_types": ALL_PACKAGE_TYPES,
            "verification_statuses": statuses,
            "mvp_fits": mvp_fits,
        }


@app.post("/api/admin/factories")
def create_factory() -> dict[str, Any]:
    payload = json_payload(FactoryCreate)
    with db_session() as db:
        next_id = (db.scalar(select(func.max(Factory.id))) or 0) + 1
        factory = Factory(
            factory_code=f"MANUAL-{next_id}",
            company_name=payload.company_name,
            primary_category=payload.primary_category,
            product_keywords=payload.product_keywords,
            certification_signal=payload.certification_signal,
            location_signal=payload.location_signal,
            mvp_fit=payload.mvp_fit,
            source_url=payload.source_url,
            verification_status=payload.verification_status,
            notes=payload.notes,
            oem_signal=True,
            odm_signal=True,
        )
        db.add(factory)
        db.flush()
        return serialize_factory(factory)


@app.patch("/api/admin/factories/<int:factory_id>")
def patch_factory(factory_id: int) -> dict[str, Any]:
    payload = json_payload(FactoryPatch)
    with db_session() as db:
        factory = db.get(Factory, factory_id)
        if not factory:
            api_error(404, "factory_not_found")
        update_data = payload.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            setattr(factory, key, value)
        factory.updated_at = now_utc()
        return serialize_factory(factory)


@app.get("/api/admin/rules")
def list_rules() -> dict[str, Any]:
    limit = query_int("limit", 50, 1, 100)
    offset = query_int("offset", 0, 0)
    with db_session() as db:
        rows = db.scalars(select(RegulatoryRule).order_by(RegulatoryRule.rule_id).limit(limit).offset(offset)).all()
        return {
            "items": [
                {
                    "rule_id": row.rule_id,
                    "scope": row.scope,
                    "trigger_field": row.trigger_field,
                    "trigger_value": row.trigger_value,
                    "severity": row.severity,
                    "check_item": row.check_item,
                    "required_evidence": row.required_evidence,
                    "source_url": row.source_url,
                    "active": row.active,
                }
                for row in rows
            ]
        }


@app.post("/api/product-requests/<int:request_id>/documents/<doc_type>/pdf")
def create_document_pdf(request_id: int, doc_type: str) -> dict[str, Any]:
    if doc_type not in {"product_plan", "sample_brief", "purchase_order"}:
        api_error(400, "invalid_doc_type")
    with db_session() as db:
        req = load_owned_request_or_404(db, request_id)
        generated = render_pdf(db, req, doc_type)
        return {"file_uid": generated.file_uid, "download_url": download_url_for(generated.file_uid), "checksum": generated.checksum}


@app.get("/api/files/<file_uid>/download")
def download_file(file_uid: str):
    with db_session() as db:
        generated = load_owned_generated_file_or_404(db, file_uid)
        return send_file(generated.storage_path, mimetype="application/pdf", as_attachment=True, download_name=Path(generated.storage_path).name)


@app.get("/api/export/product-requests/<int:request_id>.json")
def export_request_json(request_id: int) -> Response:
    with db_session() as db:
        req = load_owned_request_or_404(db, request_id)
        body = json.dumps(request_detail(db, req, include_private=True), ensure_ascii=False, indent=2)
        return Response(body.encode("utf-8"), mimetype="application/json; charset=utf-8")
