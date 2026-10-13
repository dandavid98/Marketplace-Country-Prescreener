/* Rule Editor and JSON Editor share one validated ProductWatch-style filter tree. */
(function (global) {
  'use strict';
  var RF = global.ReviewFilter;
  function build(config) {
    var $ = function (id) { return document.getElementById(id); };
    var dialog = $('fr-filter-dialog');
    var tree = RF.empty(), draft = RF.empty(), mode = 'rules';
    var error = $('fr-filter-error');
    function report(message) {
      error.hidden = !message;
      error.textContent = message || '';
    }
    function clone(value) { return JSON.parse(JSON.stringify(value)); }
    function escape(text) {
      return String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
    function options(pairs, current) {
      return pairs.map(function (entry) {
        return '<option value="' + entry[0] + '"' + (entry[0] === current ? ' selected' : '') + '>' + escape(entry[1]) + '</option>';
      }).join('');
    }
    function groupAt(path) {
      return path.reduce(function (parent, index) { return parent.filters[index]; }, draft);
    }
    function drawGroup(group, path) {
      var key = path.join('.');
      var inner = group.filters.map(function (item, index) {
        var childPath = path.concat(index);
        var where = childPath.join('.');
        if ('columnId' in item) {
          return '<div class="fr-rule" data-path="' + where + '">' +
            '<label>Field<select data-prop="columnId">' + options(RF.fields, item.columnId) + '</select></label>' +
            '<label>Match<select data-prop="operator">' + options(RF.operators, item.operator) + '</select></label>' +
            '<label>Value<input type="text" data-prop="value" value="' + escape(item.value) + '"' +
            (item.operator === 'isEmpty' || item.operator === 'isNotEmpty' ? ' disabled' : '') + '></label>' +
            '<button type="button" class="button secondary" data-action="remove" aria-label="Remove rule">Remove</button></div>';
        }
        return drawGroup(item, childPath);
      }).join('');
      return '<fieldset class="fr-rule-group" data-path="' + key + '"><legend>' +
        (path.length ? 'Nested group' : 'Match rules') + '</legend>' +
        '<label>Match<select data-prop="operator">' + options([['and', 'ALL (AND)'], ['or', 'ANY (OR)']], group.operator) + '</select></label>' +
        (path.length ? '<button type="button" class="button secondary" data-action="remove" aria-label="Remove group">Remove group</button>' : '') +
        '<div class="fr-rule-children">' + (inner || '<p class="tiny">No filters yet. Add a rule or group.</p>') + '</div>' +
        '<div class="row-actions"><button type="button" class="button secondary" data-action="add-rule">+ Add rule</button>' +
        '<button type="button" class="button secondary" data-action="add-group">+ Add group</button></div></fieldset>';
    }
    function render() { $('fr-filter-tree').innerHTML = drawGroup(draft, []); }
    function fromJson() {
      try {
        var parsed = RF.validate(JSON.parse($('fr-filter-source').value));
        draft = parsed;
        report('');
        return true;
      } catch (e) { report(e.message); return false; }
    }
    function setMode(next) {
      if (next === mode) return;
      if (mode === 'json' && !fromJson()) return;
      mode = next;
      $('fr-filter-tree').hidden = next !== 'rules';
      $('fr-filter-json-area').hidden = next !== 'json';
      $('fr-filter-rules').setAttribute('aria-pressed', String(next === 'rules'));
      $('fr-filter-json').setAttribute('aria-pressed', String(next === 'json'));
      if (next === 'json') $('fr-filter-source').value = JSON.stringify(draft, null, 2);
      else render();
    }
    function close() { dialog.close(); report(''); config.onClose(); }
    $('fr-filter-open').addEventListener('click', function () {
      if (config.blocked()) return;
      draft = clone(tree);
      mode = 'rules';
      $('fr-filter-tree').hidden = false;
      $('fr-filter-json-area').hidden = true;
      $('fr-filter-rules').setAttribute('aria-pressed', 'true');
      $('fr-filter-json').setAttribute('aria-pressed', 'false');
      render(); report(''); dialog.showModal();
    });
    $('fr-filter-close').addEventListener('click', close);
    dialog.addEventListener('cancel', function (e) { e.preventDefault(); close(); });
    $('fr-filter-rules').addEventListener('click', function () { setMode('rules'); });
    $('fr-filter-json').addEventListener('click', function () { setMode('json'); });
    $('fr-filter-clear').addEventListener('click', function () {
      draft = RF.empty();
      if (mode === 'json') $('fr-filter-source').value = JSON.stringify(draft, null, 2);
      else render();
      report('');
    });
    $('fr-filter-source').addEventListener('keydown', function (e) {
      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
        e.preventDefault(); $('fr-filter-apply').click();
      }
    });
    function updateApplied() {
      var count = RF.count(tree);
      $('fr-filter-count').textContent = count ? '(' + count + ')' : '';
      $('fr-filter-clear-quick').hidden = !count;
      config.onApply(tree);
    }
    $('fr-filter-clear-quick').addEventListener('click', function () {
      if (config.blocked() || dialog.open) return;
      tree = RF.empty();
      updateApplied();
    });
    $('fr-filter-apply').addEventListener('click', function () {
      if (mode === 'json' && !fromJson()) return;
      try { tree = RF.validate(draft); }
      catch (e) { report(e.message); return; }
      updateApplied();
      close();
    });
    $('fr-filter-tree').addEventListener('input', function (e) {
      if (e.target.matches('input[data-prop="value"]')) {
        var wrapper = e.target.closest('.fr-rule[data-path]');
        var path = wrapper.dataset.path.split('.').map(Number);
        groupAt(path.slice(0, -1)).filters[path[path.length - 1]].value = e.target.value;
      }
    });
    $('fr-filter-tree').addEventListener('change', function (e) {
      var control = e.target.closest('[data-prop]');
      if (!control) return;
      var fieldset = control.closest('fieldset[data-path]');
      var rule = control.closest('.fr-rule[data-path]');
      var wrapper = rule || fieldset;
      var path = wrapper.dataset.path ? wrapper.dataset.path.split('.').map(Number) : [];
      var item = path.length ? groupAt(path.slice(0, -1)).filters[path[path.length - 1]] : draft;
      if (control.dataset.prop === 'columnId' && control.value !== 'price_value' &&
          ['greaterThan', 'lessThan', 'greaterThanOrEquals', 'lessThanOrEquals'].includes(item.operator)) {
        item.operator = 'is';
      }
      item[control.dataset.prop] = control.value;
      if (control.dataset.prop === 'columnId' && control.value === 'price_value') {
        item.value = item.value === '' ? 0 : item.value;
      }
      render(); report('');
    });
    $('fr-filter-tree').addEventListener('click', function (e) {
      var button = e.target.closest('[data-action]');
      if (!button) return;
      var wrapper = button.dataset.action === 'remove' ? button.closest('[data-path]') : button.closest('fieldset[data-path]');
      var path = wrapper.dataset.path ? wrapper.dataset.path.split('.').map(Number) : [];
      if (button.dataset.action === 'remove') {
        groupAt(path.slice(0, -1)).filters.splice(path[path.length - 1], 1);
      } else {
        try {
          var candidate = clone(draft);
          var target = path.reduce(function (parent, index) { return parent.filters[index]; }, candidate);
          if (button.dataset.action === 'add-rule') target.filters.push({ columnId: 'title', operator: 'includes', value: '' });
          if (button.dataset.action === 'add-group') target.filters.push(RF.empty());
          RF.validate(candidate);
          draft = candidate;
        } catch (err) { report(err.message); return; }
      }
      try { RF.validate(draft); report(''); }
      catch (err) { report(err.message); }
      render();
    });
    return { matches: function (row) { return RF.matches(tree, row); },
      active: function () { return RF.count(tree) > 0; } };
  }
  global.ReviewFilterBuilder = { build: build };
})(window);
