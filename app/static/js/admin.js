'use strict';

const API = '/api';
let token = sessionStorage.getItem('admin_token') || '';
let currentTeamKey = '';
let currentRoster = [];

// ── Auth ──────────────────────────────────────────────────────────────────

async function apiPost(path, body) {
  const res = await fetch(API + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

async function apiGet(path) {
  const res = await fetch(API + path, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

async function apiDelete(path) {
  const res = await fetch(API + path, {
    method: 'DELETE',
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

function showMsg(id, text, isError = false) {
  const el = document.getElementById(id);
  el.className = 'msg ' + (isError ? 'error' : 'success');
  el.textContent = text;
  setTimeout(() => { el.textContent = ''; el.className = ''; }, 6000);
}

document.getElementById('login-btn').addEventListener('click', async () => {
  const pw = document.getElementById('login-pw').value;
  try {
    const res = await fetch(API + '/admin/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: pw }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Login failed');
    token = data.access_token;
    sessionStorage.setItem('admin_token', token);
    showAdminContent();
  } catch (e) {
    const el = document.getElementById('login-msg');
    el.className = 'msg error';
    el.textContent = e.message;
  }
});

document.getElementById('login-pw').addEventListener('keydown', e => {
  if (e.key === 'Enter') document.getElementById('login-btn').click();
});

document.getElementById('logout-btn').addEventListener('click', () => {
  token = '';
  sessionStorage.removeItem('admin_token');
  document.getElementById('login-screen').style.display = 'flex';
  document.getElementById('admin-content').style.display = 'none';
  document.getElementById('logout-btn').style.display = 'none';
});

function showAdminContent() {
  document.getElementById('login-screen').style.display = 'none';
  document.getElementById('admin-content').style.display = 'block';
  document.getElementById('logout-btn').style.display = 'inline-block';
  loadStatus();
  loadTeams();
  loadOverrides();
  initDraftOrder();
  loadTrades();
  loadHistoricalLeagues();
}

// If token already exists, try to auto-login
if (token) {
  apiGet('/admin/status')
    .then(() => showAdminContent())
    .catch(() => { token = ''; sessionStorage.removeItem('admin_token'); });
}

// ── Status ────────────────────────────────────────────────────────────────

async function loadStatus() {
  try {
    const s = await apiGet('/admin/status');
    const grid = document.getElementById('status-grid');
    const items = [
      { k: 'Yahoo Auth', v: s.is_authorized ? '✓ Authorized' : '✗ Not authorized', ok: s.is_authorized },
      { k: 'ETL Status', v: s.etl_status, ok: s.etl_status === 'complete' },
      { k: 'ETL Completed', v: s.etl_completed_at ? new Date(s.etl_completed_at).toLocaleString() : '—' },
      { k: 'Last Updated', v: s.last_updated ? new Date(s.last_updated).toLocaleString() : '—' },
      { k: 'Previous Season', v: s.previous_season },
      { k: 'Upcoming Season', v: s.upcoming_season },
      { k: 'League ID', v: s.league_id },
      { k: 'ETL Error', v: s.etl_error || '—', ok: !s.etl_error || s.etl_error === '' },
    ];
    grid.innerHTML = items.map(item => `
      <div class="status-item">
        <div class="status-key">${item.k}</div>
        <div class="status-val" style="color:${item.ok === false ? 'var(--ineligible)' : item.ok === true ? 'var(--keeper)' : 'var(--text)'}">${item.v}</div>
      </div>`).join('');
  } catch (e) {
    document.getElementById('status-grid').innerHTML =
      `<div class="msg error" style="grid-column:1/-1">Failed to load status: ${e.message}</div>`;
  }
}

document.getElementById('run-etl-btn').addEventListener('click', async () => {
  if (!confirm('Run full initial ETL? This may take several minutes and will make many API calls.')) return;
  document.getElementById('etl-msg').className = 'msg';
  document.getElementById('etl-msg').textContent = 'ETL running… this may take a few minutes';
  try {
    const result = await apiPost('/admin/etl/run', {});
    showMsg('etl-msg', `ETL ${result.status}: ${JSON.stringify(result.results || result.error)}`);
    loadStatus();
  } catch (e) {
    showMsg('etl-msg', e.message, true);
  }
});

document.getElementById('recalc-btn').addEventListener('click', async () => {
  try {
    await apiPost('/admin/etl/recalculate-keepers', {});
    showMsg('etl-msg', 'Keeper eligibility recalculated successfully');
    loadStatus();
  } catch (e) {
    showMsg('etl-msg', e.message, true);
  }
});

document.getElementById('import-pick-trades-btn').addEventListener('click', async () => {
  try {
    const result = await apiPost('/admin/etl/import-pick-trades', {});
    showMsg('etl-msg', `Pick trades imported: ${result.picks_imported} picks`);
    loadTrades();
  } catch (e) {
    showMsg('etl-msg', e.message, true);
  }
});

// ── Historical League IDs ─────────────────────────────────────────────────

async function loadHistoricalLeagues() {
  try {
    const entries = await apiGet('/admin/historical-leagues');
    const container = document.getElementById('hist-list');
    if (!entries.length) {
      container.innerHTML = '<div style="color:var(--text-dim);font-size:0.82rem">No historical league IDs saved yet.</div>';
      return;
    }
    container.innerHTML = entries.map(e => `
      <div class="trade-row">
        <span style="font-weight:600;min-width:60px">${e.season}</span>
        <span style="flex:1;color:var(--text-muted);font-family:monospace">${e.league_id}</span>
        <button class="btn btn-primary" style="font-size:0.78rem;padding:4px 10px;background:rgba(99,102,241,0.6)"
          onclick="fetchSeason(${e.season}, this)">Fetch Season</button>
        <button class="tag-remove" onclick="deleteHistLeague(${e.season})">Remove</button>
      </div>`).join('');
  } catch (e) {
    document.getElementById('hist-list').innerHTML =
      `<div class="msg error">${e.message}</div>`;
  }
}

async function deleteHistLeague(season) {
  if (!confirm(`Remove league ID for ${season}?`)) return;
  try {
    await apiDelete(`/admin/historical-leagues/${season}`);
    loadHistoricalLeagues();
  } catch (e) {
    showMsg('hist-msg', e.message, true);
  }
}

async function fetchSeason(season, btn) {
  const orig = btn.textContent;
  btn.textContent = 'Fetching…';
  btn.disabled = true;
  try {
    const result = await fetch(`${API}/admin/etl/fetch-season?season=${season}`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}` },
    }).then(r => r.json());
    showMsg('hist-msg', `Season ${season}: ${result.status}${result.error ? ' — ' + result.error : ''}`);
  } catch (e) {
    showMsg('hist-msg', e.message, true);
  } finally {
    btn.textContent = orig;
    btn.disabled = false;
  }
}

document.getElementById('hist-save-btn').addEventListener('click', async () => {
  const season = parseInt(document.getElementById('hist-season').value);
  const leagueId = document.getElementById('hist-league-id').value.trim();
  if (!season || !leagueId) { showMsg('hist-msg', 'Enter both a season year and a league ID', true); return; }
  try {
    await apiPost('/admin/historical-leagues', { season, league_id: leagueId });
    showMsg('hist-msg', `Saved league ID ${leagueId} for ${season}`);
    document.getElementById('hist-season').value = '';
    document.getElementById('hist-league-id').value = '';
    loadHistoricalLeagues();
  } catch (e) {
    showMsg('hist-msg', e.message, true);
  }
});

// ── Teams / Franchise Tags ────────────────────────────────────────────────

async function loadTeams() {
  try {
    const teams = await apiGet('/admin/teams');
    const select = document.getElementById('ft-team-select');
    select.innerHTML = teams.map(t =>
      `<option value="${t.team_key}">${t.name} (${t.manager_name || t.team_id})</option>`
    ).join('');
  } catch (e) {
    console.error('Failed to load teams:', e);
  }
}

document.getElementById('load-team-btn').addEventListener('click', async () => {
  currentTeamKey = document.getElementById('ft-team-select').value;
  if (!currentTeamKey) return;
  document.getElementById('ft-roster-area').innerHTML =
    '<div class="loading-state" style="padding:16px"><div class="spinner"></div></div>';

  try {
    currentRoster = await apiGet(`/admin/teams/${encodeURIComponent(currentTeamKey)}/roster`);
    renderRoster(currentRoster);
  } catch (e) {
    document.getElementById('ft-roster-area').innerHTML =
      `<div class="msg error">Failed to load roster: ${e.message}</div>`;
  }
});

function renderRoster(players) {
  const area = document.getElementById('ft-roster-area');
  if (!players.length) {
    area.innerHTML = '<div class="empty-state" style="padding:20px">No players found on this roster.</div>';
    return;
  }

  const eligible = players.filter(p => p.franchise_eligible || p.keeper_eligible);
  const ineligible = players.filter(p => !p.franchise_eligible && !p.keeper_eligible);

  let html = '<div style="margin-bottom:10px;font-size:0.82rem;color:var(--text-muted)">Check players to apply franchise tag for the upcoming season:</div>';

  for (const p of eligible) {
    const keeperInfo = p.keeper_eligible
      ? `<span style="color:var(--keeper)">K Rd${p.keep_round}</span>`
      : '<span style="color:var(--text-dim)">No keeper</span>';
    const ftInfo = p.franchise_eligible
      ? `<span style="color:var(--franchise)">FT Rd${p.franchise_round}</span>`
      : '<span style="color:var(--text-dim)">FT ineligible</span>';
    const adpInfo = p.adp_rank ? `ADP #${p.adp_rank}` : '';
    const checked = p.is_franchised ? 'checked' : '';
    const tagId = p.franchise_tag_id;

    html += `
      <div class="player-check" data-player-key="${p.player_key}" data-tag-id="${tagId || ''}">
        <input type="checkbox" id="ft-${p.player_key}" ${checked} ${!p.franchise_eligible ? 'disabled title="' + (p.franchise_ineligibility_reason || '') + '"' : ''}
          onchange="handleFranchiseToggle(this, '${p.player_key}', ${tagId || 'null'})">
        <label for="ft-${p.player_key}">
          <span style="font-weight:600">${p.player_name}</span>
          <span style="color:var(--text-muted)"> · ${p.position} ${p.nfl_team || ''}</span>
        </label>
        <div class="round-info">${keeperInfo} &nbsp; ${ftInfo} ${adpInfo ? '· ' + adpInfo : ''}</div>
      </div>`;
  }

  if (ineligible.length) {
    html += `<div style="margin-top:14px;color:var(--text-dim);font-size:0.8rem">Ineligible players (${ineligible.length}):</div>`;
    for (const p of ineligible.slice(0, 10)) {
      html += `
        <div class="player-check" style="opacity:0.45">
          <input type="checkbox" disabled>
          <label>${p.player_name} · ${p.position}</label>
          <div class="round-info" style="font-size:0.72rem;color:var(--text-dim)">${p.ineligibility_reason || 'Ineligible'}</div>
        </div>`;
    }
  }

  area.innerHTML = html;
}

async function handleFranchiseToggle(checkbox, playerKey, existingTagId) {
  const season = parseInt(document.getElementById('ft-team-select').selectedOptions[0]?.dataset?.upcoming || '2026');
  // Get upcoming season from status
  try {
    const s = await apiGet('/status');
    const upcomingSeason = s.upcoming_season;

    if (checkbox.checked) {
      await apiPost('/admin/franchise-tags', {
        team_key: currentTeamKey,
        player_key: playerKey,
        season: upcomingSeason,
      });
      showMsg('ft-msg', `Franchise tag added for ${playerKey}`);
    } else if (existingTagId) {
      await apiDelete(`/admin/franchise-tags/${existingTagId}`);
      showMsg('ft-msg', `Franchise tag removed`);
    }
    // Reload roster to get updated tag IDs
    currentRoster = await apiGet(`/admin/teams/${encodeURIComponent(currentTeamKey)}/roster`);
    renderRoster(currentRoster);
  } catch (e) {
    showMsg('ft-msg', e.message, true);
    checkbox.checked = !checkbox.checked; // revert
  }
}

// ── ADP Matching ──────────────────────────────────────────────────────────

async function loadAdpMatching() {
  const out = document.getElementById('val-output');
  out.innerHTML = '<div class="loading-state" style="padding:20px"><div class="spinner"></div></div>';
  try {
    const data = await apiGet('/admin/validation/adp-matching');
    renderAdpMatching(data);
  } catch (e) {
    out.innerHTML = `<div class="msg error">Failed: ${e.message}</div>`;
  }
}

function ratioColor(r) {
  if (r >= 0.92) return 'var(--keeper)';
  if (r >= 0.80) return 'var(--franchise)';
  return 'var(--text-muted)';
}

function renderAdpMatching(data) {
  const out = document.getElementById('val-output');
  const unmatchedFP = (data.unmatched_fp || []);
  const unmatchedYahoo = (data.unmatched_yahoo_roster || []);
  const highSim = unmatchedFP.filter(p => p.candidates.some(c => c.ratio >= 0.80));

  let html = `
    <div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:16px">
      <div class="status-item"><div class="status-key">Total FP Players</div><div class="status-val">${data.total_fp}</div></div>
      <div class="status-item"><div class="status-key">Matched to Yahoo</div><div class="status-val" style="color:var(--keeper)">${data.matched}</div></div>
      <div class="status-item"><div class="status-key">Unmatched FP (non-K/DEF)</div><div class="status-val" style="color:${unmatchedFP.length ? 'var(--franchise)' : 'var(--keeper)'}">${unmatchedFP.length}</div></div>
      <div class="status-item"><div class="status-key">Roster players w/ no ADP</div><div class="status-val" style="color:${unmatchedYahoo.length ? 'var(--ineligible)' : 'var(--keeper)'}">${unmatchedYahoo.length}</div></div>
    </div>`;

  // Section 1: Yahoo roster players with no ADP (most important — affects franchise rounds)
  if (unmatchedYahoo.length) {
    html += `<div style="font-weight:700;font-size:0.88rem;margin-bottom:8px;color:var(--ineligible)">Roster Players Missing ADP (${unmatchedYahoo.length})</div>
    <table class="val-table" style="margin-bottom:20px">
      <thead><tr><th>Yahoo Name</th><th>Pos</th><th>NFL Team</th><th>Best FP Candidate</th><th>Similarity</th><th>Action</th></tr></thead>
      <tbody>
        ${unmatchedYahoo.map(p => {
          const best = p.candidates[0];
          return `<tr>
            <td style="font-weight:600">${p.yahoo_name}</td>
            <td style="color:var(--text-muted)">${p.position}</td>
            <td style="color:var(--text-muted)">${p.nfl_team || '—'}</td>
            <td>${best ? `${best.fp_name} <span style="color:var(--text-dim);font-size:0.75rem">(ADP #${best.adp_rank})</span>` : '<span style="color:var(--text-dim)">No candidate found</span>'}</td>
            <td>${best ? `<span style="color:${ratioColor(best.ratio)};font-weight:600">${Math.round(best.ratio * 100)}%</span>` : '—'}</td>
            <td>${best && best.ratio >= 0.80
              ? `<button class="add-tag-btn" onclick="adpLink(${best.fp_player_id}, '${p.player_key}', this)">Link</button>`
              : ''}</td>
          </tr>`;
        }).join('')}
      </tbody>
    </table>`;
  } else {
    html += `<div style="color:var(--keeper);font-size:0.85rem;margin-bottom:16px">✓ All roster players have ADP data</div>`;
  }

  // Section 2: FP players with no Yahoo match but high-similarity candidate (likely mismatches)
  if (highSim.length) {
    html += `<div style="font-weight:700;font-size:0.88rem;margin-bottom:8px;color:var(--franchise)">Likely Mismatches — FP Players with Similar Yahoo Names (${highSim.length})</div>
    <table class="val-table" style="margin-bottom:20px">
      <thead><tr><th>FP Name</th><th>Pos</th><th>ADP</th><th>Best Yahoo Candidate</th><th>Similarity</th><th>On Roster?</th><th>Action</th></tr></thead>
      <tbody>
        ${highSim.map(p => {
          const best = p.candidates[0];
          return `<tr>
            <td style="font-weight:600">${p.fp_name}</td>
            <td style="color:var(--text-muted)">${p.position}</td>
            <td style="color:var(--text-muted)">#${p.adp_rank}</td>
            <td>${best.yahoo_name}</td>
            <td><span style="color:${ratioColor(best.ratio)};font-weight:600">${Math.round(best.ratio * 100)}%</span></td>
            <td>${best.on_roster ? '<span style="color:var(--keeper)">Yes</span>' : '<span style="color:var(--text-dim)">No</span>'}</td>
            <td><button class="add-tag-btn" onclick="adpLink(${p.fp_player_id}, '${best.player_key}', this)">Link</button></td>
          </tr>`;
        }).join('')}
      </tbody>
    </table>`;
  } else {
    html += `<div style="color:var(--keeper);font-size:0.85rem;margin-bottom:16px">✓ No likely mismatches detected</div>`;
  }

  out.innerHTML = html;
}

async function adpLink(fpPlayerId, yahooPlayerKey, btn) {
  btn.disabled = true;
  btn.textContent = '…';
  try {
    await apiPost('/admin/adp/manual-link', { fp_player_id: fpPlayerId, yahoo_player_key: yahooPlayerKey });
    btn.textContent = 'Linked ✓';
    btn.style.color = 'var(--keeper)';
    loadAdpMatching(); // refresh
  } catch (e) {
    btn.textContent = 'Error';
    btn.disabled = false;
    showMsg('val-adp-msg', e.message, true);
  }
}

// ── Historical Keeper Import ──────────────────────────────────────────────

document.getElementById('import-btn').addEventListener('click', async () => {
  const raw = document.getElementById('import-json').value.trim();
  if (!raw) { showMsg('import-msg', 'No JSON provided', true); return; }

  let items;
  try {
    items = JSON.parse(raw);
    if (!Array.isArray(items)) throw new Error('Expected a JSON array');
  } catch (e) {
    showMsg('import-msg', 'Invalid JSON: ' + e.message, true);
    return;
  }

  try {
    const result = await apiPost('/admin/historical-keepers/import', items);
    showMsg('import-msg', `Imported ${result.added} records`);
    loadOverrides();
  } catch (e) {
    showMsg('import-msg', e.message, true);
  }
});

async function loadOverrides() {
  try {
    const overrides = await apiGet('/admin/historical-keepers');
    const el = document.getElementById('overrides-list');
    if (!overrides.length) {
      el.innerHTML = '<div style="color:var(--text-dim);font-size:0.82rem;padding:8px 0">No historical overrides imported yet.</div>';
      return;
    }
    el.innerHTML = overrides.map(o => `
      <div class="player-check">
        <div style="flex:1;font-size:0.82rem">
          <strong>${o.player_key}</strong> (${o.season})
          · Drafted ${o.original_draft_year} Rd ${o.original_draft_round}
          · Team: ${o.team_key}
          ${o.was_franchise_tagged ? '<span style="color:var(--franchise)"> [FT]</span>' : ''}
          ${o.notes ? `<span style="color:var(--text-dim)"> — ${o.notes}</span>` : ''}
        </div>
      </div>`).join('');
  } catch (e) {
    console.error('Failed to load overrides:', e);
  }
}

// ── Draft Order ───────────────────────────────────────────────────────────

let allTeams = [];
let upcomingSeason = 2026;

async function initDraftOrder() {
  try {
    const s = await apiGet('/status');
    upcomingSeason = s.upcoming_season;
    document.getElementById('draft-order-season').textContent = upcomingSeason;

    const [teams, order] = await Promise.all([
      apiGet('/admin/teams'),
      apiGet('/admin/draft-order'),
    ]);
    allTeams = teams;

    // Populate pick-trade team dropdowns
    const teamOptions = allTeams.map(t =>
      `<option value="${t.team_key}">${t.name}</option>`
    ).join('');
    document.getElementById('trade-orig-team').innerHTML = teamOptions;
    document.getElementById('trade-new-owner').innerHTML = teamOptions;

    renderDraftOrderForm(teams, order);
  } catch (e) {
    document.getElementById('draft-order-area').innerHTML =
      `<div class="msg error">Failed to load draft order: ${e.message}</div>`;
  }
}

function renderDraftOrderForm(teams, existingOrder) {
  const area = document.getElementById('draft-order-area');
  const numTeams = teams.length || 12;

  // Build position → team_key map from existing order
  const existingMap = {};
  for (const row of existingOrder) existingMap[row.draft_position] = row.team_key;

  const teamOptions = ['<option value="">— Select team —</option>',
    ...teams.map(t => `<option value="${t.team_key}">${t.name}</option>`)
  ].join('');

  const slots = Array.from({ length: numTeams }, (_, i) => {
    const pos = i + 1;
    const selected = existingMap[pos] || '';
    return `
      <div class="draft-order-slot">
        <span class="draft-order-pos">${pos}</span>
        <select class="draft-order-select" data-position="${pos}">
          ${teams.map(t =>
            `<option value="${t.team_key}" ${t.team_key === selected ? 'selected' : ''}>${t.name}</option>`
          ).join('')}
        </select>
      </div>`;
  }).join('');

  area.innerHTML = `<div class="draft-order-grid">${slots}</div>`;
}

document.getElementById('save-draft-order-btn').addEventListener('click', async () => {
  const selects = document.querySelectorAll('.draft-order-select');
  if (!selects.length) { showMsg('draft-order-msg', 'No draft order form loaded', true); return; }

  const items = [];
  const seen = new Set();
  let valid = true;

  for (const sel of selects) {
    const pos = parseInt(sel.dataset.position);
    const key = sel.value;
    if (!key) { showMsg('draft-order-msg', `Position ${pos} has no team selected`, true); valid = false; break; }
    if (seen.has(key)) { showMsg('draft-order-msg', `Duplicate team at position ${pos}`, true); valid = false; break; }
    seen.add(key);
    items.push({ team_key: key, draft_position: pos });
  }
  if (!valid) return;

  try {
    const result = await apiPost('/admin/draft-order', items);
    showMsg('draft-order-msg', `Draft order saved (${result.count} teams)`);
  } catch (e) {
    showMsg('draft-order-msg', e.message, true);
  }
});

document.getElementById('clear-draft-order-btn').addEventListener('click', async () => {
  if (!confirm('Clear the draft order? The board will fall back to alphabetical team order.')) return;
  try {
    await apiPost('/admin/draft-order', []);
    showMsg('draft-order-msg', 'Draft order cleared');
    renderDraftOrderForm(allTeams, []);
  } catch (e) {
    showMsg('draft-order-msg', e.message, true);
  }
});

// ── Pick Trades ───────────────────────────────────────────────────────────

async function loadTrades() {
  try {
    const trades = await apiGet('/admin/pick-ownership');
    renderTradesList(trades);
  } catch (e) {
    document.getElementById('trades-list').innerHTML =
      `<div class="msg error">Failed to load trades: ${e.message}</div>`;
  }
}

function renderTradesList(trades) {
  const el = document.getElementById('trades-list');
  if (!trades.length) {
    el.innerHTML = '<div style="color:var(--text-dim);font-size:0.82rem">No traded picks recorded.</div>';
    return;
  }
  el.innerHTML = trades.map(t => `
    <div class="trade-row">
      <span style="color:var(--text-muted)">Rd ${t.round_number}</span>
      <span style="font-weight:600">${t.original_team_name}</span>
      <span style="color:var(--accent)">→</span>
      <span style="font-weight:600">${t.current_owner_name}</span>
      ${t.note ? `<span style="color:var(--text-dim);font-size:0.78rem">· ${t.note}</span>` : ''}
      <button onclick="removeTrade(${t.id})" class="tag-remove" style="margin-left:auto">Remove</button>
    </div>`).join('');
}

document.getElementById('add-trade-btn').addEventListener('click', async () => {
  const origKey = document.getElementById('trade-orig-team').value;
  const round = parseInt(document.getElementById('trade-round').value);
  const newOwnerKey = document.getElementById('trade-new-owner').value;

  if (!origKey || !newOwnerKey) { showMsg('trade-msg', 'Select both teams', true); return; }
  if (origKey === newOwnerKey) { showMsg('trade-msg', 'Original team and new owner must be different', true); return; }

  try {
    await apiPost('/admin/pick-ownership', {
      original_team_key: origKey,
      round_number: round,
      current_owner_team_key: newOwnerKey,
    });
    showMsg('trade-msg', `Rd ${round} pick trade recorded`);
    loadTrades();
  } catch (e) {
    showMsg('trade-msg', e.message, true);
  }
});

async function showRawTxn(txnKey, el) {
  const box = el.nextElementSibling;
  if (box.style.display !== 'none') { box.style.display = 'none'; return; }
  box.style.display = 'block';
  box.textContent = 'Loading…';
  try {
    const data = await apiGet(`/admin/validation/transaction-raw/${encodeURIComponent(txnKey)}`);
    box.textContent = JSON.stringify(data, null, 2);
    box.style.cssText = 'display:block;font-family:monospace;font-size:0.68rem;white-space:pre-wrap;word-break:break-all;background:var(--bg);border:1px solid var(--border);border-radius:4px;padding:8px;margin-top:4px;max-height:300px;overflow-y:auto;color:var(--text-muted)';
  } catch (e) {
    box.textContent = 'Error: ' + e.message;
  }
}

async function importPickTrade(originalTeamKey, round, newOwnerKey) {
  try {
    await apiPost('/admin/pick-ownership', {
      original_team_key: originalTeamKey,
      round_number: parseInt(round),
      current_owner_team_key: newOwnerKey,
    });
    showMsg('trade-msg', `Rd ${round} pick trade saved`);
    loadTrades();
  } catch (e) {
    showMsg('trade-msg', e.message, true);
  }
}

async function removeTrade(id) {
  try {
    await apiDelete(`/admin/pick-ownership/${id}`);
    loadTrades();
  } catch (e) {
    showMsg('trade-msg', e.message, true);
  }
}

// ── Data Validation ───────────────────────────────────────────────────────

document.getElementById('val-draft-btn').addEventListener('click', () => loadValidation('draft-picks'));
document.getElementById('val-txn-btn').addEventListener('click', () => loadValidation('transactions'));
document.getElementById('val-trade-btn').addEventListener('click', () => loadValidation('transactions', 'trade'));
document.getElementById('val-roster-btn').addEventListener('click', () => loadValidation('rosters'));
document.getElementById('val-adp-btn').addEventListener('click', () => loadAdpMatching());

async function loadValidation(type, txnType = '') {
  const season = document.getElementById('val-season').value;
  if (!season) { return; }
  const out = document.getElementById('val-output');
  out.innerHTML = '<div class="loading-state" style="padding:20px"><div class="spinner"></div></div>';

  try {
    let url = `/admin/validation/${type}?season=${season}`;
    if (txnType) url += `&txn_type=${txnType}`;
    const data = await apiGet(url);
    renderValidation(type, data, season, txnType);
  } catch (e) {
    out.innerHTML = `<div class="msg error">Failed: ${e.message}</div>`;
  }
}

function renderValidation(type, data, season, txnType = '') {
  const out = document.getElementById('val-output');

  if (type === 'draft-picks') {
    if (!data.length) { out.innerHTML = `<div style="color:var(--text-dim);padding:12px 0">No draft picks for ${season}.</div>`; return; }
    out.innerHTML = `
      <div style="color:var(--text-muted);font-size:0.8rem;margin-bottom:8px">${data.length} picks for ${season}</div>
      <table class="val-table">
        <thead><tr><th>#</th><th>Rd</th><th>Team</th><th>Player</th><th>Keeper?</th><th>Cost Rd</th></tr></thead>
        <tbody>
          ${data.map(p => `
            <tr>
              <td>${p.pick_number}</td>
              <td>${p.round_number}</td>
              <td>${p.team_name}</td>
              <td>${p.player_name}</td>
              <td>${p.is_keeper ? '<span class="val-badge-keeper">K</span>' : ''}</td>
              <td>${p.keeper_cost_round || ''}</td>
            </tr>`).join('')}
        </tbody>
      </table>`;

  } else if (type === 'transactions') {
    if (!data.length) { out.innerHTML = `<div style="color:var(--text-dim);padding:12px 0">No transactions for ${season}.</div>`; return; }

    const typeColor = { trade: 'var(--franchise)', add: 'var(--keeper)', drop: 'var(--ineligible)', 'add/drop': 'var(--accent)' };
    const note = `<div style="color:var(--text-dim);font-size:0.78rem;margin-bottom:10px">
      These are <strong>player</strong> transactions (adds, drops, trades).
      Draft pick slot trades must be entered separately in the <strong>Pick Trades</strong> section above.
    </div>`;

    const rows = data.map(t => {
      const color = typeColor[t.transaction_type] || 'var(--text-muted)';
      const date = t.timestamp ? new Date(t.timestamp).toLocaleDateString() : '—';

      const playerLines = (t.players || []).map(p => {
        if (t.transaction_type === 'trade') {
          return `<div style="font-size:0.78rem;color:var(--text-muted)">${p.player_name} · <span style="color:var(--text-dim)">${p.source_team}</span> → <span style="color:var(--text)">${p.destination_team}</span></div>`;
        }
        const dest = p.destination_team !== '—' ? p.destination_team : p.source_team;
        const action = p.move_type === 'add' ? `added by ${dest}` : `dropped by ${p.source_team}`;
        return `<div style="font-size:0.78rem;color:var(--text-muted)">${p.player_name} · ${action}</div>`;
      }).join('') || `<div style="font-size:0.72rem;color:var(--text-dim)">No player details (legacy record)</div>`;

      const pickLines = (t.picks || []).map(pk => {
        const btn = `<button onclick="importPickTrade('${pk.original_team_key}',${pk.round},'${pk.destination_team_key}')"
          style="margin-left:8px;padding:1px 8px;font-size:0.7rem;background:rgba(245,158,11,0.2);color:var(--franchise);border:1px solid rgba(245,158,11,0.4);border-radius:4px;cursor:pointer"
          title="Add to Pick Trades">+ Add</button>`;
        return `<div style="font-size:0.78rem;color:var(--franchise);margin-top:2px">
          📋 Rd ${pk.round} (${pk.original_team}) · <span style="color:var(--text-dim)">${pk.source_team}</span> → <span style="color:var(--text)">${pk.destination_team}</span>${btn}
        </div>`;
      }).join('');

      return `
        <tr>
          <td style="font-size:0.72rem;color:var(--text-dim)">
            <span style="cursor:pointer;text-decoration:underline;text-decoration-style:dotted"
              onclick="showRawTxn('${t.transaction_key}', this)">${t.transaction_key}</span>
            <div class="raw-json-box" style="display:none"></div>
          </td>
          <td><span style="color:${color};font-weight:600">${t.transaction_type}</span></td>
          <td style="font-size:0.72rem">${date}</td>
          <td>${playerLines}${pickLines}</td>
        </tr>`;
    }).join('');

    out.innerHTML = note + `
      <div style="color:var(--text-muted);font-size:0.8rem;margin-bottom:8px">${data.length} ${txnType || 'all'} transactions for ${season}</div>
      <table class="val-table">
        <thead><tr><th>Key</th><th>Type</th><th>Date</th><th>Players</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;

  } else if (type === 'rosters') {
    const teams = Object.keys(data);
    if (!teams.length) { out.innerHTML = `<div style="color:var(--text-dim);padding:12px 0">No roster snapshots for ${season}.</div>`; return; }
    out.innerHTML = teams.map(tname => `
      <div style="margin-bottom:16px">
        <div style="font-weight:700;color:var(--text);font-size:0.88rem;padding:6px 0;border-bottom:1px solid var(--border);margin-bottom:6px">${tname}</div>
        <table class="val-table">
          <thead><tr><th>Player</th><th>Pos</th><th>NFL Team</th><th>Slot</th><th>Acquisition</th></tr></thead>
          <tbody>
            ${data[tname].map(p => `
              <tr>
                <td>${p.player_name}</td>
                <td style="color:var(--text-muted)">${p.position}</td>
                <td style="color:var(--text-muted)">${p.nfl_team || ''}</td>
                <td style="color:var(--text-muted)">${p.roster_position || ''}</td>
                <td style="color:var(--text-dim)">${p.acquisition_type || ''}</td>
              </tr>`).join('')}
          </tbody>
        </table>
      </div>`).join('');
  }
}

// ── Historical Franchise Tags ─────────────────────────────────────────────

document.getElementById('hft-load-btn').addEventListener('click', () => {
  const season = document.getElementById('hft-season-select').value;
  loadHistoricalKeeperPicks(parseInt(season));
});

async function loadHistoricalKeeperPicks(season) {
  const list = document.getElementById('hft-list');
  list.innerHTML = '<div class="loading-state" style="padding:12px"><div class="spinner"></div></div>';
  try {
    const picks = await apiGet(`/admin/historical-keeper-picks?season=${season}`);
    if (!picks.length) {
      list.innerHTML = `<div style="color:var(--text-dim);font-size:0.83rem">No keeper picks found for ${season}. Make sure you have fetched that season's data.</div>`;
      return;
    }
    list.innerHTML = `
      <div style="font-size:0.8rem;color:var(--text-dim);margin-bottom:10px">${picks.length} keeper picks in ${season} — check any that were franchise tags</div>
      ${picks.map(p => `
        <div class="tag-row" style="justify-content:space-between">
          <label style="display:flex;align-items:center;gap:10px;cursor:pointer;flex:1">
            <input type="checkbox" ${p.is_franchise_tag ? 'checked' : ''}
              style="width:16px;height:16px;accent-color:var(--franchise);flex-shrink:0"
              onchange="toggleHistoricalFT(${season}, '${p.player_key}', '${p.team_key}', this.checked)">
            <span style="font-size:0.88rem"><strong>${p.player_name}</strong> <span style="color:var(--text-muted)">${p.position}</span></span>
          </label>
          <span style="font-size:0.8rem;color:var(--text-muted);white-space:nowrap">${p.team_name} &mdash; Rd ${p.round_number}</span>
        </div>`).join('')}`;
  } catch (e) {
    list.innerHTML = `<div class="msg error">${e.message}</div>`;
  }
}

async function toggleHistoricalFT(season, playerKey, teamKey, checked) {
  try {
    if (checked) {
      await apiPost('/admin/historical-franchise-tags', { season, player_key: playerKey, team_key: teamKey });
    } else {
      await apiDelete(`/admin/historical-franchise-tags/${season}/${encodeURIComponent(playerKey)}`);
    }
    showMsg('hft-msg', checked ? 'Marked as franchise tag — keepers recalculated.' : 'Removed franchise tag — keepers recalculated.');
  } catch (e) {
    showMsg('hft-msg', e.message, true);
    // Revert the checkbox on failure
    loadHistoricalKeeperPicks(season);
  }
}

