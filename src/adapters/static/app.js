// ─── Marked Config ──────────────────────────────────────
marked.setOptions({
  highlight: (code, lang) => {
    if (lang && hljs.getLanguage(lang)) return hljs.highlight(code, { language: lang }).value;
    return hljs.highlightAuto(code).value;
  },
  breaks: true,
  gfm: true,
});

// ─── State ─────────────────────────────────────────────
let convId = null, sending = false, paused = false;
let currentAbortController = null;  // 用于中断流式请求
const history = [];
let autoSaveTimer = null;

// ─── DOM ───────────────────────────────────────────────
const $ = id => document.getElementById(id);
const msgsEl = $('messages'), inputEl = $('input'),
      sendBtn = $('sendBtn'), welcomeEl = $('welcome');

// ─── Init ──────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  loadLLM();
  loadSavedConversations();
  loadSchedules();
  loadWorkflows();
  setInterval(loadTasks, 5000);
  setInterval(loadSchedules, 15000);
  setInterval(() => { if (history.length) saveConversation(); }, 30000);
  setupSSE();
  setupInput();
  setupSidebar();
  setupFileUpload();
});

// ─── SSE ───────────────────────────────────────────────
let sseConnection = null;

function setupSSE() {
  connectSSE();
}

function connectSSE() {
  if (sseConnection) { sseConnection.close(); sseConnection = null; }
  const url = convId ? `/events?conversation_id=${encodeURIComponent(convId)}` : '/events';
  sseConnection = new EventSource(url);
  sseConnection.onmessage = e => { try { handleSSE(JSON.parse(e.data)); } catch {} };
  sseConnection.onerror = () => {
    // 断线重连
    setTimeout(connectSSE, 3000);
  };
}

function reconnectSSE() {
  // 当 convId 变化时重连 SSE，带上新的 conversation_id
  connectSSE();
}

function handleSSE(ev) {
  if (paused) return;
  const source = ev.payload?.source || '';

  if (ev.event === 'task_completed' && ev.payload) {
    const result = ev.payload;
    let text = result.reply || result.text || result.output || '';
    if (typeof result === 'string') text = result;
    const src = source || 'skill';
    const skillName = ev.skill_name || '';
    addMsg('assistant', text || '✅ 任务完成', src, skillName);

    // 附件文件
    const files = result._files || result.files || [];
    files.forEach(f => {
      const el = document.createElement('div'); el.className = 'msg assistant';
      const isImg = /^image\//.test(f.mime), isVid = /^video\//.test(f.mime), isAud = /^audio\//.test(f.mime);
      let body = '';
      if (isImg)
        body = `<a href="/files/${ev.task_id}/${encodeURIComponent(f.name)}" target="_blank"><img src="/files/${ev.task_id}/${encodeURIComponent(f.name)}" alt="${escHtml(f.name)}" style="max-width:320px;max-height:240px;border-radius:8px;margin-top:8px;display:block;"/></a>`;
      else if (isVid)
        body = `<video controls style="max-width:400px;border-radius:8px;margin-top:8px;display:block;"><source src="/files/${ev.task_id}/${encodeURIComponent(f.name)}" type="${f.mime}">你的浏览器不支持 video</video>`;
      else if (isAud)
        body = `<audio controls style="width:280px;margin-top:8px;display:block;"><source src="/files/${ev.task_id}/${encodeURIComponent(f.name)}" type="${f.mime}">你的浏览器不支持 audio</audio>`;
      else
        body = `<div class="file-dl-wrap"><span class="file-dl-name">📎 ${escHtml(f.name)}</span><span class="file-dl-size">${fmtSize(f.size)}</span><a href="/files/${ev.task_id}/${encodeURIComponent(f.name)}" download="${escHtml(f.name)}" class="file-dl-btn">↓ 下载</a></div>`;
      const srcHtml = renderSourceLabel(src, skillName);
      el.innerHTML = `<div class="msg-av">🦝</div><div class="msg-body">${srcHtml}<div class="msg-bbl">${body}</div></div>`;
      msgsEl.appendChild(el); msgsEl.scrollTop = msgsEl.scrollHeight;
    });

    // 远程媒体
    const imgUrl = result.image_url, vidUrl = result.video_url;
    if (imgUrl || vidUrl) {
      const el = document.createElement('div'); el.className = 'msg assistant';
      const srcHtml = renderSourceLabel(src, skillName);
      let mediaBody = '';
      if (vidUrl) {
        mediaBody = `<div class="media-card"><div class="media-tag">🎬 视频</div><video controls style="max-width:480px;border-radius:8px;display:block;"><source src="${escHtml(vidUrl)}">你的浏览器不支持 video</video><a href="${escHtml(vidUrl)}" target="_blank" class="media-open">↗ 打开原视频</a></div>`;
      } else {
        mediaBody = `<div class="media-card"><div class="media-tag">🎨 图片</div><a href="${escHtml(imgUrl)}" target="_blank"><img src="${escHtml(imgUrl)}" alt="生成的图片" style="max-width:480px;border-radius:8px;display:block;"/></a><a href="${escHtml(imgUrl)}" target="_blank" class="media-open">↗ 查看原图</a></div>`;
      }
      el.innerHTML = `<div class="msg-av">🦝</div><div class="msg-body">${srcHtml}<div class="msg-bbl">${mediaBody}</div></div>`;
      msgsEl.appendChild(el); msgsEl.scrollTop = msgsEl.scrollHeight;
    }
  } else if (ev.event === 'task_failed') {
    addMsg('assistant', `❌ 任务失败: ${ev.payload?.error || '未知'}`, source || 'skill', ev.skill_name || '');
  } else if (ev.event === 'task_progress') {
    addMsg('system', `⏳ ${ev.skill_name || '任务'}: ${ev.payload?.message || '处理中...'}`);
  } else if (ev.event === 'session_started') {
    addMsg('system', `🔄 ${ev.payload?.skill_name || 'Skill'} 流程开始 (${ev.payload?.total_steps || 0} 步)`);
  } else if (ev.event === 'session_step') {
    const stepSrc = source || 'skill';
    const stepName = ev.payload?.step_name || '';
    const stepType = ev.payload?.step_type || '';
    const stepSkillName = ev.skill_name || '';
    if (stepType === 'user_choice' && ev.payload?.options) {
      // 选项步骤已在主回复中展示
    } else if (ev.payload?.result) {
      addMsg('assistant', `**${stepName}**: ${ev.payload.result}`, stepSrc, stepSkillName);
    }
    // 远程媒体
    const imgUrl = ev.payload?.image_url, vidUrl = ev.payload?.video_url;
    if (imgUrl || vidUrl) {
      const el = document.createElement('div'); el.className = 'msg assistant';
      const srcHtml = renderSourceLabel(stepSrc, stepSkillName);
      let mediaBody = '';
      if (vidUrl) {
        mediaBody = `<div class="media-card"><div class="media-tag">🎬 视频</div><video controls style="max-width:480px;border-radius:8px;display:block;"><source src="${escHtml(vidUrl)}">你的浏览器不支持 video</video><a href="${escHtml(vidUrl)}" target="_blank" class="media-open">↗ 打开原视频</a></div>`;
      } else {
        mediaBody = `<div class="media-card"><div class="media-tag">🎨 图片</div><a href="${escHtml(imgUrl)}" target="_blank"><img src="${escHtml(imgUrl)}" alt="生成的图片" style="max-width:480px;border-radius:8px;display:block;"/></a><a href="${escHtml(imgUrl)}" target="_blank" class="media-open">↗ 查看原图</a></div>`;
      }
      el.innerHTML = `<div class="msg-av">🦝</div><div class="msg-body">${srcHtml}<div class="msg-bbl">${mediaBody}</div></div>`;
      msgsEl.appendChild(el); msgsEl.scrollTop = msgsEl.scrollHeight;
    }
  } else if (ev.event === 'session_ended') {
    const status = ev.payload?.status || '';
    if (status === 'completed') addMsg('system', '✅ 流程已完成');
    else if (status === 'cancelled') addMsg('system', '🚫 流程已取消');
  } else if (ev.event === 'skill_installing') {
    addMsg('system', `📥 正在安装技能: ${ev.payload?.name || ''}`);
  } else if (ev.event === 'skill_installed') {
    addMsg('system', `✅ 技能「${ev.payload?.name || ''}」安装成功 (v${ev.payload?.version || ''})`);
    loadSkills();
    loadMarketSkills();
  } else if (ev.event === 'skill_install_failed') {
    addMsg('system', `❌ 技能安装失败: ${ev.payload?.name || ''} - ${ev.payload?.error || ''}`);
    loadMarketSkills();
  }

  loadTasks();
}

function fmtSize(b) {
  if (b < 1024) return b + 'B';
  if (b < 1048576) return (b / 1024).toFixed(1) + 'KB';
  return (b / 1048576).toFixed(1) + 'MB';
}

// ─── Input ─────────────────────────────────────────────
function setupInput() {
  inputEl.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMsg(); }
  });
  inputEl.addEventListener('input', () => {
    inputEl.style.height = 'auto';
    inputEl.style.height = Math.min(inputEl.scrollHeight, 120) + 'px';
    $('charCount').textContent = inputEl.value.length + ' 字';
  });
  sendBtn.addEventListener('click', sendMsg);
}

// ─── Send (Streaming) ──────────────────────────────────
async function sendMsg() {
  if (paused) { addMsg('system', '⏸️ 对话已暂停，点击继续按钮恢复'); return; }
  const text = inputEl.value.trim();
  if (!text || sending) return;
  sending = true; sendBtn.disabled = true;
  if (welcomeEl) welcomeEl.style.display = 'none';
  addMsg('user', text);
  inputEl.value = ''; inputEl.style.height = 'auto'; $('charCount').textContent = '0 字';

  const bid = 'b' + Date.now();
  let full = '', lastSource = '', lastSkillName = '';
  createStreamBubble(bid);

  try {
    currentAbortController = new AbortController();
    const res = await fetch('/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, conversation_id: convId }),
      signal: currentAbortController.signal,
    });
    const reader = res.body.getReader(), dec = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split('\n'); buf = lines.pop() || '';
      for (const ln of lines) {
        if (!ln.startsWith('data: ')) continue;
        try {
          const d = JSON.parse(ln.slice(6));
          if (d.type === 'text') {
            full += d.content;
            if (d.source) lastSource = d.source;
            if (d.skill_name) lastSkillName = d.skill_name;
            updateBubble(bid, full, lastSource, lastSkillName);
            if (d.conversation_id && convId === null) { convId = d.conversation_id; reconnectSSE(); }
          } else if (d.type === 'done') {
            if (d.conversation_id && convId === null) { convId = d.conversation_id; reconnectSSE(); }
            if (d.source) lastSource = d.source;
            if (d.skill_name) lastSkillName = d.skill_name;
            const srcHtml = renderSourceLabel(lastSource, lastSkillName);
            if (srcHtml) {
              const el = document.getElementById(bid);
              if (el) {
                const existing = el.querySelector('.msg-src');
                if (existing) existing.remove();
                const srcEl = document.createElement('span');
                srcEl.outerHTML = srcHtml;
                el.querySelector('.msg-body')?.insertAdjacentHTML('afterbegin', srcHtml);
              }
            }
          } else if (d.type === 'error') {
            full += '\n\n⚠️ ' + d.content; updateBubble(bid, full);
          }
        } catch {}
      }
    }
    finalizeBubble(bid, full, lastSource, lastSkillName);
  } catch (err) {
    if (err.name === 'AbortError') {
      finalizeBubble(bid, full || '(已暂停)', lastSource, lastSkillName);
      addMsg('system', '⏸️ 对话已暂停');
    } else try {
      const r = await fetch('/message', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, conversation_id: convId }),
      });
      const d = await r.json(); if (convId === null) { convId = d.conversation_id; reconnectSSE(); } full = d.reply || '(无响应)';
      finalizeBubble(bid, full);
    } catch (e2) {
      finalizeBubble(bid, '⚠️ 请求失败: ' + err.message);
    }
  } finally { sending = false; sendBtn.disabled = false; currentAbortController = null; inputEl.focus(); }
}

function sendQuick(t) { inputEl.value = t; sendMsg(); }

// ─── Messages ──────────────────────────────────────────
const SOURCE_LABELS = {
  llm: { icon: '🧠', label: 'LLM', cls: 'src-llm' },
  skill: { icon: '⚙️', label: 'Skill', cls: 'src-skill' },
  skill_matched: { icon: '⚙️', label: 'Skill', cls: 'src-skill' },
  skill_session: { icon: '⚙️', label: 'Skill', cls: 'src-skill' },
  flow_llm: { icon: '🧠', label: '流程LLM', cls: 'src-flow-llm' },
  background: { icon: '🔄', label: '后台', cls: 'src-background' },
  system: { icon: '⚙️', label: '系统', cls: 'src-system' },
  learn_confirm: { icon: '📚', label: '学习', cls: 'src-system' },
};

function renderSourceLabel(source, skillName) {
  if (!source || !SOURCE_LABELS[source]) return '';
  const s = SOURCE_LABELS[source];
  const label = (source === 'skill' || source === 'skill_matched' || source === 'skill_session') && skillName
    ? `${s.icon} ${skillName}`
    : `${s.icon} ${s.label}`;
  return `<span class="msg-src ${s.cls}">${label}</span>`;
}

function addMsg(role, content, source, skillName) {
  const div = document.createElement('div'); div.className = 'msg ' + role;
  const icon = role === 'user' ? '👤' : role === 'assistant' ? '🦝' : '⚙️';
  const now = new Date(),
        time = now.getHours().toString().padStart(2, '0') + ':' + now.getMinutes().toString().padStart(2, '0');
  const body = role === 'assistant' ? renderMd(content) : escHtml(content);
  const srcHtml = renderSourceLabel(source, skillName);
  div.innerHTML = `<div class="msg-av">${icon}</div><div class="msg-body">${srcHtml}<div class="msg-bbl">${body}</div><div class="msg-time">${time}</div></div>`;
  msgsEl.appendChild(div); msgsEl.scrollTop = msgsEl.scrollHeight;
  history.push({ role, content, time, source, skillName });
}

function createStreamBubble(id) {
  const div = document.createElement('div'); div.className = 'msg assistant'; div.id = id;
  div.innerHTML = `<div class="msg-av">🦝</div><div class="msg-body"><div class="msg-bbl stream-cursor"></div></div>`;
  msgsEl.appendChild(div); msgsEl.scrollTop = msgsEl.scrollHeight;
}

function updateBubble(id, text, source, skillName) {
  const el = document.getElementById(id);
  if (!el) return;
  const bbl = el.querySelector('.msg-bbl');
  bbl.innerHTML = renderMd(text);
  bbl.classList.add('stream-cursor');
  // 更新 source label
  const srcHtml = renderSourceLabel(source, skillName);
  const existing = el.querySelector('.msg-src');
  if (srcHtml) {
    if (existing) existing.outerHTML = srcHtml;
    else el.querySelector('.msg-body')?.insertAdjacentHTML('afterbegin', srcHtml);
  } else if (existing) {
    existing.remove();
  }
  msgsEl.scrollTop = msgsEl.scrollHeight;
}

function finalizeBubble(id, text, source, skillName) {
  const el = document.getElementById(id);
  if (!el) return;
  const bbl = el.querySelector('.msg-bbl');
  bbl.classList.remove('stream-cursor');
  bbl.innerHTML = renderMd(text);
  bbl.querySelectorAll('pre').forEach(pre => {
    const code = pre.querySelector('code');
    const lang = (code?.className?.match(/language-(\w+)/) || [])[1] || '';
    const hdr = document.createElement('div'); hdr.className = 'code-hdr';
    hdr.innerHTML = `<span>${lang || 'code'}</span><button class="copy-btn" onclick="copyCode(this)">复制</button>`;
    pre.insertBefore(hdr, pre.firstChild);
  });
  const now = new Date(),
        time = now.getHours().toString().padStart(2, '0') + ':' + now.getMinutes().toString().padStart(2, '0');
  const timeEl = document.createElement('div'); timeEl.className = 'msg-time'; timeEl.textContent = time;
  el.querySelector('.msg-body').appendChild(timeEl);
  msgsEl.scrollTop = msgsEl.scrollHeight;
  history.push({ role: 'assistant', content: text, time, source, skillName });
}

function renderMd(text) { try { return marked.parse(text); } catch { return escHtml(text); } }
function escHtml(t) { const d = document.createElement('div'); d.textContent = t; return d.innerHTML; }

function copyCode(btn) {
  const code = btn.closest('pre').querySelector('code');
  navigator.clipboard.writeText(code.textContent).then(() => {
    btn.textContent = '已复制'; setTimeout(() => btn.textContent = '复制', 1500);
  });
}

function clearChat() {
  if (currentAbortController) currentAbortController.abort();
  msgsEl.innerHTML = ''; history.length = 0; convId = null; paused = false; sending = false;
  updatePauseBtn();
  if (welcomeEl) welcomeEl.style.display = '';
}

// ─── Pause / New Conversation ──────────────────────────
function togglePause() {
  paused = !paused;
  updatePauseBtn();
  if (paused && currentAbortController) {
    // 暂停时中断正在进行的流式请求
    currentAbortController.abort();
  }
  if (!paused) addMsg('system', '▶️ 对话已恢复');
}

function updatePauseBtn() {
  const btn = $('pauseBtn');
  if (btn) {
    btn.textContent = paused ? '▶️ 继续' : '⏸️ 暂停';
    btn.className = 'chat-act' + (paused ? ' paused' : '');
  }
}

function newConversation() {
  if (history.length) saveConversation();
  if (currentAbortController) currentAbortController.abort();
  msgsEl.innerHTML = ''; history.length = 0; convId = null; paused = false; sending = false;
  updatePauseBtn();
  reconnectSSE();
  if (welcomeEl) welcomeEl.style.display = '';
  highlightActiveConv();
  inputEl.focus();
}

// ─── Conversation Save/Load ────────────────────────────
async function saveConversation() {
  if (!convId || !history.length) return;
  try {
    await fetch('/conversations/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ conversation_id: convId, messages: history }),
    });
  } catch {}
}

async function loadSavedConversations() {
  try {
    const res = await fetch('/conversations'), convs = await res.json();
    updateConversationsPanel(convs);
  } catch {}
}

function updateConversationsPanel(convs) {
  const list = $('conv-list'), empty = $('conv-empty');
  if (!list) return;
  if (!convs || !convs.length) { if (empty) empty.style.display = ''; list.innerHTML = ''; return; }
  if (empty) empty.style.display = 'none';
  list.innerHTML = convs.map(c =>
    `<div class="conv-item${c.conversation_id === convId ? ' active' : ''}" onclick="loadConversation('${c.conversation_id}')"><div class="conv-preview">${escHtml(c.preview)}</div><div class="conv-meta">${c.message_count} 条消息 · ${c.last_time}</div><button class="conv-del" onclick="event.stopPropagation();deleteConversation('${c.conversation_id}')">🗑</button></div>`
  ).join('');
}

function highlightActiveConv() {
  document.querySelectorAll('.conv-item').forEach(el => {
    el.classList.toggle('active', el.onclick?.toString().includes(convId));
  });
}

async function loadConversation(id) {
  try {
    const res = await fetch(`/conversations/${id}`), data = await res.json();
    if (!data.messages || !data.messages.length) return;
    msgsEl.innerHTML = ''; history.length = 0; convId = id; paused = false;
    updatePauseBtn();
    reconnectSSE();  // 切换会话时重连 SSE
    if (welcomeEl) welcomeEl.style.display = 'none';
    data.messages.forEach(m => {
      const div = document.createElement('div'); div.className = 'msg ' + m.role;
      const icon = m.role === 'user' ? '👤' : m.role === 'assistant' ? '🦝' : '⚙️';
      const body = m.role === 'assistant' ? renderMd(m.content) : escHtml(m.content);
      div.innerHTML = `<div class="msg-av">${icon}</div><div class="msg-body"><div class="msg-bbl">${body}</div><div class="msg-time">${m.time || ''}</div></div>`;
      msgsEl.appendChild(div);
      history.push({ role: m.role, content: m.content, time: m.time || '' });
    });
    msgsEl.scrollTop = msgsEl.scrollHeight;
    highlightActiveConv();
  } catch (e) { console.error('load conv failed', e); }
}

async function deleteConversation(id) {
  try {
    await fetch(`/conversations/${id}`, { method: 'DELETE' });
    loadSavedConversations();
  } catch {}
}

// ─── File Upload ───────────────────────────────────────
function setupFileUpload() {
  const btn = $('attachBtn'), input = $('fileInput');
  if (!btn || !input) return;
  btn.addEventListener('click', () => input.click());
  input.addEventListener('change', async () => {
    const file = input.files[0];
    if (!file) return;
    if (welcomeEl) welcomeEl.style.display = 'none';
    addMsg('user', `📎 ${file.name} (${fmtSize(file.size)})`);
    const formData = new FormData();
    formData.append('file', file);
    try {
      const res = await fetch('/upload/file', { method: 'POST', body: formData });
      const data = await res.json();
      if (data.status === 'uploaded') {
        const el = document.createElement('div'); el.className = 'msg assistant';
        const isImg = /^image\//.test(file.type);
        let body = '';
        if (isImg)
          body = `<img src="${data.url}" alt="${escHtml(file.name)}" style="max-width:320px;max-height:240px;border-radius:8px;display:block;"/>`;
        else
          body = `<div class="file-dl-wrap"><span class="file-dl-name">📎 ${escHtml(file.name)}</span><span class="file-dl-size">${fmtSize(data.size)}</span><a href="${data.url}" download="${escHtml(file.name)}" class="file-dl-btn">↓ 下载</a></div>`;
        el.innerHTML = `<div class="msg-av">🦝</div><div class="msg-body"><div class="msg-bbl">${body}</div></div>`;
        msgsEl.appendChild(el); msgsEl.scrollTop = msgsEl.scrollHeight;
      }
    } catch (e) {
      addMsg('system', '⚠️ 文件上传失败: ' + e.message);
    }
    input.value = '';
  });
}

// ─── Sidebar (Collapsible Sections) ────────────────────
function setupSidebar() {
  $('sidebarToggle')?.addEventListener('click', () => $('sidebar').classList.toggle('open'));
}

function toggleSec(name) {
  const body = $('sec-' + name);
  const arrow = $('arrow-' + name);
  if (!body) return;
  body.classList.toggle('collapsed');
  if (arrow) {
    arrow.textContent = body.classList.contains('collapsed') ? '▸' : '▾';
  }
  // 切换展开时刷新数据
  if (!body.classList.contains('collapsed')) {
    if (name === 'schedules') loadSchedules();
    if (name === 'workflows') loadWorkflows();
    if (name === 'skills') loadSkills();
    if (name === 'tasks') loadTasks();
  }
}

// ─── Skills ────────────────────────────────────────────
async function loadSkills() {
  try {
    const res = await fetch('/skills'), skills = await res.json();
    const enabled = skills.filter(s => s.enabled);

    // 已启用列表
    const list = $('skills-list'), empty = $('skills-empty');
    if (!enabled.length) {
      empty.innerHTML = '<div class="empty-icon">📦</div><div>暂无已启用技能</div>';
      empty.style.display = ''; list.innerHTML = '';
    } else {
      empty.style.display = 'none';
      list.innerHTML = enabled.map(s => renderSkillCard(s, true)).join('');
    }
  } catch { $('skills-empty').innerHTML = '<div class="empty-icon">⚠️</div><div>加载失败</div>'; }
}

function renderSkillCard(s, isEnabled) {
  const toggleClass = isEnabled ? 'sk-toggle on' : 'sk-toggle';
  const toggleLabel = isEnabled ? '已启用' : '启用';
  const riskBadge = s.risk_level && s.risk_level !== 'low'
    ? `<span class="sk-risk ${s.risk_level}">${s.risk_level === 'medium' ? '⚠️' : '🔴'} ${s.risk_level}</span>` : '';
  const interBadge = s.interactive ? '<span class="sk-badge interactive">交互式</span>' : '';
  return `<div class="skill-card${isEnabled ? ' enabled' : ''}">
    <div class="sk-top">
      <div class="sk-name">${s.name}<span class="sk-ver">v${s.version}</span>${riskBadge}${interBadge}</div>
      <button class="${toggleClass}" onclick="event.stopPropagation();toggleSkill('${s.name}',${isEnabled})" title="${toggleLabel}"><span class="sk-toggle-dot"></span></button>
    </div>
    <div class="sk-desc">${s.description || '无描述'}</div>
    <div class="sk-triggers">${s.trigger_words.map(w => `<span class="ttag">${w}</span>`).join('')}</div>
  </div>`;
}

async function toggleSkill(name, currentEnabled) {
  try {
    const res = await fetch(`/skills/${name}/toggle`, { method: 'POST' });
    const data = await res.json();
    if (res.ok) {
      loadSkills(); // 刷新侧边栏列表
      // 如果商城弹窗开着，也刷新
      if (!$('marketModal').hasAttribute('hidden')) loadMarketSkills();
    }
  } catch {}
}

// ═════════════════════════════════════════════════════════
// ─── Skill Market Modal ──────────────────────────────────
// ═════════════════════════════════════════════════════════

let marketTab = 'not_installed';  // 'not_installed' | 'installed' | 'ranking'
let marketCat = 'all';       // 'all' | 'builtin' | 'essential' | ...
let marketSkillsCache = [];   // 缓存技能列表

// ─── 技能分类映射（基于 intent_tags + 名称规则）───
const SKILL_CATEGORIES = {
  builtin:    { label: '🔧 系统自带', match: s => ['echo'].includes(s.name) },
  essential:  { label: '⭐ 装机必装', match: s => ['file_read','file_search','shell_exec','web_browse'].includes(s.name) },
  efficiency: { label: '⚡ 效率',     match: s => ['clipboard','screenshot','system_info','change_detector','file_batch','file_analyze'].includes(s.name) },
  info:       { label: '📰 资讯',     match: s => s.intent_tags?.includes('learned') || ['36kr_hot','baidu_hot','bilibili_hot','weibo_hot','csdn_hot','juejin_hot','cnblogs_hot','ai_daily_report','xiaohongshu_daily_report'].includes(s.name) },
  creative:   { label: '🎨 创作',     match: s => s.intent_tags?.includes('image') || s.intent_tags?.includes('generation') || ['image_gen'].includes(s.name) },
  dev:        { label: '💻 开发',     match: s => ['git_helper','log_watcher'].includes(s.name) || s.intent_tags?.includes('system') },
  browser:    { label: '🌐 浏览器',   match: s => s.intent_tags?.includes('browser') || s.intent_tags?.includes('web') || s.intent_tags?.includes('automate') || ['web_automate','app_control'].includes(s.name) },
  learned:    { label: '📚 自学习',   match: s => s.intent_tags?.includes('learned') },
};

// 获取技能所属分类 key
function getSkillCategory(s) {
  for (const [key, cat] of Object.entries(SKILL_CATEGORIES)) {
    if (cat.match(s)) return key;
  }
  return 'efficiency'; // 默认归入效率
}

// 排行榜权重（使用次数模拟 + 人工权重）
const RANKING_WEIGHTS = {
  echo: 100, file_read: 95, shell_exec: 90, web_browse: 85,
  image_gen: 80, web_automate: 75, file_search: 70, clipboard: 65,
  screenshot: 60, git_helper: 55, file_batch: 50, system_info: 45,
  change_detector: 40, ai_daily_report: 38, '36kr_hot': 35,
  bilibili_hot: 32, baidu_hot: 30, weibo_hot: 28, log_watcher: 25,
  file_analyze: 22, app_control: 20, csdn_hot: 18, juejin_hot: 16,
  cnblogs_hot: 14, xiaohongshu_daily_report: 12,
};

function openMarket() {
  $('marketModal').removeAttribute('hidden');
  loadMarketSkills();
}

function closeMarket() {
  $('marketModal').setAttribute('hidden', '');
}

function switchMarketTab(tab) {
  marketTab = tab;
  document.querySelectorAll('.market-tab').forEach(el => {
    el.classList.toggle('active', el.dataset.tab === tab);
  });
  loadMarketSkills();
}

function switchMarketCat(cat) {
  marketCat = cat;
  document.querySelectorAll('.market-cat').forEach(el => {
    el.classList.toggle('active', el.dataset.cat === cat);
  });
  loadMarketSkills();
}

async function loadMarketSkills() {
  try {
    // 从市场 API 获取（包含远程索引 + 已安装状态）
    const res = await fetch('/market');
    const skills = await res.json();
    marketSkillsCache = skills.map(s => ({
      ...s,
      category: s.category || getSkillCategory(s),
      rankScore: RANKING_WEIGHTS[s.name] || 10,
    }));

    renderMarketContent();
  } catch {
    $('market-modal-empty').innerHTML = '<div class="empty-icon">⚠️</div><div>加载失败</div>';
    $('market-modal-empty').style.display = '';
    $('market-modal-list').innerHTML = '';
  }
}

function renderMarketContent() {
  let skills = [...marketSkillsCache];
  const list = $('market-modal-list');
  const empty = $('market-modal-empty');

  // 搜索过滤（仅匹配技能名称）
  const q = ($('market-search-input')?.value || '').toLowerCase();
  if (q) {
    skills = skills.filter(s => s.name.toLowerCase().includes(q));
  }

  // 分类过滤
  if (marketCat !== 'all') {
    skills = skills.filter(s => s.category === marketCat);
  }

  // Tab 过滤
  if (marketTab === 'not_installed') {
    skills = skills.filter(s => !s.installed);
  } else if (marketTab === 'installed') {
    skills = skills.filter(s => s.installed);
  }
  if (marketTab === 'ranking') {
    skills.sort((a, b) => b.rankScore - a.rankScore);
  }

  if (!skills.length) {
    empty.style.display = '';
    const msgs = {
      not_installed: '<div class="empty-icon">✅</div><div>所有技能均已安装</div>',
      installed:  '<div class="empty-icon">📦</div><div>该分类下暂无已安装技能</div>',
      ranking:  '<div class="empty-icon">🏆</div><div>暂无排行数据</div>',
    };
    empty.innerHTML = msgs[marketTab] || '<div class="empty-icon">📦</div><div>暂无技能</div>';
    list.innerHTML = '';
    return;
  }

  empty.style.display = 'none';

  if (marketTab === 'ranking') {
    list.innerHTML = skills.map((s, i) => renderMarketRankCard(s, i + 1)).join('');
  } else {
    list.innerHTML = skills.map(s => renderMarketSkillCard(s)).join('');
  }
}

function renderMarketSkillCard(s) {
  const cat = SKILL_CATEGORIES[s.category];
  const catLabel = cat ? cat.label.split(' ').pop() : s.category;
  const riskBadge = s.risk_level && s.risk_level !== 'low'
    ? `<span class="sk-risk ${s.risk_level}">${s.risk_level === 'medium' ? '⚠️' : '🔴'} ${s.risk_level}</span>` : '';
  const interBadge = s.interactive ? '<span class="sk-badge interactive">交互式</span>' : '';
  const starClass = s.starred ? 'sk-star on' : 'sk-star';

  // 操作按钮：已安装 → star + toggle + 卸载；未安装 → 安装
  let actionsHtml;
  if (s.installed) {
    const toggleClass = s.enabled ? 'sk-toggle on' : 'sk-toggle';
    actionsHtml = `
      <button class="${starClass}" onclick="event.stopPropagation();toggleStarInMarket('${s.name}',${!!s.starred})" title="${s.starred ? '取消收藏' : '收藏'}">★</button>
      <button class="${toggleClass}" onclick="event.stopPropagation();toggleSkillInMarket('${s.name}',${s.enabled})" title="${s.enabled ? '禁用' : '启用'}"><span class="sk-toggle-dot"></span></button>
      <button class="sk-uninstall" onclick="event.stopPropagation();uninstallSkillInMarket('${s.name}')" title="卸载">🗑</button>`;
  } else {
    actionsHtml = `<button class="sk-install" onclick="event.stopPropagation();installSkillFromMarket('${s.name}')" title="安装">📥 安装</button>`;
  }

  const versionBadge = s.installed && s.installed_version && s.installed_version !== s.version
    ? `<span class="sk-badge outdated">v${s.installed_version} → v${s.version}</span>`
    : '';

  return `<div class="market-skill-card${s.installed && s.enabled ? ' enabled' : ''}${s.installed ? ' installed' : ''}">
    <div class="market-skill-top">
      <div class="market-skill-name">${s.name}<span class="market-skill-ver">v${s.installed ? (s.installed_version || s.version) : s.version}</span><span class="market-skill-cat">${catLabel}</span>${riskBadge}${interBadge}${versionBadge}</div>
      <div class="market-skill-actions">${actionsHtml}</div>
    </div>
    <div class="market-skill-desc">${s.description || '无描述'}</div>
    <div class="market-skill-triggers">${(s.trigger_words || []).map(w => `<span class="ttag">${w}</span>`).join('')}</div>
  </div>`;
}

function renderMarketRankCard(s, rank) {
  const cat = SKILL_CATEGORIES[s.category];
  const catLabel = cat ? cat.label.split(' ').pop() : s.category;
  const rankCls = rank <= 3 ? `r${rank}` : 'rn';
  const riskBadge = s.risk_level && s.risk_level !== 'low'
    ? `<span class="sk-risk ${s.risk_level}">${s.risk_level === 'medium' ? '⚠️' : '🔴'} ${s.risk_level}</span>` : '';
  const starClass = s.starred ? 'sk-star on' : 'sk-star';

  let actionsHtml;
  if (s.installed) {
    const toggleClass = s.enabled ? 'sk-toggle on' : 'sk-toggle';
    actionsHtml = `
      <button class="${starClass}" onclick="event.stopPropagation();toggleStarInMarket('${s.name}',${!!s.starred})" title="${s.starred ? '取消收藏' : '收藏'}">★</button>
      <button class="${toggleClass}" onclick="event.stopPropagation();toggleSkillInMarket('${s.name}',${s.enabled})" title="${s.enabled ? '禁用' : '启用'}"><span class="sk-toggle-dot"></span></button>`;
  } else {
    actionsHtml = `<button class="sk-install" onclick="event.stopPropagation();installSkillFromMarket('${s.name}')" title="安装">📥 安装</button>`;
  }

  return `<div class="market-skill-card${s.installed && s.enabled ? ' enabled' : ''}${s.installed ? ' installed' : ''}">
    <div class="market-skill-top">
      <div class="market-skill-name">
        <span class="market-rank-badge ${rankCls}">${rank}</span>
        ${s.name}<span class="market-skill-ver">v${s.installed ? (s.installed_version || s.version) : s.version}</span><span class="market-skill-cat">${catLabel}</span>${riskBadge}
      </div>
      <div class="market-skill-actions">${actionsHtml}</div>
    </div>
    <div class="market-skill-desc">${s.description || '无描述'}</div>
    <div class="market-skill-triggers">
      ${(s.trigger_words || []).map(w => `<span class="ttag">${w}</span>`).join('')}
      <span class="market-rank-stat">🔥 热度 ${s.rankScore}</span>
    </div>
  </div>`;
}

async function toggleSkillInMarket(name, currentEnabled) {
  try {
    const res = await fetch(`/skills/${name}/toggle`, { method: 'POST' });
    if (res.ok) {
      loadSkills();        // 刷新侧边栏
      loadMarketSkills();  // 刷新商城弹窗
    }
  } catch {}
}

async function toggleStarInMarket(name, currentStarred) {
  try {
    const res = await fetch(`/skills/${name}/star`, { method: 'POST' });
    if (res.ok) {
      loadMarketSkills();  // 刷新商城弹窗
    }
  } catch {}
}

async function installSkillFromMarket(name) {
  try {
    // 立即在 UI 上显示安装中状态
    const btn = document.querySelector(`.sk-install[onclick*="${name}"]`);
    if (btn) { btn.disabled = true; btn.textContent = '⏳ 安装中...'; }
    const res = await fetch(`/market/${name}/install`, { method: 'POST' });
    const data = await res.json();
    if (data.status === 'installed' || data.status === 'already_installed') {
      loadSkills();        // 刷新侧边栏
      loadMarketSkills();  // 刷新商城弹窗
    } else {
      if (btn) { btn.disabled = false; btn.textContent = '📥 安装'; }
    }
  } catch (e) {
    const btn = document.querySelector(`.sk-install[onclick*="${name}"]`);
    if (btn) { btn.disabled = false; btn.textContent = '📥 安装'; }
  }
}

async function uninstallSkillInMarket(name) {
  if (!confirm(`确定卸载技能「${name}」？`)) return;
  try {
    const res = await fetch(`/skills/${name}`, { method: 'DELETE' });
    if (res.ok) {
      loadSkills();
      loadMarketSkills();
    }
  } catch {}
}

function filterMarketSkills() {
  renderMarketContent();
}

// ─── Tasks ─────────────────────────────────────────────
async function loadTasks() {
  try {
    const res = await fetch('/tasks'), tasks = await res.json();
    const list = $('tasks-list'), empty = $('tasks-empty');
    if (!tasks.length) { empty.style.display = ''; list.innerHTML = ''; return; }
    empty.style.display = 'none';
    list.innerHTML = tasks.map(t =>
      `<div class="task-card"><div class="task-hdr"><span class="task-sk">${t.skill_name}</span><span class="task-st ${t.status}">${statusLabel(t.status)}</span></div><div class="task-id">${t.task_id.substring(0, 12)}...</div>${['pending', 'running'].includes(t.status) ? `<button class="task-cancel" onclick="cancelTask('${t.task_id}')">取消</button>` : ''}</div>`
    ).join('');
  } catch {}
}

function statusLabel(s) {
  return { pending: '等待中', running: '运行中', success: '已完成', failed: '失败', cancelled: '已取消' }[s] || s;
}

async function cancelTask(id) {
  try { await fetch(`/tasks/${id}/cancel`, { method: 'POST' }); loadTasks(); } catch {}
}

// ─── LLM Status ────────────────────────────────────────
async function loadLLM() {
  try {
    const res = await fetch('/status'), data = await res.json(), llm = data.llm || {};
    const dot = $('llmDot'), prov = $('llmProv'), model = $('llmModel');
    const map = {
      ready: { cls: 'ready', text: '已连接' },
      mock: { cls: 'mock', text: 'Mock 模式' },
      no_api_key: { cls: 'no_key', text: '未配置 Key' },
    };
    const s = map[llm.status] || map.mock;
    dot.className = 'llm-dot ' + s.cls;

    // 尝试从模型列表获取 vendor 信息
    let vendorLabel = '';
    try {
      const modelsRes = await fetch('/settings/models');
      const models = await modelsRes.json();
      const active = models.find(m => m.active);
      if (active) {
        vendorLabel = active.vendor ? ` · ${active.vendor}` : '';
      }
    } catch {}

    prov.textContent = (llm.provider === 'spark' ? '讯飞 codeplan' : llm.provider) + vendorLabel + ' · ' + s.text;
    model.textContent = llm.model || '-';
    const hdr = $('chatStatus');
    if (hdr && llm.status === 'ready') {
      hdr.querySelector('.status-dot').style.background = 'var(--ok)';
      const sp = hdr.querySelector('span:last-child'); if (sp) sp.textContent = 'codeplan 在线';
    }
  } catch {}
}

// ═════════════════════════════════════════════════════════
// ─── Schedules (定时任务) ──────────────────────────────
// ═════════════════════════════════════════════════════════

async function loadSchedules() {
  try {
    const res = await fetch('/schedules'), schedules = await res.json();
    const list = $('schedules-list'), empty = $('schedules-empty');
    if (!schedules || !schedules.length) { if (empty) empty.style.display = ''; list.innerHTML = ''; return; }
    if (empty) empty.style.display = 'none';
    list.innerHTML = schedules.map(s => `
      <div class="sched-card${s.enabled ? '' : ' disabled'}">
        <div class="sched-hdr">
          <span class="sched-name">${escHtml(s.name)}</span>
          <span class="sched-badge ${s.enabled ? 'on' : 'off'}">${s.enabled ? '启用' : '禁用'}</span>
        </div>
        <div class="sched-cron">${escHtml(s.cron)}</div>
        <div class="sched-msg">${escHtml(s.message)}</div>
        <div class="sched-meta">
          ${s.last_run ? `上次: ${s.last_run.replace('T', ' ').substring(0, 16)}` : '未运行'}
          ${s.next_run ? ` · 下次: ${s.next_run.replace('T', ' ').substring(0, 16)}` : ''}
        </div>
        <div class="sched-acts">
          <button class="sched-btn" onclick="toggleSchedule('${s.schedule_id}')">${s.enabled ? '⏸ 禁用' : '▶ 启用'}</button>
          <button class="sched-btn danger" onclick="deleteSchedule('${s.schedule_id}')">🗑 删除</button>
        </div>
      </div>
    `).join('');
  } catch {}
}

function toggleSchedForm() {
  const formEl = $('sched-form');
  if (formEl.style.display === 'none') {
    formEl.style.display = '';
    formEl.innerHTML = `
      <div class="sched-form">
        <label>任务名称</label>
        <input type="text" id="sched-name" placeholder="例：每日简报">
        <label>Cron 表达式</label>
        <input type="text" id="sched-cron" placeholder="例：0 8 * * * (每天 8:00)">
        <label>执行消息</label>
        <textarea id="sched-msg" placeholder="例：帮我生成今日简报"></textarea>
        <div class="sched-form-acts">
          <button class="sched-btn" onclick="toggleSchedForm()">取消</button>
          <button class="sched-btn primary" onclick="createSchedule()">创建</button>
        </div>
      </div>
    `;
  } else {
    formEl.style.display = 'none';
    formEl.innerHTML = '';
  }
}

async function createSchedule() {
  const name = $('sched-name')?.value?.trim();
  const cron = $('sched-cron')?.value?.trim();
  const message = $('sched-msg')?.value?.trim();
  if (!name || !cron || !message) { addMsg('system', '⚠️ 请填写所有字段'); return; }
  try {
    const res = await fetch('/schedules', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, cron, message }),
    });
    if (!res.ok) {
      const err = await res.json();
      addMsg('system', `⚠️ 创建失败: ${err.detail || '未知错误'}`);
      return;
    }
    addMsg('system', `✅ 定时任务「${name}」已创建`);
    toggleSchedForm();
    loadSchedules();
  } catch (e) {
    addMsg('system', '⚠️ 创建失败: ' + e.message);
  }
}

async function toggleSchedule(id) {
  try {
    await fetch(`/schedules/${id}/toggle`, { method: 'POST' });
    loadSchedules();
  } catch {}
}

async function deleteSchedule(id) {
  if (!confirm('确定删除此定时任务？')) return;
  try {
    await fetch(`/schedules/${id}`, { method: 'DELETE' });
    loadSchedules();
  } catch {}
}

// ═════════════════════════════════════════════════════════
// ─── Workflows (工作流) ────────────────────────────────
// ═════════════════════════════════════════════════════════

async function loadWorkflows() {
  try {
    const res = await fetch('/workflows'), workflows = await res.json();
    const list = $('workflows-list'), empty = $('workflows-empty');
    if (!workflows || !workflows.length) { if (empty) empty.style.display = ''; list.innerHTML = ''; return; }
    if (empty) empty.style.display = 'none';
    list.innerHTML = workflows.map(w => `
      <div class="wf-card">
        <div class="wf-hdr">
          <span class="wf-name">${escHtml(w.name)}</span>
          <span class="wf-steps-badge">${w.steps_count} 步</span>
        </div>
        ${w.description ? `<div class="wf-desc">${escHtml(w.description)}</div>` : ''}
        <div class="wf-meta">创建于 ${w.created_at.replace('T', ' ').substring(0, 16)}</div>
        <div class="wf-acts">
          <button class="wf-btn primary" onclick="executeWorkflow('${w.workflow_id}')">▶ 执行</button>
          <button class="wf-btn danger" onclick="deleteWorkflow('${w.workflow_id}')">🗑 删除</button>
        </div>
      </div>
    `).join('');
  } catch {}
}

function toggleWfForm() {
  const formEl = $('wf-form');
  if (formEl.style.display === 'none') {
    formEl.style.display = '';
    formEl.innerHTML = `
      <div class="wf-form">
        <label>工作流名称</label>
        <input type="text" id="wf-name" placeholder="例：每日简报流程">
        <label>描述</label>
        <input type="text" id="wf-desc" placeholder="可选描述">
        <label>步骤 (JSON)</label>
        <textarea id="wf-steps" placeholder='[{"type":"skill","skill_name":"echo","params":{"text":"hello"}}]'></textarea>
        <div class="wf-form-acts">
          <button class="wf-btn" onclick="toggleWfForm()">取消</button>
          <button class="wf-btn primary" onclick="createWorkflow()">创建</button>
        </div>
      </div>
    `;
  } else {
    formEl.style.display = 'none';
    formEl.innerHTML = '';
  }
}

async function createWorkflow() {
  const name = $('wf-name')?.value?.trim();
  const description = $('wf-desc')?.value?.trim() || '';
  const stepsStr = $('wf-steps')?.value?.trim();
  if (!name || !stepsStr) { addMsg('system', '⚠️ 请填写名称和步骤'); return; }
  let steps;
  try { steps = JSON.parse(stepsStr); } catch { addMsg('system', '⚠️ 步骤 JSON 格式错误'); return; }
  try {
    const res = await fetch('/workflows', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, description, steps }),
    });
    if (!res.ok) {
      const err = await res.json();
      addMsg('system', `⚠️ 创建失败: ${err.detail || '未知错误'}`);
      return;
    }
    addMsg('system', `✅ 工作流「${name}」已创建`);
    toggleWfForm();
    loadWorkflows();
  } catch (e) {
    addMsg('system', '⚠️ 创建失败: ' + e.message);
  }
}

async function executeWorkflow(id) {
  try {
    const res = await fetch(`/workflows/${id}/execute`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    const data = await res.json();
    if (data.status === 'completed') {
      addMsg('system', `✅ 工作流执行完成`);
    } else if (data.status === 'failed') {
      addMsg('system', `❌ 工作流执行失败: ${data.error || '未知'}`);
    } else {
      addMsg('system', `🔄 工作流执行中 (${data.execution_id?.substring(0, 8)}...)`);
    }
  } catch (e) {
    addMsg('system', '⚠️ 执行失败: ' + e.message);
  }
}

async function deleteWorkflow(id) {
  if (!confirm('确定删除此工作流？')) return;
  try {
    await fetch(`/workflows/${id}`, { method: 'DELETE' });
    loadWorkflows();
  } catch {}
}

// ═════════════════════════════════════════════════════════
// ─── LLM Model Dropdown & Settings ────────────────────
// ═════════════════════════════════════════════════════════

let llmPresets = [];
let editingModelId = null;  // null = 新增模式, string = 编辑模式

// ─── Model Dropdown ──────────────────────────────────────

async function toggleModelDropdown(e) {
  e.stopPropagation();
  const dd = $('modelDropdown');
  if (!dd.hidden) { dd.hidden = true; return; }
  // 加载模型列表
  await refreshModelDropdown();
  dd.hidden = false;
  // 点击外部关闭
  setTimeout(() => document.addEventListener('click', closeModelDropdown, { once: true }), 0);
}

function closeModelDropdown() {
  $('modelDropdown').hidden = true;
}

async function refreshModelDropdown() {
  const list = $('modelDropdownList');
  try {
    const res = await fetch('/settings/models');
    const models = await res.json();
    if (!models.length) {
      list.innerHTML = '<div class="model-dropdown-empty">暂无配置的模型</div>';
      return;
    }
    list.innerHTML = models.map(m => `
      <div class="model-item${m.active ? ' active' : ''}" data-id="${escHtml(m.id)}">
        <div class="model-item-info" onclick="event.stopPropagation();activateModel('${escHtml(m.id)}')">
          <span class="model-item-name">${escHtml(m.name || m.id)}</span>
          <span class="model-item-vendor">${escHtml(m.vendor || '')}</span>
        </div>
        <button class="model-item-edit" onclick="event.stopPropagation();openLLMSettings('${escHtml(m.id)}')" title="编辑">✏️</button>
        ${m.active ? '<span class="model-item-active-badge">当前</span>' : ''}
      </div>
    `).join('');
  } catch {
    list.innerHTML = '<div class="model-dropdown-empty">加载失败</div>';
  }
}

async function activateModel(modelId) {
  try {
    const res = await fetch(`/settings/models/${modelId}/activate`, { method: 'POST' });
    const data = await res.json();
    if (data.status === 'activated') {
      addMsg('system', `✅ 已切换到模型: ${data.model}`);
      closeModelDropdown();
      loadLLM();
    }
  } catch (e) {
    addMsg('system', '⚠️ 切换模型失败: ' + e.message);
  }
}

// ─── LLM Settings Modal (新增/编辑模型) ──────────────────

async function openLLMSettings(modelId) {
  closeModelDropdown();
  editingModelId = modelId;
  const modal = $('llmModal');
  modal.removeAttribute('hidden');

  // 更新标题
  const hdrTitle = modal.querySelector('.modal-hdr span');
  hdrTitle.textContent = modelId ? '✏️ 编辑模型' : '➕ 新增模型';

  // 加载预设
  try {
    const res = await fetch('/settings/llm/presets');
    llmPresets = await res.json();
    const sel = $('llm-preset');
    sel.innerHTML = '<option value="">-- 选择预设 --</option>' +
      llmPresets.map(p => `<option value="${p.id}">${p.name} — ${p.description}</option>`).join('');
  } catch {}

  if (modelId) {
    // 编辑模式：加载模型数据
    try {
      const res = await fetch(`/settings/models/${modelId}`);
      const m = await res.json();
      $('llm-apikey').value = m.apiKey || '';
      $('llm-apikey').placeholder = m.apiKey ? '已设置 (输入新值覆盖)' : '输入 API Key';
      $('llm-model').value = m.name || '';
      $('llm-baseurl').value = m.url || '';
      $('llm-maxtok').value = m.maxOutputTokens || 8192;
      // 额外字段
      $('llm-model-id').value = m.id || '';
      $('llm-vendor').value = m.vendor || 'Custom';
      $('llm-max-input').value = m.maxInputTokens || '';
      $('llm-supports-toolcall').checked = m.supportsToolCall || false;
      $('llm-supports-images').checked = m.supportsImages || false;
      $('llm-supports-reasoning').checked = m.supportsReasoning || false;
    } catch {}
    // 显示删除按钮
    $('llm-delete-btn').style.display = '';
  } else {
    // 新增模式：清空表单
    $('llm-apikey').value = '';
    $('llm-apikey').placeholder = '输入 API Key';
    $('llm-model').value = '';
    $('llm-baseurl').value = '';
    $('llm-temp').value = 0.7;
    $('llm-maxtok').value = 8192;
    $('llm-model-id').value = '';
    $('llm-vendor').value = 'Custom';
    $('llm-max-input').value = '';
    $('llm-supports-toolcall').checked = false;
    $('llm-supports-images').checked = false;
    $('llm-supports-reasoning').checked = false;
    // 隐藏删除按钮
    $('llm-delete-btn').style.display = 'none';
  }
  // 清空测试结果
  const tr = $('llm-test-result');
  tr.style.display = 'none'; tr.className = '';
}

function closeLLMSettings() {
  $('llmModal').setAttribute('hidden', '');
  editingModelId = null;
}

function applyPreset() {
  const id = $('llm-preset').value;
  const preset = llmPresets.find(p => p.id === id);
  if (!preset) return;
  $('llm-model').value = preset.model || '';
  $('llm-baseurl').value = preset.base_url || '';
  $('llm-vendor').value = preset.name || 'Custom';
  // 根据 provider 设置 vendor
  if (preset.provider === 'spark') $('llm-vendor').value = 'Spark';
  else if (preset.provider === 'openai') $('llm-vendor').value = 'OpenAI';
}

function toggleKeyVis() {
  const inp = $('llm-apikey');
  inp.type = inp.type === 'password' ? 'text' : 'password';
}

// 从 vendor 推断 provider
function vendorToProvider(vendor) {
  const v = (vendor || '').toLowerCase();
  if (v === 'spark' || v.includes('星火')) return 'spark';
  if (v === 'mock') return 'mock';
  return 'openai';
}

async function testLLM() {
  const tr = $('llm-test-result');
  tr.style.display = ''; tr.className = ''; tr.textContent = '⏳ 测试连接中...';
  try {
    const res = await fetch('/settings/llm/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        provider: vendorToProvider($('llm-vendor').value),
        api_key: $('llm-apikey').value || undefined,
        model: $('llm-model').value,
        base_url: $('llm-baseurl').value,
      }),
    });
    const data = await res.json();
    if (data.status === 'ok') {
      tr.className = 'ok';
      tr.textContent = `✅ 连接成功！回复: ${data.reply} (Provider: ${data.provider}, Model: ${data.model})`;
    } else {
      tr.className = 'err';
      tr.textContent = `❌ 连接失败: ${data.error}`;
    }
  } catch (e) {
    tr.className = 'err';
    tr.textContent = `❌ 请求失败: ${e.message}`;
  }
}

async function saveLLMSettings() {
  const modelData = {
    name: $('llm-model').value.trim(),
    vendor: $('llm-vendor').value.trim() || 'Custom',
    url: $('llm-baseurl').value.trim(),
    apiKey: $('llm-apikey').value || undefined,
    maxInputTokens: parseInt($('llm-max-input').value) || 0,
    maxOutputTokens: parseInt($('llm-maxtok').value) || 8192,
    supportsToolCall: $('llm-supports-toolcall').checked,
    supportsImages: $('llm-supports-images').checked,
    supportsReasoning: $('llm-supports-reasoning').checked,
  };
  // apiKey 含 *** 则跳过
  if (modelData.apiKey && modelData.apiKey.includes('***')) {
    delete modelData.apiKey;
  }

  try {
    if (editingModelId) {
      // 编辑模式
      const res = await fetch(`/settings/models/${editingModelId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(modelData),
      });
      const data = await res.json();
      if (data.status === 'updated') {
        addMsg('system', `✅ 模型「${modelData.name}」已更新`);
        closeLLMSettings();
        loadLLM();
      }
    } else {
      // 新增模式
      modelData.id = modelData.name;  // 用模型名作为默认 ID
      const res = await fetch('/settings/models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(modelData),
      });
      const data = await res.json();
      if (data.status === 'added') {
        addMsg('system', `✅ 模型「${modelData.name}」已添加`);
        closeLLMSettings();
        loadLLM();
      }
    }
  } catch (e) {
    addMsg('system', '⚠️ 保存失败: ' + e.message);
  }
}

async function deleteModelFromSettings() {
  if (!editingModelId) return;
  if (!confirm('确定删除此模型配置？')) return;
  try {
    await fetch(`/settings/models/${editingModelId}`, { method: 'DELETE' });
    addMsg('system', '✅ 模型已删除');
    closeLLMSettings();
    loadLLM();
  } catch (e) {
    addMsg('system', '⚠️ 删除失败: ' + e.message);
  }
}
