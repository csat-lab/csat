
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import tempfile
import time

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup


# ============================================================
# Configuration
# ============================================================

BASE_URL = "https://horaeng.com"
POSTS_API = f"{BASE_URL}/wp-json/wp/v2/posts"

DEFAULT_OUTPUT_DIR = Path("data/raw")
DEFAULT_START_YEAR = 2020

USER_AGENT = "horaeng-exam-downloader/1.0"

EXAM_TITLE_RE = re.compile(
    r"(?P<year>20\d{2})년\s*"
    r"(?P<month>1[0-2]|0?[1-9])월\s*"
    r"(?P<grade>고\s*[123])"
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)

logger = logging.getLogger(__name__)


# ============================================================
# Data Models
# ============================================================

@dataclass(frozen=True)
class ExamPost:
    post_id: int
    year: int
    month: int
    grade: str
    title: str
    link: str
    content: str


@dataclass(frozen=True)
class PdfLinks:
    problem: str | None
    answer: str | None


# ============================================================
# HTTP Client
# ============================================================

def http_get(
    url: str,
    *,
    timeout: int = 30,
    retries: int = 3,
) -> tuple[bytes, dict[str, str]]:
    """GET request with bounded retries."""

    last_error: Exception | None = None

    for attempt in range(retries + 1):

        request = Request(
            url,
            headers={"User-Agent": USER_AGENT},
        )

        try:
            with urlopen(request, timeout=timeout) as response:

                return (
                    response.read(),
                    dict(response.headers.items()),
                )

        except HTTPError as exc:

            last_error = exc

            # Permanent client errors are not retried.
            if 400 <= exc.code < 500 and exc.code != 429:
                raise

        except (URLError, TimeoutError, OSError) as exc:

            last_error = exc

        if attempt < retries:

            delay = min(2 ** attempt, 8)

            logger.warning(
                "Request failed (%s). Retrying in %ss.",
                last_error,
                delay,
            )

            time.sleep(delay)

    raise RuntimeError(
        f"Request failed after {retries + 1} attempts: {url}"
    ) from last_error


# ============================================================
# WordPress REST API
# ============================================================

def fetch_posts_page(
    page: int,
    per_page: int = 100,
) -> tuple[list[dict[str, Any]], int]:

    from urllib.parse import urlencode

    query = urlencode({
        "per_page": per_page,
        "page": page,
        "orderby": "date",
        "order": "desc",
        "status": "publish",
    })

    payload, headers = http_get(
        f"{POSTS_API}?{query}"
    )

    try:
        data = json.loads(payload.decode("utf-8"))

    except (UnicodeDecodeError, json.JSONDecodeError) as exc:

        raise RuntimeError(
            f"Invalid WordPress API response on page {page}"
        ) from exc

    if not isinstance(data, list):

        raise RuntimeError(
            f"Unexpected WordPress payload: {type(data).__name__}"
        )

    total_pages = int(
        headers.get(
            "X-WP-TotalPages",
            headers.get("x-wp-totalpages", "1"),
        )
    )

    return (
        [item for item in data if isinstance(item, dict)],
        total_pages,
    )


def fetch_all_posts() -> list[dict[str, Any]]:

    posts: list[dict[str, Any]] = []

    page = 1
    total_pages = 1

    while page <= total_pages:

        batch, total_pages = fetch_posts_page(page)

        if not batch:
            break

        posts.extend(batch)

        logger.info(
            "Fetched WordPress page %s/%s (%s posts)",
            page,
            total_pages,
            len(batch),
        )

        page += 1

    return posts


# ============================================================
# Exam Post Parser
# ============================================================

def parse_exam_post(
    post: dict[str, Any],
) -> ExamPost | None:

    title_obj = post.get("title") or {}
    content_obj = post.get("content") or {}

    title = html.unescape(
        str(title_obj.get("rendered", ""))
    ).strip()

    match = EXAM_TITLE_RE.search(title)

    if not match:
        return None

    post_id = post.get("id")

    if not isinstance(post_id, int):
        return None

    year = int(match.group("year"))
    month = int(match.group("month"))

    grade = re.sub(
        r"\s+",
        "",
        match.group("grade"),
    )

    return ExamPost(
        post_id=post_id,
        year=year,
        month=month,
        grade=grade,
        title=title,
        link=str(post.get("link", "")),
        content=str(content_obj.get("rendered", "")),
    )


def collect_exam_posts(
    start_year: int,
    end_year: int,
) -> list[ExamPost]:

    if start_year < 2000 or end_year < start_year:

        raise ValueError(
            "연도 범위가 올바르지 않습니다."
        )

    exams_by_id: dict[int, ExamPost] = {}

    for post in fetch_all_posts():

        exam = parse_exam_post(post)

        if exam and start_year <= exam.year <= end_year:

            exams_by_id[exam.post_id] = exam

    exams = list(exams_by_id.values())

    exams.sort(
        key=lambda item: (
            item.year,
            item.month,
            int(item.grade[-1]),
            item.post_id,
        )
    )

    return exams


# ============================================================
# HTML Link Classification
# ============================================================

def classify_link(
    href: str,
    anchor_text: str,
) -> str | None:
    """Classify problem and answer PDF links conservatively."""

    combined = f"{href} {anchor_text}".lower()

    answer_ko = (
        "정답",
        "해설",
        "답지",
        "해설지",
    )

    problem_ko = (
        "문제지",
        "시험지",
        "문제",
    )

    has_answer = any(
        token in combined
        for token in answer_ko
    )

    has_problem = any(
        token in combined
        for token in problem_ko
    )

    # English keywords: whole-word matching.
    has_answer = has_answer or bool(
        re.search(
            r"\b(answer|solution|answers|solutions)\b",
            combined,
        )
    )

    has_problem = has_problem or bool(
        re.search(
            r"\b(problem|question|questions|paper)\b",
            combined,
        )
    )

    # Ignore ambiguous or unclassified links.
    if has_answer == has_problem:
        return None

    return "answer" if has_answer else "problem"


def normalize_download_url(
    href: str,
    post_url: str,
) -> str | None:

    absolute = urljoin(
        post_url or BASE_URL,
        html.unescape(href.strip()),
    )

    parsed = urlparse(absolute)

    if parsed.scheme not in {"http", "https"}:
        return None

    if not parsed.hostname:
        return None

    if parsed.username or parsed.password:
        return None

    # Percent-encode non-ASCII characters (e.g. Korean file names) so
    # urllib can send the request line as ASCII. Existing escapes are
    # preserved by keeping "%" safe.
    return urlunparse(
        parsed._replace(
            path=quote(parsed.path, safe="/%"),
            query=quote(parsed.query, safe="=&%"),
        )
    )


def _links_in_scope(
    scope: Any,
    post_url: str,
) -> dict[str, str]:

    found: dict[str, str] = {}

    for anchor in scope.find_all("a", href=True):

        href = normalize_download_url(
            str(anchor["href"]),
            post_url,
        )

        if not href:
            continue

        kind = classify_link(
            href,
            anchor.get_text(" ", strip=True),
        )

        if kind and kind not in found:

            found[kind] = href

    return found


# ============================================================
# English PDF Extraction
# ============================================================

def parse_english_pdfs_from_html(
    content: str,
    post_url: str = BASE_URL,
) -> PdfLinks:
    """
    Extract problem and answer PDFs from the same
    English-labelled HTML region.
    """

    if not content:

        return PdfLinks(
            problem=None,
            answer=None,
        )

    soup = BeautifulSoup(
        html.unescape(content),
        "html.parser",
    )

    candidates: list[Any] = []

    # --------------------------------------------------------
    # 1. Table rows
    # --------------------------------------------------------

    for row in soup.find_all("tr"):

        row_text = row.get_text(
            " ",
            strip=True,
        ).lower()

        if "영어" in row_text or "english" in row_text:

            candidates.append(row)

    # --------------------------------------------------------
    # 2. Local HTML blocks
    # --------------------------------------------------------

    for node in soup.find_all(
        ["p", "li", "div", "section"]
    ):

        node_text = node.get_text(
            " ",
            strip=True,
        ).lower()

        if not (
            "영어" in node_text
            or "english" in node_text
        ):
            continue

        if not node.find("a", href=True):
            continue

        # Skip broad containers when a more specific
        # direct child contains the same English label.
        has_specific_child = any(
            child.name in {
                "p",
                "li",
                "div",
                "section",
            }
            and (
                "영어" in child.get_text(" ", strip=True).lower()
                or "english" in child.get_text(" ", strip=True).lower()
            )
            and child.find("a", href=True)

            for child in node.find_all(
                ["p", "li", "div", "section"],
                recursive=False,
            )
        )

        if not has_specific_child:

            candidates.append(node)

    # --------------------------------------------------------
    # 3. Validate complete pairs
    # --------------------------------------------------------

    for scope in candidates:

        links = _links_in_scope(
            scope,
            post_url,
        )

        if (
            "problem" in links
            and "answer" in links
        ):

            return PdfLinks(
                problem=links["problem"],
                answer=links["answer"],
            )

    # No cross-region fallback:
    # avoiding incorrect problem/answer pairing.

    return PdfLinks(
        problem=None,
        answer=None,
    )


# ============================================================
# File Management
# ============================================================

def build_filename(
    exam: ExamPost,
    kind: str,
) -> str:

    return (
        f"{exam.year}년_"
        f"{exam.month:02d}월_"
        f"{exam.grade}_"
        f"수능영어_{kind}.pdf"
    )


def is_valid_pdf(path: Path) -> bool:

    try:

        if not path.is_file():
            return False

        if path.stat().st_size < 8:
            return False

        with path.open("rb") as file:

            return file.read(5) == b"%PDF-"

    except OSError:

        return False


def download_pdf(
    url: str,
    target: Path,
    *,
    overwrite: bool = False,
) -> bool:
    """
    Download PDF to a temporary file.
    Validate the signature and atomically replace the target.
    """

    target.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    if (
        target.exists()
        and not overwrite
        and is_valid_pdf(target)
    ):

        logger.info(
            "Already downloaded; skipping: %s",
            target.name,
        )

        return False

    temp_path: Path | None = None

    try:

        request = Request(
            url,
            headers={"User-Agent": USER_AGENT},
        )

        with urlopen(request, timeout=60) as response:

            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{target.name}.",
                suffix=".part",
                dir=target.parent,
                delete=False,
            ) as temp:

                temp_path = Path(temp.name)

                first = response.read(5)

                if first != b"%PDF-":

                    raise ValueError(
                        f"Response is not a PDF: {url}"
                    )

                temp.write(first)

                while chunk := response.read(
                    1024 * 1024
                ):

                    temp.write(chunk)

        if not is_valid_pdf(temp_path):

            raise ValueError(
                f"Downloaded PDF failed validation: {url}"
            )

        os.replace(
            temp_path,
            target,
        )

        logger.info(
            "Downloaded: %s",
            target.name,
        )

        return True

    finally:

        if temp_path and temp_path.exists():

            temp_path.unlink(missing_ok=True)


# ============================================================
# CLI
# ============================================================

def build_parser() -> argparse.ArgumentParser:

    parser = argparse.ArgumentParser(
        description=(
            "Download English exam PDFs from horaeng.com."
        )
    )

    parser.add_argument(
        "--start-year",
        type=int,
        default=DEFAULT_START_YEAR,
    )

    parser.add_argument(
        "--end-year",
        type=int,
        default=date.today().year,
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing valid PDF files.",
    )

    return parser


# ============================================================
# Main
# ============================================================

def main() -> int:

    args = build_parser().parse_args()

    if (
        args.start_year < 2000
        or args.end_year < args.start_year
    ):

        logger.error(
            "Invalid year range: %s~%s",
            args.start_year,
            args.end_year,
        )

        return 2

    logger.info(
        "Collecting exam posts: %s~%s",
        args.start_year,
        args.end_year,
    )

    try:

        exams = collect_exam_posts(
            args.start_year,
            args.end_year,
        )

    except Exception:

        logger.exception(
            "Failed to collect WordPress posts."
        )

        return 1

    if not exams:

        logger.warning(
            "No matching exam posts found."
        )

        return 0

    downloaded = 0
    skipped = 0
    failed = 0
    incomplete = 0

    for exam in exams:

        links = parse_english_pdfs_from_html(
            exam.content,
            exam.link or BASE_URL,
        )

        if not links.problem or not links.answer:

            incomplete += 1

            logger.warning(
                "Incomplete PDF pair: "
                "post_id=%s title=%r problem=%s answer=%s",
                exam.post_id,
                exam.title,
                bool(links.problem),
                bool(links.answer),
            )

            continue

        for kind, url in (
            ("문제", links.problem),
            ("정답", links.answer),
        ):

            target = (
                args.output_dir
                / build_filename(exam, kind)
            )

            try:

                was_downloaded = download_pdf(
                    url,
                    target,
                    overwrite=args.overwrite,
                )

                if was_downloaded:
                    downloaded += 1
                else:
                    skipped += 1

            except Exception:

                failed += 1

                logger.exception(
                    "Download failed: "
                    "post_id=%s title=%r kind=%s",
                    exam.post_id,
                    exam.title,
                    kind,
                )

    logger.info(
        "Finished. posts=%d downloaded=%d "
        "skipped=%d incomplete_posts=%d failed_files=%d",
        len(exams),
        downloaded,
        skipped,
        incomplete,
        failed,
    )

    return 1 if failed else 0


if __name__ == "__main__":

    raise SystemExit(main())
