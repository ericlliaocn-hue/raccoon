// ─── Marked Config ──────────────────────────
marked.setOptions({
  highlight:(code,lang)=>{if(lang&&hljs.getLanguage(lang))return hljs.highlight(code,{language:lang}).value;return hljs.highlightAuto(code).value},
  breaks:true,gfm:true
});

// ─── State ──────────────────────────────────
let convId=null,sending=false,paused=false;
const history=[];
let autoSaveTimer=null;

// ─── DOM ────────────────────────────────────
const $=id=>document.getElementById(id);
const msgsEl=$('messages'),inputEl=$('input'),sendBtn=$('sendBtn'),welcomeEl=$('welcome');

// ─── Init ───────────────────────────────────
document.addEventListener('DOMContentLoaded',()=>{
  loadSkills();loadTasks();loadLLM();loadSavedConversations();
  setInterval(loadTasks,5000);
  setInterval(()=>{if(history.length)saveConversation()},30000); // auto-save every 30s
  setupSSE();setupInput();setupSidebar();setupFileUpload();
});

// ─── SSE ────────────────────────────────────
function setupSSE(){
  const es=new EventSource('/events');
  es.onmessage=e=>{try{handleSSE(JSON.parse(e.data))}catch{}};
}
function handleSSE(ev){
  if(paused)return; // 暂停时不处理新消息
  const source=ev.payload?.source||'';
  if(ev.event==='task_completed'&&ev.payload){
    const result=ev.payload;
    let text=result.reply||result.text||result.output||'';
    if(typeof result==='string')text=result;
    const src=source||'skill';
    addMsg('assistant',text||'✅ 任务完成',src);
    const files=result._files||result.files||[];
    files.forEach(f=>{
      const el=document.createElement('div');el.className='msg assistant';
      const isImg=/^image\//.test(f.mime),isVid=/^video\//.test(f.mime),isAud=/^audio\//.test(f.mime);
      let body='';
      if(isImg)body=`<a href="/files/${ev.task_id}/${encodeURIComponent(f.name)}" target="_blank"><img src="/files/${ev.task_id}/${encodeURIComponent(f.name)}" alt="${escHtml(f.name)}" style="max-width:320px;max-height:240px;border-radius:8px;margin-top:8px;display:block;"/></a>`;
      else if(isVid)body=`<video controls style="max-width:400px;border-radius:8px;margin-top:8px;display:block;"><source src="/files/${ev.task_id}/${encodeURIComponent(f.name)}" type="${f.mime}">你的浏览器不支持 video</video>`;
      else if(isAud)body=`<audio controls style="width:280px;margin-top:8px;display:block;"><source src="/files/${ev.task_id}/${encodeURIComponent(f.name)}" type="${f.mime}">你的浏览器不支持 audio</audio>`;
      else body=`<div class="file-dl-wrap"><span class="file-dl-name">📎 ${escHtml(f.name)}</span><span class="file-dl-size">${fmtSize(f.size)}</span><a href="/files/${ev.task_id}/${encodeURIComponent(f.name)}" download="${escHtml(f.name)}" class="file-dl-btn">↓ 下载</a></div>`;
      // Source label for file messages
      let srcHtml='';
      if(src&&SOURCE_LABELS[src]){const s=SOURCE_LABELS[src];srcHtml=`<span class="msg-src ${s.cls}">${s.icon} ${s.label}</span>`}
      el.innerHTML=`<div class="msg-av">🦝</div><div class="msg-body">${srcHtml}<div class="msg-bbl">${body}</div></div>`;
      msgsEl.appendChild(el);msgsEl.scrollTop=msgsEl.scrollHeight;
    });
    // 展示远程媒体（图片/视频）
    const imgUrl=result.image_url;
    const vidUrl=result.video_url;
    if(imgUrl||vidUrl){
      const el=document.createElement('div');el.className='msg assistant';
      let srcHtml='';
      if(src&&SOURCE_LABELS[src]){const s=SOURCE_LABELS[src];srcHtml=`<span class="msg-src ${s.cls}">${s.icon} ${s.label}</span>`}
      let mediaBody='';
      if(vidUrl){
        mediaBody=`<div class="media-card"><div class="media-tag">🎬 视频</div><video controls style="max-width:480px;border-radius:8px;display:block;"><source src="${escHtml(vidUrl)}">你的浏览器不支持 video</video><a href="${escHtml(vidUrl)}" target="_blank" class="media-open">↗ 打开原视频</a></div>`;
      }else{
        mediaBody=`<div class="media-card"><div class="media-tag">🎨 图片</div><a href="${escHtml(imgUrl)}" target="_blank"><img src="${escHtml(imgUrl)}" alt="生成的图片" style="max-width:480px;border-radius:8px;display:block;"/></a><a href="${escHtml(imgUrl)}" target="_blank" class="media-open">↗ 查看原图</a></div>`;
      }
      el.innerHTML=`<div class="msg-av">🦝</div><div class="msg-body">${srcHtml}<div class="msg-bbl">${mediaBody}</div></div>`;
      msgsEl.appendChild(el);msgsEl.scrollTop=msgsEl.scrollHeight;
    }
  }else if(ev.event==='task_failed'){
    addMsg('assistant',`❌ 任务失败: ${ev.payload?.error||'未知'}`,source||'skill');
  }else if(ev.event==='task_progress'){
    addMsg('system',`⏳ ${ev.skill_name||'任务'}: ${ev.payload?.message||'处理中...'}`);
  }else if(ev.event==='session_started'){
    addMsg('system',`🔄 ${ev.payload?.skill_name||'Skill'} 流程开始 (${ev.payload?.total_steps||0} 步)`);
  }else if(ev.event==='session_step'){
    const stepSrc=source||'skill';
    const stepName=ev.payload?.step_name||'';
    const stepType=ev.payload?.step_type||'';
    if(stepType==='user_choice'&&ev.payload?.options){
      // 选项步骤已经在主回复中展示，这里不重复
    }else if(ev.payload?.result){
      addMsg('assistant',`**${stepName}**: ${ev.payload.result}`,stepSrc);
    }
    // 展示远程媒体（图片/视频）
    const imgUrl=ev.payload?.image_url;
    const vidUrl=ev.payload?.video_url;
    if(imgUrl||vidUrl){
      const el=document.createElement('div');el.className='msg assistant';
      let srcHtml='';
      if(stepSrc&&SOURCE_LABELS[stepSrc]){const s=SOURCE_LABELS[stepSrc];srcHtml=`<span class="msg-src ${s.cls}">${s.icon} ${s.label}</span>`}
      let mediaBody='';
      if(vidUrl){
        mediaBody=`<div class="media-card"><div class="media-tag">🎬 视频</div><video controls style="max-width:480px;border-radius:8px;display:block;"><source src="${escHtml(vidUrl)}">你的浏览器不支持 video</video><a href="${escHtml(vidUrl)}" target="_blank" class="media-open">↗ 打开原视频</a></div>`;
      }else{
        mediaBody=`<div class="media-card"><div class="media-tag">🎨 图片</div><a href="${escHtml(imgUrl)}" target="_blank"><img src="${escHtml(imgUrl)}" alt="生成的图片" style="max-width:480px;border-radius:8px;display:block;"/></a><a href="${escHtml(imgUrl)}" target="_blank" class="media-open">↗ 查看原图</a></div>`;
      }
      el.innerHTML=`<div class="msg-av">🦝</div><div class="msg-body">${srcHtml}<div class="msg-bbl">${mediaBody}</div></div>`;
      msgsEl.appendChild(el);msgsEl.scrollTop=msgsEl.scrollHeight;
    }
  }else if(ev.event==='session_ended'){
    const status=ev.payload?.status||'';
    if(status==='completed'){
      addMsg('system','✅ 流程已完成');
    }else if(status==='cancelled'){
      addMsg('system','🚫 流程已取消');
    }
  }
  loadTasks();
}

function fmtSize(b){if(b<1024)return b+'B';if(b<1048576)return(b/1024).toFixed(1)+'KB';return(b/1048576).toFixed(1)+'MB'}

// ─── Input ──────────────────────────────────
function setupInput(){
  inputEl.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendMsg()}});
  inputEl.addEventListener('input',()=>{
    inputEl.style.height='auto';
    inputEl.style.height=Math.min(inputEl.scrollHeight,120)+'px';
    $('charCount').textContent=inputEl.value.length+' 字';
  });
  sendBtn.addEventListener('click',sendMsg);
}

// ─── Send (Streaming) ───────────────────────
async function sendMsg(){
  if(paused){addMsg('system','⏸️ 对话已暂停，点击继续按钮恢复');return}
  const text=inputEl.value.trim();
  if(!text||sending)return;
  sending=true;sendBtn.disabled=true;
  if(welcomeEl)welcomeEl.style.display='none';
  addMsg('user',text);
  inputEl.value='';inputEl.style.height='auto';$('charCount').textContent='0 字';

  const bid='b'+Date.now();
  let full='';
  createStreamBubble(bid);

  try{
    const res=await fetch('/chat/stream',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text,conversation_id:convId})
    });
    const reader=res.body.getReader(),dec=new TextDecoder();
    let buf='';
    while(true){
      const{done,value}=await reader.read();
      if(done)break;
      buf+=dec.decode(value,{stream:true});
      const lines=buf.split('\n');buf=lines.pop()||'';
        for(const ln of lines){
        if(!ln.startsWith('data: '))continue;
        try{
          const d=JSON.parse(ln.slice(6));
          if(d.type==='text'){full+=d.content;updateBubble(bid,full,d.source);if(d.conversation_id)convId=d.conversation_id}
          else if(d.type==='done'){
            if(d.conversation_id)convId=d.conversation_id;
            // Add source label to finalized bubble
            if(d.source&&SOURCE_LABELS[d.source]){
              const el=document.getElementById(bid);
              if(el){
                const s=SOURCE_LABELS[d.source];
                const srcEl=document.createElement('span');
                srcEl.className='msg-src '+s.cls;
                srcEl.textContent=s.icon+' '+s.label;
                el.querySelector('.msg-body')?.insertBefore(srcEl,el.querySelector('.msg-bbl'));
              }
            }
          }
          else if(d.type==='error'){full+='\n\n⚠️ '+d.content;updateBubble(bid,full)}
        }catch{}
      }
    }
    finalizeBubble(bid,full);
  }catch(err){
    try{
      const r=await fetch('/message',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text,conversation_id:convId})});
      const d=await r.json();convId=d.conversation_id;full=d.reply||'(无响应)';
      finalizeBubble(bid,full);
    }catch(e2){
      finalizeBubble(bid,'⚠️ 请求失败: '+err.message);
    }
  }finally{sending=false;sendBtn.disabled=false;inputEl.focus()}
}

function sendQuick(t){inputEl.value=t;sendMsg()}

// ─── Messages ───────────────────────────────
const SOURCE_LABELS = {
  llm: {icon:'🧠', label:'LLM', cls:'src-llm'},
  skill: {icon:'⚙️', label:'Skill', cls:'src-skill'},
  flow_llm: {icon:'🧠', label:'流程LLM', cls:'src-flow-llm'},
  background: {icon:'🔄', label:'后台', cls:'src-background'},
  system: {icon:'⚙️', label:'系统', cls:'src-system'}
};

function addMsg(role,content,source){
  const div=document.createElement('div');div.className='msg '+role;
  const icon=role==='user'?'👤':role==='assistant'?'🦝':'⚙️';
  const now=new Date(),time=now.getHours().toString().padStart(2,'0')+':'+now.getMinutes().toString().padStart(2,'0');
  const body=role==='assistant'?renderMd(content):escHtml(content);
  // Source label
  let srcHtml='';
  if(source&&SOURCE_LABELS[source]){
    const s=SOURCE_LABELS[source];
    srcHtml=`<span class="msg-src ${s.cls}">${s.icon} ${s.label}</span>`;
  }
  div.innerHTML=`<div class="msg-av">${icon}</div><div class="msg-body">${srcHtml}<div class="msg-bbl">${body}</div><div class="msg-time">${time}</div></div>`;
  msgsEl.appendChild(div);msgsEl.scrollTop=msgsEl.scrollHeight;
  history.push({role,content,time,source});
  updateHistoryPanel();
}

function createStreamBubble(id){
  const div=document.createElement('div');div.className='msg assistant';div.id=id;
  div.innerHTML=`<div class="msg-av">🦝</div><div class="msg-body"><div class="msg-bbl stream-cursor"></div></div>`;
  msgsEl.appendChild(div);msgsEl.scrollTop=msgsEl.scrollHeight;
}

function updateBubble(id,text){
  const el=document.getElementById(id);
  if(!el)return;
  const bbl=el.querySelector('.msg-bbl');
  bbl.innerHTML=renderMd(text);
  bbl.classList.add('stream-cursor');
  msgsEl.scrollTop=msgsEl.scrollHeight;
}

function finalizeBubble(id,text){
  const el=document.getElementById(id);
  if(!el)return;
  const bbl=el.querySelector('.msg-bbl');
  bbl.classList.remove('stream-cursor');
  bbl.innerHTML=renderMd(text);
  bbl.querySelectorAll('pre').forEach(pre=>{
    const code=pre.querySelector('code');
    const lang=(code?.className?.match(/language-(\w+)/)||[])[1]||'';
    const hdr=document.createElement('div');hdr.className='code-hdr';
    hdr.innerHTML=`<span>${lang||'code'}</span><button class="copy-btn" onclick="copyCode(this)">复制</button>`;
    pre.insertBefore(hdr,pre.firstChild);
  });
  const now=new Date(),time=now.getHours().toString().padStart(2,'0')+':'+now.getMinutes().toString().padStart(2,'0');
  const timeEl=document.createElement('div');timeEl.className='msg-time';timeEl.textContent=time;
  el.querySelector('.msg-body').appendChild(timeEl);
  msgsEl.scrollTop=msgsEl.scrollHeight;
  history.push({role:'assistant',content:text,time});
  updateHistoryPanel();
}

function renderMd(text){try{return marked.parse(text)}catch{return escHtml(text)}}
function escHtml(t){const d=document.createElement('div');d.textContent=t;return d.innerHTML}
function copyCode(btn){
  const code=btn.closest('pre').querySelector('code');
  navigator.clipboard.writeText(code.textContent).then(()=>{btn.textContent='已复制';setTimeout(()=>btn.textContent='复制',1500)});
}

function clearChat(){
  msgsEl.innerHTML='';history.length=0;convId=null;paused=false;
  updatePauseBtn();
  if(welcomeEl)welcomeEl.style.display='';
  updateHistoryPanel();
}

// ─── Pause / New Conversation ───────────────
function togglePause(){
  paused=!paused;
  updatePauseBtn();
  addMsg('system',paused?'⏸️ 对话已暂停':'▶️ 对话已恢复');
}

function updatePauseBtn(){
  const btn=$('pauseBtn');
  if(btn){
    btn.textContent=paused?'▶️ 继续':'⏸️ 暂停';
    btn.className='chat-act'+(paused?' paused':'');
  }
}

function newConversation(){
  // 先保存当前会话
  if(history.length)saveConversation();
  // 清空开始新会话
  msgsEl.innerHTML='';history.length=0;convId=null;paused=false;
  updatePauseBtn();
  if(welcomeEl)welcomeEl.style.display='';
  updateHistoryPanel();
  inputEl.focus();
}

// ─── Conversation Save/Load ─────────────────
async function saveConversation(){
  if(!convId||!history.length)return;
  try{
    await fetch('/conversations/save',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({conversation_id:convId,messages:history})
    });
  }catch{}
}

async function loadSavedConversations(){
  try{
    const res=await fetch('/conversations'),convs=await res.json();
    updateConversationsPanel(convs);
  }catch{}
}

function updateConversationsPanel(convs){
  const list=$('conv-list'),empty=$('conv-empty');
  if(!list)return;
  if(!convs||!convs.length){if(empty)empty.style.display='';list.innerHTML='';return}
  if(empty)empty.style.display='none';
  list.innerHTML=convs.map(c=>`<div class="conv-item" onclick="loadConversation('${c.conversation_id}')"><div class="conv-preview">${escHtml(c.preview)}</div><div class="conv-meta">${c.message_count} 条消息 · ${c.last_time}</div><button class="conv-del" onclick="event.stopPropagation();deleteConversation('${c.conversation_id}')">🗑</button></div>`).join('');
}

async function loadConversation(id){
  try{
    const res=await fetch(`/conversations/${id}`),data=await res.json();
    if(!data.messages||!data.messages.length)return;
    msgsEl.innerHTML='';history.length=0;convId=id;paused=false;
    updatePauseBtn();
    if(welcomeEl)welcomeEl.style.display='none';
    data.messages.forEach(m=>{
      const div=document.createElement('div');div.className='msg '+m.role;
      const icon=m.role==='user'?'👤':m.role==='assistant'?'🦝':'⚙️';
      const body=m.role==='assistant'?renderMd(m.content):escHtml(m.content);
      div.innerHTML=`<div class="msg-av">${icon}</div><div class="msg-body"><div class="msg-bbl">${body}</div><div class="msg-time">${m.time||''}</div></div>`;
      msgsEl.appendChild(div);
      history.push({role:m.role,content:m.content,time:m.time||''});
    });
    msgsEl.scrollTop=msgsEl.scrollHeight;
    updateHistoryPanel();
  }catch(e){console.error('load conv failed',e)}
}

async function deleteConversation(id){
  try{
    await fetch(`/conversations/${id}`,{method:'DELETE'});
    loadSavedConversations();
  }catch{}
}

// ─── File Upload ────────────────────────────
function setupFileUpload(){
  const btn=$('attachBtn'),input=$('fileInput');
  if(!btn||!input)return;
  btn.addEventListener('click',()=>input.click());
  input.addEventListener('change',async()=>{
    const file=input.files[0];
    if(!file)return;
    // Show preview in chat
    if(welcomeEl)welcomeEl.style.display='none';
    addMsg('user',`📎 ${file.name} (${fmtSize(file.size)})`);
    // Upload
    const formData=new FormData();
    formData.append('file',file);
    try{
      const res=await fetch('/upload/file',{method:'POST',body:formData});
      const data=await res.json();
      if(data.status==='uploaded'){
        // Show uploaded file as assistant message
        const el=document.createElement('div');el.className='msg assistant';
        const isImg=/^image\//.test(file.type);
        let body='';
        if(isImg)body=`<img src="${data.url}" alt="${escHtml(file.name)}" style="max-width:320px;max-height:240px;border-radius:8px;display:block;"/>`;
        else body=`<div class="file-dl-wrap"><span class="file-dl-name">📎 ${escHtml(file.name)}</span><span class="file-dl-size">${fmtSize(data.size)}</span><a href="${data.url}" download="${escHtml(file.name)}" class="file-dl-btn">↓ 下载</a></div>`;
        el.innerHTML=`<div class="msg-av">🦝</div><div class="msg-body"><div class="msg-bbl">${body}</div></div>`;
        msgsEl.appendChild(el);msgsEl.scrollTop=msgsEl.scrollHeight;
      }
    }catch(e){
      addMsg('system','⚠️ 文件上传失败: '+e.message);
    }
    input.value=''; // reset
  });
}

// ─── Sidebar ────────────────────────────────
function setupSidebar(){
  document.querySelectorAll('.stab').forEach(tab=>{
    tab.addEventListener('click',()=>{
      document.querySelectorAll('.stab').forEach(t=>t.classList.remove('active'));
      document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
      tab.classList.add('active');
      $('panel-'+tab.dataset.panel).classList.add('active');
      // Load conversations when switching to history tab
      if(tab.dataset.panel==='history')loadSavedConversations();
    });
  });
  $('sidebarToggle')?.addEventListener('click',()=>$('sidebar').classList.toggle('open'));
}

// ─── Skills ─────────────────────────────────
async function loadSkills(){
  try{
    const res=await fetch('/skills'),skills=await res.json();
    const list=$('skills-list'),empty=$('skills-empty');
    if(!skills.length){empty.innerHTML='<div class="empty-icon">📦</div><div>暂无已安装技能</div>';empty.style.display='';list.innerHTML='';return}
    empty.style.display='none';
    list.innerHTML=skills.map(s=>`<div class="skill-card" onclick="sendQuick('${s.trigger_words[0]||s.name}')"><div class="sk-name">${s.name}<span class="sk-ver">v${s.version}</span></div><div class="sk-desc">${s.description||'无描述'}</div><div class="sk-triggers">${s.trigger_words.map(w=>`<span class="ttag">${w}</span>`).join('')}</div></div>`).join('');
  }catch{$('skills-empty').innerHTML='<div class="empty-icon">⚠️</div><div>加载失败</div>'}
}

// ─── Tasks ──────────────────────────────────
async function loadTasks(){
  try{
    const res=await fetch('/tasks'),tasks=await res.json();
    const list=$('tasks-list'),empty=$('tasks-empty');
    if(!tasks.length){empty.style.display='';list.innerHTML='';return}
    empty.style.display='none';
    list.innerHTML=tasks.map(t=>`<div class="task-card"><div class="task-hdr"><span class="task-sk">${t.skill_name}</span><span class="task-st ${t.status}">${statusLabel(t.status)}</span></div><div class="task-id">${t.task_id.substring(0,12)}...</div>${['pending','running'].includes(t.status)?`<button class="task-cancel" onclick="cancelTask('${t.task_id}')">取消</button>`:''}</div>`).join('');
  }catch{}
}
function statusLabel(s){return{pending:'等待中',running:'运行中',success:'已完成',failed:'失败',cancelled:'已取消'}[s]||s}
async function cancelTask(id){try{await fetch(`/tasks/${id}/cancel`,{method:'POST'});loadTasks()}catch{}}

// ─── History Panel ──────────────────────────
function updateHistoryPanel(){
  const list=$('history-list'),empty=$('history-empty');
  if(!list)return;
  if(!history.length){if(empty)empty.style.display='';list.innerHTML='';return}
  if(empty)empty.style.display='none';
  list.innerHTML=history.filter(h=>h.role==='user').map(h=>`<div class="hist-item"><div class="hist-preview">${escHtml(h.content)}</div><div class="hist-time">${h.time}</div></div>`).join('');
}

// ─── LLM Status ─────────────────────────────
async function loadLLM(){
  try{
    const res=await fetch('/status'),data=await res.json(),llm=data.llm||{};
    const dot=$('llmDot'),prov=$('llmProv'),model=$('llmModel');
    const map={ready:{cls:'ready',text:'已连接'},mock:{cls:'mock',text:'Mock 模式'},no_api_key:{cls:'no_key',text:'未配置 Key'}};
    const s=map[llm.status]||map.mock;
    dot.className='llm-dot '+s.cls;
    prov.textContent=(llm.provider==='spark'?'讯飞 codeplan':llm.provider)+' · '+s.text;
    model.textContent=llm.model||'-';
    const hdr=$('chatStatus');
    if(hdr&&llm.status==='ready'){
      hdr.querySelector('.status-dot').style.background='var(--ok)';
      const sp=hdr.querySelector('span:last-child');if(sp)sp.textContent='codeplan 在线';
    }
  }catch{}
}
