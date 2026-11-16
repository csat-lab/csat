import difflib
import os
import re
from pathlib import Path

RAW_DIR = Path("data/raw/md")
OUT_DIR = Path("data/output")
SUMMARY_PATH = os.environ.get("GITHUB_STEP_SUMMARY")
GITHUB_OUTPUT_PATH = os.environ.get("GITHUB_OUTPUT")
ISSUE_BODY_PATH = Path("/tmp/verify_issue_body.md")


def normalize_text(text: str) -> str:
    """마크다운 문법, 여백, 줄바꿈 차이를 정규화하여 순수 텍스트 비교 준비"""
    text = re.sub(r"^---\n.*?\n---\n", "", text, flags=re.DOTALL)
    text = re.sub(r"[#*>`_\-\[\]()]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_question_numbers(text: str) -> set[int]:
    """영어 영역 문항 번호(18~45번) 추출"""
    matches = re.findall(r"(?:^|\s)(1[89]|[23]\d|4[0-5])\.", text)
    return {int(m) for m in matches}


def calculate_token_recall(raw_norm: str, out_norm: str) -> float:
    """원본(raw)의 단어들이 복원본(output)에 유실 없이 보존되었는지 계산 (0~100%)"""
    raw_words = re.findall(r"[A-Za-z0-9가-힣]+", raw_norm.lower())
    out_words = set(re.findall(r"[A-Za-z0-9가-힣]+", out_norm.lower()))
    if not raw_words:
        return 0.0
    preserved = sum(1 for w in raw_words if w in out_words)
    return (preserved / len(raw_words)) * 100


def set_github_output(name: str, value: str) -> None:
    if GITHUB_OUTPUT_PATH:
        with open(GITHUB_OUTPUT_PATH, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")


def verify_all() -> None:
    if not RAW_DIR.exists():
        print("❌ `data/raw/md` 디렉터리가 존재하지 않습니다.")
        set_github_output("has_issues", "false")
        return

    raw_files = sorted(RAW_DIR.glob("*.md"))
    if not raw_files:
        print("⚠️ `data/raw/md`에 비교할 `.md` 파일이 없습니다.")
        set_github_output("has_issues", "false")
        return

    results = []
    missing_outputs = []
    low_quality_files = []

    for raw_path in raw_files:
        out_path = OUT_DIR / raw_path.name
        if not out_path.exists():
            missing_outputs.append(raw_path.name)
            continue

        raw_text = raw_path.read_text(encoding="utf-8", errors="ignore")
        out_text = out_path.read_text(encoding="utf-8", errors="ignore")

        raw_norm = normalize_text(raw_text)
        out_norm = normalize_text(out_text)

        recall = calculate_token_recall(raw_norm, out_norm)
        similarity = difflib.SequenceMatcher(None, raw_norm, out_norm).ratio() * 100
        len_ratio = (len(out_norm) / len(raw_norm) * 100) if raw_norm else 0.0

        raw_q = extract_question_numbers(raw_text)
        out_q = extract_question_numbers(out_text)
        missing_q = sorted(raw_q - out_q)

        if recall < 85.0 or len_ratio < 50.0 or missing_q:
            status = "⚠️ 확인 필요"
            low_quality_files.append(
                {
                    "name": raw_path.name,
                    "recall": recall,
                    "similarity": similarity,
                    "len_ratio": len_ratio,
                    "missing_q": ", ".join(map(str, missing_q)) if missing_q else "-",
                }
            )
        else:
            status = "✅ 정상"

        results.append(
            {
                "name": raw_path.name,
                "status": status,
                "recall": recall,
                "similarity": similarity,
                "len_ratio": len_ratio,
                "missing_q": ", ".join(map(str, missing_q)) if missing_q else "-",
            }
        )

    # 1. Summary용 전체 리포트 생성
    summary_lines = [
        "## 📊 수능 영어 원문 복원율 검증 리포트",
        "",
        f"- **전체 원본 파일 (`data/raw/md`)**: {len(raw_files)}개",
        f"- **복원 완료 파일 (`data/output`)**: {len(results)}개",
        f"- **복원본 미생성 파일**: {len(missing_outputs)}개",
        f"- **주의/확인 필요 파일**: {len(low_quality_files)}개",
        "",
    ]

    if missing_outputs:
        summary_lines.append("### ❌ `data/output/`에 없는 파일 목록")
        for name in missing_outputs:
            summary_lines.append(f"- `{name}`")
        summary_lines.append("")

    if results:
        summary_lines.extend(
            [
                "### 📋 파일별 상세 복원율",
                "",
                "| 상태 | 파일명 | 원문 보존율(Recall) | 구조 유사도 | 길이 비율 | 누락 의심 문항 |",
                "| :---: | :--- | :---: | :---: | :---: | :---: |",
            ]
        )
        for r in results:
            summary_lines.append(
                f"| {r['status']} | `{r['name']}` | **{r['recall']:.1f}%** | "
                f"{r['similarity']:.1f}% | {r['len_ratio']:.1f}% | {r['missing_q']} |"
            )

    report = "\n".join(summary_lines)
    print(report)

    if SUMMARY_PATH:
        with open(SUMMARY_PATH, "a", encoding="utf-8") as f:
            f.write("\n" + report + "\n")

    # 2. 문제가 있는 파일만 추려서 GitHub Issue 본문 생성
    has_issues = bool(low_quality_files or missing_outputs)
    set_github_output("has_issues", "true" if has_issues else "false")
    set_github_output("issue_count", str(len(low_quality_files) + len(missing_outputs)))

    if has_issues:
        issue_lines = [
            "원문 복원 파이프라인 검증 중 **원문 보존율 저하(85% 미만), 문항 누락, 또는 미생성 파일**이 감지되었습니다.",
            "",
        ]
        if low_quality_files:
            issue_lines.extend(
                [
                    "### ⚠️ 품질 확인 필요 파일",
                    "",
                    "| 파일명 | 원문 보존율 | 구조 유사도 | 길이 비율 | 누락 의심 문항 |",
                    "| :--- | :---: | :---: | :---: | :---: |",
                ]
            )
            for item in low_quality_files:
                issue_lines.append(
                    f"| `{item['name']}` | **{item['recall']:.1f}%** | "
                    f"{item['similarity']:.1f}% | {item['len_ratio']:.1f}% | {item['missing_q']} |"
                )
            issue_lines.append("")

        if missing_outputs
