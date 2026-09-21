# 무드 라이브러리 파이프라인 패키지 진입점
#
# backend/app.py 는 검색(search)만 쓰는데, 예전에는 이 파일이 build_library·cluster·
# label 까지 즉시 import 해서 Flask 기동마다 pandas·matplotlib·sklearn·umap 이
# 함께 올라왔다. 그래서 하위 모듈은 실제로 이름을 꺼낼 때 로드한다(PEP 562).
import importlib
from typing import TYPE_CHECKING

# 공개 이름 → 실제 구현이 있는 하위 모듈
_EXPORTS = {
    "prepare_dataset": "preprocess",
    "run_embedding": "embed",
    "run_clustering": "cluster",
    "plot_umap": "cluster",
    "plot_elbow": "cluster",
    "run_labeling": "label",
    "run_build_library": "build_library",
    "build_mood_text_embeddings": "search",
    "search_mood_by_prompt": "search",
    "search_images_within_mood": "search",
    "search_images_globally": "search",
    "search_mood_with_images": "search",
    "plot_search_result": "search",
}

# 외부에서 import 가능한 공개 API 목록
__all__ = list(_EXPORTS)


def __getattr__(name: str):
    # `from mood_search_v1 import search_mood_with_images` 처럼 이름을 꺼낼 때만
    # 해당 하위 모듈(그리고 그 무거운 의존성)을 로드한다.
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(f".{module}", __name__), name)
    globals()[name] = value  # 두 번째 접근부터는 __getattr__ 를 타지 않는다
    return value


def __dir__():
    return sorted([*globals(), *__all__])


if TYPE_CHECKING:  # 타입 검사기·IDE 자동완성용 (런타임에는 실행되지 않음)
    from .build_library import run_build_library
    from .cluster import plot_elbow, plot_umap, run_clustering
    from .embed import run_embedding
    from .label import run_labeling
    from .preprocess import prepare_dataset
    from .search import (
        build_mood_text_embeddings,
        plot_search_result,
        search_images_globally,
        search_images_within_mood,
        search_mood_by_prompt,
        search_mood_with_images,
    )
