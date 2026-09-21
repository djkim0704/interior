(function () {
  const textarea = document.getElementById("moodInput");
  const tagButtons = document.querySelectorAll(".tag-btn");
  const generateBtn = document.getElementById("generateBtn");
  const previewBox = document.getElementById("stylePreview");

  const currentScript = document.currentScript;
  const generateUrl = currentScript.dataset.generateUrl;
  const moodSearchUrl = currentScript.dataset.moodSearchUrl;

  if (!textarea || !generateBtn || !previewBox) {
    return;
  }

  const activeTags = new Set();

  let selectedImage = "";
  let searchTimer = null;
  let searchController = null;
  let latestSearchId = 0;
  let isSearching = false;
  let isSaving = false;

  function removeTagFromInput(tag) {
    const parts = textarea.value
      .split(",")
      .map((part) => part.trim())
      .filter(Boolean)
      .filter((part) => part.toLowerCase() !== tag.trim().toLowerCase());

    textarea.value = parts.join(", ");
  }

  function updateGenerateButton() {
    generateBtn.disabled = isSearching || isSaving;

    if (isSaving) {
      generateBtn.textContent = "저장하는 중...";
    } else if (isSearching) {
      generateBtn.textContent = "추천 이미지 검색 중...";
    } else {
      generateBtn.textContent = "다음: 방 사진 업로드";
    }
  }

  function clearSelection() {
    selectedImage = "";

    previewBox.querySelectorAll(".mood-option").forEach((option) => {
      option.classList.remove("selected");
      option.setAttribute("aria-selected", "false");
      option.style.outline = "";
      option.style.outlineOffset = "";
      option.style.boxShadow = "";
    });
  }

  function selectMoodOption(option) {
    if (!option || isSearching) {
      return;
    }

    clearSelection();

    selectedImage = option.dataset.imagePath || "";

    option.classList.add("selected");
    option.setAttribute("aria-selected", "true");
    option.style.outline = "3px solid #212529";
    option.style.outlineOffset = "2px";
    option.style.boxShadow = "0 0 0 4px rgba(33, 37, 41, 0.15)";
  }

  function renderInitialMessage() {
    clearSelection();

    previewBox.innerHTML = `
      <div class="col-12">
        <p class="text-muted small mb-0">
          원하는 분위기를 입력하면 비슷한 인테리어 이미지를 찾아드려요.
        </p>
      </div>
    `;
  }

  function renderLoading() {
    clearSelection();

    previewBox.innerHTML = `
      <div class="col-12">
        <div
          class="d-flex flex-column align-items-center justify-content-center py-5"
          role="status"
          aria-live="polite"
        >
          <div
            class="spinner-border text-dark mb-3"
            aria-hidden="true"
          ></div>

          <p class="mb-1 fw-semibold">
            추천 이미지를 찾는 중이에요...
          </p>

          <p class="text-muted small mb-0">
            입력한 분위기와 비슷한 이미지를 검색하고 있습니다.
          </p>
        </div>
      </div>
    `;
  }

  function renderNoResults(needsSelection = false) {
    clearSelection();

    previewBox.innerHTML = `
      <div class="col-12">
        <div class="text-center py-4">
          <p class="mb-1">
            ${
              needsSelection
                ? "입력한 분위기를 정확히 이해하지 못했어요."
                : "비슷한 추천 이미지를 찾지 못했어요."
            }
          </p>

          <p class="text-muted small mb-0">
            ${
              needsSelection
                ? "아래 무드 버튼 중 가까운 분위기를 선택해 주세요."
                : "문구를 조금 다르게 입력해 주세요."
            }
          </p>
        </div>
      </div>
    `;
  }

  function renderError(message) {
    clearSelection();

    previewBox.innerHTML = `
      <div class="col-12">
        <div class="text-center py-4">
          <p class="mb-1 text-danger">
            추천 이미지를 불러오지 못했습니다.
          </p>

          <p class="text-muted small mb-0">
            ${message || "잠시 후 다시 시도해 주세요."}
          </p>
        </div>
      </div>
    `;
  }

  function renderPreview(results, needsSelection = false) {
    clearSelection();

    if (!Array.isArray(results) || results.length === 0) {
      renderNoResults(needsSelection);
      return;
    }

    previewBox.innerHTML = results
      .map((result) => {
        const imagePath = result.path || "";
        const imageUrl = result.url || "";

        return `
          <div class="col-6">
            <div
              class="style-preview-item mood-option"
              data-image-path="${imagePath}"
              role="button"
              tabindex="0"
              aria-selected="false"
              style="height: 160px; cursor: pointer;"
            >
              <img
                src="${imageUrl}"
                alt="추천 인테리어 이미지"
                loading="lazy"
                style="
                  width: 100%;
                  height: 100%;
                  object-fit: cover;
                  border-radius: 8px;
                "
              >
            </div>
          </div>
        `;
      })
      .join("");
  }

  async function updatePreview(query) {
    const trimmedQuery = query.trim();

    if (!moodSearchUrl || !trimmedQuery) {
      isSearching = false;
      updateGenerateButton();
      renderInitialMessage();
      return;
    }

    if (searchController) {
      searchController.abort();
    }

    searchController = new AbortController();

    const currentSearchId = ++latestSearchId;

    isSearching = true;
    updateGenerateButton();
    renderLoading();

    try {
      const response = await fetch(
        `${moodSearchUrl}?q=${encodeURIComponent(trimmedQuery)}`,
        {
          signal: searchController.signal,
        }
      );

      const data = await response.json();

      if (currentSearchId !== latestSearchId) {
        return;
      }

      if (!response.ok || !data.ok) {
        renderError(data.error);
        return;
      }

      renderPreview(data.results, data.needs_selection);
    } catch (error) {
      if (error.name === "AbortError") {
        return;
      }

      console.error("추천 이미지 검색 실패:", error);

      if (currentSearchId === latestSearchId) {
        renderError("네트워크 상태를 확인한 뒤 다시 시도해 주세요.");
      }
    } finally {
      if (currentSearchId === latestSearchId) {
        isSearching = false;
        updateGenerateButton();
      }
    }
  }

  function scheduleSearch() {
    clearTimeout(searchTimer);

    const query = textarea.value.trim();

    clearSelection();

    if (!query) {
      if (searchController) {
        searchController.abort();
      }

      latestSearchId += 1;
      isSearching = false;
      updateGenerateButton();
      renderInitialMessage();
      return;
    }

    /*
     * 사용자가 입력하는 즉시 로딩 상태를 보여준다.
     * 실제 검색 요청은 입력이 멈춘 뒤 600ms 후 실행된다.
     */
    isSearching = true;
    updateGenerateButton();
    renderLoading();

    searchTimer = setTimeout(() => {
      updatePreview(query);
    }, 600);
  }

  textarea.addEventListener("input", scheduleSearch);

  tagButtons.forEach((button) => {
    button.addEventListener("click", () => {
      const tag = button.dataset.tag;

      if (!tag) {
        return;
      }

      // 태그는 분위기를 하나로 좁히는 장치라 여러 개가 동시에 켜지면
      // 검색어가 서로 상쇄된다. 하나만 남기고 나머지는 해제한다.
      const wasActive = activeTags.has(tag);

      activeTags.forEach((activeTag) => removeTagFromInput(activeTag));
      activeTags.clear();
      tagButtons.forEach((other) => other.classList.remove("active"));

      if (!wasActive) {
        button.classList.add("active");
        activeTags.add(tag);

        const inputParts = textarea.value
          .split(",")
          .map((part) => part.trim())
          .filter(Boolean);
        const hasTag = inputParts.some(
          (part) => part.toLowerCase() === tag.trim().toLowerCase()
        );

        if (!hasTag) {
          inputParts.push(tag);
          textarea.value = inputParts.join(", ");
        }
      }

      scheduleSearch();
    });
  });

  previewBox.addEventListener("click", (event) => {
    const option = event.target.closest(".mood-option");

    if (option) {
      selectMoodOption(option);
    }
  });

  previewBox.addEventListener("keydown", (event) => {
    const option = event.target.closest(".mood-option");

    if (!option) {
      return;
    }

    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      selectMoodOption(option);
    }
  });

  generateBtn.addEventListener("click", async () => {
    const promptText = textarea.value.trim();

    if (isSearching) {
      alert("추천 이미지 검색이 끝날 때까지 잠시 기다려 주세요.");
      return;
    }

    if (!promptText) {
      alert("원하는 인테리어 분위기를 입력해 주세요.");
      textarea.focus();
      return;
    }

    if (!selectedImage) {
      alert("추천 이미지 중 하나를 선택해 주세요.");
      return;
    }

    isSaving = true;
    updateGenerateButton();

    try {
      const response = await fetch(generateUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          prompt: promptText,
          tags: Array.from(activeTags),
          selected_image: selectedImage,
        }),
      });

      const data = await response.json();

      if (!response.ok || !data.ok) {
        alert(
          data.error ||
            "입력 정보를 저장하지 못했습니다. 다시 시도해 주세요."
        );

        isSaving = false;
        updateGenerateButton();
        return;
      }

      window.location.href = data.redirect;
    } catch (error) {
      console.error("스타일 저장 실패:", error);

      alert(
        "네트워크 오류로 입력 정보를 저장하지 못했습니다."
      );

      isSaving = false;
      updateGenerateButton();
    }
  });

  updateGenerateButton();
})();
