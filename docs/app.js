(function () {
  "use strict";

  var DATA_DIR = "./data";

  var COMPANIES = [
    "GS리테일", "BGF", "세븐일레븐", "이마트24", "이마트", "이마트에브리데이",
    "롯데슈퍼", "롯데마트", "롯데홈쇼핑", "현대홈쇼핑", "CJ온스타일", "롯데백화점",
    "현대백화점", "신세계백화점"
  ];

  var state = {
    manifest: null,
    monthCache: {},
    usingFallback: false
  };

  function $(id) { return document.getElementById(id); }

  function escapeHtml(str) {
    return String(str || "").replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function truncate(text, n) {
    if (!text) return "";
    return text.length > n ? text.slice(0, n) + "…" : text;
  }

  function pad2(n) { return n < 10 ? "0" + n : "" + n; }

  function shiftDate(dateStr, days) {
    var p = dateStr.split("-").map(Number);
    var d = new Date(Date.UTC(p[0], p[1] - 1, p[2]));
    d.setUTCDate(d.getUTCDate() + days);
    return d.getUTCFullYear() + "-" + pad2(d.getUTCMonth() + 1) + "-" + pad2(d.getUTCDate());
  }

  function monthRangeKeys(startStr, endStr) {
    var s = startStr.split("-").map(Number);
    var e = endStr.split("-").map(Number);
    var d = new Date(Date.UTC(s[0], s[1] - 1, 1));
    var end = new Date(Date.UTC(e[0], e[1] - 1, 1));
    var keys = [];
    while (d.getTime() <= end.getTime()) {
      keys.push(d.getUTCFullYear() + "-" + pad2(d.getUTCMonth() + 1));
      d.setUTCMonth(d.getUTCMonth() + 1);
    }
    return keys;
  }

  // ---------------- 데이터 로딩 ----------------

  function loadManifest() {
    return fetch(DATA_DIR + "/manifest.json", { cache: "no-store" })
      .then(function (res) {
        if (!res.ok) throw new Error("manifest http " + res.status);
        return res.json();
      })
      .then(function (data) {
        state.manifest = data;
        state.usingFallback = false;
        return data;
      })
      .catch(function (err) {
        console.warn("[대시보드] 실 데이터 manifest 로드 실패, 샘플 데이터로 표시합니다:", err);
        state.usingFallback = true;
        state.manifest = (window.__SAMPLE_DASHBOARD_DATA && window.__SAMPLE_DASHBOARD_DATA.manifest) || { months: [], last_updated: null };
        return state.manifest;
      });
  }

  function loadMonth(monthKey) {
    if (!monthKey) return Promise.resolve([]);
    if (state.monthCache[monthKey]) return Promise.resolve(state.monthCache[monthKey]);

    if (state.usingFallback) {
      var arr = (window.__SAMPLE_DASHBOARD_DATA && window.__SAMPLE_DASHBOARD_DATA.months[monthKey]) || [];
      state.monthCache[monthKey] = arr;
      return Promise.resolve(arr);
    }

    return fetch(DATA_DIR + "/" + monthKey + ".json", { cache: "no-store" })
      .then(function (res) {
        if (!res.ok) throw new Error("month http " + res.status);
        return res.json();
      })
      .then(function (data) {
        state.monthCache[monthKey] = data;
        return data;
      })
      .catch(function (err) {
        console.warn("[대시보드] " + monthKey + " 데이터 로드 실패:", err);
        state.monthCache[monthKey] = [];
        return [];
      });
  }

  function loadAllMonths() {
    var months = (state.manifest && state.manifest.months) || [];
    return Promise.all(months.map(loadMonth)).then(function (arrays) {
      return [].concat.apply([], arrays);
    });
  }

  function latestMonthKey() {
    var months = (state.manifest && state.manifest.months) || [];
    if (months.length === 0) return null;
    return months.slice().sort()[months.length - 1];
  }

  // ---------------- 렌더링 도우미 ----------------

  function sentimentLabel(s) { return s === "POSITIVE" ? "긍정" : "부정"; }
  function sentimentClass(s) { return s === "POSITIVE" ? "positive" : "negative"; }

  function articleCardHtml(article) {
    var cls = sentimentClass(article.sentiment);
    var pill = article.related_count > 1
      ? '<span class="related-pill">동일 주제 보도 ' + article.related_count + '건</span>'
      : "";
    return (
      '<div class="article-card ' + cls + '">' +
        '<div class="card-top">' +
          '<a class="card-title" href="' + escapeHtml(article.link) + '" target="_blank" rel="noopener">' + escapeHtml(article.title) + "</a>" +
          '<span class="card-date">' + escapeHtml(article.date) + "</span>" +
        "</div>" +
        (article.description ? '<p class="card-desc">' + escapeHtml(truncate(article.description, 140)) + "</p>" : "") +
        '<div class="card-foot">' +
          '<span class="card-company-tag">' + escapeHtml(article.company) + " · " + sentimentLabel(article.sentiment) + "</span>" +
          pill +
        "</div>" +
      "</div>"
    );
  }

  function emptyState(message) {
    return '<div class="empty-state">' + escapeHtml(message) + "</div>";
  }

  function renderSentimentSections(articles, opts) {
    opts = opts || {};
    var groupByCompany = opts.groupByCompany !== false;
    var html = "";

    ["POSITIVE", "NEGATIVE"].forEach(function (senti) {
      var list = articles.filter(function (a) { return a.sentiment === senti; });
      html += '<div class="section-block">';
      html += '<div class="section-heading ' + sentimentClass(senti) + '">' +
        (senti === "POSITIVE" ? "긍정 동향" : "부정 동향") +
        ' <span class="count-tag">' + list.length + "건</span></div>";

      if (list.length === 0) {
        html += emptyState("해당 조건의 " + sentimentLabel(senti) + " 동향 뉴스가 없습니다.");
      } else if (groupByCompany) {
        var byCompany = {};
        list.forEach(function (a) { (byCompany[a.company] = byCompany[a.company] || []).push(a); });
        COMPANIES.forEach(function (company) {
          var arr = byCompany[company];
          if (!arr || arr.length === 0) return;
          arr.sort(function (a, b) { return a.date < b.date ? 1 : -1; });
          html += '<div class="company-group">';
          html += '<div class="company-group-name">' + escapeHtml(company) + " (" + arr.length + "건)</div>";
          arr.forEach(function (a) { html += articleCardHtml(a); });
          html += "</div>";
        });
      } else {
        list.sort(function (a, b) { return a.date < b.date ? 1 : -1; });
        list.forEach(function (a) { html += articleCardHtml(a); });
      }
      html += "</div>";
    });
    return html;
  }

  // ---------------- 1. 일자별 리포트 ----------------

  function renderDaily(dateStr) {
    var monthKey = dateStr.slice(0, 7);
    loadMonth(monthKey).then(function (articles) {
      var dayArticles = articles.filter(function (a) { return a.date === dateStr; });
      $("dailyResult").innerHTML = renderSentimentSections(dayArticles, { groupByCompany: true });
    });
  }

  // ---------------- 2. 주간 보도 랭킹 ----------------

  function renderWeekly(startStr, endStr) {
    if (!startStr || !endStr) return;
    var months = monthRangeKeys(startStr, endStr);
    Promise.all(months.map(loadMonth)).then(function (arrays) {
      var all = [].concat.apply([], arrays);
      var ranked = all.filter(function (a) { return a.date >= startStr && a.date <= endStr; });
      ranked.sort(function (a, b) {
        if (b.related_count !== a.related_count) return b.related_count - a.related_count;
        return a.date < b.date ? 1 : -1;
      });
      ranked = ranked.slice(0, 15);

      var el = $("weeklyResult");
      if (ranked.length === 0) {
        el.innerHTML = emptyState("해당 기간에 수집된 ESG 동향 뉴스가 없습니다.");
        return;
      }
      el.innerHTML = ranked.map(function (a, idx) {
        return '<div class="rank-row"><div class="rank-badge">' + (idx + 1) + "</div>" + articleCardHtml(a) + "</div>";
      }).join("");
    });
  }

  // ---------------- 3. 기업별 월별 모음 ----------------

  function renderMonthly(company, monthKey) {
    loadMonth(monthKey).then(function (articles) {
      var filtered = articles.filter(function (a) { return a.company === company; });
      $("monthlyResult").innerHTML = renderSentimentSections(filtered, { groupByCompany: false });
    });
  }

  function populateMonthlySelectors() {
    var companySel = $("monthlyCompany");
    companySel.innerHTML = COMPANIES.map(function (c) {
      return '<option value="' + escapeHtml(c) + '">' + escapeHtml(c) + "</option>";
    }).join("");

    var months = ((state.manifest && state.manifest.months) || []).slice().sort().reverse();
    var monthSel = $("monthlyMonth");
    if (months.length === 0) {
      monthSel.innerHTML = '<option value="">데이터 없음</option>';
      return;
    }
    monthSel.innerHTML = months.map(function (m) {
      var label = m.slice(0, 4) + "년 " + m.slice(5, 7) + "월";
      return '<option value="' + m + '">' + label + "</option>";
    }).join("");
  }

  // ---------------- 4. 키워드 검색 ----------------

  function renderSearch(keyword) {
    var el = $("searchResult");
    if (!keyword || !keyword.trim()) {
      el.innerHTML = "";
      return;
    }
    el.innerHTML = emptyState("검색 중…");
    loadAllMonths().then(function (all) {
      var kw = keyword.trim().toLowerCase();
      var matched = all.filter(function (a) {
        return (a.title && a.title.toLowerCase().indexOf(kw) !== -1) ||
               (a.description && a.description.toLowerCase().indexOf(kw) !== -1) ||
               (a.company && a.company.toLowerCase().indexOf(kw) !== -1);
      });
      matched.sort(function (a, b) { return a.date < b.date ? 1 : -1; });
      matched = matched.slice(0, 60);

      if (matched.length === 0) {
        el.innerHTML = emptyState('"' + keyword + '"에 대한 검색 결과가 없습니다.');
        return;
      }
      el.innerHTML = '<p class="hint" style="margin-top:0;">총 ' + matched.length + "건 검색됨</p>" + matched.map(articleCardHtml).join("");
    });
  }

  // ---------------- 사이드바 탭 전환 ----------------

  function setupNav() {
    var items = Array.prototype.slice.call(document.querySelectorAll(".nav-item"));
    items.forEach(function (btn) {
      btn.addEventListener("click", function () {
        items.forEach(function (b) { b.classList.remove("is-active"); });
        btn.classList.add("is-active");
        Array.prototype.slice.call(document.querySelectorAll(".panel")).forEach(function (p) { p.classList.remove("is-active"); });
        $("panel-" + btn.dataset.tab).classList.add("is-active");
        $("panelTitle").textContent = btn.dataset.title;
      });
    });
  }

  // ---------------- 초기화 ----------------

  function init() {
    loadManifest().then(function () {
      $("fallbackBanner").hidden = !state.usingFallback;

      if (state.manifest.last_updated) {
        var d = new Date(state.manifest.last_updated);
        $("lastUpdated").textContent = "마지막 수집: " + d.toLocaleString("ko-KR", { dateStyle: "medium", timeStyle: "short" });
      } else {
        $("lastUpdated").textContent = "아직 수집된 데이터가 없습니다.";
      }

      setupNav();
      populateMonthlySelectors();

      var latest = latestMonthKey();
      if (!latest) {
        ["dailyResult", "weeklyResult", "monthlyResult"].forEach(function (id) {
          $(id).innerHTML = emptyState("아직 수집된 데이터가 없습니다. 수집 스크립트가 처음 실행되면 표시됩니다.");
        });
        $("searchRun").addEventListener("click", function () { renderSearch($("searchInput").value); });
        return;
      }

      loadMonth(latest).then(function (articles) {
        var dates = articles.map(function (a) { return a.date; }).sort();
        var latestDate = dates.length ? dates[dates.length - 1] : shiftDate(latest + "-01", 0);

        $("dailyDate").value = latestDate;
        renderDaily(latestDate);
        $("dailyDate").addEventListener("change", function () { if (this.value) renderDaily(this.value); });
        $("dailyPrev").addEventListener("click", function () {
          $("dailyDate").value = shiftDate($("dailyDate").value, -1);
          renderDaily($("dailyDate").value);
        });
        $("dailyNext").addEventListener("click", function () {
          $("dailyDate").value = shiftDate($("dailyDate").value, 1);
          renderDaily($("dailyDate").value);
        });

        var weekStart = shiftDate(latestDate, -6);
        $("weeklyStart").value = weekStart;
        $("weeklyEnd").value = latestDate;
        renderWeekly(weekStart, latestDate);
        $("weeklyRun").addEventListener("click", function () {
          renderWeekly($("weeklyStart").value, $("weeklyEnd").value);
        });

        $("monthlyMonth").value = latest;
        $("monthlyCompany").value = COMPANIES[0];
        renderMonthly($("monthlyCompany").value, latest);
        $("monthlyCompany").addEventListener("change", function () {
          renderMonthly(this.value, $("monthlyMonth").value);
        });
        $("monthlyMonth").addEventListener("change", function () {
          renderMonthly($("monthlyCompany").value, this.value);
        });
      });

      $("searchRun").addEventListener("click", function () { renderSearch($("searchInput").value); });
      $("searchInput").addEventListener("keydown", function (e) {
        if (e.key === "Enter") renderSearch(this.value);
      });
    });
  }

  document.addEventListener("DOMContentLoaded", init);
})();
