from __future__ import annotations

import argparse
import html
import json
import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from urllib.parse import quote, unquote, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

BASE_URL = "https://horaeng.com"
POSTS_API = f"{BASE_URL}/wp-json/wp/v2/posts"
DEFAULT_OUTPUT_DIR = Path("data") / "raw"
START_YEAR = 2020

EXAM_TITLE_RE = re.compile(r"(?P<year>20\d{2})년\s*(?P<month>\d{1,2})월\s*(?P<grade>고\s*[123])")

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


@dataclass(frozen=True)
class ExamPost:
    year: int
    month: int
    grade: str
    title: str
    link: str
    content: str


def fetch_posts_page(page: int, per_page: int = 100) -> tuple[list[dict], int]:
    query = {"per_page": per_page, "page": page, "orderby": "date", "order": "desc"}
    url = f"{POSTS_API}?{urlencode(query)}"
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))
        total_pages = int(response.headers.get("X-WP-TotalPages", "1"))
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected posts payload: {type(data)!r}")
    return [post for post in data if isinstance(post, dict)], total_pages


def fetch_all_posts() -> list[dict]:
    posts: list[dict] = []
    page = 1
    total_pages = 1
    while page <= total_pages:
        batch, total_pages = fetch_posts_page(page)
        if not batch:
            break
        posts.extend(batch)
        page += 1
    return posts


# ==============================================================================
# HTML 파서 헬퍼 함수
# ==============================================================================

def _clean_text(html_snippet: str) -> str:
    """HTML 태그를 제거하고 공백을 정규화합니다."""
    text = re.sub(r"<[^>]+>", " ", html_snippet)
    return " ".join(text.split()).strip()


def _extract_links_from_snippet(snippet: str) -> List[Tuple[str, str]]:
    """HTML 스니펫 내 모든 <a> 태그에서 (href, anchor_text)를 추출합니다."""
    links = []
    pattern = r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>'
    for match in re.findall(pattern, snippet, flags=re.S | re.I):
        href, raw_anchor = match
        anchor = _clean_text(raw_anchor)
        links.append((href, anchor))
    return links


def _classify_link_kind(href: str, anchor_text: str) -> Optional[str]:
    """href 및 anchor text를 분석하여 '문제' 또는 '정답' 유무를 판별합니다."""
    combined = f"{href.lower()} {anchor_text.lower()}"

    answer_keywords = ("정답", "해설", "답지", "해설지", "answer", "solution", "key")
    problem_keywords = ("문제", "문제지", "시험지", "problem", "question", "paper")

    if any(k in combined for k in answer_keywords):
        return "정답"
    if any(k in combined for k in problem_keywords):
        return "문제"
    return None


def _is_complete(res: Dict[str, str]) -> bool:
    return "문제" in res and "정답" in res


def _parse_from_tables(html_text: str) -> Dict[str, str]:
    """HTML 표(<table>) 행(<tr>)을 스캔하여 영어 행의 PDF 링크를 추출합니다."""
    found: Dict[str, str] = {}
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html_text, flags=re.S | re.I)

    for row in rows:
        row_text = _clean_text(row)
        is_english_row = ("영어" in row_text or "english" in row_text.lower())
        if not is_english_row:
            continue

        cells = re.findall(r"<(?:td|th)[^>]*>(.*?)</(?:td|th)>", row, flags=re.S | re.I)
        for cell in cells:
            links = _extract_links_from_snippet(cell)
            for href, anchor in links:
                kind = _classify_link_kind(href, anchor)
                if kind and kind not in found:
                    found[kind] = href

        if not _is_complete(found):
            for href, anchor in _extract_links_from_snippet(row):
                kind = _classify_link_kind(href, anchor)
                if kind and kind not in found:
                    found[kind] = href

        if _is_complete(found):
            break

    return found


def _parse_from_blocks(html_text: str) -> Dict[str, str]:
    """<p>, <li>, <div> 블록 단락 기반으로 영어 PDF 링크를 추출합니다."""
    found: Dict[str, str] = {}
    blocks = re.findall(r"<(p|li|div)[^>]*>(.*?)</\1>", html_text, flags=re.S | re.I)

    for _, block_content in blocks:
        block_text = _clean_text(block_content)
        if "영어" not in block_text and "english" not in block_text.lower():
            continue

        links = _extract_links_from_snippet(block_content)
        for href, anchor in links:
            kind = _classify_link_kind(href, anchor)
            if kind and kind not in found:
                found[kind] = href

        if _is_complete(found):
            break

    return found


def _parse_from_direct_links(html_text: str) -> Dict[str, str]:
    """링크 주소 또는 앵커 텍스트 자체에 '영어' 키워드가 포함된 경우 추출합니다."""
    found: Dict[str, str] = {}
    links = _extract_links_from_snippet(html_text)

    for href, anchor in links:
        combined = f"{href.lower()} {anchor.lower()}"
        if "영어" in combined or "eng" in combined:
            kind = _classify_link_kind(href, anchor)
            if kind and kind not in found:
                found[kind] = href

    return found


def parse_english_pdfs_from_html(html_content: str) -> Dict[str, str]:
    """3단계 추적 방식을 통해 HTML 본문에서 영어 문제/정답 PDF URL을 추출합니다."""
    if not html_content:
        return {}

    decoded_html = html.unescape(html_content)

    # 1단계: Table Row 검사
    result = _parse_from_tables(decoded_html)
    if _is_complete(result):
        return result

    # 2단계: Block (p, li, div) 검사
    block_result = _parse_from_blocks(decoded_html)
    result.update({k: v for k, v in block_result.items() if k not in result})
    if _is_complete(result):
        return result

    # 3단계: 직접 앵커/URL 키워드 매칭
    fallback_result = _parse_from_direct_links(decoded_html)
    result.update({k: v for k, v in fallback_result.items() if k not in result})

    return result


# ==============================================================================
# 게시글 수집 및 파일 다운로드 로직
# ==============================================================================

def parse_exam_post(post: dict) -> ExamPost | None:
    title = html.unescape(str(post.get("title", {}).get("rendered", ""))).strip()
    match = EXAM_TITLE_RE.search(title)
    if not match:
        return None

    year = int(match.group("year"))
    if year < START_YEAR:
        return None

    month = int(match.group("month"))
    grade = match.group("grade").replace(" ", "")
    content = str(post.get("content", {}).get("rendered", ""))
    link = str(post.get("link", ""))
    return ExamPost(year=year, month=month, grade=grade, title=title, link=link, content=content)


def collect_exam_posts(start_year: int, end_year: int) -> list[ExamPost]:
    exams = []
    for post in fetch_all_posts():
        exam = parse_exam_post(post)
        if exam is None:
            continue
        if start_year <= exam.year <= end_year:
            exams.append(exam)
    exams.sort(key=lambda item: (item.year, item.month, int(item.grade[-1])))
    return exams


def build_filename(year: int, month: int, grade: str, kind: str) -> str:
    return f"{year}년_{month}월_{grade}_수능영어_{kind}.pdf"


def download_file(url: str, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)

    parsed = urlparse(url)
    # 이중 인코딩 방지를 위해 unquote 후 quote 적용
    safe_path = quote(unquote(parsed.path))
    safe_query = quote(unquote(parsed.query), safe="=&")

    encoded_url = urlunparse(
        parsed._replace(path=safe_path, query=safe_query)
    )
    request = Request(encoded_url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=60) as response:
        target.write_bytes(response.read())

    return target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download horaeng.com English PDF files.")
    parser.add_argument(
        "--start-year",
        type=int,
        default=START_YEAR,
        help=f"Start year to download from (default: {START_YEAR})",
    )
    parser.add_argument(
        "--end-year",
        type=int,
        default=None,
        help="End year to download to (default: current year)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory to save PDFs (default: {DEFAULT_OUTPUT_DIR})",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    end_year = args.end_year or date.today().year

    logging.info(f"Fetching exam posts from API ({args.start_year}~{end_year})...")
    exams = collect_exam_posts(args.start_year, end_year)
    if not exams:
        logging.error("No matching exam posts found.")
        return

    logging.info(f"Found {len(exams)} exam posts from {args.start_year} to {end_year}.")
    downloaded = 0

    for exam in exams:
        pdf_map = parse_english_pdfs_from_html(exam.content)
        problem_url = pdf_map.get("문제")
        answer_url = pdf_map.get("정답")

        if not problem_url or not answer_url:
            p_status = "O" if problem_url else "X"
            a_status = "O" if answer_url else "X"
            logging.warning(
                f"Skipping '{exam.title}': "
                f"Missing PDF link (Problem: {p_status}, Answer: {a_status})"
            )
            continue

        problem_target = args.output_dir / build_filename(exam.year, exam.month, exam.grade, "문제")
        answer_target = args.output_dir / build_filename(exam.year, exam.month, exam.grade, "정답")

        # 문제 PDF 중복 체크 및 다운로드
        if problem_target.exists():
            logging.info(f"Skipping (already exists): {problem_target.name}")
        else:
            try:
                download_file(problem_url, problem_target)
                downloaded += 1
                logging.info(f"Downloaded: {problem_target.name}")
            except Exception as err:
                logging.error(f"Failed to download problem file for '{exam.title}': {err}")

        # 정답 PDF 중복 체크 및 다운로드
        if answer_target.exists():
            logging.info(f"Skipping (already exists): {answer_target.name}")
        else:
            try:
                download_file(answer_url, answer_target)
                downloaded += 1
                logging.info(f"Downloaded: {answer_target.name}")
            except Exception as err:
                logging.error(f"Failed to download answer file for '{exam.title}': {err}")

    logging.info(f"Completed! Total new files downloaded: {downloaded}")


if __name__ == "__main__":
    main()
