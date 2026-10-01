(function () {
  const script = document.currentScript;
  const saveUrl = script && script.dataset.saveUrl;
  const box = document.getElementById("editableFloorplanBox");
  const editButton = document.getElementById("floorplanEditButton");
  const nextButton = document.getElementById("floorplanNextButton");
  const status = document.getElementById("floorplanEditStatus");
  let svg = box && box.querySelector("svg");

  if (!svg || !editButton || !saveUrl) return;

  let editing = false;
  let saving = false;
  svg.dataset.editEnabled = "false";

  function controls() {
    return svg.querySelectorAll("[data-edit-control]");
  }

  function setControlsVisible(visible) {
    controls().forEach((control) => {
      control.style.display = visible ? "inline" : "none";
    });
  }

  function setEditMode(enabled) {
    editing = enabled;
    svg.dataset.editEnabled = enabled ? "true" : "false";
    svg.classList.toggle("floorplan-edit-mode", enabled);
    setControlsVisible(enabled);
    editButton.textContent = enabled ? "수정 완료" : "수정하기";
    editButton.classList.toggle("btn-dark", enabled);
    editButton.classList.toggle("btn-outline-dark", !enabled);
    if (status) {
      status.textContent = enabled
        ? "가구를 끌어서 이동하고, 위의 − · + · ↻ 버튼으로 크기와 방향을 조절하세요."
        : "";
    }
  }

  async function saveChanges() {
    if (saving) return false;
    saving = true;
    editButton.disabled = true;
    setControlsVisible(false);
    // Runtime-only state must not be serialized into the SVG. Event listeners
    // themselves are not persisted, so keeping a "bound" marker would make a
    // later page incorrectly skip initialization.
    svg.removeAttribute("data-drag-bound");
    svg.removeAttribute("data-edit-enabled");
    svg.classList.remove("floorplan-dragging", "floorplan-edit-mode");
    if (status) status.textContent = "수정한 평면도를 저장하고 있습니다…";

    try {
      const response = await fetch(saveUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ svg: svg.outerHTML }),
      });
      const result = await response.json();
      if (!response.ok || !result.ok) {
        throw new Error(result.error || "저장하지 못했습니다.");
      }
      // 서버가 보정까지 마친 배치로 다시 그린 평면도를 주면 화면을 그것으로 바꾼다.
      // 겹친 가구를 비켜 준 결과가 화면에 바로 보여야 2D와 3D가 같다.
      if (result.svg) {
        const holder = document.createElement("div");
        holder.innerHTML = result.svg;
        const fresh = holder.querySelector("svg");
        if (fresh) {
          svg.replaceWith(fresh);
          svg = fresh;
          if (window.initFloorplanDrag) window.initFloorplanDrag();
        }
      }
      if (result.scene_3d) {
        window.dispatchEvent(
          new CustomEvent("floorplan:scene-updated", { detail: result.scene_3d })
        );
      }
      setEditMode(false);
      const moved = (result.adjustments || []).filter((item) => item.reason === "collision").length;
      if (status) {
        status.textContent = moved
          ? `수정한 배치를 저장했습니다. 겹친 가구 ${moved}개를 옆으로 비켜 놓았어요.`
          : "수정한 배치를 저장했습니다.";
      }
      return true;
    } catch (error) {
      svg.dataset.editEnabled = "true";
      svg.classList.add("floorplan-edit-mode");
      setControlsVisible(true);
      if (status) {
        status.textContent = error.message || "평면도 저장에 실패했습니다.";
      }
      return false;
    } finally {
      saving = false;
      editButton.disabled = false;
    }
  }

  editButton.addEventListener("click", async () => {
    if (!editing) {
      setEditMode(true);
      return;
    }
    await saveChanges();
  });

  if (nextButton) {
    nextButton.addEventListener("click", async (event) => {
      if (!editing) return;
      event.preventDefault();
      const destination = nextButton.href;
      if (await saveChanges()) {
        window.location.href = destination;
      }
    });
  }
})();
