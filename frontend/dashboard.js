// ════════ STATE ════════
const urlParams = new URLSearchParams(window.location.search);
const clientId = urlParams.get('client_id');
if(!clientId) window.location.href = 'clients.html';

let currentTab = 'overview';
let currentAccountId = null; // null = Family

let apiData = {
    overview: null,
    portfolio: null,
    performance: null,
    risk: null,
    insights: null
};

// ════════ INITIALIZATION ════════
async function initDashboard() {
    await fetchAllData();
    setupSidebar();
    renderDashboard();
}

async function fetchAllData() {
    const actParam = currentAccountId ? `?account_id=${currentAccountId}` : '';
    
    // Fetch all concurrently
    const [oRes, pRes, pfRes, rRes, iRes] = await Promise.all([
        fetch(`/api/overview/${clientId}${actParam}`),
        fetch(`/api/portfolio/${clientId}${actParam}`),
        fetch(`/api/performance/${clientId}${actParam}`),
        fetch(`/api/risk/${clientId}${actParam}`),
        fetch(`/api/insights/${clientId}${actParam}`)
    ]);

    apiData.overview = await oRes.json();
    apiData.portfolio = await pRes.json();
    apiData.performance = await pfRes.json();
    apiData.risk = await rRes.json();
    apiData.insights = await iRes.json();
}

// ════════ UI UPDATES ════════
function formatCr(val) {
    if(!val) return '₹0.00';
    if(val >= 10000000) return `₹${(val/10000000).toFixed(2)} Cr`;
    if(val >= 100000) return `₹${(val/100000).toFixed(2)} L`;
    return `₹${val.toLocaleString()}`;
}

function renderDashboard() {
    // 1. Update Header / Dropdown
    const mainEl = document.querySelector('.mn');
    
    // Update active state in sidebar
    document.querySelectorAll('.sb-nav a').forEach(a => {
        a.classList.remove('act');
        if(a.innerText.toLowerCase().includes(currentTab.toLowerCase())) a.classList.add('act');
    });

    // Generate the standard header
    const headerHtml = `
        <div class="hdr">
          <div><h1 style="text-transform: capitalize;">${currentTab}</h1><p>Your Wealth, Our Focus.</p></div>
          <div class="hdr-r">
            <div class="hdr-i"><svg viewBox="0 0 24 24"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg><strong>${apiData.overview.client_name}</strong></div>
            <select class="acct" onchange="changeAccount(this.value)">
              <option value="family" ${currentAccountId === null ? 'selected' : ''}>Family Portfolio ▾</option>
              ${apiData.overview.accounts.map(a => `<option value="${a.id}" ${currentAccountId == a.id ? 'selected' : ''}>${a.label}</option>`).join('')}
            </select>
          </div>
        </div>
    `;

    // Render Tab Content
    let contentHtml = '';
    if(currentTab === 'overview') contentHtml = renderOverview();
    else if(currentTab === 'portfolio') contentHtml = renderPortfolio();
    else if(currentTab === 'performance') contentHtml = renderPerformance();
    else if(currentTab === 'risk') contentHtml = renderRisk();
    else if(currentTab === 'insights') contentHtml = renderInsights();
    else contentHtml = `<div class="crd"><p>Work in progress.</p></div>`;

    mainEl.innerHTML = headerHtml + contentHtml;
}

// ════════ TAB RENDERERS ════════
function renderOverview() {
    const o = apiData.overview;
    const pf = apiData.performance;
    const p = apiData.portfolio;

    // KPI Cards
    const kpis = `
    <div class="kpis">
      <div class="kpi">
        <div class="kpi-top"><span class="kpi-lb">Portfolio Value</span><div class="kpi-ic ic1"><svg viewBox="0 0 24 24"><rect x="2" y="6" width="20" height="14" rx="2"/><path d="M2 10h20"/></svg></div></div>
        <div class="kpi-v">${formatCr(o.total_value)}</div><div class="kpi-d ${o.return_pct >= 0 ? 'pos' : 'neu'}">${o.return_pct >= 0 ? '▲' : '▼'} ${o.return_pct.toFixed(2)}% vs. Invested</div>
      </div>
      <div class="kpi">
        <div class="kpi-top"><span class="kpi-lb">Invested Value</span><div class="kpi-ic ic2"><svg viewBox="0 0 24 24"><path d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg></div></div>
        <div class="kpi-v">${formatCr(o.invested_value)}</div><div class="kpi-d neu">&nbsp;</div>
      </div>
      <div class="kpi">
        <div class="kpi-top"><span class="kpi-lb">Total Gain</span><div class="kpi-ic ic3"><svg viewBox="0 0 24 24"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/></svg></div></div>
        <div class="kpi-v">${formatCr(o.total_gain)}</div><div class="kpi-d ${o.total_gain >= 0 ? 'pos' : 'tn'}">${o.total_gain >= 0 ? '▲' : '▼'}</div>
      </div>
      <div class="kpi">
        <div class="kpi-top"><span class="kpi-lb">Portfolio Return</span><div class="kpi-ic ic2"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M8 12h8"/><path d="M12 8v8"/></svg></div></div>
        <div class="kpi-v">${o.return_pct.toFixed(2)}%</div><div class="kpi-d neu">vs. Cost</div>
      </div>
      <div class="kpi">
        <div class="kpi-top"><span class="kpi-lb">Portfolio Health</span><div class="kpi-ic ic3"><svg viewBox="0 0 24 24"><path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78L12 21.23l8.84-8.84a5.5 5.5 0 0 0 0-7.78z"/></svg></div></div>
        <div class="kpi-v">${o.health_score} / 100</div><div class="hbar"><div class="hfill" style="width:${o.health_score}%"></div></div>
      </div>
    </div>`;

    // Asset Allocation
    const eqPct = o.asset_allocation.find(a => a.class === 'Equity')?.pct || 0;
    const debtPct = o.asset_allocation.find(a => a.class === 'Debt')?.pct || 0;
    const otherPct = 100 - eqPct - debtPct;
    
    const circ = 314.159;
    const eqDash = circ * (eqPct / 100);
    const debtDash = circ * (debtPct / 100);
    
    const eqOffset = 0;
    const debtOffset = -eqDash;
    
    // Top Holdings
    const topHoldingsRows = pf.top_performers.slice(0,5).map(h => `
        <tr><td><div class="hn"><span class="hl">${h.security_name.substring(0,25)}</span></div></td>
        <td class="tv">${formatCr(h.gain_value)} Gain</td><td class="tw">${h.weight_pct}%</td><td class="tg tp">+${h.gain_pct.toFixed(2)}%</td></tr>
    `).join('');

    // Sector Allocation
    const sectorsHtml = o.sector_allocation.slice(0,6).map(s => `
        <div class="sr"><span class="sn">${s.sector}</span><div class="sb2"><div class="sf" style="width:${s.pct}%"></div></div><span class="sp">${s.pct.toFixed(1)}%</span></div>
    `).join('');

    // Insights
    const insHtml = apiData.insights.slice(0,3).map(i => `
        <div class="ins">
            <div class="iic ${i.type==='danger'?'ib':(i.type==='success'?'ig':'iw')}"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/></svg></div>
            <div><div class="it">${i.title}</div><div class="id">${i.description}</div></div>
        </div>
    `).join('');

    return `
    ${kpis}
    
    <!-- AI Narrative -->
    ${o.narrative ? `<div class="ban" style="margin-bottom:18px;background:#fff;border:1px solid #e8ecf1;color:#1e293b"><strong style="color:#2563eb">AI Summary:</strong> &nbsp; ${o.narrative}</div>` : ''}

    <div class="mid">
      <div class="crd">
        <div class="crd-h"><div class="crd-t">Asset Allocation</div></div>
        <div class="don-area">
          <div class="don-wrap"><svg viewBox="0 0 120 120">
            <circle cx="60" cy="60" r="50" fill="none" stroke="#f1f5f9" stroke-width="16"/>
            <circle cx="60" cy="60" r="50" fill="none" stroke="#2563eb" stroke-width="16" stroke-dasharray="${eqDash} ${circ}" stroke-dashoffset="${eqOffset}" stroke-linecap="round"/>
            <circle cx="60" cy="60" r="50" fill="none" stroke="#10b981" stroke-width="16" stroke-dasharray="${debtDash} ${circ}" stroke-dashoffset="${debtOffset}" stroke-linecap="round"/>
          </svg><div class="don-ctr"><b>${formatCr(o.total_value)}</b></div></div>
          <div class="leg">
            <div class="leg-r"><div class="leg-d" style="background:#2563eb"></div><span class="leg-n">Equity</span><span class="leg-p">${eqPct.toFixed(2)}%</span></div>
            <div class="leg-r"><div class="leg-d" style="background:#10b981"></div><span class="leg-n">Debt/Cash</span><span class="leg-p">${debtPct.toFixed(2)}%</span></div>
          </div>
        </div>
      </div>
      
      <div class="crd">
        <div class="crd-h"><div class="crd-t">Top Winners (by Gain)</div></div>
        <table class="ht">
          <thead><tr><th>Security</th><th>Gain Value</th><th>Weight</th><th>Gain %</th></tr></thead>
          <tbody>${topHoldingsRows}</tbody>
        </table>
      </div>
    </div>

    <div class="bot">
      <div class="crd">
        <div class="crd-h"><div class="crd-t">Sector Allocation</div></div>
        <div style="padding-top:4px">${sectorsHtml}</div>
      </div>

      <div class="crd">
        <div class="crd-h"><div class="crd-t">AI Portfolio Insights</div></div>
        <div>${insHtml}</div>
      </div>
    </div>
    `;
}

function renderPortfolio() {
    const hList = apiData.portfolio.holdings;
    const rows = hList.map(h => `
        <tr>
            <td><div class="hn"><span class="hl">${h.security_name}</span></div></td>
            <td class="tw">${h.account_label}</td>
            <td class="tv">${formatCr(h.current_value)}</td>
            <td class="tw">${h.weight_pct}%</td>
            <td class="tw">${formatCr(h.total_cost)}</td>
            <td class="tg ${h.gain_pct >= 0 ? 'tp' : 'tn'}">${h.gain_pct >= 0 ? '+' : ''}${h.gain_pct.toFixed(2)}%</td>
        </tr>
    `).join('');

    return `
    <div class="crd">
      <div class="crd-h"><div class="crd-t">Complete Holdings (${apiData.portfolio.total_holdings})</div></div>
      <table class="ht" style="width:100%">
        <thead><tr><th>Security Name</th><th>Account</th><th>Market Value</th><th>Weight</th><th>Cost</th><th>Gain %</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    `;
}

function renderPerformance() {
    const pf = apiData.performance;
    return `
    <!-- AI Narrative -->
    ${pf.performance_summary ? `<div class="ban" style="margin-bottom:18px;background:#fff;border:1px solid #e8ecf1;color:#1e293b"><strong style="color:#2563eb">AI Performance Summary:</strong> &nbsp; ${pf.performance_summary} <br><br> <strong>Top Winner Insight:</strong> ${pf.top_performer_insight}</div>` : ''}

    <div class="mid" style="grid-template-columns: 1fr 1fr;">
      <div class="crd">
        <div class="crd-h"><div class="crd-t">Worst Performers (Drags)</div></div>
        <table class="ht">
          <thead><tr><th>Security</th><th>Loss Value</th><th>Weight</th><th>Loss %</th></tr></thead>
          <tbody>${pf.worst_performers.map(h => `
              <tr><td><div class="hn"><span class="hl">${h.security_name.substring(0,30)}</span></div></td>
              <td class="tv">${formatCr(h.gain_value)}</td><td class="tw">${h.weight_pct}%</td><td class="tg tn">${h.gain_pct.toFixed(2)}%</td></tr>
          `).join('')}</tbody>
        </table>
      </div>
      <div class="crd">
        <div class="crd-h"><div class="crd-t">Performance Metrics</div></div>
        <div style="display:flex; flex-direction:column; gap: 15px;">
            <div class="kpi-top" style="border-bottom: 1px solid #f1f5f9; padding-bottom: 10px;">
                <span class="kpi-lb">Gainers vs Losers</span>
                <b style="font-size:16px"><span style="color:#059669">${pf.gainers_count}</span> / <span style="color:#dc2626">${pf.losers_count}</span></b>
            </div>
            <div class="kpi-top" style="border-bottom: 1px solid #f1f5f9; padding-bottom: 10px;">
                <span class="kpi-lb">Weighted Avg Return</span>
                <b style="font-size:16px">${pf.weighted_avg_return.toFixed(2)}%</b>
            </div>
            <div class="kpi-top" style="border-bottom: 1px solid #f1f5f9; padding-bottom: 10px;">
                <span class="kpi-lb">Biggest Absolute Gainer</span>
                <b style="font-size:14px; color:#059669">${pf.biggest_absolute_gain ? pf.biggest_absolute_gain.security_name : 'N/A'}</b>
            </div>
        </div>
      </div>
    </div>
    `;
}

function renderRisk() {
    const r = apiData.risk;
    return `
    ${r.risk_summary ? `<div class="ban" style="margin-bottom:18px;background:#fff;border:1px solid #e8ecf1;color:#1e293b"><strong style="color:#2563eb">AI Risk Summary:</strong> &nbsp; ${r.risk_summary}</div>` : ''}
    
    <div class="mid" style="grid-template-columns: 1fr 1fr;">
      <div class="crd">
        <div class="crd-h"><div class="crd-t">Concentration Risk</div></div>
        <div class="kpi-top" style="margin-bottom:15px"><span class="kpi-lb">HHI Index</span><b style="font-size:18px;color:${r.hhi_label === 'High' ? '#dc2626' : '#1e293b'}">${r.hhi.toFixed(4)} (${r.hhi_label})</b></div>
        <div class="kpi-top" style="margin-bottom:15px"><span class="kpi-lb">Top 5 Concentration</span><b style="font-size:18px">${r.concentration.top5_pct.toFixed(2)}%</b></div>
        <div class="kpi-top" style="margin-bottom:15px"><span class="kpi-lb">Top 10 Concentration</span><b style="font-size:18px">${r.concentration.top10_pct.toFixed(2)}%</b></div>
      </div>

      <div class="crd">
        <div class="crd-h"><div class="crd-t">Cross-Account Overlaps</div></div>
        <table class="ht">
          <thead><tr><th>Security</th><th>Total Exposure</th><th>Found In</th></tr></thead>
          <tbody>${r.cross_account_overlaps.map(o => `
              <tr><td><span class="hl" style="color:#dc2626">${o.security.substring(0,30)}</span></td>
              <td class="tv">${o.total_pct.toFixed(2)}%</td>
              <td class="tw">${o.accounts.length} accounts</td></tr>
          `).join('')}
          ${r.cross_account_overlaps.length === 0 ? '<tr><td colspan="3">No overlaps found across accounts.</td></tr>' : ''}
          </tbody>
        </table>
      </div>
    </div>
    `;
}

function renderInsights() {
    return `
    <div class="crd">
      <div class="crd-h"><div class="crd-t">AI & Algorithmic Insights</div></div>
      <div>
        ${apiData.insights.map(i => `
        <div class="ins" style="padding: 16px 0; border-bottom: 1px solid #f1f5f9;">
            <div class="iic ${i.type==='danger'?'ib':(i.type==='success'?'ig':(i.type==='warning'?'iw':''))}" style="width:36px;height:36px;"><svg viewBox="0 0 24 24" style="width:20px;height:20px"><circle cx="12" cy="12" r="10"/></svg></div>
            <div style="margin-left: 6px;"><div class="it" style="font-size:14px; margin-bottom:4px">${i.title}</div><div class="id" style="font-size:13px">${i.description}</div></div>
        </div>
        `).join('')}
      </div>
    </div>
    `;
}

// ════════ EVENT LISTENERS ════════
document.querySelectorAll('.sb-nav a').forEach(a => {
    a.addEventListener('click', function(e) {
        e.preventDefault();
        const navTarget = this.innerText.trim().toLowerCase().split(' ')[0]; // overview, portfolio, performance, risk, insights
        if(navTarget === 'reports') { alert("Reports coming soon"); return; }
        currentTab = navTarget;
        renderDashboard();
    });
});

async function changeAccount(val) {
    currentAccountId = val === 'family' ? null : parseInt(val);
    await fetchAllData();
    renderDashboard();
}

// Start
initDashboard();
