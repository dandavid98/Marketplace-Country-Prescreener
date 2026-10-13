/* listing_table.js - one listing table, shared by the Live scan page ("Latest scan
   rows": China / US / Unknown) and the Review queue, so both show the same
   columns, thumbnails (hover preview via initThumbPreview) and sort/filter UI.

   Usage:
     const t = ListingTable.create(tableEl, { pageSize, emptyText, extraColumns, storageKey });
     t.setRows(rows);   // call again whenever data changes; sort/filter state is kept

   storageKey (optional): remembers sort + filters in sessionStorage, so they
   survive the full-page reload after Approve/Reject on the queue.
   leadColumns (optional): columns placed BEFORE the base ones (e.g. a select box).
   afterTitleColumns (optional): columns placed after Title (e.g. queue listing brand).
   onRender(shownRows) (optional): called after every render with the rows on screen,
   in on-screen order. Rows with an `id` get <tr data-row-id="...">.

   Column spec: { key, label, headerHtml?, value(r), sortValue?(r), search?(r), cell?(r),
                  filter?: 'text' | 'select', thClass?, tdClass? }
   A column is sortable when it has value or sortValue. */
(function (global) {
  'use strict';

  function esc(v) {
    return String(v == null ? '' : v).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function link(href, text, title) {
    var t = title ? ' title="' + esc(title) + '"' : '';
    return href ? '<a href="' + esc(href) + '" target="_blank" rel="noopener noreferrer"' + t + '>' + esc(text) + '</a>'
                : '<span' + t + '>' + esc(text) + '</span>';
  }
  function priceText(r) {
    return r.price_text || (r.price_value != null ? '$' + Number(r.price_value).toFixed(2) : 'Unknown');
  }
  var KIND_LABELS = { disguised: 'Disguised', fuzzy: 'Fuzzy' };
  function kindBadge(r) {   // how the keyword matched; nothing for plain exact matches
    var label = KIND_LABELS[r.match_kind];
    return label ? '<span class="badge kind-' + esc(r.match_kind) + '" title="' + esc(r.evidence || '') + '">' + label + '</span>' : '';
  }

  var BASE_COLUMNS = [
    { key: 'item_id', label: 'Item ID', thClass: 'col-id', filter: 'text', value: function (r) { return r.item_id || ''; } },
    { key: 'image', label: 'Image', thClass: 'col-image', cell: function (r) {
        if (!r.image_url) return '<span class="muted">None</span>';
        var src = esc(r.image_url);
        return '<a class="thumb-link" href="' + src + '" target="_blank" rel="noopener noreferrer" data-preview-src="' + src + '">' +
               '<img class="thumb-image" src="' + src + '" alt="Listing image" loading="lazy"></a>';
      } },
    { key: 'price', label: 'Price', thClass: 'col-price', value: priceText,
      sortValue: function (r) { return r.price_value == null ? null : Number(r.price_value); } },
    { key: 'keyword', label: 'Keyword', thClass: 'col-mid', filter: 'select', value: function (r) { return r.keyword || ''; } },
    { key: 'title', label: 'Title', filter: 'text',
      value: function (r) { return r.title || r.listing_url || ''; },
      cell: function (r) { return link(r.listing_url, r.title || r.listing_url || ''); } },
    { key: 'seller', label: 'Seller', thClass: 'col-mid', filter: 'text',
      value: function (r) { return r.seller_id || 'Unknown'; },
      search: function (r) { return (r.seller_id || '') + ' ' + (r.seller_name || ''); },
      cell: function (r) { return link(r.seller_page_url, r.seller_id || 'Unknown', r.seller_name); } },
    { key: 'country', label: 'Country', thClass: 'col-short', filter: 'select', value: function (r) { return r.country_signal || 'Unknown'; } },
    { key: 'counterfeit', label: 'Counterfeit Reviews', thClass: 'col-short',
      sortValue: function (r) { return Number(r.counterfeit_review_count || 0); },
      cell: function (r) {
        var n = Number(r.counterfeit_review_count || 0);
        return n ? '<span class="badge counterfeit-flag">' + n + '</span>' : '0';
      } },
    { key: 'status', label: 'Status', thClass: 'col-short', filter: 'select',
      value: function (r) { return r.scan_status || 'unknown'; },
      cell: function (r) {
        var s = r.scan_status || 'unknown';
        return '<span class="badge ' + esc(s) + '" title="' + esc(r.evidence || '') + '">' + esc(s) + '</span>';
      } },
    { key: 'match_kind', label: 'Matched as', thClass: 'col-short', filter: 'select',
      value: function (r) { return KIND_LABELS[r.match_kind] || 'Exact'; },
      cell: function (r) { return kindBadge(r) || '<span class="muted">Exact</span>'; } }
  ];

  var ARROWS = { none: '\u2195', ascending: '\u2191', descending: '\u2193' };

  function compare(a, b) {          // nulls always last; numbers numeric; text natural
    if (a === null || a === '') return (b === null || b === '') ? 0 : 1;
    if (b === null || b === '') return -1;
    if (typeof a === 'number' && typeof b === 'number') return a - b;
    return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: 'base' });
  }

  function create(table, opts) {
    opts = opts || {};
    var titleIndex = BASE_COLUMNS.findIndex(function (column) { return column.key === 'title'; }) + 1;
    var columns = (opts.leadColumns || []).concat(
      BASE_COLUMNS.slice(0, titleIndex), opts.afterTitleColumns || [],
      BASE_COLUMNS.slice(titleIndex), opts.extraColumns || []);
    var pageSize = opts.pageSize || 100;
    var moreChoices = opts.showMoreChoices || null;
    var pagination = opts.paginationTarget || table.tFoot;
    var state = { rows: [], sortKey: null, sortDir: 'none', filters: {}, limit: pageSize, optionSigs: {} };
    var byKey = {};
    columns.forEach(function (c) { byKey[c.key] = c; });
    var sortable = function (c) { return !!(c.value || c.sortValue); };

    function loadSaved() {
      if (!opts.storageKey) return;
      try {
        var saved = JSON.parse(sessionStorage.getItem(opts.storageKey) || 'null');
        if (!saved) return;
        if (saved.sortKey && byKey[saved.sortKey]) { state.sortKey = saved.sortKey; state.sortDir = saved.sortDir; }
        Object.keys(saved.filters || {}).forEach(function (k) { if (byKey[k]) state.filters[k] = saved.filters[k]; });
      } catch (e) { /* corrupt or blocked storage: start fresh */ }
    }
    function save() {
      if (!opts.storageKey) return;
      try {
        sessionStorage.setItem(opts.storageKey, JSON.stringify({ sortKey: state.sortKey, sortDir: state.sortDir, filters: state.filters }));
      } catch (e) { /* storage full or blocked: sorting still works, just isn't remembered */ }
    }
    loadSaved();

    table.classList.add('listing-table');
    table.innerHTML =
      '<thead><tr>' + columns.map(function (c) {
        var cls = c.thClass ? ' class="' + c.thClass + '"' : '';
        if (c.headerHtml) return '<th scope="col"' + cls + '>' + c.headerHtml + '</th>';
        if (!sortable(c)) return '<th scope="col"' + cls + '>' + esc(c.label) + '</th>';
        return '<th scope="col"' + cls + ' data-key="' + c.key + '" aria-sort="none">' +
               '<button type="button" class="sort-btn" title="Sort by ' + esc(c.label) + '">' + esc(c.label) +
               '<span class="sort-arrow" aria-hidden="true">' + ARROWS.none + '</span></button></th>';
      }).join('') + '</tr>' +
      '<tr class="filter-row">' + columns.map(function (c) {
        var label = ' aria-label="Filter by ' + esc(c.label) + '"';
        if (c.filter === 'text') return '<th><input type="search" data-filter="' + c.key + '" placeholder="Filter"' + label + '></th>';
        if (c.filter === 'select') return '<th><select data-filter="' + c.key + '"' + label + '><option value="">All</option></select></th>';
        return '<th></th>';
      }).join('') + '</tr></thead><tbody></tbody><tfoot></tfoot>';

    var tbody = table.tBodies[0], tfoot = table.tFoot;
    // put restored text filters back in their boxes (selects are restored once options exist)
    table.tHead.querySelectorAll('input[data-filter]').forEach(function (inp) {
      inp.value = state.filters[inp.dataset.filter] || '';
    });

    table.tHead.addEventListener('click', function (e) {
      var btn = e.target.closest('.sort-btn');
      if (!btn) return;
      var key = btn.parentNode.dataset.key;
      // cycle: ascending -> descending -> off
      if (state.sortKey !== key) { state.sortKey = key; state.sortDir = 'ascending'; }
      else if (state.sortDir === 'ascending') state.sortDir = 'descending';
      else { state.sortKey = null; state.sortDir = 'none'; }
      state.limit = pageSize;
      save();
      render();
    });
    function onFilter(e) {
      var key = e.target.dataset.filter;
      if (!key) return;
      var v = e.target.value.trim();
      v = e.target.tagName === 'SELECT' ? v : v.toLowerCase();
      // text boxes fire 'change' again on blur; re-rendering then would swap out the
      // row button the user is clicking, eating the click. Only render on a real change.
      if ((state.filters[key] || '') === v) return;
      state.filters[key] = v;
      state.limit = pageSize;
      save();
      render();
    }
    table.tHead.addEventListener('input', onFilter);
    table.tHead.addEventListener('change', onFilter);
    pagination.addEventListener('click', function (e) {
      if (!e.target.closest('.show-more')) return;
      var choice = pagination.querySelector('.show-more-count');
      state.limit += choice ? Number(choice.value) : pageSize;
      render();
    });

    function refreshSelectOptions() {
      columns.forEach(function (c) {
        if (c.filter !== 'select') return;
        var seen = {};
        state.rows.forEach(function (r) { seen[c.value(r)] = true; });
        var values = Object.keys(seen).sort(compare);
        var sig = values.join('\u0000');
        if (state.optionSigs[c.key] === sig) return;   // unchanged: don't disturb an open dropdown
        state.optionSigs[c.key] = sig;
        var sel = table.tHead.querySelector('select[data-filter="' + c.key + '"]');
        var current = sel.value || state.filters[c.key] || '';
        if (current && !seen[current]) values.push(current);
        sel.innerHTML = '<option value="">All</option>' + values.map(function (v) {
          return '<option value="' + esc(v) + '">' + esc(v || '(blank)') + '</option>';
        }).join('');
        sel.value = current;
      });
    }

    function visibleRows() {
      var active = columns.filter(function (c) { return state.filters[c.key]; });
      var rows = !active.length ? state.rows : state.rows.filter(function (r) {
        return active.every(function (c) {
          var f = state.filters[c.key];
          if (c.filter === 'select') return String(c.value(r)) === f;
          return String((c.search || c.value)(r)).toLowerCase().indexOf(f) !== -1;
        });
      });
      if (state.sortKey) {
        var c = byKey[state.sortKey];
        var sv = c.sortValue || function (r) { return String(c.value(r)); };
        var dir = state.sortDir === 'descending' ? -1 : 1;
        rows = rows.map(function (r) { return [sv(r), r]; }).sort(function (a, b) {
          var aNull = a[0] === null || a[0] === '', bNull = b[0] === null || b[0] === '';
          if (aNull || bNull) return compare(a[0], b[0]);    // blanks stay at the bottom either way
          return dir * compare(a[0], b[0]);
        }).map(function (x) { return x[1]; });
      }
      return rows;
    }

    function render() {
      refreshSelectOptions();
      table.tHead.querySelectorAll('th[data-key]').forEach(function (th) {
        var dir = th.dataset.key === state.sortKey ? state.sortDir : 'none';
        th.setAttribute('aria-sort', dir);
        th.querySelector('.sort-arrow').textContent = ARROWS[dir];
      });
      var rows = visibleRows();
      var shown = rows.slice(0, state.limit);
      var filtered = columns.some(function (c) { return state.filters[c.key]; });
      var empty = filtered ? 'No rows match these filters.'
                           : (typeof opts.emptyText === 'function' ? opts.emptyText() : (opts.emptyText || 'No rows yet.'));
      tbody.innerHTML = shown.map(function (r) {
        var rid = r.id != null ? ' data-row-id="' + esc(r.id) + '"' : '';
        return '<tr' + rid + '>' + columns.map(function (c) {
          var td = c.tdClass ? '<td class="' + c.tdClass + '">' : '<td>';
          return td + (c.cell ? c.cell(r) : esc(c.value(r))) + '</td>';
        }).join('') + '</tr>';
      }).join('') || '<tr><td colspan="' + columns.length + '" class="muted">' + esc(empty) + '</td></tr>';

      var previousChoice = pagination.querySelector('.show-more-count');
      var selectedCount = previousChoice ? previousChoice.value : String(moreChoices ? moreChoices[0] : pageSize);
      var foot = '';
      if (rows.length > shown.length) {
        var next = rows.length - shown.length;
        var controls = moreChoices
          ? ' <span class="show-more-controls"><button type="button" class="button secondary show-more">Show more</button>' +
            '<label>Rows to add <select class="show-more-count" aria-label="Rows to add">' +
            moreChoices.map(function (count) {
              return '<option value="' + count + '"' + (String(count) === selectedCount ? ' selected' : '') + '>' + count.toLocaleString() + '</option>';
            }).join('') + '</select></label></span>'
          : ' <button type="button" class="button secondary show-more">Show ' + Math.min(pageSize, next) + ' more</button>';
        foot = 'Showing ' + shown.length + ' of ' + rows.length + (filtered ? ' matching' : '') + controls;
      } else if (moreChoices && rows.length) {
        foot = 'Showing ' + shown.length + ' of ' + rows.length + (filtered ? ' matching' : '');
      } else if (filtered && rows.length) {
        foot = rows.length + ' of ' + state.rows.length + ' rows match';
      }
      if (opts.paginationTarget) {
        pagination.innerHTML = foot;
        tfoot.innerHTML = '';
      } else {
        tfoot.innerHTML = foot ? '<tr><td colspan="' + columns.length + '" class="tiny">' + foot + '</td></tr>' : '';
      }
      if (opts.onRender) opts.onRender(shown, rows.length);
    }

    function syncStickyOffset() {
      var h = table.tHead.rows[0].getBoundingClientRect().height;
      if (h) table.style.setProperty('--filter-row-top', Math.round(h) + 'px');
      // scroll box keeps keyboard-focused rows clear of the sticky header
      var all = table.tHead.getBoundingClientRect().height;
      if (all && table.parentElement) table.parentElement.style.setProperty('--thead-h', Math.round(all) + 'px');
    }
    if (global.ResizeObserver) new ResizeObserver(syncStickyOffset).observe(table.tHead);

    return {
      setRows: function (rows) { state.rows = rows || []; render(); },
      refresh: render,
      getRows: function () { return state.rows; },
      getMatchingRows: visibleRows,      // every row passing the filters, in sorted order
      render: render
    };
  }

  global.ListingTable = { create: create, escapeHtml: esc };
})(window);
