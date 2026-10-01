// result 화면: 상품을 검색하거나 추천받아 평면도에 추가한다.
//
// 상품마다 서버가 방에 실제로 놓아 본 적합도(무드·공간·크기)와 근거를 함께 준다(항목 8·21).
// "가구 선택 요약"에서 교체로 표시한 같은 종류의 가구가 있으면 그 자리 기준으로 잰다.
(function () {
  const typeSel = document.getElementById("productType");
  const queryInput = document.getElementById("productQuery");
  const searchBtn = document.getElementById("productSearchBtn");
  const recommendBtn = document.getElementById("productRecommendBtn");
  const status = document.getElementById("productSearchStatus");
  const results = document.getElementById("productResults");
  const planBox = document.getElementById("modifiedPlanBox");
  if (!searchBtn || !results) return;

  const won = (n) => (n ? Number(n).toLocaleString() + "원" : "가격 정보 없음");
  const pct = (v) => (typeof v === "number" ? Math.round(v * 100) : null);
  const DIMENSION_SOURCES = {
    title: "상품명",
    snippet: "상품 설명",
    page: "상품 페이지",
    image: "상품 사진 치수표",
    size_class: "규격",
    type_default: "표준 크기(추정)",
  };

  // 교체로 표시한 같은 종류의 기존 가구
  function replaceTarget() {
    const kind = typeSel.value;
    const card = Array.from(document.querySelectorAll(".selection-summary-card[data-type]")).find(
      (el) => el.dataset.type === kind && el.querySelector('[data-decision="replace"].btn-primary')
    );
    return card ? card.dataset.sceneId || "" : "";
  }

  function escapeHtml(text) {
    return String(text || "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function bar(label, value) {
    const v = pct(value);
    if (v === null) return "";
    const tone = v >= 70 ? "#3f7d4f" : v >= 40 ? "#c08a2b" : "#b04a3a";
    return `
      <div class="d-flex align-items-center gap-1 small" style="font-size:0.75rem;">
        <span class="text-muted" style="width:38px;">${label}</span>
        <span style="flex:1;height:5px;background:#eee8df;border-radius:3px;overflow:hidden;">
          <span style="display:block;width:${v}%;height:100%;background:${tone};"></span>
        </span>
        <span style="width:30px;text-align:right;">${v}%</span>
      </div>`;
  }

  function fitBlock(fit) {
    if (!fit) return "";
    const dims = fit.dimensions || {};
    const size = dims.w_m
      ? `${Math.round(dims.w_m * 100)}×${Math.round(dims.d_m * 100)}${dims.h_m ? "×" + Math.round(dims.h_m * 100) : ""}cm · ${DIMENSION_SOURCES[dims.dimension_source] || ""}`
      : "";
    const badge = fit.fits === false
      ? '<span class="badge text-bg-danger">공간 부족</span>'
      : `<span class="badge" style="background:#5b4a3c;">적합도 ${pct(fit.total)}%</span>`;
    return `
      <div class="mt-1 mb-2">
        <div class="mb-1">${badge}</div>
        ${fit.mood === null || fit.mood === undefined ? "" : bar("무드", fit.mood)}
        ${bar("공간", fit.space)}
        ${bar("크기", fit.size)}
        ${size ? `<p class="text-muted mb-0 mt-1" style="font-size:0.72rem;">${escapeHtml(size)}</p>` : ""}
        <ul class="list-unstyled mb-0 mt-1" style="font-size:0.72rem;color:#6b5d50;">
          ${(fit.reasons || []).map((r) => `<li>· ${escapeHtml(r)}</li>`).join("")}
        </ul>
      </div>`;
  }

  function card(product) {
    const col = document.createElement("div");
    col.className = "col-6 col-md-4 col-lg-3";
    col.innerHTML = `
      <div class="border rounded-4 p-2 h-100 d-flex flex-column">
        <div style="aspect-ratio:1/1; overflow:hidden; border-radius:8px; background:#f0ece4;">
          ${product.image
            ? `<img src="${escapeHtml(product.image)}" alt="" style="width:100%;height:100%;object-fit:cover;">`
            : `<div class="d-flex align-items-center justify-content-center h-100 text-muted small">이미지 없음</div>`}
        </div>
        <p class="small fw-semibold mt-2 mb-1" style="height:38px;overflow:hidden;">${escapeHtml(product.title)}</p>
        <p class="small mb-1">${won(product.price)}</p>
        <p class="text-muted small mb-1">${escapeHtml(product.shop || "")}</p>
        ${fitBlock(product.fit)}
        <button type="button" class="btn btn-sm btn-outline-dark mt-auto add-btn">평면도에 추가</button>
      </div>`;
    const btn = col.querySelector(".add-btn");
    btn.addEventListener("click", () => addProduct(product, btn));
    return col;
  }

  function show(products, label) {
    results.innerHTML = "";
    if (!products.length) {
      status.textContent = "결과가 없습니다.";
      return;
    }
    status.textContent = label;
    products.forEach((p) => results.appendChild(card(p)));
  }

  async function search() {
    const q = (queryInput.value || "").trim();
    if (!q) { status.textContent = "검색어를 입력해 주세요."; return; }
    status.textContent = "검색하고, 이 방에 맞는지 재는 중…";
    results.innerHTML = "";
    try {
      const params = new URLSearchParams({ q, type: typeSel.value });
      const target = replaceTarget();
      if (target) params.set("replace_id", target);
      const res = await fetch("/search-products?" + params.toString(), { credentials: "same-origin" });
      const data = await res.json();
      if (!data.ok) { status.textContent = data.error || "검색 실패"; return; }
      show(data.products, `${data.products.length}개 상품 · 방에 들어가는 상품을 앞에 보여 줘요`);
    } catch (err) {
      console.error(err);
      status.textContent = "네트워크 오류가 발생했습니다.";
    }
  }

  async function recommend() {
    if (!recommendBtn) return;
    recommendBtn.disabled = true;
    status.textContent = "무드에 맞는 상품을 찾고, 방에 놓아 보는 중…";
    results.innerHTML = "";
    try {
      const target = replaceTarget();
      const res = await fetch("/api/recommendations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ type: typeSel.value, replace_id: target || undefined }),
      });
      const data = await res.json();
      if (!data.ok) { status.textContent = data.error || "추천 실패"; return; }
      show(
        data.products,
        data.replace_id
          ? `교체할 가구 자리에 맞춘 추천 ${data.products.length}개`
          : `무드·공간·크기를 함께 본 추천 ${data.products.length}개`
      );
    } catch (err) {
      console.error(err);
      status.textContent = "네트워크 오류가 발생했습니다.";
    } finally {
      recommendBtn.disabled = false;
    }
  }

  async function addProduct(product, btn) {
    btn.disabled = true;
    btn.textContent = "추가 중…";
    try {
      const res = await fetch("/add-product", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({
          type: typeSel.value,
          title: product.title,
          link: product.link,
          image: product.image,
          price: product.price,
          shop: product.shop,
          brand: product.brand,
          maker: product.maker,
          snippet: product.snippet,
        }),
      });
      const data = await res.json();
      if (!data.ok) { btn.textContent = "실패"; console.error(data.error); return; }
      if (planBox && data.svg_markup) {
        planBox.innerHTML = data.svg_markup;
        if (window.initFloorplanDrag) window.initFloorplanDrag();
      }
      // 같은 화면의 3D도 새 배치로 바꾼다
      if (data.scene_3d) {
        window.dispatchEvent(new CustomEvent("floorplan:scene-updated", { detail: data.scene_3d }));
      }
      btn.className = "btn btn-sm btn-success mt-auto add-btn";
      btn.textContent = "추가됨 ✓";
    } catch (err) {
      console.error(err);
      btn.textContent = "오류";
      btn.disabled = false;
    }
  }

  searchBtn.addEventListener("click", search);
  if (recommendBtn) recommendBtn.addEventListener("click", recommend);
  queryInput.addEventListener("keydown", (e) => { if (e.key === "Enter") search(); });
})();
