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

  function renderWeekly(startStr, endStr, topN) {
    if (!startStr || !endStr) return;
    var months = monthRangeKeys(startStr, endStr);
    Promise.all(months.map(loadMonth)).then(function (arrays) {
      var all = [].concat.apply([], arrays);
      var ranked = all.filter(function (a) { return a.date >= startStr && a.date <= endStr; });
      ranked.sort(function (a, b) {
        if (b.related_count !== a.related_count) return b.related_count - a.related_count;
        return a.date < b.date ? 1 : -1;
      });
      if (topN !== 0) ranked = ranked.slice(0, topN || 15);   // topN=0(월 전체)이면 자르지 않음

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

  // ---------------- 3. 기업별 월간 보도 모음 ----------------

  function renderMonthly(company, monthKey) {
    loadMonth(monthKey).then(function (articles) {
      var filtered = articles.filter(function (a) {
        return a.company === company && (a.sentiment === "POSITIVE" || a.sentiment === "NEGATIVE");
      });
      state.lastMonthly = { rows: filtered, label: "ESG_기업별월간보도_" + company + "_" + monthKey };
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
    sel.innerHTML = '<option value="all">전체 (월 전체)</option>' + weeks.map(function (w, i) {
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
    if ($("weeklyWeek").value === "all") {
      var p = monthKey.split("-").map(Number);
      var first = new Date(Date.UTC(p[0], p[1] - 1, 1));
      var last = new Date(Date.UTC(p[0], p[1], 0));
      renderWeekly(fmtYMD(first), fmtYMD(last), 0);   // 0 = 건수 제한 없이 월 전체 조회
      return;
    }
    var idx = parseInt($("weeklyWeek").value, 10);
    var w = weeks[idx];
    if (!w) return;
    renderWeekly(w.start, w.end, 15);
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

  // ---------------- 5. ESG 법률 모니터링 (의안 정보 / 법령 정보) ----------------

  var LEGAL_DIR = "./legal";
  var CAT_LABEL = { ENV_REG: "환경규제", DISC_MANDATE: "의무공시", SOC_CONTRIB: "사회공헌" };
  var STAGES = ["접수", "본회의 심의", "정부이송", "공포"];
  var legal = { bills: null, laws: null, meta: null, failed: {}, view: { bills: [], laws: [] } };

  function loadLegalFile(name) {
    return fetch(LEGAL_DIR + "/" + name + ".json", { cache: "no-store" })
      .then(function (res) {
        if (!res.ok) throw new Error("http " + res.status);
        return res.json();
      });
  }

  function loadLegal(kind) {
    if (legal[kind]) return Promise.resolve(legal[kind]);
    var metaP = legal.meta ? Promise.resolve(legal.meta) : loadLegalFile("meta").catch(function () { return null; });
    return Promise.all([loadLegalFile(kind), metaP]).then(function (r) {
      legal[kind] = Array.isArray(r[0]) ? r[0] : [];
      legal.meta = r[1];
      return legal[kind];
    }).catch(function (err) {
      console.warn("[대시보드] 법률 데이터 로드 실패:", kind, err);
      legal[kind] = [];
      legal.failed[kind] = true;
      return legal[kind];
    });
  }

  function dotDate(s) { return s ? String(s).replace(/-/g, ".") : "-"; }

  function monthStart(v) { return v ? v + "-01" : ""; }
  function monthEnd(v) { return v ? v + "-31" : ""; }

  /* 필터: 기간(YYYY-MM) / 유형 / 키워드(공백으로 나눈 단어를 모두 포함) */
  function legalMatches(rec, f, dateField, textFields) {
    var d = rec[dateField] || "";
    if (f.from && d < monthStart(f.from)) return false;
    if (f.to && d > monthEnd(f.to)) return false;
    if (f.cat && rec.category !== f.cat) return false;
    var terms = (f.kw || "").trim().toLowerCase().split(/\s+/).filter(Boolean);
    if (terms.length) {
      var hay = textFields.map(function (k) { return rec[k] || ""; }).join(" ").toLowerCase();
      for (var i = 0; i < terms.length; i++) { if (hay.indexOf(terms[i]) === -1) return false; }
    }
    return true;
  }

  var LEGAL_CFG = {
    bills: { dateField: "receipt_date", textFields: ["name", "committee", "proposer", "background", "summary", "stage", "plenary_result"] },
    laws:  { dateField: "promulgation_date", textFields: ["name", "kind", "ministry", "summary", "background", "revision_type"] }
  };

  /* 법령의 소관위원회: 수집 데이터에 committee 값이 있으면 그것을, 없으면 소관부처로 상임위를 추정 */
  var COMMITTEE_MINISTRY = {
    "법제사법위원회": ["법무부", "법제처", "감사원", "대법원", "헌법재판소", "검찰"],
    "정무위원회": ["공정거래위원회", "금융위원회", "금융감독원", "국가보훈부", "국민권익위원회", "개인정보보호위원회"],
    "기획재정위원회": ["기획재정부", "국세청", "관세청", "조달청", "통계청", "국가데이터처", "한국은행"],
    "교육위원회": ["교육부", "국가교육위원회"],
    "과학기술정보방송통신위원회": ["과학기술정보통신부", "방송통신위원회", "원자력안전위원회", "우주항공청", "과학기술부"],
    "행정안전위원회": ["행정안전부", "경찰청", "소방청", "인사혁신처", "중앙선거관리위원회", "재난안전"],
    "문화체육관광위원회": ["문화체육관광부", "국가유산청", "문화재청"],
    "농림축산식품해양수산위원회": ["농림축산식품부", "해양수산부", "농촌진흥청", "산림청", "해양경찰청"],
    "산업통상자원중소벤처기업위원회": ["산업통상자원부", "산업통상부", "중소벤처기업부", "특허청"],
    "보건복지위원회": ["보건복지부", "식품의약품안전처", "질병관리청"],
    "환경노동위원회": ["환경부", "기후에너지환경부", "고용노동부", "기상청", "기후위기"],
    "국토교통위원회": ["국토교통부", "행정중심복합도시건설청", "새만금개발청"],
    "정보위원회": ["국가정보원"],
    "여성가족위원회": ["여성가족부", "성평등가족부"]
  };

  function lawCommittees(rec) {
    var out = [];
    var direct = rec.committee || "";
    Object.keys(COMMITTEE_MINISTRY).forEach(function (c) {
      if (direct && direct.indexOf(c) !== -1) { out.push(c); return; }
      var m = rec.ministry || "";
      if (!m) return;
      var list = COMMITTEE_MINISTRY[c];
      for (var i = 0; i < list.length; i++) { if (m.indexOf(list[i]) !== -1) { out.push(c); return; } }
    });
    return out;
  }

  function readLegalFilter(kind) {
    var act = $(kind + "Active");
    var stg = $(kind + "Stage");
    var com = $(kind + "Committee");
    return { from: $(kind + "From").value, to: $(kind + "To").value, cat: $(kind + "Cat").value, kw: $(kind + "Kw").value,
             active: !!(act && act.checked), stage: stg ? stg.value : "", committee: com ? com.value : "" };
  }

  /* 진행 중 의안 = 종료(폐기·부결·철회)와 공포(완료)를 제외한 접수 · 본회의 심의 · 정부이송 단계 */
  function isActiveBill(b) { return b.stage !== "종료" && b.stage !== "공포"; }

  function chipHtml(text, cls) { return '<span class="chip ' + (cls || "") + '">' + escapeHtml(text) + "</span>"; }

  function safeHref(u) { return isHttpUrl(u) ? escapeHtml(u) : ""; }

  function linkButton(url, label) {
    var h = safeHref(url);
    return h ? '<a class="legal-link" href="' + h + '" target="_blank" rel="noopener">' + escapeHtml(label) + " ↗</a>" : "";
  }

  function sectionText(label, text) {
    if (!text) return "";
    return '<div class="legal-text"><span class="legal-text-label">' + escapeHtml(label) + "</span><p>" + escapeHtml(text) + "</p></div>";
  }

  function stageTrackerHtml(b) {
    if (b.stage === "종료") {   // 폐기·부결·철회: 접수 -> 종료 2단계로 표시
      var why = b.closed_reason || b.plenary_result || "";
      var cd = b.closed_date || b.plenary_date;
      return '<ol class="stage-track">' +
        '<li class="reached"><b>접수</b><span>' + escapeHtml(b.receipt_date ? dotDate(b.receipt_date) : "-") + "</span></li>" +
        '<li class="reached current"><b>종료</b><span>' + escapeHtml(cd ? dotDate(cd) : "일자 미확인") + "</span>" +
        (why ? '<em class="ng">' + escapeHtml(why) + "</em>" : "") + "</li></ol>";
    }
    var cur = Math.max(0, STAGES.indexOf(b.stage));
    var dates = [b.receipt_date, b.plenary_date, b.transfer_date, b.promulgation_date];
    return '<ol class="stage-track">' + STAGES.map(function (name, i) {
      var reached = i <= cur;
      var sub = "";
      if (i === 1 && b.plenary_result) sub = '<em class="' + (/가결/.test(b.plenary_result) ? "ok" : "ng") + '">' + escapeHtml(b.plenary_result) + "</em>";
      return '<li class="' + (reached ? "reached" : "") + (i === cur ? " current" : "") + '">' +
        '<b>' + escapeHtml(name) + "</b><span>" + escapeHtml(dates[i] ? dotDate(dates[i]) : (reached ? "일자 미확인" : "-")) + "</span>" + sub + "</li>";
    }).join("") + "</ol>";
  }

  function billItemHtml(b) {
    return '<li class="thread-item">' +
      '<div class="thread-head"><span class="thread-date">' + escapeHtml(dotDate(b.receipt_date)) + " 접수</span>" +
        chipHtml(CAT_LABEL[b.category] || b.category, "cat-" + b.category) + chipHtml(b.stage, "stage-chip") + "</div>" +
      '<div class="legal-card">' +
        '<div class="legal-title">' + escapeHtml(b.name) + "</div>" +
        '<div class="legal-meta">소관위원회 <b>' + escapeHtml(b.committee || "-") + "</b>" +
          (b.bill_no ? " · 의안번호 " + escapeHtml(b.bill_no) : "") + (b.proposer ? " · " + escapeHtml(b.proposer) : "") + "</div>" +
        stageTrackerHtml(b) +
        sectionText("제·개정 배경", b.background) + sectionText("주요 내용", b.summary) +
        '<div class="legal-foot">' + linkButton(b.link, "의안 본문 보기") + "</div>" +
      "</div></li>";
  }

  function lawItemHtml(l) {
    return '<li class="thread-item">' +
      '<div class="thread-head"><span class="thread-date">' + escapeHtml(dotDate(l.promulgation_date)) + " 공포</span>" +
        chipHtml(CAT_LABEL[l.category] || l.category, "cat-" + l.category) + chipHtml(l.kind || "법령", "stage-chip") +
        (l.revision_type ? chipHtml(l.revision_type, "") : "") + "</div>" +
      '<div class="legal-card">' +
        '<div class="legal-title">' + escapeHtml(l.name) + "</div>" +
        '<dl class="legal-facts">' +
          "<div><dt>공포일자</dt><dd>" + escapeHtml(dotDate(l.promulgation_date)) + "</dd></div>" +
          "<div><dt>시행일자</dt><dd>" + escapeHtml(dotDate(l.effective_date)) + "</dd></div>" +
          "<div><dt>법령종류</dt><dd>" + escapeHtml(l.kind || "-") + "</dd></div>" +
          "<div><dt>소관부처</dt><dd>" + escapeHtml(l.ministry || "-") + "</dd></div>" +
        "</dl>" +
        sectionText("주요 내용", l.summary) +
        '<div class="legal-foot">' + linkButton(l.link, "법령 본문 보기") + "</div>" +
      "</div></li>";
  }

  /* 최신 -> 과거 순서로 월별 구분선을 넣어 스레드를 구성 */
  function threadHtml(rows, dateField, itemFn) {
    var html = "", lastMonth = "";
    rows.forEach(function (r) {
      var m = (r[dateField] || "").slice(0, 7);
      if (m !== lastMonth) {
        if (lastMonth) html += "</ol></div>";
        html += '<div class="thread-month"><div class="thread-month-label">' +
          (m ? m.slice(0, 4) + "년 " + m.slice(5, 7) + "월" : "일자 미상") + '</div><ol class="thread">';
        lastMonth = m;
      }
      html += itemFn(r);
    });
    return html + (lastMonth !== "" || rows.length ? "</ol></div>" : "");
  }

  function renderLegal(kind) {
    var cfg = LEGAL_CFG[kind];
    var el = $(kind + "Result");
    el.innerHTML = emptyState("불러오는 중…");
    loadLegal(kind).then(function (all) {
      var f = readLegalFilter(kind);
      legal.applied = legal.applied || {};
      legal.applied[kind] = f;
      if (f.from && f.to && f.from > f.to) {
        el.innerHTML = emptyState("기간의 시작월이 종료월보다 늦습니다. 기간을 다시 선택해 주세요.");
        legal.view[kind] = [];
        $(kind + "Count").textContent = "";
        return;
      }
      var rows = all.filter(function (r) {
        if (kind === "bills") {
          // 상태를 고르면 그 단계만 표시('진행 중만 보기'보다 우선). 상태가 '전체'일 때만 체크박스를 적용
          if (f.stage) { if (r.stage !== f.stage) return false; }
          else if (f.active && !isActiveBill(r)) return false;
        } else if (kind === "laws" && f.committee) {
          if (lawCommittees(r).indexOf(f.committee) === -1) return false;
        }
        return legalMatches(r, f, cfg.dateField, cfg.textFields);
      });
      rows.sort(function (a, b) {
        var x = a[cfg.dateField] || "", y = b[cfg.dateField] || "";
        if (x !== y) return x < y ? 1 : -1;
        return String(b.id).localeCompare(String(a.id));
      });
      legal.view[kind] = rows;

      var upd = legal.meta && legal.meta.last_updated ? " · 마지막 수집 " + legal.meta.last_updated.slice(0, 16).replace("T", " ") : "";
      $(kind + "Count").textContent = "총 " + rows.length + "건 (전체 " + all.length + "건" + (kind === "bills" ? (f.stage ? ", 상태 " + f.stage : (f.active ? ", 진행 중만" : "")) : (f.committee ? ", 소관위 " + f.committee : "")) + ")" + upd;
      if (legal.failed[kind]) {
        el.innerHTML = emptyState("아직 수집된 데이터가 없습니다. ESG 법률 모니터링 워크플로가 처음 실행되면 표시됩니다.");
      } else if (rows.length === 0) {
        el.innerHTML = emptyState("선택한 조건에 해당하는 " + (kind === "bills" ? "의안" : "법령") + "이 없습니다.");
      } else {
        el.innerHTML = threadHtml(rows, cfg.dateField, kind === "bills" ? billItemHtml : lawItemHtml);
      }
    });
  }

  var BILL_HEADERS = ["의안명", "소관위원회", "유형", "현재 심사단계", "접수일", "본회의 심의일", "본회의 심의결과",
                      "정부이송일", "공포일", "제·개정 배경", "주요 내용", "의안 본문 링크"];
  var LAW_HEADERS = ["법령명", "법령종류", "소관부처", "유형", "제·개정 구분", "공포일자", "시행일자", "주요 내용", "법령 본문 링크"];

  function legalExcelRows(kind, rows) {
    if (kind === "bills") {
      return rows.map(function (b) {
        return [b.name || "", b.committee || "", CAT_LABEL[b.category] || "", b.stage || "", b.receipt_date || "",
                b.plenary_date || "", b.plenary_result || "", b.transfer_date || "", b.promulgation_date || "",
                b.background || "", b.summary || "", b.link || ""];
      });
    }
    return rows.map(function (l) {
      return [l.name || "", l.kind || "", l.ministry || "", CAT_LABEL[l.category] || "", l.revision_type || "",
              l.promulgation_date || "", l.effective_date || "", l.summary || "", l.link || ""];
    });
  }

  /* 마지막 열(링크)을 하이퍼링크로 만든 시트 */
  function buildLegalWorkbook(kind, rows) {
    var XLSX = window.XLSX;
    var headers = kind === "bills" ? BILL_HEADERS : LAW_HEADERS;
    var aoa = [headers].concat(legalExcelRows(kind, rows));
    var ws = XLSX.utils.aoa_to_sheet(aoa);
    var linkCol = headers.length - 1;
    for (var r = 1; r < aoa.length; r++) {
      var url = aoa[r][linkCol];
      if (!isHttpUrl(url)) continue;
      var addr = XLSX.utils.encode_cell({ r: r, c: linkCol });
      if (ws[addr]) ws[addr].l = { Target: url, Tooltip: kind === "bills" ? "의안 본문 열기" : "법령 본문 열기" };
    }
    ws["!cols"] = kind === "bills"
      ? [{ wch: 45 }, { wch: 20 }, { wch: 10 }, { wch: 12 }, { wch: 12 }, { wch: 14 }, { wch: 14 }, { wch: 12 }, { wch: 12 }, { wch: 50 }, { wch: 70 }, { wch: 50 }]
      : [{ wch: 40 }, { wch: 10 }, { wch: 16 }, { wch: 10 }, { wch: 12 }, { wch: 12 }, { wch: 12 }, { wch: 70 }, { wch: 50 }];
    ws["!autofilter"] = { ref: XLSX.utils.encode_range({ s: { r: 0, c: 0 }, e: { r: aoa.length - 1, c: linkCol } }) };
    var wb = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(wb, ws, kind === "bills" ? "의안 정보" : "법령 정보");
    return wb;
  }

  function exportLegal(kind) {
    var rows = legal.view[kind] || [];
    if (rows.length === 0) { alert("추출할 " + (kind === "bills" ? "의안" : "법령") + "이 없습니다. 조건을 확인해 주세요."); return false; }
    if (!window.XLSX) { alert("엑셀 라이브러리를 불러오지 못했습니다. 인터넷 연결(또는 사내망의 cdnjs 접근 차단 여부)을 확인한 뒤 새로고침해 주세요."); return false; }
    var f = (legal.applied && legal.applied[kind]) || readLegalFilter(kind);
    var parts = [kind === "bills" ? "ESG_의안정보" : "ESG_법령정보"];
    if (f.from || f.to) parts.push((f.from || "처음") + "~" + (f.to || "현재"));
    if (f.cat) parts.push(CAT_LABEL[f.cat]);
    if (kind === "bills" && f.stage) parts.push(f.stage);
    else if (kind === "bills" && f.active) parts.push("진행중");
    if (kind === "laws" && f.committee) parts.push(f.committee);
    if (f.kw.trim()) parts.push(f.kw.trim());
    parts.push(todayCompact());
    window.XLSX.writeFile(buildLegalWorkbook(kind, rows), safeFileName(parts.join("_")) + ".xlsx");
    return true;
  }

  function setupLegal() {
    // 브라우저의 입력값 복원(새로고침·뒤로가기)과 무관하게 '진행 중 의안만 보기'는 항상 체크 상태로 시작
    if ($("billsActive")) $("billsActive").checked = true;
    ["bills", "laws"].forEach(function (kind) {
      // 조건을 고른 뒤 '조회' 버튼(또는 키워드 입력창에서 Enter)을 눌러야 결과에 반영됩니다.
      $(kind + "Search").addEventListener("click", function () { renderLegal(kind); });
      $(kind + "Kw").addEventListener("keydown", function (e) {
        if (e.key === "Enter") { e.preventDefault(); renderLegal(kind); }
      });
      $(kind + "Reset").addEventListener("click", function () {
        $(kind + "From").value = ""; $(kind + "To").value = ""; $(kind + "Cat").value = ""; $(kind + "Kw").value = "";
        if ($(kind + "Stage")) $(kind + "Stage").value = "";
        if ($(kind + "Committee")) $(kind + "Committee").value = "";
        if ($(kind + "Active")) $(kind + "Active").checked = true;   // 초기화 시 '진행 중만 보기' 기본값(체크)으로 복원
        renderLegal(kind);
      });
      $(kind + "Export").addEventListener("click", function () { exportLegal(kind); });
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
        if (btn.dataset.tab === "bills" || btn.dataset.tab === "laws") renderLegal(btn.dataset.tab);
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
      setupLegal();
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
  window.__esgDash = { matchesDetail: matchesDetail, buildExcelRows: buildExcelRows, buildWorkbook: buildWorkbook, legal: legal, legalMatches: legalMatches, buildLegalWorkbook: buildLegalWorkbook, renderLegal: renderLegal, state: state, init: init };

  document.addEventListener("DOMContentLoaded", init);
})();
