import * as THREE from 'three';
import { GLTFLoader } from './vendor/GLTFLoader.js';
import { DRACOLoader } from './vendor/DRACOLoader.js';
import { Character } from './Character.js';

const OPERATORS = [
  { id: 'amiya', node: 'AI3D_Character', accent: 0x4d9eeb, x: -1.95 },
  { id: 'dania', node: 'AIModelRoot_Dania_Red', accent: 0xc67fae, x: -0.65 },
  { id: 'kaltsit', node: 'AI3D_Kaltsit', accent: 0x7fbf8c, x: 0.65 },
  { id: 'bigfish', node: 'AIModelRoot_DeepSeek', accent: 0x4d9eeb, x: 1.95 },
];

const container = document.getElementById('lounge-stage');
const wrap = document.getElementById('lounge-stage-wrap');
const placeholder = document.getElementById('lounge-stage-placeholder');
const progressText = document.getElementById('lounge-stage-progress');

if (!container) {
  throw new Error('lounge-stage container missing');
}

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x1a1512);
scene.fog = new THREE.Fog(0x1a1512, 8, 16);

const camera = new THREE.PerspectiveCamera(38, 1, 0.1, 60);
camera.position.set(0, 1.15, 3.9);

const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.15;
renderer.setPixelRatio(Math.min(devicePixelRatio, 1.5));
container.appendChild(renderer.domElement);

// 环境光压得很低，让角色灯和圣光主导画面
scene.add(new THREE.HemisphereLight(0xffffff, 0x2a211b, 1.5));
const rim = new THREE.DirectionalLight(0xbfd4ff, 0.35);
rim.position.set(-3, 4, -2);
scene.add(rim);

const draco = new DRACOLoader();
draco.setDecoderPath('/panel-assets/stage/vendor/draco/');
const loader = new GLTFLoader();
loader.setDRACOLoader(draco);

const clock = new THREE.Clock();
const characters = new Map();
const pickables = [];
let selectedId = null;
let needsRender = true;
let disposed = false;

function resize() {
  const w = container.clientWidth;
  const h = container.clientHeight;
  if (!w || !h) return;
  renderer.setSize(w, h);  // 同时更新 CSS 尺寸，避免 buffer 溢出容器被裁切
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  needsRender = true;
}

const ro = new ResizeObserver(resize);
ro.observe(container);
resize();

function setProgress(text) {
  if (progressText) progressText.textContent = text;
}

function addCharacterLights(root, accent) {
  // 根节点（阿米娅/凯尔希）的变换已烘焙到顶点，world position 不可靠，
  // 用包围盒中心定位灯光。
  const box = new THREE.Box3().setFromObject(root);
  const center = box.getCenter(new THREE.Vector3());
  const spot = new THREE.SpotLight(accent, 55, 8, 0.5, 0.45, 1.1);
  spot.position.set(center.x, box.max.y + 1.5, center.z + 0.15);
  spot.target.position.set(center.x, center.y, center.z);
  scene.add(spot);
  scene.add(spot.target);
  return spot;
}

function bindPicking(root, character) {
  root.traverse((obj) => {
    if (obj.isMesh || obj.isSkinnedMesh) {
      obj.userData.character = character;
      pickables.push(obj);
    }
  });
}

function frameCharacters() {
  // 相机固定在能完整容纳四个站位的位置；aspect 变化只更新投影矩阵。
  camera.position.set(0, 1.12, 3.9);
  camera.lookAt(0, 0.62, 0);
  camera.updateProjectionMatrix();
  camera.updateMatrixWorld(true);
  needsRender = true;
}

function select(id, notifyPanel = false) {
  if (!characters.has(id)) return;
  if (selectedId === id) return;
  if (selectedId && characters.has(selectedId)) {
    characters.get(selectedId).deselect();
  }
  selectedId = id;
  characters.get(id).select();
  needsRender = true;
  if (notifyPanel) {
    window.dispatchEvent(new CustomEvent('operator:changed', { detail: { id } }));
  }
}

const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2();
let pointerDown = null;

renderer.domElement.addEventListener('pointerdown', (e) => {
  pointerDown = { x: e.clientX, y: e.clientY };
});

renderer.domElement.addEventListener('pointerup', (e) => {
  if (!pointerDown) return;
  const moved = Math.hypot(e.clientX - pointerDown.x, e.clientY - pointerDown.y);
  pointerDown = null;
  if (moved > 6) return;

  const rect = renderer.domElement.getBoundingClientRect();
  pointer.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
  pointer.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
  raycaster.setFromCamera(pointer, camera);
  const hits = raycaster.intersectObjects(pickables, false);
  if (!hits.length) return;
  const character = hits[0].object.userData.character;
  if (character) {
    select(character.id, true);
  }
});

window.addEventListener('operator:select', (e) => {
  const id = e?.detail?.id;
  if (id) select(id, false);
});

function animate() {
  if (disposed) return;
  requestAnimationFrame(animate);
  const cw = container.clientWidth;
  const chh = container.clientHeight;
  if (cw && chh && Math.abs(camera.aspect - cw / chh) > 0.001) {
    camera.aspect = cw / chh;
    camera.updateProjectionMatrix();
    needsRender = true;
  }
  const dt = Math.min(clock.getDelta(), 0.1);
  let active = false;
  for (const character of characters.values()) {
    if (character.update(dt)) active = true;
  }
  if (active || needsRender) {
    renderer.render(scene, camera);
    needsRender = false;
  }
}

setProgress('加载角色模型…');

loader.load(
  '/panel-assets/stage/assets/lounge.glb',
  (gltf) => {
    scene.add(gltf.scene);

    const clips = {};
    for (const clip of gltf.animations) {
      clips[clip.name] = clip;
    }

    for (const def of OPERATORS) {
      const root = gltf.scene.getObjectByName(def.node);
      if (!root) {
        console.warn('[stage] missing operator node', def.id, def.node);
        continue;
      }
      const spot = addCharacterLights(root, def.accent);
      const character = new Character(def.id, root, clips, spot, def.accent);
      character.stageX = def.x;
      characters.set(def.id, character);
      bindPicking(root, character);
    }

    setProgress('就绪');
    if (wrap) wrap.classList.add('is-ready');
    if (placeholder) placeholder.classList.add('is-hidden');

    // 调试/联调入口：便于面板或控制台查询舞台状态
    window.__STAGE__ = { scene, camera, renderer, characters, select, THREE };

    frameCharacters();

    const initial = window.__INITIAL_OPERATOR__ || 'amiya';
    select(initial, false);

    clock.start();
    animate();
  },
  (event) => {
    if (event.total) {
      const pct = Math.round((event.loaded / event.total) * 100);
      setProgress(`加载场景 ${pct}%`);
    }
  },
  (err) => {
    console.error('[stage] load failed', err);
    setProgress('3D 加载失败，使用静态图');
    if (wrap) wrap.classList.add('is-failed');
    if (placeholder) placeholder.classList.add('is-hidden');
  }
);

window.addEventListener('beforeunload', () => {
  disposed = true;
  ro.disconnect();
  renderer.dispose();
});
