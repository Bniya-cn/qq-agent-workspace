    let currentWorkspace = 'bot';
    const _uiPendingLock = {};
    let _lastConfirmedDashboard = {};
    let _groupChatFormDirty = false;
    let _groupChatSaving = false;
    let _stickerFormDirty = false;
    let _stickerSaving = false;

    // 聊天、日志和模型输出只作为文本显示，避免其中的 HTML/CSS 改写控制台。
    function escapePanelText(value) {
      return String(value ?? '').replace(/[&<>"']/g, char => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      })[char]);
    }

    function lockUi(key, ms = 3000) {
      _uiPendingLock[key] = Date.now() + ms;
    }
    function isUiLocked(key) {
      return !!(_uiPendingLock[key] && Date.now() < _uiPendingLock[key]);
    }

    function showToast(msg) {
      const box = document.getElementById('toast-box');
      if (!box) return;
      box.innerText = msg;
      box.classList.remove('hidden');
      clearTimeout(window._toastTimer);
      window._toastTimer = setTimeout(() => box.classList.add('hidden'), 3200);
    }

    function switchWorkspace(mode) {
      currentWorkspace = mode;
      document.body.setAttribute('data-workspace', mode);
      const botSec = document.getElementById('workspace-bot');
      const voiceSec = document.getElementById('workspace-voice');
      const btnBot = document.getElementById('tab-btn-bot');
      const btnVoice = document.getElementById('tab-btn-voice');

      if (mode === 'bot') {
        botSec.classList.remove('hidden');
        voiceSec.classList.add('hidden');
        btnBot.className = 'tactile-btn px-3 sm:px-4 py-1.5 rounded-lg text-xs font-semibold flex items-center gap-1.5 bg-sky-500 text-slate-950 shadow-sm whitespace-nowrap';
        btnVoice.className = 'tactile-btn px-3 sm:px-4 py-1.5 rounded-lg text-xs font-semibold flex items-center gap-1.5 text-slate-400 hover:text-slate-200 whitespace-nowrap';
      } else {
        botSec.classList.add('hidden');
        voiceSec.classList.remove('hidden');
        btnVoice.className = 'tactile-btn px-3 sm:px-4 py-1.5 rounded-lg text-xs font-semibold flex items-center gap-1.5 bg-emerald-500 text-slate-950 shadow-sm whitespace-nowrap';
        btnBot.className = 'tactile-btn px-3 sm:px-4 py-1.5 rounded-lg text-xs font-semibold flex items-center gap-1.5 text-slate-400 hover:text-slate-200 whitespace-nowrap';
      }
    }

    /**
     * 写操作统一使用 POST 请求并同时校验 HTTP 状态码与业务返回码（code/status）。
     * 绝不在 POST 失败后回退到会修改状态的 GET 请求。
     */
    async function apiPost(path, payload, timeoutMs = 10000) {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), timeoutMs);
      let resp;
      try {
        resp = await fetch(path, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload || {}),
          cache: 'no-store',
          signal: ctrl.signal
        });
      } catch (netErr) {
        const err = new Error(netErr && netErr.name === 'AbortError' ? '请求超时，回执未确认' : ('网络请求异常: ' + (netErr.message || netErr)));
        err.isUncertainReceipt = true;
        throw err;
      } finally {
        clearTimeout(timer);
      }

      let data = null;
      try {
        data = await resp.json();
      } catch (_) {
        const err = new Error(`服务端响应格式异常 (HTTP ${resp.status})`);
        err.isUncertainReceipt = resp.status >= 500;
        err.isDeterministicError = resp.status < 500;
        throw err;
      }

      const bizOk = data && (data.code === 0 || data.status === 'ok') && data.status !== 'error' && data.code !== 1 && data.code !== 400;
      if (!resp.ok || !bizOk) {
        const msg = (data && (data.message || data.detail || data.error)) || `请求失败 (HTTP ${resp.status})`;
        const err = new Error(msg);
        err.isDeterministicError = true;
        err.response = data;
        throw err;
      }
      return data;
    }

    /**
     * 安全执行开关/配置变更：
     * - 严格只发 POST，绝不改用变更型 GET 重试；
     * - 若发生超时或回执丢失（isUncertainReceipt），通过只读接口 GET /api/status 核实当前状态并返回 { uncertain: true, statusData }；
     * - 若明确业务失败，抛出异常由调用方恢复服务端已确认状态。
     */
    async function sendSwitchSafe(path, payload) {
      try {
        return await apiPost(path, payload, 9000);
      } catch (err) {
        if (err && err.isUncertainReceipt) {
          let verified = null;
          try {
            const roResp = await fetch('/api/status', { cache: 'no-store' });
            if (roResp.ok) {
              verified = await roResp.json();
            }
          } catch (_) {}
          return {
            code: -1,
            status: 'uncertain',
            uncertain: true,
            message: '结果待确认（回执超时，已通过只读状态核实，未重复提交）',
            verifiedStatus: verified
          };
        }
        throw err;
      }
    }

    function markGroupChatFormDirty() {
      _groupChatFormDirty = true;
      const st = document.getElementById('group-cfg-status-msg');
      if (st) {
        st.innerText = '● 有未保存的修改（轮询不会覆盖您正在编辑的值）';
        st.className = 'font-mono text-amber-400 break-words';
      }
    }

    function syncGroupChatFormFromData(d, force = false) {
      if (!d) return;
      const card = document.getElementById('group-chat-quota-card');
      const hasFocusInside = card && document.activeElement && card.contains(document.activeElement);
      if (!force && (_groupChatFormDirty || _groupChatSaving || hasFocusInside)) {
        return;
      }
      const bufEl = document.getElementById('group-cfg-buffer-enabled');
      const winEl = document.getElementById('group-cfg-merge-window');
      const maxMsgEl = document.getElementById('group-cfg-max-messages');
      const maxCharEl = document.getElementById('group-cfg-max-chars');
      const minIntEl = document.getElementById('group-cfg-min-interval');
      const maxPmEl = document.getElementById('group-cfg-max-per-minute');
      const paceEl = document.getElementById('group-cfg-chunk-pace');

      if (bufEl && d.group_buffer_enabled !== undefined) bufEl.checked = !!d.group_buffer_enabled;
      if (winEl && d.group_merge_window_sec !== undefined) winEl.value = Number(d.group_merge_window_sec).toFixed(1);
      if (maxMsgEl && d.group_merge_max_messages !== undefined) maxMsgEl.value = Number(d.group_merge_max_messages);
      if (maxCharEl && d.group_merge_max_chars !== undefined) maxCharEl.value = Number(d.group_merge_max_chars);
      if (minIntEl && d.group_quota_min_interval_sec !== undefined) minIntEl.value = Number(d.group_quota_min_interval_sec).toFixed(1);
      if (maxPmEl && d.group_quota_max_per_minute !== undefined) maxPmEl.value = Number(d.group_quota_max_per_minute);
      if (paceEl && d.group_chunk_pace_sec !== undefined) paceEl.value = Number(d.group_chunk_pace_sec).toFixed(1);
    }

    function resetGroupChatConfigForm() {
      _groupChatFormDirty = false;
      syncGroupChatFormFromData(_lastConfirmedDashboard, true);
      const st = document.getElementById('group-cfg-status-msg');
      if (st) {
        st.innerText = '已恢复为服务端当前生效的全局默认配置';
        st.className = 'font-mono text-slate-400 break-words';
      }
    }

    async function saveGroupChatConfig(btnEl) {
      if (_groupChatSaving) return;
      _groupChatSaving = true;
      const btn = btnEl || document.getElementById('btn-save-group-cfg');
      const st = document.getElementById('group-cfg-status-msg');
      if (btn) btn.disabled = true;
      if (st) {
        st.innerText = '正在保存全局群聊合并与配额配置...';
        st.className = 'font-mono text-sky-300 break-words';
      }

      const payload = {
        group_buffer_enabled: !!document.getElementById('group-cfg-buffer-enabled')?.checked,
        group_merge_window_sec: parseFloat(document.getElementById('group-cfg-merge-window')?.value || '1.5'),
        group_merge_max_messages: parseInt(document.getElementById('group-cfg-max-messages')?.value || '5', 10),
        group_merge_max_chars: parseInt(document.getElementById('group-cfg-max-chars')?.value || '2000', 10),
        group_quota_min_interval_sec: parseFloat(document.getElementById('group-cfg-min-interval')?.value || '8.0'),
        group_quota_max_per_minute: parseInt(document.getElementById('group-cfg-max-per-minute')?.value || '4', 10),
        group_chunk_pace_sec: parseFloat(document.getElementById('group-cfg-chunk-pace')?.value || '1.2'),
      };

      try {
        const res = await sendSwitchSafe('/api/group_chat/config', payload);
        if (res && res.uncertain) {
          if (st) {
            st.innerText = '⚠️ 结果待确认：请求回执超时，正在通过只读接口核实，未自动重复提交';
            st.className = 'font-mono text-amber-400 break-words';
          }
          showToast('⚠️ 群节奏配置结果待确认，请核对回显状态');
          try {
            const ro = await fetch('/api/group_chat/config', { cache: 'no-store' });
            if (ro.ok) {
              const roData = await ro.json();
              Object.assign(_lastConfirmedDashboard, roData);
              _groupChatFormDirty = false;
              syncGroupChatFormFromData(roData, true);
            }
          } catch (_) {}
          return;
        }

        _groupChatFormDirty = false;
        Object.assign(_lastConfirmedDashboard, res);
        syncGroupChatFormFromData(res, true);
        if (st) {
          st.innerText = `✅ 已保存全局默认配置（合并=${res.group_buffer_enabled ? '开' : '关'} · 窗口=${res.group_merge_window_sec}s · 间隔=${res.group_quota_min_interval_sec}s · 上限=${res.group_quota_max_per_minute}/min · 分段=${res.group_chunk_pace_sec}s）`;
          st.className = 'font-mono text-emerald-400 break-words';
        }
        showToast('✅ 已保存全局群聊合并与配额配置');
      } catch (err) {
        if (st) {
          st.innerText = '❌ 保存失败：' + (err.message || err);
          st.className = 'font-mono text-rose-400 break-words';
        }
        showToast('❌ 保存群节奏配置失败：' + (err.message || err));
      } finally {
        _groupChatSaving = false;
        if (btn) btn.disabled = false;
      }
    }

    function markStickerFormDirty() {
      _stickerFormDirty = true;
      const st = document.getElementById('amiya-sticker-status');
      if (st) {
        st.innerText = '● 有未保存的表情包设置';
        st.className = 'font-mono text-amber-400 break-words';
      }
    }

    function setStickerDelayPresetCustom() {
      const preset = document.getElementById('amiya-sticker-delay-preset');
      if (preset) preset.value = 'custom';
    }

    function applyStickerDelayPreset() {
      const preset = document.getElementById('amiya-sticker-delay-preset')?.value || 'custom';
      const bounds = { '1-5': [1, 5], '1-10': [1, 10], '1-20': [1, 20], '5-20': [5, 20] }[preset];
      if (!bounds) return;
      const minEl = document.getElementById('amiya-sticker-min-delay');
      const maxEl = document.getElementById('amiya-sticker-max-delay');
      if (minEl) minEl.value = bounds[0];
      if (maxEl) maxEl.value = bounds[1];
      markStickerFormDirty();
    }

    function syncStickerFormFromData(data, force = false) {
      if (!data) return;
      const card = document.getElementById('amiya-sticker-config-card');
      const hasFocusInside = card && document.activeElement && card.contains(document.activeElement);
      if (!force && (_stickerFormDirty || _stickerSaving || hasFocusInside)) return;
      const enabled = document.getElementById('amiya-sticker-enabled');
      const minEl = document.getElementById('amiya-sticker-min-delay');
      const maxEl = document.getElementById('amiya-sticker-max-delay');
      const preset = document.getElementById('amiya-sticker-delay-preset');
      if (enabled && data.amiya_sticker_enabled !== undefined) enabled.checked = !!data.amiya_sticker_enabled;
      const minDelay = Number(data.amiya_sticker_min_delay_sec ?? 1);
      const maxDelay = Number(data.amiya_sticker_max_delay_sec ?? 20);
      if (minEl) minEl.value = minDelay.toFixed(1);
      if (maxEl) maxEl.value = maxDelay.toFixed(1);
      if (preset) {
        const key = `${Number(minDelay.toFixed(1))}-${Number(maxDelay.toFixed(1))}`;
        preset.value = ['1-5', '1-10', '1-20', '5-20'].includes(key) ? key : 'custom';
      }
    }

    function resetStickerConfigForm() {
      _stickerFormDirty = false;
      syncStickerFormFromData(_lastConfirmedDashboard, true);
      const st = document.getElementById('amiya-sticker-status');
      if (st) {
        st.innerText = '已恢复为服务端当前设置';
        st.className = 'font-mono text-slate-400 break-words';
      }
    }

    async function saveStickerConfig(btnEl) {
      if (_stickerSaving) return;
      const enabled = !!document.getElementById('amiya-sticker-enabled')?.checked;
      const minDelay = Number(document.getElementById('amiya-sticker-min-delay')?.value);
      const maxDelay = Number(document.getElementById('amiya-sticker-max-delay')?.value);
      const st = document.getElementById('amiya-sticker-status');
      const btn = btnEl || document.getElementById('btn-save-sticker-config');
      if (!Number.isFinite(minDelay) || !Number.isFinite(maxDelay) || minDelay < 1 || maxDelay > 20 || minDelay > maxDelay) {
        if (st) {
          st.innerText = '❌ 延迟范围无效：需满足 1 ≤ 最短 ≤ 最长 ≤ 20 秒';
          st.className = 'font-mono text-rose-400 break-words';
        }
        return;
      }
      _stickerSaving = true;
      if (btn) btn.disabled = true;
      if (st) {
        st.innerText = '正在保存表情包设置...';
        st.className = 'font-mono text-fuchsia-300 break-words';
      }
      const payload = {
        amiya_sticker_enabled: enabled,
        amiya_sticker_min_delay_sec: minDelay,
        amiya_sticker_max_delay_sec: maxDelay,
      };
      try {
        const res = await sendSwitchSafe('/api/sticker/config', payload);
        if (res && res.uncertain) {
          if (st) {
            st.innerText = '⚠️ 保存回执待确认，正在读取服务端状态；不会重复提交';
            st.className = 'font-mono text-amber-400 break-words';
          }
          try {
            const ro = await fetch('/api/sticker/config', { cache: 'no-store' });
            if (ro.ok) {
              const roData = await ro.json();
              Object.assign(_lastConfirmedDashboard, roData);
              _stickerFormDirty = false;
              syncStickerFormFromData(roData, true);
            }
          } catch (_) {}
          showToast('⚠️ 表情包设置回执待确认，已读取当前状态');
          return;
        }
        _stickerFormDirty = false;
        Object.assign(_lastConfirmedDashboard, res);
        syncStickerFormFromData(res, true);
        if (st) {
          st.innerText = `✅ 已保存：${res.amiya_sticker_enabled ? '已启用' : '已关闭'} · 延迟 ${res.amiya_sticker_min_delay_sec}–${res.amiya_sticker_max_delay_sec} 秒`;
          st.className = 'font-mono text-emerald-400 break-words';
        }
        showToast('✅ 已保存阿米娅表情包设置');
      } catch (err) {
        _stickerFormDirty = false;
        syncStickerFormFromData(_lastConfirmedDashboard, true);
        if (st) {
          st.innerText = '❌ 保存失败：' + (err.message || err);
          st.className = 'font-mono text-rose-400 break-words';
        }
        showToast('❌ 保存表情包设置失败：' + (err.message || err));
      } finally {
        _stickerSaving = false;
        if (btn) btn.disabled = false;
      }
    }

    async function refreshDashboard() {
      try {
        const resp = await fetch('/api/dashboard', { cache: 'no-store' });
        if (!resp.ok) return;
        const d = await resp.json();
        _lastConfirmedDashboard = d;

        // 顶部状态
        const badge = document.getElementById('header-conn-badge');
        if (badge) {
          if (d.bot_connected) {
            badge.className = 'inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-mono bg-emerald-500/10 text-emerald-400 border border-emerald-500/30 whitespace-nowrap';
            badge.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"></span>全链路在线 (' + (d.connected_bots?.[0] || 'Bot') + ')';
          } else {
            badge.className = 'inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-mono bg-amber-500/10 text-amber-400 border border-amber-500/30 whitespace-nowrap';
            badge.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-amber-400"></span>8080在线 · 等待NapCat接入';
          }
        }

        // 机器人模式状态
        const gToggle = document.getElementById('bot-global-toggle');
        if (gToggle && !isUiLocked('bot-global-toggle') && !gToggle.disabled) {
          gToggle.checked = !!d.global_active;
        }
        const charBadge = document.getElementById('active-char-badge');
        if (charBadge) charBadge.innerText = d.active_character || 'none';
        document.querySelectorAll('.char-btn').forEach(btn => {
          const isCur = btn.getAttribute('data-char') === (d.active_character || 'none');
          btn.className = isCur
            ? 'char-btn tactile-btn px-3 py-2 rounded-xl border border-sky-500 bg-sky-500/15 text-left'
            : 'char-btn tactile-btn px-3 py-2 rounded-xl border border-surface-700 bg-surface-850 hover:border-surface-600 text-left';
        });

        // 同步群聊合并与配额卡片（若用户正在编辑且未提交则不覆盖）
        syncGroupChatFormFromData(d, false);
        syncStickerFormFromData(d, false);

        // 已归档立绘
        const refs = d.saved_refs || [];
        const count = d.saved_refs_count !== undefined ? d.saved_refs_count : refs.length;
        const refCntEl = document.getElementById('info-refs-count');
        if (refCntEl) refCntEl.innerText = count + ' 张';

        // 群白名单
        const groups = d.active_groups || [];
        const gCntBadge = document.getElementById('group-count-badge');
        if (gCntBadge) gCntBadge.innerText = groups.length + ' 个白名单群';
        const gListEl = document.getElementById('active-groups-list');
        if (gListEl) {
          gListEl.innerHTML = groups.length
            ? groups.map(g => `<span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-surface-850 border border-surface-700 font-mono text-xs text-slate-200">${g}<button onclick="modifyGroup('del','${g}', this)" class="text-slate-400 hover:text-red-400 ml-1">×</button></span>`).join('')
            : '<span class="text-xs text-slate-500">暂无白名单群</span>';
        }

        // 安慰模式目标
        const comforts = d.comfort_targets || [];
        const cListEl = document.getElementById('comfort-targets-list');
        if (cListEl) {
          cListEl.innerHTML = comforts.length
            ? comforts.map(c => `<span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-sky-500/10 border border-sky-500/30 font-mono text-xs text-sky-300">💙 ${c}<button onclick="modifyComfort(false,'${c}', this)" class="text-sky-400 hover:text-red-400 ml-1">×</button></span>`).join('')
            : '<span class="text-xs text-slate-500">当前未绑定定向安慰对象</span>';
        }

        // 已提取画像的群
        const gPersonas = d.group_personas || [];
        const gPills = document.getElementById('extracted-groups-pills');
        if (gPills) gPills.innerText = gPersonas.length ? `已有画像: ${gPersonas.join(', ')}` : '';

        // AI 最新对话
        const dialogues = (d.recent_dialogues || []).slice().reverse();
        const dCntEl = document.getElementById('dialogue-count-label');
        if (dCntEl) dCntEl.innerText = dialogues.length + ' 条记录';
        const dBoxEl = document.getElementById('dialogue-list-box');
        if (dBoxEl) {
          dBoxEl.innerHTML = dialogues.length
            ? dialogues.map(item => `
              <div class="dialogue-item p-3 rounded-xl bg-surface-950 border border-surface-800 space-y-1">
                <div class="flex flex-wrap items-center justify-between gap-1 text-[11px] font-mono text-slate-400">
                  <span>[${escapePanelText(item.source)}] 用户 ${escapePanelText(item.sender_id)}</span>
                  <span>${escapePanelText(item.time)} ${item.voice ? '🎙️' : ''}</span>
                </div>
                <div class="msg-bubble whitespace-pre-wrap text-xs text-slate-300">💬 ${escapePanelText(item.user_text)}</div>
                <div class="msg-bubble whitespace-pre-wrap text-xs text-sky-300 pt-0.5">🤖 [${escapePanelText(item.character)}] ${escapePanelText(item.reply)}</div>
              </div>
            `).join('')
            : '<div class="text-xs text-slate-500 py-8 text-center">暂无对话记录，在白名单群内 @机器人 即可触发实时展示</div>';
        }

        // 实时日志
        const logs = (d.recent_logs || []).slice().reverse();
        const lCntEl = document.getElementById('log-count-label');
        if (lCntEl) lCntEl.innerText = logs.length + ' 条事件';
        const lBoxEl = document.getElementById('log-list-box');
        if (lBoxEl) {
          lBoxEl.innerHTML = logs.length
            ? logs.map(l => {
                const col = l.level === 'ERROR' ? 'text-red-400' : (l.level === 'WARN' ? 'text-amber-400' : 'text-slate-300');
                return `<div class="${col} break-words">[${escapePanelText(l.time)}] ${escapePanelText(l.text)}</div>`;
              }).join('')
            : '<div class="text-slate-500">等待事件进入...</div>';
        }

        // 语音模式状态同步
        if (!isUiLocked('voice-config')) {
          const vmToggle = document.getElementById('voice-main-toggle');
          const voToggle = document.getElementById('voice-only-toggle');
          const veToggle = document.getElementById('voice-emotion-director-toggle');
          if (vmToggle && !vmToggle.disabled) vmToggle.checked = !!d.voice_enabled;
          if (voToggle && !voToggle.disabled) voToggle.checked = !!d.voice_only_reply;
          if (veToggle && !veToggle.disabled) veToggle.checked = !!d.voice_emotion_director_enabled;
        }
        const vDot = document.getElementById('voice-tab-dot');
        if (vDot) {
          vDot.className = d.voice_enabled
            ? 'w-2 h-2 rounded-full bg-emerald-400 shrink-0'
            : 'w-2 h-2 rounded-full bg-slate-600 shrink-0';
        }

        // 9880 / 9876 在线状态
        const ok9880 = !!d.tts_9880_online;
        const ok9876 = !!d.studio_9876_online;
        const dot9880 = document.getElementById('dot-9880');
        const txt9880 = document.getElementById('text-9880');
        const dot9876 = document.getElementById('dot-9876');
        const txt9876 = document.getElementById('text-9876');
        if (dot9880) dot9880.className = ok9880 ? 'w-2 h-2 rounded-full bg-emerald-400 shrink-0' : 'w-2 h-2 rounded-full bg-red-500 shrink-0';
        if (txt9880) txt9880.innerText = ok9880 ? '运行正常 · 实时合成就绪' : '未运行 · 点击右侧一键拉起';
        if (dot9876) dot9876.className = ok9876 ? 'w-2 h-2 rounded-full bg-emerald-400 shrink-0' : 'w-2 h-2 rounded-full bg-slate-500 shrink-0';
        if (txt9876) txt9876.innerText = ok9876 ? '运行正常 · 支持多参考融合调音' : '未启动（可选专业调音台）';

        // 语音参考下拉框与参数
        const modelSelect = document.getElementById('voice-model-select');
        if (modelSelect) {
          const modelOptions = [{id: 'auto', name: '跟随当前角色'}, ...(d.voice_models || []).filter(m => m.ready)];
          const modelKey = JSON.stringify(modelOptions);
          if (modelSelect.dataset.options !== modelKey) {
            modelSelect.replaceChildren(...modelOptions.map(m => new Option(m.name, m.id)));
            modelSelect.dataset.options = modelKey;
          }
          if (document.activeElement !== modelSelect) modelSelect.value = d.voice_model || 'auto';
        }
        const vRefs = d.voice_refs || [];
        const sel = document.getElementById('voice-ref-select');
        if (sel) {
          const refKey = JSON.stringify(vRefs);
          if (sel.dataset.options !== refKey) {
            sel.replaceChildren(new Option('自动选择参考音频', ''), ...vRefs.map(r => new Option(`${r.label} — ${r.text.slice(0, 22)}...`, r.id)));
            sel.dataset.options = refKey;
          }
        }

        const radios = document.getElementsByName('voice_mode_radio');
        radios.forEach(r => { r.checked = (r.value === (d.voice_mode || 'emotion_auto')); });

        if (!document.activeElement || document.activeElement.tagName !== 'INPUT') {
          if (d.voice_speed) {
            const sSpd = document.getElementById('slider-voice-speed');
            const vSpd = document.getElementById('val-voice-speed');
            if (sSpd) sSpd.value = d.voice_speed;
            if (vSpd) vSpd.innerText = parseFloat(d.voice_speed).toFixed(2) + 'x';
          }
          if (d.voice_temperature) {
            const sTmp = document.getElementById('slider-voice-temp');
            const vTmp = document.getElementById('val-voice-temp');
            if (sTmp) sTmp.value = d.voice_temperature;
            if (vTmp) vTmp.innerText = parseFloat(d.voice_temperature).toFixed(2);
          }
          if (d.voice_top_p) {
            const sTp = document.getElementById('slider-voice-topp');
            const vTp = document.getElementById('val-voice-topp');
            if (sTp) sTp.value = d.voice_top_p;
            if (vTp) vTp.innerText = parseFloat(d.voice_top_p).toFixed(2);
          }
        }

        // 闲聊模式与风格状态同步
        const stToggle = document.getElementById('small-talk-toggle');
        if (stToggle && !isUiLocked('small-talk-toggle') && !stToggle.disabled) {
          stToggle.checked = !!d.small_talk_mode;
          const currentStyle = d.small_talk_style || 'pure_persona';
          const stBadge = document.getElementById('small-talk-status-badge');
          if (stBadge) {
            if (d.small_talk_mode) {
              stBadge.innerText = currentStyle === 'pure_persona' ? 'ON · 纯角色闲聊(5-20字)' : 'ON · 群友镜像(角色优先)';
              stBadge.className = 'px-2 py-0.5 rounded text-[10px] font-mono bg-emerald-500/20 text-emerald-300 border border-emerald-500/30';
            } else {
              stBadge.innerText = 'OFF · 原版干员模式';
              stBadge.className = 'px-2 py-0.5 rounded text-[10px] font-mono bg-slate-800 text-slate-400';
            }
          }
        }

        const btnPure = document.getElementById('btn-style-pure');
        const btnGroup = document.getElementById('btn-style-group');
        const styleDesc = document.getElementById('small-talk-style-desc');
        if (btnPure && btnGroup && !isUiLocked('small-talk-style')) {
          const currentStyle = d.small_talk_style || 'pure_persona';
          if (currentStyle === 'pure_persona') {
            btnPure.className = 'cursor-pointer select-none px-2.5 py-1 rounded text-[11px] font-semibold transition-all bg-sky-500/25 text-sky-300 border border-sky-500/50 shadow-sm whitespace-nowrap';
            btnGroup.className = 'cursor-pointer select-none px-2.5 py-1 rounded text-[11px] font-medium transition-all text-slate-400 hover:text-slate-200 border border-transparent whitespace-nowrap';
            if (styleDesc) styleDesc.innerText = '🌸 纯角色设定 (5~20字，专属口吻)';
          } else {
            btnGroup.className = 'cursor-pointer select-none px-2.5 py-1 rounded text-[11px] font-semibold transition-all bg-amber-500/25 text-amber-300 border border-amber-500/50 shadow-sm whitespace-nowrap';
            btnPure.className = 'cursor-pointer select-none px-2.5 py-1 rounded text-[11px] font-medium transition-all text-slate-400 hover:text-slate-200 border border-transparent whitespace-nowrap';
            if (styleDesc) styleDesc.innerText = '🐟 群友镜像模式 (借用群表达，固定人格优先)';
          }
        }

        // 默认回复模式（免触发词）状态同步
        const arToggle = document.getElementById('auto-reply-toggle');
        if (arToggle && !isUiLocked('auto-reply-toggle') && !arToggle.disabled) {
          arToggle.checked = d.auto_reply_mode !== false;
          const arBadge = document.getElementById('auto-reply-status-badge');
          if (arBadge) {
            arBadge.innerText = d.auto_reply_mode !== false ? 'ON · 免触发词' : 'OFF · 需@唤醒';
            arBadge.className = d.auto_reply_mode !== false ? 'px-2 py-0.5 rounded text-[10px] font-mono bg-emerald-500/20 text-emerald-300 border border-emerald-500/30' : 'px-2 py-0.5 rounded text-[10px] font-mono bg-slate-800 text-slate-400';
          }
        }

        // 群训练状态同步
        const trToggle = document.getElementById('training-master-toggle');
        if (trToggle && !isUiLocked('training-master-toggle') && !trToggle.disabled) {
          trToggle.checked = !!d.training_mode;
        }
        renderTrainingHub(d.training_status || {}, d.training_groups || d.active_groups || []);
        if (typeof renderAutonomousStatus === 'function') {
          renderAutonomousStatus(d.autonomous_status || {});
        }
        if (typeof renderProfilesSummaryList === 'function' && Array.isArray(d.profiles_summary)) {
          renderProfilesSummaryList(d.profiles_summary);
        }

        // 默认测试 QQ 标识
        const sqqBadge = document.getElementById('superuser-qq-badge');
        if (sqqBadge && d.superuser_qq) {
          sqqBadge.innerText = '默认QQ: ' + d.superuser_qq;
        }

        // 群成员白名单总开关与选项同步
        const wlToggle = document.getElementById('group-user-wl-master-toggle');
        if (wlToggle && !isUiLocked('group-user-wl-toggle') && !wlToggle.disabled) {
          wlToggle.checked = !!d.group_user_whitelist_enabled;
        }
        const wlStatusLabel = document.getElementById('group-user-wl-status-label');
        if (wlStatusLabel) {
          wlStatusLabel.innerText = d.group_user_whitelist_enabled ? '白名单过滤: 已启用' : '白名单过滤: 禁用';
          wlStatusLabel.className = d.group_user_whitelist_enabled
            ? 'text-xs font-mono text-emerald-400 font-semibold'
            : 'text-xs font-mono text-slate-400';
        }

        // 同步群白名单下拉列表选项
        const wlGroupSelect = document.getElementById('wl-group-select');
        if (wlGroupSelect) {
          const currentVal = wlGroupSelect.value;
          const opts = ['<option value="">-- 选择白名单群 --</option>'].concat(
            groups.map(g => `<option value="${g}">群 ${g}</option>`)
          );
          const newHtml = opts.join('');
          if (wlGroupSelect.innerHTML !== newHtml) {
            wlGroupSelect.innerHTML = newHtml;
            if (currentVal && groups.includes(currentVal)) {
              wlGroupSelect.value = currentVal;
            } else if (groups.length > 0 && !currentWlGroupId) {
              wlGroupSelect.value = groups[0];
              currentWlGroupId = groups[0];
              const customInp = document.getElementById('wl-custom-group-input');
              if (customInp && !customInp.value) customInp.value = groups[0];
            }
          }
        }
      } catch (e) {
        console.error(e);
      }
    }

    let currentWlGroupId = '';
    let currentGroupMembers = [];
    let selectedWlUserIds = new Set();

    async function toggleGroupUserWhitelist(checked) {
      lockUi('group-user-wl-toggle', 3500);
      const wlToggle = document.getElementById('group-user-wl-master-toggle');
      if (wlToggle) wlToggle.disabled = true;
      try {
        const res = await sendSwitchSafe('/api/switch/group_user_whitelist', { enable: checked });
        if (res && res.uncertain) {
          showToast('⚠️ 群成员白名单开关结果待确认，已核实当前状态');
          if (wlToggle && res.verifiedStatus) wlToggle.checked = !!res.verifiedStatus.group_user_whitelist_enabled;
          return;
        }
        const confirmed = res.enabled !== undefined ? !!res.enabled : !!checked;
        if (wlToggle) wlToggle.checked = confirmed;
        showToast(confirmed ? '🛡️ 群成员发言白名单过滤已开启' : '已关闭群成员白名单过滤');
        refreshDashboard();
      } catch (e) {
        if (wlToggle) wlToggle.checked = !!_lastConfirmedDashboard.group_user_whitelist_enabled;
        showToast('❌ 切换白名单开关失败: ' + (e.message || e));
      } finally {
        if (wlToggle) wlToggle.disabled = false;
      }
    }

    function onWhitelistGroupChange() {
      const sel = document.getElementById('wl-group-select');
      const val = (sel ? sel.value : '').trim();
      if (val) {
        currentWlGroupId = val;
        const customInp = document.getElementById('wl-custom-group-input');
        if (customInp) customInp.value = val;
        fetchGroupMembers();
      }
    }

    async function fetchGroupMembers() {
      const customGid = (document.getElementById('wl-custom-group-input').value || '').trim();
      const selGid = (document.getElementById('wl-group-select').value || '').trim();
      const gid = customGid || selGid || currentWlGroupId;
      if (!gid) {
        showToast('请先选择或输入群号');
        return;
      }
      currentWlGroupId = gid;
      const btn = document.getElementById('btn-fetch-members');
      const grid = document.getElementById('wl-members-grid');
      const stats = document.getElementById('wl-member-stats');
      if (btn) btn.disabled = true;
      if (stats) stats.innerText = `⏳ 正在读取群 ${gid} 成员列表...`;
      if (grid) grid.innerHTML = '<div class="col-span-full text-center py-10 text-xs text-sky-400">正在通过 OneBot API 检索群成员列表...</div>';

      try {
        const [memResp, wlResp] = await Promise.all([
          fetch(`/api/groups/members?group_id=${gid}`).then(r => r.json()),
          fetch(`/api/whitelist/users?group_id=${gid}`).then(r => r.json())
        ]);

        if (memResp.code !== 0) {
          if (grid) grid.innerHTML = `<div class="col-span-full text-center py-10 text-xs text-red-400">读取失败: ${memResp.message || '未知错误'}</div>`;
          if (stats) stats.innerText = `读取失败: ${memResp.message}`;
          return;
        }

        currentGroupMembers = memResp.members || [];
        const savedUsers = (wlResp.code === 0 && Array.isArray(wlResp.users)) ? wlResp.users : [];
        selectedWlUserIds = new Set(savedUsers.map(String));

        renderGroupMembers();
        showToast(`✅ 成功获取群 ${gid} 的 ${currentGroupMembers.length} 名成员`);
      } catch (e) {
        if (grid) grid.innerHTML = `<div class="col-span-full text-center py-10 text-xs text-red-400">网络异常: ${e}</div>`;
        if (stats) stats.innerText = `异常: ${e}`;
      } finally {
        if (btn) btn.disabled = false;
      }
    }

    function renderGroupMembers() {
      const grid = document.getElementById('wl-members-grid');
      const stats = document.getElementById('wl-member-stats');
      if (!grid) return;

      const filterKw = (document.getElementById('wl-member-search')?.value || '').trim().toLowerCase();
      const visibleMembers = currentGroupMembers.filter(m => {
        if (!filterKw) return true;
        const name = (m.display_name || '').toLowerCase();
        const nick = (m.nickname || '').toLowerCase();
        const card = (m.card || '').toLowerCase();
        const uid = String(m.user_id);
        return name.includes(filterKw) || nick.includes(filterKw) || card.includes(filterKw) || uid.includes(filterKw);
      });

      if (stats) {
        stats.innerText = `群 ${currentWlGroupId} · 共 ${currentGroupMembers.length} 人 · 当前已勾选 ${selectedWlUserIds.size} 人 (当前显示 ${visibleMembers.length} 人)`;
      }

      if (!visibleMembers.length) {
        grid.innerHTML = '<div class="col-span-full text-center py-8 text-xs text-slate-500">未找到匹配的群成员</div>';
        return;
      }

      grid.innerHTML = visibleMembers.map(m => {
        const uid = String(m.user_id);
        const isSelected = selectedWlUserIds.has(uid);
        const roleBadge = m.role === 'owner'
          ? '<span class="text-[9px] px-1 py-0.2 rounded bg-amber-500/20 text-amber-300 border border-amber-500/30">群主</span>'
          : (m.role === 'admin'
            ? '<span class="text-[9px] px-1 py-0.2 rounded bg-sky-500/20 text-sky-300 border border-sky-500/30">管理</span>'
            : '');

        const cardClass = isSelected
          ? 'cursor-pointer p-2 rounded-xl border border-sky-500 bg-sky-500/15 hover:bg-sky-500/20 transition-all flex items-center gap-2 relative shadow min-w-0'
          : 'cursor-pointer p-2 rounded-xl border border-surface-800 bg-surface-900/80 hover:border-surface-700 hover:bg-surface-850 transition-all flex items-center gap-2 relative min-w-0';

        const checkMark = isSelected
          ? '<div class="w-4 h-4 rounded-md bg-sky-500 text-slate-950 flex items-center justify-center font-bold text-[10px] shrink-0">✓</div>'
          : '<div class="w-4 h-4 rounded-md border border-surface-700 bg-surface-950 shrink-0"></div>';

        return `
          <div onclick="toggleMemberSelection('${uid}')" class="${cardClass}">
            <img src="https://q1.qlogo.cn/g?b=qq&nk=${uid}&s=100" class="w-7 h-7 rounded-full bg-surface-800 shrink-0" onerror="this.src='data:image/svg+xml;utf8,<svg xmlns=\\'http://www.w3.org/2000/svg\\' viewBox=\\'0 0 24 24\\' fill=\\'%2364748b\\'><circle cx=\\'12\\' cy=\\'8\\' r=\\'4\\'/><path d=\\'M4 20c0-4 4-6 8-6s8 2 8 6\\'/></svg>'" />
            <div class="flex-1 min-w-0 pr-1">
              <div class="flex items-center gap-1">
                <span class="text-xs font-semibold text-slate-200 truncate" title="${escapePanelText(m.display_name)}">${escapePanelText(m.display_name)}</span>
                ${roleBadge}
              </div>
              <div class="text-[10px] font-mono text-slate-400 truncate">${uid}</div>
            </div>
            ${checkMark}
          </div>
        `;
      }).join('');
    }

    function toggleMemberSelection(userId) {
      const uid = String(userId);
      if (selectedWlUserIds.has(uid)) {
        selectedWlUserIds.delete(uid);
      } else {
        selectedWlUserIds.add(uid);
      }
      renderGroupMembers();
    }

    function selectAllMembers(selectAll) {
      const filterKw = (document.getElementById('wl-member-search')?.value || '').trim().toLowerCase();
      const targetList = currentGroupMembers.filter(m => {
        if (!filterKw) return true;
        const name = (m.display_name || '').toLowerCase();
        const nick = (m.nickname || '').toLowerCase();
        const card = (m.card || '').toLowerCase();
        const uid = String(m.user_id);
        return name.includes(filterKw) || nick.includes(filterKw) || card.includes(filterKw) || uid.includes(filterKw);
      });

      targetList.forEach(m => {
        const uid = String(m.user_id);
        if (selectAll) {
          selectedWlUserIds.add(uid);
        } else {
          selectedWlUserIds.delete(uid);
        }
      });
      renderGroupMembers();
    }

    function invertMemberSelection() {
      const filterKw = (document.getElementById('wl-member-search')?.value || '').trim().toLowerCase();
      const targetList = currentGroupMembers.filter(m => {
        if (!filterKw) return true;
        const name = (m.display_name || '').toLowerCase();
        const nick = (m.nickname || '').toLowerCase();
        const card = (m.card || '').toLowerCase();
        const uid = String(m.user_id);
        return name.includes(filterKw) || nick.includes(filterKw) || card.includes(filterKw) || uid.includes(filterKw);
      });

      targetList.forEach(m => {
        const uid = String(m.user_id);
        if (selectedWlUserIds.has(uid)) {
          selectedWlUserIds.delete(uid);
        } else {
          selectedWlUserIds.add(uid);
        }
      });
      renderGroupMembers();
    }

    function filterGroupMembers() {
      renderGroupMembers();
    }

    async function saveGroupUserWhitelist() {
      const gid = currentWlGroupId || (document.getElementById('wl-custom-group-input').value || '').trim();
      if (!gid) {
        showToast('请先选择或输入群号');
        return;
      }
      const users = Array.from(selectedWlUserIds);
      const btn = document.getElementById('btn-save-user-wl');
      const statusEl = document.getElementById('wl-save-status-msg');
      if (btn) btn.disabled = true;
      if (statusEl) statusEl.innerText = '正在保存白名单...';

      try {
        const res = await sendSwitchSafe('/api/whitelist/users', { group_id: gid, users: users });
        if (res && res.uncertain) {
          showToast('⚠️ 白名单保存结果待确认，请刷新核实');
          if (statusEl) statusEl.innerText = '⚠️ 结果待确认，未自动重发';
          return;
        }
        showToast(`💾 群 ${gid} 白名单已保存！(共 ${users.length} 人)`);
        if (statusEl) statusEl.innerText = `✅ 已于 ${new Date().toLocaleTimeString()} 保存 ${users.length} 个白名单QQ`;
        setTimeout(() => { if (statusEl) statusEl.innerText = ''; }, 4000);
        refreshDashboard();
      } catch (e) {
        showToast('❌ 保存失败: ' + (e.message || e));
        if (statusEl) statusEl.innerText = '❌ 保存失败: ' + (e.message || e);
      } finally {
        if (btn) btn.disabled = false;
      }
    }

    async function toggleSmallTalk(enabled) {
      lockUi('small-talk-toggle', 3500);
      const stToggle = document.getElementById('small-talk-toggle');
      if (stToggle) stToggle.disabled = true;
      try {
        const res = await sendSwitchSafe('/api/switch/small_talk', { enable: enabled });
        if (res && res.uncertain) {
          showToast('⚠️ 闲聊模式切换结果待确认，已核实当前状态');
          if (stToggle && res.verifiedStatus) stToggle.checked = !!res.verifiedStatus.small_talk_mode;
          return;
        }
        const confirmed = res.small_talk_mode !== undefined ? !!res.small_talk_mode : !!enabled;
        if (stToggle) stToggle.checked = confirmed;
        showToast(confirmed ? '💬 日常闲聊模式已开启' : '🤖 已恢复原版干员模式');
      } catch (e) {
        if (stToggle) stToggle.checked = !!_lastConfirmedDashboard.small_talk_mode;
        showToast('❌ 切换失败: ' + (e.message || e));
      } finally {
        if (stToggle) stToggle.disabled = false;
        setTimeout(refreshDashboard, 600);
      }
    }

    async function setSmallTalkStyle(style) {
      lockUi('small-talk-style', 3500);
      const btnPure = document.getElementById('btn-style-pure');
      const btnGroup = document.getElementById('btn-style-group');
      if (btnPure) btnPure.disabled = true;
      if (btnGroup) btnGroup.disabled = true;
      try {
        const res = await sendSwitchSafe('/api/switch/small_talk_style', { style: style });
        if (res && res.uncertain) {
          showToast('⚠️ 闲聊风格切换结果待确认，正在核实状态');
          return;
        }
        const confirmedStyle = res.small_talk_style || style;
        _lastConfirmedDashboard.small_talk_style = confirmedStyle;
        showToast(confirmedStyle === 'pure_persona' ? '🌸 已切换为纯角色设定闲聊' : '🐟 已切换为群友镜像模式（借用表达，阿米娅人格优先）');
      } catch (e) {
        showToast('❌ 切换闲聊风格失败: ' + (e.message || e));
      } finally {
        if (btnPure) btnPure.disabled = false;
        if (btnGroup) btnGroup.disabled = false;
        setTimeout(refreshDashboard, 400);
      }
    }

    async function toggleAutoReply(enabled) {
      lockUi('auto-reply-toggle', 3500);
      const arToggle = document.getElementById('auto-reply-toggle');
      if (arToggle) arToggle.disabled = true;
      try {
        const res = await sendSwitchSafe('/api/switch/auto_reply', { enable: enabled });
        if (res && res.uncertain) {
          showToast('⚠️ 默认回复模式结果待确认，已核实当前状态');
          if (arToggle && res.verifiedStatus) arToggle.checked = res.verifiedStatus.auto_reply_mode !== false;
          return;
        }
        const confirmed = res.auto_reply_mode !== undefined ? !!res.auto_reply_mode : !!enabled;
        if (arToggle) arToggle.checked = confirmed;
        showToast(confirmed ? '⚡ 默认回复模式已开启' : '🔒 已切换为需@唤醒模式');
      } catch (e) {
        if (arToggle) arToggle.checked = _lastConfirmedDashboard.auto_reply_mode !== false;
        showToast('❌ 切换默认回复模式失败: ' + (e.message || e));
      } finally {
        if (arToggle) arToggle.disabled = false;
        setTimeout(refreshDashboard, 600);
      }
    }

    async function toggleTrainingMaster(enabled) {
      lockUi('training-master-toggle', 3500);
      const trToggle = document.getElementById('training-master-toggle');
      if (trToggle) trToggle.disabled = true;
      try {
        const res = await sendSwitchSafe('/api/training/toggle', { enable: enabled });
        if (res && res.uncertain) {
          showToast('⚠️ 训练开关结果待确认，已核实当前状态');
          if (trToggle && res.verifiedStatus) trToggle.checked = !!res.verifiedStatus.training_mode;
          return;
        }
        const confirmed = res.training_mode !== undefined ? !!res.training_mode : !!enabled;
        if (trToggle) trToggle.checked = confirmed;
        showToast(confirmed ? '📚 群聊风格实时采集与训练已开启' : '已关闭群聊训练模式');
      } catch (e) {
        if (trToggle) trToggle.checked = !!_lastConfirmedDashboard.training_mode;
        showToast('❌ 切换训练模式失败: ' + (e.message || e));
      } finally {
        if (trToggle) trToggle.disabled = false;
        setTimeout(refreshDashboard, 600);
      }
    }

    async function triggerHistoryTrain(targetGid, btnEl) {
      const inputEl = document.getElementById('input-train-gid');
      const gid = (targetGid || (inputEl ? inputEl.value : '') || '').trim();
      if (!gid) {
        showToast('⚠️ 请在输入框输入群号，或直接点击下方群卡片的「⚡ 立即训练」');
        if (inputEl) {
          inputEl.focus();
          inputEl.classList.add('border-amber-400');
          setTimeout(() => inputEl.classList.remove('border-amber-400'), 1800);
        }
        return;
      }
      if (inputEl && !targetGid) {
        inputEl.value = gid;
      }
      const activeBtn = btnEl || document.getElementById('btn-trigger-train');
      const origText = activeBtn ? activeBtn.innerHTML : '⚡ 抓取历史并立即训练';
      if (activeBtn) {
        activeBtn.disabled = true;
        activeBtn.innerHTML = '<span>⏳ 正在训练...</span>';
      }
      showToast('⏳ 已提交群 ' + gid + ' 后台训练任务...');
      try {
        const res = await apiPost('/api/training/trigger', { group_id: gid, count: 120, async_mode: true }, 20000);
        const taskId = res.task_id || '';
        let finalJob = res.job || {};
        if (taskId && (res.status === 'pending' || res.status === 'running')) {
          const deadline = Date.now() + 90000;
          while (Date.now() < deadline) {
            await new Promise(r => setTimeout(r, 1200));
            const pollCtrl = new AbortController();
            const pollTimer = setTimeout(() => pollCtrl.abort(), 10000);
            try {
              const pResp = await fetch('/api/training/job/' + encodeURIComponent(taskId), { signal: pollCtrl.signal, cache: 'no-store' });
              const pData = await pResp.json();
              finalJob = pData.job || pData || {};
              if (!['pending', 'running'].includes(finalJob.status)) {
                break;
              }
            } finally {
              clearTimeout(pollTimer);
            }
          }
        }

        const tr = finalJob.result || res.result || {};
        const st = finalJob.status || tr.status || res.status;
        if (st === 'error' || st === 'empty' || st === 'cancelled' || tr.status === 'error' || tr.status === 'empty') {
          const errReason = finalJob.error_message || tr.message || res.message || '无有效训练语料或模型提炼失败';
          showToast(`❌ 群 ${gid} 训练未完成 (${st}): ${errReason}`);
        } else if (st === 'done' || st === 'ok' || tr.status === 'ok') {
          const lines = tr.fetched_history_count !== undefined ? tr.fetched_history_count : (tr.lines || 0);
          const pairs = tr.dataset_pairs || 0;
          const exprs = tr.learned_expressions || 0;
          showToast(`✅ 群 ${gid} 训练完成！有效语料 ${lines} 条，真实引用对话对 ${pairs} 组，场景表达 ${exprs} 条`);
        } else {
          showToast(`⏳ 群 ${gid} 训练状态: ${st || '已进入后台队列'} (task_id=${taskId.slice(0, 8)})`);
        }
      } catch (e) {
        if (e && e.isUncertainReceipt) {
          showToast('⚠️ 训练提交回执超时（结果待确认），请在下方训练列表查看真实任务状态，不会自动重发');
        } else {
          showToast('❌ 训练请求失败: ' + (e && e.message ? e.message : e));
        }
      } finally {
        if (activeBtn) {
          activeBtn.disabled = false;
          activeBtn.innerHTML = origText;
        }
        setTimeout(refreshDashboard, 500);
      }
    }

    function renderTrainingHub(tStatus, trainingGroups) {
      const box = document.getElementById('training-groups-table');
      if (!box) return;
      const groups = tStatus.groups || [];
      const jobs = tStatus.jobs || [];
      if (!groups.length && !jobs.length) {
        box.innerHTML = '<div class="text-xs text-slate-500 py-3 text-center">暂无已采集训练记录，输入群号点击上方按钮即可一键抓取并训练</div>';
        return;
      }
      const latestJobByGid = {};
      jobs.forEach(j => {
        if (j && j.group_id && !latestJobByGid[j.group_id]) {
          latestJobByGid[j.group_id] = j;
        }
      });

      box.innerHTML = groups.map(g => {
        const j = latestJobByGid[String(g.group_id)] || {};
        const jobSt = j.status || g.status || (g.has_trained_profile ? 'done' : 'idle');
        const errTxt = j.error_message || g.last_error || '';
        let stBadge = '<span class="text-slate-400">待训练</span>';
        if (jobSt === 'running' || jobSt === 'pending') {
          stBadge = `<span class="text-amber-300 font-bold">${jobSt.toUpperCase()}</span>`;
        } else if (jobSt === 'error') {
          stBadge = `<span class="text-rose-400 font-bold" title="${escapePanelText(errTxt)}">失败: ${escapePanelText(errTxt.slice(0, 26) || '异常')}</span>`;
        } else if (jobSt === 'cancelled') {
          stBadge = '<span class="text-slate-400">已取消</span>';
        } else if (g.has_trained_profile || jobSt === 'done') {
          stBadge = '<span class="text-emerald-400 font-bold">已完成</span>';
        }
        return `
          <div class="p-2.5 rounded-xl bg-surface-950 border border-surface-800 flex flex-wrap items-center justify-between gap-2 text-xs hover:border-surface-700 transition-colors">
            <div class="cursor-pointer select-none min-w-0 flex-1" onclick="const el = document.getElementById('input-train-gid'); if(el){ el.value='${g.group_id}'; el.focus(); showToast('已填入群 ${g.group_id}'); }">
              <div class="flex flex-wrap items-center gap-2">
                <span class="font-mono font-bold text-slate-200 hover:text-sky-300">群 ${g.group_id}</span>
                <span class="text-[10px] px-1.5 py-0.5 rounded bg-surface-800">${stBadge}</span>
              </div>
              <div class="text-[11px] font-mono text-slate-400 mt-1 break-words">
                语料: ${g.raw_lines} 行 · 引用对话对: ${g.dataset_pairs} 条 · 场景表达: ${g.learned_expressions || 0} 条
                ${g.last_trained_at ? '· 上次: ' + g.last_trained_at.slice(5, 16) : ''}
              </div>
            </div>
            <div class="flex items-center gap-1.5 shrink-0">
              <button onclick="triggerHistoryTrain('${g.group_id}', this)" class="tactile-btn px-2.5 py-1 rounded-lg bg-sky-500/15 hover:bg-sky-500/25 border border-sky-500/30 text-sky-300 text-[11px] whitespace-nowrap">
                ⚡ 立即训练
              </button>
            </div>
          </div>
        `;
      }).join('');
    }

    async function sendTestChatToDefaultQQ() {
      const text = (document.getElementById('input-test-chat').value || '').trim();
      if (!text) return;
      const stMsg = document.getElementById('test-chat-status');
      const btn = document.getElementById('btn-send-test-chat');
      if (btn) btn.disabled = true;
      if (stMsg) stMsg.innerText = '正在生成并投递私聊...';
      try {
        const resp = await fetch('/api/test/send_private_chat?text=' + encodeURIComponent(text), { cache: 'no-store' });
        const res = await resp.json();
        if (resp.ok && res.code === 0) {
          if (stMsg) stMsg.innerText = '已发送！Bot 回复: ' + (res.reply_text || '无');
          showToast('✅ 对话已发送至 QQ ' + res.uid + '！');
        } else {
          if (stMsg) stMsg.innerText = '发送失败: ' + (res.error || res.message || '状态异常');
        }
        refreshDashboard();
      } catch (e) {
        if (stMsg) stMsg.innerText = '请求异常: ' + e;
      } finally {
        if (btn) btn.disabled = false;
      }
    }

    async function toggleGlobal(active) {
      lockUi('bot-global-toggle', 3500);
      const gToggle = document.getElementById('bot-global-toggle');
      if (gToggle) gToggle.disabled = true;
      try {
        const res = await sendSwitchSafe('/api/switch/global', { active });
        if (res && res.uncertain) {
          showToast('⚠️ 全局总闸切换结果待确认，已核实当前状态');
          if (gToggle && res.verifiedStatus) gToggle.checked = !!res.verifiedStatus.global_active;
          return;
        }
        const confirmed = res.global_active !== undefined ? !!res.global_active : !!active;
        if (gToggle) gToggle.checked = confirmed;
        showToast(confirmed ? '已开启机器人全局响应' : '已切换为全局静默模式');
      } catch (e) {
        if (gToggle) gToggle.checked = !!_lastConfirmedDashboard.global_active;
        showToast('❌ 切换全局状态失败: ' + (e.message || e));
      } finally {
        if (gToggle) gToggle.disabled = false;
        setTimeout(refreshDashboard, 600);
      }
    }

    async function setCharacter(character) {
      document.querySelectorAll('.char-btn').forEach(b => { b.disabled = true; });
      try {
        const res = await sendSwitchSafe('/api/switch/character', { character });
        if (res && res.uncertain) {
          showToast('⚠️ 角色切换结果待确认，已通过只读接口核实');
          return;
        }
        const confirmedChar = res.active_character || character;
        showToast('已切换当前人格模式: ' + confirmedChar);
        window.dispatchEvent(new CustomEvent('operator:select', { detail: { id: confirmedChar } }));
      } catch (e) {
        showToast('❌ 切换角色失败: ' + (e.message || e));
      } finally {
        document.querySelectorAll('.char-btn').forEach(b => { b.disabled = false; });
        refreshDashboard();
      }
    }

    async function modifyGroup(action, gid, btnEl) {
      const groupId = gid || document.getElementById('input-group-id').value.trim();
      if (!groupId) return showToast('请输入有效的 QQ 群号');
      const btn = btnEl || document.getElementById('btn-add-group');
      if (btn) btn.disabled = true;
      try {
        const res = await sendSwitchSafe('/api/switch/group', { action, group_id: groupId });
        if (res && res.uncertain) {
          showToast('⚠️ 群白名单变更结果待确认，正在刷新状态');
          return;
        }
        document.getElementById('input-group-id').value = '';
        showToast(action === 'add' ? `已将群 ${groupId} 加入白名单` : `已移除群 ${groupId}`);
      } catch (e) {
        showToast('❌ 更新群白名单失败: ' + (e.message || e));
      } finally {
        if (btn) btn.disabled = false;
        refreshDashboard();
      }
    }

    async function modifyComfort(enable, qq, btnEl) {
      const target = qq || document.getElementById('input-comfort-qq').value.trim();
      if (!target) return showToast('请输入 QQ 号');
      const btn = btnEl || document.getElementById('btn-add-comfort');
      if (btn) btn.disabled = true;
      try {
        const res = await sendSwitchSafe('/api/switch/comfort', { target, enable });
        if (res && res.uncertain) {
          showToast('⚠️ 安慰名单变更结果待确认，正在刷新状态');
          return;
        }
        document.getElementById('input-comfort-qq').value = '';
        showToast(enable ? `已为 ${target} 开启安慰模式` : `已解除 ${target} 安慰绑定`);
      } catch (e) {
        showToast('❌ 更新安慰绑定失败: ' + (e.message || e));
      } finally {
        if (btn) btn.disabled = false;
        refreshDashboard();
      }
    }

    function loadTxtFile(input) {
      const file = input.files?.[0];
      if (!file) return;
      const groupGuess = file.name.replace(/\.[^.]+$/, '');
      if (/^\d{5,12}$/.test(groupGuess) && !document.getElementById('extract-group-id').value) {
        document.getElementById('extract-group-id').value = groupGuess;
      }
      const reader = new FileReader();
      reader.onload = e => {
        document.getElementById('extract-raw-text').value = e.target.result || '';
        showToast('已加载聊天记录文件: ' + file.name);
      };
      reader.readAsText(file, 'utf-8');
    }

    async function submitGroupExtract() {
      const gid = document.getElementById('extract-group-id').value.trim();
      const raw = document.getElementById('extract-raw-text').value.trim();
      if (!gid || !raw) return showToast('请填写群号并粘贴或选择聊天记录文本');
      const statusEl = document.getElementById('extract-status-msg');
      const btn = document.getElementById('btn-extract-group');
      if (btn) btn.disabled = true;
      if (statusEl) statusEl.innerText = '正在清洗 CQ 码并提炼群表达参考...';
      try {
        const res = await apiPost('/api/extract/group', { group_id: gid, raw_text: raw }, 90000);
        if (res && res.result && res.result.status === 'ok') {
          if (statusEl) statusEl.innerText = `✅ 已生成 ${gid}.json (${res.result.lines || 0} 行有效记录, ${res.result.learned_expressions || 0} 条场景表达)`;
          showToast(`群 ${gid} 风格与场景表达提炼完成`);
          refreshDashboard();
        } else {
          const msg = (res && (res.message || (res.result && res.result.message))) || '未提取到有效语料';
          if (statusEl) statusEl.innerText = '❌ ' + msg;
          showToast('❌ 提炼失败: ' + msg);
        }
      } catch (e) {
        if (statusEl) statusEl.innerText = (e && e.isUncertainReceipt) ? '⚠️ 提炼结果待确认，请刷新查看' : ('❌ ' + (e.message || e));
        showToast('❌ 提炼请求未完成: ' + (e && e.message ? e.message : e));
      } finally {
        if (btn) btn.disabled = false;
      }
    }

    async function saveVoiceConfig() {
      lockUi('voice-config', 3000);
      const enabled = document.getElementById('voice-main-toggle').checked;
      const voiceOnly = document.getElementById('voice-only-toggle').checked;
      const emotionDirectorEnabled = document.getElementById('voice-emotion-director-toggle').checked;
      let vMode = 'emotion_auto';
      document.getElementsByName('voice_mode_radio').forEach(r => { if (r.checked) vMode = r.value; });
      const refName = document.getElementById('voice-ref-select').value || 'default';
      const speed = parseFloat(document.getElementById('slider-voice-speed').value || '1.0');
      const temp = parseFloat(document.getElementById('slider-voice-temp').value || '0.9');
      const topp = parseFloat(document.getElementById('slider-voice-topp').value || '0.95');

      try {
        const res = await sendSwitchSafe('/api/voice/config', {
          voice_model: document.getElementById('voice-model-select').value,
          voice_enabled: enabled,
          voice_only_reply: voiceOnly,
          voice_mode: vMode,
          voice_emotion_director_enabled: emotionDirectorEnabled,
          voice_ref_name: refName,
          voice_speed: speed,
          voice_temperature: temp,
          voice_top_p: topp
        });
        if (res && res.uncertain) {
          showToast('⚠️ 语音配置结果待确认，正在核实当前状态');
          return;
        }
        showToast('已同步语音模式声学配置');
      } catch (e) {
        showToast('❌ 保存语音配置失败: ' + (e.message || e));
      } finally {
        refreshDashboard();
      }
    }

    async function resetVoiceParams() {
      document.getElementById('slider-voice-speed').value = 1.00;
      document.getElementById('val-voice-speed').innerText = '1.00x';
      document.getElementById('slider-voice-temp').value = 0.90;
      document.getElementById('val-voice-temp').innerText = '0.90';
      document.getElementById('slider-voice-topp').value = 0.95;
      document.getElementById('val-voice-topp').innerText = '0.95';
      await saveVoiceConfig();
    }

    async function controlVoiceService(target, action) {
      showToast(`正在 ${action === 'start' ? '启动' : '停止'} ${target} 服务...`);
      try {
        const res = await apiPost('/api/voice/service', { target, action }, 12000);
        showToast(res.message || '操作已发送');
      } catch (e) {
        showToast('❌ 服务控制失败: ' + (e.message || e));
      } finally {
        setTimeout(refreshDashboard, 1200);
      }
    }

    async function runVoicePreview() {
      const text = document.getElementById('preview-tts-text').value.trim();
      if (!text) return showToast('请输入试听台词');
      const tag = document.getElementById('preview-status-tag');
      const btn = document.getElementById('btn-tts-preview');
      tag.innerText = 'SYNTHESIZING...';
      btn.disabled = true;
      try {
        const refName = document.getElementById('voice-ref-select').value || '';
        const res = await apiPost('/api/voice/preview', { text, ref_name: refName }, 60000);
        if (res.code === 0 && res.audio_data_uri) {
          const player = document.getElementById('preview-audio-player');
          player.src = res.audio_data_uri;
          player.play();
          tag.innerText = 'DONE';
          showToast('语音合成成功，正在播放');
        } else {
          tag.innerText = 'OFFLINE / ERR';
          showToast(res.message || '合成失败，请确认 9880 服务已启动');
        }
      } catch (e) {
        tag.innerText = 'ERR';
        showToast('❌ 试听失败: ' + (e.message || e));
      } finally {
        btn.disabled = false;
      }
    }

    async function restartBackend() {
      showToast('正在热重启 8080 后端...');
      try {
        await apiPost('/api/restart', {}, 8000);
      } catch (_) {}
      setTimeout(refreshDashboard, 1800);
    }

    refreshDashboard();
    setInterval(refreshDashboard, 2000);


    (function() {
      let expandedAll = false;
      const savedHeights = {};

      const PIPELINE_DEFAULTS = [
        {
          slot: 'text',
          title: '📝 文字生成流水线',
          stage: '待机中 · 等待群聊/私聊消息触发（解析别称 → 百炼/DeepSeek推理 → 文档/消息下发）',
          percent: 0,
          status: 'idle',
          input_text: '',
          prompt_text: '',
          output_text: '',
          extra_info: '支持超过 650 字自动打包 .docx / .pdf 文档',
          steps: []
        },
        {
          slot: 'image',
          title: '🎨 二次元生图（4 阶段）',
          stage: '待机中 · 1/4 别称与refs立绘匹配 → 2/4 百炼Agent发散 → 3/4 深度思考渲染 → 4/4 压缩下发',
          percent: 0,
          status: 'idle',
          input_text: '',
          prompt_text: '',
          output_text: '',
          extra_info: 'Agent: 97fcb1dc96504f879c58f3b4c3a3e6d0 | 模型: qwen-image-2.1-pro',
          steps: []
        },
        {
          slot: 'voice',
          title: '🎙️ 语音合成（3 阶段）',
          stage: '待机中 · 1/3 情绪参考音频匹配 → 2/3 M5 MPS (:9880) 声学推理 → 3/3 SILK_v3 编码回传',
          percent: 0,
          status: 'idle',
          input_text: '',
          prompt_text: '',
          output_text: '',
          extra_info: '引擎: GPT-SoVITS v2ProPlus + pilk SILK_v3',
          steps: []
        }
      ];

      function fitTextarea(el) {
        if (!el) return;
        el.style.height = 'auto';
        el.style.height = Math.max(74, Math.min(520, el.scrollHeight + 8)) + 'px';
      }

      window.toggleAllBoxesExpand = function() {
        expandedAll = !expandedAll;
        document.querySelectorAll('textarea[data-box-key], textarea#extract-raw-text, textarea#rag-entry-content, textarea#preview-tts-text, .rt-textarea').forEach(el => {
          const k = el.getAttribute('data-box-key');
          if (expandedAll) {
            fitTextarea(el);
            if (k) savedHeights[k] = el.style.height;
          } else {
            el.style.height = '74px';
            if (k) delete savedHeights[k];
          }
        });
        if (typeof showToast === 'function') {
          showToast(expandedAll ? '已一键展开目标文本框高度' : '已恢复目标文本框默认高度');
        }
      };

      window.__toggleAllBoxesExpand = window.toggleAllBoxesExpand;

      function bindResizeObserver(container) {
        container.querySelectorAll('textarea[data-box-key]').forEach(ta => {
          const k = ta.getAttribute('data-box-key');
          if (savedHeights[k]) {
            ta.style.height = savedHeights[k];
          } else if (expandedAll) {
            fitTextarea(ta);
          }
          ta.addEventListener('mouseup', () => {
            if (ta.style.height) savedHeights[k] = ta.style.height;
          });
        });
      }

      function pickLatestByKind(tasks, kind) {
        for (const t of tasks) {
          if (t.kind === kind) return t;
        }
        return null;
      }

      function renderCards(tasks) {
        const container = document.getElementById('rt-cards-container');
        if (!container) return;

        container.querySelectorAll('textarea[data-box-key]').forEach(ta => {
          const k = ta.getAttribute('data-box-key');
          if (k && ta.style.height) savedHeights[k] = ta.style.height;
        });

        const cardsData = PIPELINE_DEFAULTS.map(def => {
          const matched = pickLatestByKind(tasks, def.slot);
          return matched ? Object.assign({}, def, matched, { slot: def.slot, title: def.title }) : def;
        });

        container.innerHTML = cardsData.map(t => {
          const keyPrefix = t.slot;
          const st = t.status || 'idle';
          const pct = st === 'idle' ? 0 : Math.min(100, Math.max(8, Number(t.percent || 0)));
          const badgeText = st === 'running' ? ('进行中 ' + pct + '%') : (st === 'done' ? '已完成 100%' : (st === 'error' ? '异常' : '待机中'));
          const timeMeta = t.updated_at ? `${t.updated_at} · ${t.elapsed_s || 0}s` : '等待触发';
          const stepsText = (t.steps || []).map(s => `[${s.time}] (${s.percent}%) ${s.stage}`).join('\n');
          const outCombined = (t.output_text ? t.output_text + (stepsText ? '\n━━━━━━━━━━━━━━\n' : '') : '') + stepsText;

          return `
            <div class="rt-card">
              <div class="flex flex-wrap items-center justify-between gap-2">
                <div class="font-bold text-xs text-slate-100">${escapePanelText(t.title)}</div>
                <div class="flex items-center gap-1.5">
                  <span class="text-[11px] font-mono text-slate-400">${escapePanelText(timeMeta)}</span>
                  <span class="rt-badge ${st}">${badgeText}</span>
                </div>
              </div>
              <div class="text-xs text-sky-300 font-mono truncate" title="${escapePanelText(t.stage)}">${escapePanelText(t.stage || '-')}</div>
              <div class="rt-bar-bg"><div class="rt-bar-fill" style="width:${pct}%;"></div></div>
              <div class="text-[11px] text-slate-400 font-mono truncate">${escapePanelText(t.source ? t.source + ' | ' : '')}${escapePanelText(t.extra_info)}</div>

              <div class="rt-field-label">
                <span>📥 接收信息</span>
                <span class="font-mono">${(t.input_text || '').length} 字</span>
              </div>
              <textarea class="rt-textarea" data-box-key="${keyPrefix}_in" readonly placeholder="尚无接收信息..."></textarea>

              <div class="rt-field-label">
                <span>🧠 完整生图/声学 Prompt</span>
                <span class="font-mono">${(t.prompt_text || '').length} 字</span>
              </div>
              <textarea class="rt-textarea" data-box-key="${keyPrefix}_prompt" readonly placeholder="尚无重写/声学 Prompt..."></textarea>

              <div class="rt-field-label">
                <span>📤 输出结果与阶段流水</span>
                <span class="font-mono">${outCombined.length} 字</span>
              </div>
              <textarea class="rt-textarea" data-box-key="${keyPrefix}_out" readonly placeholder="尚无输出结果与阶段流水..."></textarea>
            </div>
          `;
        }).join('');

        cardsData.forEach(t => {
          const keyPrefix = t.slot;
          const stepsText = (t.steps || []).map(s => `[${s.time}] (${s.percent}%) ${s.stage}`).join('\n');
          const outCombined = (t.output_text ? t.output_text + (stepsText ? '\n━━━━━━━━━━━━━━\n' : '') : '') + stepsText;
          const elIn = container.querySelector(`textarea[data-box-key="${keyPrefix}_in"]`);
          const elPr = container.querySelector(`textarea[data-box-key="${keyPrefix}_prompt"]`);
          const elOut = container.querySelector(`textarea[data-box-key="${keyPrefix}_out"]`);
          if (elIn) elIn.value = t.input_text || '';
          if (elPr) elPr.value = t.prompt_text || '';
          if (elOut) elOut.value = outCombined || '';
        });

        bindResizeObserver(container);
      }

      async function pollProgress() {
        try {
          const r = await fetch('/api/progress', { cache: 'no-store' });
          if (!r.ok) return;
          const data = await r.json();
          renderCards(data.tasks || []);
        } catch (e) {}
      }

      renderCards([]);
      pollProgress();
      setInterval(pollProgress, 1200);
    })();


// 配置只提交当前修改字段；轮询不会覆盖尚未保存的本地输入。
const conversationDailyFields = {
  accept_poke: 'accept-poke-toggle',
  fast_reply_mode: 'rag-fast-toggle',
  chat_1v1_enabled: 'rag-history-toggle',
  group_buffer_enabled: 'rag-buffer-toggle',
  daily_9am_enabled: 'daily-enabled-toggle',
  daily_qzone_enabled: 'daily-qzone-toggle',
  daily_morning_group_enabled: 'daily-morning-toggle',
  daily_schedule_time: 'daily-time-input'
};
const pendingConversationSettings = new Set();
const pendingDailyActions = new Set();
let conversationDailySummary = {};
let conversationDailyRefreshing = false;

async function panelRequestJSON(path, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 8000);
  try {
    const response = await fetch(path, {cache: 'no-store', ...options, signal: controller.signal});
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '请求参数或服务状态异常');
    return result;
  } finally {
    clearTimeout(timer);
  }
}

function renderPanelRagEntries(entries) {
  const parent = document.getElementById('panel-rag-entries');
  if (!parent) return;
  parent.replaceChildren();
  for (const entry of entries.slice(0, 20)) {
    const row = document.createElement('div');
    row.className = 'rounded-lg border border-surface-700 p-2 break-words';
    const title = document.createElement('strong');
    title.textContent = `[${entry.character}] ${entry.title}`;
    const content = document.createElement('p');
    content.className = 'text-slate-400 mt-1';
    content.textContent = entry.content;
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'text-red-300 mt-2';
    remove.textContent = '删除此条';
    remove.onclick = async () => {
      remove.disabled = true;
      try {
        const result = await panelRequestJSON('/api/rag/delete', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id: entry.id})});
        if (result.code !== 0) throw new Error('条目已不存在');
        showToast('知识已删除');
        await refreshConversationDailyPanel();
      } catch (error) {
        showToast('删除失败：' + error.message);
      } finally { remove.disabled = false; }
    };
    row.append(title, content, remove);
    parent.append(row);
  }
  if (!entries.length) parent.textContent = '知识库暂无条目';
}

async function refreshConversationDailyPanel() {
  if (conversationDailyRefreshing) return;
  conversationDailyRefreshing = true;
  try {
    const result = await panelRequestJSON('/api/rag/status');
    if (result.code !== 0) throw new Error('状态读取失败');
    conversationDailySummary = result.summary;
    for (const [field, id] of Object.entries(conversationDailyFields)) {
      const input = document.getElementById(id);
      if (!input || pendingConversationSettings.has(field) || document.activeElement === input) continue;
      if (input.type === 'checkbox') input.checked = !!result.summary[field];
      else input.value = result.summary[field] || '09:00';
    }
    const reqSt = document.getElementById('rag-request-status');
    if (reqSt) {
      reqSt.textContent = result.summary.initialization_error || result.summary.conversation_policy + `；最近快速请求 ${result.summary.last_latency_ms || 0}ms`;
    }
    for (const kind of ['qzone', 'morning']) {
      const job = (result.summary.daily_jobs || {})[kind] || {};
      const prefix = kind === 'qzone' ? '空间：' : '早安：';
      const rEl = document.getElementById(`daily-${kind}-result`);
      const bEl = document.getElementById(`daily-${kind}-button`);
      const aEl = document.getElementById(`daily-${kind}-acknowledge`);
      if (rEl) rEl.textContent = prefix + (job.status_label || '暂无任务') + (job.persistence_warning ? '；' + job.persistence_warning : '');
      if (bEl) bEl.disabled = pendingDailyActions.has(kind) || job.can_start === false;
      if (aEl) aEl.hidden = job.can_start !== false || ['pending', 'running'].includes(job.status);
    }
    renderPanelRagEntries(result.entries || []);
  } catch (error) {
    const reqSt = document.getElementById('rag-request-status');
    if (reqSt) reqSt.textContent = '状态暂不可用：' + error.message;
  } finally {
    conversationDailyRefreshing = false;
  }
}

async function saveRagSetting(key, input) {
  pendingConversationSettings.add(key);
  input.disabled = true;
  const value = input.type === 'checkbox' ? input.checked : input.value;
  try {
    const result = await panelRequestJSON('/api/rag/config', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({[key]: value})});
    if (result.code !== 0) throw new Error('配置未保存');
    showToast('设置已保存');
  } catch (error) {
    if (input.type === 'checkbox') input.checked = !!conversationDailySummary[key];
    else input.value = conversationDailySummary[key] || '09:00';
    showToast('保存失败：' + error.message);
  } finally {
    pendingConversationSettings.delete(key);
    input.disabled = false;
    await refreshConversationDailyPanel();
  }
}

async function submitPanelDaily(kind) {
  if (pendingDailyActions.has(kind)) return;
  pendingDailyActions.add(kind);
  const button = document.getElementById(`daily-${kind}-button`);
  const label = document.getElementById(`daily-${kind}-result`);
  button.disabled = true;
  label.textContent = '正在提交任务…';
  const requestId = window.crypto.randomUUID();
  try {
    const result = await panelRequestJSON(`/api/daily/${kind === 'qzone' ? 'qzone_now' : 'morning_now'}`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({with_image: document.getElementById('daily-with-image').checked, request_id: requestId})
    });
    label.textContent = result.status_label || '任务已受理，等待实际投递结果';
    showToast(result.status === 'pending' ? '任务已受理，可在任务进度中查看' : label.textContent);
  } catch (error) {
    label.textContent = '提交结果未确认，请先查看任务状态；不会自动重发。' + error.message;
  } finally {
    pendingDailyActions.delete(kind);
    await refreshConversationDailyPanel();
  }
}

async function acknowledgePanelDaily(kind) {
  const job = (conversationDailySummary.daily_jobs || {})[kind] || {};
  const button = document.getElementById(`daily-${kind}-acknowledge`);
  button.disabled = true;
  try {
    await panelRequestJSON(`/api/daily/${kind}/acknowledge`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({task_id: job.task_id})});
    showToast('已解除手动重发限制；没有发送消息');
    await refreshConversationDailyPanel();
  } catch (error) { showToast('状态确认失败：' + error.message); }
  finally { button.disabled = false; }
}

async function addPanelRagEntry(button) {
  const title = document.getElementById('rag-entry-title').value.trim();
  const content = document.getElementById('rag-entry-content').value.trim();
  if (!title || !content) return showToast('请填写知识标题和正文');
  button.disabled = true;
  try {
    const result = await panelRequestJSON('/api/rag/add', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({title, content, character: document.getElementById('rag-entry-character').value, keywords: document.getElementById('rag-entry-keywords').value.trim()})
    });
    if (result.code !== 0) throw new Error('知识未保存');
    document.getElementById('rag-entry-title').value = '';
    document.getElementById('rag-entry-content').value = '';
    document.getElementById('rag-entry-keywords').value = '';
    showToast('知识已保存');
    await refreshConversationDailyPanel();
  } catch (error) { showToast('保存失败：' + error.message); }
  finally { button.disabled = false; }
}

(async function pollConversationDaily() {
  await refreshConversationDailyPanel();
  setTimeout(pollConversationDaily, 8000);
})();

// 3D 舞台点击角色后回写现有角色切换流程（舞台未加载时不会触发）
window.addEventListener('operator:changed', function (event) {
  const id = event && event.detail && event.detail.id;
  if (!id) return;
  setCharacter(id).catch(function (err) {
    console.error('[panel] operator sync failed', err);
  });
});


async function saveAutonomousSetting(key, inputOrBool) {
  lockUi('autonomous-setting', 3500);
  const isElem = inputOrBool && typeof inputOrBool === 'object' && 'checked' in inputOrBool;
  const enabled = isElem ? !!inputOrBool.checked : !!inputOrBool;
  if (isElem) inputOrBool.disabled = true;
  try {
    const res = await sendSwitchSafe('/api/autonomous/config', { [key]: enabled });
    if (res && res.uncertain) {
      showToast('⚠️ 自主决策开关结果待确认，正在核实状态');
      const ro = await fetch('/api/autonomous/status', { cache: 'no-store' }).then(r => r.json()).catch(() => null);
      if (ro && ro.data) renderAutonomousStatus(ro.data);
      return;
    }
    if (res && res.data) {
      renderAutonomousStatus(res.data);
      showToast('已更新自主决策设置');
    }
  } catch (e) {
    if (_lastConfirmedDashboard.autonomous_status) {
      renderAutonomousStatus(_lastConfirmedDashboard.autonomous_status);
    }
    showToast('❌ 更新自主设置失败: ' + (e.message || e));
  } finally {
    if (isElem) inputOrBool.disabled = false;
  }
}

function renderAutonomousStatus(aStatus) {
  const gToggle = document.getElementById('autonomous-group-toggle');
  const qToggle = document.getElementById('autonomous-qzone-toggle');
  const qBadge = document.getElementById('autonomous-quiet-badge');
  const stText = document.getElementById('autonomous-status-text');
  const actList = document.getElementById('autonomous-actions-list');

  if (!isUiLocked('autonomous-setting')) {
    if (gToggle && !gToggle.disabled) gToggle.checked = !!aStatus.autonomous_group_chat_enabled;
    if (qToggle && !qToggle.disabled) qToggle.checked = !!aStatus.autonomous_qzone_enabled;
  }
  if (qBadge) {
    const winStr = aStatus.quiet_hours_window || '23:00 - 08:00';
    qBadge.textContent = aStatus.quiet_hours_active ? `🌙 安静时段生效中 (${winStr})` : `☀️ 活跃时段 (安静: ${winStr})`;
    qBadge.className = aStatus.quiet_hours_active
      ? 'px-2 py-0.5 rounded text-[10px] font-mono bg-amber-500/20 text-amber-300 border border-amber-500/30'
      : 'px-2 py-0.5 rounded text-[10px] font-mono bg-surface-800 text-slate-300';
  }
  if (stText) {
    const mood = (aStatus.mood && aStatus.mood.mood) ? aStatus.mood.mood : '平静温和';
    const qzCount = aStatus.qzone_today_count !== undefined ? `${aStatus.qzone_today_count}/${aStatus.qzone_max_per_day || 2}` : '0/2';
    stText.textContent = `当前心情：${mood} · 今日空间已发：${qzCount} 条 (间隔≥${aStatus.qzone_min_interval_hours || 6}h · 状态：${aStatus.qzone_quota_reason || 'ok'})`;
  }
  if (actList) {
    const actions = (aStatus.recent_actions || []).slice().reverse();
    if (!actions.length) {
      actList.innerHTML = '<div class="text-slate-500">暂无近期自主决策记录</div>';
    } else {
      actList.innerHTML = actions.slice(0, 6).map(act => {
        const delivTag = act.delivered
          ? '<span class="text-emerald-400">已送达</span>'
          : (act.status === 'uncertain' ? '<span class="text-amber-300">结果待确认</span>' : '<span class="text-rose-400">未送达</span>');
        const tsStr = act.ts ? new Date(act.ts * 1000).toLocaleTimeString() : '';
        return `<div class="p-1.5 rounded bg-surface-950 border border-surface-800 break-words">[${escapePanelText(tsStr)}] ${escapePanelText(act.action)} (群:${escapePanelText(act.group_id || '-')} · ${delivTag})：${escapePanelText((act.content || '').slice(0, 38))}</div>`;
      }).join('');
    }
  }
}

function formatMem0StatusBadge(status, err) {
  const st = status || 'unconfigured';
  if (st === 'synced') return '<span class="text-emerald-400 font-bold">Mem0:已同步(synced)</span>';
  if (st === 'syncing') return '<span class="text-sky-300 font-bold">Mem0:同步中(syncing)</span>';
  if (st === 'pending_sync') return '<span class="text-amber-300">Mem0:待同步(pending_sync)</span>';
  if (st === 'failed') return `<span class="text-rose-400 font-bold" title="${escapePanelText(err)}">Mem0:失败(${escapePanelText(err ? err.slice(0, 22) : 'failed')})</span>`;
  return '<span class="text-slate-400" title="仅本地 SQLite 可用，未配置或未启用 Mem0 向量同步">Mem0:未配置(仅本地SQLite)</span>';
}

function renderProfilesSummaryList(profiles) {
  const box = document.getElementById('profile-summary-list');
  if (!box) return;
  if (!profiles || !profiles.length) {
    box.innerHTML = '<div class="text-[11px] text-slate-500">暂无已记录画像</div>';
    return;
  }
  box.innerHTML = profiles.slice(0, 12).map(p => {
    const scopeLabel = p.visibility === 'group' ? `群聊(${p.group_id})` : '私聊(private)';
    const charLabel = p.character_id || 'amiya';
    const syncBadge = formatMem0StatusBadge(p.mem0_sync_status || p.sync_status, p.mem0_last_error);
    return `
      <div onclick="selectProfileFromList('${p.uid}', '${p.group_id || ''}', '${charLabel}')" class="cursor-pointer p-2 rounded-lg bg-surface-950 border border-surface-800 hover:border-sky-500/40 flex flex-wrap items-center justify-between gap-1.5 text-[11px] font-mono">
        <span class="text-slate-200">QQ:${p.uid} · 角色:${charLabel} · ${scopeLabel} · v${p.profile_version || 1}</span>
        <span>${syncBadge}</span>
      </div>
    `;
  }).join('');
}

function selectProfileFromList(uid, groupId, charId) {
  const uEl = document.getElementById('profile-uid-input');
  const gEl = document.getElementById('profile-group-input');
  const cEl = document.getElementById('profile-char-select');
  if (uEl) uEl.value = uid || '';
  if (gEl) gEl.value = groupId || '';
  if (cEl && charId) cEl.value = charId;
  loadUserProfileForPanel();
}

async function loadUserProfileForPanel(btn) {
  const uid = (document.getElementById('profile-uid-input')?.value || '').trim();
  const gid = (document.getElementById('profile-group-input')?.value || '').trim();
  const charId = (document.getElementById('profile-char-select')?.value || 'amiya').trim();
  const out = document.getElementById('profile-manage-result');
  if (!uid) return showToast('请先输入要查询的 QQ 号');
  if (btn) btn.disabled = true;
  try {
    const params = new URLSearchParams();
    if (gid) params.set('group_id', gid);
    if (charId) params.set('character', charId);
    const q = params.toString() ? `?${params.toString()}` : '';
    const resp = await fetch(`/api/profiles/${encodeURIComponent(uid)}${q}`, { cache: 'no-store' });
    const data = await resp.json();
    if (!resp.ok || data.code === 400 || data.status === 'error') {
      throw new Error(data.message || '查询失败');
    }
    const p = data.profile || data;
    if (out) {
      const headerLine = `QQ: ${p.uid} | 角色: ${p.character_id || charId} | 范围: ${p.visibility === 'group' ? ('群 ' + p.group_id) : '私聊 (private)'} | 版本: v${p.profile_version}\nMem0同步状态: ${p.mem0_sync_status || p.sync_status} (已同步版本: v${p.mem0_synced_version || 0})${p.mem0_last_error ? '\nMem0最近错误: ' + p.mem0_last_error : ''}\n━━━━━━━━━━━━━━\n`;
      out.textContent = headerLine + JSON.stringify(p, null, 2);
    }
  } catch (e) {
    if (out) out.textContent = '❌ 查询失败: ' + (e.message || e);
    showToast('❌ 查询画像失败: ' + (e.message || e));
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function submitUserProfileCorrection(btn) {
  const uid = (document.getElementById('profile-uid-input')?.value || '').trim();
  const gid = (document.getElementById('profile-group-input')?.value || '').trim();
  const charId = (document.getElementById('profile-char-select')?.value || 'amiya').trim();
  const title = (document.getElementById('profile-title-input')?.value || '').trim();
  const addFact = (document.getElementById('profile-fact-input')?.value || '').trim();
  const invKw = (document.getElementById('profile-invalidate-input')?.value || '').trim();
  const out = document.getElementById('profile-manage-result');
  if (!uid) return showToast('请填写目标 QQ 号');
  if (btn) btn.disabled = true;
  try {
    const body = { uid, group_id: gid || null, character: charId, add_fact: addFact, invalidate_keyword: invKw };
    if (title) body.preferred_title = title;
    const res = await sendSwitchSafe('/api/profiles/correct', body);
    if (res && res.uncertain) {
      if (out) out.textContent = '⚠️ 画像纠正结果待确认（请求超时，正在通过只读查询核实，未自动重复提交）';
      showToast('⚠️ 画像保存结果待确认');
      await loadUserProfileForPanel();
      return;
    }
    const p = res.profile || res;
    if (out) {
      const headerLine = `✅ 已保存纠正 | QQ: ${p.uid} | 角色: ${p.character_id || charId} | 范围: ${p.visibility === 'group' ? ('群 ' + p.group_id) : '私聊'} | 版本: v${p.profile_version} | Mem0状态: ${p.mem0_sync_status || p.sync_status}\n━━━━━━━━━━━━━━\n`;
      out.textContent = headerLine + JSON.stringify(p, null, 2);
    }
    document.getElementById('profile-fact-input').value = '';
    document.getElementById('profile-invalidate-input').value = '';
    showToast('✅ 已更新该用户画像');
    refreshDashboard();
  } catch (e) {
    if (out) out.textContent = '❌ 保存纠正失败: ' + (e.message || e);
    showToast('❌ 更新画像失败: ' + (e.message || e));
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function deleteUserProfileForPanel(btn) {
  const uid = (document.getElementById('profile-uid-input')?.value || '').trim();
  const gid = (document.getElementById('profile-group-input')?.value || '').trim();
  const out = document.getElementById('profile-manage-result');
  if (!uid) return showToast('请填写目标 QQ 号');
  if (btn) btn.disabled = true;
  try {
    const res = await sendSwitchSafe('/api/profiles/delete', { uid, group_id: gid || null, all_scopes: !gid });
    if (res && res.uncertain) {
      if (out) out.textContent = '⚠️ 清理操作结果待确认，请点击查询画像核实';
      showToast('⚠️ 清理结果待确认');
      return;
    }
    if (out) out.textContent = JSON.stringify(res, null, 2);
    showToast(res.deleted ? `🧹 已定向清空 QQ ${uid} 的画像与关联聊天记录` : '该 QQ 暂无画像记录');
    refreshDashboard();
  } catch (e) {
    if (out) out.textContent = '❌ 清理失败: ' + (e.message || e);
    showToast('❌ 清理画像失败: ' + (e.message || e));
  } finally {
    if (btn) btn.disabled = false;
  }
}

let macActionSettingsLoaded = false;
async function refreshMacActions(fillForm = false) {
  const out = document.getElementById('mac-action-status');
  try {
    const res = await panelRequestJSON('/api/actuator/status');
    const cfg = res.mac_actuator;
    if (!macActionSettingsLoaded || fillForm) {
      document.getElementById('mac-action-enabled').checked = cfg.enabled;
      document.getElementById('mac-action-auto-care').checked = cfg.auto_care_enabled;
      document.getElementById('mac-action-auto-music').checked = cfg.auto_music_enabled;
      document.getElementById('mac-action-confirm').checked = cfg.require_confirmation;
      document.getElementById('mac-action-cooldown').value = cfg.cooldown_sec;
      document.getElementById('mac-action-daily').value = cfg.daily_max_actions;
      document.getElementById('mac-action-shortcuts').value = cfg.allowed_shortcuts.join('\n');
      macActionSettingsLoaded = true;
    }
    out.textContent = `${cfg.enabled ? '已启用' : '已关闭'} · 主人 ${cfg.owner_qq_whitelist.join('、')} · ${cfg.planning_jobs} 个规划任务`;
    const list = document.getElementById('mac-action-jobs');
    list.replaceChildren();
    for (const job of cfg.jobs) {
      const row = document.createElement('div');
      row.className = 'rounded-xl border border-surface-700 p-3 space-y-2 break-words';
      const label = document.createElement('p');
      label.textContent = `${job.status} · ${job.id} · ${job.summary}`;
      const details = document.createElement('pre');
      details.className = 'whitespace-pre-wrap break-words text-slate-400';
      details.textContent = JSON.stringify(job.result || job.plan, null, 2);
      row.append(label, details);
      if (job.status === 'prepared') {
        for (const [title, cancel] of [['确认执行', false], ['取消', true]]) {
          const button = document.createElement('button');
          button.type = 'button';
          button.textContent = title;
          button.className = 'tactile-btn px-3 py-2 mr-2 rounded-lg border border-surface-600';
          button.onclick = () => confirmMacAction(button, job.id, cancel);
          row.append(button);
        }
      }
      list.append(row);
    }
  } catch (error) { out.textContent = `读取失败：${error.message}`; }
}
async function macActionPost(button, path, payload, timeout = 10000) {
  button.disabled = true;
  const out = document.getElementById('mac-action-status');
  try {
    const result = await apiPost(path, payload, timeout);
    await refreshMacActions(false);
    return result;
  } catch (error) {
    out.textContent = error.isUncertainReceipt ? '结果待确认，请刷新动作记录核对，不要重复执行。' : `操作失败：${error.message}`;
    return null;
  } finally { button.disabled = false; }
}
async function saveMacActions(button) {
  const cooldown = Number(document.getElementById('mac-action-cooldown').value);
  const daily = Number(document.getElementById('mac-action-daily').value);
  if (!Number.isFinite(cooldown) || cooldown < 0 || cooldown > 86400 || !Number.isInteger(daily) || daily < 1 || daily > 100) return showToast('冷却或日配额超出范围');
  const result = await macActionPost(button, '/api/actuator/config', {
    enabled: document.getElementById('mac-action-enabled').checked,
    auto_care_enabled: document.getElementById('mac-action-auto-care').checked,
    auto_music_enabled: document.getElementById('mac-action-auto-music').checked,
    require_confirmation: document.getElementById('mac-action-confirm').checked,
    cooldown_sec: cooldown, daily_max_actions: daily,
    allowed_shortcuts: document.getElementById('mac-action-shortcuts').value.split('\n').map(x => x.trim()).filter(Boolean)
  });
  if (result) showToast('本机联动设置已保存');
}
async function testMacNotification(button) {
  await macActionPost(button, '/api/actuator/mac/test', {action: 'notify'});
}
async function planMacAction(button) {
  const text = document.getElementById('mac-action-request').value.trim();
  if (!text) return showToast('请输入希望准备的动作');
  const result = await macActionPost(button, '/api/actuator/mac/plan', {text}, 30000);
  if (result && !result.job) showToast('阿米娅判断此消息无需设备动作，或联动尚未启用');
}
async function confirmMacAction(button, jobId, cancel) {
  await macActionPost(button, '/api/actuator/mac/confirm', {job_id: jobId, cancel}, 50000);
}
async function verifyMuMu(button) {
  const result = await macActionPost(button, '/api/actuator/mumu/verify', {dry_run: true, task_goal: '定位屏幕左上角的返回按钮'}, 60000);
  if (result) document.getElementById('mumu-action-result').textContent = JSON.stringify(result.verification, null, 2);
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => refreshMacActions(false), {once: true});
else refreshMacActions(false);
