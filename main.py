import os
import re
import json
import time
import hashlib
import logging
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
import schedule
import gspread
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


# =========================
# 기본 설정
# =========================

load_dotenv()

BASE_URL = "https://newsac.kosac.re.kr"

LIST_URL = os.getenv(
    "NEWSAC_LIST_URL",
    "https://newsac.kosac.re.kr/public/program/list?page=1&size=10&programTypeCode=C0101&programRegionCode=C0504&operationStatusCode=C1102&schoolLevelCode=G007",
)

GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "service_account.json")
GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT", "")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
GOOGLE_WORKSHEET_NAME = os.getenv("GOOGLE_WORKSHEET_NAME", "newsac_programs")

# 텔레그램 설정으로 변경
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

RUN_INTERVAL_MINUTES = int(os.getenv("RUN_INTERVAL_MINUTES", "0"))
USE_PLAYWRIGHT_FALLBACK = os.getenv("USE_PLAYWRIGHT_FALLBACK", "true").lower() == "true"

MAX_PAGES = int(os.getenv("MAX_PAGES", "50"))

REQUEST_TIMEOUT = 15
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 2

LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "crawler.log")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}


# =========================
# 로깅 설정
# =========================

def setup_logging() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


# =========================
# 공통 유틸
# =========================

def retry_request(url: str, method: str = "GET", **kwargs) -> requests.Response:
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.request(
                method=method,
                url=url,
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT,
                **kwargs,
            )
            response.raise_for_status()
            return response

        except requests.RequestException as exc:
            last_error = exc
            wait_seconds = BACKOFF_BASE_SECONDS ** attempt

            logging.warning(
                "요청 실패: attempt=%s/%s, url=%s, wait=%ss, error=%s",
                attempt,
                MAX_RETRIES,
                sanitize_url_for_log(url),
                wait_seconds,
                str(exc),
            )

            time.sleep(wait_seconds)

    raise RuntimeError(f"HTTP 요청 최종 실패: {sanitize_url_for_log(url)} / {last_error}")


def sanitize_url_for_log(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def normalize_text(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def extract_program_id(url: str) -> str:
    match = re.search(r"/public/program/list/(\d+)", url)
    if match:
        return match.group(1)
    return ""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_absolute_url(href: str) -> str:
    return urljoin(BASE_URL, href)


def build_list_url(page_number: int) -> str:
    if re.search(r"([?&])page=\d+", LIST_URL):
        return re.sub(r"([?&])page=\d+", rf"\g<1>page={page_number}", LIST_URL)

    separator = "&" if "?" in LIST_URL else "?"
    return f"{LIST_URL}{separator}page={page_number}"


# =========================
# 목록 페이지 수집
# =========================

def fetch_list_html_by_requests() -> str:
    response = retry_request(LIST_URL)
    return response.text


def parse_list_from_html(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results = []

    tables = soup.select("table")

    for table in tables:
        rows = table.select("tbody tr")

        for row in rows:
            cells = [normalize_text(td.get_text(" ", strip=True)) for td in row.select("td")]
            link_tag = row.select_one("a[href*='/public/program/list/']")

            if not link_tag:
                continue

            detail_url = make_absolute_url(link_tag.get("href", ""))
            program_id = extract_program_id(detail_url)
            title = normalize_text(link_tag.get_text(" ", strip=True))

            education_target = ""
            if len(cells) >= 8:
                education_target = cells[7]

            if program_id and title:
                results.append(
                    {
                        "program_id": program_id,
                        "title": title,
                        "education_target": education_target,
                        "detail_url": detail_url,
                        "source": "requests_table",
                    }
                )

    return deduplicate_programs(results)


def fetch_list_by_playwright() -> list[dict]:
    if sync_playwright is None:
        raise RuntimeError(
            "playwright가 설치되어 있지 않습니다. "
            "python -m pip install playwright 후 python -m playwright install chromium 실행 필요."
        )

    logging.info("Playwright Headless 방식으로 목록 페이지 렌더링 시작")

    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        page = browser.new_page(
            user_agent=HEADERS["User-Agent"],
            locale="ko-KR",
            viewport={"width": 1440, "height": 1000},
        )

        for page_number in range(1, MAX_PAGES + 1):
            list_url = build_list_url(page_number)

            logging.info("목록 페이지 접속: page=%s, url=%s", page_number, list_url)

            try:
                page.goto(list_url, wait_until="networkidle", timeout=30000)
                page.wait_for_timeout(5000)

                row_count = page.locator("table tbody tr").count()
                logging.info("목록 table tbody tr 개수: page=%s, rows=%s", page_number, row_count)

                if row_count == 0:
                    logging.info("page=%s에서 목록 행이 없어 페이지 순회를 종료합니다.", page_number)
                    break

                for i in range(row_count):
                    try:
                        rows = page.locator("table tbody tr")
                        row = rows.nth(i)

                        cells = row.locator("td")
                        cell_count = cells.count()

                        if cell_count == 0:
                            continue

                        cell_texts = []
                        for c in range(cell_count):
                            cell_texts.append(normalize_text(cells.nth(c).inner_text()))

                        title = ""
                        list_education_target = ""

                        if cell_count >= 8:
                            title = normalize_text(cells.nth(3).inner_text())
                            list_education_target = normalize_text(cells.nth(7).inner_text())
                        else:
                            row_text = normalize_text(row.inner_text())
                            title_link = row.locator("a").first

                            if title_link.count() > 0:
                                title = normalize_text(title_link.inner_text())

                            edu_keywords = [
                                "일반형",
                                "사회적 배려형(도서벽지)",
                                "사회적 배려형(다문화)",
                                "사회적 배려형(특수교육)",
                            ]
                            found = [x for x in edu_keywords if x in row_text]
                            list_education_target = ",".join(found)

                        if not title:
                            logging.warning(
                                "제목을 찾지 못해 건너뜀: page=%s, row=%s, cells=%s",
                                page_number,
                                i + 1,
                                cell_texts,
                            )
                            continue

                        logging.info(
                            "목록 항목 클릭 시도: page=%s, row=%s, title=%s",
                            page_number,
                            i + 1,
                            title,
                        )

                        title_link = row.locator("a").first

                        if title_link.count() == 0:
                            logging.warning("제목 링크를 찾지 못함: title=%s", title)
                            continue

                        title_link.click()

                        page.wait_for_url("**/public/program/list/*", timeout=10000)
                        page.wait_for_load_state("networkidle", timeout=30000)
                        page.wait_for_timeout(1500)

                        detail_url = page.url
                        program_id = extract_program_id(detail_url)

                        if not program_id:
                            logging.warning(
                                "상세 URL에서 program_id 추출 실패: title=%s, url=%s",
                                title,
                                detail_url,
                            )
                        else:
                            results.append(
                                {
                                    "program_id": program_id,
                                    "title": title,
                                    "education_target": list_education_target,
                                    "detail_url": detail_url,
                                    "source": f"playwright_click_page_{page_number}",
                                }
                            )

                            logging.info(
                                "목록 항목 추출 성공: page=%s, program_id=%s, title=%s, education_target=%s",
                                page_number,
                                program_id,
                                title,
                                list_education_target,
                            )

                        page.goto(list_url, wait_until="networkidle", timeout=30000)
                        page.wait_for_timeout(3000)

                    except Exception as exc:
                        logging.exception(
                            "목록 행 처리 실패: page=%s, row=%s, error=%s",
                            page_number,
                            i + 1,
                            str(exc),
                        )

                        try:
                            page.goto(list_url, wait_until="networkidle", timeout=30000)
                            page.wait_for_timeout(3000)
                        except Exception:
                            pass

            except Exception as exc:
                logging.exception("목록 페이지 처리 실패: page=%s, error=%s", page_number, str(exc))
                break

        browser.close()

    return deduplicate_programs(results)


def deduplicate_programs(programs: list[dict]) -> list[dict]:
    seen = set()
    unique = []

    for program in programs:
        key = program.get("program_id") or program.get("detail_url")
        if not key or key in seen:
            continue

        seen.add(key)
        unique.append(program)

    return unique


def collect_program_list() -> list[dict]:
    logging.info("목록 페이지 수집 시작")

    try:
        html = fetch_list_html_by_requests()
        programs = parse_list_from_html(html)

        if programs:
            logging.info("requests 방식 목록 수집 성공: %s건", len(programs))
            return programs

        logging.warning("requests 방식에서 실제 목록 행을 찾지 못함")

    except Exception as exc:
        logging.warning("requests 목록 수집 실패: %s", str(exc))

    if USE_PLAYWRIGHT_FALLBACK:
        programs = fetch_list_by_playwright()
        logging.info("Playwright 방식 목록 수집 완료: %s건", len(programs))
        return programs

    return []


# =========================
# 상세 페이지 수집
# =========================

def extract_detail_fields_from_text(body_text: str) -> dict:
    text = normalize_text(body_text)

    result = {
        "application_period": "",
        "education_period": "",
        "education_target": "",
        "program_level": "",
        "program_literacy": "",
        "total_lessons": "",
        "education_place": "",
        "operation_region": "",
        "application_target": "",
        "recruitment_classes": "",
        "contact": "",
        "organization_info": "",
    }

    label_defs = [
        ("application_period", r"신청\s*기간"),
        ("education_period", r"교육\s*기간"),
        ("education_target", r"교육\s*대상"),
        ("program_level", r"프로그램\s*수준"),
        ("program_literacy", r"프로그램\s*소양"),
        ("total_lessons", r"총\s*교육\s*차시"),
        ("education_place", r"교육\s*장소"),
        ("operation_region", r"운영\s*권역"),
        ("application_target", r"신청\s*대상"),
        ("recruitment_classes", r"모집\s*학급"),
    ]

    found_labels = []

    for field, pattern in label_defs:
        match = re.search(pattern, text)
        if match:
            found_labels.append(
                {
                    "field": field,
                    "start": match.start(),
                    "end": match.end(),
                }
            )

    found_labels.sort(key=lambda x: x["start"])

    for i, current in enumerate(found_labels):
        value_start = current["end"]

        if i + 1 < len(found_labels):
            value_end = found_labels[i + 1]["start"]
        else:
            value_end = len(text)

        value = normalize_text(text[value_start:value_end])
        value = re.sub(r"^[\s\|:：\-]+", "", value).strip()

        stop_words = [
            "신청하기",
            "목록",
            "프로그램 소개",
            "커리큘럼",
            "프로그램 교안 첨부파일",
            "안내사항",
            "문의처",
            "기관 정보",
            "개인정보처리방침",
            "Copyright",
        ]

        for stop in stop_words:
            stop_index = value.find(stop)
            if stop_index >= 0:
                value = normalize_text(value[:stop_index])

        result[current["field"]] = value

    contact_idx = text.rfind("문의처")
    org_idx = text.rfind("기관 정보")

    if contact_idx >= 0:
        if org_idx > contact_idx:
            contact_text = text[contact_idx + len("문의처"):org_idx]
        else:
            contact_text = text[contact_idx + len("문의처"):]

        contact_text = normalize_text(contact_text)
        contact_text = re.sub(r"^[\s\|:：\-]+", "", contact_text).strip()

        contact_stop_words = [
            "기관 정보",
            "개인정보처리방침",
            "Copyright",
        ]

        for stop in contact_stop_words:
            stop_index = contact_text.find(stop)
            if stop_index >= 0:
                contact_text = normalize_text(contact_text[:stop_index])

        result["contact"] = contact_text

    if org_idx >= 0:
        org_text = text[org_idx + len("기관 정보"):]

        org_text = normalize_text(org_text)
        org_text = re.sub(r"^[\s\|:：\-]+", "", org_text).strip()

        org_stop_words = [
            "+ 프로그램 더보기",
            "프로그램 더보기",
            "개인정보처리방침",
            "Copyright",
            "(06130)",
        ]

        for stop in org_stop_words:
            stop_index = org_text.find(stop)
            if stop_index >= 0:
                org_text = normalize_text(org_text[:stop_index])

        org_text = org_text.replace("+", "").strip()

        result["organization_info"] = org_text

    return result


def fetch_detail_by_requests(detail_url: str) -> dict:
    html = retry_request(detail_url).text
    return parse_detail_html(html, detail_url)


def parse_detail_html(html: str, detail_url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    body_text = normalize_text(soup.get_text(" ", strip=True))

    detail = extract_detail_fields_from_text(body_text)
    detail["detail_url"] = detail_url
    detail["body_text"] = body_text

    return detail


def fetch_detail_by_playwright(detail_url: str) -> dict:
    if sync_playwright is None:
        raise RuntimeError("playwright가 설치되어 있지 않습니다.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        page = browser.new_page(
            user_agent=HEADERS["User-Agent"],
            locale="ko-KR",
            viewport={"width": 1440, "height": 1600},
        )

        page.goto(detail_url, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(3000)

        body_text = normalize_text(page.locator("body").inner_text())

        browser.close()

    detail = extract_detail_fields_from_text(body_text)
    detail["detail_url"] = detail_url
    detail["body_text"] = body_text

    return detail


def is_detail_enough(detail: dict) -> bool:
    important_fields = [
        "application_period",
        "education_period",
        "education_target",
        "program_level",
        "program_literacy",
        "total_lessons",
        "education_place",
        "operation_region",
        "application_target",
        "recruitment_classes",
    ]

    filled_count = sum(1 for field in important_fields if detail.get(field))
    return filled_count >= 5


def collect_detail(detail_url: str) -> dict:
    try:
        detail = fetch_detail_by_playwright(detail_url)

        if is_detail_enough(detail):
            return detail

        logging.warning(
            "상세 페이지 Playwright 추출 결과가 부족함. requests 방식 재시도: %s",
            sanitize_url_for_log(detail_url),
        )

    except Exception as exc:
        logging.warning(
            "상세 페이지 Playwright 수집 실패: %s / %s",
            sanitize_url_for_log(detail_url),
            str(exc),
        )

    try:
        detail = fetch_detail_by_requests(detail_url)
        return detail

    except Exception as exc:
        logging.warning(
            "상세 페이지 requests 수집 실패: %s / %s",
            sanitize_url_for_log(detail_url),
            str(exc),
        )

    return {
        "detail_url": detail_url,
        "body_text": "",
        "application_period": "",
        "education_period": "",
        "education_target": "",
        "program_level": "",
        "program_literacy": "",
        "total_lessons": "",
        "education_place": "",
        "operation_region": "",
        "application_target": "",
        "recruitment_classes": "",
        "contact": "",
        "organization_info": "",
    }


# =========================
# Google Sheets
# =========================

SHEET_HEADERS = [
    "program_id",
    "title",
    "detail_url",
    "application_period",
    "education_period",
    "education_target",
    "program_level",
    "program_literacy",
    "total_lessons",
    "education_place",
    "operation_region",
    "application_target",
    "recruitment_classes",
    "contact",
    "organization_info",
    "body_hash",
    "first_seen_at",
    "last_checked_at",
    "last_changed_at",
    "source",
]


def get_worksheet():
    if not GOOGLE_SHEET_ID:
        raise RuntimeError("GOOGLE_SHEET_ID가 .env에 설정되어 있지 않습니다.")

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    if GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT:
        service_account_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT)
        credentials = Credentials.from_service_account_info(
            service_account_info,
            scopes=scopes,
        )
    else:
        credentials = Credentials.from_service_account_file(
            GOOGLE_SERVICE_ACCOUNT_JSON,
            scopes=scopes,
        )

    client = gspread.authorize(credentials)
    spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)

    try:
        worksheet = spreadsheet.worksheet(GOOGLE_WORKSHEET_NAME)

    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=GOOGLE_WORKSHEET_NAME,
            rows=1000,
            cols=len(SHEET_HEADERS),
        )

    ensure_headers(worksheet)
    return worksheet


def ensure_headers(worksheet) -> None:
    existing = worksheet.row_values(1)

    if existing != SHEET_HEADERS:
        worksheet.update(
            values=[SHEET_HEADERS],
            range_name="A1",
        )
        logging.info("Google Sheets 헤더 설정 완료")


def get_existing_rows(worksheet) -> dict:
    records = worksheet.get_all_records()
    existing = {}

    for index, record in enumerate(records, start=2):
        program_id = str(record.get("program_id", "")).strip()

        if program_id:
            existing[program_id] = {
                "row_number": index,
                "record": record,
            }

    return existing


def column_letter(n: int) -> str:
    result = ""

    while n:
        n, remainder = divmod(n - 1, 26)
        result = chr(65 + remainder) + result

    return result


def make_body_hash(program: dict) -> str:
    hash_source = {
        "title": program.get("title", ""),
        "detail_url": program.get("detail_url", ""),
        "application_period": program.get("application_period", ""),
        "education_period": program.get("education_period", ""),
        "education_target": program.get("education_target", ""),
        "program_level": program.get("program_level", ""),
        "program_literacy": program.get("program_literacy", ""),
        "total_lessons": program.get("total_lessons", ""),
        "education_place": program.get("education_place", ""),
        "operation_region": program.get("operation_region", ""),
        "application_target": program.get("application_target", ""),
        "recruitment_classes": program.get("recruitment_classes", ""),
        "contact": program.get("contact", ""),
        "organization_info": program.get("organization_info", ""),
    }

    return sha256_text(json.dumps(hash_source, ensure_ascii=False, sort_keys=True))


def build_row_data(program: dict, body_hash: str, now: str) -> dict:
    return {
        "program_id": program.get("program_id", ""),
        "title": program.get("title", ""),
        "detail_url": program.get("detail_url", ""),
        "application_period": program.get("application_period", ""),
        "education_period": program.get("education_period", ""),
        "education_target": program.get("education_target", ""),
        "program_level": program.get("program_level", ""),
        "program_literacy": program.get("program_literacy", ""),
        "total_lessons": program.get("total_lessons", ""),
        "education_place": program.get("education_place", ""),
        "operation_region": program.get("operation_region", ""),
        "application_target": program.get("application_target", ""),
        "recruitment_classes": program.get("recruitment_classes", ""),
        "contact": program.get("contact", ""),
        "organization_info": program.get("organization_info", ""),
        "body_hash": body_hash,
        "first_seen_at": now,
        "last_checked_at": now,
        "last_changed_at": now,
        "source": program.get("source", ""),
    }


def upsert_programs_to_sheet(programs: list[dict]) -> dict:
    worksheet = get_worksheet()
    existing = get_existing_rows(worksheet)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    new_items = []
    changed_items = []
    unchanged_items = []

    rows_to_append = []
    rows_to_update = []

    for program in programs:
        program_id = program["program_id"]
        body_hash = make_body_hash(program)

        row_data_dict = build_row_data(program, body_hash, now)

        if program_id not in existing:
            rows_to_append.append([row_data_dict[h] for h in SHEET_HEADERS])
            new_items.append(program)
            continue

        old = existing[program_id]["record"]
        row_number = existing[program_id]["row_number"]
        old_hash = str(old.get("body_hash", "")).strip()

        row_data_dict["first_seen_at"] = old.get("first_seen_at", now)

        if old_hash != body_hash:
            row_data_dict["last_changed_at"] = now
            changed_items.append(program)
        else:
            row_data_dict["last_changed_at"] = old.get("last_changed_at", "")
            unchanged_items.append(program)

        row_values = [row_data_dict[h] for h in SHEET_HEADERS]
        rows_to_update.append((row_number, row_values))

    if rows_to_append:
        worksheet.append_rows(rows_to_append, value_input_option="USER_ENTERED")
        logging.info("신규 행 추가: %s건", len(rows_to_append))

    for row_number, row_values in rows_to_update:
        range_name = f"A{row_number}:{column_letter(len(SHEET_HEADERS))}{row_number}"

        worksheet.update(
            values=[row_values],
            range_name=range_name,
            value_input_option="USER_ENTERED",
        )

    logging.info(
        "시트 반영 완료: 신규=%s, 변경=%s, 동일=%s",
        len(new_items),
        len(changed_items),
        len(unchanged_items),
    )

    return {
        "new_items": new_items,
        "changed_items": changed_items,
        "unchanged_items": unchanged_items,
    }


# =========================
# 텔레그램 알림 시스템으로 교체
# =========================

def send_telegram_message(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("텔레그램 토큰 또는 채팅 ID가 설정되지 않아 알림을 건너뜁니다.")
        return

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        response = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        logging.info("Telegram 알림 발송 완료")

    except requests.RequestException as exc:
        logging.warning("Telegram 알림 발송 실패: %s", str(exc))


def notify_changes(result: dict) -> None:
    new_items = result.get("new_items", [])
    changed_items = result.get("changed_items", [])

    for item in new_items:
        message = (
            "[디지털새싹 새 프로그램 발견]\n\n"
            f"📌 제목: {item.get('title', '')}\n"
            f"📅 신청 기간: {item.get('application_period', '')}\n"
            f"🏫 교육 기간: {item.get('education_period', '')}\n"
            f"👤 교육 대상: {item.get('education_target', '')}\n"
            f"🔗 상세 URL: {item.get('detail_url', '')}"
        )
        send_telegram_message(message)

    for item in changed_items:
        message = (
            "[디지털새싹 프로그램 변경 감지]\n\n"
            f"📌 제목: {item.get('title', '')}\n"
            f"📅 신청 기간: {item.get('application_period', '')}\n"
            f"🏫 교육 기간: {item.get('education_period', '')}\n"
            f"👤 교육 대상: {item.get('education_target', '')}\n"
            f"🔗 상세 URL: {item.get('detail_url', '')}"
        )
        send_telegram_message(message)


# =========================
# 메인 작업
# =========================

def run_once() -> None:
    logging.info("===== 디지털새싹 프로그램 크롤링 시작 =====")

    try:
        list_items = collect_program_list()

        if not list_items:
            logging.warning("목록에서 프로그램을 찾지 못했습니다.")
            return

        logging.info("목록 수집 결과: %s건", len(list_items))

        enriched_programs = []

        for index, item in enumerate(list_items, start=1):
            logging.info(
                "상세 수집 중: %s/%s, program_id=%s, title=%s",
                index,
                len(list_items),
                item.get("program_id"),
                item.get("title"),
            )

            try:
                detail = collect_detail(item["detail_url"])
                merged = {**item, **detail}

                if not merged.get("education_target"):
                    merged["education_target"] = item.get("education_target", "")

                enriched_programs.append(merged)

                time.sleep(1)

            except Exception as exc:
                logging.exception(
                    "상세 수집 실패: program_id=%s, url=%s, error=%s",
                    item.get("program_id"),
                    sanitize_url_for_log(item.get("detail_url", "")),
                    str(exc),
                )

        if not enriched_programs:
            logging.warning("상세 수집 성공 데이터가 없습니다.")
            return

        result = upsert_programs_to_sheet(enriched_programs)
        notify_changes(result)

        logging.info(
            "실행 결과 요약: 전체=%s, 신규=%s, 변경=%s, 동일=%s",
            len(enriched_programs),
            len(result["new_items"]),
            len(result["changed_items"]),
            len(result["unchanged_items"]),
        )

    except Exception as exc:
        logging.exception("크롤러 전체 실행 실패: %s", str(exc))

    finally:
        logging.info("===== 디지털새싹 프로그램 크롤링 종료 =====")


def main() -> None:
    setup_logging()

    if RUN_INTERVAL_MINUTES <= 0:
        run_once()
        return

    logging.info("스케줄 실행 모드: %s분마다 실행", RUN_INTERVAL_MINUTES)
    run_once()

    schedule.every(RUN_INTERVAL_MINUTES).minutes.do(run_once)

    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()