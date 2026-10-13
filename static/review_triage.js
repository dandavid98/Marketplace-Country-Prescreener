/* Review-only grouping and triage. Never turns evidence heuristics into decisions. */
(function (global) {
  'use strict';
  var LANES = [
    ['all', 'All'], ['walmart', 'Walmart sold & shipped'],
    ['approved_before', 'Approved before'], ['generic', 'Generic brand'],
    ['missing_brand', 'Missing brand'], ['verify_seller', 'Verify seller evidence']
  ];
  function groupKey(row) {
    var id = String(row.item_id || '').trim();
    return id && id !== 'Unknown' ? 'item:' + id : 'row:' + row.id;
  }
  function prepare(rows, lane, grouped) {
    var inLane = rows.filter(function (r) {
      return lane === 'all' || (r.triage_lanes || []).indexOf(lane) !== -1;
    });
    if (!grouped) return inLane.map(function (r) { return Object.assign({}, r, { copies: [r] }); });
    var groups = new Map();
    inLane.forEach(function (r) {
      var key = groupKey(r);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(r);
    });
    return Array.from(groups.values()).map(function (copies) {
      return Object.assign({}, copies[0], { copies: copies });
    });
  }
  function counts(rows) {
    var result = {};
    LANES.forEach(function (entry) {
      var matches = prepare(rows, entry[0], false);
      result[entry[0]] = { rows: matches.length,
        items: new Set(matches.map(groupKey)).size };
    });
    return result;
  }
  global.ReviewTriage = { lanes: LANES, prepare: prepare, counts: counts };
})(window);
