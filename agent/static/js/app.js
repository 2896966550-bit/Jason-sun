/* Equis · 外壳 / 路由 / 主题 / 输入 / 上传（P3-37 由 equis.html 拆出，普通 <script>/<link>，无构建工具） */

/* ============================================
   State
   ============================================ */
let isStreaming = false;
let currentAbortController = null;

// ── 键盘：Enter 发送，Shift+Enter 换行 ──
(function() {
  var inp = document.getElementById('msgInput');
  if (inp) {
    var composing = false;
    inp.addEventListener('compositionstart', function() { composing = true; });
    inp.addEventListener('compositionend', function() { composing = false; });
    inp.addEventListener('keydown', function(e) {
      if (composing || e.isComposing || e.keyCode === 229) return;
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        e.stopPropagation();
        sendMessage();
      }
    });
  }
})();

/* ============================================
   Theme
   ============================================ */
function toggleTheme() {
  var html = document.documentElement;
  var sun = document.getElementById('iconSun');
  var moon = document.getElementById('iconMoon');
  var btn = document.getElementById('themeBtn');
  var isDark = html.getAttribute('data-theme') === 'dark';

  btn.style.transition = 'none';
  btn.style.transform = 'rotate(0deg) scale(1)';
  requestAnimationFrame(function() {
    btn.style.transition = 'transform 0.4s cubic-bezier(0.34,1.56,0.64,1)';
    btn.style.transform = 'rotate(360deg) scale(1.15)';
    setTimeout(function() { btn.style.transform = 'rotate(360deg) scale(1)'; }, 400);
  });

  if (isDark) {
    html.setAttribute('data-theme', 'light');
    sun.style.display = '';
    moon.style.display = 'none';
    localStorage.setItem('equis-theme', 'light');
  } else {
    html.setAttribute('data-theme', 'dark');
    sun.style.display = 'none';
    moon.style.display = '';
    localStorage.setItem('equis-theme', 'dark');
  }
}

/* P3-32：原 chat ↔ monitor 的"隐藏对话"式切换已废弃，
   改为切到 #/workspace 视图；传感器轮询只在工作台激活时运行 */
var monitorTimer = null;
function toggleMode() { navigateTo('workspace'); }
function startMonitorPolling() {
  if (monitorTimer) return;
  pollSensors();
  monitorTimer = setInterval(pollSensors, 2000);
}
function stopMonitorPolling() {
  if (monitorTimer) { clearInterval(monitorTimer); monitorTimer = null; }
}
function pollSensors() {
  var st = document.getElementById('monitorStatus');
  if (st) st.textContent = '刷新中...';
  fetch('/api/sensors')
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (d.ok) {
        document.getElementById('monTemp').textContent = d.temp.toFixed(1);
        document.getElementById('monTemp').className = 'monitor-card-value' + (d.temp > 80 ? ' bad' : d.temp > 60 ? ' warn' : '');
        document.getElementById('monHum').textContent = d.hum.toFixed(1);
        document.getElementById('monVib').textContent = (Math.sqrt(d.ax*d.ax + d.ay*d.ay + d.az*d.az)).toFixed(2);
        if (st) { st.textContent = '在线'; st.className = 'monitor-status live'; }
        document.getElementById('monitorTime').textContent = new Date().toLocaleTimeString();
      } else {
        if (st) { st.textContent = (d.error || '未连接'); st.className = 'monitor-status'; }
      }
    })
    .catch(function() {
      if (st) { st.textContent = '请求失败'; st.className = 'monitor-status'; }
    });
}

// Init theme — auto match system
(function() {
  var saved = localStorage.getItem('equis-theme');
  var sun = document.getElementById('iconSun');
  var moon = document.getElementById('iconMoon');
  if (saved) {
    var isDark = saved === 'dark';
    document.documentElement.setAttribute('data-theme', isDark ? 'dark' : 'light');
    if (isDark) { sun.style.display = 'none'; moon.style.display = ''; }
  } else if (window.matchMedia('(prefers-color-scheme: dark)').matches) {
    document.documentElement.setAttribute('data-theme', 'dark');
    sun.style.display = 'none'; moon.style.display = '';
  }
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function(e) {
    if (!localStorage.getItem('equis-theme')) {
      document.documentElement.setAttribute('data-theme', e.matches ? 'dark' : 'light');
    }
  });
})();

/* ============================================
   Sidebar / Rail
   ============================================ */
function toggleSidebar() {
  var sidebar = document.getElementById('sidebar');
  var overlay = document.getElementById('sidebarOverlay');
  var app = document.getElementById('app');

  if (window.innerWidth <= 900) {
    // Mobile：抽屉
    sidebar.classList.toggle('open');
    overlay.classList.toggle('show');
  } else {
    // Desktop：栅格 context 列收起（P3-33 起不再依赖 flex 宽度）
    var collapsed = sidebar.classList.toggle('collapsed');
    if (app) app.classList.toggle('side-collapsed', collapsed);
  }
}

// P3-33：导航 rail 与顶栏导航共用 VIEW_META，避免两处维护
function renderRail() {
  var box = document.getElementById('railItems');
  if (!box || box.dataset.built === '1') return;
  box.innerHTML = '';
  VIEW_META.forEach(function(m) {
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'rail-btn rail-item';
    b.dataset.view = m.id;
    b.setAttribute('aria-label', m.label);
    b.innerHTML = '<span aria-hidden="true">' + m.icon + '</span><span class="rail-tip">' + m.label + '</span>';
    b.addEventListener('click', function() { navigateTo(m.id); });
    box.appendChild(b);
  });
  box.dataset.built = '1';
}

/* ============================================
   Input
   ============================================ */
function autoResize(textarea) {
  textarea.style.height = 'auto';
  textarea.style.height = Math.min(textarea.scrollHeight, 200) + 'px';
  textarea.style.overflowY = textarea.scrollHeight > 200 ? 'auto' : 'hidden';

  var sendBtn = document.getElementById('sendBtn');
  sendBtn.disabled = textarea.value.trim() === '' || isStreaming;
}

/* ============================================
   Welcome & Presets
   ============================================ */
function sendPreset(text) {
  document.getElementById('msgInput').value = text;
  autoResize(document.getElementById('msgInput'));
  sendMessage();
}

function newChat() {
  if (isStreaming) return;

  var messageList = document.getElementById('messageList');
  var welcome = document.getElementById('welcomeScreen');

  // 清空消息，显示欢迎页
  messageList.innerHTML = '';
  messageList.appendChild(welcome);
  welcome.style.display = '';
  welcome.classList.add('fade-in');

  // 关闭移动端侧边栏
  if (window.innerWidth <= 900 && document.getElementById('sidebar').classList.contains('open')) {
    toggleSidebar();
  }

  // 滚动到顶部
  document.getElementById('chatContainer').scrollTop = 0;

  // 刷新历史
  loadHistory();
}

/* ============================================
   History
   ============================================ */
// 历史列表来自后端 /api/history（只读接口，没有 DELETE）。
// 因此"删除"实现为本地隐藏 + localStorage 记录，刷新后不再出现，
// 既不假装已从服务端删除，也不越权改动 Python 侧。
var HISTORY_HIDDEN_KEY = 'equis-history-hidden';
function hiddenHistory() {
  try { return JSON.parse(localStorage.getItem(HISTORY_HIDDEN_KEY) || '[]'); } catch(e) { return []; }
}
function hideHistoryItem(q) {
  var list = hiddenHistory();
  if (list.indexOf(q) < 0) list.push(q);
  try { localStorage.setItem(HISTORY_HIDDEN_KEY, JSON.stringify(list.slice(-200))); } catch(e) {}
}

async function loadHistory() {
  try {
    var res = await fetch('/api/history');
    var data = await res.json();
    var list = document.getElementById('historyList');
    list.innerHTML = '';
    var hidden = hiddenHistory();
    var questions = (data.questions || []).filter(function(q) { return hidden.indexOf(q) < 0; });

    if (questions.length > 0) {
      questions.forEach(function(q, i) {
        var item = document.createElement('div');
        item.className = 'history-item';
        item.textContent = q;
        item.title = q;
        item.onclick = function() { loadConversation(q); };
        var del = document.createElement('button');
        del.className = 'hist-del';
        del.textContent = '×';
        del.title = '从本地列表移除（后端无删除接口）';
        del.onclick = function(e) {
          e.stopPropagation();
          hideHistoryItem(q);
          item.remove();
        };
        item.appendChild(del);
        list.appendChild(item);
      });
    } else {
      var empty = document.createElement('div');
      empty.style.cssText = 'padding:12px 8px;font-size: var(--fs-2);color:var(--text-tertiary);';
      empty.textContent = '暂无历史对话';
      list.appendChild(empty);
    }
  } catch(e) {
    console.error('加载历史失败', e);
  }
}

async function loadConversation(question) {
  document.getElementById('msgInput').value = question;
  autoResize(document.getElementById('msgInput'));
  if (window.innerWidth <= 900 && document.getElementById('sidebar').classList.contains('open')) {
    toggleSidebar();
  }
  sendMessage();
}

// 初始化加载历史
loadHistory();

/* ============================================
   Send Message
   ============================================ */
async function sendMessage() {
  var input = document.getElementById('msgInput');
  var text = input.value.trim();

  if (!text || isStreaming) return;

  // 隐藏欢迎页
  var welcome = document.getElementById('welcomeScreen');
  if (welcome) welcome.style.display = 'none';

  // 添加用户消息
  addMessage('user', text);

  // 清空输入框
  input.value = '';
  autoResize(input);

  // 开始流式回复
  await streamReply(text);
}

/* ============================================
   Image Upload
   ============================================ */
// ── 图片处理统一入口（选择文件 / 粘贴 / 拖拽 三通道共用）──
async function processImageFile(file) {
  if (!file) return;
  // P3-28：流式期间禁止开启第二条流（否则出现并发流 + 重复 id="thinkingMessage"）
  if (isStreaming) {
    addMessage('ai', '⏳ 正在生成回答，请等当前回答结束或点击 ⏹ 停止后再上传图片。');
    return;
  }
  if (!file.type || file.type.indexOf('image/') !== 0) {
    addMessage('ai', '仅支持图片文件（jpg / png / webp / bmp）');
    return;
  }
  if (file.size > 20 * 1024 * 1024) {
    addMessage('ai', '图片过大（超过 20MB），请压缩后重试');
    return;
  }

  // 隐藏欢迎页
  var welcome = document.getElementById('welcomeScreen');
  if (welcome) welcome.style.display = 'none';

  // 1) 本地预览气泡（缩略图）—— 用 DOM 构建，避免任何字符串拼接进 innerHTML
  var previewUrl = URL.createObjectURL(file);
  var messageList = document.getElementById('messageList');
  var msgDiv = document.createElement('div');
  msgDiv.className = 'message user';
  var av = document.createElement('div');
  av.className = 'msg-avatar';
  av.textContent = '你';
  var mc = document.createElement('div');
  mc.className = 'msg-content';
  var mb = document.createElement('div');
  mb.className = 'msg-bubble';
  mb.style.padding = '8px';
  var img = document.createElement('img');
  img.src = previewUrl;
  img.alt = '待分析图片';
  img.style.cssText = 'max-width:220px;border-radius:12px;display:block;';
  var cap = document.createElement('div');
  cap.style.cssText = 'font-size: var(--fs-2);color:var(--text-secondary);margin-top:6px;';
  cap.textContent = '正在上传：' + file.name;
  mb.appendChild(img); mb.appendChild(cap);
  mc.appendChild(mb);
  msgDiv.appendChild(av); msgDiv.appendChild(mc);
  messageList.appendChild(msgDiv);
  scrollToBottom();

  // 2) 上传到后端，取本地路径（多模态入链）
  try {
    var fd = new FormData();
    fd.append('file', file);
    var up = await fetch('/api/upload', { method: 'POST', body: fd });
    var upData = await up.json();
    if (!upData.ok) throw new Error(upData.error || '上传失败');
    cap.textContent = '图片已上传：' + file.name;
    window.__pendingImagePath = upData.path;
    // 3) 走对话流：多智能体协同 + 结构化视觉卡片
    await streamReply('请分析这张图片中的工件，并给出检测依据');
  } catch (err) {
    cap.textContent = '图片上传失败：' + err.message;
    addMessage('ai', '图片上传失败：' + err.message);
  }
}

// 通道 1：点击按钮选择文件
function handleImageUpload(event) {
  var file = event.target.files[0];
  event.target.value = '';
  processImageFile(file);
}

// 通道 2：粘贴图片（Ctrl+V）
document.addEventListener('paste', function (e) {
  var items = (e.clipboardData && e.clipboardData.items) || [];
  for (var i = 0; i < items.length; i++) {
    if (items[i].type && items[i].type.indexOf('image') === 0) {
      var f = items[i].getAsFile();
      if (f) {
        e.preventDefault();
        processImageFile(f);
        return;
      }
    }
  }
});

// 通道 3：拖拽图片到窗口
(function initDropUpload() {
  var overlay = document.createElement('div');
  overlay.className = 'drop-overlay';
  overlay.innerHTML = '<div class="drop-overlay-inner"><svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-image"/></svg> 松开鼠标即可上传图片</div>';
  document.body.appendChild(overlay);

  var depth = 0;
  function hide() { depth = 0; overlay.classList.remove('active'); }

  window.addEventListener('dragenter', function (e) {
    e.preventDefault();
    depth++;
    overlay.classList.add('active');
  });
  window.addEventListener('dragover', function (e) { e.preventDefault(); });
  window.addEventListener('dragleave', function (e) {
    e.preventDefault();
    depth--;
    if (depth <= 0) hide();
  });
  window.addEventListener('drop', function (e) {
    e.preventDefault();
    hide();
    var files = (e.dataTransfer && e.dataTransfer.files) || [];
    if (files.length) processImageFile(files[0]);
  });
})();

async function analyzeImage(file) {
  if (isStreaming) return;   // P3-28：并发守卫
  addThinkingBox();

  try {
    var formData = new FormData();
    formData.append('image', file);

    var response = await fetch('/api/vision', {
      method: 'POST',
      body: formData
    });

    var data = await response.json();
    removeThinkingBox();
    addMessage('ai', data.response || '无法分析图片。');
  } catch(e) {
    console.error('图片分析失败', e);
    removeThinkingBox();
    addMessage('ai', '图片分析失败，请确保后端服务已启动且多模态功能可用。');
  }
}

/* ============================================
   Init
   ============================================ */
document.addEventListener('DOMContentLoaded', function() {
  // Desktop keyboard entry should not move the welcome page or summon a mobile keyboard.
  if (window.innerWidth > 768 && document.body.dataset.view === 'chat') document.getElementById('msgInput').focus({preventScroll: true});
});
// ── PDF 导出 ──
function downloadReport() {
  var bubbles = document.querySelectorAll('.msg-bubble');
  var text = 'Equis 测量报告\\n' + '='.repeat(40) + '\\n\\n';
  bubbles.forEach(function(b) {
    var role = b.closest('.message.user') ? '用户' : 'Equis';
    text += '【' + role + '】\\n' + b.textContent + '\\n\\n';
  });
  if (bubbles.length === 0) return alert('请先进行对话');
  var blob = new Blob([text], { type: 'text/plain' });
  var a = document.createElement('a'); a.href = URL.createObjectURL(blob);
  a.download = 'equis_report.txt'; a.click();
  URL.revokeObjectURL(a.href);
}

// P3-38：移除境外 CDN 的 KaTeX 注入（本地 /static/katex/katex.min.js 已在 <head> 加载）。
// 合规红线：数据不出境，前端不得请求任何境外域名。

// ── 语音输入：本地录音 + 后端 fun-asr（P0-8）────────────────
// 代码审计修复：原先使用 Web Speech API
//   var SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
// Chrome/Edge 的 webkitSpeechRecognition 会把**音频上传到 Google** 做识别，
// 违反项目"语音等敏感数据必须脱敏、数据不出境"的硬性要求。
// 现改为 MediaRecorder 本地录音 → POST /api/speech-to-text（境内 fun-asr），
// 与 static/index.html 的改法一致。
var isRecording = false;
var _rec = null;
var _recChunks = [];
var _recStream = null;

function startVoiceInput() {
  var btn = document.getElementById('voiceBtn');
  if (isRecording) { stopVoiceInput(); return; }

  if (!navigator.mediaDevices || !window.MediaRecorder) {
    alert('当前浏览器不支持录音，请使用 Chrome / Edge。');
    return;
  }

  navigator.mediaDevices.getUserMedia({ audio: true }).then(function(stream) {
    _recStream = stream;
    _recChunks = [];
    _rec = new MediaRecorder(stream);
    _rec.ondataavailable = function(e) { if (e.data && e.data.size) _recChunks.push(e.data); };
    _rec.onstop = function() { uploadVoice(); };
    _rec.start();
    isRecording = true;
    if (btn) {
      btn.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-stop"/></svg>';
      btn.style.animation = 'pulse 1s ease-in-out infinite';
    }
  }).catch(function() {
    alert('无法访问麦克风，请检查浏览器权限。');
  });
}

function stopVoiceInput() {
  isRecording = false;
  var btn = document.getElementById('voiceBtn');
  if (btn) { btn.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-mic"/></svg>'; btn.style.animation = ''; }
  if (_rec && _rec.state !== 'inactive') {
    _rec.stop();                       // 触发 onstop -> uploadVoice
  } else if (_recStream) {
    _recStream.getTracks().forEach(function(t) { t.stop(); });
    _recStream = null;
  }
}

function uploadVoice() {
  if (_recStream) {
    _recStream.getTracks().forEach(function(t) { t.stop(); });
    _recStream = null;
  }
  var type = (_rec && _rec.mimeType) || 'audio/webm';
  var blob = new Blob(_recChunks, { type: type });
  if (!blob.size) return;

  var ta = document.getElementById('msgInput');
  var oldPh = ta ? ta.placeholder : '';
  if (ta) ta.placeholder = '语音识别中…';

  var fd = new FormData();
  fd.append('audio', blob, 'voice.webm');
  fetch('/api/speech-to-text', { method: 'POST', body: fd })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (ta) ta.placeholder = oldPh;
      if (d && d.text) {
        ta.value = d.text;
        if (typeof autoResize === 'function') autoResize(ta);
      } else {
        alert('语音识别失败：' + ((d && d.error) || '无结果'));
      }
    })
    .catch(function() {
      if (ta) ta.placeholder = oldPh;
      alert('语音识别请求失败');
    });
}

// Card lighting, throttled pointer tilt and draft-safe placeholders live in motion.js.

/* ============================================
   P3-32 Hash 路由与视图层级
   #/chat · #/workspace · #/standards · #/cases · #/system
   - hashchange / popstate 驱动视图切换（浏览器后退可用）
   - 钻取用 history.pushState，视图中枢不重建 DOM
   - 当前视图存 localStorage('equis-view')，与 'equis-theme' 并列
   ============================================ */
var VIEW_META = [
  { id: 'chat',      icon: '<svg viewBox="0 0 24 24" class="vi" aria-hidden="true"><use href="#i-chat"/></svg>', label: '对话', title: '测量可信度保障系统' },
  { id: 'workspace', icon: '<svg viewBox="0 0 24 24" class="vi" aria-hidden="true"><use href="#i-sensor"/></svg>', label: '工作台', title: '工作台 · 实时监控' },
  { id: 'standards', icon: '<svg viewBox="0 0 24 24" class="vi" aria-hidden="true"><use href="#i-book"/></svg>', label: '标准库', title: '标准库与历史提问' },
  { id: 'cases',     icon: '<svg viewBox="0 0 24 24" class="vi" aria-hidden="true"><use href="#i-compass"/></svg>', label: '案例', title: '运行案例 · 可追溯' },
  { id: 'system',    icon: '<svg viewBox="0 0 24 24" class="vi" aria-hidden="true"><use href="#i-shield"/></svg>', label: '系统', title: '系统与数据合规' }
];
var VIEW_STORAGE_KEY = 'equis-view';
var _currentRoute = { view: null, sub: null };

function viewMeta(id) {
  for (var i = 0; i < VIEW_META.length; i++) { if (VIEW_META[i].id === id) return VIEW_META[i]; }
  return VIEW_META[0];
}
function isValidView(id) {
  return VIEW_META.some(function(m) { return m.id === id; });
}
function parseHash(hash) {
  var parts = String(hash || '').replace(/^#\/?/, '').split('/').filter(function(s) { return s.length; });
  var view = isValidView(parts[0]) ? parts[0] : '';
  return { view: view, sub: parts.slice(1).join('/') || null };
}

function renderViewNav() {
  var nav = document.getElementById('viewNav');
  if (!nav || nav.dataset.built === '1') return;
  nav.innerHTML = '';
  VIEW_META.forEach(function(m) {
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'view-nav-btn';
    b.setAttribute('aria-label', m.label);
    b.title = m.label;
    b.dataset.view = m.id;
    b.innerHTML = '<span class="vn-icon">' + m.icon + '</span><span class="vn-label">' + m.label + '</span>';
    b.addEventListener('click', function() { navigateTo(m.id); });
    nav.appendChild(b);
  });
  nav.dataset.built = '1';
}

function navigateTo(view, sub, opts) {
  opts = opts || {};
  if (!isValidView(view)) view = 'chat';
  var hash = '#/' + view + (sub ? '/' + sub : '');
  if (location.hash === hash) { applyRoute(); return; }
  var state = { view: view, sub: sub || null };
  if (opts.replace) history.replaceState(state, '', hash);
  else history.pushState(state, '', hash);
  applyRoute();
}

/* ── P3-34 View Transitions：Chrome 原生零依赖，不支持时优雅降级 ── */
function supportsViewTransition() {
  return typeof document.startViewTransition === 'function' &&
         (document.documentElement.dataset.motion ? document.documentElement.dataset.motion === 'full' :
          !(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches));
}
function withViewTransition(update, opts) {
  opts = opts || {};
  if (!supportsViewTransition()) { update(); return null; }
  var root = document.documentElement;
  root.dataset.vt = opts.kind || 'view';
  root.dataset.vtDir = opts.direction === 'back' ? 'back' : 'forward';
  var cleanup = function() {
    delete root.dataset.vt;
    delete root.dataset.vtDir;
  };
  try {
    var t = document.startViewTransition(function() { update(); });
    t.finished.then(cleanup).catch(cleanup);
    return t;
  } catch (e) {
    cleanup();
    update();
    return null;
  }
}
// 卡片 → 详情 的共享元素过渡（两端复用同一个 view-transition-name）
function drillTransition(cardEl, update) {
  if (!cardEl || !supportsViewTransition()) { update(); return; }
  cardEl.style.viewTransitionName = 'drill-card';
  withViewTransition(function() {
    update();
    cardEl.style.viewTransitionName = '';        // 旧快照已捕获，避免重名
    var target = document.querySelector('.inspector-body .drill-target');
    if (target) target.style.viewTransitionName = 'drill-card';
  }, { kind: 'drill', direction: 'forward' });
}
function routeDirection(fromView, toView) {
  var a = -1, b = -1;
  VIEW_META.forEach(function(m, i) {
    if (m.id === fromView) a = i;
    if (m.id === toView) b = i;
  });
  if (a < 0 || b < 0 || a === b) return 'forward';
  return b < a ? 'back' : 'forward';
}

function applyRoute() {
  var r = parseHash(location.hash);
  var view = r.view || 'chat';
  var sub = r.sub;

  // 幂等：后退同时触发 popstate + hashchange，避免重复渲染
  if (_currentRoute.view === view && _currentRoute.sub === sub) return;
  var dir = routeDirection(_currentRoute.view, view);
  _currentRoute = { view: view, sub: sub };
  withViewTransition(function() { applyRouteInner(view, sub); }, { kind: 'view', direction: dir });
}

function applyRouteInner(view, sub) {
  document.querySelectorAll('.view').forEach(function(sec) {
    sec.classList.toggle('active', sec.getAttribute('data-view') === view);
  });
  document.querySelectorAll('.view-nav-btn').forEach(function(b) {
    b.classList.toggle('active', b.getAttribute('data-view') === view);
    b.setAttribute('aria-current', b.getAttribute('data-view') === view ? 'page' : 'false');
  });
  document.querySelectorAll('.rail-btn.rail-item').forEach(function(b) {
    b.classList.toggle('active', b.getAttribute('data-view') === view);
    b.setAttribute('aria-current', b.getAttribute('data-view') === view ? 'page' : 'false');
  });
  var title = document.getElementById('viewTitle');
  if (title) title.textContent = viewMeta(view).title;
  document.body.setAttribute('data-view', view);
  try { localStorage.setItem(VIEW_STORAGE_KEY, view); } catch(e) {}

  // 各视图副作用
  if (view === 'workspace') { startMonitorPolling(); loadWorkspaceRuns(); }
  else { stopMonitorPolling(); }

  if (view === 'standards') { loadStandardsView(); }
  if (view === 'cases') {
    if (sub && sub.indexOf('run/') === 0) renderCaseDetail(sub.slice(4));
    else loadCasesView();
  }
  if (view === 'system') {
    var active = document.querySelector('#viewSystem .ptab.active');
    switchPanelTab(active ? active.getAttribute('data-tab') : 'arch');
    loadCompliance();   // P3-32：进入即自检，把「数据不出境」结论显示在标题旁
  }
  if (view === 'chat') {
    var inp = document.getElementById('msgInput');
    if (inp && window.innerWidth > 768) inp.focus({preventScroll: true});
    var welcome = document.getElementById('welcomeScreen');
    if (welcome && welcome.style.display !== 'none') document.getElementById('chatContainer').scrollTop = 0;
    else scrollToBottom();
  }
}

function initRouter() {
  renderViewNav();
  renderRail();
  var r = parseHash(location.hash);
  if (!r.view) {
    var saved = null;
    try { saved = localStorage.getItem(VIEW_STORAGE_KEY); } catch(e) {}
    var start = isValidView(saved) ? saved : 'chat';
    navigateTo(start, null, { replace: true });
  } else {
    applyRoute();
  }
}

window.addEventListener('hashchange', applyRoute);
window.addEventListener('popstate', applyRoute);
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initRouter);
} else {
  initRouter();
}

// 运行记录钻取（P3-32/P3-36）：[data-run] 走 pushState，后退可回到列表
document.addEventListener('click', function(e) {
  var el = e.target && e.target.closest ? e.target.closest('[data-run]') : null;
  if (!el) return;
  var id = el.getAttribute('data-run');
  if (id) navigateTo('cases', 'run/' + id);
});
