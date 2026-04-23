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
const history = [];
let autoSaveTimer = null;

// ─── DOM ───────────────────────────────────────────────
const $ = id => document.getElementById(id);
const msgsEl = $('messages'), inputEl = $('input'),
      sendBtn = $('sendBtn'), welcomeEl = $('welcome');

// ─── Init ──────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  loadSkills();
  loadTasks();
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
function setupSSE() {
  const es = new EventSource('/events');
  es.onmessage = e => { try { handleSSE(JSON.parse(e.data)); } catch {} };
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
    const res = await fetch('/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, conversation_id: convId }),
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
            if (d.conversation_id) convId = d.conversation_id;
          } else if (d.type === 'done') {
            if (d.conversation_id) convId = d.conversation_id;
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
    try {
      const r = await fetch('/message', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, conversation_id: convId }),
      });
      const d = await r.json(); convId = d.conversation_id; full = d.reply || '(无响应)';
      finalizeBubble(bid, full);
    } catch (e2) {
      finalizeBubble(bid, '⚠️ 请求失败: ' + err.message);
    }
  } finally { sending = false; sendBtn.disabled = false; inputEl.focus(); }
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
  msgsEl.innerHTML = ''; history.length = 0; convId = null; paused = false;
  updatePauseBtn();
  if (welcomeEl) welcomeEl.style.display = '';
}

// ─── Pause / New Conversation ──────────────────────────
function togglePause() {
  paused = !paused;
  updatePauseBtn();
  addMsg('system', paused ? '⏸️ 对话已暂停' : '▶️ 对话已恢复');
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
  msgsEl.innerHTML = ''; history.length = 0; convId = null; paused = false;
  updatePauseBtn();
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
    const list = $('skills-list'), empty = $('skills-empty');
    if (!skills.length) {
      empty.innerHTML = '<div class="empty-icon">📦</div><div>暂无已安装技能</div>';
      empty.style.display = ''; list.innerHTML = ''; return;
    }
    empty.style.display = 'none';
    list.innerHTML = skills.map(s =>
      `<div class="skill-card" onclick="sendQuick('${s.trigger_words[0] || s.name}')"><div class="sk-name">${s.name}<span class="sk-ver">v${s.version}</span></div><div class="sk-desc">${s.description || '无描述'}</div><div class="sk-triggers">${s.trigger_words.map(w => `<span class="ttag">${w}</span>`).join('')}</div></div>`
    ).join('');
  } catch { $('skills-empty').innerHTML = '<div class="empty-icon">⚠️</div><div>加载失败</div>'; }
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
    prov.textContent = (llm.provider === 'spark' ? '讯飞 codeplan' : llm.provider) + ' · ' + s.text;
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
