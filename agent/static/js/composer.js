/* Measurement entry only prepares user-owned text. It never supplies default observations. */
function measurementDraft(values) {
  var fields = [
    ['measured', '实测值', 'mm'], ['reference', '参考值', 'mm'],
    ['precision', '器具精度', 'mm'], ['temperature', '环境温度', '°C']
  ];
  var uncertainty = values.mode === 'uncertainty';
  if (!uncertainty && values.mode !== 'consistency') return {error: '请选择分析任务', field: 'mode'};
  var parts = [];
  for (var i = 0; i < fields.length; i++) {
    var field = fields[i], key = field[0];
    if (uncertainty && key === 'reference') continue;
    var raw = String(values[key] == null ? '' : values[key]).trim().replace(/−/g, '-');
    if (!raw) return {error: '请填写' + field[1], field: key};
    if (!/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(raw) || !Number.isFinite(Number(raw))) {
      return {error: field[1] + '请输入有效数值，单位已在字段旁标注', field: key};
    }
    if (key === 'precision' && Number(raw) < 0) return {error: '器具误差限不能为负数', field: key};
    if (key === 'temperature' && Number(raw) < -273.15) return {error: '温度不能低于 −273.15 °C', field: key};
    parts.push(field[1] + raw + field[2]);
  }
  return {text: parts.join('，') + (uncertainty ? '，请估算不确定度预算并说明假设。' : '，请进行参考一致性判定并给出不确定度预算。')};
}

function prepareDraft(text) {
  var input = document.getElementById('msgInput');
  if (!input) return;
  if (typeof navigateTo === 'function') navigateTo('chat');
  var previous = input.value.trimEnd();
  input.value = previous ? previous + '\n\n' + text : text;
  autoResize(input);
  input.dispatchEvent(new Event('input', {bubbles: true}));
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
}

function openMeasurementForm(mode) {
  var dialog = document.getElementById('measurementDialog');
  var select = document.getElementById('measurementMode');
  if (!dialog || dialog.open) return;
  select.value = mode === 'uncertainty' ? 'uncertainty' : 'consistency';
  select.dispatchEvent(new Event('change'));
  document.getElementById('measurementError').hidden = true;
  dialog.querySelectorAll('[aria-invalid]').forEach(function (input) { input.removeAttribute('aria-invalid'); input.removeAttribute('aria-describedby'); });
  dialog.showModal();
  document.getElementById('measureValue').focus();
}

(function () {
  'use strict';
  var form = document.getElementById('measurementForm');
  var dialog = document.getElementById('measurementDialog');
  var mode = document.getElementById('measurementMode');
  var error = document.getElementById('measurementError');
  if (!form || !dialog) return;
  function close() { dialog.close(); }
  document.getElementById('measurementClose').addEventListener('click', close);
  document.getElementById('measurementCancel').addEventListener('click', close);
  mode.addEventListener('change', function () {
    var uncertainty = mode.value === 'uncertainty';
    document.getElementById('referenceField').hidden = uncertainty;
    document.getElementById('referenceValue').disabled = uncertainty;
    document.getElementById('measurementScope').textContent = uncertainty
      ? '按当前简化模型估计预算；重复性与参考值不确定度需另行评定。'
      : '比较实测与参考值的一致性，不用于工件公差合格判定。';
    error.hidden = true;
  });
  form.addEventListener('submit', function (event) {
    event.preventDefault();
    var values = {};
    form.querySelectorAll('[name]').forEach(function (input) {
      values[input.name] = input.value;
      input.removeAttribute('aria-invalid'); input.removeAttribute('aria-describedby');
    });
    var result = measurementDraft(values);
    if (result.error) {
      error.textContent = result.error; error.hidden = false;
      var invalid = form.elements.namedItem(result.field);
      if (invalid) { invalid.setAttribute('aria-invalid', 'true'); invalid.setAttribute('aria-describedby', 'measurementError'); invalid.focus(); }
      return;
    }
    close();
    prepareDraft(result.text);
    // No observations silently carry into the next measurement event.
    form.reset();
  });
  var more = document.getElementById('composerMore');
  if (more) {
    document.addEventListener('click', function (event) {
      if (!more.contains(event.target) || event.target.closest('.composer-menu button')) more.open = false;
    });
    more.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && more.open) {
        event.preventDefault(); event.stopPropagation(); more.open = false; more.querySelector('summary').focus();
      }
    });
  }
})();
