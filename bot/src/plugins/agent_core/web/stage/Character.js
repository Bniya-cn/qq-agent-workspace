import * as THREE from 'three';

const IDLE_A_SECONDS = 4;
const IDLE_B_SECONDS = 5;
const FADE = 0.3;

/**
 * 单个角色的动画状态机：idle(A/B 交替循环) ↔ selected(Hover 一次 + IdleA)。
 * Hover 由前端触发，与 bot 实际任务无关。
 */
export class Character {
  constructor(id, root, clips, spotlight, accentColor) {
    this.id = id;
    this.root = root;
    this.mixer = new THREE.AnimationMixer(root);
    this.clips = clips;
    this.spotlight = spotlight;
    this.accentColor = accentColor;

    this.baseY = root.position.y;
    this.selected = false;
    this.idleKey = 'A';
    this.idleElapsed = 0;

    this.actionA = clips[idleName(id, 'A')] ? this.mixer.clipAction(clips[idleName(id, 'A')]) : null;
    this.actionB = clips[idleName(id, 'B')] ? this.mixer.clipAction(clips[idleName(id, 'B')]) : null;
    this.actionHover = clips[hoverName(id)] ? this.mixer.clipAction(clips[hoverName(id)]) : null;

    if (this.actionHover) {
      this.actionHover.setLoop(THREE.LoopOnce, 1);
      this.actionHover.clampWhenFinished = true;
      this.actionHover.timeScale = 1;
    }
    if (this.actionA) this.actionA.play();
  }

  /** 选中：播放 Hover 一次，然后回到 IdleA 循环 */
  select() {
    if (this.selected) return;
    this.selected = true;
    if (this.actionHover) {
      this.actionHover.reset();
      this.actionHover.timeScale = 1;
      this.actionHover.play();
    }
    if (this.actionA) {
      this.actionA.reset().play();
      this.actionA.setEffectiveWeight(1);
    }
    if (this.actionB) this.actionB.setEffectiveWeight(0);
    this.idleKey = 'A';
    this.idleElapsed = 0;
    this.setLight(0xfff4d6, 240);
  }

  /** 取消选中：Hover 反向播放降回台面 */
  deselect() {
    if (!this.selected) return;
    this.selected = false;
    if (this.actionHover) {
      this.actionHover.paused = false;
      this.actionHover.timeScale = -1;
      this.actionHover.play();
    }
    this.setLight(this.accentColor, 55);
  }

  setLight(color, intensity) {
    if (!this.spotlight) return;
    this.spotlight.color.setHex(color);
    this.spotlight.intensity = intensity;
  }

  /** @returns {boolean} 是否仍在动，用于脏检查渲染 */
  update(dt) {
    this.mixer.update(dt);
    if (this.selected) return true;

    this.idleElapsed += dt;
    const limit = this.idleKey === 'A' ? IDLE_A_SECONDS : IDLE_B_SECONDS;
    if (this.idleElapsed >= limit) {
      this.idleElapsed = 0;
      this.idleKey = this.idleKey === 'A' ? 'B' : 'A';
      this.crossFade();
    }
    return true;
  }

  crossFade() {
    const from = this.idleKey === 'A' ? this.actionB : this.actionA;
    const to = this.idleKey === 'A' ? this.actionA : this.actionB;
    if (!to) return;
    to.reset().play();
    if (from) from.crossFadeTo(to, FADE, false);
  }
}

function idleName(id, key) {
  return `${cap(id)}_Idle_${key}`;
}

function hoverName(id) {
  return `${cap(id)}_Hover`;
}

function cap(s) {
  return s.charAt(0).toUpperCase() + s.slice(1);
}
