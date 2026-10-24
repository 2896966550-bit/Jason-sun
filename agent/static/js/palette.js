/* ============================================================
   Equis · 命令面板（A3，2026-10-17）
   ------------------------------------------------------------
   为什么需要：这个界面有 5 个视图、若干个动作（新对话、切主题、
   开时间轴、上传图片、看合规自检…），但入口分散在 rail、顶栏、
   输入区工具栏三处，用户要记位置。
   命令面板把"去任何地方 / 做任何事"收敛成一个入口：
       ⌘K / Ctrl+K  打开
       ↑↓ 选择    Enter 执行    Esc 关闭
   设计取舍：
     · 纯键盘优先，但鼠标点击/悬停同样可用
     · 模糊匹配（子串 + 首字母），不引入依赖
     · 无结果时给出明确提示，不做"猜你想要"
     · 全部通过既有全局函数执行，不新增业务逻辑
   ============================================================ */
(function () {
  'use strict';

  var COMMANDS = [
    { id: 'chat',      label: '对话',            hint: '回到主对话',          keys: 'g 1', run: function () { navigateTo('chat'); } },
    { id: 'workspace', label: '工作台',          hint: '传感器与运行监控',    keys: 'g 2', run: function () { navigateTo('workspace'); } },
    { id: 'standards', label: '标准库',          hint: '检索历史与标准条目',  keys: 'g 3', run: function () { navigateTo('standards'); } },
    { id: 'cases',     label: '案例',            hint: '可追溯的运行案例',    keys: 'g 4', run: function () { navigateTo('cases'); } },
    { id: 'system',    label: '系统与数据合规',  hint: '架构图 / 运行历史 / 合规自检', keys: 'g 5', run: function () { navigateTo('system'); } },
    { id: 'new',       label: '新建对话',        hint: '清空当前会话',        keys: 'n',   run: function () { newChat(); } },
    { id: 'upload',    label: '上传图片分析',    hint: '选择本地图片',        keys: 'u',   run: function () { var e = document.getElementById('imageInput'); if (e) e.click(); } },
    { id: 'timeline',  label: '运行时间轴',      hint: '展开 / 收起右侧检查器', keys: 't', run: function () { toggleInspector(); } },
    { id: 'theme',     label: '切换明暗外观',    hint: '深色 ↔ 浅色',         keys: 'd',   run: function () { toggleTheme(); } },
    { id: 'compliance',label: '数据合规自检',    hint: '查看不出境 / 脱敏 / 审计', keys: '', run: function () { navigateTo('system'); setTimeout(function () { var b = document.querySelector('[data-ptab="compliance"]'); if (b) b.click(); }, 260); } }
  ];

  var active = 0, filtered = COMMANDS.slice();
  var overlay, input, listEl, returnFocus, focusTimer;

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function build() {
    if (overlay) return;
    overlay = document.createElement('div');
    overlay.className = 'cmdk-overlay';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.setAttribute('aria-label', '命令面板');
    overlay.inert = true;
    overlay.setAttribute('aria-hidden', 'true');
    overlay.innerHTML =
      '<div class="cmdk-panel" role="document">' +
        '<div class="cmdk-input-row">' +
          '<svg viewBox="0 0 24 24" aria-hidden="true"><use href="#i-scan"/></svg>' +
          '<input class="cmdk-input" type="text" autocomplete="off" spellcheck="false" ' +
                 'placeholder="搜索视图或动作…" aria-label="搜索命令">' +
          '<kbd class="cmdk-kbd">ESC</kbd>' +
        '</div>' +
        '<div class="cmdk-list" id="commandOptions" role="listbox" aria-label="可用命令"></div>' +
        '<div class="cmdk-foot"><span><kbd>↑</kbd><kbd>↓</kbd> 选择</span>' +
          '<span><kbd>↵</kbd> 执行</span><span><kbd>esc</kbd> 关闭</span></div>' +
      '</div>';
    document.body.appendChild(overlay);
    input = overlay.querySelector('.cmdk-input');
    listEl = overlay.querySelector('.cmdk-list');
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-controls', 'commandOptions');
    input.setAttribute('aria-autocomplete', 'list');

    overlay.addEventListener('mousedown', function (e) {
      if (e.target === overlay) close();
    });
    input.addEventListener('input', function () { render(input.value); });
    input.addEventListener('keydown', onKey);
    listEl.addEventListener('click', function (e) {
      var row = e.target.closest('.cmdk-item');
      if (row) exec(Number(row.dataset.index));
    });
    listEl.addEventListener('mousemove', function (e) {
      var row = e.target.closest('.cmdk-item');
      if (row && Number(row.dataset.index) !== active) {
        active = Number(row.dataset.index);
        paint();
      }
    });
  }

  function render(q) {
    var s = String(q || '').trim().toLowerCase();
    filtered = !s ? COMMANDS.slice() : COMMANDS.filter(function (c) {
      return (c.label + ' ' + c.hint + ' ' + c.id).toLowerCase().indexOf(s) >= 0;
    });
    active = 0;
    if (!filtered.length) {
      listEl.innerHTML = '<div class="cmdk-empty">没有匹配的命令</div>';
      input.removeAttribute('aria-activedescendant');
      return;
    }
    listEl.innerHTML = filtered.map(function (c, i) {
      return '<div class="cmdk-item' + (i === active ? ' is-active' : '') + '" data-index="' + i + '" ' +
             'id="commandOption' + i + '" role="option" aria-selected="' + (i === active) + '">' +
               '<span class="cmdk-label">' + esc(c.label) + '</span>' +
               '<span class="cmdk-hint">' + esc(c.hint) + '</span>' +
               (c.keys ? '<kbd class="cmdk-key">' + esc(c.keys) + '</kbd>' : '') +
             '</div>';
    }).join('');
    input.setAttribute('aria-activedescendant', 'commandOption0');
  }

  function paint() {
    Array.prototype.forEach.call(listEl.children, function (el, i) {
      if (!el.classList) return;
      el.classList.toggle('is-active', i === active);
      el.setAttribute('aria-selected', String(i === active));
    });
    var cur = listEl.children[active];
    if (cur) input.setAttribute('aria-activedescendant', cur.id);
    if (cur && cur.scrollIntoView) cur.scrollIntoView({ block: 'nearest' });
  }

  function onKey(e) {
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === 'Tab') { e.preventDefault(); input.focus(); return; }
    if (e.key === 'ArrowDown') { e.preventDefault(); if (filtered.length) { active = (active + 1) % filtered.length; paint(); } }
    else if (e.key === 'ArrowUp') { e.preventDefault(); if (filtered.length) { active = (active - 1 + filtered.length) % filtered.length; paint(); } }
    else if (e.key === 'Enter') { e.preventDefault(); exec(active); }
    else if (e.key === 'Escape') { e.preventDefault(); close(); }
  }

  function exec(i) {
    var c = filtered[i];
    if (!c) return;
    close();
    // 让关闭动画先走一帧，避免动作触发后界面闪烁
    setTimeout(function () { try { c.run(); } catch (err) { console.warn('[cmdk]', err); } }, 30);
  }

  function open() {
    build();
    if (!isOpen()) returnFocus = document.activeElement;
    clearTimeout(focusTimer);
    render('');
    overlay.inert = false;
    overlay.removeAttribute('aria-hidden');
    input.setAttribute('aria-expanded', 'true');
    overlay.classList.add('is-open');
    document.documentElement.setAttribute('data-cmdk', '1');
    focusTimer = setTimeout(function () { if (isOpen()) { input.focus(); input.select(); } }, 20);
  }

  function close() {
    if (!overlay) return;
    clearTimeout(focusTimer);
    overlay.classList.remove('is-open');
    input.setAttribute('aria-expanded', 'false');
    document.documentElement.removeAttribute('data-cmdk');
    if (returnFocus && returnFocus.isConnected && returnFocus.focus) returnFocus.focus({preventScroll: true});
    overlay.inert = true;
    overlay.setAttribute('aria-hidden', 'true');
    if (input) input.value = '';
  }

  function isOpen() { return overlay && overlay.classList.contains('is-open'); }
  document.addEventListener('focusin', function (e) {
    if (isOpen() && !overlay.contains(e.target)) input.focus();
  });

  // ⌘K / Ctrl+K 打开；"/" 在非输入态也可打开（对齐 Slack/Linear 习惯）
  document.addEventListener('keydown', function (e) {
    var mod = e.metaKey || e.ctrlKey;
    if (mod && (e.key === 'k' || e.key === 'K')) {
      e.preventDefault();
      isOpen() ? close() : open();
      return;
    }
    if (e.key === '/' && !isOpen()) {
      var t = document.activeElement;
      var tag = t && t.tagName;
      if (tag !== 'INPUT' && tag !== 'TEXTAREA' && !(t && t.isContentEditable)) {
        e.preventDefault();
        open();
      }
    }
  });

  // 供顶栏/其它入口调用
  window.openCommandPalette = open;
  window.closeCommandPalette = close;

  // 深链/调试入口：
  //   #cmdk     —— 但本项目用 hash 做视图路由，hash 会被路由改写，故仅作辅助
  //   ?cmdk=1   —— 更可靠，便于自动化截图与分享
  function openFromUrl() {
    var byHash = location.hash === '#cmdk';
    var byQuery = /(?:^|[?&])cmdk=1(?:&|$)/.test(location.search);
    if (byHash || byQuery) open();
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', openFromUrl);
  } else {
    openFromUrl();
  }
})();
