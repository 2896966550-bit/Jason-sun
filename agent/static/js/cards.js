/* Equis · 清洗 / 卡片 / 视图 / 检查器（P3-37 由 equis.html 拆出，普通 <script>/<link>，无构建工具） */

/* ============================================
   P3-31 安全：本地转义 + HTML 白名单清洗
   （marked v11 已移除 sanitize 选项；不引入任何境外 CDN，
     第三方库一律本地化到 /static/）
   ============================================ */
function escapeHtml(s) {
  return String(s === null || s === undefined ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
var escapeAttr = escapeHtml;

// 数字强转：LLM 返回的字段可能是字符串甚至 HTML，绝不允许进 DOM
function num(v, digits, fallback) {
  if (v === null || v === undefined || v === '' || typeof v === 'boolean') return fallback === undefined ? '—' : fallback;
  var n = Number(v);
  if (!isFinite(n)) return fallback === undefined ? '—' : fallback;
  var places = digits === undefined ? 4 : digits;
  return n !== 0 && Math.abs(n) < Math.pow(10, -places) ? n.toExponential(2) : n.toFixed(places);
}

var ALLOWED_TAGS = {
  A: ['href', 'title', 'target', 'rel'], P: [], BR: [], HR: [], STRONG: [], EM: [], B: [], I: [],
  U: [], S: [], DEL: [], INS: [], CODE: ['class'], PRE: [], BLOCKQUOTE: [], UL: [], OL: ['start'],
  LI: [], H1: [], H2: [], H3: [], H4: [], H5: [], H6: [], TABLE: [], THEAD: [], TBODY: [],
  TR: [], TH: ['align'], TD: ['align'], SPAN: ['class'], DIV: ['class'], IMG: ['src', 'alt', 'title', 'width', 'height'],
  SUP: [], SUB: [], KBD: [], DL: [], DT: [], DD: [], FIGURE: [], FIGCAPTION: []
};

function sanitizeHtml(html) {
  var tpl = document.createElement('template');
  tpl.innerHTML = String(html === null || html === undefined ? '' : html);
  var els = tpl.content.querySelectorAll('*');
  for (var i = els.length - 1; i >= 0; i--) {
    var el = els[i];
    var tag = el.tagName;
    if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'IFRAME' || tag === 'OBJECT' ||
        tag === 'EMBED' || tag === 'FORM' || tag === 'LINK' || tag === 'META' ||
        tag === 'BASE' || tag === 'SVG' || tag === 'MATH' || tag === 'AUDIO' || tag === 'VIDEO') {
      if (el.parentNode) el.parentNode.removeChild(el);
      continue;
    }
    if (!Object.prototype.hasOwnProperty.call(ALLOWED_TAGS, tag)) {
      // 非白名单标签 → 只保留文字
      if (el.parentNode) el.parentNode.replaceChild(document.createTextNode(el.textContent || ''), el);
      continue;
    }
    var allowed = ALLOWED_TAGS[tag];
    for (var j = el.attributes.length - 1; j >= 0; j--) {
      var at = el.attributes[j];
      var name = String(at.name || '').toLowerCase();
      if (allowed.indexOf(name) < 0) { el.removeAttribute(at.name); continue; }
      var val = String(at.value || '').replace(/[\u0000-\u0020]/g, '').toLowerCase();
      if ((name === 'href' || name === 'src') && /^(javascript|vbscript|data):/.test(val)) {
        el.removeAttribute(at.name);
      }
    }
    if (tag === 'A') {
      el.setAttribute('rel', 'noopener noreferrer');
      if (!el.getAttribute('target')) el.setAttribute('target', '_blank');
    }
  }
  return tpl.innerHTML;
}

/* ============================================
   Message Formatting
   ============================================ */
function formatMessage(text) {
  if (typeof marked === 'undefined') {
    return escapeHtml(text).replace(/\n/g, '<br>');
  }
  marked.setOptions({ breaks: true, gfm: true });
  var html = marked.parse(String(text === null || text === undefined ? '' : text));
  // Post-process: render anomaly-card / compliance-card code blocks as styled cards
  var temp = document.createElement('div');
  temp.innerHTML = html;
  // P3-31：marked 输出先过本地白名单清洗，再替换卡片
  temp.innerHTML = sanitizeHtml(temp.innerHTML);
  // Anomaly detection cards
  var anomalyCards = temp.querySelectorAll('code.language-anomaly-card');
  anomalyCards.forEach(function(codeEl) {
    try {
      var data = JSON.parse(codeEl.textContent);
      var cardHtml = renderAnomalyCard(data);
      var pre = codeEl.closest('pre');
      if (pre) {
        var wrapper = document.createElement('div');
        wrapper.innerHTML = cardHtml;
        // P3-36：把原始 JSON 挂在卡片上，供下钻展示
        if (wrapper.firstElementChild) { wrapper.firstElementChild.__cardData = data; wrapper.firstElementChild.setAttribute('data-card-json', JSON.stringify(data)); }
        pre.replaceWith(wrapper.firstElementChild);
      }
    } catch(e) { /* JSON parse failed, leave as code block */ }
  });
  // Vision understanding cards（多模态图像理解）
  var visionCards = temp.querySelectorAll('code.language-vision-card');
  visionCards.forEach(function(codeEl) {
    try {
      var data = JSON.parse(codeEl.textContent);
      var cardHtml = renderVisionCard(data);
      var pre = codeEl.closest('pre');
      if (pre) {
        var wrapper = document.createElement('div');
        wrapper.innerHTML = cardHtml;
        if (wrapper.firstElementChild) { wrapper.firstElementChild.__cardData = data; wrapper.firstElementChild.setAttribute('data-card-json', JSON.stringify(data)); }
        pre.replaceWith(wrapper.firstElementChild);
      }
    } catch(e) { /* JSON parse failed, leave as code block */ }
  });
  // Compliance decision cards
  var compCards = temp.querySelectorAll('code.language-compliance-card');
  compCards.forEach(function(codeEl) {
    try {
      var data = JSON.parse(codeEl.textContent);
      var cardHtml = renderComplianceCard(data);
      var pre = codeEl.closest('pre');
      if (pre) {
        var wrapper = document.createElement('div');
        wrapper.innerHTML = cardHtml;
        if (wrapper.firstElementChild) { wrapper.firstElementChild.__cardData = data; wrapper.firstElementChild.setAttribute('data-card-json', JSON.stringify(data)); }
        pre.replaceWith(wrapper.firstElementChild);
      }
    } catch(e) { /* JSON parse failed, leave as code block */ }
  });
  // Sensor data — update panel
  var sensorBlocks = temp.querySelectorAll('code.language-sensor-data');
  sensorBlocks.forEach(function(codeEl) {
    try {
      var data = JSON.parse(codeEl.textContent);
      updateSensorPanel(data);
      codeEl.closest('pre').remove();
    } catch(e) { /* ignore */ }
  });
  return temp.innerHTML;
}

function renderVisionCard(data) {
  if (!data || data.error) {
    return '<div class="vision-card"><div class="vision-card-header">图像理解失败</div>'
      + '<div class="vision-card-body"><div class="vision-card-row"><span class="label">原因</span>'
      + '<span class="value">' + escapeHtml((data && data.error) || '未知错误') + '</span></div></div></div>';
  }
  var obj = escapeHtml(data.object || '未识别');
  var material = escapeHtml(data.material_hint && data.material_hint !== 'unknown' ? data.material_hint : '—');
  var surface = escapeHtml(data.surface_state || '—');
  var defects = data.visible_defects || [];
  var feats = data.measurable_features || [];
  var suggestion = escapeHtml(data.inspection_suggestion || '—');
  var conf = (typeof data.confidence === 'number' && isFinite(data.confidence)) ? data.confidence : null;

  var defectHtml = defects.length
    ? '<div class="vision-tags">' + defects.map(function(d){
        return '<span class="vision-tag defect">' + escapeHtml(d) + '</span>'; }).join('') + '</div>'
    : '<span class="value ok">未发现明显缺陷</span>';
  var featHtml = feats.length
    ? '<div class="vision-tags">' + feats.map(function(f){
        return '<span class="vision-tag">' + escapeHtml(f) + '</span>'; }).join('') + '</div>'
    : '<span class="value">—</span>';

  return '<div class="vision-card" data-card="vision" role="region" aria-label="图像理解结果">'
    + '<div class="vision-card-header">图像理解<button class="card-drill" type="button" data-card-detail>查看详情 ›</button></div>'
    + '<div class="vision-card-body">'
    + '<div class="vision-card-row"><span class="label">识别对象</span><span class="value">' + obj + '</span></div>'
    + '<div class="vision-card-row"><span class="label">材质推测</span><span class="value">' + material + '</span></div>'
    + '<div class="vision-card-row"><span class="label">表面状态</span><span class="value">' + surface + '</span></div>'
    + '<div class="vision-card-row"><span class="label">可见缺陷</span>' + defectHtml + '</div>'
    + '<div class="vision-card-row"><span class="label">可测特征</span>' + featHtml + '</div>'
    + '<div class="vision-card-row"><span class="label">检测建议</span><span class="value">' + suggestion + '</span></div>'
    + (conf !== null ? '<div class="vision-card-row"><span class="label">置信度</span><span class="value">'
        + (conf * 100).toFixed(0) + '%</span></div>' : '')
    + '</div></div>';
}

function assessmentPresentation(data, kind) {
  data = data || {};
  var status = data.execution_status;
  var message = data.error && typeof data.error === 'object' ? data.error.message : data.error;
  if ((status && status !== 'success') || (kind === 'anomaly' && data.success !== true)) {
    var names = {needs_input:'缺少输入', invalid_input:'输入无效', unsupported:'尚不支持', unavailable:'无法评定'};
    return {cls:'unavailable', title:names[status] || '结果不可用', message:message || data.reason || '本次未完成有效评定'};
  }
  var finite = function(value) { return typeof value === 'number' && isFinite(value); };
  if (kind === 'anomaly') {
    if (data.decision_execution_status === 'needs_calibration' && finite(data.anomaly_score) && data.class_used) {
      return {cls:'neutral', title:'视觉分数：阈值待标定', message:data.verdict || '仅返回分数，本次不作正常/异常二分结论'};
    }
    if (!finite(data.anomaly_score) || typeof data.is_anomaly !== 'boolean' || !data.class_used) {
      return {cls:'unavailable', title:'检测结果不完整', message:'缺少有效分数、类别或检测结论，请重新检测'};
    }
    return {cls:data.is_anomaly ? 'fail' : 'pass',
      title:data.is_anomaly ? '视觉偏离：已检出' : '视觉偏离：未超过阈值', message:data.verdict || ''};
  }
  if (!finite(data.U) || data.U <= 0 || !finite(data.measured_value)) {
    return {cls:'unavailable', title:'评定结果不完整', message:'缺少有效测量值或不确定度，请重新计算'};
  }
  if (data.assessment_type === 'uncertainty_only') {
    return {cls:'neutral', title:'不确定度预算估计', message:data.reason || '本次未执行参考一致性比较'};
  }
  var names = {pass:'参考一致性：满足工程规则', fail:'参考一致性：不一致', indeterminate:'参考一致性：无法确定'};
  if (!names[data.verdict] || !finite(data.reference_value) || !finite(data.deviation)) {
    return {cls:'unavailable', title:'缺少有效三态结论', message:'旧结果或不完整结果需要重新计算'};
  }
  return {cls:data.verdict, title:names[data.verdict], message:data.reason || ''};
}

function resultMetric(label, value, unit) {
  return '<div class="result-metric"><span>' + escapeHtml(label) + '</span><strong>'
    + escapeHtml(value) + (unit ? '<small>' + escapeHtml(unit) + '</small>' : '') + '</strong></div>';
}

function resultNextStep(p, data, kind) {
  var text;
  if (p.cls === 'unavailable') {
    text = data.execution_status === 'needs_input' || data.execution_status === 'invalid_input'
      ? '补充或修正输入后重新分析，具体缺项见上方原因。' : '核对输入与设备、模型状态后重新分析。';
  } else if (kind === 'anomaly') {
    text = data.decision_execution_status === 'needs_calibration'
      ? '完成对应类别的阈值标定后，再进行正常 / 异常判别。' : '结合原图与参考测量复核，视觉结论不代表尺寸合格。';
  } else {
    text = data.assessment_type === 'uncertainty_only'
      ? '补齐重复性与参考值不确定度等证据，完善测量预算。' : '核对参考值来源与不确定度；工件合格还需公差与相应判定规则。';
  }
  return '<div class="result-next"><span>下一步</span><p>' + escapeHtml(text) + '</p></div>';
}

function renderAnomalyCard(data) {
  data = data || {};
  var p = assessmentPresentation(data, 'anomaly');
  var html = '<div class="anomaly-card ' + p.cls + '" data-card="anomaly" data-card-json="' + escapeAttr(JSON.stringify(data)) + '" role="region" aria-label="视觉分析结果">'
    + '<div class="anomaly-card-header"><span>' + escapeHtml(p.title) + '</span><button class="card-drill" type="button" data-card-detail>查看详情 ›</button></div>'
    + '<div class="anomaly-card-body"><div class="assessment-reason">' + escapeHtml(p.message) + '</div>';
  if (p.cls !== 'unavailable') {
    html += '<div class="result-metrics">' + resultMetric('工件类别', data.class_used, '')
      + resultMetric('异常分数', num(data.anomaly_score,4), '')
      + resultMetric('判别阈值', num(data.threshold,4, '待标定'), '') + '</div>';
  }
  return html + resultNextStep(p, data, 'anomaly') + '</div></div>';
}

function renderComplianceCard(data) {
  data = data || {};
  var p = assessmentPresentation(data, 'compliance');
  var html = '<div class="compliance-card ' + p.cls + '" data-card="compliance" data-card-json="' + escapeAttr(JSON.stringify(data)) + '" role="region" aria-label="测量数值分析结果">'
    + '<div class="compliance-card-header"><span>' + escapeHtml(p.title) + '</span><button class="card-drill" type="button" data-card-detail>查看详情 ›</button></div>'
    + '<div class="compliance-card-body"><div class="assessment-reason">' + escapeHtml(p.message) + '</div>';
  if (p.cls === 'unavailable') return html + resultNextStep(p, data, 'compliance') + '</div></div>';
  html += '<div class="result-metrics">' + resultMetric('实测值', num(data.measured_value,4), 'mm');
  if (data.assessment_type !== 'uncertainty_only') {
    html += resultMetric('参考值', num(data.reference_value,4), 'mm') + resultMetric('偏差 d', num(data.deviation,4), 'mm');
  }
  html += resultMetric('扩展不确定度 U', num(data.U,4), 'mm') + '</div>';
  html += '<details class="assessment-details"><summary>查看预算与判定依据</summary><div class="compliance-budget">' + budgetRowsHtml(data) + '</div>';
  if (data.decision_bounds && data.assessment_type !== 'uncertainty_only') {
    html += '<p class="assessment-rule">基础一致范围：|d| ≤ ' + num(data.decision_bounds.pass_abs_mm,6) + ' mm；不一致边界：|d| > '
      + num(data.decision_bounds.fail_abs_mm,6) + ' mm。其他门控条件见判定依据。</p>';
  }
  if (data.ood_policy === 'disabled_unvalidated_mapping') {
    html += '<p class="assessment-rule">OOD 毫米映射未验证，本次未纳入预算；测量风险未评估。</p>';
  }
  if (data.event_id) html += '<p class="assessment-rule">测量事件：' + escapeHtml(data.event_id) + '</p>';
  html += '</details>';
  return html + resultNextStep(p, data, 'compliance') + '</div></div>';
}

function updateSensorPanel(data) {
  var panel = document.getElementById('sensorPanel');
  var status = document.getElementById('sensorStatus');
  if (!panel || !data) return;
  var t = data.temp, h = data.hum, vib = data.vib;
  if (typeof t === 'number' && isFinite(t)) {
    var el = document.getElementById('sensorTemp').querySelector('.sensor-val');
    el.textContent = t.toFixed(1);
    document.getElementById('sensorTemp').className = 'sensor-item' + (t > 80 ? ' bad' : t > 60 ? ' warn' : '');
  }
  if (typeof h === 'number' && isFinite(h)) {
    var el = document.getElementById('sensorHum').querySelector('.sensor-val');
    el.textContent = h.toFixed(1);
    document.getElementById('sensorHum').className = 'sensor-item' + (h > 90 ? ' warn' : '');
  }
  if (typeof vib === 'number' && isFinite(vib)) {
    var el = document.getElementById('sensorVib').querySelector('.sensor-val');
    el.textContent = vib.toFixed(2);
    document.getElementById('sensorVib').className = 'sensor-item' + (vib > 12.0 ? ' bad' : vib > 9.8*1.2 ? ' warn' : '');
  }
  if (status) {
    status.textContent = data.source === 'bound_sensor' ? '本事件传感器快照' : '用户输入读数';
    status.className = 'sensor-status';
  }
}

/* P3-32：系统面板由模态升级为 #/system 视图，这里保留同名入口以便旧调用继续工作 */
function openSystemPanel() { navigateTo('system'); }
function closeSystemPanel() { navigateTo('chat'); }
function switchPanelTab(name) {
  document.querySelectorAll('#viewSystem .ptab').forEach(function(b){
    b.classList.toggle('active', b.getAttribute('data-tab') === name);
  });
  var panes = { arch: 'paneArch', hist: 'paneHist', compliance: 'paneCompliance' };
  Object.keys(panes).forEach(function(k){
    var el = document.getElementById(panes[k]);
    if (el) el.style.display = (k === name) ? 'block' : 'none';
  });
  if (name === 'arch') { loadArchitecture(); }
  else if (name === 'hist') { loadTraceHistory(); }
  else { loadCompliance(); }
}
var _archLoaded = false;
var _mermaidPromise = null;

/* P3-37：mermaid.min.js 有 3.3MB，改为首次打开架构面板时才按需加载 */
function ensureMermaid() {
  if (window.mermaid) return Promise.resolve(window.mermaid);
  if (_mermaidPromise) return _mermaidPromise;
  _mermaidPromise = new Promise(function(resolve, reject) {
    var s = document.createElement('script');
    s.src = '/static/mermaid.min.js';
    s.async = true;
    s.onload = function() { window.mermaid ? resolve(window.mermaid) : reject(new Error('mermaid 未就绪')); };
    s.onerror = function() { _mermaidPromise = null; reject(new Error('mermaid 加载失败')); };
    document.head.appendChild(s);
  });
  return _mermaidPromise;
}

function renderMermaidInto(box, code) {
  ensureMermaid().then(function(mermaid) {
    // P3-31：strict（默认）安全级别。
    //
    // 2026-10-17 修复：原先只切 theme:'dark'/'default'，但 mermaid 生成的 SVG
    // 会在内部 <style> 里写死节点底色（实测深色下出现近白色 #f2f0ff 填充），
    // 而文字是浅色 —— 架构图变成白底白字，几乎不可读。
    // 用外部 CSS 覆盖也压不过它（mermaid 的内联样式带 #id 前缀 + !important，
    // 特异性更高）。
    // 正确做法是**从源头配置**：theme:'base' + themeVariables，
    // 让 mermaid 自己生成正确的配色，不再需要 CSS 打架。
    var isDark = document.documentElement.getAttribute('data-theme') === 'dark';
    var vars = isDark ? {
      darkMode: true,
      background: 'transparent',
      primaryColor: '#14171d',
      primaryTextColor: '#e9ecf2',
      primaryBorderColor: '#8b8cff',
      secondaryColor: '#1b1f27',
      tertiaryColor: '#101217',
      lineColor: '#9aa3b2',
      textColor: '#e9ecf2',
      mainBkg: '#14171d',
      nodeBorder: '#8b8cff',
      clusterBkg: '#101217',
      clusterBorder: 'rgba(255,255,255,0.12)',
      edgeLabelBackground: '#0a0b0e',
      fontSize: '14px'
    } : {
      darkMode: false,
      background: 'transparent',
      primaryColor: '#ffffff',
      primaryTextColor: '#14171d',
      primaryBorderColor: '#5155d8',
      lineColor: '#6b7280',
      textColor: '#14171d',
      mainBkg: '#ffffff',
      edgeLabelBackground: '#f4f5f8',
      fontSize: '14px'
    };
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: 'strict',
      theme: 'base',
      themeVariables: vars,
      flowchart: { curve: 'basis', padding: 12, htmlLabels: true }
    });
    return Promise.resolve(mermaid.render('archGraph' + Date.now(), code)).then(function(res) {
      // mermaid strict 模式内部已用 DOMPurify 清洗，这里仅取 svg 节点
      box.innerHTML = (res && res.svg) ? res.svg : String(res || '');
      // ── 渲染后强制校正配色（2026-10-17）──────────────────────
      // 实测：只靠 CSS 压不住 mermaid 自己生成的内联规则
      // （`.default>*{fill:#f2f0ff!important}` 把节点刷成近白，
      //   文字 #ccc → 白底白字，架构图几乎不可读）。
      // 这里直接写**带 important 的内联样式**（style.setProperty(...,'important')）
      // —— 内联 + important 的优先级高于任何样式表规则，是唯一稳妥的做法。
      try {
        var nodeFill = isDark ? '#14171d' : '#ffffff';
        var nodeStroke = isDark ? 'rgba(139,140,255,0.55)' : 'rgba(81,85,216,0.45)';
        var labelColor = isDark ? '#e9ecf2' : '#14171d';
        box.querySelectorAll('.node > rect, .node > polygon, .node > circle, .node > ellipse')
          .forEach(function (el) {
            el.style.setProperty('fill', nodeFill, 'important');
            el.style.setProperty('stroke', nodeStroke, 'important');
            el.style.setProperty('stroke-width', '1px', 'important');
          });
        box.querySelectorAll('.node .nodeLabel, .node span, .node p, .node .label')
          .forEach(function (el) {
            el.style.setProperty('color', labelColor, 'important');
            el.style.setProperty('fill', labelColor, 'important');
          });
        // 起止节点给一点强调，形成视觉锚点
        box.querySelectorAll('.node.first > rect, .node.last > rect').forEach(function (el) {
          el.style.setProperty('fill', isDark ? 'rgba(139,140,255,0.16)' : 'rgba(81,85,216,0.08)', 'important');
        });
      } catch (e) { /* 配色校正失败不影响显示 */ }
      box.dataset.loaded = '1';
      _archLoaded = true;
    });
  }).catch(function() {
    box.innerHTML = '<pre style="text-align:left">' + escapeHtml(code) + '</pre>';
  });
}

/* ── B3：架构图自绘 SVG（替代 mermaid）─────────────────────────
   为什么换掉 mermaid：
     · 它带来 2.8MB 依赖（mermaid.min.js）；
     · 深色主题适配反复出问题 —— 它会在生成的内联样式里写死浅色填充
       （`.default>*{fill:#f2f0ff!important}`），外部 CSS 压不住，
       只能靠渲染后再写内联 !important 去纠正，非常脆弱；
     · 我们只需要画一张固定形态的流程图。
   现在：解析 /api/architecture 返回的 mermaid 文本 → 自绘 SVG，
        配色走 CSS 变量，天然跟随明暗主题，且零第三方依赖。 */

function parseMermaidFlow(code) {
  var nodes = {};          // id -> label
  var order = [];
  var edges = [];          // {from, to, dotted}
  var lines = String(code || '').split('\n');
  lines.forEach(function (raw) {
    var line = raw.trim().replace(/;$/, '');
    if (!line) return;
    // 跳过 YAML front-matter 与图声明/样式类
    // （实测：`curve: linear` 曾被误当成一个节点）
    if (line === '---' || /^graph\s|^flowchart\s|^classDef\s|^class\s/.test(line)) return;
    if (line.indexOf('-->') === -1 && line.indexOf('-.->') === -1) {
      if (line.indexOf(':') !== -1) return;             // key: value
      if (!/^\w+\s*[\(\[\{]/.test(line)) return;         // 不是节点定义
      var idm = line.match(/^(\w+)/);
      if (!idm) return;
      var id = idm[1];
      var rest = line.slice(id.length).trim();
      // 去掉包裹符号：([...]) / (...) / [...] / {...}，并剥离 mermaid 的 <p>
      var label = rest.replace(/^[\(\[\{]+/, '').replace(/[\)\]\}]+$/, '')
                      .replace(/<p>|<\/p>/g, '').trim() || id;
      if (!nodes[id]) { nodes[id] = label; order.push(id); }
      return;
    }
    // 边：A --> B 或 A -.-> B
    var e = line.match(/^(\w+)\s*(-\.->|-->)\s*(\w+)/);
    if (e) {
      edges.push({ from: e[1], to: e[3], dotted: e[2] === '-.->' });
      if (!nodes[e[1]]) { nodes[e[1]] = e[1]; order.push(e[1]); }
      if (!nodes[e[3]]) { nodes[e[3]] = e[3]; order.push(e[3]); }
    }
  });
  return { nodes: nodes, order: order, edges: edges };
}

function renderArchSvg(box, code) {
  var g = parseMermaidFlow(code);
  if (!g.order.length) return false;

  // 计算层级：从入度为 0 的节点开始 BFS，取最长路径层级
  var level = {};
  var indeg = {};
  g.order.forEach(function (n) { indeg[n] = 0; level[n] = 0; });
  g.edges.forEach(function (e) { indeg[e.to] = (indeg[e.to] || 0) + 1; });
  var roots = g.order.filter(function (n) { return !indeg[n]; });
  if (!roots.length) roots = [g.order[0]];
  var queue = roots.slice();
  var guard = 0;
  while (queue.length && guard++ < 500) {
    var cur = queue.shift();
    g.edges.filter(function (e) { return e.from === cur; }).forEach(function (e) {
      var cand = (level[cur] || 0) + 1;
      if (cand > (level[e.to] || 0)) { level[e.to] = cand; queue.push(e.to); }
    });
  }

  // 按层分组，保持原始顺序
  var byLevel = {};
  g.order.forEach(function (n) {
    var l = level[n] || 0;
    (byLevel[l] = byLevel[l] || []).push(n);
  });
  var levels = Object.keys(byLevel).map(Number).sort(function (a, b) { return a - b; });

  var NW = 112, NH = 32, HGAP = 26, VGAP = 46, PAD = 16;
  var maxW = 0;
  levels.forEach(function (l) { maxW = Math.max(maxW, byLevel[l].length); });
  var width = Math.max(maxW * (NW + HGAP) - HGAP, 420);
  var height = levels.length * (NH + VGAP) - VGAP;

  var pos = {};
  levels.forEach(function (l, li) {
    var arr = byLevel[l];
    var rowW = arr.length * (NW + HGAP) - HGAP;
    var x0 = (width - rowW) / 2;
    arr.forEach(function (n, i) {
      pos[n] = {
        x: x0 + i * (NW + HGAP) + NW / 2,
        y: li * (NH + VGAP) + NH / 2
      };
    });
  });

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  var parts = [];
  parts.push('<svg class="arch-svg" viewBox="0 0 ' + (width + PAD * 2) + ' ' + (height + PAD * 2) +
             '" width="100%" role="img" aria-label="智能体流程图" xmlns="http://www.w3.org/2000/svg">');
  parts.push('<g transform="translate(' + PAD + ',' + PAD + ')">');

  // 先画边（在节点下方）
  g.edges.forEach(function (e) {
    var a = pos[e.from], b = pos[e.to];
    if (!a || !b) return;
    var y1 = a.y + NH / 2, y2 = b.y - NH / 2;
    var mid = (y1 + y2) / 2;
    var d = 'M' + a.x + ',' + y1 + ' C' + a.x + ',' + mid + ' ' + b.x + ',' + mid + ' ' + b.x + ',' + y2;
    parts.push('<path class="arch-edge' + (e.dotted ? ' is-optional' : '') +
               '" d="' + d + '" fill="none"/>');
  });

  // 再画节点
  g.order.forEach(function (n) {
    var p = pos[n];
    if (!p) return;
    var isTerminal = /^__.*__$/.test(n);
    var cls = 'arch-node' + (isTerminal ? ' is-terminal' : '');
    var label = String(g.nodes[n] || n).replace(/^__|__$/g, '');
    parts.push('<g class="' + cls + '" data-node="' + esc(n) + '">');
    if (isTerminal) {
      parts.push('<rect x="' + (p.x - 52) + '" y="' + (p.y - NH / 2) + '" width="104" height="' + NH +
                 '" rx="' + (NH / 2) + '"/>');
    } else {
      parts.push('<rect x="' + (p.x - NW / 2) + '" y="' + (p.y - NH / 2) + '" width="' + NW +
                 '" height="' + NH + '" rx="8"/>');
    }
    parts.push('<text x="' + p.x + '" y="' + (p.y + 4) + '" text-anchor="middle">' + esc(label) + '</text>');
    parts.push('</g>');
  });

  parts.push('</g></svg>');
  box.innerHTML = parts.join('');
  box.dataset.loaded = '1';
  box.dataset.renderer = 'svg';
  return true;
}

function loadArchitecture() {
  var box = document.getElementById('mermaidBox');
  if (!box) return;
  if (_archLoaded && box.dataset.loaded === '1') return;
  box.innerHTML = '加载中…';
  fetch('/api/architecture').then(function(r){ return r.json(); }).then(function(d){
    // B3：优先自绘 SVG；解析失败才退回 mermaid（兜底，不让架构图空白）
    var ok = false;
    try { ok = renderArchSvg(box, d.mermaid || ''); } catch (e) { ok = false; }
    if (!ok) renderMermaidInto(box, d.mermaid || '');
  }).catch(function(e){ box.textContent = '加载失败: ' + e; });
}
/* ============================================
   P3-32 视图数据加载（标准库 / 案例 / 工作台 / 合规自检）
   ============================================ */
function traceItemHtml(x, dd) {
  var steps = ((dd && dd.steps) || []).map(function(s2) {
    var ms = Number(s2.ms) || 0;
    var pct = (dd && dd.total_ms) ? Math.min(100, Math.round(ms / dd.total_ms * 100)) : 0;
    // P3-31：节点名与用户提问都必须转义（原来直接拼 innerHTML）
    return '<div class="tr-step"><span class="tr-node">' + escapeHtml(s2.label || s2.node) + '</span>'
      + '<div class="tr-bar"><div style="width:' + pct + '%"></div></div>'
      + '<span class="tr-ms">' + ms + 'ms</span></div>';
  }).join('');
  return '<div class="trace-item clickable" data-run="' + escapeHtml(x.id) + '" title="点击查看逐节点详情">'
    + '<div class="trace-head"><span>#' + escapeHtml(x.id) + ' ' + escapeHtml(String(x.query || '').slice(0, 30)) + '</span>'
    + '<span>' + (Number(x.total_ms) || 0) + 'ms ' + '' + '</span></div>'
    + steps + '</div>';
}

function renderTraceList(boxId, opts) {
  opts = opts || {};
  var box = document.getElementById(boxId);
  if (!box) return;
  box.innerHTML = '加载中…';
  fetch('/api/trace?n=' + (opts.n || 12)).then(function(r){ return r.json(); }).then(function(d){
    var runs = d.runs || [];
    if (!runs.length) { box.innerHTML = '<div class="trace-item">暂无运行记录</div>'; return; }
    return Promise.all(runs.map(function(x) {
      return fetch('/api/trace/' + encodeURIComponent(x.id))
        .then(function(r){ return r.json(); })
        .then(function(dd) { return traceItemHtml(x, dd); })
        .catch(function() { return traceItemHtml(x, null); });
    })).then(function(htmls) { box.innerHTML = htmls.join(''); });
  }).catch(function(e) {
    box.innerHTML = '加载失败: ' + escapeHtml((e && e.message) || e);
  });
}

function loadTraceHistory() { renderTraceList('traceList', { n: 20 }); }
function loadCasesView() { renderTraceList('casesList', { n: 20 }); }
function loadWorkspaceRuns() { renderTraceList('workspaceRuns', { n: 5 }); }

/* ── 运行详情（钻取目标；浏览器后退可回到列表）── */
function renderCaseDetail(id) {
  var box = document.getElementById('casesList');
  if (!box) return;
  box.innerHTML = '加载中…';
  fetch('/api/trace/' + encodeURIComponent(id)).then(function(r){ return r.json(); }).then(function(dd) {
    var steps = (dd.steps || []);
    var rows = steps.map(function(s2) {
      var ms = Number(s2.ms) || 0;
      var pct = dd.total_ms ? Math.min(100, Math.round(ms / dd.total_ms * 100)) : 0;
      return '<div class="tr-step"><span class="tr-node">' + escapeHtml(s2.label || s2.node) + '</span>'
        + '<div class="tr-bar"><div style="width:' + pct + '%"></div></div>'
        + '<span class="tr-ms">' + ms + 'ms</span></div>';
    }).join('');
    box.innerHTML = '<button class="ghost-btn" id="caseBackBtn">‹ 返回列表</button>'
      + '<div class="trace-item" style="margin-top:12px"><div class="trace-head">'
      + '<span>#' + escapeHtml(id) + ' ' + escapeHtml(String(dd.query || '').slice(0, 40)) + '</span>'
      + '<span>' + (Number(dd.total_ms) || 0) + 'ms</span></div>' + (rows || '无节点记录') + '</div>';
    var back = document.getElementById('caseBackBtn');
    if (back) back.addEventListener('click', function() { history.back(); });
    showInspector('运行 #' + id + ' 详情', runDetailHtml(dd, id));
  }).catch(function(e) {
    box.innerHTML = '加载失败: ' + escapeHtml((e && e.message) || e);
  });
}

function runDetailHtml(dd, id) {
  var steps = (dd && dd.steps) || [];
  var rows = steps.map(function(s2) {
    return '<div class="cr-row"><span class="k">' + escapeHtml(s2.label || s2.node) + '</span>'
      + '<span class="v">' + (Number(s2.ms) || 0) + ' ms</span></div>';
  }).join('');
  return '<div class="run-meta">运行 #' + escapeHtml(id) + ' · 总耗时 '
    + (Number(dd && dd.total_ms) || 0) + ' ms · ' + (steps.length) + ' 个节点'
    + (dd && dd.note ? ' · ' + escapeHtml(dd.note) : '') + '</div>'
    + '<div class="cr-card">' + (rows || '<div class="cr-row"><span class="k">无节点记录</span></div>') + '</div>'
    + '<h4 class="view-h3">原始 JSON</h4><pre class="raw-json">' + escapeHtml(JSON.stringify(dd, null, 2).slice(0, 4000)) + '</pre>';
}

/* ── 标准库 / 历史提问（/api/history）── */
function loadStandardsView(force) {
  var box = document.getElementById('standardsList');
  if (!box) return;
  if (box.dataset.loaded === '1' && !force) return;
  box.innerHTML = '加载中…';
  fetch('/api/history').then(function(r){ return r.json(); }).then(function(d) {
    var qs = d.questions || [];
    box.innerHTML = '';
    if (!qs.length) { box.innerHTML = '<div class="trace-item">暂无历史提问</div>'; return; }
    qs.forEach(function(q, i) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'std-card';
      var qd = document.createElement('div');
      qd.className = 'std-q';
      qd.textContent = q;
      var md = document.createElement('div');
      md.className = 'std-meta';
      var s1 = document.createElement('span'); s1.textContent = '#' + (i + 1);
      var s2 = document.createElement('span'); s2.textContent = '点击重跑 →';
      md.appendChild(s1); md.appendChild(s2);
      b.appendChild(qd); b.appendChild(md);
      b.addEventListener('click', function() { navigateTo('chat'); loadConversation(q); });
      box.appendChild(b);
    });
    box.dataset.loaded = '1';
  }).catch(function(e) {
    box.innerHTML = '加载失败: ' + escapeHtml((e && e.message) || e);
  });
}

/* ── 数据合规自检（/api/compliance）——"数据不出境"卖点 ── */
var NODE_LABELS_CN = {
  perceive: '感知采集', supervisor: '调度器', recall: '记忆召回', retrieval: '规元 · 标准检索',
  vision: '度元 · 视觉理解', compute: '度元 · 计算评定', plan_execute: '规划执行', web: '索元 · 联网检索',
  judge: '合规判定', merge: '结果融合'
};

function loadCompliance(force) {
  var box = document.getElementById('complianceBox');
  var chip = document.getElementById('residencyChip');
  if (!box) return;
  if (box.dataset.loaded === '1' && !force) return;
  box.innerHTML = '检查中…';
  fetch('/api/compliance').then(function(r){ return r.json(); }).then(function(d) {
    box.innerHTML = complianceHtml(d);
    box.dataset.loaded = '1';
    var ok = !(d && d.data_residency && d.data_residency.ok === false);
    if (chip) {
      chip.textContent = ok ? '数据不出境' : '出境风险';
      chip.className = 'cr-badge ' + (ok ? 'pass' : 'fail');
    }
  }).catch(function(e) {
    box.innerHTML = '自检失败: ' + escapeHtml((e && e.message) || e);
    if (chip) { chip.textContent = '自检失败'; chip.className = 'cr-badge fail'; }
  });
}

function complianceHtml(d) {
  d = d || {};
  var res = d.data_residency || {};
  var ok = res.ok !== false;
  var hosts = (res.allowed_hosts || []).map(function(h) { return escapeHtml(h); }).join(' · ');
  var face = d.face_redaction || {};
  var audit = d.audit || {};
  var privacy = d.privacy || {};
  return '<div class="cr-card">'
      + '<h4>数据出境自检 <span class="cr-badge ' + (ok ? 'pass' : 'fail') + '">'
      + (ok ? '通过 · 数据不出境' : '未通过') + '</span></h4>'
      + '<div class="cr-row"><span class="k">模型调用域名</span><span class="v">' + escapeHtml(res.llm_base_url_host || '—') + '</span></div>'
      + '<div class="cr-row"><span class="k">境内白名单</span><span class="v">' + (hosts || '—') + '</span></div>'
      + '<div class="cr-row"><span class="k">规则</span><span class="v">' + escapeHtml(res.rule || '—') + '</span></div>'
    + '</div>'
    + '<div class="cr-card">'
      + '<h4>人脸脱敏 <span class="cr-badge ' + (face.enabled ? 'pass' : 'fail') + '">'
      + (face.enabled ? '已启用' : '未启用') + '</span></h4>'
      + '<div class="cr-row"><span class="k">方法</span><span class="v">' + escapeHtml(face.method || '—') + '</span></div>'
      + '<div class="cr-row"><span class="k">触发时机</span><span class="v">' + escapeHtml(face.trigger || '—') + '</span></div>'
    + '</div>'
    + '<div class="cr-card">'
      + '<h4>审计留痕 <span class="cr-badge ' + (audit.enabled ? 'pass' : 'fail') + '">'
      + (audit.enabled ? '已启用' : '未启用') + '</span></h4>'
      + '<div class="cr-row"><span class="k">审计库</span><span class="v">' + escapeHtml(audit.db || '—') + '</span></div>'
      + '<div class="cr-row"><span class="k">近期记录数</span><span class="v">' + (Number(audit.recent_count) || 0) + '</span></div>'
    + '</div>'
    + '<div class="cr-card">'
      + '<h4>隐私处理</h4>'
      + '<div class="cr-row"><span class="k">图片存储</span><span class="v">' + escapeHtml(privacy.image_storage || '—') + '</span></div>'
      + '<div class="cr-row"><span class="k">敏感数据</span><span class="v">' + escapeHtml(privacy.sensitive_data || '—') + '</span></div>'
    + '</div>';
}

/* ── 检查器（右侧面板）── */
// 2026-10-17：新增"收起"态。空面板常驻会白占约 22% 宽度，
// 现在默认收起、有运行/下钻时自动展开、可手动唤回。
function setInspectorCollapsed(collapsed) {
  var app = document.getElementById('app');
  var btn = document.getElementById('inspectorReopen');
  if (app) app.classList.toggle('is-inspector-collapsed', !!collapsed);
  if (btn) {
    var empty = document.querySelector('#inspectorBody .run-empty');
    btn.hidden = !collapsed || !!empty;
    var title = document.getElementById('inspectorTitle');
    var label = title && title.textContent || '结果详情';
    var text = btn.querySelector('span');
    if (text) text.textContent = label;
    btn.setAttribute('aria-label', '打开' + label);
  }
}
function toggleInspector() {
  var app = document.getElementById('app');
  if (!app) return;
  setInspectorCollapsed(!app.classList.contains('is-inspector-collapsed'));
}
function showInspector(title, html, forceOpen) {
  var insp = document.getElementById('inspector');
  var t = document.getElementById('inspectorTitle');
  var b = document.getElementById('inspectorBody');
  if (!insp || !b) return;
  if (t) t.textContent = title;
  b.innerHTML = html;
  insp.classList.remove('hidden');
  setInspectorCollapsed(window.innerWidth <= 900 && !forceOpen);
}
var _inspectorReturnFocus = null;
function closeInspector() {
  var insp = document.getElementById('inspector');
  if (insp) insp.classList.add('hidden');
  setInspectorCollapsed(true);
  if (_inspectorReturnFocus && _inspectorReturnFocus.isConnected) _inspectorReturnFocus.focus();
  _inspectorReturnFocus = null;
}
// 首屏默认收起（空态），避免空白面板挤压内容区
document.addEventListener('DOMContentLoaded', function () {
  var b = document.getElementById('inspectorBody');
  var empty = !b || !b.textContent.trim() || b.querySelector('.run-empty');
  if (empty) setInspectorCollapsed(true);
});

/* ============================================
   P3-36 卡片下钻：视觉卡 / 异常卡 / 合规卡 → 检查器
   展示：本次模型预算表 · 引用溯源清单 ·
        该次运行逐节点耗时(/api/trace/<id>) · 原始 JSON
   ============================================ */
var CARD_TITLES = { vision: '视觉理解详情', anomaly: '异常检测详情', compliance: '测量数值分析详情' };

function openCardDetail(cardEl) {
  _inspectorReturnFocus = cardEl.querySelector('[data-card-detail]');
  var kind = cardEl.getAttribute('data-card') || 'vision';
  var data = cardEl.__cardData || null;
  if (!data) { try { data = JSON.parse(cardEl.getAttribute('data-card-json') || 'null'); } catch(e) {} }
  drillTransition(cardEl, function() {
    showInspector(CARD_TITLES[kind] || '卡片详情', cardDetailHtml(kind, data), true);
    fillDetailTrace(data && data.run_id);
    var close = document.querySelector('#inspector .panel-close');
    if (close) close.focus();
  });
}

function budgetRowsHtml(d) {
  var rows = [['器具误差限（B类）',d.u_a],['温度影响（简化B类）',d.u_b],
    ['研究附加项 OOD（未验证）',d.u_ood],['合成标准不确定度 u_c',d.u_c],
    ['扩展不确定度 U (k=' + num(d.k,2) + ')',d.U]];
  return rows.map(function(row) {
    return '<div class="cr-row"><span class="k">' + escapeHtml(row[0]) + '</span><span class="v">'
      + num(row[1],6) + ' mm</span></div>';
  }).join('');
}

function citationsHtml(d) {
  var items = [];
  ['references', 'citations', 'sources', 'standards', 'standard', 'evidence', 'basis'].forEach(function(key) {
    var v = d[key];
    if (!v) return;
    if (Array.isArray(v)) {
      v.forEach(function(x) {
        items.push(typeof x === 'string' ? x : (x && (x.title || x.name || x.id)) || JSON.stringify(x));
      });
    } else if (typeof v === 'string' || typeof v === 'number') {
      items.push(String(v));
    }
  });
  if (d.query) items.push('原始提问：' + String(d.query));
  if (!items.length) return '<div class="cr-row"><span class="k">后端未返回引用/溯源字段</span></div>';
  return items.map(function(t) {
    return '<div class="cr-row"><span class="k">·</span><span class="v">' + escapeHtml(String(t).slice(0, 200)) + '</span></div>';
  }).join('');
}

function cardDetailHtml(kind, d) {
  d = d || {};
  var presentation = kind === 'compliance' || kind === 'anomaly' ? assessmentPresentation(d,kind) : null;
  var title = presentation ? presentation.title : (d.object || '图像理解');
  var badgeCls = presentation ? presentation.cls : 'neutral';
  var html = '<div class="drill-target">'
    + '<div class="detail-title">' + escapeHtml(title)
    + ' <span class="cr-badge ' + badgeCls + '">' + escapeHtml(kind) + '</span></div>'
    + '</div>';

  if (presentation && presentation.cls === 'unavailable') {
    html += '<div class="assessment-reason">' + escapeHtml(presentation.message) + '</div>';
  } else if (kind === 'compliance') {
    html += '<h4 class="view-h3">本次模型预算表</h4><div class="cr-card">' + budgetRowsHtml(d) + '</div>';
    if (d.critical) {
      html += '<div class="hitl-review"><b>临界判定</b>——'
        + escapeHtml(d.review || '偏差接近扩展不确定度，处于判定不确定区') + '</div>';
    }
  } else if (kind === 'anomaly') {
    html += '<h4 class="view-h3">异常检测</h4><div class="cr-card">'
      + '<div class="cr-row"><span class="k">异常分数</span><span class="v">' + escapeHtml(num(d.anomaly_score, 4, '—')) + ' / 阈值 ' + escapeHtml(num(d.threshold, 4, '待标定')) + '</span></div>'
      + '<div class="cr-row"><span class="k">判定</span><span class="v">' + escapeHtml(d.verdict || '—') + '</span></div>'
      + '<div class="cr-row"><span class="k">检测类别</span><span class="v">' + escapeHtml(d.class_used || '—') + '</span></div>'
      + '<div class="cr-row"><span class="k">OOD 分量 u_ood</span><span class="v">'
      + (typeof d.u_ood === 'number' && isFinite(d.u_ood) ? '±' + d.u_ood.toFixed(6) : '未评定') + '</span></div>'
      + '</div>';
  } else {
    html += '<h4 class="view-h3">图像理解</h4><div class="cr-card">'
      + '<div class="cr-row"><span class="k">识别对象</span><span class="v">' + escapeHtml(d.object || '—') + '</span></div>'
      + '<div class="cr-row"><span class="k">材质推测</span><span class="v">' + escapeHtml(d.material_hint || '—') + '</span></div>'
      + '<div class="cr-row"><span class="k">表面状态</span><span class="v">' + escapeHtml(d.surface_state || '—') + '</span></div>'
      + '<div class="cr-row"><span class="k">可见缺陷</span><span class="v">'
      + escapeHtml((d.visible_defects || []).join('、') || '未发现明显缺陷') + '</span></div>'
      + '<div class="cr-row"><span class="k">可测特征</span><span class="v">'
      + escapeHtml((d.measurable_features || []).join('、') || '—') + '</span></div>'
      + '<div class="cr-row"><span class="k">检测建议</span><span class="v">' + escapeHtml(d.inspection_suggestion || '—') + '</span></div>'
      + '<div class="cr-row"><span class="k">置信度</span><span class="v">'
      + (typeof d.confidence === 'number' ? (d.confidence * 100).toFixed(0) + '%' : '—') + '</span></div>'
      + '</div>';
  }

  html += '<h4 class="view-h3">引用 / 溯源清单</h4><div class="cr-card">' + citationsHtml(d) + '</div>';
  html += '<h4 class="view-h3">本次运行逐节点耗时</h4><div class="cr-card" id="detailTrace">'
    + '<div class="cr-row"><span class="k">加载中…</span></div></div>';
  html += '<h4 class="view-h3">原始 JSON</h4><pre class="raw-json">'
    + escapeHtml(JSON.stringify(d, null, 2).slice(0, 4000)) + '</pre>';
  return html;
}

// 逐节点耗时：优先 /api/trace/<run_id>，没有 run_id 时用前端时钟测得的时间轴
function fillDetailTrace(boundRunId) {
  var box = document.getElementById('detailTrace');
  if (!box) return;
  var runId = boundRunId || null;
  if (!runId) { box.textContent = '该结果没有绑定运行记录'; return; }
  var localRows = function() {
    var rows = '';
    if (typeof _runState !== 'undefined') {
      RUN_NODES.forEach(function(n) {
        var rec = _runState.nodes[n.id];
        if (!rec) return;
        rows += '<div class="cr-row"><span class="k">' + escapeHtml(n.label) + '</span><span class="v">'
          + (rec.ms === null || rec.ms === undefined ? '—' : rec.ms + ' ms') + '</span></div>';
      });
    }
    return rows;
  };
  if (!runId) {
    var r = localRows();
    box.innerHTML = r || '<div class="cr-row"><span class="k">本次会话暂无运行记录</span></div>';
    return;
  }
  fetch('/api/trace/' + encodeURIComponent(runId))
    .then(function(resp) { return resp.ok ? resp.json() : null; })
    .then(function(dd) {
      if (!dd || !dd.steps || !dd.steps.length) {
        box.innerHTML = localRows() || '<div class="cr-row"><span class="k">运行 #' + escapeHtml(runId) + ' 暂无节点明细</span></div>';
        return;
      }
      box.innerHTML = dd.steps.map(function(s2) {
        return '<div class="cr-row"><span class="k">' + escapeHtml(s2.label || s2.node) + '</span>'
          + '<span class="v">' + (Number(s2.ms) || 0) + ' ms</span></div>';
      }).join('') + '<div class="cr-row"><span class="k">总耗时</span><span class="v">'
        + (Number(dd.total_ms) || 0) + ' ms</span></div>';
    })
    .catch(function() { box.innerHTML = localRows() || '<div class="cr-row"><span class="k">运行记录读取失败</span></div>'; });
}

// Only the explicit detail button opens the inspector; disclosure and text selection remain independent.
document.addEventListener('click', function(e) {
  var trigger = e.target && e.target.closest ? e.target.closest('[data-card-detail]') : null;
  var card = trigger && trigger.closest('[data-card]');
  if (card) openCardDetail(card);
});

document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    if (document.body.getAttribute('data-view') === 'cases' && _currentRoute.sub) { history.back(); }
    else { closeInspector(); }
  }
});
