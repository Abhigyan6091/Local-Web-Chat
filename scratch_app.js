"use strict";

const $ = (s) => document.querySelector(s);
const fmt = (v, d = 0) => (v === null || v === undefined) ? "–" : Number(v).toFixed(d);

function ago(ts) {
  if (!ts) return "–";
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return s + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
}

function dur(s) {
  s = Math.max(0, Math.round(s));
  const m = Math.floor(s / 60), r = s % 60;
  return m ? (r ? `${m}m ${r}s` : `${m}m`) : `${r}s`;
}

function badgeHtml(row) {
  let out = "";
  const badges = row.badges || [];
  const status = row.status;
  if (status === "RUNNING" || status === "QUEUED") {
    out += ` <span class="badge running">${status.toLowerCase()}</span>`;
  }
  for (const b of badges) out += ` <span class="badge ${b}">${b}</span>`;
  if (row.feed_percent !== null && row.feed_percent !== undefined) {
    out += ` <span class="badge feed">feed ${fmt(row.delivered)}/${fmt(row.accepted_msgs)} (${fmt(row.feed_percent, 1)}%)</span>`;
  } else if (status === 'DONE') {
    const label = row.integrity_status === 'no_messages' ? 'no accepted messages' : 'feed unavailable';
    out += ` <span class="badge incomplete" title="${escapeHtml(row.integrity_note || '')}">${label}</span>`;
  }
  if (row.correctness !== null && row.correctness !== undefined) {
    const style = row.correctness === 1 ? 'correct' : 'incomplete';
    out += ` <span class="badge ${style}">correct ${fmt(row.correctness_passed)}/${fmt(row.correctness_checked)} (${fmt(row.correctness * 100, 1)}%)</span>`;
  } else if (row.integrity_status === 'legacy') {
    out += ` <span class="badge incomplete" title="${escapeHtml(row.integrity_note || '')}">correctness: rerun required</span>`;
  }
  return out;
}

function renderQueue(q) {
  const el = $("#queue-strip");
  const parts = [];
  if (q.running) {
    parts.push(`running: ${q.running.roll_id} (${dur(q.running.elapsed)} of ~${dur(q.running.estimated_total)})`);
  } else {
    parts.push("idle");
  }
  parts.push(`${q.queued} queued`);
  el.textContent = parts.join(" · ");
}

function renderMarks(rows) {
  const tbody = document.querySelector('table[data-board="marks"] tbody');
  if (!tbody) return;
  if (!rows || !rows.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty">No submissions yet.</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map((r) => {
    const rank = (v) => (v === null || v === undefined) ? "–" : v;
    return `<tr>
      <td>${escapeHtml(r.name || "")}</td>
      <td>${escapeHtml(r.roll_id)}</td>
      <td class="num">${rank(r.static_rank)}</td>
      <td class="num">${rank(r.breakpoint_rank)}</td>
      <td class="num">${r.mean_rank ?? "–"}</td>
      <td class="num"><strong>${r.marks ?? "–"}</strong></td>
      <td class="num">${fmt(r.run_count)}</td>
    </tr>`;
  }).join("");
}

function renderBoard(board, rows) {
  const tbody = document.querySelector(`table[data-board="${board}"] tbody`);
  if (!tbody) return;
  if (!rows.length) {
    const cols = board === "breakpoint" ? 11 : 10;
    tbody.innerHTML = `<tr><td colspan="${cols}" class="empty">No submissions yet.</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map((r) => {
    const done = r.status === "DONE";
    const reason = r.invalid_reason
      ? `<div class="reason">${escapeHtml(String(r.invalid_reason).slice(0, 160))}</div>` : "";
    const progress = (r.status === "RUNNING" && r.progress)
      ? `<div class="dim">${escapeHtml(r.progress)}</div>` : "";
    const cd = r.cooldown_remaining > 0
      ? `<span class="dim"> (next in ${dur(r.cooldown_remaining)})</span>` : "";
    const d = (v, g = 0) => (done ? fmt(v, g) : "–");

    // The two boards lead with different metrics: static ranks on mean
    // response time (lower is better), breakpoint on requests served before
    // it broke.
    const metrics = board === "breakpoint"
      ? `<td class="num"><strong>${d(r.total_successes)}</strong></td>
         <td class="num">${done ? (r.break_concurrency ? fmt(r.break_concurrency) + " users" : "held") : "–"}</td>
         <td class="num">${d(r.peak_rps, 1)}</td>
         <td class="num">${d(r.mean_response_ms, 0)}</td>`
      : `<td class="num"><strong>${d(r.mean_response_ms, 0)}</strong></td>
         <td class="num">${done && r.err_rate_overall !== null ? fmt(r.err_rate_overall * 100, 2) : "–"}</td>
         <td class="num">${d(r.total_requests)}${done && r.total_expected ? ` / ${fmt(r.total_expected)}` : ""}</td>`;

    return `<tr>
      <td class="rank">${r.rank ?? "–"}</td>
      <td>${escapeHtml(r.name)}${badgeHtml(r)}${reason}${progress}</td>
      <td>${escapeHtml(r.roll_id)}</td>
      ${metrics}
      <td class="num">${done && r.completeness !== null ? fmt(r.completeness * 100, 2) : "–"}</td>
      <td class="num">${fmt(r.run_count)}</td>
      <td class="dim">${ago(r.last_run_at)}${cd}</td>
      <td><a href="/run/${r.run_id}">detail</a></td>
    </tr>`;
  }).join("");
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function refresh() {
  try {
    const res = await fetch("/api/leaderboard");
    const data = await res.json();
    renderQueue(data.queue);
    for (const [board, rows] of Object.entries(data.boards)) renderBoard(board, rows);
    renderMarks(data.marks);
  } catch (e) {
    /* transient; next poll will retry */
  }
}

// ---- roster autofill: debounced lookup as the roll ID is typed ----
(function setupRoster() {
  const form = $("#submit-form");
  if (!form || form.dataset.roster !== "on") return;
  const rollInput = form.roll_id;
  const nameInput = form.name;
  const btn = form.querySelector("button");
  const msg = $("#submit-msg");
  let timer = null;
  let matched = false;

  function setState(state, text) {
    msg.className = "msg" + (state === "err" ? " err" : state === "ok" ? " ok" : "");
    msg.textContent = text || "";
  }

  async function lookup() {
    const roll = rollInput.value.trim();
    matched = false;
    btn.disabled = true;
    nameInput.value = "";
    if (!roll) { setState("", ""); return; }
    setState("", "checking roll number…");
    try {
      const res = await fetch(`/api/roster/${encodeURIComponent(roll)}`);
      const data = await res.json();
      if (res.ok && data.found) {
        nameInput.value = data.name;
        matched = true;
        btn.disabled = false;
        setState("ok", `${data.name}`);
      } else {
        setState("err", `'${roll}' is not on the class roster`);
      }
    } catch (e) {
      setState("err", "could not reach the leaderboard server");
    }
  }

  rollInput.addEventListener("input", () => {
    clearTimeout(timer);
    matched = false;
    btn.disabled = true;
    timer = setTimeout(lookup, 350);
  });
  rollInput.addEventListener("blur", () => { clearTimeout(timer); lookup(); });
})();

$("#submit-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = ev.target;
  const btn = form.querySelector("button");
  const msg = $("#submit-msg");
  const body = {
    name: form.name.value.trim(),
    roll_id: form.roll_id.value.trim(),
    url: form.url.value.trim(),
  };
  btn.disabled = true;
  msg.className = "msg";
  msg.textContent = "checking your endpoint…";
  try {
    const res = await fetch("/api/submit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      msg.className = "msg err";
      msg.textContent = data.error || `submission failed (HTTP ${res.status})`;
    } else {
      msg.className = "msg ok";
      const pos = data.position === 0 ? "starting now" : `queue position ${data.position}`;
      let text = `Accepted (${data.payload_mode}). ${pos}, starting in about ${dur(data.eta_seconds)}. Both boards will be tested.`;
      if (data.warning) text += ` Note: ${data.warning}.`;
      msg.innerHTML = escapeHtml(text) + ` <a href="/run/${data.run_id}">follow this run</a>`;
    }
  } catch (e) {
    msg.className = "msg err";
    msg.textContent = "could not reach the leaderboard server";
  } finally {
    btn.disabled = false;
    refresh();
  }
});

refresh();
setInterval(refresh, 3000);
