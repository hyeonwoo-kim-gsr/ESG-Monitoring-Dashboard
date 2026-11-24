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
    usingFallback: false,
    searchMode: "basic",      // basic | detail
    lastMonthly: null,        // { rows, label }  엑셀 추출용
    lastSearch: null          // { rows, label }
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

  function fmtYMD(d) { return d.getUTCFullYear() + "-" + pad2(d.getUTCMonth() + 1) + "-" + pad2(d.getUTCDate()); }
  function fmtMD(d) { return pad2(d.getUTCMonth() + 1) + "/" + pad2(d.getUTCDate()); }

  /* 주어진 월(YYYY-MM)이 걸쳐 있는 월~일 기준 주차 목록을 계산. */
  function getMonthWeeks(monthKey) {
    var p = monthKey.split("-").map(Number);
    var year = p[0], month = p[1];
    var firstDay = new Date(Date.UTC(year, month - 1, 1));
    var lastDay  = new Date(Date.UTC(year, month, 0));
    var firstWeekday = firstDay.getUTCDay();
    var daysSinceMonday = (firstWeekday + 6) % 7;
    var cursor = new Date(firstDay);
    cursor.setUTCDate(cursor.getUTCDate() - daysSinceMonday);

    var weeks = [];
    var idx = 1;
    while (cursor.getTime() <= lastDay.getTime()) {
      var weekStart = new Date(cursor);
      var weekEnd = new Date(cursor);
      weekEnd.setUTCDate(weekEnd.getUTCDate() + 6);
      weeks.push({
        label: idx + "주차 (" + fmtMD(weekStart) + " ~ " + fmtMD(weekEnd) + ")",
        start: fmtYMD(weekStart),
        end: fmtYMD(weekEnd)
      });
      cursor.setUTCDate(cursor.getUTCDate() + 7);
      idx++;
    }
    return weeks;
  }

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

  function loadMonths(keys) {
    return Promise.all(keys.map(loadMonth)).then(function (arrays) {
      return [].concat.apply([], arrays);
    });
  }

  function loadAllMonths() {
    return loadMonths((state.manifest && state.manifest.months) || []);
  }

  function latestMonthKey() {
    var months = (state.manifest && state.manifest.months) || [];
    if (months.length === 0) return null;
    return months.slice().sort()[months.length - 1];
  }

  // ---------------- 렌더링 도우미 ----------------

  function sentimentLabel(s) { return s === "POSITIVE" ? "긍정" : (s === "NEGATIVE" ? "부정" : "보류"); }
  function sentimentClass(s) { return s === "POSITIVE" ? "positive" : (s === "NEGATIVE" ? "negative" : "uncertain"); }

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

  // ---------------- 엑셀 추출 ----------------

  var EXCEL_HEADERS = ["기업명", "날짜", "긍정/부정 여부", "기사 제목", "기사 요약내용", "기사 URL"];

  function safeFileName(s) {
    return String(s || "").replace(/[\\/:*?"<>|\s]+/g, "_");
  }

  function todayCompact() {
    var d = new Date();
    return d.getFullYear() + pad2(d.getMonth() + 1) + pad2(d.getDate());
  }

  function isHttpUrl(u) { return /^https?:\/\//i.test(u || ""); }

  /* rows(기사 배열) -> 엑셀 행 배열. 요약은 화면과 달리 잘라내지 않고 전체를 담는다. */
  function buildExcelRows(rows) {
    var sorted = rows.slice().sort(function (a, b) {
      if (a.date !== b.date) return a.date < b.date ? 1 : -1;
      return String(a.company).localeCompare(String(b.company));
    });
    return sorted.map(function (a) {
      return [a.company || "", a.date || "", sentimentLabel(a.sentiment), a.title || "", a.description || "", a.link || ""];
    });
  }

  function buildWorkbook(rows) {
    var XLSX = window.XLSX;
    var aoa = [EXCEL_HEADERS].concat(buildExcelRows(rows));
    var ws = XLSX.utils.aoa_to_sheet(aoa);

    // URL 열(F)을 클릭 가능한 하이퍼링크로 설정
    for (var r = 1; r < aoa.length; r++) {
      var url = aoa[r][5];
      if (!isHttpUrl(url)) continue;
      var addr = XLSX.utils.encode_cell({ r: r, c: 5 });
      if (ws[addr]) ws[addr].l = { Target: url, Tooltip: "기사 원문 열기" };
    }

    ws["!cols"] = [{ wch: 14 }, { wch: 12 }, { wch: 14 }, { wch: 55 }, { wch: 80 }, { wch: 50 }];
    ws["!autofilter"] = { ref: "A1:F" + aoa.length };

    var wb = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(wb, ws, "ESG 기사");
    return wb;
  }

  function exportToExcel(rows, fileBase) {
    if (!rows || rows.length === 0) {
      alert("추출할 기사가 없습니다. 먼저 조회/검색을 실행해 주세요.");
      return false;
    }
    if (!window.XLSX) {
      alert("엑셀 라이브러리를 불러오지 못했습니다. 인터넷 연결(또는 사내망의 cdnjs 접근 차단 여부)을 확인한 뒤 새로고침해 주세요.");
      return false;
    }
    var wb = buildWorkbook(rows);
    window.XLSX.writeFile(wb, safeFileName(fileBase) + ".xlsx");
    return true;
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
      var filtered = articles.filter(function (a) {
        return a.company === company && (a.sentiment === "POSITIVE" || a.sentiment === "NEGATIVE");
      });
      state.lastMonthly = { rows: filtered, label: "ESG_기업별월별_" + company + "_" + monthKey };
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

  function populateWeeklyMonths() {
    var months = ((state.manifest && state.manifest.months) || []).slice().sort().reverse();
    var sel = $("weeklyMonth");
    if (months.length === 0) {
      sel.innerHTML = '<option value="">데이터 없음</option>';
      $("weeklyWeek").innerHTML = '<option value="">데이터 없음</option>';
      return;
    }
    sel.innerHTML = months.map(function (m) {
      var label = m.slice(0, 4) + "년 " + m.slice(5, 7) + "월";
      return '<option value="' + m + '">' + label + "</option>";
    }).join("");
  }

  function populateWeeklyWeeks(monthKey, preferredDate) {
    if (!monthKey) return [];
    var weeks = getMonthWeeks(monthKey);
    var sel = $("weeklyWeek");
    sel.innerHTML = weeks.map(function (w, i) {
      return '<option value="' + i + '">' + w.label + "</option>";
    }).join("");
    sel.dataset.weeksJson = JSON.stringify(weeks);

    var defaultIdx = weeks.length - 1;
    if (preferredDate) {
      for (var i = 0; i < weeks.length; i++) {
        if (preferredDate >= weeks[i].start && preferredDate <= weeks[i].end) { defaultIdx = i; break; }
      }
    }
    sel.value = defaultIdx;
    return weeks;
  }

  function runWeeklyFromSelectors() {
    var monthKey = $("weeklyMonth").value;
    if (!monthKey) return;
    var weeks = [];
    try { weeks = JSON.parse($("weeklyWeek").dataset.weeksJson || "[]"); } catch (e) { weeks = []; }
    var idx = parseInt($("weeklyWeek").value, 10);
    var w = weeks[idx];
    if (!w) return;
    renderWeekly(w.start, w.end);
  }

  // ---------------- 4. 키워드 검색 (기본 / 상세) ----------------

  function includesKw(a, kw) {
    return (a.title && a.title.toLowerCase().indexOf(kw) !== -1) ||
           (a.description && a.description.toLowerCase().indexOf(kw) !== -1);
  }

  /* 상세검색 조건에 맞는 기사만 남긴다.
     c = { company, kw1, kw2, op(AND|OR|NOT), start, end, sentiment } */
  function matchesDetail(a, c) {
    if (c.company && a.company !== c.company) return false;
    if (c.sentiment && a.sentiment !== c.sentiment) return false;
    if (c.start && a.date < c.start) return false;
    if (c.end && a.date > c.end) return false;

    var k1 = (c.kw1 || "").trim().toLowerCase();
    var k2 = (c.kw2 || "").trim().toLowerCase();
    var m1 = k1 ? includesKw(a, k1) : null;
    var m2 = k2 ? includesKw(a, k2) : null;

    if (m1 !== null && m2 !== null) {
      if (c.op === "OR") return m1 || m2;
      if (c.op === "NOT") return m1 && !m2;
      return m1 && m2;
    }
    if (m1 !== null) return m1;
    if (m2 !== null) return c.op === "NOT" ? !m2 : m2;
    return true;
  }

  function matchesBasic(a, kw) {
    return (a.title && a.title.toLowerCase().indexOf(kw) !== -1) ||
           (a.description && a.description.toLowerCase().indexOf(kw) !== -1) ||
           (a.company && a.company.toLowerCase().indexOf(kw) !== -1);
  }

  function showSearchResults(matched, label, summary) {
    var el = $("searchResult");
    matched.sort(function (a, b) { return a.date < b.date ? 1 : -1; });
    state.lastSearch = { rows: matched, label: label };
    if (matched.length === 0) {
      el.innerHTML = emptyState(summary + " 검색 결과가 없습니다.");
      return;
    }
    // 개수 제한 없음: 검색된 기사를 모두 표시
    el.innerHTML = '<p class="hint" style="margin-top:0;">총 ' + matched.length + "건 검색됨</p>" + matched.map(articleCardHtml).join("");
  }

  function renderSearch(keyword) {
    var el = $("searchResult");
    if (!keyword || !keyword.trim()) {
      el.innerHTML = "";
      state.lastSearch = null;
      return;
    }
    el.innerHTML = emptyState("검색 중…");
    loadAllMonths().then(function (all) {
      var kw = keyword.trim().toLowerCase();
      var matched = all.filter(function (a) { return matchesBasic(a, kw); });
      showSearchResults(matched, "ESG_키워드검색_" + keyword.trim() + "_" + todayCompact(), '"' + keyword + '"에 대한');
    });
  }

  function readDetailConditions() {
    return {
      company: $("detailCompany").value,
      kw1: $("detailKw1").value,
      op: $("detailOp").value,
      kw2: $("detailKw2").value,
      sentiment: $("detailSentiment").value,
      start: $("detailStart").value,
      end: $("detailEnd").value
    };
  }

  function renderDetailSearch() {
    var c = readDetailConditions();
    var el = $("searchResult");

    if (c.start && c.end && c.start > c.end) {
      el.innerHTML = emptyState("기간의 시작일이 종료일보다 늦습니다. 기간을 다시 선택해 주세요.");
      state.lastSearch = null;
      return;
    }
    if (!c.company && !c.kw1.trim() && !c.kw2.trim() && !c.sentiment && !c.start && !c.end) {
      el.innerHTML = emptyState("조건을 하나 이상 입력해 주세요.");
      state.lastSearch = null;
      return;
    }

    el.innerHTML = emptyState("검색 중…");
    var manifestMonths = ((state.manifest && state.manifest.months) || []).slice().sort();
    var keys = manifestMonths.filter(function (m) {
      if (c.start && m < c.start.slice(0, 7)) return false;
      if (c.end && m > c.end.slice(0, 7)) return false;
      return true;
    });

    loadMonths(keys).then(function (all) {
      var matched = all.filter(function (a) { return matchesDetail(a, c); });
      var parts = [c.company, c.kw1.trim(), c.kw2.trim()].filter(Boolean);
      var label = "ESG_상세검색_" + (parts.join("_") || "전체") + "_" + todayCompact();
      showSearchResults(matched, label, "입력하신 조건에 대한");
    });
  }

  function setSearchMode(mode) {
    state.searchMode = mode;
    $("searchBasicBox").hidden = mode !== "basic";
    $("searchDetailBox").hidden = mode !== "detail";
    $("modeBasic").classList.toggle("is-active", mode === "basic");
    $("modeDetail").classList.toggle("is-active", mode === "detail");
    $("searchResult").innerHTML = "";
    state.lastSearch = null;
  }

  function populateDetailCompanies() {
    $("detailCompany").innerHTML = '<option value="">전체 기업</option>' + COMPANIES.map(function (c) {
      return '<option value="' + escapeHtml(c) + '">' + escapeHtml(c) + "</option>";
    }).join("");
  }

  function resetDetail() {
    ["detailKw1", "detailKw2", "detailStart", "detailEnd"].forEach(function (id) { $(id).value = ""; });
    $("detailCompany").value = "";
    $("detailOp").value = "AND";
    $("detailSentiment").value = "";
    $("searchResult").innerHTML = "";
    state.lastSearch = null;
  }

  function setupSearch() {
    populateDetailCompanies();
    $("modeBasic").addEventListener("click", function () { setSearchMode("basic"); });
    $("modeDetail").addEventListener("click", function () { setSearchMode("detail"); });

    $("searchRun").addEventListener("click", function () { renderSearch($("searchInput").value); });
    $("searchInput").addEventListener("keydown", function (e) {
      if (e.key === "Enter") renderSearch(this.value);
    });

    $("detailRun").addEventListener("click", renderDetailSearch);
    $("detailReset").addEventListener("click", resetDetail);
    ["detailKw1", "detailKw2"].forEach(function (id) {
      $(id).addEventListener("keydown", function (e) { if (e.key === "Enter") renderDetailSearch(); });
    });

    $("searchExport").addEventListener("click", function () {
      if (!state.lastSearch) { alert("먼저 검색을 실행해 주세요."); return; }
      exportToExcel(state.lastSearch.rows, state.lastSearch.label);
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
    return loadManifest().then(function () {
      $("fallbackBanner").hidden = !state.usingFallback;

      if (state.manifest.last_updated) {
        var d = new Date(state.manifest.last_updated);
        $("lastUpdated").textContent = "마지막 수집: " + d.toLocaleString("ko-KR", { dateStyle: "medium", timeStyle: "short" });
      } else {
        $("lastUpdated").textContent = "아직 수집된 데이터가 없습니다.";
      }

      setupNav();
      setupSearch();
      populateMonthlySelectors();
      populateWeeklyMonths();

      $("monthlyExport").addEventListener("click", function () {
        if (!state.lastMonthly) { alert("먼저 기업과 월을 선택해 주세요."); return; }
        exportToExcel(state.lastMonthly.rows, state.lastMonthly.label);
      });

      var latest = latestMonthKey();
      if (!latest) {
        ["dailyResult", "weeklyResult", "monthlyResult"].forEach(function (id) {
          $(id).innerHTML = emptyState("아직 수집된 데이터가 없습니다. 수집 스크립트가 처음 실행되면 표시됩니다.");
        });
        return;
      }

      return loadMonth(latest).then(function (articles) {
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

        $("weeklyMonth").value = latest;
        populateWeeklyWeeks(latest, latestDate);
        runWeeklyFromSelectors();
        $("weeklyMonth").addEventListener("change", function () {
          populateWeeklyWeeks(this.value);
          runWeeklyFromSelectors();
        });
        $("weeklyWeek").addEventListener("change", runWeeklyFromSelectors);

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
    });
  }

  // 테스트용 노출 (화면 동작에는 영향 없음)
  window.__esgDash = { matchesDetail: matchesDetail, buildExcelRows: buildExcelRows, buildWorkbook: buildWorkbook, state: state, init: init };

  document.addEventListener("DOMContentLoaded", init);
})();
