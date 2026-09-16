// 台股ETF除息追蹤 — 前端渲染邏輯
// 讀取 data/dividends.json (逐筆除息事件) 與 data/etf_meta.json (每檔ETF的頻率推估)

const DOW_LABELS = ["日", "一", "二", "三", "四", "五", "六"];
const FREQ_ORDER = ["月配", "雙月配", "季配", "半年配", "年配", "未知"];

let STATE = {
  events: [],
  etfs: {},
  filters: { q: "", market: "全部", category: "全部" },
};

async function loadData() {
  const [divRes, metaRes] = await Promise.all([
    fetch("data/dividends.json").then((r) => r.json()).catch(() => ({ events: [], updated_at: null })),
    fetch("data/etf_meta.json").then((r) => r.json()).catch(() => ({ etfs: {}, updated_at: null })),
  ]);
  STATE.events = divRes.events || [];
  STATE.etfs = metaRes.etfs || {};
  STATE.updatedAt = divRes.updated_at || metaRes.updated_at || null;
}

function fmtUpdated(iso) {
  if (!iso) return "尚未執行過首次抓取";
  const d = new Date(iso);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}/${pad(d.getMonth() + 1)}/${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())} 更新`;
}

function renderHero() {
  document.getElementById("updated-at").textContent = fmtUpdated(STATE.updatedAt);
  const etfCount = Object.keys(STATE.etfs).length;
  const today = new Date();
  const in7 = new Date(today);
  in7.setDate(today.getDate() + 7);
  const upcoming = STATE.events.filter((e) => {
    const d = parseISODate(e.ex_date);
    return d >= stripTime(today) && d <= stripTime(in7);
  }).length;
  document.getElementById("hero-stats").innerHTML =
    `追蹤 <span class="num">${etfCount}</span> 檔ETF ・ 未來7天內 <span class="num">${upcoming}</span> 檔除息`;
}

function stripTime(d) {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

// "YYYY-MM-DD" -> 本地時間的 Date（避免 new Date(字串) 被當UTC解析造成日期位移）
function parseISODate(str) {
  const [y, m, d] = str.split("-").map(Number);
  return new Date(y, m - 1, d);
}

function eventsByDay(year, month /* 0-indexed */) {
  const map = {};
  for (const e of STATE.events) {
    const d = parseISODate(e.ex_date);
    if (d.getFullYear() === year && d.getMonth() === month) {
      const key = d.getDate();
      (map[key] = map[key] || []).push(e);
    }
  }
  return map;
}

function freqOf(code) {
  return (STATE.etfs[code] && STATE.etfs[code].frequency) || "未知";
}

function monthsToShow() {
  const today = new Date();
  const list = [{ year: today.getFullYear(), month: today.getMonth() }];
  const next = new Date(today.getFullYear(), today.getMonth() + 1, 1);
  list.push({ year: next.getFullYear(), month: next.getMonth() });
  // 月底前(倒數7天內)就先把下下個月也接上，避免翻月瞬間才出現
  const lastDay = new Date(today.getFullYear(), today.getMonth() + 1, 0).getDate();
  if (today.getDate() >= lastDay - 6) {
    const next2 = new Date(today.getFullYear(), today.getMonth() + 2, 1);
    list.push({ year: next2.getFullYear(), month: next2.getMonth() });
  }
  return list;
}

function buildMonthCard(year, month) {
  const today = stripTime(new Date());
  const first = new Date(year, month, 1);
  const startWeekday = first.getDay();
  const daysInMonth = new Date(year, month + 1, 0).getDate();
  const dayMap = eventsByDay(year, month);

  const card = document.createElement("div");
  card.className = "month-card";

  const title = document.createElement("div");
  title.className = "month-title";
  title.innerHTML = `${month + 1}月 <span class="yr num">${year}</span>`;
  card.appendChild(title);

  const dowRow = document.createElement("div");
  dowRow.className = "dow-row";
  DOW_LABELS.forEach((l) => {
    const s = document.createElement("span");
    s.textContent = l;
    dowRow.appendChild(s);
  });
  card.appendChild(dowRow);

  const grid = document.createElement("div");
  grid.className = "day-grid";

  for (let i = 0; i < startWeekday; i++) {
    const empty = document.createElement("div");
    empty.className = "day-cell empty";
    grid.appendChild(empty);
  }

  for (let day = 1; day <= daysInMonth; day++) {
    const cell = document.createElement("div");
    const cellDate = new Date(year, month, day);
    cell.className = "day-cell" + (stripTime(cellDate).getTime() === today.getTime() ? " today" : "");

    const num = document.createElement("div");
    num.className = "day-num num";
    num.textContent = day;
    cell.appendChild(num);

    const dayEvents = (dayMap[day] || []).slice().sort((a, b) => a.code.localeCompare(b.code));
    if (dayEvents.length) {
      const row = document.createElement("div");
      row.className = "badge-row";
      const shown = dayEvents.slice(0, 3);
      shown.forEach((ev) => {
        const b = document.createElement("span");
        b.className = `etf-badge freq-${freqOf(ev.code)}`;
        b.textContent = ev.code;
        b.title = `${ev.code} ${ev.name}`;
        b.addEventListener("click", () => openDayPopover(cellDate, dayEvents));
        row.appendChild(b);
      });
      if (dayEvents.length > shown.length) {
        const more = document.createElement("span");
        more.className = "etf-more";
        more.textContent = `+${dayEvents.length - shown.length}`;
        more.addEventListener("click", () => openDayPopover(cellDate, dayEvents));
        row.appendChild(more);
      }
      cell.appendChild(row);
    }
    grid.appendChild(cell);
  }

  card.appendChild(grid);
  return card;
}

function renderCalendars() {
  const container = document.getElementById("calendars");
  container.innerHTML = "";
  if (!STATE.events.length) {
    container.innerHTML = `<div class="empty-state">尚無除息資料 — 每日排程第一次成功執行後會自動出現在這裡。</div>`;
    return;
  }
  monthsToShow().forEach(({ year, month }) => {
    container.appendChild(buildMonthCard(year, month));
  });
}

function openDayPopover(date, events) {
  const overlay = document.getElementById("overlay");
  const pop = document.getElementById("popover-content");
  const pad = (n) => String(n).padStart(2, "0");
  const title = `${date.getFullYear()}/${pad(date.getMonth() + 1)}/${pad(date.getDate())} 除息 (${events.length}檔)`;

  pop.innerHTML = "";
  const h3 = document.createElement("h3");
  h3.textContent = title;
  pop.appendChild(h3);

  events
    .slice()
    .sort((a, b) => a.code.localeCompare(b.code))
    .forEach((ev) => {
      const item = document.createElement("div");
      item.className = "item";
      item.innerHTML = `
        <div class="code-name">${ev.code} ${ev.name}</div>
        <div class="row"><span>配息頻率</span><span class="val">${freqOf(ev.code)}</span></div>
        <div class="row"><span>配息金額</span><span class="val num">${ev.amount} 元/股</span></div>
        <div class="row"><span>市場</span><span class="val">${ev.market === "TWSE" ? "上市" : "上櫃"}</span></div>
      `;
      pop.appendChild(item);
    });

  const closeBtn = document.createElement("button");
  closeBtn.className = "close";
  closeBtn.textContent = "關閉";
  closeBtn.addEventListener("click", closePopover);
  pop.appendChild(closeBtn);

  overlay.classList.add("open");
}

function closePopover() {
  document.getElementById("overlay").classList.remove("open");
}

function renderAnnualTable() {
  const wrap = document.getElementById("annual-table-wrap");
  const codes = Object.keys(STATE.etfs).sort();
  if (!codes.length) {
    wrap.innerHTML = `<div class="empty-state">尚無ETF清單資料 — 每日排程第一次成功執行後會自動出現在這裡。</div>`;
    return;
  }

  const q = STATE.filters.q.trim().toLowerCase();
  const filtered = codes.filter((code) => {
    const e = STATE.etfs[code];
    if (STATE.filters.market !== "全部" && e.market !== STATE.filters.market) return false;
    if (STATE.filters.category !== "全部" && e.category !== STATE.filters.category) return false;
    if (q && !(e.code.toLowerCase().includes(q) || e.name.toLowerCase().includes(q))) return false;
    return true;
  });

  const table = document.createElement("table");
  table.className = "annual";
  const thead = document.createElement("thead");
  thead.innerHTML =
    `<tr><th class="name-col">ETF</th>` +
    Array.from({ length: 12 }, (_, i) => `<th>${i + 1}月</th>`).join("") +
    `</tr>`;
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  filtered.forEach((code) => {
    const e = STATE.etfs[code];
    const tr = document.createElement("tr");
    const marketLabel = e.market === "TWSE" ? "上市" : "上櫃";
    let row = `<td class="name-col"><span class="code num">${e.code}</span><span class="nm">${e.name}</span><span class="tag">${marketLabel}</span><span class="tag">${e.category}</span></td>`;
    for (let m = 1; m <= 12; m++) {
      const on = e.freq_months.includes(m);
      row += `<td><span class="mo-dot ${on ? "on freq-" + e.frequency : ""}"></span></td>`;
    }
    tr.innerHTML = row;
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);

  wrap.innerHTML = "";
  if (!filtered.length) {
    wrap.innerHTML = `<div class="empty-state">找不到符合條件的ETF</div>`;
    return;
  }
  const scrollDiv = document.createElement("div");
  scrollDiv.className = "table-scroll";
  scrollDiv.appendChild(table);
  wrap.appendChild(scrollDiv);
}

function setupFilters() {
  const searchInput = document.getElementById("search-input");
  searchInput.addEventListener("input", (e) => {
    STATE.filters.q = e.target.value;
    renderAnnualTable();
  });

  document.querySelectorAll("[data-market-chip]").forEach((chip) => {
    chip.addEventListener("click", () => {
      document.querySelectorAll("[data-market-chip]").forEach((c) => c.classList.remove("active"));
      chip.classList.add("active");
      STATE.filters.market = chip.dataset.marketChip;
      renderAnnualTable();
    });
  });

  document.querySelectorAll("[data-cat-chip]").forEach((chip) => {
    chip.addEventListener("click", () => {
      document.querySelectorAll("[data-cat-chip]").forEach((c) => c.classList.remove("active"));
      chip.classList.add("active");
      STATE.filters.category = chip.dataset.catChip;
      renderAnnualTable();
    });
  });

  document.getElementById("overlay").addEventListener("click", (e) => {
    if (e.target.id === "overlay") closePopover();
  });
}

async function init() {
  await loadData();
  renderHero();
  renderCalendars();
  renderAnnualTable();
  setupFilters();
}

init();
