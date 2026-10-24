/* Equis · SSE 流式 / 消息渲染 / 运行时间轴（P3-37 由 equis.html 拆出，普通 <script>/<link>，无构建工具） */

// ── 消息操作栏（P3-27：改用 data-action + 事件委托，见下方 click 委托）──
function buildMsgActions() {
  var actions = document.createElement('div');
  actions.className = 'msg-actions';
  [['copy', '复制', ''], ['speak', '朗读', ''], ['redo', '重新生成', '']].forEach(function(it) {
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'msg-act-btn';
    b.dataset.action = it[0];
    b.title = it[1];
    b.setAttribute('aria-label', it[1]);
    b.textContent = it[2];
    actions.appendChild(b);
  });
  return actions;
}

// 只取正文，避免把操作栏图标读进复制/朗读内容
function bubbleText(bubble) {
  var body = bubble.querySelector('.msg-body');
  var t = body ? body.innerText : bubble.textContent;
  return String(t || '').replace(/\u{1F4CB}|\u{1F50A}|\u{1F504}/gu, '').trim();
}

function setBubbleContent(bubble, text) {
  var body = bubble.querySelector('.msg-body');
  if (!body) {
    body = document.createElement('div');
    body.className = 'msg-body';
    bubble.insertBefore(body, bubble.firstChild);
  }
  body.innerHTML = formatMessage(text);
  renderLatex(body);   // P3-27a：流式与最终渲染都要走 KaTeX
  return body;
}

function addMessage(role, content) {
  var messageList = document.getElementById('messageList');
  var msgDiv = document.createElement('div');
  msgDiv.className = 'message ' + role;
  if (role === 'user') msgDiv.setAttribute('data-original', content);
  var avatar = document.createElement('div');
  avatar.className = 'msg-avatar';
  avatar.innerHTML = role === 'user' ? '你' : '<img src="/static/mascot.png" style="width:100%;height:100%;object-fit:cover;border-radius:50%">';
  var contentDiv = document.createElement('div');
  contentDiv.className = 'msg-content';
  var bubble = document.createElement('div');
  bubble.className = 'msg-bubble';
  // 正文单独一层：流式重绘只替换 .msg-body，操作栏不参与重绘（P3-27）
  var body = document.createElement('div');
  body.className = 'msg-body';
  body.innerHTML = formatMessage(content);
  bubble.appendChild(body);
  contentDiv.appendChild(bubble);
  // 消息操作栏（仅bot消息）
  if (role === 'ai') {
    bubble.appendChild(buildMsgActions());
  }
  msgDiv.appendChild(avatar);
  msgDiv.appendChild(contentDiv);
  var now = new Date();
  var time = document.createElement('span');
  time.className = 'msg-time';
  time.textContent = now.getHours().toString().padStart(2,'0') + ':' + now.getMinutes().toString().padStart(2,'0');
  contentDiv.appendChild(time);
  messageList.appendChild(msgDiv);
  scrollToBottom();
  // KaTeX 渲染
  if (role === 'ai') renderLatex(body);
  return bubble;
}

// ── 操作栏事件委托（P3-27b）──
// renderLatex 与流式重绘都会重建气泡内部 DOM，属性处理器（onclick）会被丢弃，
// 因此统一委托到 document 上，只认 data-action，不受 DOM 重建影响。
document.addEventListener('click', function(e) {
  var btn = e.target && e.target.closest ? e.target.closest('.msg-act-btn') : null;
  if (!btn) return;
  var bubble = btn.closest('.msg-bubble');
  if (!bubble) return;
  var action = btn.dataset.action;

  if (action === 'copy') {
    var txt = bubbleText(bubble);
    function done() {
      bubble.classList.remove('copy-failed');
      bubble.classList.add('copied');
      setTimeout(function() { bubble.classList.remove('copied'); }, 1600);
    }
    function fallback() {
      if (legacyCopy(txt)) done();
      else {
        bubble.classList.remove('copied');
        bubble.classList.add('copy-failed');
        setTimeout(function() { bubble.classList.remove('copy-failed'); }, 2400);
      }
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(txt).then(done).catch(fallback);
    } else {
      fallback();
    }
  } else if (action === 'speak') {
    if (!window.speechSynthesis) { alert('当前浏览器不支持语音朗读'); return; }
    var t = bubbleText(bubble);
    if (!t) return;
    speechSynthesis.cancel();
    var u = new SpeechSynthesisUtterance(t);
    u.lang = 'zh-CN'; u.rate = 1.1;
    speechSynthesis.speak(u);
  } else if (action === 'redo') {
    if (isStreaming) return;
    var msg = btn.closest('.message');
    var prevUser = msg ? msg.previousElementSibling : null;
    while (prevUser && !prevUser.classList.contains('user')) prevUser = prevUser.previousElementSibling;
    if (!prevUser) return;
    var text = prevUser.getAttribute('data-original');
    if (!text) return;
    document.getElementById('msgInput').value = text;
    msg.remove();
    sendMessage();
  }
});

// 剪贴板降级（http / 无 clipboard API 时）
function legacyCopy(text) {
  var ta;
  try {
    ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;top:-1000px;opacity:0;';
    document.body.appendChild(ta);
    ta.select();
    return document.execCommand('copy') === true;
  } catch(e) { console.warn('复制失败', e); return false; }
  finally { if (ta) ta.remove(); }
}

// ── KaTeX 公式渲染 ──
function renderLatex(el) {
  if (!el || typeof katex === 'undefined') return;
  try {
    el.innerHTML = el.innerHTML.replace(/\$\$([\s\S]*?)\$\$/g, function(_, latex) {
      try { return katex.renderToString(latex.trim(), {displayMode:true,throwOnError:false}); }
      catch(e) { return _; }
    });
    el.innerHTML = el.innerHTML.replace(/\$([^$]+)\$/g, function(_, latex) {
      try { return katex.renderToString(latex.trim(), {displayMode:false,throwOnError:false}); }
      catch(e) { return _; }
    });
  } catch(e) {}
}

function addThinkingBox() {
  // P3-28：防止重复 id="thinkingMessage"（并发流会让 getElementById 取到错误的那个）
  var existing = document.getElementById('thinkingMessage');
  if (existing && existing.parentNode) existing.parentNode.removeChild(existing);
  var messageList = document.getElementById('messageList');
  var msgDiv = document.createElement('div');
  msgDiv.className = 'message ai';
  msgDiv.id = 'thinkingMessage';

  var avatar = document.createElement('div');
  avatar.className = 'msg-avatar';
  avatar.innerHTML = '<img src="/static/mascot.png" style="width:100%;height:100%;object-fit:cover;border-radius:50%">';

  var contentDiv = document.createElement('div');
  contentDiv.className = 'msg-content';

  var thinkingBox = document.createElement('div');
  thinkingBox.className = 'thinking-box';
  thinkingBox.innerHTML = `
    <div class="thinking-title">
      <span class="spark"></span>
      <span>LangGraph 状态图 · 智能体协同</span>
    </div>
    <div id="thinkSteps">
      <div class="think-step" data-agent="perceive">
        <div class="step-dot waiting">1</div>
        <div class="step-text">感知采集 · 图像 / 传感器总线</div>
      </div>
      <div class="think-step" data-agent="supervisor">
        <div class="step-dot waiting">2</div>
        <div class="step-text">调度器 · 意图分析与分支规划</div>
      </div>
      <div class="think-step" data-agent="recall">
        <div class="step-dot waiting">3</div>
        <div class="step-text">记忆召回 · 历史相似案例</div>
      </div>
      <div class="think-step" data-agent="retrieval">
        <div class="step-dot waiting">4</div>
        <div class="step-text">规元 · 检索计量标准</div>
      </div>
      <div class="think-step" data-agent="vision">
        <div class="step-dot waiting">5</div>
        <div class="step-text">度元 · 多模态视觉理解</div>
      </div>
      <div class="think-step" data-agent="compute">
        <div class="step-dot waiting">6</div>
        <div class="step-text">度元 · 精密计算评定</div>
      </div>
      <div class="think-step" data-agent="plan_execute">
        <div class="step-dot waiting">7</div>
        <div class="step-text">规划执行 · 任务分解与逐步求解</div>
      </div>
      <div class="think-step" data-agent="web">
        <div class="step-dot waiting">8</div>
        <div class="step-text">索元 · 联网搜索新知</div>
      </div>
      <div class="think-step" data-agent="judge">
        <div class="step-dot waiting">9</div>
        <div class="step-text">合规判定 · 结构化卡片汇总</div>
      </div>
      <div class="think-step" data-agent="merge">
        <div class="step-dot waiting">10</div>
        <div class="step-text">结果融合 · 生成可追溯回答</div>
        <div class="think-sub-steps">
          <div class="think-sub-step" data-agent="du_visual">
            <span class="sub-dot">○</span>
            <span class="sub-text">视觉异常检测</span>
          </div>
          <div class="think-sub-step" data-agent="du_sensor">
            <span class="sub-dot">○</span>
            <span class="sub-text">传感器总线采样</span>
          </div>
          <div class="think-sub-step" data-agent="du_gum">
            <span class="sub-dot">○</span>
            <span class="sub-text">GUM 不确定度合成</span>
          </div>
          <div class="think-sub-step" data-agent="du_judge">
            <span class="sub-dot">○</span>
            <span class="sub-text">合规判定</span>
          </div>
        </div>
      </div>
    </div>
    <div class="think-status-line" id="thinkStatusLine"></div>
    <div class="think-done-line" onclick="this.closest('.thinking-box').classList.toggle('think-collapsed');this.parentElement.querySelector('.thinking-title .spark').style.animation='none'">
      <span>✓</span>
      <span>状态图协同完成</span>
    </div>
  `;

  contentDiv.appendChild(thinkingBox);
  msgDiv.appendChild(avatar);
  msgDiv.appendChild(contentDiv);
  messageList.appendChild(msgDiv);

  scrollToBottom();
  return thinkingBox;
}

// 节点级流式：把分支初步结论追加到思考框（治"等20s无反馈"硬伤）
function appendPartialResult(p) {
  var steps = document.getElementById('thinkSteps');
  if (!steps) return;
  var box = document.getElementById('partialResults');
  if (!box) {
    box = document.createElement('div');
    box.id = 'partialResults';
    box.style.cssText = 'margin-top:8px;border-top:1px solid var(--border);padding-top:6px;display:flex;flex-direction:column;gap:6px;max-height:180px;overflow-y:auto;';
    steps.appendChild(box);
  }
  var d = document.createElement('div');
  d.style.cssText = 'font-size: var(--fs-2);line-height:1.5;color:var(--text-secondary);background:var(--primary-subtle);border-radius:8px;padding:6px 9px;';
  var label = {vision:'视觉理解', retrieval:'标准检索', plan_execute:'任务规划', compute:'计算判定', web:'联网检索'}[p.node] || p.node;
  // P3-31：label 与正文全部转义（原来只把 '<' 换掉，仍可注入属性）
  d.innerHTML = '<b style="color:var(--primary)">' + escapeHtml(label) + '</b>　'
    + escapeHtml(String(p.text).replace(/```[\s\S]*?```/g, '[结构化卡片]').slice(0, 220));
  box.appendChild(d);
  // 长任务会产生很多 partial：限制条目数，避免 #partialResults 无界增长
  while (box.children.length > 8) box.removeChild(box.firstChild);
  box.scrollTop = box.scrollHeight;
  scrollToBottom();
}

/* ── P3-27c：agent id 规范化映射 ──────────────────────────────
   graph 引擎节点：perceive / supervisor / recall / retrieval / vision /
                   compute / plan_execute / web / judge / merge
   分支节点（coordinator）：guiyuan / suoyuan / duyuan /
                   du_visual / du_sensor / du_gum / du_judge
   两套命名都要能点亮同一行时间轴。                              */
var AGENT_ALIASES = {
  guiyuan: ['retrieval'],
  suoyuan: ['web'],
  duyuan: ['vision', 'compute'],   // 度元：视觉理解或精密计算，视调度而定
  retrieval_agent: ['retrieval'],
  vision_agent: ['vision']
};

var AGENT_LABELS = {
  perceive:      ['感知采集 · 采集图像/传感器中…', '感知采集 · 完成', '感知采集 · 失败'],
  supervisor:    ['调度器 · 意图分析与分支规划中…', '调度器 · 分支规划完成', '调度器 · 规划失败'],
  recall:        ['记忆召回 · 检索历史相似案例中…', '记忆召回 · 完成', '记忆召回 · 失败'],
  retrieval:     ['规元 · 检索计量标准中…', '规元 · 检索完成', '规元 · 检索失败'],
  vision:        ['度元 · 多模态视觉理解中…', '度元 · 视觉理解完成', '度元 · 视觉理解失败'],
  compute:       ['度元 · 精密计算评定中…', '度元 · 计算完成', '度元 · 计算出错'],
  plan_execute:  ['规划执行 · 任务分解与逐步求解中…', '规划执行 · 完成', '规划执行 · 失败'],
  web:           ['索元 · 联网搜索中…', '索元 · 搜索完成', '索元 · 搜索超时'],
  judge:         ['合规判定 · 合成不确定度中…', '合规判定 · 完成', '合规判定 · 失败'],
  merge:         ['结果融合 · 生成可追溯回答中…', '结果融合 · 完成', '结果融合 · 失败']
};

function resolveAgentIds(agent) {
  if (!agent) return [];
  return AGENT_ALIASES[agent] || [agent];
}

// 单个 agent id → 单行时间轴；返回是否命中该行
function applyAgentState(stepsContainer, agentId, state) {
  var step = stepsContainer.querySelector('[data-agent="' + agentId + '"]');
  if (!step) return false;

  // 子步骤（agent 以 du_ 开头）：父行 = merge（结果融合），旧代码误写成 duyuan
  if (agentId.indexOf('du_') === 0) {
    var parentStep = stepsContainer.querySelector('[data-agent="merge"]');
    if (parentStep && state !== 'done') parentStep.classList.add('active');
    var subDot = step.querySelector('.sub-dot');
    if (state === 'active') {
      step.classList.add('active'); step.classList.remove('done');
      if (subDot) { subDot.textContent = '●'; subDot.style.color = 'var(--primary)'; }
    } else if (state === 'done') {
      step.classList.remove('active'); step.classList.add('done');
      if (subDot) { subDot.textContent = '✓'; subDot.style.color = 'var(--tag-green-fg, var(--tag-green))'; }
    } else if (state === 'error') {
      step.classList.remove('active'); step.classList.add('error');
      if (subDot) { subDot.textContent = '✗'; subDot.style.color = 'var(--danger)'; }
    }
    return true;
  }

  var dot = step.querySelector('.step-dot');
  var text = step.querySelector('.step-text');
  var labels = AGENT_LABELS[agentId];

  if (state === 'active') {
    step.classList.add('active');
    step.classList.remove('done', 'error');
    if (dot) { dot.className = 'step-dot current'; dot.textContent = '●'; }
    if (text && labels) text.textContent = labels[0];
  } else if (state === 'done') {
    step.classList.remove('active', 'error');
    step.classList.add('done');
    if (dot) { dot.className = 'step-dot finished'; dot.textContent = '✓'; }
    if (text && labels) text.textContent = labels[1];
  } else if (state === 'error') {
    step.classList.remove('active', 'done');
    step.classList.add('error');
    if (dot) { dot.className = 'step-dot error-dot'; dot.textContent = '✗'; }
    if (text && labels) text.textContent = labels[2];
  }
  return true;
}

function updateThinkStep(data) {
  // 多Agent状态：{agent_status: {agent: "guiyuan", state: "active"}}
  // 或旧格式：{status: "正在检索..."}
  var stepsContainer = document.getElementById('thinkSteps');
  if (!stepsContainer) return;

  // 节点级流式：分支初步结论追加到思考框底部
  if (data.partial) {
    appendPartialResult(data.partial);
    // 不 return，继续走状态点亮逻辑
  }

  if (data.agent_status) {
    var as = data.agent_status;
    var agent = as.agent;
    var state = as.state;

    // P3-35：同步驱动右侧常驻运行时间轴
    updateRunTimeline(as);

    if (agent === 'status') {
      // P3-27c：引擎内部状态（"Coordinator 分析完毕…"）不再覆写面板标题，
      // 改写进思考框内的子状态行，标题保持产品文案。
      var sl = document.getElementById('thinkStatusLine');
      if (sl) { sl.textContent = state; sl.classList.add('on'); }
      return;
    }

    var ids = resolveAgentIds(agent);
    var hit = false;
    ids.forEach(function(id) { if (applyAgentState(stepsContainer, id, state)) hit = true; });
    if (!hit) return;

    // 全部节点完成后自动折叠（保留原交互）
    if (state === 'done' || state === 'error') {
      var allSteps = stepsContainer.querySelectorAll('.think-step');
      var allDone = true;
      allSteps.forEach(function(s) {
        if (!s.classList.contains('done') && !s.classList.contains('error')) allDone = false;
      });
      if (allDone) {
        setTimeout(function() {
          var box = document.querySelector('.thinking-box');
          if (box && !box.classList.contains('think-collapsed')) {
            box.classList.add('think-collapsed');
          }
        }, 600);
      }
    }
    return;
  }

  // 兼容旧格式
  if (data.status) {
    var steps = stepsContainer.querySelectorAll('.think-step');
    var dots = stepsContainer.querySelectorAll('.step-dot');
    var texts = stepsContainer.querySelectorAll('.step-text');
    var s = String(data.status).toLowerCase();
    var idx = (s.indexOf('检索') >= 0 || s.indexOf('知识') >= 0) ? 0 :
              (s.indexOf('搜索') >= 0 || s.indexOf('联网') >= 0) ? 2 :
              (s.indexOf('计算') >= 0 || s.indexOf('评定') >= 0) ? 1 : -1;
    if (idx >= 0 && idx < steps.length) {
      steps[idx].classList.add('active');
      dots[idx].className = 'step-dot current';
      dots[idx].textContent = '●';
    }
  }
}

function addSuggestionPills(bubble, userMsg) {
  var pills = [];
  var m = userMsg.toLowerCase();
  if (m.includes('不确定度') || m.includes('精度')) {
    pills = ['A类评定 和 B类评定 有什么区别？', '扩展不确定度的包含因子怎么选？', '这个计算结果是否符合 JJF1059.1 要求？'];
  } else if (m.includes('标定') || m.includes('棋盘') || m.includes('校准')) {
    pills = ['张正友标定法需要多少张图片？', '双目相机的基线距离怎么确定？', '标定板的精度要求是多少？'];
  } else if (m.includes('温度') || m.includes('湿度') || m.includes('振动')) {
    pills = ['传感器数据有什么异常？', '环境参数对测量有多大影响？', '需要补偿多少温漂？'];
  } else if (m.includes('标准') || m.includes('jjf') || m.includes('jjg')) {
    pills = ['最新版本修订了什么内容？', '有没有中文版的解读？', '适用于哪种测量场景？'];
  } else {
    pills = ['能给我举个实际计算的例子吗？', '有没有相关的国家标准？', '这个问题能不能用代码实现？'];
  }
  var div = document.createElement('div');
  div.className = 'suggest-pills';
  pills.forEach(function(p) {
    var span = document.createElement('span');
    span.className = 'suggest-pill';
    span.textContent = p;
    span.onclick = function() {
      document.getElementById('msgInput').value = p;
      document.getElementById('sendBtn').click();
    };
    div.appendChild(span);
  });
  var content = bubble.parentElement;
  if (content) content.appendChild(div);
}

function removeThinkingBox() {
  var thinkingMsg = document.getElementById('thinkingMessage');
  if (thinkingMsg) {
    thinkingMsg.remove();
  }
}

/* ============================================
   P3-35 常驻运行时间轴（右侧 inspector，由 SSE agent_status 驱动）
   思考框不再在首个 token 时被删除，而是折叠成一行留痕；
   逐节点耗时先由前端时钟测量，若 final 事件带 run_id 再用
   /api/trace/<id> 覆盖校准（没有 run_id 也不报错）。
   ============================================ */
var RUN_NODES = [
  { id: 'perceive',     label: '感知采集 · 图像 / 传感器总线' },
  { id: 'supervisor',   label: '调度器 · 意图分析与分支规划' },
  { id: 'recall',       label: '记忆召回 · 历史相似案例' },
  { id: 'retrieval',    label: '规元 · 检索计量标准' },
  { id: 'vision',       label: '度元 · 多模态视觉理解' },
  { id: 'compute',      label: '度元 · 精密计算评定' },
  { id: 'plan_execute', label: '规划执行 · 任务分解' },
  { id: 'web',          label: '索元 · 联网搜索' },
  { id: 'judge',        label: '合规判定 · 结构化卡片' },
  { id: 'merge',        label: '结果融合 · 可追溯回答' }
];
var RUN_SUB_NODES = {
  du_visual: '视觉异常检测',
  du_sensor: '传感器总线采样',
  du_gum: 'GUM 不确定度合成',
  du_judge: '合规判定'
};
var _runState = { active: false, t0: 0, nodes: {}, subHtml: '', runId: null };

function applyRunMetadata(data) {
  var meta = (data && data.run_meta) || data || {};
  if (meta.run_id) {
    _runState.runId = meta.run_id;
    window.__lastRunId = meta.run_id;
  }
}

function runTimelineHtml(query) {
  var html = '<div class="run-meta" id="runMeta">' + escapeHtml(String(query || '').slice(0, 60)) + '</div>';
  html += '<div class="run-timeline" id="runTimeline">';
  RUN_NODES.forEach(function(n) {
    html += '<div class="run-node" data-run-node="' + n.id + '">'
      + '<span class="rn-dot"></span>'
      + '<span class="rn-label">' + escapeHtml(n.label) + '</span>'
      + '<span class="rn-ms"></span></div>';
  });
  html += '</div><div class="run-sub" id="runSub"></div>';
  return html;
}

function startRunTimeline(query) {
  window.__lastRunId = null;
  _runState = { active: true, t0: (window.performance && performance.now) ? performance.now() : Date.now(), nodes: {}, subHtml: '', runId: null };
  showInspector('运行时间轴', runTimelineHtml(query));
  var meta = document.getElementById('runMeta');
  if (meta) meta.innerHTML = escapeHtml(String(query || '').slice(0, 60)) + '<br>开始于 ' + new Date().toLocaleTimeString();
}

function _now() { return (window.performance && performance.now) ? performance.now() : Date.now(); }

function updateRunTimeline(agentStatus) {
  if (!agentStatus || !_runState.active) return;
  var agent = agentStatus.agent;
  var state = agentStatus.state;
  if (!agent || agent === 'status') return;

  // 子步骤（du_*）→ 显示在时间轴下方
  if (RUN_SUB_NODES[agent]) {
    var icon = state === 'done' ? '✓' : state === 'error' ? '✗' : '●';
    var cls = state === 'error' ? 'error' : state === 'done' ? 'done' : 'active';
    _runState.subHtml = '<div class="run-sub-row ' + cls + '">' + icon + ' '
      + escapeHtml(RUN_SUB_NODES[agent]) + '</div>';
    var subBox = document.getElementById('runSub');
    if (subBox) subBox.innerHTML = '<div class="run-sub-title">当前子步骤</div>' + _runState.subHtml;
    return;
  }

  resolveAgentIds(agent).forEach(function(id) {
    var el = document.querySelector('#runTimeline [data-run-node="' + id + '"]');
    if (!el) return;
    var rec = _runState.nodes[id] || (_runState.nodes[id] = {});
    if (state === 'active') {
      rec.t0 = _now();
      el.className = 'run-node active';
    } else if (state === 'done' || state === 'error') {
      rec.end = _now();
      rec.ms = rec.ms !== undefined ? rec.ms : (rec.t0 ? Math.round(rec.end - rec.t0) : null);
      el.className = 'run-node ' + (state === 'error' ? 'error' : 'done');
      var ms = el.querySelector('.rn-ms');
      if (ms && rec.ms !== null && rec.ms !== undefined) ms.textContent = rec.ms + ' ms';
    }
  });
}

function finishRunTimeline(ok) {
  if (!_runState.active) return;
  _runState.active = false;
  var total = Math.round(_now() - _runState.t0);
  var meta = document.getElementById('runMeta');
  if (meta) {
    meta.innerHTML = meta.innerHTML + '<br>总耗时 ' + total + ' ms · ' + (ok ? '成功' : '中断');
  }
  // 有 run_id 就补全真实节点耗时；没有也不报错
  var runId = _runState.runId || null;
  if (runId) { enrichTimelineFromTrace(runId); }
}

function enrichTimelineFromTrace(runId) {
  fetch('/api/trace/' + encodeURIComponent(runId))
    .then(function(r) { return r.ok ? r.json() : null; })
    .then(function(dd) {
      if (!dd || !dd.steps) return;
      dd.steps.forEach(function(s2) {
        var el = document.querySelector('#runTimeline [data-run-node="' + (s2.node || '') + '"]');
        if (!el) return;
        var ms = el.querySelector('.rn-ms');
        if (ms) ms.textContent = (Number(s2.ms) || 0) + ' ms';
        if (!el.classList.contains('active')) el.classList.add('done');
      });
      var meta = document.getElementById('runMeta');
      if (meta && dd.total_ms) meta.innerHTML += '<br>溯源 #' + escapeHtml(runId) + ' · ' + (Number(dd.total_ms) || 0) + ' ms';
    })
    .catch(function() { /* run_id 尚未落库时静默忽略 */ });
}

// 首个 token 到达：折叠而非删除思考框（保留留痕）
function collapseThinkingBox() {
  var box = document.querySelector('#thinkingMessage .thinking-box');
  if (box) {
    box.classList.add('think-collapsed');
    var spark = box.querySelector('.thinking-title .spark');
    if (spark) spark.style.animation = 'none';
  }
}

/* ============================================
   Stream Reply
   ============================================ */
/* ── P3-28：取消能力与流式 UI 状态 ───────────────────────────── */
function setStreamingUI(on) {
  isStreaming = !!on;
  var sendBtn = document.getElementById('sendBtn');
  var stopBtn = document.getElementById('stopBtn');
  var inp = document.getElementById('msgInput');
  if (sendBtn) sendBtn.disabled = !!on || !inp || inp.value.trim() === '';
  if (stopBtn) stopBtn.classList.toggle('visible', !!on);
  document.body.classList.toggle('is-streaming', !!on);
}

function stopStreaming() {
  if (currentAbortController) {
    try { currentAbortController.abort(); } catch(e) {}
    currentAbortController = null;
  }
  setStreamingUI(false);
  collapseThinkingBox();   // P3-35：保留折叠留痕，不再直接删除
  finishRunTimeline(false);
  var lastUser = document.querySelector('.message.user:last-of-type');
  if (lastUser) { /* 保留用户提问，便于重新发送 */ }
  // 若已产生部分回答，标注被用户中断
  var bubbles = document.querySelectorAll('.message.ai .msg-bubble .msg-body');
  if (bubbles.length) {
    var last = bubbles[bubbles.length - 1];
    if (last.textContent.trim() && !last.dataset.stopped) {
      last.dataset.stopped = '1';
      var tip = document.createElement('div');
      tip.className = 'msg-stopped';
      tip.textContent = '⏹ 已停止生成';
      last.appendChild(tip);
    }
  }
}

async function streamReply(userMessage) {
  if (isStreaming) return;              // P3-28：并发守卫，避免两条流互相覆盖
  setStreamingUI(true);
  currentAbortController = new AbortController();
  var signal = currentAbortController.signal;

  // 添加思考框 + P3-35 常驻运行时间轴
  addThinkingBox();
  startRunTimeline(userMessage);

  var aiBubble = null;
  var fullText = '';
  var hasStarted = false;
  var runOk = true;

  try {
    var response = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: signal,
      body: JSON.stringify({ message: userMessage, image_path: window.__pendingImagePath || null })
    });
    window.__pendingImagePath = null;  // 用后即清，避免污染后续对话
    if (!response.ok) throw new Error('HTTP ' + response.status);

    var reader = response.body.getReader();
    var decoder = new TextDecoder();
    var buffer = '';

    while (true) {
      var { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      var lines = buffer.split('\n');
      buffer = lines.pop();

      for (var i = 0; i < lines.length; i++) {
        var line = lines[i].trim();
        if (!line || !line.startsWith('data: ')) continue;

        var dataStr = line.slice(6);
        try {
          var data = JSON.parse(dataStr);

          // P3-35：后端 final 事件会带 run_id（若尚未提供则忽略，不报错）
          applyRunMetadata(data);

          if (data.status || data.agent_status) {
            updateThinkStep(data);
          }

          // 节点级流式：分支完成即显示初步结论（治"等20s"硬伤）
          if (data.partial) {
            appendPartialResult(data.partial);
          }

          if (data.token !== undefined && !data.done) {
            if (!hasStarted) {
              hasStarted = true;
              collapseThinkingBox();   // P3-35：折叠而非删除，思考记录常驻
              aiBubble = addMessage('ai', '');
              var ph = aiBubble.querySelector('.msg-body');
              if (ph) ph.innerHTML = '<span class="typing-dots"><span>·</span><span>·</span><span>·</span></span>';
            }
            fullText += data.token;
            // P3-27a：统一走 setBubbleContent —— 它内部会 formatMessage + renderLatex
            setBubbleContent(aiBubble, fullText);
            scrollToBottom();
          }

          if (data.done) {
            // P3-28：服务端已收尾，主动释放连接，避免 reader 悬挂
            try { await reader.cancel(); } catch(e) {}

            if (!hasStarted) {
              collapseThinkingBox();
              // 服务端在异常时用 {token:'[错误] ...', done:true} 下发，别丢这条信息
              var errText = (typeof data.token === 'string' && data.token) ? data.token : '';
              if (!errText) runOk = false;
              addMessage('ai', errText || '抱歉，处理您的请求时出现了问题，请稍后重试。');
            } else if (aiBubble) {
              // P3-27a：流结束时用完整文本重绘一次并渲染公式（$...$ 必须在这里落到 KaTeX）
              setBubbleContent(aiBubble, fullText);
              renderLatex(aiBubble.querySelector('.msg-body') || aiBubble);
            }
            // P3-35：收尾时间轴（有 run_id 则用 /api/trace 校准）
            _runState.runId = data.run_id || _runState.runId || null;
            finishRunTimeline(runOk);

            loadHistory();
            if (aiBubble) addSuggestionPills(aiBubble, userMessage);

            return;
          }
        } catch(e) {
          console.error('解析SSE数据失败', e, dataStr);
        }
      }
    }
  } catch(e) {
    // P3-28：用户主动停止 → 不打错误、不走降级通道
    if (e && (e.name === 'AbortError' || signal.aborted)) {
      console.info('流式请求已被用户取消');
      finishRunTimeline(false);
      loadHistory();
      return;
    }
    console.error('流式请求失败', e);
    collapseThinkingBox();
    finishRunTimeline(false);

    // 降级：用非流式API
    try {
      addMessage('ai', '流式响应中断，正在切换到备用通道...');
      var res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal: signal,
        body: JSON.stringify({ message: userMessage })
      });
      var data = await res.json();
      addMessage('ai', data.response || '抱歉，出现了错误。');
    } catch(e2) {
      if (e2 && e2.name === 'AbortError') return;
      addMessage('ai', '网络连接失败，请检查后端服务是否启动。');
    }

    loadHistory();
  } finally {
    currentAbortController = null;
    setStreamingUI(false);
  }
}

/* ============================================
   Scroll
   ============================================ */
function scrollToBottom() {
  var container = document.getElementById('chatContainer');
  var reducedMotion = document.documentElement.dataset.motion === 'reduced' ||
    (!document.documentElement.dataset.motion && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  container.scroll({top: container.scrollHeight, behavior: reducedMotion ? 'auto' : 'smooth'});
}

var scrollBtn = document.getElementById('scrollBottomBtn');
if (scrollBtn) {
  var chatEl = document.getElementById('chatContainer');
  chatEl.addEventListener('scroll', function() {
    var dist = chatEl.scrollHeight - chatEl.scrollTop - chatEl.clientHeight;
    scrollBtn.classList.toggle('visible', dist > 200);
  });
  scrollBtn.addEventListener('click', scrollToBottom);
}
