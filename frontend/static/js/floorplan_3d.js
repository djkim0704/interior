// 평면도 3D 배치 확인 뷰어
//
// 서버(model2/floorplan_3d.py)가 만든 씬 데이터를 받아 three.js 로 그린다.
// 목적은 "사진처럼 예쁜 렌더"가 아니라 **배치·치수 확인**이다. 그래서
//   - 1m 격자와 방 치수를 항상 표시한다
//   - 가구는 타입별 파라메트릭 박스로 만든다(3D 에셋 불필요)
//   - 마우스로 돌려보며 가구를 짚으면 실측 크기를 알려준다
//
// 씬 데이터는 <script type="application/json" id="floorplan3dScene"> 에 들어온다.
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

// ── 색 유틸 ────────────────────────────────────────────────
// 면마다 광원 계산을 하는 대신, 같은 색의 명도만 바꿔 부재를 구분한다.
function shade(hex, factor) {
  const color = new THREE.Color(hex);
  color.multiplyScalar(factor);
  return color;
}

function material(hex, factor = 1, extra = {}) {
  return new THREE.MeshStandardMaterial({
    color: shade(hex, factor),
    roughness: 0.72,
    metalness: 0.04,
    ...extra,
  });
}

// 박스 하나. y 는 바닥에서 띄운 높이(밑면 기준)라서 중심으로 변환해 넣는다.
function box(w, h, d, mat, x = 0, y = 0, z = 0) {
  const mesh = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), mat);
  mesh.position.set(x, y + h / 2, z);
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  return mesh;
}

function cylinder(radius, h, mat, x = 0, y = 0, z = 0, segments = 16) {
  const mesh = new THREE.Mesh(
    new THREE.CylinderGeometry(radius, radius, h, segments),
    mat
  );
  mesh.position.set(x, y + h / 2, z);
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  return mesh;
}

// 다리 4개를 판 아래에 달아준다(책상·식탁·선반 공용).
function addLegs(group, w, d, legTop, mat, inset = 0.06, thickness = 0.05) {
  const dx = w / 2 - inset;
  const dz = d / 2 - inset;
  [[-dx, -dz], [dx, -dz], [-dx, dz], [dx, dz]].forEach(([lx, lz]) => {
    group.add(box(thickness, legTop, thickness, mat, lx, 0, lz));
  });
}

// ── 타입별 가구 만들기 ─────────────────────────────────────
// 각 함수는 "뒷면이 -z 를 향한" 상태로 만든다. 벽 방향 회전은 호출부에서 준다.
// obj: { w_m, d_m, height_m, color, ... }
const BUILDERS = {
  bed(obj) {
    const g = new THREE.Group();
    const { w_m: w, d_m: d, height_m: h, color } = obj;
    const wood = material(color, 0.55);
    const sheet = material("#f2ece0", 1);
    const fabric = material(color, 1.05);

    g.add(box(w, h * 0.6, d, wood, 0, 0, 0));                       // 프레임
    g.add(box(w * 0.94, h * 0.4, d * 0.94, sheet, 0, h * 0.6, 0));  // 매트리스
    // 이불은 발치(+z) 쪽 60%
    g.add(box(w * 0.92, h * 0.12, d * 0.58, fabric, 0, h, d * 0.19));
    // 베개 2개는 머리쪽(-z)
    [-w * 0.23, w * 0.23].forEach((px) => {
      g.add(box(w * 0.38, h * 0.16, d * 0.16, sheet, px, h, -d * 0.36));
    });
    // 헤드보드 — 뒷면(-z)에 세운다
    g.add(box(w, h * 1.7, 0.06, wood, 0, 0, -d / 2 + 0.03));
    return g;
  },

  desk(obj) {
    const g = new THREE.Group();
    const { w_m: w, d_m: d, height_m: h, color } = obj;
    const top = material(color, 1);
    const leg = material(color, 0.7);
    g.add(box(w, 0.04, d, top, 0, h - 0.04, 0));
    addLegs(g, w, d, h - 0.04, leg);
    return g;
  },

  table(obj) {
    return BUILDERS.desk(obj);
  },

  low_table(obj) {
    const g = new THREE.Group();
    const { w_m: w, d_m: d, height_m: h, color } = obj;
    const top = material(color, 1);
    const leg = material(color, 0.7);
    g.add(box(w, 0.05, d, top, 0, h - 0.05, 0));
    addLegs(g, w, d, h - 0.05, leg, 0.05, 0.045);
    return g;
  },

  shelf(obj) {
    const g = new THREE.Group();
    const { w_m: w, d_m: d, height_m: h, color } = obj;
    const frame = material(color, 0.85);
    const inner = material(color, 1.1);
    g.add(box(0.04, h, d, frame, -w / 2 + 0.02, 0, 0));   // 좌측판
    g.add(box(0.04, h, d, frame, w / 2 - 0.02, 0, 0));    // 우측판
    g.add(box(w, 0.03, d, frame, 0, h - 0.03, 0));        // 천판
    const shelves = Math.max(2, Math.round(h / 0.4));
    for (let i = 1; i < shelves; i += 1) {
      g.add(box(w - 0.08, 0.025, d * 0.94, inner, 0, (h / shelves) * i, 0));
    }
    g.add(box(w, 0.02, d, frame, 0, 0, 0));               // 바닥판
    return g;
  },

  cabinet(obj) {
    const g = new THREE.Group();
    const { w_m: w, d_m: d, height_m: h, color } = obj;
    const body = material(color, 0.9);
    const door = material(color, 1.12);
    const knob = material("#4a4238", 1);
    g.add(box(w, h, d, body, 0, 0, 0));
    // 앞면(+z)에 문짝 두 장과 손잡이
    const half = w / 2 - 0.02;
    [-half / 1.02, half / 1.02].forEach((dx) => {
      g.add(box(w / 2 - 0.03, h - 0.06, 0.02, door, dx, 0.03, d / 2 + 0.005));
    });
    [-0.04, 0.04].forEach((dx) => {
      g.add(cylinder(0.015, 0.05, knob, dx, h * 0.55, d / 2 + 0.02, 8));
    });
    return g;
  },

  chair(obj) {
    const g = new THREE.Group();
    const { w_m: w, d_m: d, height_m: h, color } = obj;
    const seatH = Math.min(0.45, h * 0.5);
    const seat = material(color, 1);
    const leg = material(color, 0.68);
    g.add(box(w, 0.06, d, seat, 0, seatH, 0));
    addLegs(g, w, d, seatH, leg, 0.04, 0.04);
    // 등받이는 뒷면(-z)
    g.add(box(w, h - seatH, 0.05, seat, 0, seatH, -d / 2 + 0.025));
    return g;
  },

  floor_chair(obj) {
    const g = new THREE.Group();
    const { w_m: w, d_m: d, height_m: h, color } = obj;
    const fabric = material(color, 1);
    g.add(box(w, h * 0.35, d, fabric, 0, 0, 0));
    g.add(box(w, h * 0.9, 0.07, material(color, 0.85), 0, 0, -d / 2 + 0.035));
    return g;
  },

  stool(obj) {
    const g = new THREE.Group();
    const { w_m: w, d_m: d, height_m: h, color } = obj;
    const radius = Math.max(0.12, Math.min(w, d) / 2);
    g.add(cylinder(radius, 0.06, material(color, 1), 0, h - 0.06, 0));
    addLegs(g, radius * 1.5, radius * 1.5, h - 0.06, material(color, 0.7), 0.02, 0.035);
    return g;
  },

  rug(obj) {
    const { w_m: w, d_m: d, color } = obj;
    const mesh = box(w, 0.015, d, material(color, 1, { roughness: 0.95 }));
    mesh.castShadow = false;
    return mesh;
  },

  lamp(obj) {
    const g = new THREE.Group();
    const { height_m: h, color } = obj;
    const pole = material("#5c5348", 1);
    const shadeMat = new THREE.MeshStandardMaterial({
      color: shade(color, 1),
      roughness: 0.5,
      emissive: shade(color, 0.35),
    });
    g.add(cylinder(0.12, 0.03, pole, 0, 0));            // 받침
    g.add(cylinder(0.02, h - 0.25, pole, 0, 0.03));     // 기둥
    g.add(cylinder(0.16, 0.22, shadeMat, 0, h - 0.22)); // 갓
    return g;
  },

  plant(obj) {
    const g = new THREE.Group();
    const { w_m: w, d_m: d, height_m: h, color } = obj;
    const potR = Math.max(0.09, Math.min(w, d) / 2.6);
    g.add(cylinder(potR, h * 0.3, material("#a9663f", 1), 0, 0));
    const leaf = material(color, 1);
    g.add(cylinder(potR * 0.18, h * 0.35, material("#5c6b3f", 1), 0, h * 0.3, 0, 8));
    // 잎 뭉치를 구 3개로 겹쳐 표현
    [[0, h * 0.72, 0, potR * 1.5], [potR * 0.7, h * 0.62, 0, potR],
     [-potR * 0.6, h * 0.66, potR * 0.4, potR * 0.9]].forEach(([lx, ly, lz, r]) => {
      const s = new THREE.Mesh(new THREE.SphereGeometry(r, 12, 10), leaf);
      s.position.set(lx, ly, lz);
      s.castShadow = true;
      g.add(s);
    });
    return g;
  },

  mirror(obj) {
    const g = new THREE.Group();
    const { w_m: w, height_m: h, color } = obj;
    g.add(box(w, h, 0.04, material("#6b6259", 1), 0, 0, 0));
    g.add(box(w - 0.08, h - 0.08, 0.02, new THREE.MeshStandardMaterial({
      color: shade(color, 1), roughness: 0.12, metalness: 0.75,
    }), 0, 0.04, 0.025));
    return g;
  },

  door(obj) {
    const g = new THREE.Group();
    const { w_m: w, height_m: h, color } = obj;
    g.add(box(w, h, 0.06, material(color, 0.92), 0, 0, 0));
    g.add(cylinder(0.02, 0.09, material("#4a4238", 1), w / 2 - 0.12, h * 0.48, 0.05, 8));
    return g;
  },

  window(obj) {
    const g = new THREE.Group();
    const { w_m: w, height_m: h } = obj;
    const frame = material("#f6f3ec", 1);
    const glass = new THREE.MeshStandardMaterial({
      color: new THREE.Color("#cfe4ee"),
      roughness: 0.08,
      metalness: 0.1,
      transparent: true,
      opacity: 0.42,
    });
    g.add(box(w, h, 0.05, frame, 0, 0, 0));
    g.add(box(w - 0.1, h - 0.1, 0.02, glass, 0, 0.05, 0.02));
    g.add(box(0.04, h - 0.1, 0.04, frame, 0, 0.05, 0.03)); // 중간 창틀
    return g;
  },

  sofa(obj) {
    const g = new THREE.Group();
    const { w_m: w, d_m: d, height_m: h, color } = obj;
    const body = material(color, 1);
    const cushion = material(color, 1.16);
    const arm = Math.min(0.18, w * 0.14);
    g.add(box(w, h * 0.45, d, body, 0, 0, 0));                        // 좌대
    g.add(box(w, h * 0.95, d * 0.24, body, 0, 0, -d / 2 + d * 0.12)); // 등받이
    [-w / 2 + arm / 2, w / 2 - arm / 2].forEach((ax) => {             // 팔걸이
      g.add(box(arm, h * 0.68, d, body, ax, 0, 0));
    });
    g.add(box(w - arm * 2.2, h * 0.15, d * 0.68, cushion, 0, h * 0.45, d * 0.1));
    return g;
  },

  wardrobe(obj) {
    const g = new THREE.Group();
    const { w_m: w, d_m: d, height_m: h, color } = obj;
    const body = material(color, 0.88);
    const door = material(color, 1.1);
    g.add(box(w, h, d, body, 0, 0, 0));
    const half = w / 2 - 0.02;
    [-half / 1.02, half / 1.02].forEach((dx) => {
      g.add(box(w / 2 - 0.03, h - 0.08, 0.02, door, dx, 0.04, d / 2 + 0.005));
    });
    // 손잡이는 가운데 맞닿는 쪽에 세로로
    [-0.03, 0.03].forEach((dx) => {
      g.add(box(0.02, 0.22, 0.03, material("#4a4238", 1), dx, h * 0.46, d / 2 + 0.02));
    });
    return g;
  },

  dresser(obj) {
    const g = new THREE.Group();
    const { w_m: w, d_m: d, height_m: h, color } = obj;
    const body = material(color, 0.9);
    const front = material(color, 1.12);
    const knob = material("#4a4238", 1);
    g.add(box(w, h, d, body, 0, 0, 0));
    const rows = Math.max(2, Math.round(h / 0.26));
    for (let i = 0; i < rows; i += 1) {
      const y = (h / rows) * i + 0.02;
      g.add(box(w - 0.06, h / rows - 0.04, 0.02, front, 0, y, d / 2 + 0.005));
      g.add(cylinder(0.014, 0.04, knob, 0, y + h / rows / 2 - 0.03, d / 2 + 0.02, 8));
    }
    return g;
  },

  bench(obj) {
    const g = new THREE.Group();
    const { w_m: w, d_m: d, height_m: h, color } = obj;
    const seat = material(color, 1);
    const leg = material(color, 0.7);
    g.add(box(w, 0.06, d, seat, 0, h - 0.06, 0));
    addLegs(g, w, d, h - 0.06, leg, 0.05, 0.05);
    return g;
  },

  tv(obj) {
    const g = new THREE.Group();
    const { w_m: w, height_m: h, color } = obj;
    const frame = material(color, 1);
    const screen = new THREE.MeshStandardMaterial({
      color: new THREE.Color("#1b2b34"),
      roughness: 0.18,
      metalness: 0.35,
    });
    g.add(box(w, h, 0.05, frame, 0, 0, 0));
    g.add(box(w - 0.05, h - 0.05, 0.01, screen, 0, 0.025, 0.03));
    return g;
  },

  fridge(obj) {
    const g = new THREE.Group();
    const { w_m: w, d_m: d, height_m: h, color } = obj;
    const body = material(color, 1, { metalness: 0.32, roughness: 0.42 });
    const seam = material(color, 0.78);
    g.add(box(w, h, d, body, 0, 0, 0));
    // 냉동실/냉장실 경계선과 손잡이
    g.add(box(w, 0.02, 0.01, seam, 0, h * 0.68, d / 2 + 0.005));
    g.add(box(0.03, h * 0.3, 0.04, seam, w * 0.34, h * 0.32, d / 2 + 0.02));
    return g;
  },

  aircon(obj) {
    const g = new THREE.Group();
    const { w_m: w, height_m: h, color } = obj;
    g.add(box(w, h, 0.18, material(color, 1), 0, 0, 0));
    // 아래쪽 토출구
    g.add(box(w - 0.06, 0.03, 0.02, material(color, 0.72), 0, h * 0.16, 0.1));
    return g;
  },

  washer(obj) {
    const g = new THREE.Group();
    const { w_m: w, d_m: d, height_m: h, color } = obj;
    g.add(box(w, h, d, material(color, 1, { metalness: 0.22 }), 0, 0, 0));
    // 원형 도어
    const door = new THREE.Mesh(
      new THREE.CylinderGeometry(Math.min(w, h) * 0.28, Math.min(w, h) * 0.28, 0.03, 20),
      material("#8fa3ad", 1, { roughness: 0.2, metalness: 0.4 })
    );
    door.rotation.x = Math.PI / 2;
    door.position.set(0, h * 0.5, d / 2 + 0.015);
    door.castShadow = true;
    g.add(door);
    return g;
  },

  vanity(obj) {
    const g = new THREE.Group();
    const { w_m: w, d_m: d, height_m: h, color } = obj;
    const top = material(color, 1);
    const leg = material(color, 0.7);
    g.add(box(w, 0.04, d, top, 0, h - 0.04, 0));
    addLegs(g, w, d, h - 0.04, leg, 0.05, 0.045);
    // 뒤쪽(-z)에 거울
    g.add(box(w * 0.62, h * 0.85, 0.03, material(color, 0.8), 0, h, -d / 2 + 0.02));
    g.add(box(w * 0.54, h * 0.75, 0.01, new THREE.MeshStandardMaterial({
      color: new THREE.Color("#d3dde1"), roughness: 0.1, metalness: 0.7,
    }), 0, h + h * 0.05, -d / 2 + 0.04));
    return g;
  },

  nightstand(obj) {
    const g = new THREE.Group();
    const { w_m: w, d_m: d, height_m: h, color } = obj;
    g.add(box(w, h, d, material(color, 0.92), 0, 0, 0));
    g.add(box(w - 0.05, h * 0.4, 0.02, material(color, 1.12), 0, h * 0.52, d / 2 + 0.005));
    g.add(cylinder(0.014, 0.04, material("#4a4238", 1), 0, h * 0.7, d / 2 + 0.02, 8));
    return g;
  },

  desk_chair(obj) {
    const g = new THREE.Group();
    const { w_m: w, d_m: d, height_m: h, color } = obj;
    const seatH = Math.min(0.46, h * 0.48);
    const seat = material(color, 1);
    const metal = material("#6d6d74", 1, { metalness: 0.5, roughness: 0.35 });
    const radius = Math.max(0.1, Math.min(w, d) / 2);
    g.add(box(w, 0.07, d, seat, 0, seatH, 0));
    g.add(box(w * 0.92, h - seatH, 0.06, seat, 0, seatH, -d / 2 + 0.03)); // 등받이
    g.add(cylinder(0.03, seatH - 0.06, metal, 0, 0.06));                   // 기둥
    // 바퀴 5개 방사형
    for (let i = 0; i < 5; i += 1) {
      const a = (i / 5) * Math.PI * 2;
      g.add(cylinder(
        0.025, 0.05, metal,
        Math.cos(a) * radius * 0.8, 0, Math.sin(a) * radius * 0.8, 8
      ));
    }
    return g;
  },

  curtain(obj) {
    const g = new THREE.Group();
    const { w_m: w, height_m: h, color } = obj;
    const fabric = material(color, 1, { roughness: 0.94 });
    // 주름을 얇은 판 여러 장으로 흉내낸다
    const panels = Math.max(4, Math.round(w / 0.22));
    const step = w / panels;
    for (let i = 0; i < panels; i += 1) {
      const x = -w / 2 + step * (i + 0.5);
      const depth = i % 2 === 0 ? 0.05 : 0.09;
      g.add(box(step * 0.94, h, depth, fabric, x, 0, 0));
    }
    g.add(box(w + 0.06, 0.03, 0.03, material("#6d6258", 1), 0, h, 0)); // 커튼봉
    return g;
  },

  // 스피커·화병·바구니 같은 소품. 받침 위에 둥근 몸체를 얹어 상자와 구분한다.
  decor(obj) {
    const g = new THREE.Group();
    const { w_m: w, d_m: d, height_m: h, color } = obj;
    const r = Math.max(0.05, Math.min(w, d) / 2);
    g.add(box(w, h * 0.12, d, material(color, 0.8)));
    g.add(cylinder(r * 0.8, h * 0.88, material(color, 1.05), 0, h * 0.12, 0, 20));
    return g;
  },

  unknown(obj) {
    const { w_m: w, d_m: d, height_m: h, color } = obj;
    return box(w, h, d, material(color, 1));
  },
};

// Gemini 가 준 부품 설계도로 가구를 세운다. 좌표는 가구 치수에 대한 비율이라
// 여기서 미터로 환산한다. (규약은 gemini_furniture_parts.py 의 docstring)
// 손으로 짠 BUILDERS 보다 형태가 나을 때만 쓰이고, 설계도가 없으면 호출되지 않는다.
function buildFromParts(obj, parts) {
  const { w_m: w, d_m: d, height_m: h, color } = obj;
  const g = new THREE.Group();

  parts.forEach((part) => {
    const tone = typeof part.tone === "number" ? part.tone : 1;
    const mat = material(color, tone);
    const x = (part.x || 0) * w;
    const y = (part.y || 0) * h;
    const z = (part.z || 0) * d;

    if (part.shape === "cylinder") {
      // 반지름은 짧은 쪽 변에 걸어야 가구 밖으로 삐져나오지 않는다
      const r = (part.r || 0.05) * Math.min(w, d);
      g.add(cylinder(r, (part.h || 0.1) * h, mat, x, y, z));
      return;
    }

    g.add(
      box((part.w || 0.1) * w, (part.h || 0.1) * h, (part.d || 0.1) * d, mat, x, y, z)
    );
  });

  return g;
}

// ── 라벨 (캔버스 텍스처 스프라이트) ─────────────────────────
// CSS2DRenderer 대신 스프라이트를 쓴다. 오버레이 DOM 없이 한글이 선명하게 나온다.
function makeLabel(text, { accent = false } = {}) {
  const pad = 10;
  const font = "600 34px 'Malgun Gothic', 'Apple SD Gothic Neo', sans-serif";
  const measure = document.createElement("canvas").getContext("2d");
  measure.font = font;
  const width = Math.ceil(measure.measureText(text).width) + pad * 2;
  const height = 52;

  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d");
  ctx.font = font;
  // 구매로 추가한 가구는 라벨 색을 달리해 원래 있던 가구와 구분한다
  ctx.fillStyle = accent ? "rgba(150, 62, 34, 0.9)" : "rgba(24, 22, 20, 0.82)";
  ctx.beginPath();
  if (typeof ctx.roundRect === "function") {
    ctx.roundRect(0, 0, width, height, 12);
  } else {
    ctx.rect(0, 0, width, height); // roundRect 미지원 브라우저 폴백
  }
  ctx.fill();
  ctx.fillStyle = "#ffffff";
  ctx.textBaseline = "middle";
  ctx.fillText(text, pad, height / 2 + 2);

  const texture = new THREE.CanvasTexture(canvas);
  texture.minFilter = THREE.LinearFilter;
  const sprite = new THREE.Sprite(
    new THREE.SpriteMaterial({ map: texture, transparent: true, depthTest: false })
  );
  sprite.scale.set((width / height) * 0.34, 0.34, 1);
  sprite.renderOrder = 10;
  return sprite;
}

// ── 뷰어 본체 ──────────────────────────────────────────────
class Floorplan3D {
  constructor(container, sceneData) {
    this.container = container;
    this.data = sceneData;
    this.disposed = false;
    this.pickables = [];
    this.labelsVisible = true;

    this._initRenderer();
    this._initScene();
    this._buildRoom();
    this._buildFurniture();
    this.setView("iso");
    this._bindEvents();
    this._animate();
  }

  _initRenderer() {
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    this.renderer.domElement.style.display = "block";
    this.renderer.domElement.style.width = "100%";
    this.renderer.domElement.style.height = "100%";
    this.renderer.domElement.style.touchAction = "none";
    this.container.appendChild(this.renderer.domElement);
  }

  _initScene() {
    const { ceiling_m: ceiling, width_m: w, depth_m: d } = this.data.room;
    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color("#e8e4dc");

    const span = Math.max(w, d);
    this.camera = new THREE.PerspectiveCamera(48, 1, 0.05, span * 12);

    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.08;
    this.controls.maxPolarAngle = Math.PI / 2 - 0.02; // 바닥 아래로 못 내려가게
    this.controls.minDistance = 0.6;
    this.controls.maxDistance = span * 5;
    this.controls.target.set(0, ceiling * 0.35, 0);

    this.scene.add(new THREE.HemisphereLight("#ffffff", "#b3a894", 1.15));

    const sun = new THREE.DirectionalLight("#fff6e8", 1.5);
    sun.position.set(w * 0.9, ceiling * 3.2, d * 0.7);
    sun.castShadow = true;
    sun.shadow.mapSize.set(1024, 1024);
    const reach = span * 1.1;
    Object.assign(sun.shadow.camera, {
      left: -reach, right: reach, top: reach, bottom: -reach,
      near: 0.1, far: span * 8,
    });
    sun.shadow.bias = -0.0012;
    sun.shadow.camera.updateProjectionMatrix();
    this.scene.add(sun);

    // 반대쪽에서 약하게 채워 그림자 속이 완전히 검게 죽지 않게 한다
    const fill = new THREE.DirectionalLight("#dfe7f0", 0.35);
    fill.position.set(-w, ceiling * 1.6, -d * 0.6);
    this.scene.add(fill);
  }

  _buildRoom() {
    const { width_m: w, depth_m: d, ceiling_m: h, floor_color, wall_color } =
      this.data.room;

    // 바닥
    const floor = new THREE.Mesh(
      new THREE.PlaneGeometry(w, d),
      new THREE.MeshStandardMaterial({ color: floor_color, roughness: 0.85 })
    );
    floor.rotation.x = -Math.PI / 2;
    floor.receiveShadow = true;
    this.scene.add(floor);

    // 1m 격자 — 배치 확인의 핵심 단서라 항상 켜둔다
    const grid = new THREE.GridHelper(
      Math.ceil(Math.max(w, d)),
      Math.ceil(Math.max(w, d)),
      "#8d8474",
      "#b6ad9c"
    );
    grid.position.y = 0.004;
    grid.material.transparent = true;
    grid.material.opacity = 0.55;
    this.scene.add(grid);

    // 벽 4면 — 안쪽만 보이게 BackSide 로 세운다(카메라가 밖에 있으면 투시됨)
    const wallMat = new THREE.MeshStandardMaterial({
      color: wall_color,
      roughness: 0.9,
      side: THREE.BackSide,
    });
    const shell = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), wallMat);
    shell.position.y = h / 2;
    shell.receiveShadow = true;
    this.scene.add(shell);

    // 방 치수 라벨
    const dims = makeLabel(`${w.toFixed(2)}m × ${d.toFixed(2)}m`);
    dims.position.set(0, 0.12, d / 2 + 0.22);
    dims.scale.multiplyScalar(1.25);
    this.scene.add(dims);
    this.dimsLabel = dims;
  }

  _buildFurniture() {
    const { width_m: w, depth_m: d } = this.data.room;
    this.furnitureGroup = new THREE.Group();
    this.labelGroup = new THREE.Group();

    this.data.objects.forEach((obj) => {
      // 설계도가 있으면 그것으로, 없으면 손으로 짠 빌더로 세운다.
      // 설계도는 Gemini 호출이라 없을 수 있어서 항상 빌더가 뒤를 받친다.
      const recipe = (this.data.furniture_parts || {})[obj.type];
      const build = BUILDERS[obj.type] || BUILDERS.unknown;
      let node;
      try {
        node = recipe && recipe.parts && recipe.parts.length
          ? buildFromParts(obj, recipe.parts)
          : build(obj);
      } catch (error) {
        console.warn("[floorplan-3d] 가구 생성 실패, 기본 도형으로 대체:", obj.type, error);
        node = BUILDERS.unknown(obj);
      }

      // 방 중심을 원점으로 옮긴다. cy(깊이)는 three.js 의 z 축.
      let x = obj.cx - w / 2;
      let z = obj.cy - d / 2;

      // 문·창·거울은 좌표 대신 해당 벽면에 붙인다
      if (obj.wall_mounted) {
        const gap = 0.04;
        if (obj.wall === "top") z = -d / 2 + gap;
        else if (obj.wall === "bottom") z = d / 2 - gap;
        else if (obj.wall === "left") x = -w / 2 + gap;
        else if (obj.wall === "right") x = w / 2 - gap;
      }

      const wrapper = new THREE.Group();
      wrapper.add(node);
      wrapper.position.set(x, obj.base_m, z);
      // rotation_deg 는 위에서 본 시계방향 각. three.js Y축 회전은 반대라 부호를 뒤집는다.
      wrapper.rotation.y = -THREE.MathUtils.degToRad(obj.rotation_deg);
      wrapper.userData.object = obj;
      this.furnitureGroup.add(wrapper);
      this.pickables.push(wrapper);

      const labelText = obj.marker
        ? `${obj.label} #${obj.marker}`
        : obj.label;
      const label = makeLabel(labelText, { accent: Boolean(obj.is_product) });
      label.position.set(x, obj.base_m + obj.height_m + 0.2, z);
      label.userData.object = obj;
      this.labelGroup.add(label);
    });

    this.scene.add(this.furnitureGroup);
    this.scene.add(this.labelGroup);
  }

  // 카메라 프리셋 — iso(기본) / top(위에서) / eye(눈높이)
  setView(mode) {
    const { width_m: w, depth_m: d, ceiling_m: h } = this.data.room;
    const span = Math.max(w, d);
    if (mode === "top") {
      this.camera.position.set(0.001, span * 1.9, 0.001);
      this.controls.target.set(0, 0, 0);
    } else if (mode === "eye") {
      this.camera.position.set(0, Math.min(1.6, h * 0.7), d / 2 + span * 0.15);
      this.controls.target.set(0, h * 0.42, -d * 0.1);
    } else {
      this.camera.position.set(w * 0.95, span * 1.15, d * 1.15);
      this.controls.target.set(0, h * 0.3, 0);
    }
    this.viewMode = mode;
    this.controls.update();
  }

  toggleLabels() {
    this.labelsVisible = !this.labelsVisible;
    this.labelGroup.visible = this.labelsVisible;
    return this.labelsVisible;
  }

  _bindEvents() {
    this.raycaster = new THREE.Raycaster();
    this.pointer = new THREE.Vector2();
    this.hovered = null;

    this._onPointerMove = (event) => {
      const rect = this.renderer.domElement.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      this.pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
      this.pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
      this._pick();
    };
    this.renderer.domElement.addEventListener("pointermove", this._onPointerMove);

    this._onResize = () => this.resize();
    window.addEventListener("resize", this._onResize);
    this.resize();
  }

  _pick() {
    this.raycaster.setFromCamera(this.pointer, this.camera);
    const hits = this.raycaster.intersectObjects(this.pickables, true);
    let wrapper = null;
    if (hits.length) {
      let node = hits[0].object;
      while (node && !node.userData.object) node = node.parent;
      wrapper = node;
    }
    if (wrapper === this.hovered) return;

    if (this.hovered) this._setHighlight(this.hovered, false);
    this.hovered = wrapper;
    if (wrapper) this._setHighlight(wrapper, true);

    const info = wrapper ? wrapper.userData.object : null;
    if (typeof this.onHover === "function") this.onHover(info);
  }

  _setHighlight(wrapper, on) {
    wrapper.traverse((node) => {
      if (!node.isMesh || !node.material) return;
      if (on) {
        if (node.material.__origEmissive === undefined) {
          node.material.__origEmissive = node.material.emissive
            ? node.material.emissive.clone()
            : null;
        }
        if (node.material.emissive) node.material.emissive.setHex(0x3a3020);
      } else if (node.material.__origEmissive !== undefined) {
        if (node.material.emissive && node.material.__origEmissive) {
          node.material.emissive.copy(node.material.__origEmissive);
        } else if (node.material.emissive) {
          node.material.emissive.setHex(0x000000);
        }
      }
    });
  }

  resize() {
    const width = this.container.clientWidth;
    const height = this.container.clientHeight;
    if (!width || !height) return;
    this.renderer.setSize(width, height, false);
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
  }

  _animate() {
    if (this.disposed) return;
    this.frame = requestAnimationFrame(() => this._animate());
    this.controls.update();
    // 라벨이 항상 카메라를 향하도록(스프라이트는 자동이지만 크기 보정용)
    this.renderer.render(this.scene, this.camera);
  }

  dispose() {
    this.disposed = true;
    if (this.frame) cancelAnimationFrame(this.frame);
    window.removeEventListener("resize", this._onResize);
    this.renderer.domElement.removeEventListener("pointermove", this._onPointerMove);
    this.controls.dispose();
    this.scene.traverse((node) => {
      if (node.geometry) node.geometry.dispose();
      if (node.material) {
        const mats = Array.isArray(node.material) ? node.material : [node.material];
        mats.forEach((m) => {
          if (m.map) m.map.dispose();
          m.dispose();
        });
      }
    });
    this.renderer.dispose();
    if (this.renderer.domElement.parentNode) {
      this.renderer.domElement.parentNode.removeChild(this.renderer.domElement);
    }
  }
}

// ── 페이지 연결 ────────────────────────────────────────────
function readScene() {
  const node = document.getElementById("floorplan3dScene");
  if (!node) return null;
  try {
    const data = JSON.parse(node.textContent || "{}");
    if (!data.room) return null;
    return data;
  } catch (error) {
    console.error("[floorplan-3d] 씬 데이터 파싱 실패:", error);
    return null;
  }
}

function init() {
  const host = document.getElementById("floorplan3dBox");
  if (!host) return;

  // /floorplan 은 2D와 토글해서 쓰고, /preview-3d 는 전용 화면이라 바로 띄운다.
  const toggle = document.getElementById("floorplanViewToggle");
  const autostart = host.dataset.autostart === "true";

  const data = readScene();
  const status = document.getElementById("floorplan3dStatus");

  if (!data || !data.objects || !data.objects.length) {
    if (toggle) {
      toggle.disabled = true;
      toggle.title = "3D로 표시할 배치 정보가 없습니다.";
    }
    return;
  }

  let viewer = null;
  const box2d = document.getElementById("editableFloorplanBox");
  const controlsBar = document.getElementById("floorplan3dControls");

  function describe(obj) {
    if (!status) return;
    if (!obj) {
      const room = data.room;
      // 치수를 무엇으로 정했는지 밝혀 둔다. 추정 근거를 알아야 사용자가 믿고 고칠 수 있다.
      const basis = {
        user_one_side: "입력한 한 변과 사진에서 읽은 방 비율로 계산",
        reference_objects: "침대·문 같은 표준 크기 가구로 추정",
        reference_objects_clamped: "표준 크기 가구로 추정(일반적인 방 크기 범위로 보정)",
        default: "기준 가구가 없어 긴 변 4m로 가정",
      }[room.scale_source];
      status.textContent = room.estimated
        ? `방 치수 ${room.width_m}m × ${room.depth_m}m — ${basis || "추정값"}. 정확한 확인을 위해 실측값 입력을 권장합니다.`
        : `방 ${room.width_m}m × ${room.depth_m}m · 천장 ${room.ceiling_m}m · 가구를 짚으면 크기가 표시됩니다.`;
      return;
    }
    const size =
      `가로 ${obj.w_m.toFixed(2)}m × 깊이 ${obj.d_m.toFixed(2)}m` +
      ` × 높이 ${obj.height_m.toFixed(2)}m`;
    const name = obj.is_product
      ? `${obj.product_title || obj.label} (구매 선택${obj.marker ? " #" + obj.marker : ""})`
      : obj.label;
    status.textContent = `${name} — ${size}`;
  }

  function show3d() {
    if (box2d) box2d.classList.add("d-none");
    host.classList.remove("d-none");
    if (controlsBar) controlsBar.classList.remove("d-none");
    if (!viewer) {
      try {
        viewer = new Floorplan3D(host, data);
        viewer.onHover = describe;
      } catch (error) {
        console.error("[floorplan-3d] 초기화 실패:", error);
        host.classList.add("d-none");
        if (box2d) box2d.classList.remove("d-none");
        if (status) {
          status.textContent = box2d
            ? "이 브라우저에서 3D를 표시할 수 없습니다(WebGL 미지원). 2D 평면도로 확인해 주세요."
            : "이 브라우저에서 3D를 표시할 수 없습니다(WebGL 미지원).";
        }
        if (controlsBar) controlsBar.classList.add("d-none");
        if (toggle) toggle.disabled = true;
        return;
      }
    }
    viewer.resize();
    describe(null);
    if (toggle) toggle.textContent = "2D 평면도";
  }

  function show2d() {
    host.classList.add("d-none");
    if (controlsBar) controlsBar.classList.add("d-none");
    if (box2d) box2d.classList.remove("d-none");
    if (toggle) toggle.textContent = "3D로 보기";
    if (status) status.textContent = "";
  }

  if (toggle) {
    toggle.addEventListener("click", () => {
      if (host.classList.contains("d-none")) show3d();
      else show2d();
    });
  }

  document.querySelectorAll("[data-view-preset]").forEach((button) => {
    button.addEventListener("click", () => {
      if (!viewer) return;
      viewer.setView(button.getAttribute("data-view-preset"));
      document.querySelectorAll("[data-view-preset]").forEach((other) => {
        other.classList.toggle("active", other === button);
      });
    });
  });

  // 전용 화면은 사용자가 누를 것도 없이 바로 3D를 보여준다
  if (autostart) show3d();

  const labelButton = document.getElementById("floorplan3dLabels");
  if (labelButton) {
    labelButton.addEventListener("click", () => {
      if (!viewer) return;
      const visible = viewer.toggleLabels();
      labelButton.classList.toggle("active", visible);
      labelButton.textContent = visible ? "이름 숨기기" : "이름 표시";
    });
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
