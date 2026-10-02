import json
import hashlib
import os
import re
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required
from ultralytics import YOLO

import ai_backend
import product_recommendation as furniture_recommender
from auth import auth_bp
from extensions import db, login_manager
from models import SavedDesign, User


BASE_DIR = os.path.dirname(
    os.path.abspath(__file__)
)

FRONTEND_DIR = os.path.join(
    BASE_DIR,
    "..",
    "frontend",
)

PROJECT_ROOT = os.path.abspath(
    os.path.join(
        BASE_DIR,
        "..",
    )
)

if PROJECT_ROOT not in sys.path:
    sys.path.insert(
        0,
        PROJECT_ROOT,
    )


from model1 import (
    interior_to_floorplan
    as floorplan_model
)
from model2 import (
    web_floorplan
    as model2_floorplan
)
from model2 import floorplan_3d
from model2 import scene_graph
from model2 import scene_edit
from model2 import gemini_reanalyze
from model2 import spatial_fit
from model2 import gemini_furniture_parts
from model2 import gemini_furniture_views
from model2.gemini_retry import (
    call_with_retry,
    GeminiBusyError,
    api_status_code,
)

from mood_pipeline import rule_based_svg

from mood_pipeline.config import (
    IMAGE_ROOT
    as MOOD_IMAGE_ROOT,
)

from mood_search_v1 import (
    search
    as mood_search_v1,
)

from mood_search_v1.config import (
    MOOD_LIBRARY_DIR,
)


# 실행 위치와 관계없이
# 프로젝트 루트의 .env 파일을 읽는다.
load_dotenv(
    os.path.join(
        PROJECT_ROOT,
        ".env",
    )
)


app = Flask(
    __name__,
    template_folder=os.path.join(
        FRONTEND_DIR,
        "templates",
    ),
    static_folder=os.path.join(
        FRONTEND_DIR,
        "static",
    ),
)

app.secret_key = os.getenv(
    "FLASK_SECRET_KEY",
    "dev-secret-key-change-in-production",
)

app.config["SQLALCHEMY_DATABASE_URI"] = (
    "sqlite:///"
    + os.path.join(
        BASE_DIR,
        "app.db",
    )
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)
login_manager.init_app(app)
login_manager.login_view = "auth.login"
login_manager.login_message = "로그인이 필요한 페이지입니다."


@login_manager.user_loader
def load_user(user_id):
    """세션에 저장된 사용자 ID로 사용자 정보를 조회한다."""
    try:
        return db.session.get(
            User,
            int(user_id),
        )
    except (TypeError, ValueError):
        return None


app.register_blueprint(auth_bp)

with app.app_context():
    db.create_all()


def _ensure_mood_library():
    """무드 라이브러리가 없으면 첫 기동 때 한 번 만든다.

    clone 직후에는 mood_library/ 가 용량 때문에 비어 있다. 그대로 두면
    /mood-search 가 index.json 을 못 찾아 500 만 계속 뱉는데, 원인이
    코드가 아니라 "파이프라인을 아직 안 돌렸다" 라서 로그만 봐서는
    알아차리기 어렵다. 그래서 기동 시점에 직접 만든다.

    CLIP 로컬 추론만 쓰므로 Gemini 호출량(RPD)에는 영향이 없다.
    """
    index_path = (
        MOOD_LIBRARY_DIR
        / "index.json"
    )

    if index_path.exists():
        return

    # 원본 이미지가 없으면 만들 방법이 없다. 무드 검색만 죽고 평면도·상품
    # 추천은 멀쩡하므로, 기동을 막지 말고 안내만 남긴다.
    if not MOOD_IMAGE_ROOT.exists() or not any(
        MOOD_IMAGE_ROOT.iterdir()
    ):
        print(
            "[mood] images/final 이 비어 있어 "
            "무드 라이브러리를 건너뛴다. "
            "무드 검색만 비활성화된다."
        )
        return

    # CPU 기준 약 4~5분. 무거운 pandas·sklearn·umap 을 여기서 처음 끌어오므로
    # import 도 함수 안에 둔다(mood_search_v1/__init__.py 의 지연 로딩과 같은 이유).
    print(
        "[mood] 무드 라이브러리가 없어 "
        "새로 만든다. CPU 기준 4~5분 걸린다."
    )

    try:
        from mood_search_v1 import (
            run_build_library,
            run_clustering,
            run_embedding,
            run_labeling,
        )

        run_embedding()
        run_clustering()
        run_labeling()
        run_build_library()

        print(
            "[mood] 무드 라이브러리 생성 완료."
        )

    except Exception as exc:
        # 빌드가 실패해도 나머지 기능은 살아 있어야 한다.
        print(
            "[mood] 무드 라이브러리 생성 실패: "
            f"{exc}"
        )


# debug=True 의 리로더는 프로세스를 두 번 띄운다. 가드가 없으면 4~5분짜리
# 빌드가 두 번 돈다. WERKZEUG_RUN_MAIN 은 리로더가 띄운 자식에만 있다.
if (
    not app.debug
    or os.environ.get(
        "WERKZEUG_RUN_MAIN"
    )
    == "true"
):
    _ensure_mood_library()

app.json.ensure_ascii = False

# 같은 서버 프로세스에서 평면도 Gemini 요청이 동시에 실행되면 낮은 RPM
# 한도를 빠르게 소진한다. 한 요청이 끝난 뒤 다음 요청이 캐시를 확인하도록
# 생성 구간을 직렬화한다.
floorplan_generation_lock = threading.Lock()


UPLOAD_DIR = os.path.join(
    FRONTEND_DIR,
    "static",
    "uploads",
)

GENERATED_DIR = os.path.join(
    FRONTEND_DIR,
    "static",
    "generated",
)

PRODUCT_CACHE_DIR = os.path.join(
    GENERATED_DIR,
    "product_cache",
)


ALLOWED_EXT = {
    "jpg",
    "jpeg",
    "png",
}


os.makedirs(
    UPLOAD_DIR,
    exist_ok=True,
)

os.makedirs(
    GENERATED_DIR,
    exist_ok=True,
)

os.makedirs(
    PRODUCT_CACHE_DIR,
    exist_ok=True,
)


# 새로 구매할 수 있는 가구 종류
PURCHASE_LABELS = {
    "bed": "침대",
    "sofa": "소파",
    "chair": "의자",
    "desk": "책상",
    "table": "테이블",
    "bench": "벤치",
    "shelf": "선반",
    "cabinet": "수납장",
    "dresser": "서랍장",
    "wardrobe": "옷장",
    "lamp": "조명",
    "rug": "러그",
    "plant": "식물",
}


# 기존 AJAX 추천 API에서 사용하는 ID
PURCHASE_ITEM_IDS = {
    "bed": "bed-001",
    "sofa": "sofa-001",
    "chair": "chair-001",
    "desk": "desk-001",
    "table": "table-001",
    "bench": "bench-001",
    "shelf": "shelf-001",
    "cabinet": "cabinet-001",
    "dresser": "dresser-001",
    "wardrobe": "wardrobe-001",
    "lamp": "lamp-001",
    "rug": "rug-001",
    "plant": "plant-001",
}


def build_purchase_items(
    purchase_types,
):
    """저장된 구매 가구 유형을 결과 화면용 항목으로 변환한다."""
    return [
        {
            "type": item_type,
            "label": PURCHASE_LABELS.get(
                item_type,
                item_type,
            ),
            "item_id": PURCHASE_ITEM_IDS.get(
                item_type,
                item_type,
            ),
        }
        for item_type in (purchase_types or [])
        if isinstance(item_type, str)
    ]


def parse_saved_json(
    raw,
    default,
):
    """저장된 JSON을 안전하게 읽고 손상된 값에는 기본값을 반환한다."""
    try:
        value = json.loads(
            raw or ""
        )
    except (
        TypeError,
        json.JSONDecodeError,
    ):
        return default
    return value


# 아직 Model2 자동 배치 기능이 없으므로
# 새 가구를 평면도에 표시할 때 사용할 임시 좌표
PURCHASE_POSITIONS = [
    (0.24, 0.24),
    (0.50, 0.24),
    (0.76, 0.24),
    (0.24, 0.52),
    (0.50, 0.52),
    (0.76, 0.52),
    (0.36, 0.78),
    (0.64, 0.78),
]


# ──────────────────────────────────────────────────────
# 공통 유틸리티
# ──────────────────────────────────────────────────────
def allowed_file(
    filename: str,
) -> bool:
    """업로드 파일의 확장자가 허용 목록에 포함되는지 확인한다."""
    return (
        "." in filename
        and filename
        .rsplit(
            ".",
            1,
        )[1]
        .lower()
        in ALLOWED_EXT
    )


def clean_html(text):
    """
    쇼핑 API 상품명에 포함된 HTML 태그를 제거한다.
    """

    if text is None:
        return ""

    return re.sub(
        r"<.*?>",
        "",
        str(text),
    )


def safe_int(
    value,
    default=0,
):
    """값을 정수로 변환하고 실패하면 기본값을 반환한다."""
    try:
        if (
            value is None
            or value == ""
        ):
            return default

        return int(value)

    except (
        ValueError,
        TypeError,
    ):
        return default


def portable_basename(value):
    """Windows와 POSIX 경로에서 안전하게 파일명만 추출한다."""
    text = str(value or "").strip().replace("\\", "/")
    filename = text.rsplit("/", 1)[-1] if text else ""
    return "" if filename in {"", ".", ".."} else filename


def resolve_generated_file(value):
    """저장된 파일명이나 경로를 현재 생성 결과 폴더 기준으로 해석한다."""
    if not value:
        return None

    filename = portable_basename(value)
    if not filename:
        return None

    current_path = Path(GENERATED_DIR) / filename
    if current_path.is_file():
        return str(current_path)

    stored_path = Path(str(value))
    if stored_path.is_absolute() and stored_path.is_file():
        return str(stored_path)
    return None


def resolve_session_generated_file(key):
    """세션 경로를 이동 가능한 파일명으로 바꾸고 오래된 참조를 제거한다."""
    stored_value = session.get(key)
    resolved_path = resolve_generated_file(stored_value)
    if resolved_path:
        portable_value = portable_basename(resolved_path)
        if stored_value != portable_value:
            session[key] = portable_value
        return resolved_path

    if stored_value:
        session.pop(key, None)
    return None


def save_json_cache(
    prefix,
    data,
):
    """
    상품 검색 결과처럼 크기가 큰 데이터를
    Flask 세션 쿠키 대신 JSON 파일로 저장한다.
    """

    filename = (
        f"{prefix}_"
        f"{uuid.uuid4().hex[:12]}"
        ".json"
    )

    file_path = os.path.join(
        PRODUCT_CACHE_DIR,
        filename,
    )

    with open(
        file_path,
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=2,
        )

    return filename


def load_json_cache(
    filename,
    default=None,
):
    """
    저장된 상품 JSON 캐시 파일을 읽는다.
    """

    if default is None:
        default = {}

    if not filename:
        return default

    safe_filename = portable_basename(filename)

    file_path = os.path.join(
        PRODUCT_CACHE_DIR,
        safe_filename,
    )

    if not os.path.exists(
        file_path
    ):
        return default

    try:
        with open(
            file_path,
            "r",
            encoding="utf-8",
        ) as file:
            return json.load(
                file
            )

    except (
        OSError,
        json.JSONDecodeError,
    ) as exc:
        print(
            "[product-cache] "
            f"캐시 읽기 실패: {exc}"
        )

        return default


def load_session_json_cache(
    key,
    default=None,
):
    """세션의 JSON 캐시를 읽고 존재하지 않는 파일 참조는 제거한다."""
    if default is None:
        default = {}

    filename = session.get(key)
    if not filename:
        return default

    safe_filename = portable_basename(filename)
    file_path = os.path.join(
        PRODUCT_CACHE_DIR,
        safe_filename,
    )
    if not os.path.isfile(file_path):
        session.pop(key, None)
        return default

    if filename != safe_filename:
        session[key] = safe_filename
    return load_json_cache(
        safe_filename,
        default=default,
    )


def remove_cache_file(
    filename,
):
    """
    더 이상 사용하지 않는 상품 캐시를 삭제한다.
    """

    if not filename:
        return

    safe_filename = portable_basename(filename)

    # A saved history entry owns an immutable reference to this product
    # snapshot. Do not remove it when a later pipeline run replaces the
    # current session's selected-products cache.
    if (
        SavedDesign.query
        .filter_by(
            selected_products_file=(
                safe_filename
            )
        )
        .first()
        is not None
    ):
        return

    file_path = os.path.join(
        PRODUCT_CACHE_DIR,
        safe_filename,
    )

    try:
        if os.path.exists(
            file_path
        ):
            os.remove(
                file_path
            )

    except OSError as exc:
        print(
            "[product-cache] "
            f"캐시 삭제 실패: {exc}"
        )


def product_recommendation_mood_key():
    """현재 무드 조건을 상품 추천 캐시 식별자로 변환한다."""
    payload = {
        "prompt": str(
            session.get(
                "mood_prompt",
                "",
            )
        ).strip().lower(),
        "tags": sorted(
            str(tag).strip().lower()
            for tag
            in session.get(
                "style_tags",
                [],
            )
            if str(tag).strip()
        ),
        "image": str(
            session.get(
                "selected_mood_image",
                "",
            )
        ).strip(),
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def search_serpapi_shopping(
    query,
    display=5,
    item_type=None,
):
    """SerpApi Google Shopping을 호출해 기존 상품 형식으로 반환한다."""
    provider = (
        furniture_recommender
        .SerpApiShoppingProvider()
    )
    products = provider.search(
        query,
        display=display,
    )

    if item_type:
        products = [
            product
            for product
            in products
            if product_matches_furniture_type(
                product,
                item_type,
            )
        ]

    return products


def product_matches_furniture_type(
    product,
    item_type,
):
    """검색 상품이 요청한 가구 유형과 실제로 일치하는지 확인한다."""
    category_text = " ".join(
        str(
            product.get(
                f"category{index}",
                "",
            )
            or ""
        ).lower()
        for index
        in range(1, 5)
    )
    title = str(
        product.get(
            "title",
            "",
        )
        or ""
    ).lower()

    category_terms = {
        "bed": (" 침대 ", "침대프레임"),
        "sofa": ("소파",),
        "chair": ("의자",),
        "desk": ("책상",),
        "table": ("테이블", "식탁"),
        "bench": ("벤치",),
        "shelf": ("선반",),
        "cabinet": ("수납장",),
        "dresser": ("서랍장",),
        "wardrobe": ("옷장", "장롱"),
        "lamp": ("조명", "스탠드"),
        "rug": ("러그", "카페트"),
        "plant": ("식물", "화분"),
    }
    title_terms = {
        "bed": (
            "침대프레임",
            "침대 프레임",
            "bed frame",
            "bedframe",
        ),
        "sofa": ("소파", "sofa"),
        "chair": ("의자", "체어", "chair"),
        "desk": ("책상", "데스크", "desk"),
        "table": ("테이블", "식탁", "table"),
        "bench": ("벤치", "bench"),
        "shelf": ("선반", "shelf"),
        "cabinet": ("수납장", "캐비닛", "cabinet"),
        "dresser": ("서랍장", "dresser"),
        "wardrobe": ("옷장", "장롱", "wardrobe"),
        "lamp": ("조명", "램프", "스탠드", "lamp"),
        "rug": ("러그", "카페트", "rug"),
        "plant": ("식물", "화분", "plant"),
    }
    excluded_terms = {
        "bed": (
            "담요",
            "이불",
            "베개",
            "쿠션",
            "커버",
            "패드",
            "매트리스",
            "토퍼",
        ),
    }

    if any(
        term in title
        for term
        in excluded_terms.get(
            item_type,
            (),
        )
    ):
        return False

    category_match = any(
        term in f" {category_text} "
        for term
        in category_terms.get(
            item_type,
            (),
        )
    )
    title_match = any(
        term in title
        for term
        in title_terms.get(
            item_type,
            (),
        )
    )
    return category_match or title_match


def translate_furniture_label(
    item_type,
    original_label,
    fallback_number,
):
    """
    Model1이 반환한 영어 가구 이름을
    화면에 표시할 한글 이름으로 변환한다.
    """

    type_names = {
        "bed": "침대",
        "single_bed": "싱글 침대",
        "desk": "책상",
        "chair": "의자",
        "floor_chair": "좌식 의자",
        "stool": "스툴",
        "table": "테이블",
        "low_table": "낮은 테이블",
        "nightstand": "협탁",
        "side_table": "협탁",
        "tv_stand": "TV장",
        "shelf": "선반",
        "cabinet": "수납장",
        "dresser": "서랍장",
        "wardrobe": "옷장",
        "rug": "러그",
        "mirror": "거울",
        "lamp": "조명",
        "table_lamp": "탁상 조명",
        "floor_lamp": "스탠드 조명",
        "plant": "식물",
        "sofa": "소파",
        "couch": "소파",
    }

    label_aliases = {
        "single bed": "싱글 침대",
        "bed": "침대",
        "nightstand": "협탁",
        "side table": "협탁",
        "tv stand": "TV장",
        "table lamp": "탁상 조명",
        "floor lamp": "스탠드 조명",
        "low table": "낮은 테이블",
        "desk": "책상",
        "chair": "의자",
        "rug": "러그",
        "plant": "식물",
        "shelf": "선반",
        "cabinet": "수납장",
    }

    label = str(
        original_label
        or ""
    ).strip()

    lower_label = (
        label
        .lower()
        .replace(
            "_",
            " ",
        )
    )

    if lower_label in label_aliases:
        return label_aliases[
            lower_label
        ]

    normalized_type = str(
        item_type
        or "unknown"
    ).lower()

    if (
        not label
        or lower_label
        == normalized_type.replace(
            "_",
            " ",
        )
    ):
        return type_names.get(
            normalized_type,
            f"가구 {fallback_number}",
        )

    return label


def _mood_v1_results_to_urls(
    results,
):
    """
    예전 mood_library 검색 결과의 경로를
    브라우저용 URL로 변환한다.
    """

    return [
        {
            "url": url_for(
                "mood_library_image",
                filename=result[
                    "path"
                ],
            ),
            "path": result[
                "path"
            ],
            "score": result.get(
                "score"
            ),
            "mood_id": result.get(
                "mood_id"
            ),
            "mood_name_ko": (
                result.get(
                    "mood_name_ko"
                )
            ),
            "filename": result.get(
                "filename"
            ),
            "mood_scores": result.get(
                "mood_scores",
                {},
            ),
        }
        for result in results
    ]


# ──────────────────────────────────────────────────────
# HOME / 시작
# ──────────────────────────────────────────────────────
@app.route("/")
def index():
    """서비스의 첫 화면을 표시한다."""
    return render_template(
        "index.html"
    )


@app.route("/gallery")
def gallery():
    """무드 이미지 갤러리를 수집해 화면에 표시한다."""
    from mood_pipeline.preprocess import (
        collect_image_paths,
    )

    try:
        paths = collect_image_paths(
            MOOD_IMAGE_ROOT
        )

    except Exception:
        paths = []

    images = [
        str(
            path.relative_to(
                MOOD_IMAGE_ROOT
            )
        ).replace(
            "\\",
            "/",
        )
        for path in paths[:60]
    ]

    return render_template(
        "gallery.html",
        images=images,
        total=len(paths),
    )


@app.route("/my-designs")
@login_required
def my_designs():
    """현재 사용자가 저장한 디자인 목록을 표시한다."""
    designs = (
        SavedDesign.query
        .filter_by(
            user_id=current_user.id
        )
        .order_by(
            SavedDesign.created_at.desc()
        )
        .all()
    )
    return render_template(
        "my_designs.html",
        designs=designs,
    )


@app.route("/my-designs/<int:design_id>")
@login_required
def design_detail(design_id):
    """선택한 저장 디자인의 상세 정보와 결과물을 표시한다."""
    design = db.session.get(
        SavedDesign,
        design_id,
    )
    if (
        design is None
        or design.user_id
        != current_user.id
    ):
        abort(404)

    furniture_choices = parse_saved_json(
        design.furniture_choices_json,
        [],
    )
    if not isinstance(
        furniture_choices,
        list,
    ):
        furniture_choices = []

    purchase_types = parse_saved_json(
        design.purchase_items_json,
        [],
    )
    if not isinstance(
        purchase_types,
        list,
    ):
        purchase_types = []

    selected_products = load_json_cache(
        design.selected_products_file,
        default=[],
    )
    if not isinstance(
        selected_products,
        list,
    ):
        selected_products = []

    return render_template(
        "result.html",
        readonly=True,
        saved_design=design,
        generated_file=design.generated_file,
        description=design.description,
        tags=parse_saved_json(
            design.tags_json,
            [],
        ),
        original_floorplan_file=(
            design.original_floorplan_file
        ),
        modified_floorplan_file=(
            design.modified_floorplan_file
        ),
        modified_svg_markup=read_generated_svg(
            design.modified_floorplan_file
        ),
        furniture_choices=furniture_choices,
        purchase_items=build_purchase_items(
            purchase_types
        ),
        selected_products=selected_products,
    )


@app.route(
    "/my-designs/<int:design_id>/delete",
    methods=["POST"],
)
@login_required
def delete_design(design_id):
    """현재 사용자가 소유한 저장 디자인을 삭제한다."""
    design = db.session.get(
        SavedDesign,
        design_id,
    )
    if (
        design is None
        or design.user_id
        != current_user.id
    ):
        abort(404)

    db.session.delete(design)
    db.session.commit()
    return redirect(
        url_for("my_designs")
    )


@app.route("/about")
def about():
    """서비스 소개 화면을 표시한다."""
    return render_template(
        "about.html"
    )


def clear_design_session():
    """로그인 상태를 유지하면서 디자인 작업 세션만 초기화한다."""
    login_state = {
        key: session[key]
        for key in (
            "_user_id",
            "_fresh",
            "_id",
        )
        if key in session
    }
    session.clear()
    session.update(
        login_state
    )


@app.route("/start")
def start():
    """이전 디자인 세션을 비우고 무드 입력 단계로 이동한다."""
    clear_design_session()

    return redirect(
        url_for(
            "prompt"
        )
    )


# ──────────────────────────────────────────────────────
# STEP 1: 프롬프트 입력 및 무드 이미지 선택
# ──────────────────────────────────────────────────────
@app.route("/prompt")
def prompt():
    """사용자에게 원하는 인테리어 무드를 입력받는 화면을 표시한다."""
    return render_template(
        "prompt.html",
        previews=[],
    )


@app.route(
    "/save-style",
    methods=["POST"],
)
def save_style():
    """선택한 스타일 태그와 무드 문장을 세션에 저장한다."""
    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    prompt_text = (
        data.get(
            "prompt"
        )
        or ""
    ).strip()

    tags = (
        data.get(
            "tags"
        )
        or []
    )

    selected_image = (
        data.get(
            "selected_image"
        )
        or ""
    ).strip()

    if not prompt_text:
        return jsonify(
            {
                "ok": False,
                "error": (
                    "원하는 인테리어 분위기를 "
                    "입력해 주세요."
                ),
            }
        ), 400

    if not selected_image:
        return jsonify(
            {
                "ok": False,
                "error": (
                    "추천 이미지 중 하나를 "
                    "선택해 주세요."
                ),
            }
        ), 400

    if not isinstance(
        tags,
        list,
    ):
        tags = []

    session[
        "mood_prompt"
    ] = prompt_text

    session[
        "style_tags"
    ] = tags

    session[
        "selected_mood_image"
    ] = selected_image

    return jsonify(
        {
            "ok": True,
            "redirect": url_for(
                "upload"
            ),
        }
    )


@app.route("/mood-search")
def mood_search_api():
    """입력 문장과 유사한 무드 이미지를 검색해 JSON으로 반환한다."""
    query = (
        request.args.get(
            "q"
        )
        or ""
    ).strip()

    if not query:
        return jsonify(
            {
                "ok": True,
                "results": [],
            }
        )

    try:
        search_result = (
            mood_search_v1
            .search_mood_with_images(
                query,
                top_k=5,
            )
        )

        recommended_images = (
            search_result.get(
                "recommended_images",
                [],
            )
        )

        return jsonify(
            {
                "ok": True,
                "results": (
                    _mood_v1_results_to_urls(
                        recommended_images
                    )
                ),
                "selected_mood": (
                    search_result.get(
                        "selected_mood"
                    )
                ),
                "prompt_en": (
                    search_result.get(
                        "prompt_en"
                    )
                ),
                "translated": (
                    search_result.get(
                        "translated",
                        False,
                    )
                ),
                "detected_axes": (
                    search_result.get(
                        "detected_axes",
                        [],
                    )
                ),
                "detected_concepts": (
                    search_result.get(
                        "detected_concepts",
                        [],
                    )
                ),
                "needs_selection": (
                    search_result.get(
                        "needs_selection",
                        False,
                    )
                ),
            }
        )

    except Exception as exc:
        print(
            "[mood-search] "
            f"검색 실패: {exc}"
        )

        return jsonify(
            {
                "ok": False,
                "error": (
                    "무드 검색 중 오류가 "
                    "발생했습니다."
                ),
            }
        ), 500


@app.route(
    "/mood-image/<path:filename>"
)
def mood_image(
    filename,
):
    """원본 무드 이미지 파일을 안전하게 전달한다."""
    from flask import (
        send_from_directory,
    )

    return send_from_directory(
        str(
            MOOD_IMAGE_ROOT
        ),
        filename,
    )


@app.route(
    "/mood-library-image/<path:filename>"
)
def mood_library_image(
    filename,
):
    """생성된 무드 라이브러리 이미지 파일을 안전하게 전달한다."""
    from flask import (
        send_from_directory,
    )

    return send_from_directory(
        str(
            MOOD_LIBRARY_DIR
        ),
        filename,
    )


# ──────────────────────────────────────────────────────
# STEP 2: 사진 업로드 및 방 크기 입력
# ──────────────────────────────────────────────────────
@app.route(
    "/upload",
    methods=[
        "GET",
        "POST",
    ],
)
def upload():
    """방 사진과 실측 정보를 입력받아 업로드 단계를 처리한다."""
    if (
        request.method
        == "GET"
        and "mood_prompt"
        not in session
    ):
        return redirect(
            url_for(
                "prompt"
            )
        )

    if request.method == "POST":
        room_width_raw = (
            request.form.get(
                "room_width"
            )
            or ""
        ).strip()

        room_depth_raw = (
            request.form.get(
                "room_depth"
            )
            or ""
        ).strip()

        ceiling_height_raw = (
            request.form.get(
                "ceiling_height"
            )
            or ""
        ).strip()

        # 세 항목은 각각 선택이다. 가로·세로 중 한 변만 있어도
        # Scene Graph가 사진에서 읽은 비율로 나머지 변을 계산한다.
        parsed_dimensions = {}

        for name, raw, low, high in (
            ("room_width", room_width_raw, 0.5, 30.0),
            ("room_depth", room_depth_raw, 0.5, 30.0),
            ("ceiling_height", ceiling_height_raw, 1.8, 6.0),
        ):
            if raw == "":
                parsed_dimensions[name] = None
                continue

            try:
                value = float(
                    raw
                )

            except ValueError:
                return jsonify(
                    {
                        "ok": False,
                        "error": (
                            "방 크기를 숫자로 "
                            "입력해 주세요."
                        ),
                    }
                ), 400

            if not low <= value <= high:
                return jsonify(
                    {
                        "ok": False,
                        "error": (
                            f"방 크기는 {low}~{high}m "
                            "범위로 입력해 주세요."
                        ),
                    }
                ), 400

            parsed_dimensions[name] = value

        room_width = parsed_dimensions[
            "room_width"
        ]
        room_depth = parsed_dimensions[
            "room_depth"
        ]
        ceiling_height = parsed_dimensions[
            "ceiling_height"
        ]

        file = request.files.get(
            "photo"
        )

        if (
            not file
            or file.filename == ""
        ):
            return jsonify(
                {
                    "ok": False,
                    "error": (
                        "방 사진을 선택해 주세요."
                    ),
                }
            ), 400

        if not allowed_file(
            file.filename
        ):
            return jsonify(
                {
                    "ok": False,
                    "error": (
                        "JPG/PNG 파일만 "
                        "업로드 가능합니다."
                    ),
                }
            ), 400

        ext = (
            file.filename
            .rsplit(
                ".",
                1,
            )[1]
            .lower()
        )

        saved_name = (
            "upload_"
            f"{uuid.uuid4().hex[:10]}"
            f".{ext}"
        )

        file.save(
            os.path.join(
                UPLOAD_DIR,
                saved_name,
            )
        )

        session[
            "uploaded_file"
        ] = saved_name

        session[
            "original_filename"
        ] = file.filename

        for key, value in (
            ("room_width", room_width),
            ("room_depth", room_depth),
            ("ceiling_height", ceiling_height),
        ):
            if value is None:
                session.pop(
                    key,
                    None,
                )
            else:
                session[
                    key
                ] = value

        for key in [
            "detected_furniture",
            "floorplan_layout_file",
            "original_floorplan_file",
            "modified_layout_file",
            "modified_floorplan_file",
            "furniture_choices",
            "purchase_items",
            "generated_file",
            "ai_description",
        ]:
            session.pop(
                key,
                None,
            )

        remove_cache_file(
            session.pop(
                "product_candidates_file",
                None,
            )
        )

        remove_cache_file(
            session.pop(
                "selected_products_file",
                None,
            )
        )

        return jsonify(
            {
                "ok": True,
                "redirect": url_for(
                    "loading"
                ),
            }
        )

    return render_template(
        "upload.html"
    )


# ──────────────────────────────────────────────────────
# STEP 3: AI 분석 로딩 / 평면도
# ──────────────────────────────────────────────────────
@app.route("/loading")
def loading():
    """업로드 이후 평면도 생성 대기 화면을 표시한다."""
    if (
        "uploaded_file"
        not in session
    ):
        return redirect(
            url_for(
                "upload"
            )
        )

    return render_template(
        "loading.html"
    )


def carry_floorplan_to_result() -> str | None:
    """/floorplan 결과를 /result 가 읽는 자리로 그대로 넘긴다.

    [임시] 평소에는 POST /generate-design 이 선택 상품을 반영해
    modified_floorplan_file 을 만든다. 그 단계를 건너뛰는 동안에는 사용자가
    /floorplan 에서 편집한 SVG(없으면 생성 원본)를 그대로 결과 평면도로 쓴다.
    파일을 새로 만들지 않고 세션 키만 이어 붙이므로 Gemini 호출이 없다.
    """
    filename = str(
        session.get("edited_floorplan_file")
        or session.get("original_floorplan_file")
        or ""
    ).strip()
    if not filename:
        return None

    # 편집본은 업로드한 사진이 바뀌면 무효다. /floorplan 이 쓰는 판정과 맞춘다.
    if (
        session.get("edited_floorplan_file")
        and str(session.get("edited_floorplan_upload") or "")
        != str(session.get("uploaded_file") or "")
    ):
        filename = str(
            session.get("original_floorplan_file") or ""
        ).strip()
        if not filename:
            return None

    if not os.path.isfile(
        os.path.join(
            GENERATED_DIR,
            os.path.basename(filename),
        )
    ):
        return None

    session["modified_floorplan_file"] = filename
    return filename


def floorplan_cache_enabled() -> bool:
    """평면도 생성 결과를 재사용할지 여부.

    끄면 같은 사진이어도 Gemini를 매번 다시 호출한다. 무료 등급은 RPD가
    빠듯하므로(호출 2번 = 평면도 1장) 모델 비교 같은 때만 끄는 것이 좋다.
    """
    return os.getenv(
        "FLOORPLAN_CACHE",
        "1",
    ).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def current_room_plan():
    """세션에 담긴 방 실측치 → 면적·평수 요약. 없으면 (None, False).

    /floorplan 과 /preview-3d 가 같은 치수를 써야 하므로 한 곳에 모아둔다.
    """
    room_width = session.get(
        "room_width"
    )

    room_depth = session.get(
        "room_depth"
    )

    ceiling_height = session.get(
        "ceiling_height"
    )

    # 천장 높이는 없어도 된다(기본 2.4m). 가로·세로 중 한 변만 있는 경우는
    # 여기서 면적을 낼 수 없으니 Scene Graph가 비율로 나머지를 채운다.
    dimensions_provided = (
        room_width is not None
        and room_depth is not None
    )

    if not dimensions_provided:
        return (
            {"ceiling_m": ceiling_height}
            if ceiling_height is not None
            else None
        ), False

    area_sqm = (
        room_width
        * room_depth
    )

    return {
        "area_sqm": round(
            area_sqm,
            1,
        ),
        "area_pyeong": round(
            area_sqm
            / 3.3058,
            1,
        ),
        "width_m": room_width,
        "depth_m": room_depth,
        "ceiling_m": ceiling_height,
    }, True


@app.route("/floorplan")
def floorplan():
    """업로드한 방 사진으로 평면도를 생성하고 편집 화면을 표시한다."""
    if (
        "uploaded_file"
        not in session
    ):
        return redirect(
            url_for(
                "upload"
            )
        )

    room_width = session.get(
        "room_width"
    )

    room_depth = session.get(
        "room_depth"
    )

    ceiling_height = session.get(
        "ceiling_height"
    )

    # 천장 높이는 없어도 된다(기본 2.4m). 가로·세로 중 한 변만 있는 경우는
    # 여기서 면적을 낼 수 없으니 Scene Graph가 비율로 나머지를 채운다.
    dimensions_provided = (
        room_width is not None
        and room_depth is not None
    )

    plan = (
        {"ceiling_m": ceiling_height}
        if ceiling_height is not None
        else None
    )

    if dimensions_provided:
        area_sqm = (
            room_width
            * room_depth
        )

        plan = {
            "area_sqm": round(
                area_sqm,
                1,
            ),
            "area_pyeong": round(
                area_sqm
                / 3.3058,
                1,
            ),
            "width_m": (
                room_width
            ),
            "depth_m": (
                room_depth
            ),
            "ceiling_m": (
                ceiling_height
            ),
        }

    svg_markup = None
    floorplan_error = None
    floorplan_status = 200
    scene_3d = None
    review = None

    upload_path = os.path.join(
        UPLOAD_DIR,
        session[
            "uploaded_file"
        ],
    )

    try:
        with floorplan_generation_lock:
            active_floorplan_model = (
                model2_floorplan
                if os.getenv(
                    "FLOORPLAN_PROVIDER",
                    "model2_gemini_svg",
                ).strip().lower()
                == "model2_gemini_svg"
                else floorplan_model
            )
            result = (
                active_floorplan_model
                .generate_floorplan_for_web(
                    upload_path,
                    GENERATED_DIR,
                    skip_existing=(
                        floorplan_cache_enabled()
                    ),
                    room_width=(
                        room_width
                    ),
                    room_depth=(
                        room_depth
                    ),
                )
            )

        layout_file = result.get(
            "layout_file"
        )
        saved_edit_layout = (
            resolve_session_generated_file(
                "edited_floorplan_layout_file"
            )
            or ""
        )
        saved_edit_upload = str(
            session.get("edited_floorplan_upload")
            or ""
        )
        if (
            saved_edit_layout
            and saved_edit_upload
            == str(session.get("uploaded_file") or "")
            and os.path.isfile(saved_edit_layout)
            and saved_edit_matches(
                saved_edit_layout,
                layout_file,
            )
        ):
            layout_file = saved_edit_layout

        if layout_file:
            session[
                "floorplan_layout_file"
            ] = portable_basename(
                layout_file
            )

        svg_path = result.get(
            "svg_path"
        )

        edited_floorplan_file = str(
            session.get("edited_floorplan_file")
            or ""
        )
        edited_for_upload = str(
            session.get("edited_floorplan_upload")
            or ""
        )
        edited_floorplan_path = os.path.join(
            GENERATED_DIR,
            edited_floorplan_file,
        )
        reuse_edited_floorplan = (
            bool(edited_floorplan_file)
            and edited_for_upload
            == str(session.get("uploaded_file") or "")
            and os.path.isfile(edited_floorplan_path)
        )

        if reuse_edited_floorplan:
            svg_path = edited_floorplan_path
            result["svg_markup"] = Path(
                edited_floorplan_path
            ).read_text(encoding="utf-8")
        elif svg_path:
            session[
                "original_floorplan_file"
            ] = os.path.basename(
                svg_path
            )

        detected_furniture = []

        for index, obj in enumerate(
            result.get(
                "objects",
                [],
            )
        ):
            item_type = str(
                obj.get(
                    "type"
                )
                or "unknown"
            ).lower()

            if item_type in {
                "door",
                "window",
            }:
                continue

            label = (
                translate_furniture_label(
                    item_type,
                    obj.get(
                        "label"
                    ),
                    index + 1,
                )
            )

            source_index = (
                obj.get(
                    "source_index"
                )
            )

            if source_index is None:
                source_index = index

            detected_furniture.append(
                {
                    "id": (
                        f"furniture_{index}"
                    ),
                    "label": label,
                    "type": item_type,
                    "source_index": (
                        source_index
                    ),
                }
            )

        session[
            "detected_furniture"
        ] = detected_furniture

        svg_markup = result.get(
            "svg_markup"
        )
        editable_layout = None

        if svg_markup and layout_file:
            try:
                editable_layout = json.loads(
                    Path(layout_file).read_text(
                        encoding="utf-8"
                    )
                )
                if scene_graph.is_scene_graph(
                    editable_layout
                ):
                    # Scene Graph면 저장된 SVG 대신 그래프에서 다시 그린다.
                    # 그래프가 유일한 원본이라 이게 항상 3D와 같은 그림이다.
                    svg_markup = (
                        model2_floorplan
                        .render_floorplan_svg(
                            editable_layout,
                            GENERATED_DIR,
                        )
                    )
                    detected = detected_furniture_from_graph(
                        editable_layout
                    )
                    review = floorplan_review_data(
                        editable_layout
                    )
                    session[
                        "detected_furniture"
                    ] = detected
                    if "furniture_choices" in session:
                        sync_furniture_choices(
                            detected
                        )
                svg_markup = (
                    model2_floorplan
                    .prepare_floorplan_edit_markup(
                        svg_markup,
                        editable_layout,
                    )
                )
            except Exception as edit_prepare_exc:
                print(
                    "[floorplan-edit] "
                    f"편집용 SVG 준비 실패: {edit_prepare_exc}"
                )

        # 3D 배치 확인용 씬 데이터. Gemini 재호출은 없다.
        # 2D와 같은 좌표를 쓰려고 rule_based_svg 배치 파이프라인을 통과시키는데,
        # 그 안에서 방 크기 전역을 바꾸므로 평면도 생성과 같은 락을 잡는다.
        if editable_layout is not None:
            try:
                with floorplan_generation_lock:
                    scene_3d = (
                        floorplan_3d
                        .build_scene(
                            editable_layout,
                            plan,
                        )
                    )
            except Exception as scene_exc:
                print(
                    "[floorplan-3d] "
                    f"씬 데이터 생성 실패: {scene_exc}"
                )

    except Exception as exc:
        floorplan_error = str(
            exc
        )

        # google-genai 예외는 status_code가 아니라 code에 HTTP 상태를 담는다.
        gemini_status = (
            api_status_code(
                exc
            )
            or getattr(
                exc,
                "status_code",
                None,
            )
        )

        if isinstance(
            exc,
            GeminiBusyError,
        ):
            floorplan_error = (
                "AI 서버가 일시적으로 혼잡합니다. "
                "잠시 후 다시 시도해 주세요."
            )
            floorplan_status = 503

        elif gemini_status == 429:
            floorplan_status = 429

        print(
            "[floorplan] "
            f"평면도 생성 실패: {exc}"
        )

        try:
            yolo_items = (
                detect_furniture_from_image(
                    upload_path
                )
            )

            session[
                "detected_furniture"
            ] = [
                {
                    "id": (
                        f"yolo_{index}"
                    ),
                    "label": (
                        item_name
                    ),
                    "type": (
                        detected_item_to_type(
                            item_name
                        )
                    ),
                    "source_index": (
                        None
                    ),
                }
                for index, item_name
                in enumerate(
                    yolo_items
                )
            ]

        except Exception as yolo_exc:
            print(
                "[floorplan] "
                "YOLO 대체 탐지 실패: "
                f"{yolo_exc}"
            )

            session[
                "detected_furniture"
            ] = []

    return (
        render_template(
            "floorplan.html",
            plan=plan,
            review=review,
            dimensions_provided=(
                dimensions_provided
            ),
            svg_markup=svg_markup,
            floorplan_error=(
                floorplan_error
            ),
            scene_3d=scene_3d,
        ),
        floorplan_status,
    )


@app.post("/floorplan/save-edit")
def save_floorplan_edit():
    """사용자가 편집한 평면도 SVG와 배치 정보를 저장한다."""
    if "uploaded_file" not in session:
        return jsonify(
            {"ok": False, "error": "업로드된 방 사진이 없습니다."}
        ), 400
    payload = request.get_json(silent=True) or {}
    svg_markup = str(payload.get("svg") or "")
    if not svg_markup or len(svg_markup.encode("utf-8")) > 3_000_000:
        return jsonify(
            {"ok": False, "error": "저장할 평면도 데이터가 올바르지 않습니다."}
        ), 400
    try:
        sanitized = model2_floorplan.sanitize_floorplan_edit_svg(
            svg_markup
        )
        filename = f"edited_floorplan_{uuid.uuid4().hex[:16]}.svg"
        output_path = Path(GENERATED_DIR) / filename
        output_path.write_text(
            sanitized,
            encoding="utf-8",
        )
        layout_path = (
            resolve_session_generated_file(
                "floorplan_layout_file"
            )
            or ""
        )
        refreshed_svg = None
        refreshed_scene = None
        if layout_path and os.path.isfile(layout_path):
            current_layout = json.loads(
                Path(layout_path).read_text(encoding="utf-8")
            )
            edited_layout = (
                model2_floorplan
                .apply_floorplan_edits_to_layout(
                    sanitized,
                    current_layout,
                )
            )
            edited_layout_path = (
                Path(GENERATED_DIR)
                / f"edited_layout_{uuid.uuid4().hex[:16]}.json"
            )
            edited_layout_path.write_text(
                json.dumps(
                    edited_layout,
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            session["edited_floorplan_layout_file"] = (
                edited_layout_path.name
            )
            session["floorplan_layout_file"] = (
                edited_layout_path.name
            )

            # Scene Graph면 브라우저가 보낸 SVG 대신 보정된 그래프로 다시 그려
            # 저장한다. 보정기가 다른 가구를 비켜 줬을 수 있어, 그대로 두면
            # 화면의 2D와 저장된 배치(=3D)가 다시 어긋난다.
            if scene_graph.is_scene_graph(
                edited_layout
            ):
                refreshed_svg = (
                    model2_floorplan
                    .prepare_floorplan_edit_markup(
                        model2_floorplan
                        .render_floorplan_svg(
                            edited_layout,
                            GENERATED_DIR,
                        ),
                        edited_layout,
                    )
                )
                output_path.write_text(
                    refreshed_svg,
                    encoding="utf-8",
                )
                plan, _ = current_room_plan()
                refreshed_scene = (
                    floorplan_3d.build_scene(
                        edited_layout,
                        plan,
                    )
                )
        session["edited_floorplan_file"] = filename
        session["edited_floorplan_upload"] = str(
            session.get("uploaded_file")
            or ""
        )
        session["original_floorplan_file"] = filename
        return jsonify(
            {
                "ok": True,
                "filename": filename,
                # 화면을 저장된 배치와 맞추도록 새 2D·3D를 돌려준다
                "svg": refreshed_svg,
                "scene_3d": refreshed_scene,
                "uncertain": (
                    scene_edit.uncertain_objects(
                        edited_layout
                    )
                    if refreshed_svg
                    else []
                ),
                "adjustments": (
                    [
                        item
                        for item in (
                            edited_layout.get(
                                "solver_adjustments"
                            )
                            or []
                        )
                        if item.get("units") == "m"
                    ][-10:]
                    if refreshed_svg
                    else []
                ),
            }
        )
    except Exception as exc:
        print(f"[floorplan-edit] 저장 실패: {exc}")
        return jsonify(
            {"ok": False, "error": f"평면도 수정 저장에 실패했습니다: {exc}"}
        ), 400


# ──────────────────────────────────────────────────────
# STEP 4: 기존 가구 유지·제거 /
# 구매할 가구 종류 선택
# ──────────────────────────────────────────────────────
def save_scene_graph_as_edit(graph):
    """편집된 Scene Graph를 새 파일로 저장하고 세션이 그걸 보게 한다.

    /floorplan/save-edit와 같은 세션 키를 쓴다. 어느 화면에서 고쳤든 다음 단계
    (유지·제거, 상품 추가, 3D)가 같은 파일을 읽어야 한다.
    """
    token = uuid.uuid4().hex[:16]
    layout_path = Path(GENERATED_DIR) / f"edited_layout_{token}.json"
    layout_path.write_text(
        json.dumps(graph, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    markup = model2_floorplan.prepare_floorplan_edit_markup(
        model2_floorplan.render_floorplan_svg(graph, GENERATED_DIR),
        graph,
    )
    svg_name = f"edited_floorplan_{token}.svg"
    (Path(GENERATED_DIR) / svg_name).write_text(markup, encoding="utf-8")
    session["edited_floorplan_layout_file"] = layout_path.name
    session["floorplan_layout_file"] = layout_path.name
    session["edited_floorplan_file"] = svg_name
    session["edited_floorplan_upload"] = str(session.get("uploaded_file") or "")
    session["original_floorplan_file"] = svg_name
    return markup


@app.post("/api/scene/edit")
def edit_scene():
    """2D·3D·검토 패널의 편집을 Scene Graph에 반영하고 새 2D·3D를 돌려준다.

    body: {"ops": [...scene_edit 연산...], "context": "floorplan" | "final"}
      floorplan — 평면도 화면. 3D는 기준 배치로 만든다.
      final     — 결과·3D 미리보기 화면. 3D는 유지·제거·상품이 반영된 배치로 만든다.
    """
    if "uploaded_file" not in session:
        return jsonify({"ok": False, "error": "업로드된 방 사진이 없습니다."}), 400
    payload = request.get_json(silent=True) or {}
    ops = payload.get("ops")
    context = "final" if payload.get("context") == "final" else "floorplan"
    base_path = resolve_session_generated_file("floorplan_layout_file") or ""
    if not base_path or not os.path.isfile(base_path):
        return jsonify({"ok": False, "error": "평면도 배치 정보가 없습니다."}), 400
    base = json.loads(Path(base_path).read_text(encoding="utf-8"))
    if not scene_graph.is_scene_graph(base):
        return jsonify({"ok": False, "error": "이 평면도는 편집을 지원하지 않습니다. 사진을 다시 올려 주세요."}), 400
    if not isinstance(ops, list):
        return jsonify({"ok": False, "error": "편집 내용이 비어 있습니다."}), 400

    base_ids = {str(o.get("id")) for o in base.get("objects") or []}
    base_ops = [op for op in ops if isinstance(op, dict) and (op.get("op") == "add" or str(op.get("id")) in base_ids)]
    product_ops = [op for op in ops if isinstance(op, dict) and op not in base_ops]
    # 기준 배치에 없는 id는 수정 평면도의 상품이어야 한다. 아니면 잘못된 요청이다
    product_ids = set()
    modified_path = resolve_session_generated_file("modified_layout_file") or ""
    if product_ops and modified_path and os.path.isfile(modified_path):
        product_ids = {
            str(o.get("id"))
            for o in json.loads(Path(modified_path).read_text(encoding="utf-8")).get("objects") or []
            if o.get("source") == "selected_product"
        }
    if any(str(op.get("id")) not in product_ids for op in product_ops):
        return jsonify({"ok": False, "error": "해당 가구를 찾을 수 없습니다."}), 400
    overrides = dict(session.get("product_overrides") or {})
    for op in product_ops:
        # 상품은 수정 평면도에만 있다. 위치·방향만 따로 기억했다가 다시 만들 때 적용한다
        if op.get("op") not in {"move", "rotate"}:
            return jsonify({"ok": False, "error": "선택한 상품은 이동과 회전만 할 수 있습니다."}), 400
        entry = dict(overrides.get(str(op.get("id"))) or {})
        try:
            if op["op"] == "move":
                entry.update(cx=float(op["cx"]), cy=float(op["cy"]))
            else:
                entry.update(rotation_deg=float(op["rotation_deg"]) % 360)
        except (KeyError, TypeError, ValueError):
            return jsonify({"ok": False, "error": "편집 값이 올바르지 않습니다."}), 400
        overrides[str(op.get("id"))] = entry
    session["product_overrides"] = overrides

    try:
        graph, changed = (
            scene_edit.apply_ops(base, base_ops)
            if base_ops
            else (base, [])
        )
    except scene_edit.EditError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    return jsonify(
        scene_update_payload(
            graph,
            changed + [str(op.get("id")) for op in product_ops],
            context,
            saved=bool(base_ops),
        )
    )


def scene_update_payload(graph, changed, context, *, saved):
    """편집·재분석 뒤 화면을 갱신할 데이터. 2D(편집용), 수정 평면도, 3D, 확인 목록."""
    markup = save_scene_graph_as_edit(graph) if saved else (
        model2_floorplan.prepare_floorplan_edit_markup(
            model2_floorplan.render_floorplan_svg(graph, GENERATED_DIR),
            graph,
        )
    )
    detected = detected_furniture_from_graph(graph)
    session["detected_furniture"] = detected
    if "furniture_choices" in session:
        sync_furniture_choices(detected)

    modified_markup = None
    if session.get("modified_layout_file"):
        svg_filename = create_modified_floorplan(
            session.get("furniture_choices") or [],
            load_session_json_cache("selected_products_file", default=[]) or [],
        )
        if svg_filename:
            session["modified_floorplan_file"] = svg_filename
            modified_markup = read_generated_svg(svg_filename)

    plan, _ = current_room_plan()
    scene_layout = graph
    if context == "final":
        final_path, _ = resolve_final_layout_path()
        if final_path and os.path.isfile(final_path):
            scene_layout = json.loads(Path(final_path).read_text(encoding="utf-8"))
    with floorplan_generation_lock:
        scene_3d = floorplan_3d.build_scene(scene_layout, plan)

    return {
        "ok": True,
        "changed": changed,
        "svg": markup,
        "modified_svg": modified_markup,
        "scene_3d": scene_3d,
        "review": floorplan_review_data(graph),
        "uncertain": scene_edit.uncertain_objects(graph),
        "adjustments": [
            item
            for item in graph.get("solver_adjustments") or []
            if item.get("units") == "m"
        ][-10:],
    }


def floorplan_review_data(graph):
    """평면도 화면의 확인 패널 데이터 (항목 18·10)."""
    return {
        "uncertain": scene_edit.uncertain_objects(graph),
        "has_door": any(
            o.get("type") == "door"
            for o in graph.get("objects") or []
        ),
        "notes": {
            str(o["id"]): o.get("refined_note")
            for o in graph.get("objects") or []
            if o.get("refined_note")
        },
        "types": [
            [kind, label]
            for kind, label in scene_edit.KOREAN_LABELS.items()
            if kind not in {"door", "window"}
        ],
    }


def object_parts_enabled():
    return os.getenv("GEMINI_OBJECT_PARTS", "1").strip().lower() not in {"0", "false", "no", "off"}


@app.post("/api/scene/parts")
def scene_object_parts():
    """가구별 3D 형태(부품 목록)를 돌려준다. three.js는 배치만 하고 모양은 이걸로 그린다.

    캐시에 없는 가구만 Gemini에 한 번에 묻는다. 위치·회전은 캐시 키에 없어서 편집 뒤에
    다시 불러도 호출이 늘지 않고, 새로 들어온 상품만 묻는다.
    """
    if "uploaded_file" not in session or not object_parts_enabled():
        return jsonify({"ok": True, "object_parts": {}})
    payload = request.get_json(silent=True) or {}
    context = "final" if payload.get("context") == "final" else "floorplan"
    if context == "final":
        layout_path, _ = resolve_final_layout_path()
    else:
        layout_path = resolve_session_generated_file("floorplan_layout_file")
    if not layout_path or not os.path.isfile(layout_path):
        return jsonify({"ok": True, "object_parts": {}})
    layout = json.loads(Path(layout_path).read_text(encoding="utf-8"))
    plan, _ = current_room_plan()
    with floorplan_generation_lock:
        scene = floorplan_3d.build_scene(layout, plan)
    photo = Path(UPLOAD_DIR) / os.path.basename(str(session.get("uploaded_file")))
    parts = gemini_furniture_parts.generate_object_parts(
        scene,
        GENERATED_DIR,
        room_photo=photo,
        style_prompt=_preview_style_prompt(),
    )
    return jsonify({"ok": True, "object_parts": parts})


@app.post("/api/scene/views")
def scene_object_views():
    """가구별 입체 그림(4방향). three.js는 이 그림을 Scene Graph 위치에 세운다.

    이미지 생성은 가구당 여러 번 호출해 오래 걸린다. 한 번에 두 가구씩 그리고 남은
    수를 돌려주면, 화면이 남은 가구가 없을 때까지 다시 부른다.
    """
    if "uploaded_file" not in session or not gemini_furniture_views.enabled():
        return jsonify({"ok": True, "views": {}, "remaining": 0})
    payload = request.get_json(silent=True) or {}
    context = "final" if payload.get("context") == "final" else "floorplan"
    if context == "final":
        layout_path, _ = resolve_final_layout_path()
    else:
        layout_path = resolve_session_generated_file("floorplan_layout_file")
    if not layout_path or not os.path.isfile(layout_path):
        return jsonify({"ok": True, "views": {}, "remaining": 0})
    layout = json.loads(Path(layout_path).read_text(encoding="utf-8"))
    plan, _ = current_room_plan()
    with floorplan_generation_lock:
        scene = floorplan_3d.build_scene(layout, plan)
    photo = Path(UPLOAD_DIR) / os.path.basename(str(session.get("uploaded_file")))
    result = gemini_furniture_views.generate_object_views(
        scene,
        GENERATED_DIR,
        room_photo=photo,
        url_prefix=url_for("static", filename="generated").rstrip("/"),
        max_new=2,
        # 2D 평면도의 Gemini 가구 그림을 기준으로 3D 그림을 그린다(사진과 닮게)
        artwork=model2_floorplan.load_artwork(layout, GENERATED_DIR),
    )
    return jsonify({"ok": True, **result})


@app.post("/api/scene/reanalyze")
def reanalyze_scene():
    """확신이 낮은 가구만 Gemini로 다시 분석한다 (항목 17). 방 전체를 다시 묻지 않는다."""
    if "uploaded_file" not in session:
        return jsonify({"ok": False, "error": "업로드된 방 사진이 없습니다."}), 400
    payload = request.get_json(silent=True) or {}
    ids = payload.get("ids")
    ids = [str(i) for i in ids] if isinstance(ids, list) and ids else None
    base_path = resolve_session_generated_file("floorplan_layout_file") or ""
    if not base_path or not os.path.isfile(base_path):
        return jsonify({"ok": False, "error": "평면도 배치 정보가 없습니다."}), 400
    base = json.loads(Path(base_path).read_text(encoding="utf-8"))
    if not scene_graph.is_scene_graph(base):
        return jsonify({"ok": False, "error": "이 평면도는 다시 확인을 지원하지 않습니다."}), 400
    if not gemini_reanalyze.targets(base, ids):
        return jsonify({"ok": False, "error": "다시 확인할 가구가 없습니다."}), 400
    photo = Path(UPLOAD_DIR) / os.path.basename(str(session.get("uploaded_file")))
    model = (
        os.getenv("GEMINI_LAYOUT_MODEL", "").strip()
        or os.getenv("GEMINI_ANALYSIS_MODEL", "").strip()
        or model2_floorplan.DEFAULT_ANALYSIS_MODEL
    )
    try:
        client = model2_floorplan._client()
        graph, changed = call_with_retry(
            gemini_reanalyze.reanalyze,
            client,
            photo,
            base,
            model=model,
            ids=ids,
            description="가구 다시 확인",
        )
    except GeminiBusyError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503
    except Exception as exc:
        print(f"[reanalyze] 실패: {exc}")
        return jsonify({"ok": False, "error": f"다시 확인하지 못했습니다: {exc}"}), 500
    context = "final" if payload.get("context") == "final" else "floorplan"
    return jsonify(scene_update_payload(graph, changed, context, saved=bool(changed)))


@app.route(
    "/furniture-choice"
)
def furniture_choice():
    """탐지된 기존 가구의 유지·제거·교체 선택 화면을 표시한다.

    [임시] 가구 선택과 상품 추천 단계를 건너뛰고 /floorplan 다음에 바로
    /result 로 보낸다. 라우트 자체는 남겨 둬야 템플릿의
    url_for("furniture_choice")가 BuildError 없이 동작한다.
    아래 return 두 줄만 지우면 원래 화면으로 돌아온다.
    """
    carry_floorplan_to_result()
    return redirect(url_for("result"))

    if (
        "mood_prompt"
        not in session
    ):
        return redirect(
            url_for(
                "prompt"
            )
        )

    if (
        "uploaded_file"
        not in session
    ):
        return redirect(
            url_for(
                "upload"
            )
        )

    furniture_items = session.get(
        "detected_furniture",
        [],
    )

    purchase_options = [
        {
            "value": item_type,
            "label": label,
        }
        for item_type, label
        in PURCHASE_LABELS.items()
    ]

    return render_template(
        "furniture_choice.html",
        furniture_items=(
            furniture_items
        ),
        purchase_options=(
            purchase_options
        ),
    )


def default_furniture_choices():
    """탐지된 가구 목록으로 기본 유지 선택값을 생성한다."""
    return [
        {
            "id": item.get(
                "id"
            ),
            "item": item.get(
                "label"
            ),
            "type": item.get(
                "type"
            ),
            "source_index": (
                item.get(
                    "source_index"
                )
            ),
            "scene_id": item.get(
                "scene_id"
            ),
            "decision": "keep",
        }
        for item in session.get(
            "detected_furniture",
            [],
        )
    ]


# ──────────────────────────────────────────────────────
# STEP 5: 종류별 Google Shopping 상품 추천 및 선택
# ──────────────────────────────────────────────────────
def parse_price_filter_value(raw):
    """쉼표가 포함될 수 있는 가격 입력을 0 이상의 정수로 변환한다."""
    if raw is None:
        return None
    raw_text = str(raw).strip().replace(",", "")
    if not raw_text:
        return None
    try:
        value = int(raw_text)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def price_within_filter(price, price_min, price_max):
    """상품 가격이 사용자가 지정한 최소·최대 범위에 포함되는지 확인한다."""
    if not price:
        return False
    if price_min is not None and price < price_min:
        return False
    if price_max is not None and price > price_max:
        return False
    return True


@app.route(
    "/product-selection",
    methods=[
        "GET",
        "POST",
    ],
)
def product_selection():
    """가구 유형별 추천 상품을 검색해 선택 화면에 표시한다.

    [임시] furniture_choice와 같은 이유로 /result 로 보낸다.
    아래 return 두 줄만 지우면 원래 화면으로 돌아온다.
    """
    carry_floorplan_to_result()
    return redirect(url_for("result"))

    if (
        "mood_prompt"
        not in session
    ):
        return redirect(
            url_for(
                "prompt"
            )
        )

    if (
        "uploaded_file"
        not in session
    ):
        return redirect(
            url_for(
                "upload"
            )
        )

    if (
        "furniture_choices"
        not in session
    ):
        session[
            "furniture_choices"
        ] = (
            default_furniture_choices()
        )

    recommendation_session_id = session.get(
        "recommendation_session_id"
    )
    if not recommendation_session_id:
        recommendation_session_id = uuid.uuid4().hex
        session["recommendation_session_id"] = recommendation_session_id

    if request.args.get("refresh") == "1":
        session["product_request_round"] = (
            int(session.get("product_request_round", 0))
            + 1
        )
        stale_candidates = session.pop(
            "product_candidates_file",
            None,
        )
        remove_cache_file(stale_candidates)

    if request.method == "POST":
        price_min = parse_price_filter_value(
            request.form.get("price_min")
        )
        price_max = parse_price_filter_value(
            request.form.get("price_max")
        )
        if (
            price_min is not None
            and price_max is not None
            and price_min > price_max
        ):
            price_min, price_max = price_max, price_min
        session["product_price_min"] = price_min
        session["product_price_max"] = price_max

        has_decision_fields = any(
            key.startswith(
                "decision_"
            )
            for key
            in request.form.keys()
        )

        # The product picker POST has no decision_* fields. Preserve the
        # choices from the furniture page instead of resetting them to keep.
        if has_decision_fields:
            furniture_choices = []

            for item in session.get(
                "detected_furniture",
                [],
            ):
                item_id = item.get(
                    "id"
                )

                decision = (
                    request.form.get(
                        f"decision_{item_id}",
                        "keep",
                    )
                )

                if decision not in {
                    "keep",
                    "remove",
                    "replace",
                }:
                    decision = "keep"

                furniture_choices.append(
                    {
                        "id": item_id,
                        "item": item.get(
                            "label"
                        ),
                        "type": item.get(
                            "type"
                        ),
                        "source_index": (
                            item.get(
                                "source_index"
                            )
                        ),
                        "decision": (
                            decision
                        ),
                    }
                )

        else:
            furniture_choices = session.get(
                "furniture_choices",
                default_furniture_choices(),
            )

        purchase_items = [
            item
            for item
            in request.form.getlist(
                "purchase_items"
            )
            if item
            in PURCHASE_LABELS
        ]

        # A replacement removes the detected item and automatically opens
        # recommendations for the same furniture category on the next page.
        for choice in furniture_choices:
            replacement_type = choice.get(
                "type"
            )

            if (
                choice.get(
                    "decision"
                )
                == "replace"
                and replacement_type
                in PURCHASE_LABELS
                and replacement_type
                not in purchase_items
            ):
                purchase_items.append(
                    replacement_type
                )

        session[
            "furniture_choices"
        ] = furniture_choices

        session[
            "purchase_items"
        ] = purchase_items

        old_candidates = (
            session.pop(
                "product_candidates_file",
                None,
            )
        )

        old_selected = (
            session.pop(
                "selected_products_file",
                None,
            )
        )

        remove_cache_file(
            old_candidates
        )

        remove_cache_file(
            old_selected
        )

    purchase_items = session.get(
        "purchase_items",
        [],
    )
    price_min = parse_price_filter_value(
        session.get("product_price_min")
    )
    price_max = parse_price_filter_value(
        session.get("product_price_max")
    )
    price_filter_active = (
        price_min is not None
        or price_max is not None
    )

    cached_data = load_json_cache(
        session.get(
            "product_candidates_file"
        ),
        default={},
    )

    cached_types = cached_data.get(
        "purchase_types",
        [],
    )

    current_mood_key = (
        product_recommendation_mood_key()
    )

    cached_mood_key = cached_data.get(
        "mood_key"
    )

    recommendation_version = 16
    cached_recommendation_version = (
        cached_data.get(
            "recommendation_version"
        )
    )

    product_groups = cached_data.get(
        "groups",
        [],
    )

    cached_search_failed = any(
        group.get("error")
        for group in product_groups
    )

    if (
        cached_types != purchase_items
        or cached_mood_key
        != current_mood_key
        or cached_recommendation_version
        != recommendation_version
        or cached_search_failed
    ):
        product_groups = []
        mood_analysis = (
            furniture_recommender
            .analyze_mood_context(
                str(
                    session.get(
                        "mood_prompt",
                        "",
                    )
                ),
                [
                    str(tag)
                    for tag
                    in session.get(
                        "style_tags",
                        [],
                    )
                ],
                str(
                    session.get(
                        "selected_mood_image",
                        "",
                    )
                ),
            )
        )
        selected_image_path = None
        selected_relative_path = str(
            session.get(
                "selected_mood_image",
                "",
            )
            or ""
        ).strip()
        if selected_relative_path:
            try:
                library_root = (
                    MOOD_LIBRARY_DIR.resolve()
                )
                candidate_image_path = (
                    MOOD_LIBRARY_DIR
                    / selected_relative_path
                ).resolve()
                candidate_image_path.relative_to(
                    library_root
                )
                if candidate_image_path.is_file():
                    selected_image_path = (
                        candidate_image_path
                    )
            except (
                OSError,
                ValueError,
            ) as image_path_exc:
                print(
                    "[product-recommendation] "
                    "선택 이미지 경로 확인 실패: "
                    f"{image_path_exc}"
                )

        if (
            selected_image_path
            and os.getenv(
                "GEMINI_MOOD_ANALYSIS_ENABLED",
                "1",
            ).strip().lower()
            not in {
                "0",
                "false",
                "off",
            }
        ):
            mood_analysis = (
                furniture_recommender
                .enrich_mood_analysis_with_gemini(
                    mood_analysis,
                    str(
                        session.get(
                            "mood_prompt",
                            "",
                        )
                    ),
                    selected_image_path,
                    os.path.join(
                        PRODUCT_CACHE_DIR,
                        "gemini_mood_analysis",
                    ),
                )
            )

        observed = {
            "colors": list(
                mood_analysis.get(
                    "colors",
                    [],
                )
            ),
            "materials": list(
                mood_analysis.get(
                    "materials",
                    [],
                )
            ),
            "forms": list(
                mood_analysis.get(
                    "forms",
                    [],
                )
            ),
        }
        print(
            "[product-recommendation] "
            f"primary_mood={mood_analysis.get('primary_mood')} "
            f"mood_scores={mood_analysis.get('mood_scores')} "
            f"observed={observed}"
        )

        provider = (
            furniture_recommender
            .SerpApiShoppingProvider()
        )
        image_similarity_service = None
        if (
            selected_image_path
            and os.getenv(
                "PRODUCT_CLIP_ENABLED",
                "1",
            ).strip().lower()
            not in {
                "0",
                "false",
                "off",
            }
        ):
            image_similarity_service = (
                furniture_recommender
                .ClipImageSimilarityService(
                    os.path.join(
                        PRODUCT_CACHE_DIR,
                        "clip_product_embeddings",
                    )
                )
            )

        shown_product_ids = set(
            str(product_id)
            for product_id
            in session.get(
                "shown_product_ids",
                [],
            )
            if str(product_id)
        )
        request_round = int(
            session.get(
                "product_request_round",
                0,
            )
        )

        # 아래 루프는 가구 종류마다 SerpApi를 한 번씩 부른다. 순차로 돌면
        # 종류 수 x 12초(실측 중앙값)가 그대로 대기 시간이 되어, 13종을 고르면
        # 2분 30초를 넘긴다. 질의 생성은 규칙 기반이라 API를 쓰지 않으므로
        # 먼저 모든 질의를 만들어 한꺼번에 병렬로 받아 둔다. 루프 자체의 순서와
        # 누적 상태는 건드리지 않으므로 추천 결과는 달라지지 않는다.
        try:
            provider.prefetch(
                [
                    query
                    for item_type in purchase_items
                    for query in (
                        furniture_recommender
                        .generate_search_queries(
                            furniture_recommender
                            .normalize_category(
                                item_type
                            ),
                            dict(
                                mood_analysis.get(
                                    "mood_scores",
                                    {},
                                )
                            ),
                            observed,
                            str(
                                recommendation_session_id
                            ),
                            request_round,
                        )
                    )
                ]
            )
        except Exception as prefetch_exc:
            print(
                "[product-selection] "
                "사전 조회 실패, 순차 조회로 진행: "
                f"{prefetch_exc}"
            )

        for item_type in purchase_items:
            label = (
                PURCHASE_LABELS.get(
                    item_type,
                    item_type,
                )
            )

            products = []
            generated_queries = []
            error_message = None

            try:
                (
                    products,
                    shown_product_ids,
                    generated_queries,
                ) = (
                    furniture_recommender
                    .recommend_furniture(
                        item_type,
                        dict(
                            mood_analysis.get(
                                "mood_scores",
                                {},
                            )
                        ),
                        observed,
                        selected_image_path,
                        str(
                            recommendation_session_id
                        ),
                        request_round,
                        shown_product_ids,
                        provider=provider,
                        image_similarity_service=(
                            image_similarity_service
                        ),
                        final_limit=(
                            32
                            if price_filter_active
                            else 8
                        ),
                    )
                )

                if price_filter_active:
                    products = [
                        product
                        for product in products
                        if price_within_filter(
                            product.get("price"),
                            price_min,
                            price_max,
                        )
                    ][:8]

            except Exception as exc:
                error_message = str(
                    exc
                )

                print(
                    "[product-selection] "
                    f"{label} 검색 실패: "
                    f"{exc}"
                )

            product_groups.append(
                {
                    "type": (
                        item_type
                    ),
                    "label": label,
                    "query": " / ".join(
                        generated_queries
                    ),
                    "queries": (
                        generated_queries
                    ),
                    "products": (
                        products
                    ),
                    "error": (
                        error_message
                    ),
                }
            )

        session["shown_product_ids"] = sorted(
            shown_product_ids
        )[-500:]

        cache_data = {
            "purchase_types": (
                purchase_items
            ),
            "mood_key": (
                current_mood_key
            ),
            "recommendation_version": (
                recommendation_version
            ),
            "mood_analysis": (
                mood_analysis
            ),
            "request_round": (
                request_round
            ),
            "groups": (
                product_groups
            ),
        }

        cache_filename = (
            save_json_cache(
                "product_candidates",
                cache_data,
            )
        )

        session[
            "product_candidates_file"
        ] = cache_filename

    return render_template(
        "product_selection.html",
        product_groups=(
            product_groups
        ),
        purchase_items=(
            purchase_items
        ),
        purchase_options=[
            {
                "value": (
                    item_type
                ),
                "label": label,
            }
            for item_type, label
            in PURCHASE_LABELS.items()
        ],
        replacement_choices=[
            choice
            for choice in session.get(
                "furniture_choices",
                [],
            )
            if choice.get(
                "decision"
            )
            == "replace"
        ],
        original_svg_markup=(
            read_generated_svg(
                session.get(
                    "original_floorplan_file"
                )
            )
        ),
        price_min=price_min,
        price_max=price_max,
        price_filter_active=price_filter_active,
    )


# ──────────────────────────────────────────────────────
# 수정 평면도 생성
# ──────────────────────────────────────────────────────
def read_generated_svg(
    filename,
):
    """생성 결과 폴더의 SVG 파일을 읽어 문자열로 반환한다."""
    if not filename:
        return None

    path = os.path.join(
        GENERATED_DIR,
        os.path.basename(
            filename
        ),
    )

    if not os.path.exists(
        path
    ):
        return None

    try:
        with open(
            path,
            "r",
            encoding="utf-8",
        ) as file:
            return file.read()

    except OSError:
        return None


def create_modified_floorplan(
    furniture_choices,
    selected_products,
):
    """가구 선택과 구매 상품을 반영한 수정 평면도를 생성한다."""
    layout_path = resolve_session_generated_file(
        "floorplan_layout_file"
    )

    if not layout_path:
        print(
            "[generate-design] "
            "원본 layout 파일 경로가 "
            "없습니다."
        )

        return None

    if not os.path.isabs(
        layout_path
    ):
        candidates = [
            os.path.join(
                PROJECT_ROOT,
                layout_path,
            ),
            os.path.join(
                GENERATED_DIR,
                layout_path,
            ),
        ]

        layout_path = next(
            (
                candidate
                for candidate
                in candidates
                if os.path.exists(
                    candidate
                )
            ),
            layout_path,
        )

    if not os.path.exists(
        layout_path
    ):
        print(
            "[generate-design] "
            "원본 layout 파일을 "
            "찾을 수 없습니다: "
            f"{layout_path}"
        )

        return None

    try:
        with open(
            layout_path,
            "r",
            encoding="utf-8",
        ) as file:
            layout = json.load(
                file
            )

        remove_indices = set()

        for choice in furniture_choices:
            if (
                choice.get(
                    "decision"
                )
                not in {
                    "remove",
                    "replace",
                }
            ):
                continue

            target_index = choice_object_index(
                choice,
                layout.get(
                    "objects",
                    [],
                ),
            )

            if target_index is None:
                continue

            remove_indices.add(
                target_index
            )

        original_objects = (
            layout.get(
                "objects",
                [],
            )
        )

        modified_objects = [
            obj
            for index, obj
            in enumerate(
                original_objects
            )
            if index
            not in remove_indices
        ]

        for order, product in enumerate(
            selected_products
        ):
            item_type = product.get(
                "type"
            )

            if (
                item_type
                not in PURCHASE_LABELS
            ):
                continue

            replacement_choice = next(
                (
                    choice
                    for choice
                    in furniture_choices
                    if choice.get(
                        "decision"
                    )
                    == "replace"
                    and choice.get(
                        "type"
                    )
                    == item_type
                ),
                None,
            )

            replacement_object = None

            if replacement_choice:
                replacement_index = choice_object_index(
                    replacement_choice,
                    original_objects,
                )
                replacement_object = (
                    original_objects[
                        replacement_index
                    ]
                    if replacement_index is not None
                    and 0 <= replacement_index < len(original_objects)
                    else None
                )

            if replacement_object:
                x = replacement_object.get(
                    "x",
                    0.5,
                )

                y = replacement_object.get(
                    "y",
                    0.5,
                )

            else:
                x, y = PURCHASE_POSITIONS[
                    order
                    % len(
                        PURCHASE_POSITIONS
                    )
                ]

            marker = product.get(
                "marker",
                order + 1,
            )

            wall_map = {
                "desk": "top",
                "shelf": "left",
                "cabinet": "left",
            }

            modified_objects.append(
                {
                    "type": (
                        item_type
                    ),
                    "label": (
                        PURCHASE_LABELS[
                            item_type
                        ]
                    ),
                    "x": x,
                    "y": y,
                    "w": (
                        replacement_object.get(
                            "w",
                            0.0,
                        )
                        if replacement_object
                        else 0.0
                    ),
                    "h": (
                        replacement_object.get(
                            "h",
                            0.0,
                        )
                        if replacement_object
                        else 0.0
                    ),
                    "wall": (
                        wall_map.get(
                            item_type,
                            "none",
                        )
                    ),
                    "confidence": (
                        1.0
                    ),
                    "source": (
                        "selected_product"
                    ),
                    "product_link": (
                        product.get(
                            "link"
                        )
                    ),
                    "product_title": (
                        product.get(
                            "title"
                        )
                    ),
                    "product_marker": (
                        marker
                    ),
                    # 상품 실측 치수·형태 속성(항목 6·7·14). Scene Graph와 3D가 이 값으로
                    # 상품 크기와 모양을 정한다
                    **product_geometry(
                        product,
                        replacement_object,
                    ),
                    # Scene Graph면 교체 대상의 위치·크기·방향·벽을 그대로 물려받는다.
                    # legacy 값만 넘기면 벽 방향을 고정 맵(wall_map)에서 다시 정해서
                    # 교체한 가구가 엉뚱한 쪽을 보던 문제가 있었다.
                    **(
                        {
                            key: replacement_object[key]
                            for key in (
                                "cx",
                                "cy",
                                "rotation_deg",
                                "wall",
                            )
                            + (
                                ()
                                if product_has_real_size(product)
                                else ("w_m", "d_m")
                            )
                            if key in replacement_object
                        }
                        if replacement_object
                        and scene_graph.is_scene_graph(
                            layout
                        )
                        else {}
                    ),
                }
            )

        modified_layout = {
            **layout,
            "objects": (
                modified_objects
            ),
        }

        # Scene Graph면 새로 끼운 상품에도 미터 좌표·id를 채운다.
        # 크기가 0으로 들어온 상품은 타입별 표준 크기를 쓴다.
        if scene_graph.is_scene_graph(
            modified_layout
        ):
            modified_layout = scene_graph.ensure(
                modified_layout
            )
            # 교체 상품이 실측 크기로 커지면 원래 자리에서 옆 가구와 겹칠 수 있다.
            # 상품만 움직여 자리를 맞추고, 기존 가구는 그대로 둔다.
            from model2.placement_solver import solve as solve_placement

            solve_placement(
                modified_layout,
                movable_ids=[
                    str(obj.get("id"))
                    for obj in modified_layout["objects"]
                    if obj.get("source") == "selected_product"
                ],
                walkway=False,
            )
            # 결과 화면의 3D에서 사용자가 옮긴 상품 위치를 다시 적용한다.
            # 수정 평면도는 선택이 바뀔 때마다 새로 만들어지므로 따로 들고 있어야 한다.
            overrides = session.get(
                "product_overrides"
            ) or {}
            for obj in modified_layout["objects"]:
                override = overrides.get(
                    str(obj.get("id"))
                )
                if (
                    override
                    and obj.get("source")
                    == "selected_product"
                ):
                    obj.update(
                        {
                            key: float(value)
                            for key, value in override.items()
                            if key in {"cx", "cy", "rotation_deg"}
                        }
                    )
            modified_layout = scene_graph.sync_legacy(
                modified_layout
            )
            scene_graph.append_history(
                modified_layout,
                "user",
                "modify_furniture",
                removed=sorted(
                    remove_indices
                ),
                products=len(
                    selected_products
                ),
            )

        token = (
            uuid.uuid4()
            .hex[:10]
        )

        layout_filename = (
            "modified_layout_"
            f"{token}.json"
        )

        svg_filename = (
            "modified_floorplan_"
            f"{token}.svg"
        )

        modified_layout_path = (
            os.path.join(
                GENERATED_DIR,
                layout_filename,
            )
        )

        modified_svg_path = (
            os.path.join(
                GENERATED_DIR,
                svg_filename,
            )
        )

        with open(
            modified_layout_path,
            "w",
            encoding="utf-8",
        ) as file:
            json.dump(
                modified_layout,
                file,
                ensure_ascii=False,
                indent=2,
            )

        original_floorplan_file = session.get(
            "original_floorplan_file"
        )
        original_svg_path = (
            os.path.join(
                GENERATED_DIR,
                os.path.basename(
                    original_floorplan_file
                ),
            )
            if original_floorplan_file
            else None
        )
        use_model2_svg = (
            os.getenv(
                "FLOORPLAN_PROVIDER",
                "model2_gemini_svg",
            ).strip().lower()
            == "model2_gemini_svg"
            and original_svg_path
            and os.path.exists(
                original_svg_path
            )
        )

        if use_model2_svg:
            model2_floorplan.create_modified_svg(
                original_svg_path,
                layout,
                modified_layout,
                remove_indices=remove_indices,
                selected_products=selected_products,
                output_path=modified_svg_path,
            )
        else:
            rule_based_svg.save_svg(
                modified_layout,
                modified_svg_path,
                title=(
                    "추천 가구가 반영된 "
                    "평면도"
                ),
            )

        session[
            "modified_layout_file"
        ] = layout_filename

        return svg_filename

    except Exception as exc:
        print(
            "[generate-design] "
            "수정 평면도 생성 실패: "
            f"{exc}"
        )

        return None


# ──────────────────────────────────────────────────────
# STEP 6: 선택한 상품으로 결과 생성
# ──────────────────────────────────────────────────────
@app.route(
    "/generate-design",
    methods=["POST"],
)
def generate_design():
    """최종 선택 상품을 반영해 디자인 결과와 수정 평면도를 생성한다."""
    if (
        "mood_prompt"
        not in session
    ):
        return redirect(
            url_for(
                "prompt"
            )
        )

    if (
        "uploaded_file"
        not in session
    ):
        return redirect(
            url_for(
                "upload"
            )
        )

    furniture_choices = session.get(
        "furniture_choices",
        [],
    )

    purchase_items = session.get(
        "purchase_items",
        [],
    )

    candidates_data = load_json_cache(
        session.get(
            "product_candidates_file"
        ),
        default={},
    )

    product_groups = (
        candidates_data.get(
            "groups",
            [],
        )
    )

    selected_products = []

    for group in product_groups:
        item_type = group.get(
            "type"
        )

        if (
            item_type
            not in purchase_items
        ):
            continue

        products = group.get(
            "products",
            [],
        )

        selected_index_raw = (
            request.form.get(
                "selected_product_"
                f"{item_type}"
            )
        )

        if (
            selected_index_raw
            is None
        ):
            continue

        try:
            selected_index = int(
                selected_index_raw
            )

        except (
            TypeError,
            ValueError,
        ):
            continue

        if not (
            0
            <= selected_index
            < len(
                products
            )
        ):
            continue

        selected_product = dict(
            products[
                selected_index
            ]
        )

        selected_product[
            "type"
        ] = item_type

        selected_product[
            "label"
        ] = PURCHASE_LABELS.get(
            item_type,
            item_type,
        )

        selected_product[
            "marker"
        ] = (
            len(
                selected_products
            )
            + 1
        )

        selected_products.append(
            selected_product
        )

    if (
        purchase_items
        and len(
            selected_products
        )
        != len(
            purchase_items
        )
    ):
        return redirect(
            url_for(
                "product_selection"
            )
        )

    # Analyze only the final selected shopping photos. Gemini extracts detailed
    # top-view attributes once and caches them; quota/network failures fall
    # back to the existing local color-and-silhouette analyzer.
    selected_products = (
        model2_floorplan
        .enrich_products_with_visual_profiles(
            selected_products,
            PRODUCT_CACHE_DIR,
        )
    )

    old_selected_file = (
        session.pop(
            "selected_products_file",
            None,
        )
    )

    remove_cache_file(
        old_selected_file
    )

    selected_filename = (
        save_json_cache(
            "selected_products",
            selected_products,
        )
    )

    session[
        "selected_products_file"
    ] = selected_filename

    modified_floorplan_file = (
        create_modified_floorplan(
            furniture_choices,
            selected_products,
        )
    )

    if modified_floorplan_file:
        session[
            "modified_floorplan_file"
        ] = (
            modified_floorplan_file
        )

    else:
        session.pop(
            "modified_floorplan_file",
            None,
        )

    prompt_text = session[
        "mood_prompt"
    ]

    tags = session.get(
        "style_tags",
        [],
    )

    upload_path = os.path.join(
        UPLOAD_DIR,
        session[
            "uploaded_file"
        ],
    )

    generated_filename = (
        ai_backend
        .generate_interior_image(
            upload_path=(
                upload_path
            ),
            output_dir=(
                GENERATED_DIR
            ),
            prompt_text=(
                prompt_text
            ),
            tags=tags,
        )
    )

    description = (
        ai_backend
        .generate_description(
            tags,
            prompt_text,
        )
    )

    kept_count = sum(
        choice.get(
            "decision"
        )
        == "keep"
        for choice
        in furniture_choices
    )

    removed_count = sum(
        choice.get(
            "decision"
        )
        == "remove"
        for choice
        in furniture_choices
    )

    replaced_count = sum(
        choice.get(
            "decision"
        )
        == "replace"
        for choice
        in furniture_choices
    )

    description += (
        f" 기존 가구 {kept_count}개를 "
        f"유지하고 {removed_count}개를 제거했으며 "
        f"{replaced_count}개를 변경하도록 선택했습니다."
    )

    if selected_products:
        product_names = ", ".join(
            product.get(
                "label",
                product.get(
                    "type",
                    "가구",
                ),
            )
            for product
            in selected_products
        )

        description += (
            " 선택한 추천 가구는 "
            f"{product_names}입니다."
        )

    session[
        "generated_file"
    ] = generated_filename

    session[
        "ai_description"
    ] = description

    return redirect(
        url_for(
            "result"
        )
    )


@app.route(
    "/toggle-furniture",
    methods=["POST"],
)
def toggle_furniture():
    """가구 유지·제거·교체 상태를 변경하고 수정 평면도를 갱신한다."""
    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    target_scene_id = str(
        data.get("scene_id") or ""
    )
    try:
        target_source_index = int(
            data.get(
                "source_index"
            )
        )

    except (
        TypeError,
        ValueError,
    ):
        target_source_index = None

    if target_source_index is None and not target_scene_id:
        return jsonify(
            {
                "ok": False,
                "error": (
                    "잘못된 가구 식별자"
                ),
            }
        ), 400

    decision = data.get(
        "decision"
    )

    if decision not in (
        "keep",
        "remove",
        "replace",
    ):
        return jsonify(
            {
                "ok": False,
                "error": (
                    "잘못된 선택값"
                ),
            }
        ), 400

    furniture_choices = (
        session.get(
            "furniture_choices",
            [],
        )
    )

    found = False

    for choice in furniture_choices:
        # scene id가 있으면 그걸로 찾는다. 검토 패널에서 가구를 지우거나 추가하면
        # 순번(source_index)이 밀리기 때문이다.
        if (
            str(choice.get("scene_id") or "")
            == target_scene_id
            if target_scene_id
            and choice.get("scene_id")
            else choice.get(
                "source_index"
            )
            == target_source_index
        ):
            choice[
                "decision"
            ] = decision

            found = True
            break

    if not found:
        return jsonify(
            {
                "ok": False,
                "error": (
                    "가구를 찾을 수 없습니다"
                ),
            }
        ), 404

    session[
        "furniture_choices"
    ] = furniture_choices

    selected_products = (
        load_session_json_cache(
            "selected_products_file",
            default=[],
        )
    )

    svg_filename = (
        create_modified_floorplan(
            furniture_choices,
            selected_products,
        )
    )

    if not svg_filename:
        return jsonify(
            {
                "ok": False,
                "error": (
                    "수정 평면도 생성 실패"
                ),
            }
        ), 500

    session[
        "modified_floorplan_file"
    ] = svg_filename

    return jsonify(
        {
            "ok": True,
            # 결과 화면의 3D도 같은 배치로 바꾼다(항목 13·20)
            "scene_3d": final_scene_3d(),
            "svg_markup": (
                read_generated_svg(
                    svg_filename
                )
            ),
            "decision": decision,
        }
    )


@app.route(
    "/search-products",
    methods=["GET"],
)
def search_products():
    """검색어와 필터 조건으로 추가 상품을 검색해 JSON으로 반환한다."""
    query = (
        request.args.get(
            "q"
        )
        or ""
    ).strip()

    if not query:
        return jsonify(
            {
                "ok": False,
                "error": (
                    "검색어를 입력해 주세요."
                ),
            }
        ), 400

    try:
        products = (
            search_serpapi_shopping(
                query,
                display=6,
            )
        )
        # 고른 종류가 있으면 이 방에 실제로 맞는지 함께 보여 준다(항목 21).
        # 직접 검색은 무드 점수가 없으니 공간·크기만 합친다
        item_type = str(request.args.get("type") or "")
        mood_scores = search_mood_scores(products, item_type)
        layout, replace_id = (
            fit_layout_and_target(request.args.get("replace_id"))
            if item_type in PURCHASE_LABELS
            else (None, None)
        )
        if layout is not None:
            scorer = spatial_fit.make_scorer(layout, item_type, replace_id=replace_id)
            for product in products:
                try:
                    fit = scorer(product)
                except Exception as fit_exc:
                    print(f"[search-products] 적합도 계산 실패: {fit_exc}")
                    continue
                mood = mood_scores.get(id(product))
                if mood is None:
                    total = (
                        spatial_fit.WEIGHTS["space"] * fit["space"]
                        + spatial_fit.WEIGHTS["size"] * fit["size"]
                    ) / (spatial_fit.WEIGHTS["space"] + spatial_fit.WEIGHTS["size"])
                else:
                    total = (
                        spatial_fit.WEIGHTS["mood"] * mood
                        + spatial_fit.WEIGHTS["space"] * fit["space"]
                        + spatial_fit.WEIGHTS["size"] * fit["size"]
                    )
                product["fit"] = {
                    "mood": mood,
                    "space": fit["space"],
                    "size": fit["size"],
                    "total": round(total * (1 if fit["fits"] else 0.4), 3),
                    "fits": fit["fits"],
                    "reasons": fit["reasons"][:3],
                    "dimensions": fit["dimensions"],
                }
            # 들어가는 상품을 앞에 둔다. 같은 조건이면 검색 순서를 지킨다
            products.sort(key=lambda p: -((p.get("fit") or {}).get("total") or 0))

    except ValueError as exc:
        return jsonify(
            {
                "ok": False,
                "error": str(
                    exc
                ),
            }
        ), 503

    except Exception as exc:
        print(
            "[search-products] "
            f"검색 실패: {exc}"
        )

        return jsonify(
            {
                "ok": False,
                "error": (
                    "상품 검색에 실패했습니다."
                ),
            }
        ), 502

    return jsonify(
        {
            "ok": True,
            "products": products,
        }
    )


@app.route(
    "/add-product",
    methods=["POST"],
)
def add_product():
    """사용자가 고른 상품을 선택 목록에 추가하고 결과를 갱신한다."""
    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    item_type = data.get(
        "type"
    )

    if (
        item_type
        not in PURCHASE_LABELS
    ):
        return jsonify(
            {
                "ok": False,
                "error": (
                    "가구 종류를 선택해 주세요"
                    "(의자/책상/테이블/선반/"
                    "수납장/조명/러그/식물)."
                ),
            }
        ), 400

    title = (
        data.get(
            "title"
        )
        or ""
    ).strip()

    if not title:
        return jsonify(
            {
                "ok": False,
                "error": (
                    "상품 정보가 없습니다."
                ),
            }
        ), 400

    selected_products = (
        load_session_json_cache(
            "selected_products_file",
            default=[],
        )
    )

    marker = (
        len(
            selected_products
        )
        + 1
    )

    selected_products.append(
        {
            "type": item_type,
            "title": title,
            "link": data.get(
                "link"
            ),
            "image": data.get(
                "image"
            ),
            "price": data.get(
                "price"
            ),
            "shop": data.get(
                "shop"
            ),
            "brand": data.get(
                "brand"
            ),
            "maker": data.get(
                "maker"
            ),
            "label": PURCHASE_LABELS.get(
                item_type,
                item_type,
            ),
            "snippet": data.get(
                "snippet"
            ),
            "marker": marker,
        }
    )

    # 새로 고른 상품만 분석한다: 평면도 아이콘, 3D 형태 속성, 실제 치수.
    # 이미지 URL 기준으로 캐시되므로 같은 상품을 다시 고르면 호출이 없다.
    try:
        model2_floorplan.enrich_products_with_visual_profiles(
            selected_products[-1:],
            PRODUCT_CACHE_DIR,
        )
    except Exception as enrich_exc:
        print(
            "[add-product] "
            f"상품 분석 실패, 기본 형태로 진행: {enrich_exc}"
        )

    selected_filename = (
        save_json_cache(
            "selected_products",
            selected_products,
        )
    )

    session[
        "selected_products_file"
    ] = selected_filename

    furniture_choices = (
        session.get(
            "furniture_choices",
            [],
        )
    )

    svg_filename = (
        create_modified_floorplan(
            furniture_choices,
            selected_products,
        )
    )

    if not svg_filename:
        return jsonify(
            {
                "ok": False,
                "error": (
                    "수정 평면도 생성 실패"
                ),
            }
        ), 500

    session[
        "modified_floorplan_file"
    ] = svg_filename

    return jsonify(
        {
            "ok": True,
            # 결과 화면의 3D도 같은 배치로 바꾼다(항목 13·20)
            "scene_3d": final_scene_3d(),
            "svg_markup": (
                read_generated_svg(
                    svg_filename
                )
            ),
            "product": {
                "type": (
                    item_type
                ),
                "title": title,
                "marker": marker,
            },
        }
    )


# ──────────────────────────────────────────────────────
# 결과 화면
# ──────────────────────────────────────────────────────
@app.route("/result")
def result():
    """완성된 인테리어 디자인 결과 화면을 표시한다."""
    generated_file = session.get(
        "generated_file"
    )

    description = session.get(
        "ai_description"
    )

    tags = session.get(
        "style_tags",
        [
            "Cozy",
            "Plants",
            "Warm",
            "Vintage",
        ],
    )

    # 가구 선택 단계(/furniture-choice)를 임시로 건너뛰는 동안에도 결과 화면에서
    # 유지·제거·교체를 고를 수 있게 기본 선택(전부 유지)을 만든다.
    if (
        "furniture_choices" not in session
        and session.get("detected_furniture")
    ):
        session[
            "furniture_choices"
        ] = default_furniture_choices()

    furniture_choices = (
        session.get(
            "furniture_choices",
            [],
        )
    )

    purchase_types = session.get(
        "purchase_items",
        [],
    )

    purchase_items = build_purchase_items(
        purchase_types
    )

    selected_products = (
        load_session_json_cache(
            "selected_products_file",
            default=[],
        )
    )

    modified_file = session.get(
        "modified_floorplan_file"
    )

    modified_svg_markup = (
        read_generated_svg(
            modified_file
        )
    )

    return render_template(
        "result.html",
        generated_file=(
            generated_file
        ),
        description=description,
        tags=tags,
        original_floorplan_file=(
            session.get(
                "original_floorplan_file"
            )
        ),
        modified_floorplan_file=(
            modified_file
        ),
        modified_svg_markup=(
            modified_svg_markup
        ),
        # 한 화면에서 2D/3D 전환·편집·추천을 하도록 최종 배치의 3D 데이터를 넘긴다(항목 20)
        scene_3d=final_scene_3d(),
        purchase_labels=PURCHASE_LABELS,
        replaceable_types=sorted(
            PURCHASE_LABELS
        ),
        furniture_choices=(
            furniture_choices
        ),
        purchase_items=(
            purchase_items
        ),
        selected_products=(
            selected_products
        ),
        readonly=False,
    )


# ──────────────────────────────────────────────────────
# STEP 6: 3D 배치 확인
# ──────────────────────────────────────────────────────
def product_has_real_size(product):
    """상품 크기를 믿을 만한가. 실측이거나 규격(퀸, 3인용)으로 정한 경우."""
    dims = product.get("dimensions") or {}
    return bool(
        dims.get("measured")
        or dims.get("dimension_source") == "size_class"
    )


def product_geometry(product, replacement_object=None):
    """수정 평면도에 넣을 상품의 미터 크기·높이·형태 속성.

    교체일 때 상품 크기를 모르면(타입 기본값뿐이면) 원래 가구 크기를 유지한다.
    방에 맞게 놓여 있던 크기가 표준값보다 실제에 가깝기 때문이다.
    """
    dims = product.get("dimensions") or {}
    geometry = {
        "attrs": product.get("attributes3d") or {},
        "dimension_source": dims.get("dimension_source"),
        # 가구별 3D 형태를 만들 때 Gemini에게 보여 줄 상품 사진
        "image_file": product.get("image_file"),
    }
    if dims.get("h_m"):
        geometry["h_m"] = dims["h_m"]
    if dims.get("w_m") and dims.get("d_m") and (
        product_has_real_size(product)
        or not replacement_object
    ):
        geometry["w_m"] = dims["w_m"]
        geometry["d_m"] = dims["d_m"]
    return geometry


def saved_edit_matches(saved_path, fresh_path):
    """저장된 편집본을 이어서 써도 되는가.

    새 평면도가 Scene Graph인데 편집본이 예전 형식이면 쓰지 않는다. 섞어 쓰면
    2D와 3D가 다른 배치를 그리고 편집 API도 동작하지 않는다.
    """
    try:
        saved = json.loads(Path(saved_path).read_text(encoding="utf-8"))
        fresh = json.loads(Path(fresh_path).read_text(encoding="utf-8")) if fresh_path else {}
    except (OSError, ValueError):
        return False
    if scene_graph.is_scene_graph(fresh) and not scene_graph.is_scene_graph(saved):
        print("[floorplan] 예전 형식의 편집본은 쓰지 않고 새 평면도를 씁니다.")
        return False
    return True


def detected_furniture_from_graph(graph):
    """Scene Graph → 평면도 화면의 가구 목록. 선택은 순번이 아니라 scene id로 묶는다.

    검토 패널에서 가구를 지우거나 추가하면 순번이 밀린다. 순번으로 묶으면 엉뚱한
    가구가 제거되므로 scene_id를 같이 넣고, 수정 평면도는 scene_id를 먼저 본다.
    """
    rows = []
    for index, obj in enumerate(graph.get("objects") or []):
        item_type = str(obj.get("type") or "unknown").lower()
        if (
            obj.get("source") == "selected_product"
            or item_type in scene_graph.WALL_MOUNTED_TYPES
            or (
                obj.get("category") not in model2_floorplan.SELECTABLE_CATEGORIES
                and item_type not in model2_floorplan.SELECTABLE_TYPES
            )
        ):
            continue
        rows.append(
            {
                "id": f"furniture_{index}",
                "label": translate_furniture_label(
                    item_type,
                    obj.get("label"),
                    index + 1,
                ),
                "type": item_type,
                "source_index": index,
                "scene_id": str(obj.get("id")),
            }
        )
    return rows


def sync_furniture_choices(detected):
    """가구 목록이 바뀐 뒤 기존 유지·제거·교체 선택을 scene id 기준으로 옮긴다."""
    previous = {
        str(choice.get("scene_id")): choice.get("decision")
        for choice in session.get("furniture_choices") or []
        if choice.get("scene_id")
    }
    session["furniture_choices"] = [
        {
            "id": item["id"],
            "item": item["label"],
            "type": item["type"],
            "source_index": item["source_index"],
            "scene_id": item["scene_id"],
            "decision": previous.get(item["scene_id"], "keep"),
        }
        for item in detected
    ]


def choice_object_index(choice, objects):
    """선택 항목이 가리키는 layout 객체의 순번. scene id가 있으면 그걸로 찾는다."""
    scene_id = choice.get("scene_id")
    if scene_id:
        for index, obj in enumerate(objects):
            if str(obj.get("id") or obj.get("scene_id")) == str(scene_id):
                return index
        return None
    try:
        return int(choice.get("source_index"))
    except (TypeError, ValueError):
        return None


def final_scene_3d():
    """결과 화면의 3D 데이터. 유지·제거·상품이 반영된 최종 배치로 만든다."""
    layout_path, _ = resolve_final_layout_path()
    if not layout_path or not os.path.isfile(layout_path):
        return None
    try:
        layout = json.loads(Path(layout_path).read_text(encoding="utf-8"))
        plan, _ = current_room_plan()
        with floorplan_generation_lock:
            scene = floorplan_3d.build_scene(layout, plan)
        return scene if scene.get("objects") else None
    except Exception as exc:
        print(f"[result-3d] 씬 데이터 생성 실패: {exc}")
        return None


def fit_layout_and_target(replace_id=None):
    """적합도를 잴 방 상태와 교체 대상 id.

    최종 배치(상품 포함)를 쓰되, 교체로 표시돼 최종 배치에서 빠진 가구는 기준 배치에서
    가져와 다시 넣는다. 그래야 '그 자리에 들어가는가'를 잴 수 있다.
    """
    final_path, _ = resolve_final_layout_path()
    base_path = resolve_session_generated_file("floorplan_layout_file")
    if not final_path or not os.path.isfile(final_path):
        return None, None
    layout = json.loads(Path(final_path).read_text(encoding="utf-8"))
    if not scene_graph.is_scene_graph(layout):
        return None, None
    if replace_id and not any(str(o.get("id")) == str(replace_id) for o in layout.get("objects") or []):
        if base_path and os.path.isfile(base_path):
            base = json.loads(Path(base_path).read_text(encoding="utf-8"))
            target = next((o for o in base.get("objects") or [] if str(o.get("id")) == str(replace_id)), None)
            if target is not None:
                layout = {**layout, "objects": list(layout.get("objects") or []) + [target]}
            else:
                replace_id = None
        else:
            replace_id = None
    return layout, replace_id


def recommendation_context():
    """추천에 쓰는 무드 정보. /product-selection과 같은 방식으로 만든다."""
    mood_analysis = furniture_recommender.analyze_mood_context(
        str(session.get("mood_prompt", "")),
        [str(tag) for tag in session.get("style_tags", [])],
        str(session.get("selected_mood_image", "")),
    )
    selected_image_path = None
    relative = str(session.get("selected_mood_image", "") or "").strip()
    if relative:
        try:
            root = MOOD_LIBRARY_DIR.resolve()
            candidate = (MOOD_LIBRARY_DIR / relative).resolve()
            candidate.relative_to(root)
            if candidate.is_file():
                selected_image_path = candidate
        except (OSError, ValueError):
            selected_image_path = None
    if selected_image_path and os.getenv("GEMINI_MOOD_ANALYSIS_ENABLED", "1").strip().lower() not in {"0", "false", "off"}:
        mood_analysis = furniture_recommender.enrich_mood_analysis_with_gemini(
            mood_analysis,
            str(session.get("mood_prompt", "")),
            selected_image_path,
            os.path.join(PRODUCT_CACHE_DIR, "gemini_mood_analysis"),
        )
    observed = {
        key: list(mood_analysis.get(key, []))
        for key in ("colors", "materials", "forms")
    }
    # 무드 이미지를 고르지 않아도 CLIP을 쓴다(무드 문장↔상품 이미지)
    image_service = None
    if os.getenv("PRODUCT_CLIP_ENABLED", "1").strip().lower() not in {"0", "false", "off"}:
        image_service = furniture_recommender.ClipImageSimilarityService(
            os.path.join(PRODUCT_CACHE_DIR, "clip_product_embeddings")
        )
    return mood_analysis, observed, selected_image_path, image_service


def recommendation_provider():
    """테스트에서 바꿔 끼울 수 있게 함수로 둔다."""
    return furniture_recommender.SerpApiShoppingProvider()


def search_mood_scores(products, item_type):
    """직접 검색 결과에도 무드 적합도를 매긴다: 텍스트 스타일 + CLIP.

    예전 검색(/search-products)은 SerpApi 결과를 그대로 보여 줘서 무드와 무관했다.
    추천(/api/recommendations)과 같은 기준(무드 이미지 또는 무드 문장 CLIP)을 쓴다.
    {id(product): 0..1}을 돌려준다. 계산할 수 없으면 빈 dict.
    """
    if not products:
        return {}
    try:
        mood_analysis, observed, selected_image_path, image_service = recommendation_context()
    except Exception as exc:
        print(f"[search-products] 무드 정보를 만들지 못했습니다: {exc}")
        return {}
    mood_scores = mood_analysis.get("mood_scores", {})
    text_scores = furniture_recommender.normalize_scores(
        [
            furniture_recommender.calculate_text_style_score(product, observed, mood_scores)
            for product in products
        ]
    )
    mood_text = (
        furniture_recommender.mood_clip_text(item_type, mood_scores)
        if item_type
        else None
    )
    result = {}
    for product, text_score in zip(products, text_scores):
        clip = None
        if image_service is not None and product.get("image"):
            if selected_image_path:
                clip = image_service.similarity(selected_image_path, str(product["image"]))
            elif mood_text:
                clip = image_service.text_similarity(mood_text, str(product["image"]))
        score = text_score if clip is None else 0.6 * clip + 0.4 * text_score
        result[id(product)] = round(float(score), 3)
    return result


def public_product(item):
    keys = ("title", "link", "image", "price", "shop", "brand", "maker", "productId", "snippet", "category1", "fit")
    return {key: item.get(key) for key in keys if item.get(key) is not None}


@app.post("/api/recommendations")
def api_recommendations():
    """무드 + 실제 크기 + 방 크기 + 충돌 + 동선을 함께 본 추천 (항목 8·21).

    body: {"type": "sofa", "replace_id": "sofa_1"?}
    """
    if "uploaded_file" not in session:
        return jsonify({"ok": False, "error": "업로드된 방 사진이 없습니다."}), 400
    payload = request.get_json(silent=True) or {}
    category = str(payload.get("type") or "")
    if category not in PURCHASE_LABELS:
        return jsonify({"ok": False, "error": "추천할 수 없는 가구 종류입니다."}), 400
    layout, replace_id = fit_layout_and_target(payload.get("replace_id"))
    scorer = (
        spatial_fit.make_scorer(layout, category, replace_id=replace_id)
        if layout is not None
        else None
    )
    mood_analysis, observed, selected_image_path, image_service = recommendation_context()
    shown = set(str(i) for i in session.get("shown_product_ids", []))
    try:
        products, shown, queries = furniture_recommender.recommend_furniture(
            category,
            mood_analysis.get("mood_scores", {}),
            observed,
            selected_image_path,
            str(session.get("recommendation_session_id") or uuid.uuid4().hex),
            int(session.get("product_request_round", 0)),
            shown,
            provider=recommendation_provider(),
            image_similarity_service=image_service,
            spatial_scorer=scorer,
            mood_text=furniture_recommender.mood_clip_text(
                category,
                mood_analysis.get("mood_scores", {}),
            ),
        )
    except Exception as exc:
        print(f"[recommendations] 실패: {exc}")
        return jsonify({"ok": False, "error": "추천 상품을 불러오지 못했습니다."}), 502
    session["shown_product_ids"] = sorted(shown)
    return jsonify(
        {
            "ok": True,
            "type": category,
            "replace_id": replace_id,
            "queries": queries,
            "products": [public_product(item) for item in products],
        }
    )


def resolve_final_layout_path():
    """3D로 보여줄 layout 파일 경로. 사용자의 최종 선택이 반영된 것을 우선한다.

    1) modified_layout_file  — 가구 유지·제거 + 구매 상품이 반영된 layout
    2) edited_floorplan_layout_file — 평면도 화면에서 드래그로 직접 고친 layout
    3) floorplan_layout_file — AI가 처음 만든 layout
    """
    candidates = (
        ("modified_layout_file", "modified"),
        ("edited_floorplan_layout_file", "edited"),
        ("floorplan_layout_file", "original"),
    )
    for session_key, source in candidates:
        resolved_path = resolve_session_generated_file(
            session_key
        )
        if resolved_path:
            return resolved_path, source

    return None, None


# ──────────────────────────────────────────────────────
# 개발용: 이미 캐시된 평면도를 세션에 태워 Gemini 호출 없이 뒷단계를 본다.
# 무료 등급 일일 쿼터(모델당 20회)를 쓰지 않고 UI를 확인할 때 쓴다.
# debug 모드에서만 열린다.
# ──────────────────────────────────────────────────────
def cached_floorplan_uploads():
    """평면도 캐시(scene+layout+svg)가 완비된 업로드 파일명 목록."""
    if not os.path.isdir(UPLOAD_DIR):
        return []

    ready = []

    for name in sorted(
        os.listdir(UPLOAD_DIR)
    ):
        path = os.path.join(
            UPLOAD_DIR,
            name,
        )

        if (
            not os.path.isfile(path)
            or name.startswith(".")
        ):
            continue

        stem = os.path.splitext(
            name
        )[0]

        needed = [
            f"{stem}_model2_scene.json",
            f"{stem}_model2_layout.json",
            f"{stem}_model2_floorplan.svg",
        ]

        if all(
            os.path.isfile(
                os.path.join(
                    GENERATED_DIR,
                    part,
                )
            )
            for part in needed
        ):
            ready.append(name)

    return ready


@app.route("/dev/use-cached")
def dev_use_cached():
    """개발 환경에서 기존 평면도 캐시를 세션에 연결해 재사용한다."""
    if not app.debug:
        abort(404)

    available = (
        cached_floorplan_uploads()
    )

    requested = request.args.get(
        "file",
        "",
    ).strip()

    # 파일을 지정하지 않으면 재사용 가능한 캐시 목록을 JSON으로 반환한다.
    if not requested:
        return jsonify(
            {
                "count": len(
                    available
                ),
                "files": available,
            }
        )

    # 파일명을 외우지 않아도 되게 latest 를 허용한다
    if requested == "latest":
        newest = max(
            available,
            key=lambda name: os.path.getmtime(
                os.path.join(
                    UPLOAD_DIR,
                    name,
                )
            ),
            default="",
        )

        if not newest:
            return jsonify(
                {
                    "ok": False,
                    "error": (
                        "재사용할 평면도 "
                        "캐시가 없습니다."
                    ),
                }
            ), 404

        requested = newest

    # 경로 조작 방지: 파일명만 취하고 캐시 완비 목록에 있는지 확인
    safe_name = os.path.basename(
        requested
    )

    if safe_name not in available:
        return jsonify(
            {
                "ok": False,
                "error": (
                    "캐시가 완비된 업로드가 "
                    "아닙니다."
                ),
                "files": available,
            }
        ), 404

    clear_design_session()

    session[
        "uploaded_file"
    ] = safe_name

    session[
        "original_filename"
    ] = safe_name

    # STEP 1(프롬프트·무드 선택)을 건너뛰므로 그 단계가 넣던 값을 채운다.
    # 이게 없으면 /product-selection 이 /prompt 로 되돌린다.
    session[
        "mood_prompt"
    ] = request.args.get(
        "prompt",
        "",
    ).strip() or "밝고 아늑한 원룸"

    session[
        "style_tags"
    ] = []

    session[
        "selected_mood_image"
    ] = ""

    def _dimension(key, fallback):
        """쿼리 문자열의 방 치수를 읽고 유효하지 않으면 기본값을 쓴다."""
        raw = request.args.get(
            key,
            "",
        ).strip()

        try:
            value = float(raw)
        except ValueError:
            return fallback

        return (
            value
            if value > 0
            else fallback
        )

    session["room_width"] = _dimension(
        "width",
        3.6,
    )

    session["room_depth"] = _dimension(
        "depth",
        5.0,
    )

    session[
        "ceiling_height"
    ] = _dimension(
        "ceiling",
        2.4,
    )

    # 평면도 단계를 실제로 건너뛰려면 캐시 산출물 경로를 세션에 직접 넣어야 한다.
    # (/floorplan 을 거치지 않으므로 그 라우트가 해주던 일을 여기서 대신한다)
    stem = os.path.splitext(
        safe_name
    )[0]

    session[
        "floorplan_layout_file"
    ] = f"{stem}_model2_layout.json"

    session[
        "original_floorplan_file"
    ] = (
        f"{stem}_model2_floorplan.svg"
    )

    # STEP 4·5 가 기존 가구 목록을 쓰므로 캐시된 layout 에서 같은 형태로 채운다
    try:
        cached_layout_path = resolve_session_generated_file(
            "floorplan_layout_file"
        )
        if not cached_layout_path:
            raise FileNotFoundError(
                "캐시된 평면도 layout 파일을 찾을 수 없습니다."
            )
        cached_layout = json.loads(
            Path(cached_layout_path).read_text(
                encoding="utf-8"
            )
        )

        detected = []

        for index, obj in enumerate(
            cached_layout.get(
                "objects",
                [],
            )
        ):
            item_type = str(
                obj.get("type")
                or "unknown"
            ).lower()

            if item_type in {
                "door",
                "window",
                "curtain",
                "aircon",
            }:
                continue

            detected.append(
                {
                    "id": (
                        f"furniture_{index}"
                    ),
                    "label": (
                        translate_furniture_label(
                            item_type,
                            obj.get(
                                "label"
                            ),
                            index + 1,
                        )
                    ),
                    "type": item_type,
                    "source_index": index,
                }
            )

        session[
            "detected_furniture"
        ] = detected

    except Exception as exc:
        print(
            "[dev-use-cached] "
            "기존 가구 목록 구성 실패: "
            f"{exc}"
        )

    target = request.args.get(
        "to",
        "3d",
    ).strip().lower()

    destinations = {
        "3d": "preview_3d",
        "floorplan": "floorplan",
        "step5": "product_selection",
        "furniture": "furniture_choice",
        "result": "result",
    }

    return redirect(
        url_for(
            destinations.get(
                target,
                "preview_3d",
            )
        )
    )


def _preview_style_prompt() -> str:
    """무드 문장과 태그를 한 줄로 합친다. 가구별 3D 형태 생성이 스타일 참고로 쓴다."""
    return " ".join(
        [
            str(
                session.get(
                    "mood_prompt",
                    "",
                )
            ).strip(),
            " ".join(
                str(tag)
                for tag
                in session.get(
                    "style_tags",
                    [],
                )
                if str(tag).strip()
            ),
        ]
    ).strip()


@app.route("/preview-3d")
def preview_3d():
    """최종 배치의 3D 화면을 표시한다."""
    if (
        "uploaded_file"
        not in session
    ):
        return redirect(
            url_for(
                "upload"
            )
        )

    layout_path, layout_source = (
        resolve_final_layout_path()
    )

    scene_3d = None
    scene_error = None

    if not layout_path:
        scene_error = (
            "3D로 표시할 배치 정보가 없습니다. "
            "평면도를 먼저 생성해 주세요."
        )

    else:
        try:
            layout = json.loads(
                Path(
                    layout_path
                ).read_text(
                    encoding="utf-8"
                )
            )

            plan, _ = (
                current_room_plan()
            )

            # build_scene 은 SVG와 같은 배치를 얻기 위해
            # rule_based_svg 의 파이프라인을 돈다. 그 안에서 방 크기 전역
            # (ROOM_W/ROOM_H)을 바꾸므로 평면도 생성과 같은 락을 잡는다.
            with floorplan_generation_lock:
                scene_3d = (
                    floorplan_3d
                    .build_scene(
                        layout,
                        plan,
                    )
                )

            if not scene_3d.get(
                "objects"
            ):
                scene_3d = None
                scene_error = (
                    "배치된 가구가 없어 "
                    "3D로 보여줄 것이 없습니다."
                )

            # 가구 형태는 페이지를 연 뒤 /api/scene/parts로 따로 받는다(가구별 Gemini 생성).
            # 여기서 기다리면 3D 화면이 Gemini 응답만큼 늦게 뜬다.

        except Exception as exc:
            scene_error = (
                "3D 배치 정보를 읽지 "
                "못했습니다."
            )

            print(
                "[preview-3d] "
                f"씬 생성 실패: {exc}"
            )

    return render_template(
        "preview_3d.html",
        scene_3d=scene_3d,
        scene_error=scene_error,
        layout_source=layout_source,
    )


@app.route(
    "/my-designs/save",
    methods=["POST"],
)
def save_design():
    """현재 디자인 결과와 관련 캐시 정보를 사용자 계정에 저장한다."""
    if not current_user.is_authenticated:
        return jsonify(
            {
                "ok": False,
                "error": "login_required",
            }
        ), 401

    payload = request.get_json(
        silent=True
    ) or {}
    modified_svg = str(
        payload.get(
            "modified_svg"
        )
        or ""
    )
    modified_floorplan_file = session.get(
        "modified_floorplan_file"
    )

    if modified_svg:
        if (
            len(
                modified_svg.encode(
                    "utf-8"
                )
            )
            > 3_000_000
        ):
            return jsonify(
                {
                    "ok": False,
                    "error": (
                        "저장할 평면도 데이터가 너무 큽니다."
                    ),
                }
            ), 400
        try:
            sanitized_svg = (
                model2_floorplan
                .sanitize_floorplan_edit_svg(
                    modified_svg
                )
            )
            modified_floorplan_file = (
                "saved_modified_floorplan_"
                f"{uuid.uuid4().hex[:16]}.svg"
            )
            (
                Path(GENERATED_DIR)
                / modified_floorplan_file
            ).write_text(
                sanitized_svg,
                encoding="utf-8",
            )
        except Exception as exc:
            return jsonify(
                {
                    "ok": False,
                    "error": (
                        "수정 평면도를 저장하지 "
                        f"못했습니다: {exc}"
                    ),
                }
            ), 400

    selected_products = load_session_json_cache(
        "selected_products_file",
        default=[],
    )
    if not isinstance(
        selected_products,
        list,
    ):
        selected_products = []
    selected_products_file = save_json_cache(
        "saved_products",
        selected_products,
    )

    now = datetime.now()
    design = SavedDesign(
        user_id=current_user.id,
        title=now.strftime(
            "%Y년 %m월 %d일 %H:%M 디자인"
        ),
        generated_file=session.get(
            "generated_file"
        ),
        original_floorplan_file=session.get(
            "original_floorplan_file"
        ),
        modified_floorplan_file=(
            modified_floorplan_file
        ),
        selected_products_file=(
            selected_products_file
        ),
        description=session.get(
            "ai_description"
        ),
        tags_json=json.dumps(
            session.get(
                "style_tags",
                [],
            ),
            ensure_ascii=False,
        ),
        furniture_choices_json=json.dumps(
            session.get(
                "furniture_choices",
                [],
            ),
            ensure_ascii=False,
        ),
        purchase_items_json=json.dumps(
            session.get(
                "purchase_items",
                [],
            ),
            ensure_ascii=False,
        ),
    )
    db.session.add(design)
    db.session.commit()

    return jsonify(
        {
            "ok": True,
            "design_id": design.id,
        }
    )


# ──────────────────────────────────────────────────────
# 기존 AJAX 상품 추천 API
# ──────────────────────────────────────────────────────
def item_id_to_query(
    item_id,
):
    """추천 항목 ID를 Google Shopping 검색어로 변환한다."""
    query_map = {
        "chair-001": (
            "원목 의자"
        ),
        "table-001": (
            "원목 테이블"
        ),
        "sofa-001": (
            "패브릭 소파"
        ),
        "bed-001": (
            "원목 침대"
        ),
        "lamp-001": (
            "무드등"
        ),
        "desk-001": (
            "원목 책상"
        ),
        "curtain-001": (
            "베이지 커튼"
        ),
        "side-table-001": (
            "원목 협탁"
        ),
        "shelf-001": (
            "원목 선반"
        ),
        "cabinet-001": (
            "원목 수납장"
        ),
        "rug-001": (
            "베이지 인테리어 러그"
        ),
        "plant-001": (
            "인테리어 식물"
        ),
    }

    return query_map.get(
        item_id,
        item_id,
    )


YOLO_MODEL = None


def get_yolo_model():
    """YOLO 가구 탐지 모델을 한 번만 불러와 재사용한다."""
    global YOLO_MODEL

    if YOLO_MODEL is None:
        model_path = os.path.join(
            BASE_DIR,
            "yolov8n.pt",
        )

        YOLO_MODEL = YOLO(
            model_path
        )

    return YOLO_MODEL


def detect_furniture_from_image(
    image_path,
):
    """방 이미지에서 YOLO로 가구를 탐지해 정규화된 목록을 반환한다."""
    model = get_yolo_model()

    results = model.predict(
        source=image_path,
        save=False,
        verbose=False,
    )

    label_map = {
        "bed": "침대",
        "chair": "의자",
        "couch": "소파",
        "dining table": (
            "테이블"
        ),
        "tv": "TV",
        "potted plant": (
            "식물"
        ),
    }

    allowed_labels = set(
        label_map
    )

    detected_items = []

    for result in results:
        for box in result.boxes:
            class_id = int(
                box.cls[0]
            )

            confidence = float(
                box.conf[0]
            )

            label_en = (
                result.names[
                    class_id
                ]
            )

            if (
                label_en
                not in allowed_labels
            ):
                continue

            if confidence < 0.3:
                continue

            label_ko = (
                label_map.get(
                    label_en,
                    label_en,
                )
            )

            if (
                label_ko
                not in detected_items
            ):
                detected_items.append(
                    label_ko
                )

    return detected_items


def detected_item_to_type(
    item_name,
):
    """탐지된 한국어 가구 이름을 내부 가구 유형 코드로 변환한다."""
    type_map = {
        "침대": "bed",
        "소파": "unknown",
        "TV": "unknown",
        "의자": "chair",
        "테이블": "table",
        "식물": "plant",
    }

    return type_map.get(
        item_name,
        "unknown",
    )


@app.route("/recommend")
def recommend():
    """항목 ID에 맞는 쇼핑 상품을 검색해 추천 화면에 표시한다."""
    item_id = request.args.get(
        "item",
        "chair-001",
    )

    try:
        query = item_id_to_query(
            item_id
        )

        products = (
            search_serpapi_shopping(
                query=query,
                display=5,
            )
        )

        if not products:
            return jsonify(
                {
                    "ok": False,
                    "error": (
                        "Google Shopping 검색 "
                        "결과가 없습니다."
                    ),
                }
            ), 404

        main_product = (
            products[0]
        )

        similar_products = (
            products[1:4]
        )

        item = {
            "id": item_id,
            "name": query,
            "price": (
                main_product[
                    "price"
                ]
            ),
            "rating": 4,
            "image": (
                main_product[
                    "image"
                ]
            ),
            "link": (
                main_product[
                    "link"
                ]
            ),
            "shop": (
                main_product[
                    "shop"
                ]
            ),
            "similar": [
                {
                    "name": (
                        product[
                            "title"
                        ]
                    ),
                    "price": (
                        product[
                            "price"
                        ]
                    ),
                    "shop": (
                        product[
                            "shop"
                        ]
                    ),
                    "image": (
                        product[
                            "image"
                        ]
                    ),
                    "link": (
                        product[
                            "link"
                        ]
                    ),
                }
                for product
                in similar_products
            ],
        }

        return jsonify(
            {
                "ok": True,
                "query": query,
                "item": item,
            }
        )

    except Exception as exc:
        print(
            "추천 API 오류:",
            exc,
        )

        return jsonify(
            {
                "ok": False,
                "error": (
                    "상품 추천 API 호출 중 "
                    "오류가 발생했습니다."
                ),
            }
        ), 500


if __name__ == "__main__":
    app.run(
        debug=True
    )
