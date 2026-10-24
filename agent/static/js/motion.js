/* Unified motion lifecycle: one pointer frame at a time, no polling/render loop. */
(function () {
  'use strict';
  var root = document.documentElement;
  var reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
  var fine = window.matchMedia('(hover: hover) and (pointer: fine)');
  var mode = 'auto';
  try { var saved = localStorage.getItem('equis-motion'); if (['auto','on','off'].indexOf(saved) >= 0) mode = saved; } catch (_) {}
  var button = document.getElementById('motionToggle');
  var frame = 0, active = null, rect = null, pending = null;
  function resetPointer() {
    if (frame) cancelAnimationFrame(frame);
    frame = 0; pending = null;
    if (active) ['--fx-rx','--fx-ry','--fx-x','--fx-y'].forEach(function (key) { active.style.removeProperty(key); });
    active = null; rect = null;
  }
  function full() { return root.dataset.motion === 'full' && !document.hidden; }
  function apply() {
    resetPointer();
    root.dataset.motion = mode === 'on' || (mode === 'auto' && !reduced.matches) ? 'full' : 'reduced';
    root.dataset.motionPaused = document.hidden ? 'true' : 'false';
    root.dataset.motionPreference = mode;
    if (button) {
      button.dataset.mode = mode;
      button.textContent = mode === 'on' ? '增强动效' : mode === 'off' ? '静态模式' : '跟随系统';
      var next = mode === 'auto' ? '增强动效' : mode === 'on' ? '静态模式' : '跟随系统';
      button.title = '当前：' + button.textContent + (mode === 'auto' && reduced.matches ? '（系统减少动态，当前静态）' : '') + '；点击切换为' + next;
      button.setAttribute('aria-label', '动态效果：' + button.textContent + '。点击切换为' + next);
    }
  }
  if (button) button.addEventListener('click', function () {
    mode = mode === 'auto' ? 'on' : mode === 'on' ? 'off' : 'auto';
    try { localStorage.setItem('equis-motion', mode); } catch (_) {}
    apply();
  });
  if (reduced.addEventListener) reduced.addEventListener('change', apply);
  else reduced.addListener(apply);
  if (fine.addEventListener) fine.addEventListener('change', resetPointer);
  document.addEventListener('visibilitychange', apply);
  window.addEventListener('blur', resetPointer);
  window.addEventListener('resize', resetPointer, {passive: true});
  document.addEventListener('scroll', resetPointer, {capture: true, passive: true});
  window.addEventListener('storage', function (event) {
    if (event.key === 'equis-motion') { mode = ['on','off'].indexOf(event.newValue) >= 0 ? event.newValue : 'auto'; apply(); }
  });

  document.querySelectorAll('.preset-card').forEach(function (card) {
    card.setAttribute('role', 'button'); card.tabIndex = 0;
    if (!card.querySelector('.motion-rim')) {
      var rim = document.createElement('span'); rim.className = 'motion-rim'; rim.setAttribute('aria-hidden','true'); card.appendChild(rim);
    }
    card.addEventListener('keydown', function (event) {
      if ((event.key === 'Enter' || event.key === ' ') && !event.repeat) { event.preventDefault(); card.click(); }
    });
    card.addEventListener('pointerenter', function (event) {
      if (!full() || !fine.matches || event.pointerType === 'touch') return;
      resetPointer(); active = card; rect = card.getBoundingClientRect();
    });
    card.addEventListener('pointermove', function (event) {
      if (active !== card || !rect || !full()) return;
      pending = {x: Math.max(0, Math.min(rect.width, event.clientX - rect.left)), y: Math.max(0, Math.min(rect.height, event.clientY - rect.top))};
      if (frame) return;
      frame = requestAnimationFrame(function () {
        frame = 0;
        if (!active || !pending || !rect || !full()) return;
        active.style.setProperty('--fx-x', pending.x.toFixed(1) + 'px');
        active.style.setProperty('--fx-y', pending.y.toFixed(1) + 'px');
        active.style.setProperty('--fx-rx', ((.5 - pending.y / rect.height) * 5).toFixed(2) + 'deg');
        active.style.setProperty('--fx-ry', ((pending.x / rect.width - .5) * 5).toFixed(2) + 'deg');
      });
    }, {passive:true});
    card.addEventListener('pointerleave', function () { if (active === card) resetPointer(); });
    card.addEventListener('pointercancel', resetPointer);
  });
  var welcome = document.getElementById('welcomeScreen');
  if (welcome && 'IntersectionObserver' in window) {
    new IntersectionObserver(function (entries) { welcome.dataset.motionVisible = entries[0].isIntersecting ? 'true' : 'false'; }, {threshold: 0}).observe(welcome);
  }

  document.addEventListener('pointerdown', function (event) {
    if (!full() || event.button !== 0) return;
    var target = event.target.closest('button, .preset-card');
    if (!target || target.disabled || target.id === 'motionToggle') return;
    var bounds = target.getBoundingClientRect();
    if (getComputedStyle(target).position === 'static') target.classList.add('motion-relative');
    var layer = document.createElement('span'), circle = document.createElement('i');
    layer.className = 'motion-ripple'; layer.setAttribute('aria-hidden','true');
    layer.style.setProperty('--ripple-x', (event.clientX - bounds.left) + 'px');
    layer.style.setProperty('--ripple-y', (event.clientY - bounds.top) + 'px');
    layer.style.setProperty('--ripple-size', (Math.hypot(bounds.width,bounds.height)*2) + 'px');
    layer.appendChild(circle); target.appendChild(layer);
    circle.addEventListener('animationend', function () { layer.remove(); }, {once:true});
    setTimeout(function () { layer.remove(); }, 700);
  }, {passive:true});

  var input = document.getElementById('msgInput');
  var placeholders = ['向 Equis 提问，回车发送，Shift+回车换行', '输入实测、参考、器具精度（mm）及温度（℃）', '检索计量标准，或上传工件图片'];
  var placeholderIndex = 0;
  // Updating placeholder text never changes textarea opacity or the user's draft.
  if (input) setInterval(function () {
    if (document.hidden || root.dataset.motion === 'reduced' || input.value || document.activeElement === input) return;
    placeholderIndex = (placeholderIndex + 1) % placeholders.length;
    input.placeholder = placeholders[placeholderIndex];
  }, 7000);
  apply();
})();
