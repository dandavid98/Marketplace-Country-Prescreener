/* history_export.js - Scan history multi-scan export.
   Picking From/To ticks every scan whose start date (UTC, as shown) falls in the range;
   hand-ticking afterwards refines it. The checkboxes belong to #multi-export via form=. */
(function () {
  var form = document.getElementById('multi-export');
  if (!form) return;
  var from = document.getElementById('range-from');
  var to = document.getElementById('range-to');
  var submit = document.getElementById('multi-export-submit');
  var status = document.getElementById('multi-export-status');
  var boxes = Array.prototype.slice.call(document.querySelectorAll('input[name="jobs"][form="multi-export"]'));

  function refresh() {
    var n = boxes.filter(function (b) { return b.checked; }).length;
    submit.disabled = n === 0;
    submit.textContent = 'Download XLSX (' + n + ' scan' + (n === 1 ? '' : 's') + ')';
  }

  function applyRange() {
    if (!from.value && !to.value) return;
    var lo = from.value || '0000-00-00', hi = to.value || '9999-99-99';
    if (lo > hi) { status.textContent = '"From" is after "To".'; return; }
    var n = 0;
    boxes.forEach(function (b) {
      b.checked = b.dataset.date >= lo && b.dataset.date <= hi;
      if (b.checked) n++;
    });
    status.textContent = n + ' scan' + (n === 1 ? '' : 's') + ' in that range ticked.';
    refresh();
  }

  from.addEventListener('change', applyRange);
  to.addEventListener('change', applyRange);
  boxes.forEach(function (b) { b.addEventListener('change', function () { status.textContent = ''; refresh(); }); });
  document.getElementById('range-clear').addEventListener('click', function () {
    boxes.forEach(function (b) { b.checked = false; });
    from.value = to.value = '';
    status.textContent = 'All ticks cleared.';
    refresh();
  });
  refresh();
})();
