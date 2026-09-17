"""
AI 보정(마모/손상 제거) 학습 데이터 생성 스크립트

- "번개장터", "joongna_images" 폴더에서 각각 무작위 50장씩(총 100장) 샘플링
- Nano Banana 2 계열 이미지 편집 API(모델명: gemini-3-flash-image)로
  스크래치/얼룩/마모/찌그러짐을 제거한 "새 제품처럼 보이는" 이미지를 생성
- 결과는 "이미지 변환/" 폴더 하나에 "{번호}-원본.{ext}" / "{번호}-ai.png" 쌍으로 저장
  (예: 1-원본.jpg, 1-ai.png, 2-원본.jpg, 2-ai.png ...) 하여 나란히 대조 가능하게 함
- 이미 변환된 파일은 건너뛰어 재실행 시 중복 API 호출을 하지 않음
- 어떤 원본이 몇 번인지는 sampled_files.json 에 기록하여 재현/추적 가능하게 함
"""

import base64
import json
import shutil
import sys
import time
import random
from pathlib import Path

from openai import OpenAI

import config

# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent

SRC_DIRS = {
    "bunjang": SCRIPT_DIR / "번개장터",
    "joongna": SCRIPT_DIR / "joongna_images",
}

OUT_ROOT = SCRIPT_DIR / "이미지 변환"
SAMPLED_FILES_JSON = OUT_ROOT / "sampled_files.json"

SAMPLES_PER_FOLDER = 50
RANDOM_SEED = 42
REQUEST_DELAY_SEC = 1.0

MODEL = "gpt-image-1"
PROMPT = (
    "이 중고 물품 사진에서 스크래치, 얼룩, 마모 흔적, 찌그러짐을 자연스럽게 지워서 "
    "새 제품처럼 보이게 편집해줘. 배경과 물체의 형태, 색상, 구도는 그대로 유지해."
)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

# 2026-09 기준 gpt-image-1 계열 이미지 편집 단가는 화질/해상도에 따라
# 장당 대략 $0.02 ~ $0.19 수준입니다 (nano-banana 계열은 이와 다를 수 있으니
# 실제 청구 내역과 최신 OpenAI/제공처 가격표를 꼭 확인하세요).
ESTIMATED_COST_PER_IMAGE_USD = 0.07


def get_image_files(folder: Path) -> list[str]:
    files = [
        p.name for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    ]
    files.sort()  # 정렬 후 샘플링해야 random.seed 결과가 재현 가능함
    return files


def build_or_load_index_map() -> dict[str, dict]:
    """index(str) -> {"source": "bunjang"|"joongna", "filename": str} 매핑을 만들거나 불러온다."""
    if SAMPLED_FILES_JSON.exists():
        print(f"[정보] 기존 샘플 목록을 재사용합니다: {SAMPLED_FILES_JSON}")
        with open(SAMPLED_FILES_JSON, "r", encoding="utf-8") as f:
            return json.load(f)

    random.seed(RANDOM_SEED)
    per_source_samples: dict[str, list[str]] = {}
    for key, folder in SRC_DIRS.items():
        if not folder.exists():
            print(f"[경고] 폴더가 없습니다: {folder}")
            per_source_samples[key] = []
            continue
        all_files = get_image_files(folder)
        if len(all_files) < SAMPLES_PER_FOLDER:
            print(
                f"[경고] {folder} 에 이미지가 {len(all_files)}장뿐입니다. "
                f"{SAMPLES_PER_FOLDER}장보다 적어 전체를 사용합니다."
            )
            per_source_samples[key] = all_files
        else:
            per_source_samples[key] = random.sample(all_files, SAMPLES_PER_FOLDER)

    index_map: dict[str, dict] = {}
    idx = 1
    for key in ("bunjang", "joongna"):  # bunjang: 1~50, joongna: 51~100
        for filename in per_source_samples.get(key, []):
            index_map[str(idx)] = {"source": key, "filename": filename}
            idx += 1

    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    with open(SAMPLED_FILES_JSON, "w", encoding="utf-8") as f:
        json.dump(index_map, f, ensure_ascii=False, indent=2)
    print(f"[정보] 샘플 목록을 저장했습니다: {SAMPLED_FILES_JSON}")

    return index_map


def repair_image(client: OpenAI, src_path: Path) -> bytes:
    with open(src_path, "rb") as image_file:
        result = client.images.edit(
            model=MODEL,
            image=image_file,
            prompt=PROMPT,
        )
    b64_data = result.data[0].b64_json
    return base64.b64decode(b64_data)


def main() -> None:
    if not config.OPENAI_API_KEY:
        print(
            "[오류] config.py 의 OPENAI_API_KEY 가 비어 있습니다. "
            "값을 채운 뒤 다시 실행하세요."
        )
        sys.exit(1)

    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    index_map = build_or_load_index_map()
    total_planned = len(index_map)

    print("=" * 60, flush=True)
    print(f"총 {total_planned}장 변환 예정", flush=True)
    print(
        f"이미지당 예상 비용은 대략 ${ESTIMATED_COST_PER_IMAGE_USD:.2f} "
        f"(총 예상 비용 약 ${ESTIMATED_COST_PER_IMAGE_USD * total_planned:.2f})",
        flush=True,
    )
    print("※ 실제 단가는 모델/화질/해상도에 따라 다르니 최신 가격표를 확인하세요.", flush=True)
    print("=" * 60, flush=True)

    client = OpenAI(api_key=config.OPENAI_API_KEY)

    success_count = 0
    fail_count = 0
    skip_count = 0
    failed_files: list[str] = []

    for idx_str, info in index_map.items():
        source = info["source"]
        filename = info["filename"]
        src_path = SRC_DIRS[source] / filename
        ext = src_path.suffix or ".jpg"

        original_out_path = OUT_ROOT / f"{idx_str}-원본{ext}"
        ai_out_path = OUT_ROOT / f"{idx_str}-ai.png"

        # 원본 복사 (대조용, API 호출과 무관)
        if not original_out_path.exists() and src_path.exists():
            shutil.copy2(src_path, original_out_path)

        if ai_out_path.exists():
            skip_count += 1
            print(f"[건너뜀] 이미 변환됨: {idx_str}-ai.png ({source}/{filename})", flush=True)
            continue

        if not src_path.exists():
            fail_count += 1
            failed_files.append(f"{idx_str} ({source}/{filename}) - 원본 없음")
            print(f"[실패] 원본 파일을 찾을 수 없습니다: {src_path}", flush=True)
            continue

        try:
            image_bytes = repair_image(client, src_path)
            with open(ai_out_path, "wb") as f:
                f.write(image_bytes)
            success_count += 1
            print(f"[성공] {idx_str}-ai.png ({source}/{filename})", flush=True)
        except Exception as e:
            fail_count += 1
            failed_files.append(f"{idx_str} ({source}/{filename}) - {e}")
            print(f"[실패] {idx_str} ({source}/{filename}) 변환 중 오류 발생: {e}", flush=True)
        finally:
            time.sleep(REQUEST_DELAY_SEC)

    print("=" * 60, flush=True)
    print("변환 완료 요약", flush=True)
    print(f"  성공: {success_count}", flush=True)
    print(f"  실패: {fail_count}", flush=True)
    print(f"  건너뜀(이미 존재): {skip_count}", flush=True)
    print(f"  총 대상: {total_planned}", flush=True)
    if failed_files:
        print("  실패 목록:", flush=True)
        for item in failed_files:
            print(f"    - {item}", flush=True)
    print("=" * 60, flush=True)


if __name__ == "__main__":
    main()
