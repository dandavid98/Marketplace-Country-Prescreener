/* fast_review.js - Review queue "Fast Review", modelled on ProductWatch's FastReviewTable:
   - checkbox column; Gmail-style shift-click range select (the range takes the anchor
     click's action, so shift-clicking can also UN-select a range), anchor moves on click
   - header box = select / clear rows currently shown (not hidden rows)
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
  var REVIEW_LABELS = { approved: 'Approved', rejected: 'Disputed', pending: 'Pending', not_queued: 'False Positives' };
  // decision memory (decision_memory.py): why a row got its state without you clicking it
  var REMEMBERED = {
    auto_disputed: { label: 'auto', title: 'Auto-disputed: you disputed this item ID before, so it skipped the pending queue.' },
    approved_before: { label: 'approved before', title: 'You approved this item ID before. Still pending so you can check it again.' }
  };
  function reviewLabel(r) {
    var base = REVIEW_LABELS[r.review_state] || r.review_state || '';
    var note = REMEMBERED[r.remembered];
    return note ? base + ' (' + note.label + ')' : base;
  }

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
    var sourceRows = [];
    var lane = new URLSearchParams(location.search).get('lane') || 'all';
    if (!ReviewTriage.lanes.some(function (entry) { return entry[0] === lane; })) lane = 'all';
    var grouped = false;
    var expanded = new Set();
    var pendingDecision = null;
    var previewBusy = false;
    var builder = ReviewFilterBuilder.build({
      blocked: function () { return busy || previewBusy || !$('fr-preview').hidden; },
      onApply: function () { selected.clear(); expanded.clear(); refreshView(); },
      onClose: function () { $('fr-filter-open').focus(); }
    });

    function directIds(rows) {
      return Array.from(new Set(rows.reduce(function (ids, row) {
        (row.copies || [row]).forEach(function (copy) { ids.push(copy.id); });
        return ids;
      }, [])));
    }
    function selectedRows() {
      return table.getRows().filter(function (row) { return selected.has(row.id); });
    }
    function counts() {
      var summary = ReviewTriage.counts(sourceRows.filter(builder.matches));
      $('fr-lanes').innerHTML = ReviewTriage.lanes.map(function (entry) {
        var s = summary[entry[0]];
        return '<option value="' + entry[0] + '"' + (lane === entry[0] ? ' selected' : '') + '>' +
          esc(entry[1]) + ' (' + s.items.toLocaleString() + ' items / ' + s.rows.toLocaleString() + ' rows)</option>';
      }).join('');
    }
    function refreshView() {
      counts();
      anchor = null;
      var filtered = sourceRows.filter(builder.matches);
      table.setRows(ReviewTriage.prepare(filtered, lane, grouped));
    }
    $('fr-lanes').addEventListener('change', function () {
      if (busy || previewBusy || !$('fr-preview').hidden) { this.value = lane; return; }
      lane = this.value;
      history.replaceState(null, '', location.pathname + '?' + new URLSearchParams(Object.assign({}, params, lane === 'all' ? {} : { lane: lane })));
      selected.clear();
      expanded.clear();
      refreshView();
    });
    $('fr-group').addEventListener('change', function () {
      if (busy || previewBusy) { this.checked = grouped; return; }
      grouped = this.checked;
      selected.clear();
      expanded.clear();
      refreshView();
    });

    var selectColumn = {
      key: 'select', label: 'Select', thClass: 'col-select', tdClass: 'cell-select',
      headerHtml: '<input type="checkbox" class="fr-all" aria-label="Select all rows currently shown">',
      cell: function (r) {
        var on = selected.has(r.id);
        return '<label class="fr-box"><input type="checkbox" data-select-id="' + esc(r.id) + '"' +
               (on ? ' checked' : '') + ' aria-label="Select item ' + esc(r.item_id || r.id) + '"></label>';
      }
    };

    var table = ListingTable.create(tableEl, {
      pageSize: 100,
      showMoreChoices: [100, 500, 2000, 5000, 10000, 'all'],
      paginationTarget: $('fr-pagination'),
      storageKey: 'prescreener_queue',
      emptyText: 'No items in this queue view.',
      leadColumns: [selectColumn, {
        key: 'copies', label: 'Copies', thClass: 'col-short', cell: function (r) {
          if (!grouped || r.copies.length < 2) return '';
          var open = expanded.has(r.id);
          return '<button type="button" class="fr-copies" data-copies="' + esc(r.id) +
            '" aria-expanded="' + open + '" aria-label="' + (open ? 'Hide' : 'Show') + ' ' +
            r.copies.length + ' copies of item ' + esc(r.item_id) + '">' +
            r.copies.length + ' copies ' + (open ? '▴' : '▾') + '</button>';
        }
      }],
      afterTitleColumns: [
        { key: 'listing_brand', label: 'Listing Brand', thClass: 'col-mid', filter: 'text',
          value: function (r) { return r.listing_brand || ''; } }
      ],
      afterCounterfeitColumns: [
        { key: 'date_scanned', label: 'Date Scanned', thClass: 'col-mid', filter: 'text',
          value: function (r) {
            var stamp = r.created_at || '';
            if (!stamp) return '';
            var date = new Date(stamp);
            return isNaN(date.getTime()) ? stamp.slice(0, 10) :
              [date.getFullYear(), String(date.getMonth() + 1).padStart(2, '0'),
                String(date.getDate()).padStart(2, '0')].join('-');
          } }
      ],
      extraColumns: [
        { key: 'review_category', label: 'Category', thClass: 'col-mid', filter: 'select',
          value: function (r) { return r.review_category || ''; },
          cell: function (r) { return esc(r.review_category || ''); } },
        { key: 'review', label: 'Review', thClass: 'col-short', filter: 'select',
          value: reviewLabel,
          cell: function (r) {
            var s = r.review_state || '';
            var note = REMEMBERED[r.remembered];
            return '<span class="badge review-' + esc(s) + '">' + esc(REVIEW_LABELS[s] || s) + '</span>' +
              (note ? '<span class="badge remembered" title="' + esc(note.title) + '">' + esc(note.label) + '</span>' : '');
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
        renderCopies();
      }
    });

    function renderCopies() {
      tableEl.tBodies[0].querySelectorAll('.fr-copy-detail').forEach(function (el) { el.remove(); });
      if (!grouped) return;
      shown.forEach(function (row) {
        if (!expanded.has(row.id) || row.copies.length < 2) return;
        var parent = tableEl.tBodies[0].querySelector('tr[data-row-id="' + row.id + '"]');
        if (!parent) return;
        var detail = document.createElement('tr');
        detail.className = 'fr-copy-detail';
        var cell = document.createElement('td');
        cell.colSpan = tableEl.tHead.rows[0].cells.length;
        row.copies.forEach(function (copy) {
          var item = document.createElement('div');
          item.className = 'fr-copy-item';
          var a = document.createElement('a');
          a.href = copy.listing_url || '#';
          a.target = '_blank'; a.rel = 'noopener noreferrer';
          a.textContent = copy.title || 'Listing ' + copy.id;
          var info = document.createElement('span');
          info.textContent = ' · Seller: ' + (copy.seller_name || copy.seller_id || 'Unknown') +
            ' · Scan: ' + (copy.job_id || 'Unknown') + ' · ' + (copy.review_state || 'Unknown');
          item.appendChild(a); item.appendChild(info); cell.appendChild(item);
        });
        detail.appendChild(cell); parent.after(detail);
      });
    }

    // ---------- selection painting (cheap: no table re-render) ----------
    function paint() {
      var trs = tableEl.tBodies[0].querySelectorAll('tr[data-row-id]');
      for (var i = 0; i < trs.length; i++) {
        var on = selected.has(Number(trs[i].dataset.rowId));
        trs[i].classList.toggle('fr-selected-row', on);
        trs[i].classList.toggle('fr-cursor', i === cursor);
        var box = trs[i].querySelector('input[data-select-id]');
        if (box) box.checked = on;
      }
      var all = tableEl.querySelector('.fr-all');
      var onScreen = 0;
      shown.forEach(function (r) { if (selected.has(r.id)) onScreen++; });
      if (all) {
        all.checked = shown.length > 0 && onScreen === shown.length;
        all.indeterminate = onScreen > 0 && onScreen < shown.length;
      }
      var n = selected.size;
      $('fr-rowcount').textContent = table.getRows().length ? matchingCount.toLocaleString() + ' of ' +
        table.getRows().length.toLocaleString() + ' rows' : '0 rows';
      $('fr-selected').hidden = n === 0;
      // a selection you can't see is a trap: say so (e.g. after sorting moved rows past "Show more")
      $('fr-selected').textContent = '(' + n.toLocaleString() + ' selected' +
        (n > onScreen ? ', ' + (n - onScreen).toLocaleString() + ' not on screen' : '') + ')';
      document.querySelectorAll('[data-bulk], #fr-clear').forEach(function (b) { b.disabled = n === 0 || busy; });
      var eligible = n > 0 && selectedRows().length === n && selectedRows().every(function (row) {
        return (row.copies || [row]).every(function (copy) { return copy.review_state !== 'rejected'; });
      });
      $('fr-category').disabled = !eligible || busy || previewBusy || !$('fr-preview').hidden;
      $('fr-category-assign').disabled = !eligible || busy || previewBusy || !$('fr-preview').hidden || !$('fr-category').value;
      $('export-selected').disabled = n === 0 || busy;
      $('export-selected').textContent = 'Export selected (' + n.toLocaleString() + ')';
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

    // Let the browser finish toggling the checkbox (including clicks on its label)
    // before syncing it with selection. Preventing its click default action used
    // to undo paint() after the handler ran, leaving the tick one click behind.
    var checkboxShift = false;
    // Shift-click must keep its native click/change events; cancelling mousedown
    // stops the checkbox from changing at all in some browsers.
    tableEl.addEventListener('pointerdown', function (e) {
      if (e.target.closest('.fr-box')) checkboxShift = e.shiftKey;
    });
    tableEl.addEventListener('click', function (e) {
      if (e.target.matches('input[data-select-id]')) checkboxShift = checkboxShift || e.shiftKey;
      var act = e.target.closest('[data-row-action]');
      if (act) bulk(act.dataset.rowAction, [Number(act.dataset.id)]);
      var copies = e.target.closest('[data-copies]');
      if (copies) {
        var id = Number(copies.dataset.copies);
        if (expanded.has(id)) expanded.delete(id); else expanded.add(id);
        table.refresh();
      }
    });
    tableEl.addEventListener('change', function (e) {
      if (!e.target.matches('input[data-select-id]')) return;
      var tr = e.target.closest('tr');
      clickRow(Array.prototype.indexOf.call(tableEl.tBodies[0].querySelectorAll('tr[data-row-id]'), tr), checkboxShift || e.shiftKey);
      checkboxShift = false;
    });
    tableEl.tHead.addEventListener('change', function (e) {
      if (!e.target.classList.contains('fr-all')) return;
      var visibleIds = shown.map(function (r) { return r.id; });
      if (visibleIds.length && visibleIds.every(function (id) { return selected.has(id); })) {
        visibleIds.forEach(function (id) { selected.delete(id); });
      } else {
        visibleIds.forEach(function (id) { selected.add(id); });
      }
      anchor = null;
      paint();
    });

    $('fr-category').addEventListener('change', paint);
    $('fr-category-assign').addEventListener('click', function () {
      if (busy || previewBusy || !$('fr-preview').hidden || $('fr-filter-dialog').open || this.disabled) return;
      var category = $('fr-category').value;
      var ids = directIds(selectedRows());
      if (!category || !ids.length || ids.length > 5000) {
        say('Choose at most 5,000 non-disputed saved rows and a category.', false, true);
        return;
      }
      if (!window.confirm((category === '__clear__' ? 'Remove category from ' : 'Assign ' + category + ' to ') +
          ids.length.toLocaleString() + ' selected rows?')) return;
      busy = true;
      paint();
      fetch('/api/review/category', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: ids, category: category === '__clear__' ? '' : category, job: params.job })
      }).then(function (res) {
        if (!res.ok) return res.json().then(function (body) { throw new Error(body.error || 'HTTP ' + res.status); });
        return res.json();
      }).then(function (result) {
        $('fr-category').value = '';
        say('Categorized ' + result.updated.toLocaleString() + ' rows. Selection kept for your next action.', false);
        return reload();
      }).catch(function (err) { say('Could not assign category: ' + err.message, false, true); })
        .then(function () { busy = false; paint(); });
    });

    $('export-selected').addEventListener('click', function () {
      if (busy || !selected.size) return;
      var button = this;
      button.disabled = true;
      var ids = directIds(selectedRows());
      if (!ids.length) return;
      var query = new URLSearchParams({ job: params.job, scope: $('export-scope').value,
        category: $('export-category').value });
      fetch('/export.xlsx?' + query, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(ids)
      }).then(function (response) {
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return response.blob();
      }).then(function (blob) {
        var url = URL.createObjectURL(blob);
        var link = document.createElement('a');
        link.href = url;
        link.download = 'prescreener_selected_' + $('export-scope').value + '.xlsx';
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(function () { URL.revokeObjectURL(url); }, 60000);
        say('Selected rows exported.', true);
      }).catch(function (err) {
        say('Could not export selected rows (' + err.message + ').', false, true);
      }).then(function () { paint(); });
    });

    // ---------- decisions: explicit preview before EVERY write ----------
    function bulk(action, ids) {
      if (busy || previewBusy || !$('fr-preview').hidden || $('fr-filter-dialog').open) return;
      if (!ids.length) return;
      var selectedSet = new Set(ids);
      var selectedDisplay = table.getRows().filter(function (row) { return selectedSet.has(row.id); });
      var rowIds = directIds(selectedDisplay);
      if (!rowIds.length) {
        say('Select at least one saved row.', false, true);
        return;
      }
      previewBusy = true;
      var body = JSON.stringify({ action: action, ids: rowIds, job: params.job });
      fetch('/api/review/preview', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body })
        .then(function (r) { if (!r.ok) return r.json().then(function (d) { throw new Error(d.error || 'HTTP ' + r.status); }); return r.json(); })
        .then(function (info) {
          pendingDecision = { action: action, ids: rowIds };
          $('fr-preview-text').textContent = LABELS[action] + ': ' + info.rows.toLocaleString() +
            ' selected saved rows representing ' + info.items.toLocaleString() + ' item IDs. ' +
            info.pending_repeats.toLocaleString() + ' other pending repeats may change through decision memory. ' +
            info.remembered_repeats.toLocaleString() + ' existing auto-disputed / approved-before copies may have their notes or state updated. ' +
            (info.without_item_id ? info.without_item_id + ' rows have no item ID and cannot be remembered. ' : '') +
            (info.walmart_rows ? info.walmart_rows + ' Walmart-sold rows appear under False Positives (not flagged for review); disputing them remembers the item ID. ' : '') +
            'Preview is a snapshot; nothing changes until you confirm.';
          $('fr-preview').hidden = false;
          $('fr-confirm').focus();
        }).catch(function (err) { say('Cannot preview decision: ' + err.message, false, true); })
        .then(function () { previewBusy = false; });
    }
    function closePreview() {
      $('fr-preview').hidden = true;
      pendingDecision = null;
      $('fr-clear').focus();
    }
    $('fr-preview').addEventListener('keydown', function (e) {
      if (e.key !== 'Tab') return;
      var focus = [$('fr-confirm'), $('fr-cancel')];
      if (e.shiftKey && document.activeElement === focus[0]) { focus[1].focus(); e.preventDefault(); }
      else if (!e.shiftKey && document.activeElement === focus[1]) { focus[0].focus(); e.preventDefault(); }
    });
    $('fr-cancel').addEventListener('click', closePreview);
    $('fr-confirm').addEventListener('click', function () {
      if (!pendingDecision || busy) return;
      var decision = pendingDecision;
      closePreview();
      saveDecision(decision.action, decision.ids);
    });

    function saveDecision(action, ids) {
      busy = true;
      paint();
      say('Saving ' + ids.length.toLocaleString() + ' row' + (ids.length === 1 ? '' : 's') + '...');
      post(action, ids).then(function (res) {
        lastUndo = ids.map(function (id) {
          var row = sourceRows.find(function (r) { return r.id === id; });
          return { id: id, state: row && row.review_state };
        });
        selected.clear();
        var msg = LABELS[action] + ' ' + res.updated.toLocaleString() + ' row' + (res.updated === 1 ? '' : 's');
        say(msg + (res.cascaded ? ', plus ' + res.cascaded.toLocaleString() + ' remembered repeats.' : '.'), true);
        return reload();
      }).catch(function (err) {
        say('Could not save (' + err.message + '). Refresh to check current state.', false, true);
      }).then(function () { busy = false; paint(); });
    }

    function post(action, ids) {
      return fetch('/api/review/bulk', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: action, ids: ids, job: params.job, confirmed: true })
      }).then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); });
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
      var tr = tableEl.tBodies[0].querySelectorAll('tr[data-row-id]')[cursor];
      if (tr) tr.scrollIntoView({ block: 'nearest' });
    }

    document.addEventListener('keydown', function (e) {
      var t = e.target;
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      var k = e.key;
      if (k === 'Escape' && !$('fr-preview').hidden) { closePreview(); e.preventDefault(); return; }
      if (!$('fr-preview').hidden || $('fr-filter-dialog').open) return;
      if (t.closest && t.closest('input:not([type=checkbox]), select, textarea, [contenteditable]')) return;
      var handled = true;
      if (k === 'j' || k === 'J') moveCursor(1, e.shiftKey);
      else if (k === 'k' || k === 'K') moveCursor(-1, e.shiftKey);
      else if (k === ' ' && cursor >= 0 && !(t.closest && t.closest('button, a, input[type=checkbox]'))) clickRow(cursor, false);
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
          sourceRows = rows;
          refreshView();
        });
    }
    tableEl.tBodies[0].innerHTML = '<tr><td colspan="15" class="muted">Loading queue...</td></tr>';
    reload().catch(function (err) {
      tableEl.tBodies[0].innerHTML = '<tr><td colspan="15" class="muted">Couldn\'t load the queue (' +
        esc(err.message) + '). Refresh to try again.</td></tr>';
    });
  }

  global.FastReview = { init: init };
})(window);
