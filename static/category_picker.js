/* category_picker.js - export category checkboxes (queue + Scan history).
   All categories start ticked; untick the ones to withhold. Keeps the summary line
   honest ("All", "3 of 17", "None") and refuses to submit with nothing ticked. */
(function () {
  document.querySelectorAll('details.cat-pick').forEach(function (picker) {
    var boxes = Array.prototype.slice.call(picker.querySelectorAll('input[name="categories"]'));
    var summary = picker.querySelector('.cat-pick-summary');
    var form = picker.closest('form');

    function refresh() {
      var ticked = boxes.filter(function (b) { return b.checked; });
      summary.textContent = ticked.length === boxes.length ? 'All'
        : ticked.length === 0 ? 'None (tick at least one)'
        : ticked.length + ' of ' + boxes.length;
    }
    function setAll(on) { boxes.forEach(function (b) { b.checked = on; }); refresh(); }

    boxes.forEach(function (b) { b.addEventListener('change', refresh); });
    picker.querySelector('[data-cat-all]').addEventListener('click', function () { setAll(true); });
    picker.querySelector('[data-cat-none]').addEventListener('click', function () { setAll(false); });
    if (form) {
      form.addEventListener('submit', function (e) {
        if (boxes.some(function (b) { return b.checked; })) return;
        e.preventDefault();
        picker.open = true;
        summary.textContent = 'None (tick at least one)';
        boxes[0].focus();
      });
    }
    refresh();
  });
})();
