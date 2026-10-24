/* Equis interaction layer. Event-driven; no network, no continuous render loop. */
(function () {
  'use strict';
  function init() {
  var root = document.documentElement;
  function animated() { return root.dataset.motion === 'full' && !document.hidden; }
  ['viewNav', 'railItems'].forEach(function (id) {
    var nav = document.getElementById(id);
    if (!nav) return;
    var marker = document.createElement('span');
    marker.className = 'nav-marker'; marker.setAttribute('aria-hidden', 'true');
    nav.appendChild(marker); nav.classList.add('has-nav-marker');
    var frame = 0;
    function paint() {
      frame = 0;
      var active = nav.querySelector('button.active');
      if (!active || !nav.getClientRects().length) { marker.style.opacity = '0'; return; }
      marker.style.width = active.offsetWidth + 'px';
      marker.style.height = active.offsetHeight + 'px';
      marker.style.transform = 'translate(' + active.offsetLeft + 'px,' + active.offsetTop + 'px)';
      marker.style.opacity = '1';
    }
    function schedule() { if (!frame) frame = requestAnimationFrame(paint); }
    new MutationObserver(schedule).observe(nav, {subtree: true, attributes: true, attributeFilter: ['class', 'aria-current'], childList: true});
    if ('ResizeObserver' in window) new ResizeObserver(schedule).observe(nav);
    window.addEventListener('resize', schedule, {passive: true});
    document.addEventListener('visibilitychange', schedule);
    schedule();
  });
  var input = document.getElementById('msgInput');
  ['monTemp', 'monHum', 'monVib'].forEach(function (id) {
    var value = document.getElementById(id);
    if (!value) return;
    var previous = value.textContent.trim(), timer;
    function numeric(text) { return /^[-+]?\d+(?:\.\d+)?$/.test(text); }
    new MutationObserver(function () {
      var current = value.textContent.trim();
      var changed = numeric(previous) && numeric(current) && Number(previous) !== Number(current);
      previous = current;
      if (!changed || !animated() || !value.getClientRects().length) return;
      value.classList.add('reading-updated');
      clearTimeout(timer);
      timer = setTimeout(function () { value.classList.remove('reading-updated'); }, 700);
    }).observe(value, {childList: true, characterData: true, subtree: true});
  });
  var composer = document.querySelector('.input-box');
  if (input && composer) {
    var status = document.createElement('span');
    status.className = 'composer-status'; status.setAttribute('aria-live', 'off');
    var toolbar = composer.querySelector('.input-toolbar');
    if (toolbar) toolbar.appendChild(status);
    var send = document.getElementById('sendBtn');
    if (typeof autoResize === 'function') autoResize(input);
    function updateComposer() {
      var busy = document.body.classList.contains('is-streaming');
      var count = Array.from(input.value).length;
      var draft = !!input.value.trim();
      composer.dataset.state = busy ? 'busy' : draft ? 'ready' : 'idle';
      status.textContent = busy ? '正在生成 · 可停止' : draft ? count + ' 字 · 可发送' : 'Enter 发送';
      if (send) {
        send.title = busy ? '正在生成' : draft ? '发送消息' : '请输入内容';
        send.setAttribute('aria-label', send.title);
      }
    }
    input.addEventListener('input', updateComposer);
    input.addEventListener('focus', updateComposer);
    input.addEventListener('compositionend', updateComposer);
    new MutationObserver(updateComposer).observe(document.body, {attributes: true, attributeFilter: ['class']});
    document.addEventListener('click', function () { requestAnimationFrame(updateComposer); });
    updateComposer();
  }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, {once: true});
  else init();
})();
