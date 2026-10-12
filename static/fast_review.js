/* fast_review.js - Review queue "Fast Review", modelled on ProductWatch's FastReviewTable:
   - checkbox column; Gmail-style shift-click range select (the range takes the anchor
     click's action, so shift-clicking can also UN-select a range), anchor moves on click
   - header box = select / clear every row matching the current filters (not just on-screen)
   - toolbar: "N rows (M selected)" + Approve / Dispute / Not set / Clear icon buttons
   - keyboard (PW bindings): j/k move, Space toggle, Shift+J/K extend,
     Shift+A approve, Shift+D dispute, Shift+Z not set, Esc clear
   - one POST /api/review/bulk per action; rows update in place (no page reload, so
     sort/filters/scroll stay put); rows that leave the current view drop out; Undo.
   Depends on listing_table.js (ListingTable). */
(function (global) {
  'use strict';

  var LABELS = { approve: 'Approved', reject: 'Disputed', unset: 'Set back to pending' };
  var STATE_FOR = { approve: 'approved', reject: 'rejected', unset: 'pending' };
  var REVIEW_LABELS = { approved: 'Approved', rejected: 'Disputed', pending: 'Pending', not_queued: 'Not queued' };

  function init(cfg) {
    var params = cfg.params;
    var esc = ListingTable.escapeHtml;
    var tableEl = document.getElementById('queue-table');
    var $ = function (id) { return document.getElementById(id); };

    var selected = new Set();       // row ids; survives sorting, filtering and "show more"
    var shown = [];                 // rows on screen, in on-screen order (from onRender)
    var matchingCount = 0;          // rows passing the current filters
    var anchor = null;              // index in `shown` of the last plain click
    var anchorAction = 'select';    // what the anchor click did; shift-ranges repeat it
    var cursor = -1;                // keyboard row cursor (index in `shown`)
    var busy = false;
    var lastUndo = null;

    var selectColumn = {
      key: 'select', label: 'Select', thClass: 'col-select', tdClass: 'cell-select',
      headerHtml: '<input type="checkbox" class="fr-all" aria-label="Select all rows matching the filters">',
      cell: function (r) {
        var on = selected.has(r.id);
        return '<label class="fr-box"><input type="checkbox" data-select-id="' + esc(r.id) + '"' +
               (on ? ' checked' : '') + ' aria-label="Select item ' + esc(r.item_id || r.id) + '"></label>';
      }
    };

    var table = ListingTable.create(tableEl, {
      pageSize: 100,
      storageKey: 'prescreener_queue',
      emptyText: 'No items in this queue view.',
      leadColumns: [selectColumn],
      extraColumns: [
        { key: 'review', label: 'Review', thClass: 'col-short', filter: 'select',
          value: function (r) { return REVIEW_LABELS[r.review_state] || r.review_state || ''; },
          cell: function (r) {
            var s = r.review_state || '';
            return '<span class="badge review-' + esc(s) + '">' + esc(REVIEW_LABELS[s] || s) + '</span>';
          } },
        { key: 'actions', label: 'Actions', thClass: 'col-actions', cell: function (r) {
            return '<div class="fr-row-actions">' +
              '<button type="button" class="button success" data-row-action="approve" data-id="' + esc(r.id) + '">Approve</button>' +
              '<button type="button" class="button destructive" data-row-action="reject" data-id="' + esc(r.id) + '">Dispute</button></div>';
          } }
      ],
      onRender: function (rows, total) {
        shown = rows;
        matchingCount = total;
        if (cursor >= shown.length) cursor = shown.length - 1;
        paint();
      }
    });

    // ---------- selection painting (cheap: no table re-render) ----------
    function paint() {
      var trs = tableEl.tBodies[0].rows;
      for (var i = 0; i < trs.length; i++) {
        if (!trs[i].dataset.rowId) continue;
        var on = selected.has(Number(trs[i].dataset.rowId));
        trs[i].classList.toggle('fr-selected-row', on);
        trs[i].classList.toggle('fr-cursor', i === cursor);
        var box = trs[i].querySelector('input[data-select-id]');
        if (box) box.checked = on;
      }
      var all = tableEl.querySelector('.fr-all');
      if (all) {
        all.checked = matchingCount > 0 && selected.size >= matchingCount;
        all.indeterminate = selected.size > 0 && selected.size < matchingCount;
      }
      var n = selected.size;
      var onScreen = 0;
      shown.forEach(function (r) { if (selected.has(r.id)) onScreen++; });
      $('fr-rowcount').textContent = table.getRows().length ? matchingCount.toLocaleString() + ' of ' +
        table.getRows().length.toLocaleString() + ' rows' : '0 rows';
      $('fr-selected').hidden = n === 0;
      // a selection you can't see is a trap: say so (e.g. after sorting moved rows past "Show more")
      $('fr-selected').textContent = '(' + n.toLocaleString() + ' selected' +
        (n > onScreen ? ', ' + (n - onScreen).toLocaleString() + ' not on screen' : '') + ')';
      document.querySelectorAll('[data-bulk], #fr-clear').forEach(function (b) { b.disabled = n === 0 || busy; });
    }

    function setRange(from, to, action) {
      var lo = Math.min(from, to), hi = Math.max(from, to);
      for (var i = lo; i <= hi; i++) {
        if (!shown[i]) continue;
        if (action === 'select') selected.add(shown[i].id); else selected.delete(shown[i].id);
      }
    }

    function clickRow(index, shift) {
      var row = shown[index];
      if (!row) return;
      if (shift && anchor !== null) {
        setRange(anchor, index, anchorAction);       // Gmail: repeat the anchor's action
      } else {
        anchorAction = selected.has(row.id) ? 'deselect' : 'select';
        if (anchorAction === 'select') selected.add(row.id); else selected.delete(row.id);
      }
      anchor = index;                                 // chained shift-clicks continue from here
      cursor = index;
      paint();
    }

    // checkbox clicks: we own the checked state, so stop the browser toggling it itself
    tableEl.addEventListener('mousedown', function (e) {
      if (e.shiftKey && e.target.closest('.fr-box')) e.preventDefault();   // no text selection on shift-click
    });
    tableEl.addEventListener('click', function (e) {
      var box = e.target.closest('.fr-box');
      if (box) {
        e.preventDefault();
        var tr = box.closest('tr');
        clickRow(Array.prototype.indexOf.call(tableEl.tBodies[0].rows, tr), e.shiftKey);
        return;
      }
      var act = e.target.closest('[data-row-action]');
      if (act) bulk(act.dataset.rowAction, [Number(act.dataset.id)]);
    });
    tableEl.tHead.addEventListener('change', function (e) {
      if (!e.target.classList.contains('fr-all')) return;
      if (selected.size > 0) selected.clear();         // PW: any selection -> clear; none -> select all
      else table.getMatchingRows().forEach(function (r) { selected.add(r.id); });
      anchor = null;
      paint();
    });

    // ---------- bulk actions ----------
    function bulk(action, ids) {
      if (busy || !ids.length) return;
      var byId = {};
      table.getRows().forEach(function (r) { byId[r.id] = r; });
      var before = ids.map(function (id) { return { id: id, state: byId[id] && byId[id].review_state }; });
      busy = true;
      paint();
      say('Saving ' + ids.length.toLocaleString() + ' row' + (ids.length === 1 ? '' : 's') + '...');
      post(action, ids).then(function (res) {
        applyStates(ids, STATE_FOR[action]);
        ids.forEach(function (id) { selected.delete(id); });
        lastUndo = before;
        say(LABELS[action] + ' ' + res.updated.toLocaleString() + ' row' + (res.updated === 1 ? '' : 's') + '.', true);
      }).catch(function (err) {
        say('Could not save (' + err.message + '). Nothing was changed.', false, true);
      }).then(function () { busy = false; paint(); });
    }

    function post(action, ids) {
      return fetch('/api/review/bulk', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: action, ids: ids })
      }).then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); });
    }

    // update rows in place; rows that no longer belong in this state view drop out
    function applyStates(ids, state) {
      var set = new Set(ids);
      var keep = table.getRows().filter(function (r) {
        if (!set.has(r.id)) return true;
        r.review_state = state;
        return params.state === 'all' || params.state === state;
      });
      anchor = null;
      table.setRows(keep);
    }

    function undo() {
      if (!lastUndo || busy) return;
      var groups = {};
      lastUndo.forEach(function (u) { (groups[u.state] = groups[u.state] || []).push(u.id); });
      var undoing = lastUndo;
      lastUndo = null;
      busy = true;
      say('Undoing...');
      var ACTION_FOR = { approved: 'approve', rejected: 'reject', pending: 'unset' };
      Promise.all(Object.keys(groups).filter(function (s) { return ACTION_FOR[s]; }).map(function (s) {
        return post(ACTION_FOR[s], groups[s]);
      })).then(function () {
        say('Undone. Reloading the queue...');
        return reload();
      }).catch(function (err) {
        lastUndo = undoing;
        say('Undo failed (' + err.message + ').', false, true);
      }).then(function () { busy = false; paint(); });
    }

    function say(msg, withUndo, isError) {
      var el = $('fr-status');
      el.classList.toggle('error', !!isError);
      el.innerHTML = esc(msg) + (withUndo ? ' <button type="button" class="button secondary" id="fr-undo" style="height:24px;font-size:12px;margin-left:6px;">Undo</button>' : '');
      var u = $('fr-undo');
      if (u) u.addEventListener('click', undo);
    }

    // ---------- toolbar + keyboard ----------
    document.querySelectorAll('[data-bulk]').forEach(function (b) {
      b.addEventListener('click', function () { bulk(b.dataset.bulk, Array.from(selected)); });
    });
    $('fr-clear').addEventListener('click', function () { selected.clear(); anchor = null; paint(); });
    $('fr-help-toggle').addEventListener('click', function () {
      var open = $('fr-help').hidden;
      $('fr-help').hidden = !open;
      this.setAttribute('aria-expanded', String(open));
    });

    function moveCursor(delta, extend) {
      if (!shown.length) return;
      var next = Math.max(0, Math.min(shown.length - 1, (cursor < 0 ? (delta > 0 ? -1 : 0) : cursor) + delta));
      if (extend) {
        if (anchor === null) { anchor = cursor < 0 ? next : cursor; anchorAction = 'select'; }
        setRange(anchor, next, 'select');
      }
      cursor = next;
      paint();
      var tr = tableEl.tBodies[0].rows[cursor];
      if (tr) tr.scrollIntoView({ block: 'nearest' });
    }

    document.addEventListener('keydown', function (e) {
      var t = e.target;
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      if (t.closest && t.closest('input:not([type=checkbox]), select, textarea, [contenteditable]')) return;
      var k = e.key;
      var handled = true;
      if (k === 'j' || k === 'J') moveCursor(1, e.shiftKey);
      else if (k === 'k' || k === 'K') moveCursor(-1, e.shiftKey);
      else if (k === ' ' && cursor >= 0 && !(t.closest && t.closest('button, a'))) clickRow(cursor, false);
      else if (e.shiftKey && (k === 'A' || k === 'a')) bulk('approve', Array.from(selected));
      else if (e.shiftKey && (k === 'D' || k === 'd')) bulk('reject', Array.from(selected));
      else if (e.shiftKey && (k === 'Z' || k === 'z')) bulk('unset', Array.from(selected));
      else if (k === 'Escape') { selected.clear(); anchor = null; paint(); }
      else handled = false;
      if (handled) e.preventDefault();
    });

    // ---------- load ----------
    function reload() {
      return fetch('/api/queue?' + new URLSearchParams(params))
        .then(function (res) { if (!res.ok) throw new Error('HTTP ' + res.status); return res.json(); })
        .then(function (rows) {
          var ids = new Set(rows.map(function (r) { return r.id; }));
          selected.forEach(function (id) { if (!ids.has(id)) selected.delete(id); });
          anchor = null;
          table.setRows(rows);
        });
    }
    tableEl.tBodies[0].innerHTML = '<tr><td colspan="13" class="muted">Loading queue...</td></tr>';
    reload().catch(function (err) {
      tableEl.tBodies[0].innerHTML = '<tr><td colspan="13" class="muted">Couldn\'t load the queue (' +
        esc(err.message) + '). Refresh to try again.</td></tr>';
    });
  }

  global.FastReview = { init: init };
})(window);
