# 무드 라이브러리 파이프라인 공통 설정
from pathlib import Path  # 파일·폴더 경로 처리

# 프로젝트 루트 (mood_pipeline/ 의 상위 폴더)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 클러스터링 대상 원본 이미지 폴더
IMAGE_ROOT = PROJECT_ROOT / "images" / "final"
# 임베딩·클러스터·메타데이터 저장 폴더
DATA_DIR = PROJECT_ROOT / "data"
# 최종 무드 라이브러리 출력 폴더
MOOD_LIBRARY_DIR = PROJECT_ROOT / "mood_library"
# UMAP·elbow 등 시각화 저장 폴더
PREVIEW_DIR = DATA_DIR / "previews"

# Gemini API 특징 추출 산출물
GEMINI_FEATURE_MODEL = "gemini-2.5-flash-lite"
GEMINI_FEATURES_PATH = DATA_DIR / "gemini_features.json"
GEMINI_EMBEDDINGS_PATH = DATA_DIR / "gemini_embeddings.npy"
GEMINI_PATHS_PATH = DATA_DIR / "gemini_image_paths.json"
GEMINI_UMAP_PATH = PREVIEW_DIR / "umap_gemini.png"

# 허용 이미지 확장자
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
# 최소 이미지 한 변 길이 (px)
MIN_IMAGE_SIZE = 200
# 코사인 유사도가 이 값 이상이면 중복으로 간주
DEDUP_SIMILARITY = 0.95

# 영문 무드 → 폴더명용 slug 매핑
SLUG_MAP = {
    "warm cozy studio apartment interior": "warm_cozy",
    "minimal white scandinavian room": "minimal_white",
    "modern grey industrial studio": "modern_grey",
    "natural wood tone small room": "natural_wood",
    "soft beige feminine bedroom": "soft_beige",
    "dark moody compact apartment": "dark_moody",
    "bright airy open studio": "bright_airy",
    "vintage retro small room": "vintage_retro",
    "monochrome minimalist interior": "monochrome_minimal",
    "plant-filled green interior": "plant_green",
    "luxury modern studio apartment": "luxury_modern",
    "cute pastel room decor": "cute_pastel",
}
