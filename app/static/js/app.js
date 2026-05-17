'use strict';

const API = '/api';
let state = { teams: [], board: null, status: null, keeperSummaries: {} };

// ── Utilities ──────────────────────────────────────────────────────────────

async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}: ${url}`);
  return res.json();
}

function posColor(pos) {
  const map = { QB: '#f87171', RB: '#34d399', WR: '#60a5fa', TE: '#fbbf24', K: '#94a3b8', DEF: '#94a3b8' };
  return map[pos] || '#94a3b8';
}

function yearsColor(y) {
  const palette = ['#10b981', '#84cc16', '#f59e0b', '#f97316', '#ef4444'];
  return palette[Math.min((y || 1) - 1, 4)];
}

function timeAgo(isoStr) {
  if (!isoStr) return 'never';
  const diff = Math.floor((Date.now() - new Date(isoStr).getTime()) / 1000);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  return `${Math.floor(diff / 3600)}h ago`;
}

function initials(name) {
  return (name || '?')
    .split(' ')
    .map(w => w[0])
    .join('')
    .slice(0, 2)
    .toUpperCase();
}

// ── Status bar ────────────────────────────────────────────────────────────

async function loadStatus() {
  try {
    state.status = await fetchJSON(`${API}/status`);
    const { last_updated, upcoming_season, previous_season, etl_status, is_authorized } = state.status;

    document.getElementById('season-label').textContent =
      `${previous_season} Season → ${upcoming_season} Keeper Board`;

    const updatedEl = document.getElementById('last-updated');
    if (last_updated) {
      updatedEl.textContent = `Updated ${timeAgo(last_updated)}`;
      updatedEl.title = new Date(last_updated).toLocaleString();
    } else {
      updatedEl.textContent = etl_status === 'running' ? 'ETL running…' : 'No data yet';
    }

    if (!is_authorized) showSetupBanner('authorization');
    else if (etl_status !== 'complete') showSetupBanner('etl');

  } catch (e) {
    document.getElementById('last-updated').textContent = 'Offline';
  }
}

function showSetupBanner(type) {
  const container = document.getElementById('teams-container');
  if (type === 'authorization') {
    container.innerHTML = `
      <div class="setup-banner">
        <h2>Setup Required</h2>
        <p>The app needs to authorize with Yahoo Fantasy to fetch league data.</p>
        <a class="btn btn-primary" href="/setup/authorize">Authorize with Yahoo</a>
      </div>`;
  } else {
    container.innerHTML = `
      <div class="setup-banner">
        <h2>Initial Data Load Needed</h2>
        <p>Visit the admin panel to trigger the initial data pull from Yahoo and FantasyPros.</p>
        <a class="btn btn-primary" href="/admin.html">Go to Admin Panel</a>
      </div>`;
  }
}

// ── Teams tab ─────────────────────────────────────────────────────────────

async function loadTeams() {
  if (!state.status || !state.status.is_authorized || state.status.etl_status !== 'complete') return;

  try {
    const [teams, board] = await Promise.all([
      fetchJSON(`${API}/teams`),
      fetchJSON(`${API}/draft-board`),
    ]);
    state.teams = teams;
    state.board = board;

    // Load keeper summaries for all teams in parallel
    const summaries = await Promise.all(
      teams.map(t =>
        fetchJSON(`${API}/teams/${encodeURIComponent(t.team_key)}/keepers`).catch(() => null)
      )
    );
    state.keeperSummaries = {};
    teams.forEach((t, i) => { state.keeperSummaries[t.team_key] = summaries[i]; });

    renderTeamGrid(teams);
  } catch (e) {
    document.getElementById('teams-container').innerHTML =
      `<div class="empty-state">Failed to load teams: ${e.message}</div>`;
  }
}

function renderTeamGrid(teams) {
  const container = document.getElementById('teams-container');

  if (!teams.length) {
    container.innerHTML = '<div class="empty-state">No teams found.</div>';
    return;
  }

  const grid = document.createElement('div');
  grid.className = 'team-grid';

  for (const team of teams) {
    const summary = state.keeperSummaries[team.team_key];
    grid.appendChild(buildTeamCard(team, summary));
  }

  container.innerHTML = '';
  container.appendChild(grid);
}

function buildTeamCard(team, summary) {
  const card = document.createElement('div');
  card.className = 'team-card';
  card.setAttribute('data-team-key', team.team_key);

  const logoHtml = team.logo_url
    ? `<img class="team-logo" src="${team.logo_url}" alt="${team.name}" onerror="this.style.display='none'">`
    : `<div class="team-logo-placeholder">${initials(team.name)}</div>`;

  const tagged = new Set((summary?.franchise_tags_set) || []);
  const regularKeepers = (summary?.eligible_keepers || []).filter(p => !tagged.has(p.player_key));
  const taggedPlayers = (summary?.franchise_eligible || []).filter(p => tagged.has(p.player_key));
  const maxCarryovers = summary?.max_carryovers ?? 4;
  const usedSlots = regularKeepers.length + taggedPlayers.length;

  // Preview: up to 4 players (keepers first, then tagged FTs)
  const previewPlayers = [
    ...regularKeepers.slice(0, 3).map(p => ({ ...p, isFT: false })),
    ...taggedPlayers.slice(0, 2).map(p => ({ ...p, isFT: true })),
  ].slice(0, 4);

  const preview = previewPlayers.length
    ? previewPlayers.map(p => {
        const round = p.isFT ? p.franchise_round : p.keep_round;
        return `
          <div class="preview-player">
            <span class="preview-name">${p.player_name}</span>
            <span class="preview-round ${p.isFT ? 'franchise' : 'keeper'}">${round ? 'Rd ' + round : '—'}</span>
          </div>`;
      }).join('')
    : '<div class="preview-player"><span style="color:var(--text-dim)">Click to view keepers</span></div>';

  card.innerHTML = `
    <div class="team-card-header">
      ${logoHtml}
      <div class="team-info">
        <div class="team-name">${team.name}</div>
        <div class="team-manager">${team.manager_name || ''}</div>
      </div>
    </div>
    <div class="team-card-stats">
      <div class="stat-item">
        <div class="stat-value green">${regularKeepers.length}</div>
        <div class="stat-label">Keepers</div>
      </div>
      <div class="stat-item">
        <div class="stat-value amber">${taggedPlayers.length}</div>
        <div class="stat-label">Fr. Tags</div>
      </div>
      <div class="stat-item">
        <div class="stat-value" style="color:var(--text)">${usedSlots}/${maxCarryovers}</div>
        <div class="stat-label">Slots Used</div>
      </div>
    </div>
    <div class="team-card-preview">${preview}</div>
  `;

  card.addEventListener('click', () => openTeamModal(team));
  return card;
}

// ── Team modal ────────────────────────────────────────────────────────────

async function openTeamModal(team) {
  const overlay = document.getElementById('modal-overlay');
  const logoEl = document.getElementById('modal-logo');
  const nameEl = document.getElementById('modal-team-name');
  const managerEl = document.getElementById('modal-manager');
  const bodyEl = document.getElementById('modal-body');

  nameEl.textContent = team.name;
  managerEl.textContent = team.manager_name || '';

  if (team.logo_url) {
    logoEl.innerHTML = `<img style="width:40px;height:40px;border-radius:8px;object-fit:cover" src="${team.logo_url}" alt="${team.name}">`;
  } else {
    logoEl.textContent = initials(team.name);
  }

  bodyEl.innerHTML = '<div class="loading-state"><div class="spinner"></div></div>';
  overlay.classList.add('open');
  document.body.style.overflow = 'hidden';

  try {
    const summary = await fetchJSON(`${API}/teams/${encodeURIComponent(team.team_key)}/keepers`);
    renderModalBody(bodyEl, summary);
  } catch (e) {
    bodyEl.innerHTML = `<div class="empty-state">Failed to load: ${e.message}</div>`;
  }
}

function renderModalBody(bodyEl, s) {
  const { eligible_keepers, franchise_eligible, franchise_tags_set, ineligible,
          available_keeper_slots, ft_keeper_penalty, max_carryovers } = s;

  const tagged = new Set(franchise_tags_set || []);

  // Players in each category
  const regularKeepers = eligible_keepers.filter(p => !tagged.has(p.player_key));
  const taggedPlayers = franchise_eligible.filter(p => tagged.has(p.player_key));
  const untaggedFT = franchise_eligible.filter(p => !tagged.has(p.player_key));

  const total = regularKeepers.length + taggedPlayers.length;

  // Cap-bar dots
  const capDots = Array.from({ length: max_carryovers }, (_, i) => {
    const used = i < total;
    const isFT = i < taggedPlayers.length;
    return `<span class="cap-dot ${used ? (isFT ? 'franchise' : 'keeper') : 'empty'}"></span>`;
  }).join('');

  let html = `
    <div class="cap-bar">
      ${capDots}
      <span style="color:var(--text-muted);font-size:0.82rem">
        ${total}/${max_carryovers} carry-over slots used
        ${ft_keeper_penalty > 0 ? `<span style="color:var(--franchise)"> · ${ft_keeper_penalty} keeper slot${ft_keeper_penalty > 1 ? 's' : ''} forfeited for extra FT</span>` : ''}
      </span>
    </div>`;

  // Section 1: Regular keepers (keeper-eligible, not franchise tagged)
  html += `<div class="section-header">Regular Keepers (${regularKeepers.length} eligible / ${available_keeper_slots} slots)</div>`;
  if (regularKeepers.length === 0) {
    html += `<div class="empty-state" style="padding:16px 0;font-size:0.85rem">No eligible keepers</div>`;
  } else {
    html += regularKeepers.map(p => playerRow(p, 'keeper')).join('');
  }

  // Section 2: Franchise tagged (admin-set)
  if (taggedPlayers.length > 0) {
    html += `<div class="section-header">Franchise Tagged (${taggedPlayers.length})</div>`;
    html += taggedPlayers.map(p => playerRow(p, 'franchise')).join('');
  }

  // Section 3: Franchise-eligible but not yet tagged
  if (untaggedFT.length > 0) {
    html += `<div class="section-header">Franchise Tag Eligible (${untaggedFT.length})</div>`;
    html += untaggedFT.map(p => playerRow(p, 'franchise-eligible')).join('');
  }

  // Section 4: Ineligible (collapsed)
  if (ineligible.length > 0) {
    html += `
      <div class="ineligible-toggle" onclick="this.nextElementSibling.classList.toggle('open');this.querySelector('.toggle-icon').textContent=this.nextElementSibling.classList.contains('open')?'▲':'▼'">
        <span class="toggle-icon">▼</span>
        <span>Ineligible players (${ineligible.length})</span>
      </div>
      <div class="ineligible-list">
        ${ineligible.map(p => playerRowIneligible(p)).join('')}
      </div>`;
  }

  bodyEl.innerHTML = html;
}

function playerRow(p, type) {
  const round = type === 'keeper' ? p.keep_round : p.franchise_round;
  const chipClass = type === 'franchise' ? 'franchise'
    : type === 'franchise-eligible' ? 'franchise-eligible'
    : 'keeper';
  const rdLabel = round ? `Rd ${round}` : 'ADP ?';
  const keptBadge = (p.times_kept > 0) ? ` · <span style="color:var(--franchise);font-size:0.75rem;font-weight:600">Kept ${p.times_kept}×</span>` : '';
  const meta = type === 'keeper'
    ? `Drafted ${p.original_draft_year} · Rd ${p.original_draft_round}${keptBadge}`
    : p.adp_rank ? `ADP #${p.adp_rank}` : 'No ADP data — round TBD';

  const extraClass = type === 'franchise-eligible' ? ' ft-eligible' : '';

  const yrs = p.years_since_draft;
  const yc = yrs ? yearsColor(yrs) : null;
  const yrsChip = (type === 'keeper' && yrs)
    ? `<span style="display:inline-block;padding:3px 8px;border-radius:6px;font-size:0.75rem;font-weight:700;background:${yc}22;color:${yc};border:1px solid ${yc}55">${yrs}yr${yrs !== 1 ? 's' : ''}</span>`
    : '';

  return `
    <div class="player-row${extraClass}">
      <div class="pos-badge" style="color:${posColor(p.position)}">${p.position}</div>
      <div class="player-details">
        <div class="player-full-name">${p.player_name}</div>
        <div class="player-meta">${p.nfl_team || ''} · ${meta}</div>
      </div>
      <div class="player-action" style="display:flex;flex-direction:column;align-items:flex-end;gap:4px">
        ${yrsChip}
        <span class="round-chip ${chipClass}">${rdLabel}</span>
      </div>
    </div>`;
}

function playerRowIneligible(p) {
  return `
    <div class="player-row" style="opacity:0.55">
      <div class="pos-badge" style="color:${posColor(p.position)}">${p.position}</div>
      <div class="player-details">
        <div class="player-full-name">${p.player_name}</div>
        <div class="reason-text">${p.keeper_reason || p.franchise_reason || 'Ineligible'}</div>
      </div>
      <div class="player-action">
        <span class="round-chip ineligible">N/A</span>
      </div>
    </div>`;
}

function closeModal() {
  document.getElementById('modal-overlay').classList.remove('open');
  document.body.style.overflow = '';
}

// ── Draft Board tab ───────────────────────────────────────────────────────

function renderDraftBoard(board) {
  const container = document.getElementById('board-container');
  if (!board || !board.teams || !board.teams.length) {
    container.innerHTML = '<div class="empty-state">No board data available.</div>';
    return;
  }

  if (!board.draft_order_set) {
    container.innerHTML = `
      <div class="setup-banner">
        <h2>Draft Order Not Set</h2>
        <p>Set the draft order in the admin panel to populate the pick board.</p>
        <a class="btn btn-primary" href="/admin.html">Go to Admin Panel</a>
      </div>`;
    return;
  }

  const { teams, rounds, color_map } = board;

  // Build lookup: original_team_key → round → pick
  const pickGrid = {};
  for (const roundData of rounds) {
    for (const pick of roundData.picks) {
      if (!pickGrid[pick.original_team_key]) pickGrid[pick.original_team_key] = {};
      pickGrid[pick.original_team_key][roundData.round] = pick;
    }
  }

  // Legend
  const legend = document.createElement('div');
  legend.className = 'board-legend';
  legend.innerHTML = teams.map(t => `
    <div class="legend-item">
      <span class="legend-swatch" style="background:${color_map[t.team_key] || '#6c63ff'}"></span>
      <span class="legend-name">${t.name}</span>
    </div>`).join('');

  // Table
  const scrollWrap = document.createElement('div');
  scrollWrap.className = 'board-container';

  const table = document.createElement('table');
  table.className = 'draft-board';

  // Header row — one column per team, color-coded top border + subtle tint
  const thead = document.createElement('thead');
  const headerRow = document.createElement('tr');
  headerRow.innerHTML = '<th>Rd</th>' +
    teams.map(t => {
      const c = color_map[t.team_key] || '#6c63ff';
      return `<th style="border-top:4px solid ${c};background:${c}22;" title="${t.manager_name || ''}">${t.name}</th>`;
    }).join('');
  thead.appendChild(headerRow);
  table.appendChild(thead);

  // Body rows — one row per round
  const tbody = document.createElement('tbody');
  for (const roundData of rounds) {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td><strong>Rd ${roundData.round}</strong></td>`;

    for (const team of teams) {
      const pick = (pickGrid[team.team_key] || {})[roundData.round];
      if (!pick) {
        tr.innerHTML += '<td class="board-cell pick-cell"></td>';
        continue;
      }

      const ownerColor = pick.color;
      const origColor = pick.original_color || color_map[team.team_key] || '#6c63ff';
      const isTraded = pick.is_traded;
      const titleText = isTraded
        ? `Pick #${pick.overall_pick} — owned by ${pick.owner_team_name}`
        : `Pick #${pick.overall_pick} — ${pick.original_team_name}`;

      if (isTraded) {
        tr.innerHTML += `
          <td class="board-cell pick-cell traded"
              style="background:${ownerColor}30;border:2px solid ${ownerColor}!important;"
              title="${titleText}">
            <span class="pick-number" style="color:${ownerColor}">#${pick.overall_pick}</span>
            <span class="pick-traded-to" style="color:${ownerColor}">→ ${pick.owner_team_name}</span>
          </td>`;
      } else {
        tr.innerHTML += `
          <td class="board-cell pick-cell"
              style="background:${origColor}18;"
              title="${titleText}">
            <span class="pick-number">#${pick.overall_pick}</span>
          </td>`;
      }
    }

    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  scrollWrap.appendChild(table);

  const scrollHint = document.createElement('p');
  scrollHint.className = 'board-scroll-hint';
  scrollHint.textContent = '← Scroll to see all teams →';

  container.innerHTML = '';
  container.appendChild(legend);
  container.appendChild(scrollHint);
  container.appendChild(scrollWrap);
}

// ── Tab navigation ────────────────────────────────────────────────────────

function initTabs() {
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
      btn.classList.add('active');
      const panel = document.getElementById('tab-' + btn.dataset.tab);
      if (panel) panel.classList.add('active');

      if (btn.dataset.tab === 'board' && state.board) {
        renderDraftBoard(state.board);
      }
    });
  });
}

// ── Modal close ───────────────────────────────────────────────────────────

function initModal() {
  document.getElementById('modal-close').addEventListener('click', closeModal);
  document.getElementById('modal-overlay').addEventListener('click', e => {
    if (e.target === e.currentTarget) closeModal();
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeModal();
  });
}

// ── Init ──────────────────────────────────────────────────────────────────

async function init() {
  initTabs();
  initModal();
  await loadStatus();
  await loadTeams();
  setInterval(async () => { await loadStatus(); }, 5 * 60 * 1000);
}

init();
