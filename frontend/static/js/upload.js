// STEP 2: 방 사진 업로드
(function () {
  const dropZone = document.getElementById("dropZone");
  const fileInput = document.getElementById("fileInput");
  const chooseFileBtn = document.getElementById("chooseFileBtn");
  const previewCard = document.getElementById("previewCard");
  const previewThumb = document.getElementById("previewThumb");
  const previewName = document.getElementById("previewName");
  const previewSize = document.getElementById("previewSize");
  const removeFileBtn = document.getElementById("removeFileBtn");
  const nextBtn = document.getElementById("nextBtn");
  const errorEl = document.getElementById("uploadError");

  const scriptTag = document.currentScript;
  const uploadUrl = scriptTag.dataset.uploadUrl;

  const MAX_SIZE = 20 * 1024 * 1024;
  const ALLOWED_TYPES = ["image/jpeg", "image/png"];

  let selectedFile = null;

  function showError(message) {
    errorEl.textContent = message;
    errorEl.classList.remove("d-none");
  }

  function clearError() {
    errorEl.classList.add("d-none");
    errorEl.textContent = "";
  }

  function formatSize(bytes) {
    return (bytes / (1024 * 1024)).toFixed(1) + "MB";
  }

  function updateNextButton() {
    nextBtn.disabled = !selectedFile;
  }

  function handleFile(file) {
    clearError();

    if (!file) return;

    if (!ALLOWED_TYPES.includes(file.type)) {
      showError("JPG 또는 PNG 파일만 업로드할 수 있어요.");
      return;
    }

    if (file.size > MAX_SIZE) {
      showError("파일 크기는 최대 20MB까지 가능해요.");
      return;
    }

    selectedFile = file;

    const reader = new FileReader();

    reader.onload = (event) => {
      previewThumb.src = event.target.result;
    };

    reader.readAsDataURL(file);

    previewName.textContent = file.name;
    previewSize.textContent = formatSize(file.size);
    previewCard.classList.remove("d-none");

    updateNextButton();
  }

  chooseFileBtn.addEventListener("click", () => {
    fileInput.click();
  });

  fileInput.addEventListener("change", (event) => {
    handleFile(event.target.files[0]);
  });

  ["dragenter", "dragover"].forEach((eventName) => {
    dropZone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropZone.classList.add("dragover");
    });
  });

  ["dragleave", "drop"].forEach((eventName) => {
    dropZone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropZone.classList.remove("dragover");
    });
  });

  dropZone.addEventListener("drop", (event) => {
    const file = event.dataTransfer.files[0];
    handleFile(file);
  });

  removeFileBtn.addEventListener("click", () => {
    selectedFile = null;
    fileInput.value = "";
    previewCard.classList.add("d-none");
    updateNextButton();
  });

  nextBtn.addEventListener("click", async () => {
    if (!selectedFile) {
      showError("방 사진을 선택해 주세요.");
      return;
    }

    nextBtn.disabled = true;
    nextBtn.textContent = "업로드 중...";

    try {
      const formData = new FormData();

      formData.append("photo", selectedFile);
      // 방 크기는 선택 입력이다. 비어 있으면 보내지 않고 서버가 추정한다.
      [
        ["room_width", "roomWidth"],
        ["room_depth", "roomDepth"],
        ["ceiling_height", "ceilingHeight"],
      ].forEach(([name, id]) => {
        const input = document.getElementById(id);
        const value = input ? input.value.trim() : "";
        if (value) formData.append(name, value);
      });

      const response = await fetch(uploadUrl, {
        method: "POST",
        body: formData,
      });

      const data = await response.json();

      if (data.ok) {
        window.location.href = data.redirect;
      } else {
        showError(data.error || "업로드에 실패했어요.");
        nextBtn.textContent = "다음";
        updateNextButton();
      }
    } catch (error) {
      console.error(error);
      showError("네트워크 오류로 업로드에 실패했어요.");
      nextBtn.textContent = "다음";
      updateNextButton();
    }
  });

  updateNextButton();
})();
