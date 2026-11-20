// ---- auth state ----
let TOKEN = localStorage.getItem("token") || null;
let ROLE = localStorage.getItem("role") || null;
let USERNAME = localStorage.getItem("username") || null;

const TAB_ROLES = {
  cases:     ["investigator", "case_manager", "location_analyst", "admin"],
  lookup:    ["investigator", "case_manager", "admin"],
  map:       ["location_analyst", "case_manager", "admin"],
  analytics: ["location_analyst", "case_manager", "admin"],
  timeline:  ["location_analyst", "case_manager", "admin"],
  consent:   ["investigator", "case_manager", "admin"],
  graph:     ["investigator", "case_manager", "location_analyst", "admin"],
  audit:     ["auditor", "admin"],
  users:     ["admin"],
};
const TAB_LABELS = {
  cases: "📁 Cases", lookup: "🔎 Lookup", map: "🗺️ Map & Geofence", analytics: "📈 Analytics",
  timeline: "⏱️ Timeline", consent: "✅ Consent", graph: "🕸️ Entity Graph",
  audit: "📜 Audit", users: "👤 Users",
};

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (TOKEN) headers["Authorization"] = "Bearer " + TOKEN;
  const res = await fetch(path, { ...opts, headers });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) { logout(); throw new Error("Sesi berakhir, login lagi."); }
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

// Fetch a protected file with the auth header, then trigger a browser download.
async function downloadFile(path, filename) {
  try {
    const res = await fetch(path, { headers: { Authorization: "Bearer " + TOKEN } });
    if (!res.ok) { const e = await res.json().catch(() => ({})); return alert(e.error || res.statusText); }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = filename;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  } catch (e) { alert(e.message); }
}

// ---- login / logout ----
async function doLogin() {
  const username = document.getElementById("loginUser").value.trim();
  const password = document.getElementById("loginPass").value;
  const err = document.getElementById("loginErr");
  err.classList.add("hidden");
  try {
    const r = await fetch("/v1/auth/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || "Login gagal");
    TOKEN = d.token; ROLE = d.user.role; USERNAME = d.user.username;
    localStorage.setItem("token", TOKEN);
    localStorage.setItem("role", ROLE);
    localStorage.setItem("username", USERNAME);
    startApp();
  } catch (e) {
    err.textContent = e.message; err.classList.remove("hidden");
  }
}

function logout() {
  TOKEN = ROLE = USERNAME = null;
  localStorage.clear();
  document.getElementById("appShell").classList.add("hidden");
  document.getElementById("loginOverlay").classList.remove("hidden");
}

function startApp() {
  document.getElementById("loginOverlay").classList.add("hidden");
  document.getElementById("appShell").classList.remove("hidden");
  document.getElementById("whoami").textContent = `${USERNAME} · ${ROLE}`;
  buildTabs();
  refreshStats();
}

// ---- tabs (role-filtered) ----
function buildTabs() {
  const allowed = Object.keys(TAB_ROLES).filter(t => TAB_ROLES[t].includes(ROLE));
  const bar = document.getElementById("tabs");
  bar.innerHTML = allowed.map((t, i) =>
    `<div class="tab ${i === 0 ? "active" : ""}" data-tab="${t}">${TAB_LABELS[t]}</div>`).join("");
  bar.querySelectorAll(".tab").forEach(el => el.addEventListener("click", () => selectTab(el.dataset.tab)));
  document.querySelectorAll(".tabpane").forEach(p => p.classList.add("hidden"));
  if (allowed.length) selectTab(allowed[0]);
}

function selectTab(name) {
  document.querySelectorAll(".tab").forEach(x => x.classList.toggle("active", x.dataset.tab === name));
  document.querySelectorAll(".tabpane").forEach(p =>
    p.classList.toggle("hidden", p.id !== "tab-" + name));
  if (name === "map") { setTimeout(initMap, 50); loadConsentsInto("mapConsent"); loadCasesInto("gfCase", "geofence"); loadGeofences(); loadMap(); }
  if (name === "analytics") { loadConsentsInto("anConsent"); loadGeofencesInto("anGeofence"); }
  if (name === "timeline") { setTimeout(initTlMap, 50); loadConsentsInto("tlConsent"); }
  if (name === "consent") { loadConsents(); loadCasesInto("cCase"); }
  if (name === "lookup") loadCasesInto("lookupCase");
  if (name === "cases") loadCases();
  if (name === "graph") setTimeout(loadGraph, 50);
  if (name === "audit") loadAudit();
  if (name === "users") { loadUsers(); document.getElementById("retentionInfo").innerHTML = ""; }
  refreshStats();
}

// ---- stats ----
async function refreshStats() {
  try {
    const s = await api("/v1/summary");
    const cells = [
      ["Case open", s.cases_open], ["Entities", s.entities], ["Findings", s.findings],
      ["Consent aktif", s.consents_active], ["Titik lokasi", s.location_points],
      ["Geofences", s.geofences], ["Geo events", s.geofence_events],
      ["Audit", s.audit_events],
    ];
    document.getElementById("stats").innerHTML = cells.filter(([, n]) => n !== null && n !== undefined).map(([l, n]) =>
      `<div class="stat"><div class="n">${n}</div><div class="l">${l}</div></div>`).join("");
  } catch (e) {}
}

// ---- lookup ----
async function doLookup() {
  const value = document.getElementById("lookupValue").value.trim();
  const default_region = document.getElementById("defaultRegion").value || null;
  const case_id = document.getElementById("lookupCase").value || null;
  const box = document.getElementById("lookupResult");
  if (!value) { box.innerHTML = `<p class="muted">Masukkan identifier.</p>`; return; }
  if (!case_id && !isManager()) { box.innerHTML = `<p class="pill bad">Pilih case dulu.</p>`; return; }
  box.innerHTML = `<p class="muted">Menganalisis…</p>`;
  try {
    const r = await api("/v1/lookup", { method: "POST", body: JSON.stringify({ value, default_region, case_id }) });
    const e = r.enrichment || {};
    let h = `<div class="kv">
      <div class="k">Tipe</div><div>${esc(r.identifier_type)}</div>
      <div class="k">Normalized</div><div>${esc(r.normalized || "—")}</div>
      <div class="k">Confidence</div><div>${(r.confidence * 100).toFixed(0)}%</div>`;
    if (r.identifier_type === "phone") {
      h += `<div class="k">Valid</div><div>${esc(e.valid ? '<span class="pill ok">valid</span>' : '<span class="pill bad">tidak valid</span>')}</div>
        <div class="k">Negara</div><div>${esc(e.region || "—")} (+${esc(e.country_code || "—")})</div>
        <div class="k">Wilayah</div><div>${esc(e.location || "—")}</div>
        <div class="k">Operator</div><div>${esc(e.carrier || "—")}</div>
        <div class="k">Tipe line</div><div>${esc(e.line_type || "—")}</div>
        <div class="k">Timezone</div><div>${esc((e.timezones || []).join(", ") || "—")}</div>`;
    } else if (r.identifier_type === "ip") {
      if (e.status === "success") {
        h += `<div class="k">Negara</div><div>${esc(e.country || "—")} (${esc(e.country_code || "—")})</div>
          <div class="k">Wilayah</div><div>${esc(e.region || "—")}</div>
          <div class="k">Kota</div><div>${esc(e.city || "—")}</div>
          <div class="k">Koordinat (perkiraan)</div><div>${esc(e.latitude)}, ${esc(e.longitude)}</div>
          <div class="k">Timezone</div><div>${esc(e.timezone || "—")}</div>
          <div class="k">ISP / Org</div><div>${esc(e.isp || "—")} / ${esc(e.org || "—")}</div>
          <div class="k">ASN</div><div>${esc(e.asn || "—")}</div>
          <div class="k">Proxy/Hosting/Mobile</div><div>${esc(e.is_proxy ? "proxy " : "")}${esc(e.is_hosting ? "hosting " : "")}${esc(e.is_mobile ? "mobile" : "")} ${!(e.is_proxy||e.is_hosting||e.is_mobile) ? "—" : ""}</div>`;
      } else {
        h += `<div class="k">Status</div><div><span class="pill bad">${esc(e.status)}</span> ${esc(e.error || "")}</div>`;
      }
    }
    h += `</div>`;
    if (r.identifier_type === "ip" && e.status === "success") {
      h += `<p style="margin-top:8px;"><a href="https://www.openstreetmap.org/?mlat=${esc(e.latitude)}&mlon=${esc(e.longitude)}#map=11/${esc(e.latitude)}/${esc(e.longitude)}" target="_blank" rel="noopener">🗺️ Lihat perkiraan lokasi IP di OpenStreetMap</a></p>`;
    }
    if (r.identifier_type === "username") {
      const rows = (e.results || []).map(s => {
        const badge = s.status === "found" ? '<span class="pill ok">found</span>'
          : s.status === "not_found" ? '<span class="pill bad">not found</span>'
          : s.status === "manual" ? '<span class="pill warn">cek manual</span>'
          : '<span class="pill">?</span>';
        return `<tr><td>${esc(s.site)}</td><td>${badge}</td><td><a href="${esc(s.url)}" target="_blank" rel="noopener">buka</a></td></tr>`;
      }).join("");
      h += `<p style="margin-top:10px;"><b>${esc(e.found_count || 0)}</b> profil terkonfirmasi ada.</p>
        <table><tr><th>Situs</th><th>Status</th><th>Link</th></tr>${rows}</table>`;
    }
    if (e.note) h += `<p class="muted" style="margin-top:12px;">ℹ️ ${esc(e.note)}</p>`;
    box.innerHTML = h; refreshStats();
  } catch (err) { box.innerHTML = `<p class="pill bad">${esc(err.message)}</p>`; }
}

// ---- consent ----
async function createConsent() {
  const subject_label = document.getElementById("cSubject").value.trim();
  const purpose = document.getElementById("cPurpose").value.trim();
  const retention_days = parseInt(document.getElementById("cRetention").value) || 30;
  const phone = document.getElementById("cPhone").value.trim() || null;
  const case_id = document.getElementById("cCase").value || null;
  if (!subject_label || !purpose) return alert("Isi label dan tujuan.");
  if (!case_id && !isManager()) return alert("Pilih case dulu.");
  try {
    await api("/v1/consents", { method: "POST", body: JSON.stringify({ subject_label, purpose, retention_days, phone, case_id }) });
  } catch (e) { return alert(e.message); }
  document.getElementById("cSubject").value = ""; document.getElementById("cPurpose").value = "";
  document.getElementById("cPhone").value = "";
  loadConsents(); refreshStats();
}

// HTML-escape untrusted text before it goes into innerHTML.
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, ch =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}

// ---- public tunnel link + QR ----
let PUBLIC_BASE = null;
async function refreshPublicUrl() {
  const el = document.getElementById("tunnelStatus");
  try {
    const r = await api("/v1/public-url");
    PUBLIC_BASE = r.base_url;
    if (el) el.innerHTML = PUBLIC_BASE
      ? `🌐 Link publik aktif: <code>${esc(PUBLIC_BASE)}</code> — hanya halaman consent agent yang bisa diakses dari internet.`
      : `⚠️ Tunnel belum aktif (${esc(r.reason || "unknown")}). Link di bawah hanya bisa dibuka di PC ini.`;
  } catch (e) { PUBLIC_BASE = null; if (el) el.textContent = ""; }
}
function agentUrl(c) { return (PUBLIC_BASE || location.origin) + c.agent_link; }

let qrUrlCurrent = "";
function showQr(url) {
  qrUrlCurrent = url;
  const box = document.getElementById("qrBox");
  box.innerHTML = "";
  if (window.QRCode) new QRCode(box, { text: url, width: 220, height: 220 });
  else box.textContent = "Library QR gagal dimuat — salin link manual.";
  document.getElementById("qrUrl").textContent = url;
  document.getElementById("qrModal").classList.remove("hidden");
}
function closeQr() { document.getElementById("qrModal").classList.add("hidden"); }
async function copyQrUrl() {
  try { await navigator.clipboard.writeText(qrUrlCurrent); alert("Link disalin."); }
  catch (e) { prompt("Salin link ini:", qrUrlCurrent); }
}

async function loadConsents() {
  await refreshPublicUrl();
  const { consents } = await api("/v1/consents");
  const box = document.getElementById("consentList");
  if (!consents.length) { box.innerHTML = `<p class="muted">Belum ada consent.</p>`; return; }
  box.innerHTML = `<table><tr><th>Subjek</th><th>Status</th><th>Agent link</th><th></th></tr>` +
    consents.map(c => `<tr>
      <td>${esc(c.subject_label)}${c.phone_e164 ? ` <span class="pill warn">${esc(c.phone_e164)}</span>` : ""}<br><span class="muted">${esc(c.purpose)} · retention ${esc(c.retention_days ?? 30)}d</span></td>
      <td>${c.status === "active" ? '<span class="pill ok">active</span>' : '<span class="pill bad">revoked</span>'}</td>
      <td>${c.status === "active"
        ? (PUBLIC_BASE
            ? `<button style="margin:0 4px 0 0;padding:5px 10px;" data-url="${esc(agentUrl(c))}" onclick="showQr(this.dataset.url)">📱 QR</button><a href="${esc(agentUrl(c))}" target="_blank" rel="noopener">link publik</a>`
            : `<a href="${esc(c.agent_link)}" target="_blank">lokal</a>`)
        : "—"}</td>
      <td>
        <button class="secondary" style="margin:0 4px 0 0;padding:5px 10px;" onclick="linkPhone('${c.id}')">${c.phone_e164 ? "📞 Ubah" : "📞 Tautkan nomor"}</button>
        ${c.status === "active" ? `<button class="danger" style="margin:0;padding:5px 10px;" onclick="revoke('${c.id}')">Revoke</button>` : ""}
      </td>
    </tr>`).join("") + `</table>`;
}

async function loadConsentsInto(selId) {
  try {
    const { consents } = await api("/v1/consents");
    const sel = document.getElementById(selId);
    if (sel) sel.innerHTML = `<option value="">Semua consent</option>` +
      consents.map(c => `<option value="${esc(c.id)}">${esc(c.subject_label)} (${esc(c.status)})</option>`).join("");
  } catch (e) {}
}

async function linkPhone(id) {
  const phone = prompt("Nomor HP subjek (mis. 081908823122):");
  if (!phone || !phone.trim()) return;
  try {
    const r = await api(`/v1/consents/${id}/link-phone`, {
      method: "POST", body: JSON.stringify({ phone: phone.trim() }) });
    loadConsents(); refreshStats();
    alert("Tertaut: " + r.phone_e164);
  } catch (e) { alert(e.message); }
}

async function revoke(id) {
  if (!confirm("Revoke consent ini? Titik lokasi berikutnya akan ditolak.")) return;
  await api(`/v1/consents/${id}/revoke`, { method: "POST" });
  loadConsents(); refreshStats();
}

// ---- cases ----
let CURRENT_CASE = null;
const btnSm = 'style="margin:0 4px 0 0;padding:5px 10px;"';

// Strict case ABAC: managers may create data outside any case (or a shared
// geofence); everyone else must choose one of their own open cases.
const isManager = () => ["admin", "case_manager"].includes(ROLE);
async function loadCasesInto(selId, kind = "data") {
  const sel = document.getElementById(selId);
  if (!sel) return;
  const keep = sel.value;
  const noneLabel = kind === "geofence" ? "— area bersama (semua case) —" : "— tanpa case —";
  try {
    const { cases } = await api("/v1/cases");
    const open = cases.filter(c => c.status === "open");
    const opts = open.map(c => `<option value="${esc(c.id)}">${esc(c.title)}</option>`).join("");
    if (isManager()) sel.innerHTML = `<option value="">${noneLabel}</option>` + opts;
    else sel.innerHTML = opts
      ? `<option value="" disabled selected>— pilih case (wajib) —</option>` + opts
      : `<option value="" disabled selected>(Anda belum menjadi anggota case yang terbuka)</option>`;
    if (keep && [...sel.options].some(o => o.value === keep)) sel.value = keep;
  } catch (e) { sel.innerHTML = `<option value="">(gagal memuat case)</option>`; }
}

async function loadCases() {
  const box = document.getElementById("caseList");
  try {
    const { cases, can_manage } = await api("/v1/cases");
    document.getElementById("caseCreateCard").classList.toggle("hidden", !can_manage);
    if (!cases.length) {
      box.innerHTML = `<p class="muted">${can_manage ? "Belum ada case." : "Anda belum menjadi anggota case mana pun."}</p>`;
      return;
    }
    box.innerHTML = `<table><tr><th>Case</th><th>Status</th><th>Isi</th></tr>` + cases.map(c => `
      <tr class="clickable${c.id === CURRENT_CASE ? " selected" : ""}" data-id="${esc(c.id)}" onclick="openCase(this.dataset.id)">
        <td><b>${esc(c.title)}</b><br><span class="muted">oleh ${esc(c.created_by_username || "—")}</span></td>
        <td>${c.status === "open" ? '<span class="pill ok">open</span>' : '<span class="pill bad">closed</span>'}</td>
        <td class="muted">${esc(c.consent_count)} consent · ${esc(c.finding_count)} finding · ${esc(c.member_count)} anggota</td>
      </tr>`).join("") + `</table>`;
  } catch (e) { box.innerHTML = `<p class="pill bad">${esc(e.message)}</p>`; }
}

async function createCase() {
  const title = document.getElementById("caseTitle").value.trim();
  const description = document.getElementById("caseDesc").value.trim() || null;
  if (!title) return alert("Judul case wajib diisi.");
  try {
    const r = await api("/v1/cases", { method: "POST", body: JSON.stringify({ title, description }) });
    document.getElementById("caseTitle").value = ""; document.getElementById("caseDesc").value = "";
    CURRENT_CASE = r.id;
    await loadCases(); openCase(r.id); refreshStats();
  } catch (e) { alert(e.message); }
}

async function openCase(id) {
  CURRENT_CASE = id;
  document.querySelectorAll("#caseList tr.clickable").forEach(tr =>
    tr.classList.toggle("selected", tr.dataset.id === id));
  const box = document.getElementById("caseDetail");
  box.innerHTML = `<p class="muted">Memuat…</p>`;
  try {
    const d = await api(`/v1/cases/${encodeURIComponent(id)}`);
    const c = d.case, open = c.status === "open";
    const canWrite = ["investigator", "case_manager", "admin"].includes(ROLE);
    let h = `<div class="kv">
      <div class="k">Judul</div><div><b>${esc(c.title)}</b></div>
      <div class="k">Status</div><div>${open ? '<span class="pill ok">open</span>' : '<span class="pill bad">closed</span>'}</div>
      <div class="k">Dibuat</div><div>${esc(new Date(c.created_at).toLocaleString())}</div>
      <div class="k">Deskripsi</div><div>${esc(c.description || "—")}</div>
    </div>`;
    if (d.can_manage) {
      h += `<button class="${open ? "danger" : "secondary"}" style="margin:12px 0 0;padding:5px 10px;" onclick="setCaseStatus('${esc(c.id)}','${open ? "close" : "reopen"}')">${open ? "Tutup case" : "Buka kembali"}</button>`;
    }

    // members
    h += `<h3>Anggota</h3><table><tr><th>User</th><th>Role</th><th></th></tr>` + d.members.map(m => `
      <tr><td>${esc(m.username)}</td><td><span class="pill warn">${esc(m.role)}</span></td>
      <td>${d.can_manage ? `<button class="secondary" ${btnSm} data-uid="${esc(m.id)}" onclick="removeMember('${esc(c.id)}', this.dataset.uid)">Keluarkan</button>` : ""}</td></tr>`).join("") + `</table>`;
    if (d.can_manage) {
      h += `<div class="row" style="max-width:420px;"><input id="caseMemberName" placeholder="username" />
        <button ${btnSm} onclick="addMember('${esc(c.id)}')">+ Tambah anggota</button></div>`;
    }

    // consents
    h += `<h3>Consent (${d.consents.length})</h3>`;
    h += !d.consents.length ? `<p class="muted">Belum ada consent di case ini.</p>` :
      `<table><tr><th>Subjek</th><th>Status</th><th>Titik</th></tr>` + d.consents.map(x => `
        <tr><td>${esc(x.subject_label)}${x.phone_e164 ? ` <span class="pill warn">${esc(x.phone_e164)}</span>` : ""}<br><span class="muted">${esc(x.purpose)}</span></td>
        <td>${x.status === "active" ? '<span class="pill ok">active</span>' : '<span class="pill bad">revoked</span>'}</td>
        <td>${esc(x.point_count)}</td></tr>`).join("") + `</table>`;
    if (open && canWrite) {
      h += `<div class="row" style="max-width:520px;"><select id="caseAttachConsent"></select>
        <button class="secondary" ${btnSm} onclick="attachConsent('${esc(c.id)}')">Tautkan consent</button></div>`;
    }

    // geofences
    const gfs = d.geofences || [];
    h += `<h3>Geofence case (${gfs.length})</h3>`;
    h += !gfs.length ? `<p class="muted">Belum ada geofence khusus case ini (buat di tab Map & Geofence).</p>` :
      `<table><tr><th>Nama</th><th>Tipe</th></tr>` + gfs.map(g => `
        <tr><td>${esc(g.name)}</td><td>${esc(g.kind)}${g.kind === "circle" && g.radius_m ? ` · ${esc(Math.round(g.radius_m))} m` : ""}</td></tr>`).join("") + `</table>`;

    // findings
    h += `<h3>Findings OSINT (${d.findings.length})</h3>`;
    h += !d.findings.length ? `<p class="muted">Belum ada finding. Pilih case ini di tab Lookup saat melakukan query.</p>` :
      `<table><tr><th>Waktu</th><th>Tipe</th><th>Sumber</th><th>Confidence</th></tr>` + d.findings.map(f => `
        <tr><td>${esc(new Date(f.created_at).toLocaleString())}</td><td>${esc(f.finding_type)}</td>
        <td>${esc(f.source)} <span class="muted">(${esc(f.source_kind)})</span></td>
        <td>${esc((Number(f.confidence) * 100).toFixed(0))}%</td></tr>`).join("") + `</table>`;

    box.innerHTML = h;
    if (open && canWrite) fillAttachConsent(c.id, d.consents.map(x => x.id));
  } catch (e) { box.innerHTML = `<p class="pill bad">${esc(e.message)}</p>`; }
}

async function fillAttachConsent(caseId, already) {
  const sel = document.getElementById("caseAttachConsent");
  if (!sel) return;
  try {
    const { consents } = await api("/v1/consents");
    const avail = consents.filter(x => !already.includes(x.id));
    sel.innerHTML = !avail.length ? `<option value="">(tidak ada consent lain)</option>` :
      avail.map(x => `<option value="${esc(x.id)}">${esc(x.subject_label)} (${esc(x.status)})${x.case_id ? " — di case lain" : ""}</option>`).join("");
  } catch (e) { sel.innerHTML = `<option value="">(gagal memuat)</option>`; }
}

async function attachConsent(caseId) {
  const consentId = document.getElementById("caseAttachConsent").value;
  if (!consentId) return;
  try {
    await api(`/v1/cases/${encodeURIComponent(caseId)}/consents/${encodeURIComponent(consentId)}`, { method: "POST" });
    openCase(caseId); loadCases();
  } catch (e) { alert(e.message); }
}

async function addMember(caseId) {
  const username = document.getElementById("caseMemberName").value.trim();
  if (!username) return;
  try {
    await api(`/v1/cases/${encodeURIComponent(caseId)}/members`, { method: "POST", body: JSON.stringify({ username }) });
    openCase(caseId); loadCases();
  } catch (e) { alert(e.message); }
}

async function removeMember(caseId, userId) {
  if (!confirm("Keluarkan anggota ini dari case?")) return;
  try {
    await api(`/v1/cases/${encodeURIComponent(caseId)}/members/${encodeURIComponent(userId)}`, { method: "DELETE" });
    openCase(caseId); loadCases();
  } catch (e) { alert(e.message); }
}

async function setCaseStatus(caseId, action) {
  if (action === "close" && !confirm("Tutup case? Data baru tidak bisa ditambahkan sampai dibuka kembali.")) return;
  try {
    await api(`/v1/cases/${encodeURIComponent(caseId)}/${action}`, { method: "POST" });
    openCase(caseId); loadCases(); refreshStats();
  } catch (e) { alert(e.message); }
}

// ---- audit ----
async function loadAudit() {
  const { events } = await api("/v1/audit");
  const box = document.getElementById("auditList");
  box.innerHTML = !events.length ? `<p class="muted">Belum ada event.</p>` :
    `<table><tr><th>Waktu</th><th>Actor</th><th>Action</th><th>Resource</th><th>Detail</th></tr>` +
    events.map(ev => `<tr><td>${new Date(ev.created_at).toLocaleString()}</td>
      <td>${esc(ev.actor)}</td><td><b>${esc(ev.action)}</b></td>
      <td>${esc(ev.resource_type)} ${esc(ev.resource_id)}</td><td>${esc(ev.detail)}</td></tr>`).join("") + `</table>`;
}

// ---- users ----
async function loadUsers() {
  const { users } = await api("/v1/users");
  document.getElementById("userList").innerHTML =
    `<table><tr><th>Username</th><th>Role</th><th>Status</th></tr>` +
    users.map(u => `<tr><td>${esc(u.username)}</td><td><span class="pill warn">${esc(u.role)}</span></td><td>${esc(u.status)}</td></tr>`).join("") + `</table>`;
}
async function createUser() {
  const username = document.getElementById("uName").value.trim();
  const password = document.getElementById("uPass").value;
  const role = document.getElementById("uRole").value;
  if (!username || password.length < 6) return alert("Username wajib, password min 6.");
  try {
    await api("/v1/users", { method: "POST", body: JSON.stringify({ username, password, role }) });
    document.getElementById("uName").value = ""; document.getElementById("uPass").value = "";
    loadUsers();
  } catch (e) { alert(e.message); }
}

// ---- map + geofence ----
let map, ptLayer, gfLayer, drawLayer;
let drawMode = null;      // null | 'circle' | 'polygon'
let polyVertices = [];    // [[lat,lng], ...]
let heatOn = false, heatLayer = null;   // density heatmap

function initMap() {
  if (!map) {
    map = L.map("map").setView([-6.2, 106.816], 11);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
      { attribution: "© OpenStreetMap", maxZoom: 19 }).addTo(map);
    ptLayer = L.layerGroup().addTo(map);
    gfLayer = L.layerGroup().addTo(map);
    drawLayer = L.layerGroup().addTo(map);
    map.on("click", onMapClick);
  }
}

function onKindChange() {
  const isCircle = document.getElementById("gfKind").value === "circle";
  document.getElementById("gfCircleOpts").classList.toggle("hidden", !isCircle);
  if (drawMode) cancelDrawing();
}

function startDrawing() {
  const name = document.getElementById("gfName").value.trim();
  if (!name) return alert("Isi nama geofence dulu.");
  if (!document.getElementById("gfCase").value && !isManager()) return alert("Pilih case untuk geofence ini.");
  drawMode = document.getElementById("gfKind").value;
  polyVertices = [];
  if (drawLayer) drawLayer.clearLayers();
  const btn = document.getElementById("gfStartBtn");
  if (drawMode === "circle") {
    btn.textContent = "👆 Klik peta untuk taruh pusat";
    document.getElementById("gfHint").textContent = "Klik satu titik di peta sebagai pusat lingkaran.";
  } else {
    btn.classList.add("hidden");
    document.getElementById("gfPolyControls").classList.remove("hidden");
    document.getElementById("gfHint").textContent = "Klik peta untuk menambah titik sudut (minimal 3), lalu Simpan.";
    updateVertexInfo();
  }
}

function resetDrawing() {
  drawMode = null;
  polyVertices = [];
  if (drawLayer) drawLayer.clearLayers();
  const btn = document.getElementById("gfStartBtn");
  btn.classList.remove("hidden");
  btn.textContent = "✏️ Mulai gambar di peta";
  document.getElementById("gfPolyControls").classList.add("hidden");
  document.getElementById("gfHint").textContent = 'Isi nama, pilih tipe, lalu klik "Mulai gambar".';
}

function cancelDrawing() { resetDrawing(); }

function updateVertexInfo() {
  document.getElementById("gfVertexInfo").textContent =
    `${polyVertices.length} titik${polyVertices.length >= 3 ? " — siap disimpan" : " (butuh ≥3)"}.`;
}

function redrawTempPolygon() {
  drawLayer.clearLayers();
  polyVertices.forEach((v, i) =>
    L.circleMarker(v, { radius: 5, color: "#ffb454", fillColor: "#ffb454", fillOpacity: 1 })
      .bindTooltip(String(i + 1)).addTo(drawLayer));
  if (polyVertices.length >= 2) {
    const shape = polyVertices.length >= 3
      ? L.polygon(polyVertices, { color: "#ffb454", weight: 2, dashArray: "5,5", fillOpacity: 0.1 })
      : L.polyline(polyVertices, { color: "#ffb454", weight: 2, dashArray: "5,5" });
    shape.addTo(drawLayer);
  }
}

async function onMapClick(e) {
  if (!drawMode) return;
  if (drawMode === "circle") {
    const name = document.getElementById("gfName").value.trim();
    const radius_m = parseFloat(document.getElementById("gfRadius").value) || 300;
    try {
      const case_id = document.getElementById("gfCase").value || null;
      await api("/v1/geofences", { method: "POST", body: JSON.stringify({
        name, kind: "circle", center_lat: e.latlng.lat, center_lng: e.latlng.lng, radius_m, case_id }) });
      document.getElementById("gfName").value = "";
      resetDrawing(); loadGeofences(); loadMap(); refreshStats();
    } catch (err) { alert(err.message); }
  } else if (drawMode === "polygon") {
    polyVertices.push([e.latlng.lat, e.latlng.lng]);
    redrawTempPolygon(); updateVertexInfo();
  }
}

function undoVertex() {
  if (!polyVertices.length) return;
  polyVertices.pop();
  redrawTempPolygon(); updateVertexInfo();
}

async function finishPolygon() {
  if (polyVertices.length < 3) return alert("Polygon butuh minimal 3 titik.");
  const name = document.getElementById("gfName").value.trim();
  try {
    const case_id = document.getElementById("gfCase").value || null;
    await api("/v1/geofences", { method: "POST", body: JSON.stringify({
      name, kind: "polygon", polygon: polyVertices, case_id }) });
    document.getElementById("gfName").value = "";
    resetDrawing(); loadGeofences(); loadMap(); refreshStats();
  } catch (err) { alert(err.message); }
}

async function loadGeofences() {
  const { geofences } = await api("/v1/geofences");
  const box = document.getElementById("gfList");
  box.innerHTML = !geofences.length ? `<p class="muted">Belum ada geofence.</p>` :
    geofences.map(g => `<div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid var(--border);">
      <span>${g.kind === "circle" ? "⭕" : "⬡"} ${esc(g.name)}<br><span class="muted">${g.kind === "circle" ? Math.round(g.radius_m) + " m" : (g.polygon || []).length + " titik"}</span>
        ${g.case_id ? `<span class="pill warn">${esc(g.case_title || g.case_id)}</span>` : `<span class="pill">bersama</span>`}</span>
      ${(g.case_id || isManager()) ? `<button class="danger" style="margin:0;padding:4px 9px;" data-id="${esc(g.id)}" onclick="delFence(this.dataset.id)">✕</button>` : ""}
    </div>`).join("");
  drawGeofences(geofences);
}
async function delFence(id) {
  if (!confirm("Hapus geofence ini?")) return;
  try { await api(`/v1/geofences/${encodeURIComponent(id)}`, { method: "DELETE" }); }
  catch (e) { return alert(e.message); }
  loadGeofences(); refreshStats();
}
function drawGeofences(geofences) {
  if (!gfLayer) return;
  gfLayer.clearLayers();
  geofences.forEach(g => {
    if (g.kind === "circle") {
      L.circle([g.center_lat, g.center_lng], { radius: g.radius_m,
        color: "#ffb454", weight: 2, fillColor: "#ffb454", fillOpacity: 0.08 })
        .bindPopup(`<b>${esc(g.name)}</b><br>${Math.round(g.radius_m)} m`).addTo(gfLayer);
    } else if (g.polygon) {
      L.polygon(g.polygon, { color: "#ffb454", weight: 2, fillOpacity: 0.08 })
        .bindPopup(`<b>${esc(g.name)}</b>`).addTo(gfLayer);
    }
  });
}

async function loadMap() {
  initMap();
  const cid = document.getElementById("mapConsent").value;
  const prec = document.getElementById("mapPrecision").value || "exact";
  const qs = `?precision=${prec}` + (cid ? `&consent_id=${cid}` : "");
  const { points } = await api("/v1/location/points" + qs);
  ptLayer.clearLayers();
  const latlngs = [];
  points.forEach(p => {
    latlngs.push([p.latitude, p.longitude]);
    L.circleMarker([p.latitude, p.longitude], { radius: 6, color: "#4f8cff", fillColor: "#4f8cff", fillOpacity: 0.75 })
      .bindPopup(`<b>${p.captured_at}</b><br>acc ${p.accuracy_m || "?"} m`).addTo(ptLayer);
  });
  if (latlngs.length > 1) L.polyline([...latlngs].reverse(), { color: "#35d0a5", weight: 2, opacity: .6 }).addTo(ptLayer);
  if (latlngs.length) map.fitBounds(latlngs, { padding: [40, 40], maxZoom: 15 });
  if (heatOn) renderHeatmap();
  loadGfEvents(); refreshStats();
}

// ---- density heatmap (PostGIS grid aggregation) ----
function toggleHeatmap() {
  heatOn = !heatOn;
  document.getElementById("heatBtn").textContent = `🔥 Heatmap: ${heatOn ? "on" : "off"}`;
  if (heatOn) { renderHeatmap(); }
  else if (heatLayer) { map.removeLayer(heatLayer); heatLayer = null; document.getElementById("heatInfo").textContent = ""; }
}

async function renderHeatmap() {
  initMap();
  const cid = document.getElementById("mapConsent").value;
  const cell = document.getElementById("heatCell").value || 250;
  const qs = `?cell_m=${cell}` + (cid ? `&consent_id=${cid}` : "");
  const data = await api("/v1/analytics/heatmap" + qs);
  if (heatLayer) { map.removeLayer(heatLayer); heatLayer = null; }
  const pts = data.cells.map(c => [c.lat, c.lng, c.weight]);
  heatLayer = L.heatLayer(pts, {
    radius: 28, blur: 20, max: data.max_weight || 1, minOpacity: 0.25,
    gradient: { 0.2: "#3b82f6", 0.4: "#22d3ee", 0.6: "#eab308", 0.8: "#f97316", 1.0: "#ef4444" },
  }).addTo(map);
  document.getElementById("heatInfo").textContent =
    `${data.count_cells} sel · maks ${data.max_weight} titik/sel · sel ${data.cell_m}m`;
}

async function loadGfEvents() {
  try {
    const { events } = await api("/v1/geofence/events");
    document.getElementById("gfEvents").innerHTML = !events.length ? `<p class="muted">Belum ada event.</p>` :
      `<table><tr><th>Waktu</th><th>Geofence</th><th>Event</th></tr>` +
      events.slice(0, 40).map(ev => `<tr><td>${new Date(ev.created_at).toLocaleString()}</td>
        <td>${esc(ev.geofence_name || ev.geofence_id)}</td>
        <td>${ev.event_type === "enter" ? '<span class="pill ok">ENTER</span>' : '<span class="pill warn">EXIT</span>'}</td></tr>`).join("") + `</table>`;
  } catch (e) {}
}

// ---- analytics ----
async function loadGeofencesInto(selId) {
  try {
    const { geofences } = await api("/v1/geofences");
    const sel = document.getElementById(selId);
    if (sel) sel.innerHTML = `<option value="">—</option>` +
      geofences.map(g => `<option value="${esc(g.id)}">${esc(g.name)} (${esc(g.kind)})</option>`).join("");
  } catch (e) {}
}

async function runAnalytics() {
  const cid = document.getElementById("anConsent").value;
  const gid = document.getElementById("anGeofence").value;
  if (!cid) return alert("Pilih consent dulu.");
  const stats = document.getElementById("anStats");
  const detail = document.getElementById("anDetail");
  const dwellBox = document.getElementById("anDwell");
  stats.innerHTML = ""; detail.innerHTML = `<p class="muted">Menghitung…</p>`;
  try {
    const [mv, area, dwell] = await Promise.all([
      api(`/v1/analytics/movement?consent_id=${cid}`),
      api(`/v1/analytics/activity-area?consent_id=${cid}`),
      api(`/v1/analytics/dwell?consent_id=${cid}`),
    ]);
    const inGeo = gid ? await api(`/v1/analytics/in-geofence?geofence_id=${gid}`) : null;
    const cells = [
      ["Titik", mv.points], ["Jarak (km)", mv.distance_km],
      ["Area (km²)", area.area_km2], ["Rentang (jam)", mv.span_hours],
      ["Akurasi ⌀ (m)", mv.avg_accuracy_m ?? "—"],
    ];
    if (inGeo) cells.push(["Dalam geofence", inGeo.count]);
    stats.innerHTML = cells.map(([l, n]) =>
      `<div class="stat"><div class="n">${n}</div><div class="l">${l}</div></div>`).join("");
    detail.innerHTML = `<div class="kv">
      <div class="k">First seen</div><div>${mv.first_seen ? new Date(mv.first_seen).toLocaleString() : "—"}</div>
      <div class="k">Last seen</div><div>${mv.last_seen ? new Date(mv.last_seen).toLocaleString() : "—"}</div>
      <div class="k">BBox lat</div><div>${mv.min_lat ?? "—"} … ${mv.max_lat ?? "—"}</div>
      <div class="k">BBox lng</div><div>${mv.min_lng ?? "—"} … ${mv.max_lng ?? "—"}</div>
      <div class="k">Activity hull</div><div>${area.hull ? area.hull.type : "—"} · ${area.points} titik</div>
    </div>`;
    dwellBox.innerHTML = dwell.dwell.length
      ? `<table><tr><th>Geofence</th><th>Kunjungan</th><th>Dwell (menit)</th></tr>` +
        dwell.dwell.map(d => `<tr><td>${esc(d.geofence_name || d.geofence_id)}</td><td>${esc(d.visits)}</td><td>${esc(d.dwell_minutes)}</td></tr>`).join("") + `</table>`
      : `<p class="muted">Belum ada event enter/exit untuk consent ini.</p>`;
  } catch (e) { detail.innerHTML = `<p class="pill bad">${e.message}</p>`; stats.innerHTML = ""; }
}

function exportCase(kind) {
  const cid = document.getElementById("anConsent").value;
  if (!cid) return alert("Pilih consent dulu (dropdown Consent di atas).");
  const map = {
    pdf: [`/v1/reports/consent/${cid}/report.pdf`, `case_report_${cid}.pdf`],
    points: [`/v1/reports/consent/${cid}/points.csv`, `points_${cid}.csv`],
    events: [`/v1/reports/consent/${cid}/events.csv`, `events_${cid}.csv`],
  };
  downloadFile(...map[kind]);
}

// ---- retention (admin) ----
async function retentionPreview() {
  const d = await api("/v1/retention/preview");
  document.getElementById("retentionInfo").innerHTML =
    `<p><b>${d.total_expired}</b> titik melewati retention.</p>` +
    (d.by_consent.length ? `<table><tr><th>Consent</th><th>Retention</th><th>Total</th><th>Kedaluwarsa</th></tr>` +
      d.by_consent.map(c => `<tr><td>${esc(c.subject_label)}</td><td>${esc(c.retention_days)}d</td><td>${c.total}</td><td>${c.expired}</td></tr>`).join("") + `</table>` : "");
}
async function retentionRun() {
  if (!confirm("Hapus permanen semua titik GPS yang melewati retention?")) return;
  const d = await api("/v1/retention/run", { method: "POST" });
  document.getElementById("retentionInfo").innerHTML =
    `<p class="pill ok">Purge selesai — ${d.deleted} titik dihapus.</p>`;
  refreshStats();
}

// ---- timeline / playback ----
let tlmap, tlGfLayer, tlBaseLayer, tlTrail, tlMarker;
let tlPoints = [], tlIdx = 0, tlTimer = null;

function initTlMap() {
  if (!tlmap) {
    tlmap = L.map("tlmap").setView([-6.2, 106.816], 11);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
      { attribution: "© OpenStreetMap", maxZoom: 19 }).addTo(tlmap);
    tlGfLayer = L.layerGroup().addTo(tlmap);
    tlBaseLayer = L.layerGroup().addTo(tlmap);
    tlTrail = L.polyline([], { color: "#35d0a5", weight: 4, opacity: 0.9 }).addTo(tlmap);
  }
  setTimeout(() => tlmap.invalidateSize(), 120);
}

async function loadTrack() {
  initTlMap();
  const cid = document.getElementById("tlConsent").value;
  if (!cid) return alert("Pilih consent dulu.");
  const prec = document.getElementById("tlPrecision").value || "exact";
  stopPlay();
  const { points } = await api(`/v1/location/points?consent_id=${cid}&order=asc&limit=2000&precision=${prec}`);
  tlPoints = points || [];
  tlBaseLayer.clearLayers(); tlGfLayer.clearLayers(); tlTrail.setLatLngs([]);
  if (tlMarker) { tlmap.removeLayer(tlMarker); tlMarker = null; }

  // geofences for context
  try {
    const { geofences } = await api("/v1/geofences");
    geofences.forEach(g => {
      if (g.kind === "circle") L.circle([g.center_lat, g.center_lng],
        { radius: g.radius_m, color: "#ffb454", weight: 1, fillOpacity: 0.05 }).addTo(tlGfLayer);
      else if (g.polygon) L.polygon(g.polygon,
        { color: "#ffb454", weight: 1, fillOpacity: 0.05 }).addTo(tlGfLayer);
    });
  } catch (e) {}

  if (!tlPoints.length) {
    document.getElementById("tlInfo").textContent = "Consent ini belum punya titik lokasi.";
    document.getElementById("tlSlider").max = 0;
    return;
  }
  const latlngs = tlPoints.map(p => [p.latitude, p.longitude]);
  L.polyline(latlngs, { color: "#3a4660", weight: 2, dashArray: "4,4" }).addTo(tlBaseLayer);
  latlngs.forEach(ll => L.circleMarker(ll, { radius: 3, color: "#4f8cff", fillOpacity: 0.5 }).addTo(tlBaseLayer));
  tlmap.fitBounds(latlngs, { padding: [40, 40], maxZoom: 15 });
  tlMarker = L.circleMarker(latlngs[0],
    { radius: 9, color: "#fff", fillColor: "#35d0a5", fillOpacity: 1, weight: 2 }).addTo(tlmap);
  document.getElementById("tlSlider").max = tlPoints.length - 1;
  setFrame(0);
}

function setFrame(i) {
  tlIdx = Math.max(0, Math.min(i, tlPoints.length - 1));
  const p = tlPoints[tlIdx];
  const ll = [p.latitude, p.longitude];
  if (tlMarker) tlMarker.setLatLng(ll);
  tlTrail.setLatLngs(tlPoints.slice(0, tlIdx + 1).map(x => [x.latitude, x.longitude]));
  document.getElementById("tlSlider").value = tlIdx;
  document.getElementById("tlInfo").innerHTML =
    `Titik <b>${tlIdx + 1}/${tlPoints.length}</b> · ${new Date(p.captured_at).toLocaleString()} · ` +
    `${p.latitude.toFixed(5)}, ${p.longitude.toFixed(5)}` +
    (p.accuracy_m != null ? ` · ±${Math.round(p.accuracy_m)}m` : "");
}

function onScrub() { stopPlay(); setFrame(parseInt(document.getElementById("tlSlider").value)); }
function tlReset() { stopPlay(); if (tlPoints.length) setFrame(0); }
function togglePlay() { tlTimer ? stopPlay() : startPlay(); }

function startPlay() {
  if (!tlPoints.length) return;
  if (tlIdx >= tlPoints.length - 1) setFrame(0);
  document.getElementById("tlPlayBtn").textContent = "⏸ Pause";
  const speed = parseInt(document.getElementById("tlSpeed").value) || 5;
  tlTimer = setInterval(() => {
    if (tlIdx >= tlPoints.length - 1) { stopPlay(); return; }
    setFrame(tlIdx + 1);
  }, 600 / speed);
}

function stopPlay() {
  if (tlTimer) clearInterval(tlTimer);
  tlTimer = null;
  const b = document.getElementById("tlPlayBtn");
  if (b) b.textContent = "▶ Play";
}

// ---- graph ----
let network;
const GROUP_COLORS = {
  consent: "#35d0a5", session: "#8aa0c8", geofence: "#ffb454",
};
async function loadGraph() {
  const data = await api("/v1/graph");
  const nodes = data.nodes.map(n => {
    let color = "#4f8cff";
    if (n.group === "consent") color = GROUP_COLORS.consent;
    else if (n.group === "session") color = GROUP_COLORS.session;
    else if (n.group === "geofence") color = GROUP_COLORS.geofence;
    else if (n.group.startsWith("id:")) color = "#c58cff";
    return { ...n, color: { background: color, border: "#1a2233" },
             font: { color: "#e6ebf5", multi: true }, shape: "dot", size: 16 };
  });
  const container = document.getElementById("graph");
  network = new vis.Network(container, {
    nodes: new vis.DataSet(nodes),
    edges: new vis.DataSet(data.edges.map(e => ({ ...e,
      color: { color: "#3a4660" }, font: { color: "#93a1bd", size: 10 },
      arrows: "to", smooth: { type: "dynamic" } }))),
  }, {
    physics: { stabilization: true, barnesHut: { springLength: 130 } },
    interaction: { hover: true },
  });
}

// ---- boot ----
if (TOKEN && ROLE) startApp();
else document.getElementById("loginOverlay").classList.remove("hidden");
