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
import { RoundedBoxGeometry } from "three/addons/geometries/RoundedBoxGeometry.js";
import { SVGLoader } from "three/addons/loaders/SVGLoader.js";

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

// ── 파라메트릭 가구 (항목 4·7) ─────────────────────────────
// 상품 사진에서 뽑은 형태 속성(obj.attrs)으로 모양을 바꾼다. 같은 '소파'라도
// 좌석 수·팔걸이·등받이 높이·다리 모양이 실제 상품을 따라간다.
// attrs 키: leg_style, leg_height_ratio, has_armrests, back_height, seat_count,
//           has_headboard, top_shape, drawer_count, door_count, open_shelves,
//           material, secondary_color
function surface(hex, attrs, factor = 1) {
  const kind = (attrs && attrs.material) || "";
  const extra = {
    leather: { roughness: 0.42, metalness: 0.05 },
    metal: { roughness: 0.32, metalness: 0.65 },
    glass: { roughness: 0.08, metalness: 0.1, transparent: true, opacity: 0.45 },
    fabric: { roughness: 0.96, metalness: 0.0 },
    wood: { roughness: 0.62, metalness: 0.02 },
    rattan: { roughness: 0.9, metalness: 0.0 },
    marble: { roughness: 0.25, metalness: 0.05 },
  }[kind] || {};
  return material(hex, factor, extra);
}

function legHeight(attrs, h, fallback) {
  const style = attrs.leg_style || "";
  if (style === "none" || style === "plinth") return 0;
  const ratio = typeof attrs.leg_height_ratio === "number" && attrs.leg_height_ratio > 0
    ? attrs.leg_height_ratio
    : fallback;
  return Math.max(0, Math.min(h * 0.6, h * ratio));
}

// 다리 모양별로 받침을 만든다. legH는 바닥에서 몸체 아래까지 높이.
function addSupport(group, w, d, legH, attrs, mainHex) {
  const style = attrs.leg_style || "four_legs";
  const dark = material(attrs.secondary_color || "#3a3632", 1, { metalness: 0.5, roughness: 0.35 });
  const wood = material(attrs.secondary_color || mainHex, 0.62);
  if (style === "plinth") {
    group.add(box(w * 0.94, 0.06, d * 0.94, material(mainHex, 0.5), 0, 0, 0));
    return 0.06;
  }
  if (legH <= 0 || style === "none") return 0;
  if (style === "pedestal") {
    group.add(cylinder(Math.min(w, d) * 0.22, 0.02, dark, 0, 0, 0, 24));
    group.add(cylinder(0.04, legH, dark, 0, 0, 0, 12));
    return legH;
  }
  if (style === "casters") {
    group.add(cylinder(0.035, legH, dark, 0, 0, 0, 12));
    for (let i = 0; i < 5; i += 1) {
      const angle = (i / 5) * Math.PI * 2;
      const r = Math.min(w, d) * 0.4;
      const arm = box(r, 0.03, 0.04, dark, Math.cos(angle) * r / 2, 0.03, Math.sin(angle) * r / 2);
      arm.rotation.y = -angle;
      group.add(arm);
    }
    return legH;
  }
  if (style === "sled") {
    [-1, 1].forEach((side) => {
      const x = side * (w / 2 - 0.04);
      group.add(box(0.03, 0.02, d * 0.9, dark, x, 0, 0));
      group.add(box(0.03, legH, 0.03, dark, x, 0, -d * 0.42));
      group.add(box(0.03, legH, 0.03, dark, x, 0, d * 0.42));
    });
    return legH;
  }
  const thin = style === "metal_legs" || style === "hairpin";
  addLegs(group, w, d, legH, thin ? dark : wood, thin ? 0.04 : 0.06, thin ? 0.022 : 0.05);
  return legH;
}

const PARAMETRIC = {
  sofa(obj) {
    const g = new THREE.Group();
    const { w_m: w, d_m: d, height_m: h, color } = obj;
    const a = obj.attrs || {};
    const body = surface(color, a, 1);
    const cushion = surface(color, a, 1.16);
    const lift = addSupport(g, w, d, legHeight(a, h, 0.12), a, color);
    const seatH = Math.max(0.18, h * 0.45 - lift);
    const backH = { none: 0, low: h * 0.62, mid: h * 0.95, high: h * 1.2 }[a.back_height || "mid"] || h * 0.95;
    const arm = a.has_armrests === false ? 0 : Math.min(0.2, w * 0.12);
    g.add(box(w, seatH, d, body, 0, lift, 0));
    if (backH > 0) g.add(box(w, backH - lift, d * 0.22, body, 0, lift, -d / 2 + d * 0.11));
    if (arm > 0) {
      [-w / 2 + arm / 2, w / 2 - arm / 2].forEach((ax) => g.add(box(arm, h * 0.66 - lift, d, body, ax, lift, 0)));
    }
    const seats = Math.max(1, Math.min(6, a.seat_count || Math.round((w - arm * 2) / 0.65)));
    const span = (w - arm * 2 - 0.04) / seats;
    for (let i = 0; i < seats; i += 1) {
      const x = -w / 2 + arm + 0.02 + span * (i + 0.5);
      g.add(box(span - 0.025, h * 0.14, d * 0.66, cushion, x, lift + seatH, d * 0.1));
      if (backH > 0) g.add(box(span - 0.04, (backH - lift - seatH) * 0.8, d * 0.1, cushion, x, lift + seatH, -d / 2 + d * 0.26));
    }
    return g;
  },

  bed(obj) {
    const g = new THREE.Group();
    const { w_m: w, d_m: d, height_m: h, color } = obj;
    const a = obj.attrs || {};
    const frame = surface(color, a, 0.6);
    const sheet = material("#f2ece0", 1);
    const lift = addSupport(g, w, d, legHeight(a, h, 0.25), a, color);
    const frameH = Math.max(0.12, h * 0.55 - lift);
    g.add(box(w, frameH, d, frame, 0, lift, 0));
    g.add(box(w * 0.94, h * 0.4, d * 0.94, sheet, 0, lift + frameH, 0));
    g.add(box(w * 0.92, h * 0.1, d * 0.58, surface(color, a, 1.08), 0, lift + frameH + h * 0.4, d * 0.19));
    const pillows = w > 1.25 ? [-w * 0.23, w * 0.23] : [0];
    pillows.forEach((px) => g.add(box(Math.min(0.6, w * 0.38), h * 0.14, d * 0.15, sheet, px, lift + frameH + h * 0.4, -d * 0.37)));
    if (a.has_headboard !== false) g.add(box(w, h * 1.8, 0.07, frame, 0, 0, -d / 2 + 0.035));
    return g;
  },

  desk(obj) {
    const g = new THREE.Group();
    const { w_m: w, d_m: d, height_m: h, color } = obj;
    const a = obj.attrs || {};
    const top = surface(color, a, 1);
    const thickness = 0.035;
    const round = a.top_shape === "round" || a.top_shape === "oval";
    if (round) {
      const disc = cylinder(0.5, thickness, top, 0, h - thickness, 0, 40);
      disc.scale.set(w, 1, d);
      g.add(disc);
    } else {
      g.add(box(w, thickness, d, top, 0, h - thickness, 0));
    }
    const legs = { ...a, leg_style: a.leg_style && a.leg_style !== "plinth" && a.leg_style !== "none" ? a.leg_style : (round ? "pedestal" : "four_legs") };
    addSupport(g, round ? w * 0.6 : w, round ? d * 0.6 : d, h - thickness, legs, color);
    const drawers = Math.min(4, a.drawer_count || 0);
    if (drawers && !round) {
      const unitW = Math.min(0.45, w * 0.35);
      const unitH = Math.min(h * 0.6, 0.16 * drawers);
      g.add(box(unitW, unitH, d * 0.9, surface(color, a, 0.92), w / 2 - unitW / 2 - 0.03, h - thickness - unitH, 0));
      for (let i = 0; i < drawers; i += 1) {
        g.add(box(unitW - 0.03, unitH / drawers - 0.015, 0.015, surface(color, a, 1.1), w / 2 - unitW / 2 - 0.03, h - thickness - unitH + (unitH / drawers) * i + 0.008, d * 0.45 + 0.008));
      }
    }
    return g;
  },

  table(obj) {
    return PARAMETRIC.desk(obj);
  },

  low_table(obj) {
    return PARAMETRIC.desk(obj);
  },

  chair(obj) {
    const g = new THREE.Group();
    const { w_m: w, d_m: d, height_m: h, color } = obj;
    const a = obj.attrs || {};
    const seat = surface(color, a, 1);
    const seatH = Math.min(0.47, h * 0.5);
    addSupport(g, w, d, seatH - 0.05, { ...a, leg_style: a.leg_style && a.leg_style !== "none" ? a.leg_style : "four_legs" }, color);
    g.add(box(w, 0.06, d, seat, 0, seatH - 0.05, 0));
    const backH = { none: 0, low: (h - seatH) * 0.45, mid: (h - seatH) * 0.75, high: h - seatH }[a.back_height || "high"] ?? h - seatH;
    if (backH > 0) g.add(box(w, backH, 0.05, seat, 0, seatH, -d / 2 + 0.025));
    if (a.has_armrests) {
      [-w / 2 + 0.03, w / 2 - 0.03].forEach((ax) => g.add(box(0.05, 0.04, d * 0.8, seat, ax, seatH + 0.2, 0)));
    }
    return g;
  },

  desk_chair(obj) {
    return PARAMETRIC.chair({ ...obj, attrs: { leg_style: "casters", has_armrests: true, back_height: "high", ...(obj.attrs || {}) } });
  },

  cabinet(obj) {
    const g = new THREE.Group();
    const { w_m: w, d_m: d, height_m: h, color } = obj;
    const a = obj.attrs || {};
    const lift = addSupport(g, w, d, legHeight(a, h, 0.12), a, color);
    const bodyH = h - lift;
    const body = surface(color, a, 0.9);
    const front = surface(color, a, 1.12);
    const knob = material("#4a4238", 1);
    g.add(box(w, bodyH, d, body, 0, lift, 0));
    const zFront = d / 2 + 0.006;
    const drawers = Math.min(8, a.drawer_count || 0);
    const doors = Math.min(4, a.door_count || 0);
    const shelves = Math.min(6, a.open_shelves || 0);
    if (shelves && !doors && !drawers) {
      // 오픈 수납: 앞면을 비우고 칸막이만 보인다
      g.children[g.children.length - 1].material = surface(color, a, 0.7);
      for (let i = 1; i <= shelves; i += 1) {
        g.add(box(w - 0.04, 0.02, 0.02, front, 0, lift + (bodyH / (shelves + 1)) * i, zFront));
      }
      return g;
    }
    let y = lift + 0.03;
    if (drawers) {
      const zone = doors ? bodyH * 0.35 : bodyH - 0.06;
      const each = zone / drawers;
      for (let i = 0; i < drawers; i += 1) {
        g.add(box(w - 0.04, each - 0.02, 0.02, front, 0, y + each * i, zFront));
        g.add(box(Math.min(0.14, w * 0.25), 0.015, 0.02, knob, 0, y + each * i + each / 2, zFront + 0.015));
      }
      y += zone;
    }
    if (doors) {
      const top = lift + bodyH - 0.03;
      const each = (w - 0.04) / doors;
      for (let i = 0; i < doors; i += 1) {
        const x = -w / 2 + 0.02 + each * (i + 0.5);
        g.add(box(each - 0.015, top - y, 0.02, front, x, y, zFront));
        g.add(box(0.015, Math.min(0.2, (top - y) * 0.3), 0.02, knob, x + (i % 2 ? -each / 2 + 0.05 : each / 2 - 0.05), y + (top - y) * 0.45, zFront + 0.015));
      }
    }
    return g;
  },

  wardrobe(obj) {
    return PARAMETRIC.cabinet({ ...obj, attrs: { door_count: 2, leg_style: "plinth", ...(obj.attrs || {}) } });
  },
  dresser(obj) {
    return PARAMETRIC.cabinet({ ...obj, attrs: { drawer_count: 4, leg_style: "plinth", ...(obj.attrs || {}) } });
  },
  nightstand(obj) {
    return PARAMETRIC.cabinet({ ...obj, attrs: { drawer_count: 2, leg_style: "plinth", ...(obj.attrs || {}) } });
  },
  shelf(obj) {
    const a = obj.attrs || {};
    if (!a.door_count && !a.drawer_count) return BUILDERS.shelf(obj);
    return PARAMETRIC.cabinet(obj);
  },
};

// Gemini 가 준 부품 설계도로 가구를 세운다. 좌표는 가구 치수에 대한 비율이라
// 여기서 미터로 환산한다. (규약은 gemini_furniture_parts.py 의 docstring)
// 손으로 짠 BUILDERS 보다 형태가 나을 때만 쓰이고, 설계도가 없으면 호출되지 않는다.
function buildFromParts(obj, parts) {
  const { w_m: w, d_m: d, height_m: h, color } = obj;
  const g = new THREE.Group();
  const short = Math.min(w, d);
  const deg = Math.PI / 180;

  parts.forEach((part) => {
    const tone = typeof part.tone === "number" ? part.tone : 1;
    // 가구별 설계도는 부품마다 색·재질을 준다(금속 다리, 패브릭 쿠션 등)
    const mat = surface(part.color || color, { material: part.material }, part.color ? 1 : tone);
    const x = (part.x || 0) * w;
    const y = (part.y || 0) * h;
    const z = (part.z || 0) * d;
    let geometry;
    let height;

    if (part.shape === "cylinder") {
      // 반지름은 짧은 쪽 변에 건다. r2가 있으면 아래로 갈수록 굵기가 바뀐다(가늘어지는 다리, 갓)
      const top = Math.max(0.002, (part.r || 0.05) * short);
      const bottom = typeof part.r2 === "number" ? Math.max(0.0, part.r2 * short) : top;
      height = (part.h || 0.1) * h;
      geometry = new THREE.CylinderGeometry(top, bottom, height, 24);
    } else {
      const pw = (part.w || 0.1) * w;
      const pd = (part.d || 0.1) * d;
      height = (part.h || 0.1) * h;
      if (part.shape === "sphere") {
        geometry = new THREE.SphereGeometry(0.5, 24, 16);
        geometry.scale(pw, height, pd);
      } else if (part.shape === "rounded_box") {
        // 쿠션·매트리스처럼 모서리가 둥근 부품. 반지름은 가장 짧은 변의 비율
        const radius = Math.min(pw, height, pd) * Math.min(0.5, part.radius || 0.2);
        geometry = new RoundedBoxGeometry(pw, height, pd, 3, Math.max(0.001, radius));
      } else {
        geometry = new THREE.BoxGeometry(pw, height, pd);
      }
    }

    const mesh = new THREE.Mesh(geometry, mat);
    // y는 밑면 높이다. 도형은 중심 기준이라 절반만큼 올린 뒤, 그 중심에서 기울인다
    mesh.position.set(x, y + height / 2, z);
    mesh.rotation.set((part.rx || 0) * deg, (part.ry || 0) * deg, (part.rz || 0) * deg);
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    g.add(mesh);
  });

  return g;
}

// ── 바닥 무늬 ─────────────────────────────────────────
// 2D 그림의 SVG <pattern>을 브라우저가 이미지로 그리게 한 뒤 캔버스 텍스처로 쓴다.
// API를 부르지 않고 2D와 같은 바닥을 3D에 깐다.
const FLOOR_TEXTURES = new Map();
function floorPatternTexture(patternSvg, patternId, onReady) {
  const key = patternId + ":" + patternSvg.length;
  if (FLOOR_TEXTURES.has(key)) {
    onReady(FLOOR_TEXTURES.get(key));
    return;
  }
  const size = 512;
  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 240 240">` +
    `<defs>${patternSvg}</defs><rect width="240" height="240" fill="url(#${patternId})"/></svg>`;
  const image = new Image();
  image.onload = () => {
    const canvas = document.createElement("canvas");
    canvas.width = size;
    canvas.height = size;
    canvas.getContext("2d").drawImage(image, 0, 0, size, size);
    const texture = new THREE.CanvasTexture(canvas);
    texture.wrapS = THREE.RepeatWrapping;
    texture.wrapT = THREE.RepeatWrapping;
    texture.colorSpace = THREE.SRGBColorSpace;
    texture.anisotropy = 4;
    FLOOR_TEXTURES.set(key, texture);
    onReady(texture);
  };
  image.onerror = () => console.warn("[floorplan-3d] 바닥 무늬를 그리지 못해 단색으로 표시합니다");
  image.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
}

// ── AI 입체 그림 배치 ───────────────────────────────────
// 가구마다 이미지 모델이 그린 4방향 입체 그림(앞·오른쪽·뒤·왼쪽)을 받아, 가구 자리에
// 세운 판에 붙인다. 판은 카메라 쪽으로 몸을 돌리고, 카메라가 가구의 어느 쪽에 있는지에
// 따라 가장 가까운 방향의 그림으로 바꿔 끼운다. 그림 자체는 이미 빛과 그림자가 들어간
// 렌더라서 조명을 받지 않는 재질(MeshBasicMaterial)을 쓴다.
const VIEW_TEXTURES = new Map();
function viewTexture(url) {
  if (!VIEW_TEXTURES.has(url)) {
    const texture = new THREE.TextureLoader().load(url);
    texture.colorSpace = THREE.SRGBColorSpace;
    VIEW_TEXTURES.set(url, texture);
  }
  return VIEW_TEXTURES.get(url);
}

// ── 2D 그림(SVG) 돌출 ─────────────────────────────────
// 2D 평면도의 위에서 본 가구 SVG를 그대로 읽어 도형마다 위로 밀어 올린다. 2D와
// 같은 그림이라 모양·색이 같고, 3D를 위해 API를 부르지 않는다.
// 높이는 Gemini가 2D 그림을 그릴 때 도형마다 적은 값(data-z0·z1, 가구 높이 대비
// 비율)을 쓴다. 그 값이 없는 예전 그림·기본 모양은 아래 규칙으로 정한다.
const SVG_LOADER = new SVGLoader();
// 상판 아래에 다리가 보여야 자연스러운 종류. 높이 정보가 없을 때 다리를 붙인다
const LEGGED_TYPES = new Set(["desk", "table", "low_table", "chair", "desk_chair", "stool", "bench", "nightstand", "vanity"]);
const DECAL_M = 0.004;

function svgAttr(node, name) {
  // 값은 도형 자신, 없으면 묶은 <g>에서 물려받는다
  for (let el = node; el && el.getAttribute; el = el.parentNode) {
    const value = el.getAttribute(name);
    if (value !== null && value !== "") return value;
  }
  return null;
}

function svgNumber(node, name) {
  const value = parseFloat(svgAttr(node, name));
  return Number.isFinite(value) ? value : null;
}

function clamp01(value) {
  return Math.min(1, Math.max(0, value));
}

// 도형 윤곽을 가구 바닥면 좌표(m, 가구 중심 원점)로 옮긴 Shape 목록
function solidShapes(path, toLocal) {
  const out = [];
  SVGLoader.createShapes(path).forEach((shape) => {
    const { shape: outline, holes } = shape.extractPoints(10);
    if (outline.length < 3) return;
    const mapped = new THREE.Shape(outline.map(toLocal));
    holes.forEach((hole) => {
      if (hole.length >= 3) mapped.holes.push(new THREE.Path(hole.map(toLocal)));
    });
    out.push(mapped);
  });
  return out;
}

function footprintArea(shapes) {
  let area = 0;
  shapes.forEach((shape) => {
    area += Math.abs(THREE.ShapeUtils.area(shape.getPoints()));
  });
  return area;
}

// 도형 하나를 z0~z1(m) 사이 기둥으로 세운다. soft는 모서리 둥글기, taper는 위로 좁아지는 정도
function extrude(shapes, z0, z1, color, { soft = 0, taper = 0, opacity = 1 } = {}) {
  const height = Math.max(z1 - z0, DECAL_M);
  const group = new THREE.Group();
  shapes.forEach((shape) => {
    const box = new THREE.Box2().setFromPoints(shape.getPoints());
    const size = box.getSize(new THREE.Vector2());
    const rawBevel = Math.min(soft * Math.min(size.x, size.y) * 0.25, height * 0.45, 0.06);
    const bevel = rawBevel > 0.002 ? rawBevel : 0;
    const geometry = new THREE.ExtrudeGeometry(shape, {
      depth: Math.max(height - 2 * bevel, 0.0005),
      bevelEnabled: bevel > 0,
      bevelThickness: bevel,
      bevelSize: bevel,
      bevelOffset: -bevel,
      bevelSegments: 3,
      curveSegments: 8,
    });
    // 도형 평면(x, y)을 바닥(x, z)으로 눕힌다. 돌출 방향은 아래라 높이만큼 올린다
    geometry.rotateX(Math.PI / 2);
    geometry.translate(0, z0 + height - bevel, 0);
    if (taper > 0) {
      const center = box.getCenter(new THREE.Vector2());
      const pos = geometry.attributes.position;
      for (let i = 0; i < pos.count; i += 1) {
        const t = clamp01((pos.getY(i) - z0) / height);
        const k = 1 - Math.min(taper, 0.5) * t;
        pos.setX(i, center.x + (pos.getX(i) - center.x) * k);
        pos.setZ(i, center.y + (pos.getZ(i) - center.y) * k);
      }
      geometry.computeVertexNormals();
    }
    const mesh = new THREE.Mesh(
      geometry,
      material(color, 1, {
        side: THREE.DoubleSide,
        roughness: soft > 0.3 ? 0.9 : 0.7,
        transparent: opacity < 0.98,
        opacity,
      })
    );
    mesh.castShadow = opacity > 0.5;
    mesh.receiveShadow = true;
    group.add(mesh);
  });
  return group;
}

function buildSolid(obj) {
  const art = obj.art3d;
  const [bx0, by0, bx1, by1] = art.box;
  const sx = obj.w_m / Math.max(bx1 - bx0, 1e-6);
  const sy = obj.d_m / Math.max(by1 - by0, 1e-6);
  // 2D와 같은 기준 범위를 바닥면에 맞춘다. 그림의 위쪽(y=0)이 가구 뒤쪽이다
  const toLocal = (p) => new THREE.Vector2(
    (p.x - bx0) * sx - obj.w_m / 2,
    (p.y - by0) * sy - obj.d_m / 2
  );
  const H = obj.height_m;
  const parts = [];
  SVG_LOADER.parse(art.svg).paths.forEach((path) => {
    const node = path.userData && path.userData.node;
    const style = (path.userData && path.userData.style) || {};
    if (!node) return;
    const tag = node.nodeName.toLowerCase();
    if (tag === "line" || tag === "polyline") return;  // 선은 면이 없다
    if (svgAttr(node, "data-3d") === "skip") return;
    if (!style.fill || style.fill === "none" || style.fill === "transparent") return;
    const opacity = (style.opacity ?? 1) * (style.fillOpacity ?? 1);
    // 아주 옅은 칠은 그림자·빛 같은 효과라 세우지 않는다
    if (opacity < 0.3) return;
    const shapes = solidShapes(path, toLocal);
    if (!shapes.length) return;
    parts.push({
      node,
      shapes,
      color: "#" + path.color.getHexString(),
      opacity: Math.min(opacity, 1),
      area: footprintArea(shapes),
      z0: svgNumber(node, "data-z0"),
      z1: svgNumber(node, "data-z1"),
      soft: clamp01(svgNumber(node, "data-soft") ?? 0),
      taper: clamp01(svgNumber(node, "data-taper") ?? 0),
    });
  });
  if (!parts.length) return null;

  const group = new THREE.Group();
  const hinted = parts.some((p) => p.z1 !== null);
  if (hinted) {
    // 같은 높이 면이 겹치면 깜박이므로, 나중 도형(2D에서 위에 그린 것)을 아주 조금 올린다
    parts.forEach((p, i) => {
      // 값을 빠뜨린 도형은 몸체 기둥이 되지 않게 맨 위 무늬로 붙인다
      const z1 = clamp01(p.z1 ?? 1) * H;
      const z0 = p.z1 === null ? z1 : Math.min(clamp01(p.z0 ?? 0) * H, z1);
      const lift = i * 0.0006;
      const decal = z1 - z0 < DECAL_M;
      group.add(extrude(p.shapes, decal ? z1 + lift : z0, (decal ? z1 + DECAL_M : z1) + lift, p.color, p));
    });
    return group;
  }

  // 높이 정보가 없을 때: 가장 큰 도형을 몸체로, 그 뒤 도형은 몸체 위에 얹는다.
  // 몸체보다 앞에 그린 도형은 대개 바닥 그림자라 뺀다
  const footprint = obj.w_m * obj.d_m;
  let baseIndex = parts.findIndex((p) => p.area >= footprint * 0.4);
  if (baseIndex < 0) baseIndex = parts.reduce((best, p, i) => (p.area > parts[best].area ? i : best), 0);
  const base = parts[baseIndex];
  const legged = LEGGED_TYPES.has(obj.type);
  const slab = legged ? Math.min(0.05, H * 0.12) : H;
  group.add(extrude(base.shapes, H - slab, H, base.color, { soft: obj.type === "bed" || obj.type === "sofa" ? 0.4 : 0.1 }));
  if (legged) {
    const leg = Math.max(0.03, Math.min(obj.w_m, obj.d_m) * 0.06);
    const inset = leg * 1.2;
    const legColor = shade(base.color, 0.8);
    [[-1, -1], [1, -1], [-1, 1], [1, 1]].forEach(([ix, iz]) => {
      group.add(box(
        leg, H - slab, leg, material(legColor),
        ix * (obj.w_m / 2 - inset), 0, iz * (obj.d_m / 2 - inset)
      ));
    });
  }
  parts.slice(baseIndex + 1).forEach((p, i) => {
    const lift = (i + 1) * 0.0006;
    // 중간 크기 도형(쿠션·베개)은 조금 도톰하게, 작은 무늬는 얇게 붙인다.
    // 몸체만 한 도형은 대개 윗면 테두리·안쪽 면이라 도톰하게 올리면 가구가 높아진다
    const share = p.area / footprint;
    const raised = share >= 0.08 && share < 0.6 ? Math.min(0.06, H * 0.12) : DECAL_M;
    group.add(extrude(p.shapes, H + lift, H + raised + lift, p.color, { soft: raised > DECAL_M ? 0.6 : 0, opacity: p.opacity }));
  });
  return group;
}

// AI 그림을 그리지 않는 종류(gemini_furniture_parts.SKIP_TYPES와 같게 유지).
// 방 구조에 가까워 기본 모양으로 그린다
const NO_ART_TYPES = new Set(["door", "window", "rug", "mirror", "curtain", "aircon"]);

// 그림을 기다리는 가구의 바닥 자리. 모형을 미리 세우면 그림과 모양이 달라 보여서
// 위치와 크기만 알 수 있게 바닥에 판을 깐다
function makePlaceholder(obj, failed) {
  const group = new THREE.Group();
  const color = failed ? 0xb04a3a : 0x8a7a6a;
  const plate = new THREE.Mesh(
    new THREE.PlaneGeometry(obj.w_m, obj.d_m),
    new THREE.MeshBasicMaterial({ color, transparent: true, opacity: failed ? 0.22 : 0.14, depthWrite: false })
  );
  plate.rotation.x = -Math.PI / 2;
  plate.position.y = 0.006;
  group.add(plate);
  const edge = new THREE.LineSegments(
    new THREE.EdgesGeometry(new THREE.PlaneGeometry(obj.w_m, obj.d_m)),
    new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.8 })
  );
  edge.rotation.x = -Math.PI / 2;
  edge.position.y = 0.008;
  group.add(edge);
  return group;
}

function makeArtBoard(obj, views) {
  const material = new THREE.MeshBasicMaterial({
    map: viewTexture(views.views.front),
    transparent: true,
    alphaTest: 0.08,
    side: THREE.DoubleSide,
    depthWrite: true,
  });
  // 여러 번 다시 세워도 같은 텍스처를 쓰므로 뷰어를 버릴 때 텍스처는 지우지 않는다
  material.userData.sharedMap = true;
  const board = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), material);
  board.userData.views = views;
  board.userData.current = "";
  board.renderOrder = 2;
  return board;
}

// 판 크기를 그림 방향에 맞춘다. 비스듬히 위에서 본 그림이라 옆면이 조금 보이는 만큼 넓힌다
function sizeArtBoard(board, obj, view) {
  const views = board.userData.views;
  const size = (views.sizes || {})[view] || [1, 1];
  const side = view === "left" || view === "right";
  const across = side ? obj.d_m + obj.w_m * 0.3 : obj.w_m + obj.d_m * 0.3;
  const height = across * (size[1] / Math.max(1, size[0]));
  board.scale.set(across, height, 1);
  board.userData.height = height;
  board.material.map = viewTexture(views.views[view] || views.views.front);
  board.material.needsUpdate = true;
  board.userData.current = view;
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
    const floorMaterial = new THREE.MeshStandardMaterial({ color: floor_color, roughness: 0.85 });
    const floor = new THREE.Mesh(new THREE.PlaneGeometry(w, d), floorMaterial);
    floor.rotation.x = -Math.PI / 2;
    floor.receiveShadow = true;
    this.scene.add(floor);
    // 2D 그림의 바닥 무늬(원목·타일)가 있으면 이미지로 그려 바닥에 타일처럼 깐다
    const { floor_pattern_svg: patternSvg, floor_pattern_id: patternId } = this.data.room;
    if (patternSvg && patternId) {
      floorPatternTexture(patternSvg, patternId, (texture) => {
        if (this.disposed) return;
        // 무늬 한 장이 실제 바닥 1.2m를 덮도록 반복한다
        texture.repeat.set(w / 1.2, d / 1.2);
        floorMaterial.map = texture;
        floorMaterial.color.set("#ffffff");
        floorMaterial.needsUpdate = true;
      });
    }

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
    this.grid = grid;

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
    this.artGroup = new THREE.Group();
    this.artBoards = [];
    // views_state: "on" 그림을 기다린다 / "off" 그림 기능이 꺼져 기본 모양으로 그린다
    this.awaitArt = this.data.views_state === "on";
    const failedViews = new Set(this.data.views_failed || []);

    this.data.objects.forEach((obj) => {
      // 문·창문은 3D에 세우지 않는다. 기본 상자 모양이 오히려 방을 어색하게 보이게 해서
      // 위치 확인은 2D 평면도로 한다(배치 계산의 문 앞 비우기는 서버에서 그대로 한다)
      if (obj.type === "door" || obj.type === "window") return;
      // 설계도가 있으면 그것으로, 없으면 손으로 짠 빌더로 세운다.
      // 설계도는 Gemini 호출이라 없을 수 있어서 항상 빌더가 뒤를 받친다.
      // 모양은 멀티모달 모델이 가구별로 만든 부품 목록이 우선이다. 아직 없거나 실패한
      // 가구만 파라메트릭 모양으로 그린다. 타입 단위 옛 설계도는 상품에는 쓰지 않는다
      const own = (this.data.object_parts || {})[obj.id];
      const typeRecipe = obj.is_product ? null : (this.data.furniture_parts || {})[obj.type];
      const recipe = own && own.parts && own.parts.length ? own : typeRecipe;
      const build = PARAMETRIC[obj.type] || BUILDERS[obj.type] || BUILDERS.unknown;
      const useRecipe = recipe && recipe.parts && recipe.parts.length;
      let node;
      try {
        node = useRecipe
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
      // AI 입체 그림이 있으면 모형 대신 그림을 보여 준다. 모형은 숨기되 남겨서
      // 마우스 판정(고르기·끌기)과 바닥 그림자 계산에 쓴다
      const views = (this.data.object_views || {})[obj.id];
      const hasViews = Boolean(views && views.views && views.views.front && !obj.wall_mounted);
      // 2D 그림을 밀어 올린 입체. 이미지 입체 그림(예전 방식)을 켜 둔 경우에만 그쪽이 우선이다
      let solid = null;
      if (!hasViews && obj.art3d && obj.art3d.svg) {
        try {
          solid = buildSolid(obj);
        } catch (error) {
          console.warn("[floorplan-3d] 2D 그림을 입체로 세우지 못했습니다:", obj.id, error);
        }
      }
      const wantsArt = this.awaitArt && !obj.wall_mounted && !NO_ART_TYPES.has(obj.type);
      let artState = "";
      if (solid) {
        // 숨긴 모형은 고르기·끌기 판정에만 쓴다
        node.visible = false;
        wrapper.add(solid);
      } else if (wantsArt && !hasViews) {
        // 기본 모양은 보여 주지 않는다. 숨긴 모형은 고르기·끌기 판정에만 쓴다
        node.visible = false;
        artState = failedViews.has(obj.id) ? "failed" : "drawing";
        wrapper.add(makePlaceholder(obj, artState === "failed"));
      } else if (views && views.views && views.views.front && !obj.wall_mounted) {
        node.visible = false;
        const shadow = new THREE.Mesh(
          new THREE.CircleGeometry(0.5, 32),
          new THREE.MeshBasicMaterial({ color: 0x000000, transparent: true, opacity: 0.16, depthWrite: false })
        );
        shadow.rotation.x = -Math.PI / 2;
        shadow.scale.set(obj.w_m * 1.05, obj.d_m * 1.05, 1);
        shadow.position.y = 0.004;
        wrapper.add(shadow);
        const board = makeArtBoard(obj, views);
        this.artBoards.push({ board, wrapper, obj });
        this.artGroup.add(board);
      }
      wrapper.position.set(x, obj.base_m, z);
      // rotation_deg 는 위에서 본 시계방향 각. three.js Y축 회전은 반대라 부호를 뒤집는다.
      wrapper.rotation.y = -THREE.MathUtils.degToRad(obj.rotation_deg);
      wrapper.userData.object = obj;
      this.furnitureGroup.add(wrapper);
      this.pickables.push(wrapper);

      const labelText = (obj.marker
        ? `${obj.label} #${obj.marker}`
        : obj.label)
        + (artState === "drawing" ? " · 그리는 중" : artState === "failed" ? " · 그림 실패" : "");
      const label = makeLabel(labelText, { accent: Boolean(obj.is_product) });
      // 자리 표시만 있을 때는 빈 공중에 뜨지 않게 바닥 가까이 둔다
      const labelY = artState ? 0.35 : obj.base_m + obj.height_m + 0.2;
      label.position.set(x, labelY, z);
      label.userData.object = obj;
      wrapper.userData.label = label;
      this.labelGroup.add(label);
    });

    this.scene.add(this.furnitureGroup);
    this.scene.add(this.labelGroup);
    this.scene.add(this.artGroup);
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

  toggleGrid() {
    if (!this.grid) return false;
    this.grid.visible = !this.grid.visible;
    return this.grid.visible;
  }

  toggleLabels() {
    this.labelsVisible = !this.labelsVisible;
    this.labelGroup.visible = this.labelsVisible;
    return this.labelsVisible;
  }

  // 편집 모드: 가구를 바닥 위로 끌어 옮기고, 고른 가구를 90°씩 돌린다.
  // 화면에서는 바로 움직이고, 손을 떼면 onEdit로 서버에 알린다. 서버가 겹친 가구를
  // 비켜 준 결과로 다시 그리므로 여기서는 충돌을 따지지 않는다.
  setEditable(on) {
    this.editable = Boolean(on);
    if (!this.editable) this._select(null);
    this.renderer.domElement.style.cursor = this.editable ? "grab" : "";
  }

  _select(wrapper) {
    if (this.selected && this.selected !== this.hovered) this._setHighlight(this.selected, false);
    this.selected = wrapper;
    if (wrapper) this._setHighlight(wrapper, true);
    if (typeof this.onSelect === "function") {
      this.onSelect(wrapper ? wrapper.userData.object : null);
    }
  }

  rotateSelected(step = 90) {
    if (!this.selected || typeof this.onEdit !== "function") return;
    const obj = this.selected.userData.object;
    this.onEdit({ op: "rotate", id: obj.id, rotation_deg: (obj.rotation_deg + step + 360) % 360 });
  }

  _floorPoint() {
    const plane = new THREE.Plane(new THREE.Vector3(0, 1, 0), 0);
    const point = new THREE.Vector3();
    this.raycaster.setFromCamera(this.pointer, this.camera);
    return this.raycaster.ray.intersectPlane(plane, point) ? point : null;
  }

  _bindEvents() {
    this.raycaster = new THREE.Raycaster();
    this.pointer = new THREE.Vector2();
    this.hovered = null;
    this.editable = false;
    this.selected = null;
    this.dragging = null;

    const updatePointer = (event) => {
      const rect = this.renderer.domElement.getBoundingClientRect();
      if (!rect.width || !rect.height) return false;
      this.pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
      this.pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
      return true;
    };

    this._onPointerMove = (event) => {
      if (!updatePointer(event)) return;
      if (this.dragging) {
        const point = this._floorPoint();
        if (!point) return;
        const { wrapper, offset, label } = this.dragging;
        wrapper.position.x = point.x - offset.x;
        wrapper.position.z = point.z - offset.z;
        if (label) {
          label.position.x = wrapper.position.x;
          label.position.z = wrapper.position.z;
        }
        this.dragging.moved = true;
        return;
      }
      this._pick();
    };
    this.renderer.domElement.addEventListener("pointermove", this._onPointerMove);

    this._onPointerDown = (event) => {
      if (!this.editable || !updatePointer(event)) return;
      this._pick();
      if (!this.hovered) {
        this._select(null);
        return;
      }
      const point = this._floorPoint();
      if (!point) return;
      const wrapper = this.hovered;
      this.dragging = {
        wrapper,
        label: wrapper.userData.label,
        offset: new THREE.Vector3(point.x - wrapper.position.x, 0, point.z - wrapper.position.z),
        moved: false,
      };
      // 끄는 동안 카메라가 같이 돌지 않게 한다
      this.controls.enabled = false;
      this.renderer.domElement.setPointerCapture(event.pointerId);
      this.renderer.domElement.style.cursor = "grabbing";
    };
    this.renderer.domElement.addEventListener("pointerdown", this._onPointerDown);

    this._onPointerUp = (event) => {
      if (!this.dragging) return;
      const { wrapper, moved } = this.dragging;
      this.dragging = null;
      this.controls.enabled = true;
      this.renderer.domElement.style.cursor = this.editable ? "grab" : "";
      if (this.renderer.domElement.hasPointerCapture(event.pointerId)) {
        this.renderer.domElement.releasePointerCapture(event.pointerId);
      }
      if (!moved) {
        this._select(wrapper);
        return;
      }
      const obj = wrapper.userData.object;
      const { width_m: w, depth_m: d } = this.data.room;
      // three.js 좌표는 방 중심 원점이다. Scene Graph의 좌상단 원점 미터로 되돌린다
      const cx = Math.min(w, Math.max(0, wrapper.position.x + w / 2));
      const cy = Math.min(d, Math.max(0, wrapper.position.z + d / 2));
      this._select(wrapper);
      if (typeof this.onEdit === "function") {
        this.onEdit({ op: "move", id: obj.id, cx: Number(cx.toFixed(3)), cy: Number(cy.toFixed(3)) });
      }
    };
    this.renderer.domElement.addEventListener("pointerup", this._onPointerUp);
    this.renderer.domElement.addEventListener("pointercancel", this._onPointerUp);

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
    this._updateArtBoards();
    // 라벨이 항상 카메라를 향하도록(스프라이트는 자동이지만 크기 보정용)
    this.renderer.render(this.scene, this.camera);
  }

  // 그림 판이 가구를 따라가고, 카메라를 향하고, 보는 방향에 맞는 그림으로 바뀐다
  _updateArtBoards() {
    if (!this.artBoards || !this.artBoards.length) return;
    const camera = this.camera.position;
    this.artBoards.forEach(({ board, wrapper, obj }) => {
      const pos = wrapper.position;
      const dx = camera.x - pos.x;
      const dz = camera.z - pos.z;
      // 가구 기준 좌표로 카메라 방향을 돌려 본다. 가구 앞면은 +z다
      const yaw = wrapper.rotation.y;
      const localX = dx * Math.cos(yaw) - dz * Math.sin(yaw);
      const localZ = dx * Math.sin(yaw) + dz * Math.cos(yaw);
      const angle = Math.atan2(localX, localZ) * 180 / Math.PI;
      let view = "front";
      if (Math.abs(angle) > 135) view = "back";
      else if (angle < -45) view = "right";
      else if (angle > 45) view = "left";
      const available = board.userData.views.views;
      if (!available[view]) view = "front";
      if (board.userData.current !== view) sizeArtBoard(board, obj, view);
      board.position.set(pos.x, pos.y + board.userData.height / 2, pos.z);
      board.rotation.set(0, Math.atan2(dx, dz), 0);
    });
  }

  dispose() {
    this.disposed = true;
    if (this.frame) cancelAnimationFrame(this.frame);
    window.removeEventListener("resize", this._onResize);
    this.renderer.domElement.removeEventListener("pointermove", this._onPointerMove);
    this.renderer.domElement.removeEventListener("pointerdown", this._onPointerDown);
    this.renderer.domElement.removeEventListener("pointerup", this._onPointerUp);
    this.renderer.domElement.removeEventListener("pointercancel", this._onPointerUp);
    this.controls.dispose();
    this.scene.traverse((node) => {
      if (node.geometry) node.geometry.dispose();
      if (node.material) {
        const mats = Array.isArray(node.material) ? node.material : [node.material];
        mats.forEach((m) => {
          if (m.map && !(m.userData && m.userData.sharedMap)) m.map.dispose();
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

  let data = readScene();
  const status = document.getElementById("floorplan3dStatus");
  // 그림을 받을 주소가 있으면 기본 모양 대신 자리 표시로 시작한다
  if (data && host.dataset.viewsUrl && !data.views_state) data.views_state = "on";

  if (!data || !data.objects || !data.objects.length) {
    if (toggle) {
      toggle.disabled = true;
      toggle.title = "3D로 표시할 배치 정보가 없습니다.";
    }
    return;
  }

  let viewer = null;
  // 화면마다 2D 평면도 상자가 다르다(평면도 화면 / 결과 화면)
  const box2d = document.getElementById(host.dataset.box2d || "editableFloorplanBox");
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
        // 자동 브라우저 테스트가 뷰어에 접근할 수 있게 DOM 속성으로만 남긴다
        host.__viewer = viewer;
        window.dispatchEvent(new CustomEvent("floorplan:viewer-ready"));
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

  // 2D 편집을 저장하면 새 배치가 온다. 열려 있던 3D를 같은 배치로 다시 세운다.
  window.addEventListener("floorplan:scene-updated", (event) => {
    const next = event.detail;
    if (!next || !next.room || !next.objects) return;
    // 가구 형태 설계도는 배치와 무관하므로 이어서 쓴다
    if (data && data.furniture_parts && !next.furniture_parts) {
      next.furniture_parts = data.furniture_parts;
    }
    if (data && data.object_parts && !next.object_parts) {
      next.object_parts = data.object_parts;
    }
    if (data && data.object_views && !next.object_views) {
      next.object_views = data.object_views;
    }
    if (data && data.views_state && !next.views_state) {
      next.views_state = data.views_state;
    }
    if (data && data.views_failed && !next.views_failed) {
      next.views_failed = data.views_failed;
    }
    if (data && data.grid_hidden && next.grid_hidden === undefined) {
      next.grid_hidden = data.grid_hidden;
    }
    data = next;
    const visible = !host.classList.contains("d-none");
    let camera = null;
    let selectedId = null;
    if (viewer) {
      // 다시 세워도 보던 시점과 고른 가구를 유지한다
      camera = { position: viewer.camera.position.clone(), target: viewer.controls.target.clone() };
      selectedId = viewer.selected ? viewer.selected.userData.object.id : null;
      viewer.dispose();
      viewer = null;
    }
    if (visible) {
      show3d();
      if (viewer && camera) {
        viewer.camera.position.copy(camera.position);
        viewer.controls.target.copy(camera.target);
        viewer.controls.update();
      }
      if (viewer && selectedId) {
        const wrapper = viewer.pickables.find((item) => item.userData.object.id === selectedId);
        if (wrapper) viewer._select(wrapper);
      }
    }
  });

  // ── 3D 편집 (항목 16) ─────────────────────────────────
  const editUrl = host.dataset.editUrl;
  const editContext = host.dataset.editContext || "floorplan";
  const editButton = document.getElementById("floorplan3dEdit");
  const rotateButton = document.getElementById("floorplan3dRotate");
  const removeButton = document.getElementById("floorplan3dRemove");
  let editing3d = false;
  let busy = false;

  async function sendEdit(op) {
    if (!editUrl || busy) return;
    busy = true;
    if (status) status.textContent = "배치를 저장하고 있습니다…";
    try {
      const response = await fetch(editUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ops: [op], context: editContext }),
      });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.error || "저장하지 못했습니다.");
      // 같은 화면에 2D 평면도가 있으면 같은 배치로 바꾼다
      const current = box2d ? box2d.querySelector("svg") : null;
      const freshMarkup = editContext === "final" ? result.modified_svg || result.svg : result.svg;
      if (current && freshMarkup) {
        const holder = document.createElement("div");
        holder.innerHTML = freshMarkup;
        const fresh = holder.querySelector("svg");
        if (fresh) {
          current.replaceWith(fresh);
          if (window.initFloorplanDrag) window.initFloorplanDrag();
        }
      }
      window.dispatchEvent(new CustomEvent("floorplan:scene-updated", { detail: result.scene_3d }));
      window.dispatchEvent(new CustomEvent("floorplan:uncertain-updated", { detail: result.uncertain || [] }));
      const moved = (result.adjustments || []).filter((a) => String(a.reason || "").startsWith("collision")).length;
      if (status) {
        status.textContent = moved
          ? `저장했습니다. 겹친 가구 ${moved}개를 옆으로 비켜 놓았어요.`
          : "저장했습니다. 2D 평면도에도 같은 배치가 반영됐어요.";
      }
    } catch (error) {
      if (status) status.textContent = error.message || "저장하지 못했습니다.";
      // 실패하면 마지막으로 저장된 배치로 되돌린다
      window.dispatchEvent(new CustomEvent("floorplan:scene-updated", { detail: data }));
    } finally {
      busy = false;
    }
  }

  function attachEditing() {
    if (!viewer) return;
    viewer.onEdit = sendEdit;
    viewer.onSelect = (obj) => {
      if (rotateButton) rotateButton.disabled = !obj || !editing3d;
      // 지우기는 결과에 추가한 상품만 한다(기존 가구는 유지·제거 버튼으로 다룬다)
      if (removeButton) removeButton.disabled = !obj || !editing3d || !obj.is_product;
      if (obj && status && editing3d) status.textContent = `${obj.label} 선택됨 — 끌어서 옮기거나 ↻(R 키)로 돌리세요.`;
    };
    viewer.setEditable(editing3d);
  }

  if (editButton && editUrl) {
    editButton.classList.remove("d-none");
    editButton.addEventListener("click", () => {
      editing3d = !editing3d;
      editButton.classList.toggle("active", editing3d);
      editButton.textContent = editing3d ? "옮기기 끝" : "가구 옮기기";
      if (rotateButton) rotateButton.classList.toggle("d-none", !editing3d);
      if (removeButton) removeButton.classList.toggle("d-none", !editing3d);
      attachEditing();
      if (status) {
        status.textContent = editing3d
          ? "가구를 끌어 옮기세요. 고른 가구는 ↻ 버튼이나 R 키로 90°씩 돌립니다."
          : "";
      }
    });
  }
  if (rotateButton) {
    rotateButton.addEventListener("click", () => viewer && viewer.rotateSelected(90));
  }
  if (removeButton) {
    removeButton.addEventListener("click", () => {
      if (!viewer || !viewer.selected) return;
      const obj = viewer.selected.userData.object;
      if (!obj.is_product) return;
      sendEdit({ op: "remove", id: obj.id });
      // 아래 "선택한 추천 가구" 목록의 카드도 지운다
      const marker = String(obj.id).split("_").pop();
      document.querySelectorAll(`.remove-product-btn[data-marker="${marker}"]`).forEach((el) => {
        const card = el.closest("[class*='col-']");
        if (card) card.remove();
      });
    });
  }
  window.addEventListener("keydown", (event) => {
    if (!editing3d || !viewer || (event.target.closest && event.target.closest("input, textarea, select"))) return;
    if (event.key === "r" || event.key === "R") viewer.rotateSelected(event.shiftKey ? -90 : 90);
  });
  // 뷰어가 새로 만들어질 때마다 편집 연결을 다시 건다
  window.addEventListener("floorplan:viewer-ready", attachEditing);

  // ── 가구별 형태 받기 ──────────────────────────────────
  // three.js는 배치만 맡고 모양은 서버(멀티모달 모델)가 가구별로 만든다. 배치를 먼저
  // 보여 주고, 형태가 오면 같은 배치로 다시 세운다. 이미 받은 가구는 다시 묻지 않는다.
  const partsUrl = host.dataset.partsUrl;
  const requested = new Set();
  async function loadParts() {
    if (!partsUrl || !data) return;
    const have = data.object_parts || {};
    const missing = (data.objects || []).filter((o) => !have[o.id] && !requested.has(o.id));
    if (!missing.length) return;
    missing.forEach((o) => requested.add(o.id));
    try {
      const response = await fetch(partsUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ context: editContext }),
      });
      const result = await response.json();
      const fresh = (result && result.object_parts) || {};
      const added = Object.keys(fresh).filter((id) => !have[id]);
      if (!added.length) return;
      data = { ...data, object_parts: { ...have, ...fresh } };
      window.dispatchEvent(new CustomEvent("floorplan:scene-updated", { detail: data }));
    } catch (error) {
      console.warn("[floorplan-3d] 가구 형태를 받지 못해 기본 모양으로 표시합니다:", error);
    }
  }
  window.addEventListener("floorplan:viewer-ready", loadParts);

  // ── AI 입체 그림 받기 ─────────────────────────────────
  // 가구당 이미지 여러 장을 그려 오래 걸린다. 서버가 두 가구씩 그려 주면 받은 만큼
  // 바로 세우고, 남은 가구가 있으면 다시 부른다.
  const viewsUrl = host.dataset.viewsUrl;
  let viewsBusy = false;
  async function loadViews() {
    // 꺼짐으로 바뀐 뒤 다시 세운 뷰어가 또 요청하면 끝없이 돈다
    if (!viewsUrl || viewsBusy || !data || data.views_state === "off") return;
    const have = data.object_views || {};
    // 그림이 없거나 아직 네 방향이 다 안 그려진 가구가 있으면 요청한다
    // 2D 그림으로 세운 가구는 이미지 입체 그림이 필요 없다
    const missing = (data.objects || []).filter(
      (o) => !o.wall_mounted && !o.art3d && (!have[o.id] || have[o.id].complete === false)
    );
    if (!missing.length) return;
    viewsBusy = true;
    try {
      if (status) status.textContent = "AI가 가구 입체 그림을 그리고 있어요…";
      const response = await fetch(viewsUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ context: editContext }),
      });
      const result = await response.json();
      if (result && result.enabled === false) {
        // 그림 기능이 꺼져 있으면 아무것도 안 보이는 대신 기본 모양으로 그린다
        viewsBusy = false;
        if (status) status.textContent = "";
        data = { ...data, views_state: "off" };
        window.dispatchEvent(new CustomEvent("floorplan:scene-updated", { detail: data }));
        return;
      }
      const fresh = (result && result.views) || {};
      const failedNow = (result && result.failed) || [];
      const failedBefore = data.views_failed || [];
      const failedChanged = failedNow.length !== failedBefore.length
        || failedNow.some((id) => !failedBefore.includes(id));
      // 새 가구뿐 아니라 방향이 늘어난 가구도 반영한다(앞 그림 먼저, 나머지는 나중에)
      const count = (entry) => Object.keys((entry && entry.views) || {}).length;
      const added = Object.keys(fresh).filter((id) => !have[id] || count(fresh[id]) > count(have[id]));
      if (added.length || failedChanged) {
        data = { ...data, object_views: { ...have, ...fresh }, views_failed: failedNow };
        window.dispatchEvent(new CustomEvent("floorplan:scene-updated", { detail: data }));
      }
      if (status) {
        status.textContent = result && result.remaining
          ? `AI 입체 그림을 그리는 중… 남은 가구 ${result.remaining}개`
          : failedNow.length
            ? `가구 ${failedNow.length}개는 그림을 만들지 못했어요. 잠시 뒤 새로고침하면 다시 그립니다.`
            : "";
      }
      viewsBusy = false;
      // 남은 가구가 있으면 이어서 그린다. 실패한 가구는 서버가 한동안 다시 그리지 않으므로
      // 실패만 늘어도 다음 가구로 넘어가고, 아무 변화가 없으면 멈춘다
      if (result && result.remaining && (added.length || failedChanged)) loadViews();
    } catch (error) {
      viewsBusy = false;
      console.warn("[floorplan-3d] 입체 그림을 받지 못해 모형으로 표시합니다:", error);
      // 그림을 받을 길이 없으면 빈 자리만 남기지 않고 기본 모양으로 그린다
      if (status) status.textContent = "AI 그림을 받지 못해 기본 모양으로 표시합니다.";
      data = { ...data, views_state: "off" };
      window.dispatchEvent(new CustomEvent("floorplan:scene-updated", { detail: data }));
    }
  }
  window.addEventListener("floorplan:viewer-ready", loadViews);


  // 전용 화면은 사용자가 누를 것도 없이 바로 3D를 보여준다
  if (autostart) show3d();

  // 1m 격자: 배치 확인용이라 기본으로 켜 두고, 배경을 깔끔하게 보고 싶으면 끈다
  const gridButton = document.getElementById("floorplan3dGrid");
  if (gridButton) {
    gridButton.addEventListener("click", () => {
      if (!viewer) return;
      const visible = viewer.toggleGrid();
      data = { ...data, grid_hidden: !visible };
      gridButton.classList.toggle("active", visible);
      gridButton.textContent = visible ? "격자 숨기기" : "격자 표시";
    });
  }
  // 다시 세운 뷰어에도 격자 선택을 유지한다
  window.addEventListener("floorplan:viewer-ready", () => {
    if (viewer && data && data.grid_hidden && viewer.grid) viewer.grid.visible = false;
  });

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
