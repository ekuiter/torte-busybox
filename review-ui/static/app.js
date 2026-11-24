const state = { candidates: [], commits: [], filtered: [], selected: null, fullDiff: false, currentDiff: "" };
const $ = selector => document.querySelector(selector);
const escapeHtml = (value = "") => String(value).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
const pretty = value => value.replaceAll("_", " ");

async function api(url, options = {}) {
  const response = await fetch(url, {headers: {"Content-Type": "application/json"}, ...options});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

function showToast(message, error = false) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.style.color = error ? "var(--red)" : "var(--lime)";
  toast.classList.add("show");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove("show"), 4200);
}

function commitDecision(candidates) {
  const decisions = new Set(candidates.map(item => item.decision));
  return decisions.size === 1 ? candidates[0].decision : "mixed";
}

function aggregateCommits(candidates) {
  const grouped = new Map();
  for (const candidate of candidates) {
    if (!grouped.has(candidate.commit)) grouped.set(candidate.commit, []);
    grouped.get(candidate.commit).push(candidate);
  }
  return [...grouped.entries()].map(([commit, items]) => {
    const releasePairs = new Map();
    for (const item of items) for (const pair of item.release_pairs) {
      const key = `${pair.left}|${pair.right}`;
      if (!releasePairs.has(key)) releasePairs.set(key, {left: pair.left, right: pair.right, extractors: new Set()});
      pair.extractors.forEach(extractor => releasePairs.get(key).extractors.add(extractor));
    }
    const notes = [...new Set(items.map(item => item.note).filter(Boolean))];
    return {
      commit, parent: items[0].parent, subject: items[0].subject,
      author_date: items[0].author_date, summary: items[0].summary,
      candidates: items, decision: commitDecision(items), note: notes.length === 1 ? notes[0] : "",
      confidences: [...new Set(items.map(item => item.confidence))],
      kinds: [...new Set(items.map(item => item.kind))],
      release_pairs: [...releasePairs.values()].map(pair => ({...pair, extractors: [...pair.extractors]})),
    };
  });
}

function updateStats() {
  const reviewed = state.commits.filter(commit => !["pending", "mixed"].includes(commit.decision)).length;
  const accepted = state.commits.filter(commit => commit.decision === "accepted").length;
  const mappings = state.commits.reduce((sum, commit) => sum + commit.candidates.length, 0);
  $("#progress").textContent = `${reviewed}/${state.commits.length} commits reviewed · ${accepted} accepted · ${mappings} mappings`;
}

function applyFilters() {
  const query = $("#search").value.trim().toLowerCase();
  const decision = $("#decision-filter").value;
  const confidence = $("#confidence-filter").value;
  const kind = $("#kind-filter").value;
  const applicable = $("#release-filter").checked;
  const hasNote = $("#note-filter").checked;
  state.filtered = state.commits.filter(commit => {
    const haystack = [commit.commit, commit.subject, commit.summary, commit.note,
      ...commit.candidates.flatMap(item => [item.mapping, item.rationale, item.manual_check, ...(item.evidence || [])])].join(" ").toLowerCase();
    return (!query || haystack.includes(query)) && (!decision || commit.decision === decision) &&
      (!confidence || commit.confidences.includes(confidence)) && (!kind || commit.kinds.includes(kind)) &&
      (!applicable || commit.release_pairs.length) && (!hasNote || commit.note.trim());
  });
  renderList();
}

function renderList() {
  const mappings = state.filtered.reduce((sum, commit) => sum + commit.candidates.length, 0);
  $("#result-count").textContent = `${state.filtered.length} commits · ${mappings} mappings shown`;
  $("#candidate-list").innerHTML = state.filtered.map(commit => {
    const pair = commit.release_pairs[0];
    return `<button class="candidate-row ${state.selected?.commit === commit.commit ? "selected" : ""}" data-commit="${commit.commit}" role="option">
      <div class="row-top"><span class="badge ${commit.decision}">${pretty(commit.decision)}</span><span class="badge">${commit.candidates.length} mapping${commit.candidates.length === 1 ? "" : "s"}</span></div>
      <div class="commit-subject">${escapeHtml(commit.subject || "No commit subject")}</div>
      <div class="row-bottom"><span class="commit">${commit.commit.slice(0, 10)}</span><span class="pair">${pair ? `${pair.left} → ${pair.right}` : "no release pair"}</span></div>
    </button>`;
  }).join("");
  document.querySelectorAll(".candidate-row").forEach(row => row.addEventListener("click", () => selectCommit(row.dataset.commit)));
}

function decisionButtons(commit) {
  return ["accepted", "rejected", "uncertain", "pending"].map(decision =>
    `<button class="${decision} ${commit.decision === decision ? "active" : ""}" data-decision="${decision}">${decision === "accepted" ? "A · Accept commit" : decision === "rejected" ? "R · Reject commit" : decision === "uncertain" ? "U · Unsure" : "P · Pending"}</button>`
  ).join("");
}

async function selectCommit(hash) {
  state.selected = state.commits.find(commit => commit.commit === hash);
  state.fullDiff = false;
  renderList();
  renderDetail("Loading focused commit diff…");
  try {
    const result = await api(`/api/commit-diff?commit=${encodeURIComponent(hash)}`);
    renderDetail(result.diff);
  } catch (error) { renderDetail(`Could not load diff: ${error.message}`); }
}

function uniqueText(items, field) {
  const grouped = new Map();
  for (const item of items) {
    const text = field === "evidence" ? (item.evidence || []).join("\n") : item[field] || "";
    if (!text) continue;
    if (!grouped.has(text)) grouped.set(text, []);
    grouped.get(text).push(item.mapping);
  }
  return [...grouped.entries()];
}

function textGroups(groups, title) {
  return `<article class="card"><h3>${title}</h3>${groups.map(([text, mappings]) =>
    `<div class="reason"><p>${escapeHtml(text)}</p>${mappings.length > 1 ? `<span>${mappings.length} mappings share this text</span>` : `<code>${escapeHtml(mappings[0])}</code>`}</div>`
  ).join("")}</article>`;
}

function mappingTable(commit) {
  return `<details class="mapping-details" open><summary>${commit.candidates.length} proposed mapping${commit.candidates.length === 1 ? "" : "s"}</summary>
    <div class="mapping-table">${commit.candidates.map(item => `<div class="mapping-entry"><code>${escapeHtml(item.mapping)}</code><span class="badge">${item.kind}</span><span class="badge">${pretty(item.confidence)} · ${item.confidence_score}</span></div>`).join("")}</div></details>`;
}

function highlightDiff(diff, commit) {
  let escaped = escapeHtml(diff);
  const symbols = [...new Set(commit.candidates.flatMap(item => [...item.old_features, ...item.new_features]))].sort((a, b) => b.length - a.length);
  for (const symbol of symbols) escaped = escaped.replaceAll(escapeHtml(symbol), `<mark>${escapeHtml(symbol)}</mark>`);
  return escaped.split("\n").map(line => {
    let lineClass = "diff-line";
    if (line.startsWith("+") && !line.startsWith("+++")) lineClass += " diff-added";
    if (line.startsWith("-") && !line.startsWith("---")) lineClass += " diff-removed";
    if (line.startsWith("@@")) lineClass += " diff-hunk";
    return `<span class="${lineClass}">${line || " "}</span>`;
  }).join("");
}

function renderDetail(diff) {
  const commit = state.selected;
  if (!commit) return;
  state.currentDiff = diff;
  const pairs = commit.release_pairs.length
    ? commit.release_pairs.map(pair => `<span class="release-chip">${pair.left} → ${pair.right} · ${pair.extractors.length ? pair.extractors.map(pretty).join(" + ") : "not present in either extractor formula"}</span>`).join("")
    : `<span class="release-chip none">No candidate is exclusive to a staged consecutive release pair</span>`;
  $("#detail").classList.remove("empty");
  $("#detail").innerHTML = `<div class="detail-inner">
    <div class="detail-head"><div><span class="badge">commit review</span><h2>${escapeHtml(commit.subject || commit.commit)}</h2><div class="meta">${commit.commit} · parent ${commit.parent || "none"} · ${escapeHtml(commit.author_date)}</div></div><div class="decision-buttons">${decisionButtons(commit)}</div></div>
    <div class="release-strip">${pairs}</div>
    <article class="commit-summary"><h3>Model summary</h3><p>${escapeHtml(commit.summary)}</p></article>
    ${mappingTable(commit)}
    <div class="evidence-grid">${textGroups(uniqueText(commit.candidates, "rationale"), "Rationales")}${textGroups(uniqueText(commit.candidates, "manual_check"), "Manual checks")}
      <article class="card wide"><h3>Commit review note</h3><textarea id="note" placeholder="Why should this commit's mappings be accepted or rejected?">${escapeHtml(commit.note || "")}</textarea><div id="note-status" class="note-status">Saved automatically</div></article>
    </div>
    <div class="diff-head"><h3>${state.fullDiff ? "Full commit diff" : "Focused commit diff"}</h3><button id="toggle-diff">${state.fullDiff ? "Show mapping-related hunks" : "Show full diff"}</button></div>
    <pre>${highlightDiff(diff, commit)}</pre>
  </div>`;
  document.querySelectorAll("[data-decision]").forEach(button => button.addEventListener("click", () => saveCommitReview(button.dataset.decision)));
  $("#note").addEventListener("input", scheduleNoteSave);
  $("#toggle-diff").addEventListener("click", toggleDiff);
}

async function saveCommitReview(decision = state.selected.decision, {render = true} = {}) {
  const commit = state.selected;
  const normalized = decision === "mixed" ? "uncertain" : decision;
  const note = $("#note")?.value ?? commit.note ?? "";
  try {
    await api("/api/reviews/bulk", {method: "POST", body: JSON.stringify({commit: commit.commit, decision: normalized, note, only_pending: false})});
    commit.decision = normalized; commit.note = note;
    for (const candidate of commit.candidates) { candidate.decision = normalized; candidate.note = note; }
    updateStats();
    if (render) renderDetail(state.currentDiff);
    applyFilters();
    return true;
  } catch (error) { showToast(error.message, true); }
  return false;
}

function scheduleNoteSave() {
  $("#note-status").textContent = "Saving…";
  clearTimeout(scheduleNoteSave.timer);
  scheduleNoteSave.timer = setTimeout(async () => {
    const saved = await saveCommitReview(state.selected.decision, {render: false});
    if (saved && $("#note-status")) $("#note-status").textContent = "Saved";
  }, 500);
}

async function toggleDiff() {
  state.fullDiff = !state.fullDiff;
  const result = await api(`/api/commit-diff?commit=${encodeURIComponent(state.selected.commit)}&full=${state.fullDiff ? 1 : 0}`);
  renderDetail(result.diff);
}

async function exportMaps() {
  try {
    const result = await api("/api/export", {method: "POST", body: "{}"});
    showToast(`Exported ${result.mappings} mappings for ${result.release_pairs} extractor/release comparisons to ${result.archive}`);
  } catch (error) { showToast(error.message, true); }
}

function moveSelection(offset) {
  if (!state.filtered.length) return;
  const current = state.filtered.findIndex(commit => commit.commit === state.selected?.commit);
  const next = Math.min(state.filtered.length - 1, Math.max(0, (current < 0 ? 0 : current) + offset));
  selectCommit(state.filtered[next].commit);
  document.querySelector(`[data-commit="${state.filtered[next].commit}"]`)?.scrollIntoView({block: "nearest"});
}

async function init() {
  try {
    const data = await api("/api/candidates");
    state.candidates = data.candidates; state.commits = aggregateCommits(data.candidates);
    updateStats(); applyFilters(); if (state.filtered.length) selectCommit(state.filtered[0].commit);
  } catch (error) { showToast(error.message, true); }
}

["#search", "#decision-filter", "#confidence-filter", "#kind-filter", "#release-filter", "#note-filter"].forEach(selector => $(selector).addEventListener("input", applyFilters));
$("#export").addEventListener("click", exportMaps);
document.addEventListener("keydown", event => {
  if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) return;
  if (event.key === "j" || event.key === "ArrowDown") moveSelection(1);
  if (event.key === "k" || event.key === "ArrowUp") moveSelection(-1);
  if (event.key.toLowerCase() === "a") saveCommitReview("accepted");
  if (event.key.toLowerCase() === "r") saveCommitReview("rejected");
  if (event.key.toLowerCase() === "u") saveCommitReview("uncertain");
  if (event.key.toLowerCase() === "p") saveCommitReview("pending");
});
init();
