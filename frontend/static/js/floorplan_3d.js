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

// 재질 이름(가죽·금속·유리…)에 맞춘 표면. Gemini 부품 목록의 재질을 그린다
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

// 가구 높이 대비 비율. 수납장 위 소품처럼 몸체보다 조금 솟는 값은 살린다
function zFrac(value) {
  return Math.min(Math.max(value, 0), 1.6);
}

// 선으로 그린 다리·기둥(높이 구간이 있는 선)을 막대로 세운다. 위에서 본 선은 기울어진
// 다리의 그림자 같은 것이라, 가구 중심에 가까운 끝을 위(z1), 먼 끝을 바닥 쪽(z0)으로 본다
function strokeRod(path, node, style, toLocal, scale, H) {
  const z0 = svgNumber(node, "data-z0");
  const z1 = svgNumber(node, "data-z1");
  if (z0 === null || z1 === null || z1 - z0 < 0.02) return null;  // 무늬 선은 세우지 않는다
  if (!style.stroke || style.stroke === "none" || style.stroke === "transparent") return null;
  const sub = (path.subPaths || []).find((sp) => sp.getPoints().length >= 2);
  if (!sub) return null;
  let points = sub.getPoints().map(toLocal);
  if (points[points.length - 1].length() < points[0].length()) points = points.reverse();
  return {
    points,
    z0: zFrac(z0) * H,
    z1: zFrac(z1) * H,
    radius: Math.min(Math.max((style.strokeWidth || 2) * scale, 0.02), 0.06) / 2,
    color: "#" + new THREE.Color().setStyle(style.stroke).getHexString(),
  };
}

function buildRod(rod) {
  const group = new THREE.Group();
  const mat = material(rod.color, 1, { roughness: 0.5 });
  const lengths = [0];
  for (let i = 1; i < rod.points.length; i += 1) {
    lengths.push(lengths[i - 1] + rod.points[i].distanceTo(rod.points[i - 1]));
  }
  const total = lengths[lengths.length - 1];
  const at = (i) => {
    const t = total > 1e-4 ? lengths[i] / total : 0;
    return new THREE.Vector3(rod.points[i].x, rod.z1 + (rod.z0 - rod.z1) * t, rod.points[i].y);
  };
  const segments = total > 1e-4
    ? rod.points.slice(1).map((_, i) => [at(i), at(i + 1)])
    // 위에서 보면 점인 선은 곧게 선 기둥이다
    : [[new THREE.Vector3(rod.points[0].x, rod.z1, rod.points[0].y), new THREE.Vector3(rod.points[0].x, rod.z0, rod.points[0].y)]];
  segments.forEach(([a, b]) => {
    const dir = new THREE.Vector3().subVectors(b, a);
    const length = dir.length();
    if (length < 1e-4) return;
    const mesh = new THREE.Mesh(new THREE.CylinderGeometry(rod.radius, rod.radius, length, 10), mat);
    mesh.position.copy(a).add(b).multiplyScalar(0.5);
    mesh.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), dir.normalize());
    mesh.castShadow = true;
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
  const rods = [];
  SVG_LOADER.parse(art.svg).paths.forEach((path) => {
    const node = path.userData && path.userData.node;
    const style = (path.userData && path.userData.style) || {};
    if (!node) return;
    const tag = node.nodeName.toLowerCase();
    if (svgAttr(node, "data-3d") === "skip") return;
    // 면이 없는 선은 높이 구간이 있을 때만 막대(다리·기둥)로 세운다
    if (tag === "line" || tag === "polyline" || !style.fill || style.fill === "none" || style.fill === "transparent") {
      const rod = strokeRod(path, node, style, toLocal, (sx + sy) / 2, H);
      if (rod) rods.push(rod);
      return;
    }
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
  if (!parts.length && !rods.length) return null;

  const group = new THREE.Group();
  rods.forEach((rod) => group.add(buildRod(rod)));
  const hinted = rods.length > 0 || parts.some((p) => p.z1 !== null);
  if (hinted) {
    // 같은 높이 면이 겹치면 깜박이므로, 나중 도형(2D에서 위에 그린 것)을 아주 조금 올린다
    parts.forEach((p, i) => {
      // 값을 빠뜨린 도형은 몸체 기둥이 되지 않게 맨 위 무늬로 붙인다
      const z1 = zFrac(p.z1 ?? 1) * H;
      const z0 = p.z1 === null ? z1 : Math.min(zFrac(p.z0 ?? 0) * H, z1);
      const lift = i * 0.0006;
      const decal = z1 - z0 < DECAL_M;
      group.add(extrude(p.shapes, decal ? z1 + lift : z0, (decal ? z1 + DECAL_M : z1) + lift, p.color, p));
    });
    return group;
  }

  // 높이 정보가 없을 때(2D 기본 모양 등): 가장 큰 도형을 몸체로 가구 높이까지 세우고,
  // 그 뒤에 그린 도형은 윗면 무늬로 붙인다. 몸체보다 앞에 그린 도형은 대개 바닥 그림자라 뺀다
  const baseIndex = parts.reduce((best, p, i) => (p.area > parts[best].area ? i : best), 0);
  const base = parts[baseIndex];
  group.add(extrude(base.shapes, 0, H, base.color));
  parts.slice(baseIndex + 1).forEach((p, i) => {
    const lift = (i + 1) * 0.0006;
    group.add(extrude(p.shapes, H + lift, H + DECAL_M + lift, p.color, { opacity: p.opacity }));
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
      // 모양은 2D SVG 돌출(아래)이 기본이다. Gemini 부품 목록이 있으면 그것을 쓴다.
      // 손으로 짠 기본 모양은 없다. node는 고르기·끌기 판정용 상자로, 보이지 않는다
      const own = (this.data.object_parts || {})[obj.id];
      const typeRecipe = obj.is_product ? null : (this.data.furniture_parts || {})[obj.type];
      const recipe = own && own.parts && own.parts.length ? own : typeRecipe;
      let node = null;
      if (recipe && recipe.parts && recipe.parts.length) {
        try {
          node = buildFromParts(obj, recipe.parts);
        } catch (error) {
          console.warn("[floorplan-3d] 부품 목록으로 세우지 못했습니다:", obj.id, error);
        }
      }
      if (!node) {
        node = new THREE.Mesh(
          new THREE.BoxGeometry(obj.w_m, obj.height_m, obj.d_m),
          new THREE.MeshBasicMaterial({ visible: false })
        );
        node.position.y = obj.height_m / 2;
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
      } else if (!hasViews && !node.isGroup) {
        // 세울 그림이 없다. 기본 모양 대신 바닥 자리만 보여 준다
        artState = wantsArt && !failedViews.has(obj.id) ? "drawing" : "failed";
        wrapper.add(makePlaceholder(obj, artState === "failed"));
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
