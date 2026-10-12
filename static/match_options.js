/* match_options.js - New scan page: fuzzy toggle, strictness slider and the
   live plain-English preview ("febreze: up to 1 letter can differ").

   The rules live in fuzzy_match.py; this file only asks /api/match-preview,
   so the preview can never drift from what the scan actually does. */
(function () {
  'use strict';

  var PREF = 'prescreener_';
  function $(id) { return document.getElementById(id); }
  function esc(v) {
    return String(v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function letters(n) { return n + ' letter' + (n === 1 ? '' : 's'); }

  function describeWord(w, fuzzyOn) {
    if (!fuzzyOn) return 'exact or disguised only';
    if (w.short) return 'exact or disguised only (too short for typos)';
    if (w.allowed === 0) return 'exact or disguised only (no typos at this strictness)';
    return 'exact, disguised, or up to <strong>' + letters(w.allowed) + '</strong> different';
  }

  var timer = null, seq = 0;
  function refresh() {
    clearTimeout(timer);
    timer = setTimeout(load, 200);
  }

  function load() {
    var fuzzyOn = $('fuzzy').checked;
    var strictness = $('fuzzy_strictness').value;
    $('fuzzy-strictness-value').textContent = strictness;
    $('fuzzy-controls').hidden = !fuzzyOn;
    var keywords = $('keywords').value;
    if (!keywords.trim()) {
      $('match-preview').innerHTML = '<span class="tiny">Type a keyword above to see exactly how it will be matched.</span>';
      return;
    }
    var mine = ++seq;
    var qs = new URLSearchParams({ keywords: keywords, fuzzy: fuzzyOn, strictness: strictness });
    fetch('/api/match-preview?' + qs).then(function (r) { return r.json(); }).then(function (data) {
      if (mine !== seq) return;               // a newer keystroke already won
      var rows = data.keywords.map(function (k) {
        if (k.words.length === 1) {
          return '<li><code>' + esc(k.words[0].word) + '</code>: ' + describeWord(k.words[0], fuzzyOn) + '</li>';
        }
        // multi-word keyword: still ONE keyword (one search); every word must be in the title, any order
        return '<li><code>' + esc(k.keyword) + '</code>: one keyword, every word must be in the title (any order)<ul>' +
          k.words.map(function (w) {
            return '<li><code>' + esc(w.word) + '</code>: ' + describeWord(w, fuzzyOn) + '</li>';
          }).join('') + '</ul></li>';
      }).join('');
      var html = '<strong>What will match:</strong><ul>' + rows + '</ul>';
      if ($('disguised_search').checked) {
        var extra = Object.keys(data.spellings).map(function (kw) {
          var list = data.spellings[kw];
          return '<li><code>' + esc(kw) + '</code> also searched as: ' +
                 (list.length ? list.map(function (s) { return '<code>' + esc(s) + '</code>'; }).join(', ') : 'none (too short)') + '</li>';
        }).join('');
        html += '<strong>Extra Walmart searches:</strong><ul>' + extra + '</ul>';
      }
      $('match-preview').innerHTML = html;
    }).catch(function () { /* preview is a nicety; the scan itself still works */ });
  }

  function remember(el, key, prop) {
    var saved = localStorage.getItem(PREF + key);
    if (saved !== null) el[prop] = prop === 'checked' ? saved === 'true' : saved;
    el.addEventListener('input', function () { localStorage.setItem(PREF + key, el[prop]); refresh(); });
    el.addEventListener('change', function () { localStorage.setItem(PREF + key, el[prop]); refresh(); });
  }

  document.addEventListener('DOMContentLoaded', function () {
    remember($('fuzzy'), 'fuzzy', 'checked');
    remember($('fuzzy_strictness'), 'fuzzy_strictness', 'value');
    remember($('disguised_search'), 'disguised_search', 'checked');
    $('keywords').addEventListener('input', refresh);
    load();
  });
})();
