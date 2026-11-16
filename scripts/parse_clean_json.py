"""복원 md에서 변형용 독해 지문을 추출해 clean JSON으로 만든다.

원본 코드(보스 제공)를 현재 저장소 형식에 맞게 보정한 버전이다.
보정 내역:
1. 40번 요약문 분리 로직 제거: 2026-11-16 결정으로 전 파일 40번은 지문만
   남았고, 실제로 74개 파일 전부 40번 본문이 1줄이다. 요약문 분리 분기는
   죽은 코드이며, 본문이 여러 줄일 경우 마지막 줄을 요약문으로 오인한다.
   이제 original_summary는 항상 None이다.
2. needs_vocab_fix를 실측 기준으로 변경: 2026-11-16 전수 감사 수정으로
   Q29/30/42 교정 62건이 적용돼, 미해결은 아래 11건만 남았다. 플래그는
   미해결 건에만 True이다. (원본 코드는 30/42번 전부 True였다.)
3. 단일 파일 실행을 전체 74개 파일 일괄 처리로 확장.
4. metadata에 원문 출처(source) 추가: 복원 md와 원본 문제지·답지 PDF의
   저장소 경로를 기록한다. (2026-11-16)
5. OPEN_VOCAB_CASES 비움: 미해결 11건이 전부 해결돼 needs_vocab_fix는
   전 파일 False이다. 이미지 답지 4건은 커밋 f8cef486, G? 7건은 커밋
   a4a6c299에서 복원 파일에 적용됨. (2026-11-16)
"""
import argparse
import json
import re
from pathlib import Path

# 1. 문항 번호별 원문 유형 매핑
QUESTION_TYPES = {
    20: "주장 추론", 21: "함축 의미 추론", 22: "요지 추론", 23: "주제 추론", 24: "제목 추론",
    29: "어법 추론", 30: "어휘 추론", 31: "빈칸 추론", 32: "빈칸 추론", 33: "빈칸 추론",
    34: "빈칸 추론", 35: "무관한 문장", 36: "순서 배열", 37: "순서 배열", 38: "문장 삽입",
    39: "문장 삽입", 40: "요약문 완성", 41: "장문 독해(제목)", 42: "장문 독해(어휘)"
}

# 변형 대상 문항 번호 (41, 42번은 파싱 후 하나로 병합)
TARGET_NUMBERS = set(QUESTION_TYPES.keys())

# 아직 교정이 안 된 어휘 문항 (전수 감사 기준: G? 7건 + 이미지 답지 4건)
# 키: 파일 줄기(YYYY_Mn_Hn), 값: 미해결 문항 번호 집합
# 2026-11-16: 11건 전부 해결돼 비워 둔다. 새로 생기면 여기에 추가한다.
OPEN_VOCAB_CASES = {}


def parse_filename_meta(filename: str) -> dict:
    """파일명(예: 2026_M6_H1_restored.md)에서 연도, 월, 학년 추출"""
    match = re.match(r"(\d{4})_M(\d+)_H(\d+)", filename)
    if match:
        year, month, grade = match.groups()
        return {
            "year": int(year),
            "month": int(month),
            "grade": f"고{grade}",
            "exam_title": f"{year}학년도 {month}월 고{grade} 전국연합학력평가"
        }
    return {"exam_title": filename}


def parse_restored_markdown(md_path: str) -> dict:
    path = Path(md_path)
    raw_text = path.read_text(encoding="utf-8")
    meta = parse_filename_meta(path.name)
    stem_match = re.match(r"(\d{4})_M(\d+)_H(\d+)", path.name)
    year, month, grade_digit = stem_match.groups()
    stem = stem_match.group(0)
    # 원문 출처: 복원 md와 원본 문제지·답지 PDF의 저장소 경로
    # (data/raw 파일명은 "YYYY년_M월_고N_수능영어_문제/정답.pdf"이며 월은 0으로 채우지 않는다)
    meta["source"] = {
        "restored_md": f"data/output/고{grade_digit}/{stem}_restored.md",
        "questions_pdf": f"data/raw/{year}년_{month}월_고{grade_digit}_수능영어_문제.pdf",
        "answers_pdf": f"data/raw/{year}년_{month}월_고{grade_digit}_수능영어_정답.pdf",
    }
    open_cases = OPEN_VOCAB_CASES.get(stem, set())

    # 문항 번호(1. ~ 45.) 기준으로 블록 분리
    pattern = re.compile(r"^(\d{1,2})\.\s+(.+?)(?=^\d{1,2}\.\s+|\Z)", re.MULTILINE | re.DOTALL)
    matches = pattern.findall(raw_text)

    parsed_items = {}

    for num_str, block in matches:
        q_num = int(num_str)
        if q_num not in TARGET_NUMBERS:
            continue

        lines = [line.strip() for line in block.strip().split("\n") if line.strip()]
        # 이미지 태그 제거
        lines = [line for line in lines if not line.startswith("![image]")]

        stem_text = lines[0] if lines else ""
        points = 3 if "[3점]" in stem_text else 2
        stem_clean = stem_text.replace("[3점]", "").strip()

        body_lines = lines[1:]

        # 40번은 지문만 존재한다 (요약문 제거 결정 반영). 본문 전체가 지문이다.
        passage = " ".join(body_lines)
        original_summary = None

        word_count = len(passage.split())

        item_data = {
            "source_id": f"{meta.get('year', 'Unknown')}-M{meta.get('month', '0')}-H{meta.get('grade', '1')[-1]}-Q{q_num}",
            "q_num": str(q_num),
            "original_type": QUESTION_TYPES[q_num],
            "original_stem": stem_clean,
            "original_points": points,
            "passage": passage,
            "original_summary": original_summary,
            "word_count": word_count,
            "flags": {
                "incomplete_passage": word_count < 40,          # 35번 등 문장 누락 감지
                "needs_vocab_fix": q_num in open_cases,        # 미해결 어휘 교정 건만 True
                "allowed_tiers": ["기본", "중급", "고급"]
            }
        }
        parsed_items[q_num] = item_data

    # 41번, 42번 중복 지문 병합 처리
    if 41 in parsed_items and 42 in parsed_items:
        merged = parsed_items[41].copy()
        merged["source_id"] = merged["source_id"].replace("Q41", "Q41-42")
        merged["q_num"] = "41-42"
        merged["original_type"] = "장문 독해(제목/어휘)"
        merged["flags"] = dict(merged["flags"])
        merged["flags"]["needs_vocab_fix"] = 42 in open_cases
        parsed_items[41] = merged
        del parsed_items[42]

    final_list = [parsed_items[k] for k in sorted(parsed_items.keys())]

    return {
        "metadata": meta,
        "total_selected_passages": len(final_list),
        "passages": final_list
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="복원 md 74개를 clean JSON으로 일괄 변환")
    parser.add_argument("--input-dir", default="data/output",
                        help="학년 폴더(고1/고2/고3)를 품은 복원 md 루트 (기본: data/output)")
    parser.add_argument("--output-dir", default="data/output/json",
                        help="JSON 출력 디렉토리 (기본: data/output/json)")
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    md_files = sorted(input_dir.glob("고[123]/*_restored.md"))
    if not md_files:
        raise SystemExit(f"복원 md를 찾지 못했다: {input_dir}/고[123]/*_restored.md")

    expected = len(TARGET_NUMBERS) - 1  # 41/42 병합으로 19개 -> 18개
    warnings = []
    for md_path in md_files:
        result = parse_restored_markdown(str(md_path))
        if result["total_selected_passages"] != expected:
            warnings.append(f"[오류] {md_path.name}: 지문 {result['total_selected_passages']}개 (예상 {expected}개)")
            continue
        out_name = re.sub(r"_restored\.md$", "_clean.json", md_path.name)
        (output_dir / out_name).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        for p in result["passages"]:
            if p["flags"]["incomplete_passage"]:
                warnings.append(
                    f"[경고] {md_path.name} {p['q_num']}번 지문 단어 수 부족 "
                    f"({p['word_count']}단어) - 원문 확인 필요")
            if p["flags"]["needs_vocab_fix"]:
                warnings.append(f"[안내] {md_path.name} {p['q_num']}번 지문 오답 어휘 교정 미해결")

    print(f"추출 완료: {len(md_files)}개 파일 처리, {len(list(output_dir.glob('*_clean.json')))}개 JSON 저장됨.")
    for w in warnings:
        print(w)


if __name__ == "__main__":
    main()
