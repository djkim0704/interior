# 무드 라이브러리 폴더·JSON 생성 모듈
from __future__ import annotations  # 타입 힌트 forward reference 지원

import json  # meta.json·index.json 저장
import shutil  # 이미지 복사·폴더 재생성
from pathlib import Path  # 경로 처리

import numpy as np  # 픽셀 배열·색상 centroid
import pandas as pd  # metadata.csv 읽기
from PIL import Image  # 커버·팔레트 추출용 이미지 로드
from sklearn.cluster import KMeans  # 대표 색상 k-means

from .config import DATA_DIR, IMAGE_ROOT, MOOD_LIBRARY_DIR
from .search import build_mood_text_embeddings


def extract_palette(image_path: Path, n_colors: int = 5) -> list[str]:
    img = Image.open(image_path).convert("RGB").resize((200, 200))  # 작게 줄여서 빠르게
    pixels = np.array(img).reshape(-1, 3)  # (픽셀수, 3) RGB
    if len(pixels) < n_colors:
        n_colors = max(1, len(pixels))
    km = KMeans(n_clusters=n_colors, random_state=42, n_init=10)
    km.fit(pixels)  # 비슷한 색끼리 n_colors 그룹
    colors = km.cluster_centers_.astype(int)  # 각 그룹 대표 RGB
    # "#rrggbb" hex 문자열 5개까지
    return ["#{:02x}{:02x}{:02x}".format(*c) for c in colors][:5]


def run_build_library() -> dict:
    meta_path = DATA_DIR / "metadata.csv"
    labels_path = DATA_DIR / "labels.json"
    if not meta_path.exists() or not labels_path.exists():
        raise FileNotFoundError("metadata.csv / labels.json 없음. label 실행을 먼저 하세요.")

    df = pd.read_csv(meta_path)  # 이미지별 cluster_id, mood_id 등
    labels = json.loads(labels_path.read_text(encoding="utf-8"))  # 클러스터별 라벨 상세

    if MOOD_LIBRARY_DIR.exists():
        shutil.rmtree(MOOD_LIBRARY_DIR)  # 이전 빌드 삭제
    MOOD_LIBRARY_DIR.mkdir(parents=True)

    moods_index = []  # index.json에 넣을 무드 요약 리스트

    for cid, group in df.groupby("cluster_id"):
        cid = int(cid)
        first_row = group.iloc[0]
        label_info = labels.get(str(cid), {})
        mood_id = first_row["mood_id"]           # 예: minimal_white
        folder_name = f"{mood_id}_c{cid:02d}"     # 예: minimal_white_c01

        mood_dir = MOOD_LIBRARY_DIR / folder_name
        gallery_dir = mood_dir / "gallery"
        gallery_dir.mkdir(parents=True)

        # is_representative=True인 행 = 대표 이미지
        reps = group[group["is_representative"] == True]  # noqa: E712
        if reps.empty:
            reps = group.head(1)  # 없으면 첫 번째 이미지
        cover_src = IMAGE_ROOT / reps.iloc[0]["relative_path"]
        shutil.copy2(cover_src, mood_dir / "cover.jpg")

        rep_names = []
        for _, row in group.iterrows():
            src = IMAGE_ROOT / row["relative_path"]
            dest = gallery_dir / row["filename"]
            shutil.copy2(src, dest)  # gallery/에 전체 이미지 복사
            if row["is_representative"]:
                rep_names.append(row["filename"])

        palette = extract_palette(cover_src)  # 커버에서 대표 색 5개

        meta = {
            "id": folder_name,
            "mood_id": mood_id,
            "cluster_id": cid,
            "name_ko": first_row["mood_name_ko"],
            "name_en": first_row["mood_name_en"],
            "tags": first_row["tags"].split(",") if isinstance(first_row["tags"], str) else [],
            "typical_elements": [],
            "avoid_elements": [],
            "gallery_count": len(group),
            "color_palette": palette,
            "confidence": label_info.get("confidence"),
            # 사진 사본은 저장소에 올리지 않는다. 사본이 없을 때 원본(images/final)에서
            # 대표 사진을 찾을 수 있게 원본 파일 이름을 남긴다
            "cover_source": Path(str(reps.iloc[0]["relative_path"])).name,
        }
        (mood_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        moods_index.append({
            "id": folder_name,
            "mood_id": mood_id,
            "cluster_id": cid,
            "name_ko": meta["name_ko"],
            "name_en": meta["name_en"],
            "description": f"{meta['name_ko']} 무드의 원룸 인테리어 레퍼런스",
            "keywords": meta["tags"],
            "color_palette": palette,
            "image_count": len(group),
            "cover": f"{folder_name}/cover.jpg",
            "representatives": rep_names[:3],
        })

    index = {
        "version": "1.0",
        "total_images": len(df),
        "mood_count": len(moods_index),
        "moods": sorted(moods_index, key=lambda m: -m["image_count"]),  # 많은 순 정렬
    }
    (MOOD_LIBRARY_DIR / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 프롬프트 검색용 CLIP 텍스트·대표 이미지 임베딩 캐시 생성
    search_index = build_mood_text_embeddings(force=True)

    return {
        "mood_count": len(moods_index),
        "total_images": len(df),
        "library_path": str(MOOD_LIBRARY_DIR),
        "search_index": search_index,
    }
