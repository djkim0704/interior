// 평면도 확인 패널: AI가 확신하지 못한 가구를 사용자가 확인·보정한다 (항목 17·18·19).
//
// 모든 수정은 /api/scene/edit 하나로 보낸다. 서버가 Scene Graph를 고치고 겹친 가구를
// 비켜 준 뒤 2D와 3D를 같이 돌려주므로, 여기서는 받은 결과로 화면을 바꾸기만 한다.
(function () {
  const panel = document.getElementById("floorplanReview");
  const dataNode = document.getElementById("floorplanReviewData");
  if (!panel || !dataNode) return;

  const editUrl = panel.dataset.editUrl;
  const reanalyzeUrl = panel.dataset.reanalyzeUrl;
  const list = document.getElementById("reviewList");
  const status = document.getElementById("reviewStatus");
  const reanalyzeButton = document.getElementById("reviewReanalyze");
  const doorBox = document.getElementById("reviewDoor");
  const doorHint = document.getElementById("reviewDoorHint");
  let review = {};
  try {
    review = JSON.parse(dataNode.textContent || "{}");
  } catch (error) {
    review = {};
  }
  let busy = false;

  function setStatus(text) {
    if (status) status.textContent = text || "";
  }

  function render() {
    const rows = review.uncertain || [];
    list.innerHTML = "";
    if (!rows.length) {
      const empty = document.createElement("li");
      empty.className = "small text-muted";
      empty.textContent = "확인이 필요한 가구가 없어요.";
      list.appendChild(empty);
    }
    rows.forEach((row) => {
      const item = document.createElement("li");
      item.className = "review-row";

      const name = document.createElement("span");
      name.className = "fw-semibold";
      name.textContent = row.label || row.type;
      const confidence = document.createElement("span");
      confidence.className = "review-confidence";
      confidence.textContent = `확신 ${Math.round((row.confidence || 0) * 100)}%${row.refined ? " · 다시 확인함" : ""}`;
      item.append(name, confidence);

      const note = (review.notes || {})[row.id];
      if (note) {
        const noteEl = document.createElement("span");
        noteEl.className = "small text-muted w-100";
        noteEl.textContent = note;
        item.appendChild(noteEl);
      }

      const actions = document.createElement("span");
      actions.className = "ms-auto d-flex gap-1";
      const confirm = button("맞아요", () => send([{ op: "confirm", id: row.id }], `${row.label}을(를) 확인했어요.`));
      const select = document.createElement("select");
      select.className = "form-select form-select-sm";
      select.style.width = "auto";
      select.innerHTML = '<option value="">종류 바꾸기</option>';
      (review.types || []).forEach(([kind, label]) => {
        const option = document.createElement("option");
        option.value = kind;
        option.textContent = label;
        select.appendChild(option);
      });
      select.addEventListener("change", () => {
        if (!select.value) return;
        send([{ op: "retype", id: row.id, type: select.value }], "종류를 바꿨어요.");
      });
      const remove = button("없어요", () => send([{ op: "remove", id: row.id }], `${row.label}을(를) 지웠어요.`));
      actions.append(confirm, select, remove);
      item.appendChild(actions);
      list.appendChild(item);
    });

    if (reanalyzeButton) reanalyzeButton.disabled = !rows.length || busy;
    // 문이 없으면 동선을 '가장 큰 빈 영역' 기준으로만 잴 수 있다. 문 위치를 받아 통로를 맞춘다
    if (doorBox) {
      doorHint.textContent = review.has_door
        ? "문을 더 추가하려면 벽과 위치를 고르세요."
        : "사진에서 문을 찾지 못했어요. 문이 있는 벽과 위치를 알려 주시면 문 앞 통로를 비워 배치합니다.";
    }
  }

  function button(text, onClick) {
    const el = document.createElement("button");
    el.type = "button";
    el.className = "btn btn-sm btn-outline-secondary";
    el.textContent = text;
    el.addEventListener("click", onClick);
    return el;
  }

  function apply(result, doneText) {
    const box = document.getElementById("editableFloorplanBox");
    const current = box ? box.querySelector("svg") : null;
    if (current && result.svg) {
      const holder = document.createElement("div");
      holder.innerHTML = result.svg;
      const fresh = holder.querySelector("svg");
      if (fresh) {
        current.replaceWith(fresh);
        if (window.initFloorplanDrag) window.initFloorplanDrag();
      }
    }
    if (result.scene_3d) {
      window.dispatchEvent(new CustomEvent("floorplan:scene-updated", { detail: result.scene_3d }));
    }
    if (result.review) review = result.review;
    render();
    setStatus(doneText);
  }

  async function post(url, body, doneText) {
    if (busy) return;
    busy = true;
    render();
    setStatus("반영하고 있습니다…");
    try {
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.error || "반영하지 못했습니다.");
      busy = false;
      apply(result, typeof doneText === "function" ? doneText(result) : doneText);
    } catch (error) {
      busy = false;
      render();
      setStatus(error.message || "반영하지 못했습니다.");
    }
  }

  function send(ops, doneText) {
    return post(editUrl, { ops, context: "floorplan" }, doneText);
  }

  // 3D 편집 등 다른 곳에서 고쳐도 목록을 맞춘다
  window.addEventListener("floorplan:uncertain-updated", (event) => {
    review.uncertain = event.detail || [];
    render();
  });

  if (reanalyzeButton) {
    reanalyzeButton.addEventListener("click", () =>
      post(reanalyzeUrl, { context: "floorplan" }, (result) =>
        (result.changed || []).length
          ? `가구 ${result.changed.length}개를 다시 확인했어요. 결과가 맞는지 봐 주세요.`
          : "다시 확인했지만 바뀐 내용이 없어요."
      )
    );
  }

  const doorAdd = document.getElementById("reviewDoorAdd");
  if (doorAdd) {
    doorAdd.addEventListener("click", () => {
      const wall = document.getElementById("reviewDoorWall").value;
      const offset = Number(document.getElementById("reviewDoorOffset").value) / 100;
      send([{ op: "add", type: "door", wall, offset }], "문을 추가하고 문 앞 통로를 비워 다시 배치했어요.");
    });
  }

  render();
})();
