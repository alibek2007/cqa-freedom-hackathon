import { api } from "./api.js?v=2";

// ------------------------------------------------------------------ state & helpers
const root = document.getElementById("cqa-app");
const S = {
  role: store("cqa.role") || "operator",       // operator | reviewer (no auth — just a switch)
  config: null,
  checklists: [],
  checklistId: null,                             // selected checklist version
  group: store("cqa.group") || "",
  filters: { lang: "", channel: "", q: "" },
  job: null,
};

function store(k, v) {
  try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch { return null; }
}
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const $ = (sel, el = root) => el.querySelector(sel);
const $$ = (sel, el = root) => [...el.querySelectorAll(sel)];
const fmtNum = (n) => (n === null || n === undefined ? "—" : Number(n).toLocaleString("ru-RU", { maximumFractionDigits: 2 }));
const scoreCls = (t) => (t === null || t === undefined ? "" : t >= 85 ? "good" : t >= 60 ? "mid" : "bad");
const LANG = { RU: "RU", KZ: "KZ", EN: "EN" };
const SPEAKER = { client: "Клиент", operator: "Оператор", bot: "Бот", system: "Система" };
const TYPE = { binary: "Да / Нет", scale: "Шкала", flag: "Флаг (без баллов)" };

function toast(msg, err) {
  const t = document.createElement("div");
  t.className = "cqa-toast" + (err ? " err" : "");
  t.textContent = msg;
  root.appendChild(t);
  setTimeout(() => t.remove(), err ? 6000 : 3000);
}
async function guard(fn, btn) {
  if (btn) btn.disabled = true;
  try { return await fn(); } catch (e) { toast(e.message, true); } finally { if (btn) btn.disabled = false; }
}

function answerText(c, a) {
  if (a === "na") return "N/A";
  if (c.type === "scale") return `${a} из ${Math.max(...c.scale.map((s) => s.value))}`;
  if (c.type === "flag") return a === "yes" ? "Да — нарушение" : "Нет";
  return a === "yes" ? "Да" : "Нет";
}
function answerCls(c, a) {
  if (a === "na") return "na";
  if (c.type === "flag") return a === "yes" ? "gross" : "yes";
  if (c.type === "scale") {
    const top = Math.max(...c.scale.map((s) => s.value));
    return a == top ? "yes" : a == 0 ? "no" : "mid";
  }
  return a === "yes" ? "yes" : "no";
}
const chip = (c, a) => `<span class="cqa-chip ${answerCls(c, a)}">${esc(answerText(c, a))}</span>`;
function allowedAnswers(c) {
  const v = c.type === "scale" ? c.scale.map((s) => s.value) : ["yes", "no"];
  return c.allow_na ? [...v, "na"] : v;
}
const checklistOptions = (sel) => S.checklists.map((c) =>
  `<option value="${c.id}" ${c.id == sel ? "selected" : ""}>v${c.id} · ${esc(c.name)}${c.active ? " (активный)" : ""}</option>`).join("");

// ------------------------------------------------------------------ shell & router
function shell(active, body, openAppeals = 0) {
  root.innerHTML = `
  <header class="cqa-header">
    <div class="cqa-logo">FREEDOM <small>Conversation Quality Analytics</small></div>
    <nav class="cqa-nav">
      <a href="#/dialogs" class="${active === "dialogs" ? "active" : ""}">Диалоги</a>
      <a href="#/checklist" class="${active === "checklist" ? "active" : ""}">Чек-лист</a>
      <a href="#/appeals" class="${active === "appeals" ? "active" : ""}">Апелляции${openAppeals ? `<span class="cqa-badge-count">${openAppeals}</span>` : ""}</a>
    </nav>
    <div class="cqa-role">Роль:
      <span class="cqa-seg">
        <button data-role="operator" class="${S.role === "operator" ? "on" : ""}">Оператор</button>
        <button data-role="reviewer" class="${S.role === "reviewer" ? "on" : ""}">Проверяющий</button>
      </span>
    </div>
  </header>
  <main class="cqa-main">${body}</main>`;
  $$("[data-role]").forEach((b) => b.onclick = () => { S.role = b.dataset.role; store("cqa.role", S.role); route(); });
}

async function refreshMeta() {
  const [config, checklists, open] = await Promise.all([api.config(), api.checklists(), api.appeals("open")]);
  S.config = config; S.checklists = checklists;
  if (!S.checklistId || !checklists.some((c) => c.id == S.checklistId)) S.checklistId = (checklists.find((c) => c.active) || checklists[0])?.id;
  return open.length;
}

async function route() {
  const h = location.hash.replace(/^#\/?/, "");
  const [page, ...rest] = h.split("/");
  let open = 0;
  try { open = await refreshMeta(); } catch (e) { shell("", `<div class="cqa-panel cqa-empty">Сервер недоступен: ${esc(e.message)}</div>`); return; }
  if (page === "dialog" && rest.length) return viewDialog(decodeURIComponent(rest.join("/")), open);
  if (page === "checklist") return viewChecklist(open);
  if (page === "appeals") return viewAppeals(open);
  return viewDialogs(open);
}
window.addEventListener("hashchange", route);

// ------------------------------------------------------------------ dialogs list
async function viewDialogs(open) {
  const groups = S.config.groups;
  shell("dialogs", `
    <div class="cqa-toolbar">
      <div class="cqa-group"><span class="cqa-muted">Набор</span>
        <select id="grp"><option value="">Все</option>${groups.map((g) => `<option ${g === S.group ? "selected" : ""}>${esc(g)}</option>`).join("")}</select></div>
      <div class="cqa-group"><input id="gid" placeholder="group_id" size="10">
        <button id="load">Загрузить из API</button>
        <label class="cqa-link" style="cursor:pointer"><input type="file" id="file" accept=".json" hidden><span class="cqa-chip">Загрузить JSON</span></label></div>
      <div class="cqa-group"><span class="cqa-muted">Чек-лист</span><select id="cl">${checklistOptions(S.checklistId)}</select></div>
      <div class="cqa-group"><button class="cqa-primary" id="evalAll">Оценить все</button>
        <button id="reevalAll" title="Заново, даже если уже оценены">Переоценить</button>
        <span id="prog" class="cqa-progress cqa-muted cqa-small"></span></div>
      <div class="cqa-group"><button id="export" title="Формат организаторов (JSON 1.0)">Скачать results.json</button></div>
    </div>
    <div class="cqa-toolbar">
      <select id="fLang"><option value="">Все языки</option>${Object.keys(LANG).map((l) => `<option ${S.filters.lang === l ? "selected" : ""}>${l}</option>`).join("")}</select>
      <select id="fCh"><option value="">Все каналы</option><option value="agent chat" ${S.filters.channel === "agent chat" ? "selected" : ""}>Чат</option><option value="voice call" ${S.filters.channel === "voice call" ? "selected" : ""}>Звонок</option></select>
      <input id="fQ" placeholder="Поиск по ID или тексту" value="${esc(S.filters.q)}">
      <span class="cqa-spacer"></span>
      <span class="cqa-muted cqa-small">Модель: ${esc(S.config.provider === "heuristic" ? "офлайн-эвристика (без LLM)" : S.config.model + " · effort " + S.config.effort)}</span>
    </div>
    <div class="cqa-panel" id="list"><div class="cqa-empty">Загрузка…</div></div>`, open);

  $("#grp").onchange = (e) => { S.group = e.target.value; store("cqa.group", S.group); viewDialogs(open); };
  $("#cl").onchange = (e) => { S.checklistId = +e.target.value; renderList(); };
  ["fLang", "fCh", "fQ"].forEach((id) => $("#" + id).oninput = () => {
    S.filters = { lang: $("#fLang").value, channel: $("#fCh").value, q: $("#fQ").value }; drawRows();
  });
  $("#load").onclick = (e) => guard(async () => {
    const g = $("#gid").value.trim(); if (!g) throw new Error("Введите group_id");
    const r = await api.loadGroup(g); toast(`Загружено диалогов: ${r.loaded} (${r.group_id})`);
    S.group = r.group_id; store("cqa.group", S.group); route();
  }, e.target);
  $("#file").onchange = (e) => guard(async () => {
    const f = e.target.files[0]; if (!f) return;
    const r = await api.upload(f, S.group || "UPLOAD"); toast(`Загружено диалогов: ${r.loaded}`); route();
  });
  $("#evalAll").onclick = (e) => runBatch(false, e.target);
  $("#export").onclick = (e) => guard(async () => {
    if (!S.group) throw new Error("Выберите набор (DEV или контрольный)");
    const r = await api.exportResults(S.group);
    const blob = new Blob([JSON.stringify(r.document, null, 2)], { type: "application/json" });
    const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = "results.json"; a.click();
    toast(r.problems.length ? `Выгружено, но есть замечания: ${r.problems.slice(0, 3).join("; ")}` : `results.json: ${r.document.results.length} записей, проверка пройдена`, r.problems.length > 0);
  }, e.target);
  $("#reevalAll").onclick = (e) => { if (confirm("Переоценить все диалоги набора заново?")) runBatch(true, e.target); };

  let rows = [];
  async function renderList() {
    const r = await api.dialogs(S.group, S.checklistId); rows = r.dialogs; drawRows();
  }
  function drawRows() {
    const f = S.filters, q = f.q.toLowerCase();
    const shown = rows.filter((d) => (!f.lang || d.language === f.lang) && (!f.channel || d.channel === f.channel)
      && (!q || d.conversation_id.toLowerCase().includes(q) || d.preview.toLowerCase().includes(q)));
    const done = rows.filter((d) => d.evaluation).length;
    $("#list").innerHTML = !shown.length ? `<div class="cqa-empty">Нет диалогов. Загрузите набор по group_id или JSON-файл.</div>` : `
      <div class="cqa-muted cqa-small" style="margin-bottom:6px">Показано ${shown.length} из ${rows.length} · оценено ${done}</div>
      <table class="cqa-table"><thead><tr><th>ID</th><th>Канал</th><th>Язык</th><th>Реплик</th><th>Первое сообщение клиента</th><th>Итог</th><th></th></tr></thead><tbody>
      ${shown.map((d) => { const ev = d.evaluation; return `
        <tr class="cqa-click" data-id="${esc(d.conversation_id)}">
          <td><b>${esc(d.conversation_id)}</b><div class="cqa-small cqa-muted">${esc(d.group_id)}</div></td>
          <td>${d.channel === "voice call" ? "📞 Звонок" : "💬 Чат"}</td>
          <td>${esc(d.language)}</td><td>${d.turns}</td>
          <td class="cqa-preview cqa-muted">${esc(d.preview)}</td>
          <td>${ev ? `<span class="cqa-score ${scoreCls(ev.total)}">${ev.total === null ? "не рассч." : fmtNum(ev.total)}</span>
              ${ev.original_total !== ev.total ? `<span class="cqa-small cqa-muted"> (было ${fmtNum(ev.original_total)})</span>` : ""}` : `<span class="cqa-muted">—</span>`}</td>
          <td>${ev && ev.gross_error ? `<span class="cqa-chip gross">Грубая ошибка</span> ` : ""}${ev && ev.status === "appealed" ? `<span class="cqa-chip appeal">Апелляция</span>` : ev && ev.status === "reviewed" ? `<span class="cqa-chip">Пересмотрено</span>` : ""}</td>
        </tr>`; }).join("")}</tbody></table>`;
    $$("tr[data-id]").forEach((tr) => tr.onclick = () => location.hash = `#/dialog/${encodeURIComponent(tr.dataset.id)}`);
  }
  async function runBatch(force, btn) {
    await guard(async () => {
      let job = await api.batch({ group_id: S.group || null, checklist_id: S.checklistId, force });
      btn.disabled = true;
      while (job.running) {
        $("#prog").textContent = `Оценка: ${job.done}/${job.total}${job.failed.length ? ` · ошибок ${job.failed.length}` : ""}`;
        await new Promise((r) => setTimeout(r, 1500));
        job = await api.job(job.id);
        if (job.done % 5 === 0) renderList();
      }
      $("#prog").textContent = `Готово: ${job.done - job.failed.length}/${job.total}${job.cost_usd ? ` · $${job.cost_usd}` : ""}${job.failed.length ? ` · ошибок ${job.failed.length}` : ""}`;
      if (job.failed.length) toast(`Ошибки: ${job.failed.slice(0, 3).map((f) => `${f.conversation_id}: ${f.error}`).join("; ")}`, true);
      btn.disabled = false;
      renderList();
    }, btn);
  }
  await guard(renderList);
}

// ------------------------------------------------------------------ dialog card
async function viewDialog(id, open) {
  shell("dialogs", `<div class="cqa-empty">Загрузка…</div>`, open);
  const data = await guard(() => api.dialog(id, S.checklistId));
  if (!data) return;
  const { dialog, checklist, evaluation: ev, appeals } = data;
  const isCall = dialog.channel === "voice call";
  const fmtOff = (s) => s === null ? "" : `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
  const sec = Object.fromEntries((checklist.sections || []).map((s) => [s.id, s.title]));

  $(".cqa-main").innerHTML = `
    <div class="cqa-toolbar">
      <a href="#/dialogs">← К списку</a>
      <h1 style="margin:0 8px">${esc(dialog.conversation_id)}</h1>
      <span class="cqa-chip">${isCall ? "📞 Звонок" : "💬 Чат"}</span><span class="cqa-chip">${esc(dialog.language)}</span>
      <span class="cqa-chip">${dialog.turns.length} реплик</span>
      <span class="cqa-spacer"></span>
      <span class="cqa-muted">Чек-лист</span><select id="cl">${checklistOptions(checklist.id)}</select>
      <button class="cqa-primary" id="evalBtn">${ev ? "Переоценить" : "Оценить"}</button>
    </div>
    <div class="cqa-card">
      <section class="cqa-panel cqa-transcript">
        <h2>Транскрипт</h2>
        ${isCall ? `<div class="cqa-player">
          <div class="cqa-player-head"><span>🎧 Запись звонка</span>
            <a href="${esc(dialog.audio_url)}" target="_blank" rel="noopener">Открыть в отдельном плеере ↗</a></div>
          <audio class="cqa-audio" id="audio" controls preload="metadata" src="${api.audioUrl(dialog.conversation_id)}"></audio>
          <div class="cqa-player-hint">Нажмите ▶ у реплики или ссылку #N в оценке — запись перемотается к этому месту.</div>
        </div>` : ""}
        <div class="cqa-turns" id="turns">
          ${dialog.turns.map((t) => `
            <div class="cqa-turn ${esc(t.speaker)}" id="turn-${t.n}" data-n="${t.n}">
              <div class="n">#${t.n}</div>
              <div><div class="who">${esc(SPEAKER[t.speaker] || t.speaker)}
                <span class="ts">${isCall ? `<button class="cqa-link" data-seek="${t.offset_s}">▶ ${fmtOff(t.offset_s)}</button>` : esc(t.timestamp)}</span></div>
                <div>${esc(t.text)}</div></div>
            </div>`).join("")}
        </div>
      </section>
      <section id="result">${ev ? "" : `<div class="cqa-panel cqa-empty">Диалог ещё не оценён по чек-листу v${checklist.id}.<br><br>Нажмите «Оценить».</div>`}</section>
    </div>`;

  const audio = $("#audio");
  function jump(n, range) {
    $$(".cqa-turn.flash, .cqa-turn.inrange").forEach((e) => e.classList.remove("flash", "inrange"));
    const [a, b] = range || [n, n];
    for (let i = a; i <= b; i++) $(`#turn-${i}`)?.classList.add(range ? "inrange" : "flash");
    const el = $(`#turn-${a}`);
    if (el) { el.classList.add("flash"); el.scrollIntoView({ behavior: "smooth", block: "center" }); setTimeout(() => el.classList.remove("flash"), 2200); }
    const t = dialog.turns[a - 1];
    if (audio && t && t.offset_s !== null) { audio.currentTime = t.offset_s; audio.play().catch(() => {}); }
  }
  $$("[data-seek]").forEach((b) => b.onclick = () => { if (audio) { audio.currentTime = +b.dataset.seek; audio.play().catch(() => {}); } });
  $("#cl").onchange = (e) => { S.checklistId = +e.target.value; viewDialog(id, open); };
  $("#evalBtn").onclick = (e) => guard(async () => {
    e.target.textContent = "Оценка…";
    await api.evaluate(dialog.conversation_id, checklist.id, !!ev);
    toast("Оценка готова"); viewDialog(id, open);
  }, e.target);
  if (!ev) return;

  const score = ev.final.score, orig = ev.original.score;
  const refs = (item) => [
    ...item.evidence_turns.map((n) => `<button class="cqa-turnref" data-turn="${n}">#${n}</button>`),
    item.checked_range ? `<button class="cqa-turnref range" data-range="${item.checked_range.join("-")}" title="Проверенный участок">проверено #${item.checked_range[0]}–#${item.checked_range[1]}</button>` : "",
  ].join("");

  const critHtml = checklist.criteria.map((c) => {
    const it = ev.final.items[c.id]; if (!it) return "";
    const o = ev.original.items[c.id];
    const pts = score.items[c.id] || {};
    const caps = appeals.filter((a) => a.criterion_id === c.id);
    const openAp = caps.find((a) => a.status === "open");
    return `
    <div class="cqa-crit ${it.answer === "na" ? "isna" : ""}" data-crit="${c.id}">
      <div class="cqa-crit-head">
        <h3>${esc(c.code || "")} ${esc(c.title)}</h3> ${chip(c, it.answer)}
        ${it.reviewed ? `<span class="cqa-chip">проверено</span>` : ""}
        <div class="pts">${c.type === "flag" ? "без баллов" : it.answer === "na" ? `вес ${fmtNum(c.weight)} → перераспределён` :
          `<b>${fmtNum(pts.points)}</b> / ${fmtNum(pts.max_points)} балл.<br>вес ${fmtNum(c.weight)}${pts.effective_weight != c.weight ? ` → ${fmtNum(pts.effective_weight)}` : ""}`}</div>
      </div>
      <div class="cqa-small cqa-muted">${esc(sec[c.section] || "")} · ${TYPE[c.type]}</div>
      <p>${esc(it.explanation)}</p>
      ${it.answer === "na" ? `<p class="cqa-small"><b>Причина N/A:</b> ${esc(it.na_reason || "—")}</p>` : ""}
      <div style="margin-top:6px">${refs(it) || `<span class="cqa-small cqa-muted">без ссылок на реплики</span>`}</div>
      ${o && o.answer !== it.answer ? `<div class="cqa-orig">Исходная оценка: ${chip(c, o.answer)} — ${esc(o.explanation)}</div>` : ""}
      ${caps.map((a) => appealBox(c, a)).join("")}
      ${S.role === "operator" && !openAp ? `<div class="cqa-form" data-appealform="${c.id}" hidden>
          <textarea rows="2" placeholder="Почему вы не согласны с оценкой?"></textarea>
          <div class="cqa-row"><button class="cqa-primary" data-send="${c.id}">Отправить апелляцию</button><button data-cancel="${c.id}">Отмена</button></div></div>
        <div style="margin-top:8px"><button class="cqa-link" data-appeal="${c.id}">Оспорить оценку</button></div>` : ""}
    </div>`;
  }).join("");

  $("#result").innerHTML = `
    <div class="cqa-panel">
      <div class="cqa-total">
        <div><div class="cqa-muted cqa-small">Итоговый балл</div>
          <div class="big cqa-score ${scoreCls(score.total)}">${score.total === null ? "не рассч." : fmtNum(score.total)}<span class="cqa-muted" style="font-size:16px"> / 100</span></div></div>
        ${orig.total !== score.total ? `<div><div class="cqa-muted cqa-small">Исходная оценка</div><div class="was">${fmtNum(orig.total)}</div></div>` : ""}
        <div class="cqa-spacer"></div>
        <div class="cqa-small cqa-muted" style="text-align:right">Чек-лист v${ev.checklist_id} · ${esc(ev.model)}<br>
          ${ev.tokens_in ? `${ev.tokens_in.toLocaleString()} + ${ev.tokens_out.toLocaleString()} токенов · $${ev.cost_usd}` : ""}<br>${esc(ev.created_at)}</div>
      </div>
      ${score.total === null ? `<div class="cqa-banner info">Нет применимых критериев с весом — итог не рассчитывается.</div>` : ""}
      ${score.gross_error ? `<div class="cqa-banner bad"><b>⚠ Грубая ошибка.</b> ${score.gross_criteria.map((cid) => { const c = checklist.criteria.find((x) => x.id === cid); return esc(ev.final.items[cid]?.explanation || c?.title); }).join(" ")}</div>` : ""}
      <div class="cqa-sections">${score.sections.map((s) => `
        <div class="cqa-sec"><div class="cqa-small cqa-muted">${esc(s.title)}</div>
          <b>${fmtNum(s.points)}</b> <span class="cqa-muted cqa-small">/ ${fmtNum(s.max_points)} · ${s.percent === null ? "N/A" : fmtNum(s.percent) + "%"}</span>
          <div class="cqa-bar"><div style="width:${s.percent || 0}%"></div></div></div>`).join("")}</div>
      ${ev.summary ? `<p class="cqa-muted">${esc(ev.summary)}</p>` : ""}
    </div>
    <div style="margin-top:12px">${critHtml}</div>
    ${ev.decisions.length ? `<div class="cqa-panel"><h2>История пересмотра</h2>${ev.decisions.map((d) => {
      const c = checklist.criteria.find((x) => x.id === d.criterion_id);
      return `<div style="margin-bottom:8px"><b>${esc(c ? `${c.code || ""} ${c.title}` : d.criterion_id)}</b>: ${chip(c, d.old_answer)} → ${chip(c, d.new_answer)}
        (${d.action === "confirm" ? "подтверждено" : "изменено"}) · итог ${fmtNum(d.total_before)} → <b>${fmtNum(d.total_after)}</b>
        <div class="cqa-small cqa-muted">${esc(d.reviewer)}, ${esc(d.at)}: ${esc(d.reason)}</div></div>`; }).join("")}</div>` : ""}`;

  $$("[data-turn]").forEach((b) => b.onclick = () => jump(+b.dataset.turn));
  $$("[data-range]").forEach((b) => b.onclick = () => { const [a, z] = b.dataset.range.split("-").map(Number); jump(a, [a, z]); });
  $$("[data-appeal]").forEach((b) => b.onclick = () => { $(`[data-appealform="${b.dataset.appeal}"]`).hidden = false; b.hidden = true; });
  $$("[data-cancel]").forEach((b) => b.onclick = () => viewDialog(id, open));
  $$("[data-send]").forEach((b) => b.onclick = () => guard(async () => {
    const txt = $(`[data-appealform="${b.dataset.send}"] textarea`).value;
    await api.appeal(ev.id, b.dataset.send, txt); toast("Апелляция сохранена"); route();
  }, b));
  bindDecisionForms(() => route());
}

function appealBox(c, a) {
  const d = a.decision;
  return `<div class="cqa-appealbox ${a.status === "resolved" ? "resolved" : ""}">
    <div class="cqa-small"><b>Апелляция #${a.id}</b> · ${a.status === "open" ? "ожидает решения" : "решена"} · ${esc(a.created_at)}</div>
    <div>«${esc(a.comment)}»</div>
    ${d ? `<div class="cqa-small" style="margin-top:4px"><b>Решение:</b> ${d.action === "confirm" ? "оценка подтверждена" : `изменено ${chip(c, d.old_answer)} → ${chip(c, d.new_answer)}`}
      · итог ${fmtNum(d.total_before)} → <b>${fmtNum(d.total_after)}</b><br>Причина: ${esc(d.reason)}</div>` : ""}
    ${a.status === "open" && S.role === "reviewer" ? decisionForm(c, a) : ""}
    ${a.status === "open" && S.role === "operator" ? `<div class="cqa-small cqa-muted" style="margin-top:4px">Решение принимает проверяющий (переключите роль вверху).</div>` : ""}
  </div>`;
}

function decisionForm(c, a) {
  return `<div class="cqa-form" data-decide="${a.id}">
    <div class="cqa-row">
      <select data-action><option value="confirm">Подтвердить оценку</option><option value="change">Изменить оценку</option></select>
      <select data-new hidden>${allowedAnswers(c).map((v) => `<option value="${v}">${esc(answerText(c, v))}</option>`).join("")}</select>
    </div>
    <textarea rows="2" data-reason placeholder="Причина решения (обязательно)"></textarea>
    <div><button class="cqa-primary" data-submit>Сохранить решение</button></div>
  </div>`;
}

function bindDecisionForms(after) {
  $$("[data-decide]").forEach((f) => {
    const act = $("[data-action]", f), nw = $("[data-new]", f);
    act.onchange = () => nw.hidden = act.value !== "change";
    $("[data-submit]", f).onclick = (e) => guard(async () => {
      const r = await api.decide(f.dataset.decide, { action: act.value, new_answer: act.value === "change" ? nw.value : undefined,
        reason: $("[data-reason]", f).value, reviewer: "Проверяющий" });
      const d = r.appeal.decision;
      toast(`Решение сохранено · итог ${fmtNum(d.total_before)} → ${fmtNum(d.total_after)}`);
      after();
    }, e.target);
  });
}

// ------------------------------------------------------------------ appeals queue
async function viewAppeals(open) {
  const status = store("cqa.apStatus") || "open";
  shell("appeals", `
    <div class="cqa-toolbar"><h1 style="margin:0">Апелляции</h1><span class="cqa-spacer"></span>
      <select id="st"><option value="open" ${status === "open" ? "selected" : ""}>Открытые</option><option value="resolved" ${status === "resolved" ? "selected" : ""}>Решённые</option><option value="" ${status === "" ? "selected" : ""}>Все</option></select></div>
    ${S.role !== "reviewer" ? `<div class="cqa-banner info">Принимать решения может роль «Проверяющий» — переключите роль в шапке.</div>` : ""}
    <div id="aps"><div class="cqa-empty">Загрузка…</div></div>`, open);
  $("#st").onchange = (e) => { store("cqa.apStatus", e.target.value); viewAppeals(open); };
  const list = await guard(() => api.appeals(status));
  if (!list) return;
  if (!list.length) { $("#aps").innerHTML = `<div class="cqa-panel cqa-empty">Апелляций нет.</div>`; return; }
  // load the context (checklist, transcript) of each disputed evaluation
  const ctx = {};
  await Promise.all([...new Set(list.map((a) => `${a.conversation_id}|${a.checklist_id}`))].map(async (k) => {
    const [cid, cl] = k.split("|"); ctx[k] = await api.dialog(cid, cl);
  }));
  $("#aps").innerHTML = list.map((a) => {
    const x = ctx[`${a.conversation_id}|${a.checklist_id}`];
    const c = x.checklist.criteria.find((q) => q.id === a.criterion_id);
    const it = a.item;
    const ns = new Set([...(it.evidence_turns || [])]);
    if (it.checked_range) for (let i = it.checked_range[0]; i <= it.checked_range[1]; i++) ns.add(i);
    const focus = [...ns].sort((p, q) => p - q);
    const lo = Math.max(1, (focus[0] || 1) - 2), hi = Math.min(x.dialog.turns.length, (focus[focus.length - 1] || 6) + 2);
    const frag = x.dialog.turns.slice(lo - 1, hi).map((t) => `<div class="cqa-turn ${esc(t.speaker)} ${ns.has(t.n) ? "inrange" : ""}"><div class="n">#${t.n}</div>
      <div><div class="who">${esc(SPEAKER[t.speaker] || t.speaker)}<span class="ts">${esc(t.timestamp.length > 8 ? "" : t.timestamp)}</span></div>${esc(t.text)}</div></div>`).join("");
    return `<div class="cqa-panel" style="margin-bottom:12px">
      <div class="cqa-row"><a href="#/dialog/${encodeURIComponent(a.conversation_id)}"><b>${esc(a.conversation_id)}</b></a>
        <span class="cqa-muted">·</span><b>${esc(a.criterion_title)}</b> ${chip(c, it.answer)}
        <span class="cqa-spacer"></span><span class="cqa-muted cqa-small">итог сейчас: <b>${fmtNum(a.total)}</b></span></div>
      <p><span class="cqa-muted">Обоснование оценки:</span> ${esc(it.explanation)}</p>
      <div class="cqa-fragment">${frag}</div>
      ${appealBox(c, a)}
    </div>`;
  }).join("");
  bindDecisionForms(() => route());
}

// ------------------------------------------------------------------ checklist editor
async function viewChecklist(open) {
  const src = S.checklists.find((c) => c.id == S.checklistId) || S.checklists[0];
  const st = JSON.parse(JSON.stringify(src));       // editable copy
  shell("checklist", `<div id="ed"></div>`, open);

  const newCrit = () => ({ id: "", code: String(st.criteria.length + 1).padStart(2, "0"), section: st.sections[0]?.id || "", title: "",
    description: "", type: "binary", weight: 10, allow_na: false, na_condition: "", scale: [] });
  const weightSum = () => st.criteria.filter((c) => c.type !== "flag").reduce((s, c) => s + (+c.weight || 0), 0);

  function draw() {
    const ws = weightSum();
    $("#ed").innerHTML = `
      <div class="cqa-toolbar"><h1 style="margin:0">Чек-лист</h1>
        <span class="cqa-muted">Версия</span><select id="ver">${checklistOptions(src.id)}</select>
        ${src.active ? `<span class="cqa-chip yes">активный</span>` : `<button id="act">Сделать активным</button>`}
        <span class="cqa-spacer"></span>
        <span class="cqa-muted cqa-small">Каждое сохранение создаёт новую версию; старые оценки остаются привязаны к своей версии.</span></div>
      <div class="cqa-panel" style="margin-bottom:12px">
        <div class="cqa-grid">
          <label class="cqa-field">Название<input data-k="name" value="${esc(st.name)}"></label>
        </div>
        <h2 style="margin-top:12px">Секции</h2>
        ${st.sections.map((s, i) => `<div class="cqa-row" style="margin-bottom:4px"><input data-sec="${i}" value="${esc(s.title)}" style="flex:1"><span class="cqa-small cqa-muted">${esc(s.id)}</span>
          <button class="cqa-danger" data-delsec="${i}">✕</button></div>`).join("")}
        <button id="addSec">+ Секция</button>
      </div>
      ${st.criteria.map((c, i) => `
      <div class="cqa-editor-crit" data-i="${i}">
        <div class="cqa-row"><b>Критерий ${i + 1}</b><span class="cqa-spacer"></span>
          <button data-up="${i}" ${i ? "" : "disabled"}>↑</button><button data-down="${i}" ${i < st.criteria.length - 1 ? "" : "disabled"}>↓</button>
          <button class="cqa-danger" data-del="${i}">Удалить</button></div>
        <div class="cqa-grid">
          <label class="cqa-field">Код<input data-f="code" value="${esc(c.code || "")}"></label>
          <label class="cqa-field" style="grid-column: span 2">Название<input data-f="title" value="${esc(c.title)}"></label>
          <label class="cqa-field">Секция<select data-f="section">${st.sections.map((s) => `<option value="${esc(s.id)}" ${s.id === c.section ? "selected" : ""}>${esc(s.title)}</option>`).join("")}</select></label>
          <label class="cqa-field">Тип ответа<select data-f="type" data-redraw>${Object.entries(TYPE).map(([k, v]) => `<option value="${k}" ${k === c.type ? "selected" : ""}>${v}</option>`).join("")}</select></label>
          ${c.type !== "flag" ? `<label class="cqa-field">Вес<input type="number" min="0" step="1" data-f="weight" value="${c.weight}"></label>` : ""}
          ${c.type !== "flag" ? `<label class="cqa-field">N/A<select data-f="allow_na" data-redraw><option value="0">Не допускается</option><option value="1" ${c.allow_na ? "selected" : ""}>Допускается</option></select></label>` : ""}
        </div>
        <label class="cqa-field">Формулировка / правило оценки (используется моделью)<textarea rows="2" data-f="description">${esc(c.description)}</textarea></label>
        ${c.allow_na && c.type !== "flag" ? `<label class="cqa-field">Условие N/A<input data-f="na_condition" value="${esc(c.na_condition)}"></label>` : ""}
        ${c.type === "scale" ? `<div class="cqa-field">Шкала (ступени; верхняя даёт полный вес)
          ${c.scale.map((s, j) => `<div class="cqa-step"><input type="number" data-sv="${j}" value="${s.value}"><input data-sl="${j}" value="${esc(s.label)}" placeholder="Описание ступени"><button class="cqa-danger" data-sdel="${j}">✕</button></div>`).join("")}
          <div><button data-sadd>+ Ступень</button></div></div>` : ""}
      </div>`).join("")}
      <div class="cqa-sticky-bar cqa-row">
        <button id="add">+ Критерий</button>
        <span class="cqa-muted">Сумма весов: <b style="color:${ws === 100 ? "inherit" : "var(--warn)"}">${fmtNum(ws)}</b>${ws !== 100 ? " (рекомендуется 100; итог всё равно нормируется к 100)" : ""}</span>
        <span class="cqa-spacer"></span>
        <button class="cqa-primary" id="save">Сохранить новую версию</button>
        <select id="applyGrp"><option value="">Все наборы</option>${S.config.groups.map((g) => `<option ${g === S.group ? "selected" : ""}>${esc(g)}</option>`).join("")}</select>
        <button id="apply">Применить к диалогам</button>
        <span id="prog" class="cqa-muted cqa-small"></span>
      </div>`;
    bind();
  }

  function bind() {
    $("#ver").onchange = (e) => { S.checklistId = +e.target.value; viewChecklist(open); };
    $("#act") && ($("#act").onclick = (e) => guard(async () => { await api.activate(src.id); toast("Версия активна"); route(); }, e.target));
    $("[data-k=name]").oninput = (e) => st.name = e.target.value;
    $$("[data-sec]").forEach((el) => el.oninput = () => st.sections[+el.dataset.sec].title = el.value);
    $$("[data-delsec]").forEach((b) => b.onclick = () => {
      const s = st.sections[+b.dataset.delsec];
      if (st.criteria.some((c) => c.section === s.id)) return toast("В секции есть критерии", true);
      st.sections.splice(+b.dataset.delsec, 1); draw();
    });
    $("#addSec").onclick = () => { st.sections.push({ id: "s" + Date.now().toString(36), title: "Новая секция" }); draw(); };
    $$(".cqa-editor-crit").forEach((box) => {
      const c = st.criteria[+box.dataset.i];
      $$("[data-f]", box).forEach((el) => {
        const f = el.dataset.f;
        const set = () => {
          if (f === "weight") c.weight = +el.value;
          else if (f === "allow_na") c.allow_na = el.value === "1";
          else c[f] = el.value;
          if (f === "type") {
            if (c.type === "scale" && !c.scale.length) c.scale = [{ value: 10, label: "" }, { value: 5, label: "" }, { value: 0, label: "" }];
            if (c.type === "flag") { c.weight = 0; c.allow_na = false; } else if (!c.weight) c.weight = 10;
          }
          if (el.hasAttribute("data-redraw")) draw();
          else if (f === "weight") $(".cqa-sticky-bar b").textContent = fmtNum(weightSum());
        };
        el.oninput = el.tagName === "SELECT" ? null : set;
        el.onchange = set;
      });
      $$("[data-sv]", box).forEach((el) => el.oninput = () => c.scale[+el.dataset.sv].value = +el.value);
      $$("[data-sl]", box).forEach((el) => el.oninput = () => c.scale[+el.dataset.sl].label = el.value);
      $$("[data-sdel]", box).forEach((b) => b.onclick = () => { c.scale.splice(+b.dataset.sdel, 1); draw(); });
      $("[data-sadd]", box) && ($("[data-sadd]", box).onclick = () => { c.scale.push({ value: 0, label: "" }); draw(); });
    });
    $$("[data-del]").forEach((b) => b.onclick = () => { if (confirm("Удалить критерий?")) { st.criteria.splice(+b.dataset.del, 1); draw(); } });
    $$("[data-up]").forEach((b) => b.onclick = () => { const i = +b.dataset.up; [st.criteria[i - 1], st.criteria[i]] = [st.criteria[i], st.criteria[i - 1]]; draw(); });
    $$("[data-down]").forEach((b) => b.onclick = () => { const i = +b.dataset.down; [st.criteria[i + 1], st.criteria[i]] = [st.criteria[i], st.criteria[i + 1]]; draw(); });
    $("#add").onclick = () => { st.criteria.push(newCrit()); draw(); $(".cqa-editor-crit:last-of-type")?.scrollIntoView({ behavior: "smooth" }); };
    $("#save").onclick = (e) => guard(async () => {
      const saved = await api.saveChecklist(st);
      S.checklistId = saved.id;
      toast(`Сохранена версия v${saved.id} — она активна. Нажмите «Применить к диалогам».`);
      await refreshMeta(); viewChecklist(open);
    }, e.target);
    $("#apply").onclick = (e) => guard(async () => {
      let job = await api.batch({ group_id: $("#applyGrp").value || null, checklist_id: src.id, force: false });
      e.target.disabled = true;
      while (job.running) { $("#prog").textContent = `Оценка: ${job.done}/${job.total}`; await new Promise((r) => setTimeout(r, 1500)); job = await api.job(job.id); }
      $("#prog").textContent = `Готово: ${job.done - job.failed.length}/${job.total}${job.failed.length ? ` · ошибок ${job.failed.length}` : ""}`;
      e.target.disabled = false;
      toast(`Чек-лист v${src.id} применён`);
    }, e.target);
  }
  draw();
}

route();
