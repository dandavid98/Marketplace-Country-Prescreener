/* ProductWatch-compatible AND/OR rule tree, scoped to prescreener review fields.
   Safe by design: only known columns/operators, bounded nesting, no eval or HTML input. */
(function (global) {
  'use strict';
  var FIELDS = [
    ['item_id', 'Item ID'], ['keyword', 'Keyword'], ['title', 'Title'],
    ['listing_brand', 'Listing Brand'], ['seller_name', 'Seller Name'],
    ['seller_id', 'Seller ID'], ['country_signal', 'Country'],
    ['scan_status', 'Scan Status'], ['review_state', 'Review Status'],
    ['match_kind', 'Matched As'], ['remembered', 'Remembered'],
    ['evidence', 'Evidence'], ['job_id', 'Scan ID'], ['price_value', 'Price']
  ];
  var OPS = [
    ['includes', 'Includes'], ['doesNotInclude', 'Does not include'],
    ['is', 'Is'], ['isNot', 'Is not'], ['isEmpty', 'Is empty'],
    ['isNotEmpty', 'Is not empty'], ['greaterThan', 'Greater than'],
    ['lessThan', 'Less than'], ['greaterThanOrEquals', 'At least'],
    ['lessThanOrEquals', 'At most']
  ];
  var ALIASES = { notIncludes: 'doesNotInclude', equals: 'is', notEquals: 'isNot' };
  var NUMERIC = new Set(['greaterThan', 'lessThan', 'greaterThanOrEquals', 'lessThanOrEquals']);
  var FIELD_SET = new Set(FIELDS.map(function (f) { return f[0]; }));
  var OP_SET = new Set(OPS.map(function (o) { return o[0]; }));
  var MAX_DEPTH = 5, MAX_NODES = 50;
  function empty() { return { operator: 'and', filters: [] }; }
  function validate(input) {
    var size = 0;
    function group(value, depth, path) {
      if (!value || typeof value !== 'object' || Array.isArray(value) || depth > MAX_DEPTH) {
        throw new Error(path + ': expected a group (maximum nesting depth ' + MAX_DEPTH + ')');
      }
      if (value.operator !== 'and' && value.operator !== 'or') throw new Error(path + ': operator must be and or or');
      if (!Array.isArray(value.filters)) throw new Error(path + ': filters must be an array');
      return { operator: value.operator, filters: value.filters.map(function (item, i) {
        size++;
        if (size > MAX_NODES) throw new Error('Limit filters to ' + MAX_NODES + ' rules and groups');
        var at = path + '.filters[' + i + ']';
        if (item && typeof item === 'object' && !Array.isArray(item) && 'columnId' in item) {
          if ('filters' in item) throw new Error(at + ': ambiguous rule/group');
          if (!FIELD_SET.has(item.columnId)) throw new Error(at + ': unknown column ' + String(item.columnId));
          var op = ALIASES[item.operator] || item.operator;
          if (!OP_SET.has(op)) throw new Error(at + ': unknown operator ' + String(item.operator));
          if (typeof item.value !== 'string' && typeof item.value !== 'number' &&
              !(Array.isArray(item.value) && item.value.every(function (v) { return typeof v === 'string'; }))) {
            throw new Error(at + ': value must be text, a number, or a list of strings');
          }
          if (NUMERIC.has(op) && item.columnId !== 'price_value') {
            throw new Error(at + ': numeric comparisons are only available for Price');
          }
          if (NUMERIC.has(op) && (Array.isArray(item.value) || item.value === '' || !Number.isFinite(Number(item.value)))) {
            throw new Error(at + ': numeric comparison requires a number');
          }
          return { columnId: item.columnId, operator: op, value: item.value };
        }
        return group(item, depth + 1, at);
      }) };
    }
    return group(input, 0, 'root');
  }
  function match(rule, row) {
    var raw = row[rule.columnId];
    var blank = raw == null || String(raw).trim() === '';
    if (rule.operator === 'isEmpty') return blank;
    if (rule.operator === 'isNotEmpty') return !blank;
    if (NUMERIC.has(rule.operator)) {
      if (blank || !Number.isFinite(Number(raw))) return false;
      var n = Number(raw), target = Number(rule.value);
      if (rule.operator === 'greaterThan') return n > target;
      if (rule.operator === 'lessThan') return n < target;
      if (rule.operator === 'greaterThanOrEquals') return n >= target;
      return n <= target;
    }
    var text = blank ? '' : String(raw).toLocaleLowerCase();
    var values = (Array.isArray(rule.value) ? rule.value : [rule.value]).map(function (v) { return String(v).toLocaleLowerCase(); });
    if (rule.operator === 'is') return values.indexOf(text) !== -1;
    if (rule.operator === 'isNot') return values.indexOf(text) === -1;
    if (rule.operator === 'includes') return values.some(function (v) { return text.includes(v); });
    return values.every(function (v) { return !text.includes(v); });
  }
  function matches(tree, row) {
    if (!tree.filters.length) return true;
    var results = tree.filters.map(function (item) {
      return 'columnId' in item ? match(item, row) : matches(item, row);
    });
    return tree.operator === 'and' ? results.every(Boolean) : results.some(Boolean);
  }
  function count(tree) {
    return tree.filters.reduce(function (n, item) { return n + ('columnId' in item ? 1 : count(item)); }, 0);
  }
  global.ReviewFilter = { fields: FIELDS, operators: OPS, empty: empty,
    validate: validate, matches: matches, count: count };
})(window);
