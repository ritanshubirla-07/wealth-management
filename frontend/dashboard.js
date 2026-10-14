// ════════ STATE ════════
const urlParams = new URLSearchParams(window.location.search);
let clientId = urlParams.get('client_id');
let currentUser = null;

const DASHBOARD_TABS = new Set(['overview', 'portfolio', 'performance', 'risk', 'insights']);
let currentTab = DASHBOARD_TABS.has(window.location.hash.slice(1)) ? window.location.hash.slice(1) : 'overview';
let currentAccountId = null; // null = Family
let healthThreeInstance = null;
let healthOutsideClickHandler = null;
let selectedHealthPillar = null;
let portfolioHealthRequestId = 0;
let portfolioHealthRequest = null;
let currentOverviewScorecardData = null;
const portfolioScorecardCache = new Map();
const scorecardInstances = new Map();
const scorecardGroupSelections = {};
const SCORECARD_API_URL = `${window.location.protocol}//${window.location.hostname}:8050/api/scorecard`;
const HEALTH_PILLARS = ['Growth', 'Quality', 'Valuation'];

const HEALTH_COLORS = {
  Growth: '#3573c6',
  Quality: '#17916b',
  Valuation: '#c8862a',
  'Overall health': '#1c3b32'
};

let apiData = {
    overview: null,
    portfolio: null,
    performance: null,
    risk: null,
    insights: null,
    portfolio_scorecard_health: null
};

// ════════ INITIALIZATION ════════
function updateSidebarProfile() {
    const nameEl = document.getElementById('sidebar-user-name');
    const roleEl = document.getElementById('sidebar-user-role');
    if (!nameEl || !roleEl) return;

    const displayName = currentUser?.role === 'admin'
        ? (currentUser.name || 'Administrator')
        : (apiData.overview?.client_name || currentUser?.name || 'Client');

    const roleText = currentUser?.role === 'admin'
        ? 'Admin Portal'
        : (currentUser?.client_id ? `Client #${currentUser.client_id}` : 'Client Portal');

    nameEl.textContent = displayName;
    roleEl.textContent = roleText;
}

async function initDashboard() {
  const authResponse = await fetch('/api/auth/me');
  if (!authResponse.ok) { window.location.replace('login.html'); return; }
  currentUser = await authResponse.json();
  if (currentUser.role === 'client') clientId = String(currentUser.client_id);
  if (!clientId) { window.location.replace('admin.html'); return; }
    await fetchAllData();
  document.querySelector('.mn').style.visibility = 'visible';
    updateSidebarProfile();
    renderDashboard();
}

async function fetchAllData() {
  portfolioHealthRequestId += 1;
  portfolioHealthRequest = null;
    const actParam = currentAccountId ? `?account=${currentAccountId}` : '';
    
    // Check overview first to see if it's still processing
    const oResInit = await fetch(`/api/overview/${clientId}${actParam}`);
    let oDataInit = await oResInit.json();
    
    if (oDataInit.is_processing) {
        document.querySelector('.mn').innerHTML = `
            <div style="display:flex; flex-direction:column; align-items:center; justify-content:center; height:100vh; color:#64748b;">
                <div style="width:40px;height:40px;border:3px solid #e2e8f0;border-top-color:#2563eb;border-radius:50%;animation:spin 1s linear infinite;margin-bottom:16px;"></div>
                <style>@keyframes spin { to { transform: rotate(360deg); } }</style>
                <div style="font-size:16px;font-weight:500">AI is analyzing statements...</div>
                <div style="font-size:14px;margin-top:8px">This takes about 10-20 seconds for large portfolios.</div>
            </div>`;
        
        while (oDataInit.is_processing) {
            await new Promise(r => setTimeout(r, 2500));
            const pollRes = await fetch(`/api/overview/${clientId}${actParam}`);
            oDataInit = await pollRes.json();
        }
    }
    
    // Now fetch everything else concurrently
    const [pRes, pfRes, rRes, iRes] = await Promise.all([
        fetch(`/api/portfolio/${clientId}${actParam}`),
        fetch(`/api/performance/${clientId}${actParam}`),
        fetch(`/api/risk/${clientId}${actParam}`),
        fetch(`/api/insights/${clientId}${actParam}`)
    ]);

    apiData.overview = oDataInit;
    apiData.portfolio = await pRes.json();
    apiData.performance = await pfRes.json();
    apiData.risk = await rRes.json();
    apiData.insights = await iRes.json();
    apiData.portfolio_scorecard_health = null;
}

function scorecardIdentifier(holding) {
  return String(holding.isin || holding.nse_symbol || holding.security_name || '').trim();
}

function stockReportUrl(holding, index) {
  const account = currentAccountId == null ? 'family' : String(currentAccountId);
  const listKey = `vikabh-stock-list:${clientId}:${account}`;
  const holdings = apiData.portfolio?.holdings || [];
  try {
    sessionStorage.setItem(listKey, JSON.stringify(holdings.map(item => ({
      security_name: item.security_name,
      isin: item.isin,
      nse_symbol: item.nse_symbol,
      sector: item.sector,
      sector_group: item.sector_group,
      industry: item.industry,
      effective_value: item.effective_value,
      weight_pct: item.weight_pct,
      cmp: item.cmp,
      quantity: item.quantity
    }))));
  } catch (_error) {
    // The report still opens for the selected stock when storage is unavailable.
  }

  const params = new URLSearchParams({
    identifier: scorecardIdentifier(holding),
    name: holding.security_name || '',
    client_id: String(clientId || ''),
    account,
    source_tab: currentTab,
    list_key: listKey,
    index: String(index)
  });
  return `stock-report.html?${params.toString()}`;
}

function openPortfolioStock(index) {
  const holding = (apiData.portfolio?.holdings || [])[Number(index)];
  if (!holding) return;
  window.location.href = stockReportUrl(holding, Number(index));
}

function requestScorecard(holding) {
  const identifier = scorecardIdentifier(holding);
  if (!identifier) return Promise.resolve(null);
  const key = identifier.toUpperCase();
  if (portfolioScorecardCache.has(key)) return portfolioScorecardCache.get(key);

  const request = (async () => {
    try {
      const response = await fetch(`${SCORECARD_API_URL}?identifier=${encodeURIComponent(identifier)}`);
      if (!response.ok) return null;
      const payload = await response.json();
      return payload.scorecard && Array.isArray(payload.scorecard.groups) ? payload : null;
    } catch (_error) {
      return null;
    }
  })();
  portfolioScorecardCache.set(key, request);
  return request;
}

function aggregatePortfolioScorecards(holdings, results, signature) {
  const totalValue = holdings.reduce((total, holding) => total + Math.max(0, Number(holding.effective_value) || 0), 0);
  const groupTotals = Object.fromEntries(HEALTH_PILLARS.map(name => [name, {weightedPct: 0, value: 0, criteria: new Map()}]));
  let scoredValue = 0;
  let weightedTotal = 0;
  let scoredHoldings = 0;

  results.forEach(({holding, payload}) => {
    const value = Math.max(0, Number(holding.effective_value) || 0);
    const scorecard = payload?.scorecard;
    if (!scorecard || value <= 0 || !(Number(scorecard.max_total) > 0)) return;

    scoredValue += value;
    scoredHoldings += 1;
    weightedTotal += (Number(scorecard.total_score) / Number(scorecard.max_total)) * 100 * value;

    (scorecard.groups || []).forEach(group => {
      const target = groupTotals[group.name];
      const maxScore = Number(group.max_score);
      if (!target || !(maxScore > 0)) return;
      target.weightedPct += (Number(group.score) / maxScore) * 100 * value;
      target.value += value;
      (group.criteria || []).forEach(criterion => {
        const points = Number(criterion.pts);
        const score = Number(criterion.score);
        if (!(points > 0) || !Number.isFinite(score)) return;
        const item = target.criteria.get(criterion.criterion) || {weightedPct: 0, value: 0, companies: 0};
        item.weightedPct += (score / points) * 100 * value;
        item.value += value;
        item.companies += 1;
        target.criteria.set(criterion.criterion, item);
      });
    });
  });

  const groups = Object.fromEntries(HEALTH_PILLARS.map(name => {
    const group = groupTotals[name];
    return [name, {
      percentage: group.value ? Number((group.weightedPct / group.value).toFixed(1)) : null,
      coverage_pct: totalValue ? Number((group.value / totalValue * 100).toFixed(1)) : 0,
      criteria: [...group.criteria.entries()].map(([criterion, item]) => ({
        criterion,
        percentage: Number((item.weightedPct / item.value).toFixed(1)),
        coverage_pct: totalValue ? Number((item.value / totalValue * 100).toFixed(1)) : 0,
        companies: item.companies
      }))
    }];
  }));

  return {
    status: 'ready',
    signature,
    score_pct: scoredValue ? Number((weightedTotal / scoredValue).toFixed(1)) : null,
    scored_weight_pct: totalValue ? Number((scoredValue / totalValue * 100).toFixed(1)) : 0,
    scored_holdings: scoredHoldings,
    unscored_holdings: Math.max(0, holdings.length - scoredHoldings),
    groups
  };
}

function loadPortfolioScorecardHealth() {
  const holdings = apiData.portfolio?.holdings || [];
  const signature = `${clientId}|${currentAccountId || 'family'}|${holdings.map(h => `${scorecardIdentifier(h)}:${h.effective_value || 0}`).join(';')}`;
  if (apiData.portfolio_scorecard_health?.signature === signature && apiData.portfolio_scorecard_health.status === 'ready') return;
  if (portfolioHealthRequest?.signature === signature) return;

  const requestId = ++portfolioHealthRequestId;
  portfolioHealthRequest = {signature};
  apiData.portfolio_scorecard_health = {status: 'loading', signature};
  Promise.all(holdings.map(async holding => ({holding, payload: await requestScorecard(holding)})))
    .then(results => {
      if (requestId !== portfolioHealthRequestId) return;
      apiData.portfolio_scorecard_health = aggregatePortfolioScorecards(holdings, results, signature);
      portfolioHealthRequest = null;
      if (currentTab === 'overview') renderDashboard();
    });
}

// ════════ UI UPDATES ════════
function formatCr(val) {
    if(val == null) return '-';
    if(val >= 10000000) return `₹${(val/10000000).toFixed(2)} Cr`;
    if(val >= 100000) return `₹${(val/100000).toFixed(2)} L`;
    return `₹${val.toLocaleString()}`;
}

function renderDashboard() {
    updateSidebarProfile();

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
            <div class="hdr-i"><svg viewBox="0 0 24 24"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg><strong>${apiData.overview.client_name || 'Client'}</strong></div>
            <button class="acct" onclick="window.location.href='upload.html${currentUser.role === 'admin' ? `?client_id=${clientId}` : ''}'">Add statements</button>
            <select class="acct" onchange="changeAccount(this.value)">
              <option value="family" ${currentAccountId === null ? 'selected' : ''}>Family Portfolio ▾</option>
              ${(apiData.overview.accounts || []).map(a => `<option value="${a.id}" ${currentAccountId == a.id ? 'selected' : ''}>${a.label}</option>`).join('')}
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

    destroyHealthTriangle();
    mainEl.innerHTML = headerHtml + contentHtml;
    if(currentTab === 'overview' || currentTab === 'performance') {
        setTimeout(initChart, 50); // allow DOM to settle
    }
    if (currentTab === 'overview') {
      initHealthTriangle();
      loadPortfolioScorecardHealth();
    }
}

// ════════ CHART.JS LOGIC ════════
let myChart = null;

function initChart() {
  if (typeof Chart === 'undefined') {
    setTimeout(initChart, 200);
    return;
  }
  const o = apiData.overview;
  const canvas = document.getElementById('pc');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  if (myChart) myChart.destroy();

  const targetReturn = o.return_pct || 15.0;
  const benchmarkReturn = targetReturn * 0.7;
  const portfolioData = [];
  const benchmarkData = [];
  for (let i = 0; i < 12; i += 1) {
    const progress = i / 11;
    const curve = Math.pow(progress, 1.2);
    const portfolioNoise = (Math.random() - 0.5) * (targetReturn * 0.15);
    const benchmarkNoise = (Math.random() - 0.5) * (benchmarkReturn * 0.15);
    if (i === 0) {
      portfolioData.push(0);
      benchmarkData.push(0);
    } else if (i === 11) {
      portfolioData.push(targetReturn);
      benchmarkData.push(benchmarkReturn);
    } else {
      portfolioData.push(targetReturn * curve + portfolioNoise);
      benchmarkData.push(benchmarkReturn * curve + benchmarkNoise);
    }
  }
  const gradient = ctx.createLinearGradient(0, 0, 0, 300);
  gradient.addColorStop(0, 'rgba(37,99,235,0.15)');
  gradient.addColorStop(1, 'rgba(37,99,235,0)');
  myChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: ['Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec', 'Jan', 'Feb', 'Mar'],
      datasets: [
        {label: 'Portfolio', data: portfolioData, borderColor: '#2563eb', borderWidth: 2.5, backgroundColor: gradient, fill: true, tension: 0.4, pointRadius: 0, pointHoverRadius: 6},
        {label: 'Benchmark (Nifty 50)', data: benchmarkData, borderColor: '#94a3b8', borderWidth: 2, borderDash: [4, 4], tension: 0.4, pointRadius: 0, pointHoverRadius: 4}
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: {mode: 'index', intersect: false},
      scales: {
        x: {grid: {color: 'rgba(0,0,0,.04)', drawBorder: false}, ticks: {color: '#94a3b8', font: {size: 9.5}}, border: {display: false}},
        y: {grid: {color: 'rgba(0,0,0,.04)', drawBorder: false}, ticks: {color: '#94a3b8', font: {size: 9.5}, callback: value => value + '%'}, border: {display: false}}
      },
      plugins: {legend: {display: false}}
    }
  });
}

function initHealthTriangle() {
  const stage = document.getElementById('health-three-stage');
  const host = document.getElementById('health-three-canvas');
  if (!stage || !host) return;

  document.querySelectorAll('.health-three-marker, .health-pillar-control').forEach(button => {
    button.addEventListener('click', () => selectHealthPillar(button.dataset.healthSelect));
  });
  const detail = document.getElementById('health-detail');
  if (detail) {
    detail.addEventListener('click', event => {
      if (event.target.closest('.health-detail-close')) closeHealthDetail();
    });
    closeHealthDetail();
  }
  if (healthOutsideClickHandler) document.removeEventListener('click', healthOutsideClickHandler);
  healthOutsideClickHandler = event => {
    const layout = document.getElementById('health-scene-layout');
    const target = event.target;
    if (!layout?.classList.contains('has-detail') || !(target instanceof Element)) return;
    if (target.closest('.health-detail') || target.closest('[data-health-select]')) return;
    closeHealthDetail();
  };
  document.addEventListener('click', healthOutsideClickHandler);

  if (!window.THREE) {
    host.innerHTML = '<div class="health-three-fallback">The 3D health view is unavailable.</div>';
    return;
  }

  const THREE = window.THREE;
  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({alpha: true, antialias: true});
  } catch (error) {
    console.error('Could not create the health scene:', error);
    host.innerHTML = '<div class="health-three-fallback">The 3D health view is unavailable.</div>';
    return;
  }

  const portfolioHealth = apiData.portfolio_scorecard_health || {};
  const scores = Object.fromEntries(HEALTH_PILLARS.map(name => [name, portfolioHealth.groups?.[name]?.percentage ?? null]));
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(36, 1, 0.1, 60);
  camera.position.set(0, 0, 6.8);
  const group = new THREE.Group();
  group.scale.setScalar(1);
  group.position.y = 0;
  scene.add(group);
  scene.add(new THREE.HemisphereLight(0xe8fff4, 0x315247, 1.8));
  const keyLight = new THREE.DirectionalLight(0xffffff, 2.3);
  keyLight.position.set(-3, 4, 6);
  scene.add(keyLight);
  const rimLight = new THREE.PointLight(0x54d9a6, 1.4, 12);
  rimLight.position.set(3, -1, -2);
  scene.add(rimLight);

  const vertices = [
    new THREE.Vector3(0, 1.55, 0.08),
    new THREE.Vector3(1.35, -0.78, 0.08),
    new THREE.Vector3(-1.35, -0.78, 0.08)
  ];
  const backVertices = vertices.map(point => new THREE.Vector3(point.x, point.y, -0.34));
  const faceShape = new THREE.Shape();
  faceShape.moveTo(vertices[0].x, vertices[0].y);
  faceShape.lineTo(vertices[1].x, vertices[1].y);
  faceShape.lineTo(vertices[2].x, vertices[2].y);
  faceShape.closePath();
  const faceGeometry = new THREE.ShapeGeometry(faceShape);
  const faceMaterial = new THREE.MeshStandardMaterial({
    color: 0x16725f, transparent: true, opacity: 0.12,
    roughness: 0.42, metalness: 0.12, side: THREE.DoubleSide, depthWrite: false
  });
  const frontFace = new THREE.Mesh(faceGeometry, faceMaterial);
  frontFace.position.z = 0.08;
  group.add(frontFace);
  const backFace = new THREE.Mesh(faceGeometry, faceMaterial.clone());
  backFace.position.z = -0.34;
  group.add(backFace);

  const frameMaterial = new THREE.MeshStandardMaterial({color: 0x568f7d, roughness: 0.3, metalness: 0.55});
  const edgeMaterial = new THREE.MeshStandardMaterial({color: 0xb6dfcd, roughness: 0.32, metalness: 0.35});
  const addBeam = (start, end, radius, material) => {
    const direction = end.clone().sub(start);
    const beam = new THREE.Mesh(
      new THREE.CylinderGeometry(radius, radius, direction.length(), 10), material
    );
    beam.position.copy(start).add(end).multiplyScalar(0.5);
    beam.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), direction.normalize());
    group.add(beam);
  };
  for (let index = 0; index < 3; index += 1) {
    const next = (index + 1) % 3;
    addBeam(vertices[index], vertices[next], 0.025, frameMaterial);
    addBeam(backVertices[index], backVertices[next], 0.018, edgeMaterial);
    addBeam(vertices[index], backVertices[index], 0.017, edgeMaterial);
  }

  const markerPositions = {};
  const addGauge = (name, position, score, isCore = false) => {
    const color = new THREE.Color(HEALTH_COLORS[name]);
    const gauge = new THREE.Group();
    gauge.position.copy(position);
    group.add(gauge);
    const safeScore = score == null ? 0 : Math.max(0, Math.min(100, Number(score)));
    const material = new THREE.MeshStandardMaterial({
      color,
      roughness: 0.2,
      metalness: 0.34,
      emissive: color,
      emissiveIntensity: 0.14 + safeScore / 500
    });
    const sphere = new THREE.Mesh(new THREE.SphereGeometry(isCore ? 0.22 : 0.17, 28, 20), material);
    gauge.add(sphere);
    markerPositions[name] = position.clone();
  };

  addGauge('Growth', vertices[0], scores.Growth);
  addGauge('Quality', vertices[1], scores.Quality);
  addGauge('Valuation', vertices[2], scores.Valuation);
  const overallPosition = new THREE.Vector3(0, 0, 0.34);
  addGauge('Overall health', overallPosition, portfolioHealth.score_pct, true);
  markerPositions['Overall health'].z += 0.48;
  markerPositions.Growth.y += 0.43;
  markerPositions.Quality.x += 0.4;
  markerPositions.Quality.y -= 0.28;
  markerPositions.Valuation.x -= 0.4;
  markerPositions.Valuation.y -= 0.28;

  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setClearColor(0x000000, 0);
  renderer.outputEncoding = THREE.sRGBEncoding;
  renderer.domElement.className = 'health-three-renderer';
  renderer.domElement.setAttribute('aria-hidden', 'true');
  host.replaceChildren(renderer.domElement);

  const markers = [...stage.querySelectorAll('[data-health-marker]')];
  markers.forEach(marker => marker.style.setProperty('--marker-color', HEALTH_COLORS[marker.dataset.healthMarker]));
  const resize = () => {
    const width = Math.max(1, stage.clientWidth);
    const height = Math.max(1, stage.clientHeight);
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.position.z = Math.max(6.8, 2.1 / (Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)) * camera.aspect));
    camera.updateProjectionMatrix();
  };
  resize();
  const resizeObserver = new ResizeObserver(resize);
  resizeObserver.observe(stage);
  window.addEventListener('resize', resize);

  let dragging = false;
  let hovered = false;
  let lastX = 0;
  const canvas = renderer.domElement;
  stage.addEventListener('pointerenter', () => { hovered = true; });
  stage.addEventListener('pointerleave', () => { hovered = false; });
  canvas.addEventListener('pointerdown', event => {
    dragging = true;
    lastX = event.clientX;
    canvas.setPointerCapture(event.pointerId);
    canvas.style.cursor = 'grabbing';
  });
  canvas.addEventListener('pointermove', event => {
    if (!dragging) return;
    const dx = event.clientX - lastX;
    lastX = event.clientX;
    group.rotation.z -= Math.abs(dx) * 0.006;
  });
  const releasePointer = () => {
    if (!dragging) return;
    dragging = false;
    canvas.style.cursor = 'grab';
  };
  canvas.addEventListener('pointerup', releasePointer);
  canvas.addEventListener('pointercancel', () => {
    dragging = false;
    canvas.style.cursor = 'grab';
  });
  canvas.style.cursor = 'grab';

  healthThreeInstance = {scene, renderer, resizeObserver, resize, frame: 0};
  let previousTime = performance.now();
  const updateMarkers = () => {
    group.updateMatrixWorld(true);
    camera.updateMatrixWorld();
    markers.forEach(marker => {
      const position = markerPositions[marker.dataset.healthMarker].clone();
      position.applyMatrix4(group.matrixWorld).project(camera);
      const projectedX = (position.x * 0.5 + 0.5) * stage.clientWidth;
      const projectedY = (-position.y * 0.5 + 0.5) * stage.clientHeight;
      const insetX = marker.offsetWidth / 2 + 5;
      const insetY = marker.offsetHeight / 2 + 5;
      marker.style.left = `${Math.max(insetX, Math.min(stage.clientWidth - insetX, projectedX))}px`;
      marker.style.top = `${Math.max(insetY, Math.min(stage.clientHeight - insetY, projectedY))}px`;
      marker.style.opacity = position.z > 1 ? '0' : '1';
    });
  };
  const animate = now => {
    if (!healthThreeInstance) return;
    healthThreeInstance.frame = requestAnimationFrame(animate);
    const delta = Math.min((now - previousTime) / 1000, 0.05);
    previousTime = now;
    if (!dragging && !hovered) group.rotation.z -= delta * 0.28;
    renderer.render(scene, camera);
    updateMarkers();
  };
  healthThreeInstance.frame = requestAnimationFrame(animate);
}

function destroyHealthTriangle() {
  if (healthOutsideClickHandler) {
    document.removeEventListener('click', healthOutsideClickHandler);
    healthOutsideClickHandler = null;
  }
  if (!healthThreeInstance) return;
  const instance = healthThreeInstance;
  healthThreeInstance = null;
  cancelAnimationFrame(instance.frame);
  instance.resizeObserver.disconnect();
  window.removeEventListener('resize', instance.resize);
  const geometries = new Set();
  const materials = new Set();
  instance.scene.traverse(object => {
    if (object.geometry) geometries.add(object.geometry);
    if (object.material) {
      (Array.isArray(object.material) ? object.material : [object.material]).forEach(material => materials.add(material));
    }
  });
  geometries.forEach(geometry => geometry.dispose());
  materials.forEach(material => material.dispose());
  instance.renderer.dispose();
  instance.renderer.forceContextLoss();
  instance.renderer.domElement.remove();
}

function selectHealthPillar(name) {
  if (!HEALTH_PILLARS.includes(name) && name !== 'Overall health') return;
  const layout = document.getElementById('health-scene-layout');
  if (selectedHealthPillar === name && layout?.classList.contains('has-detail')) {
    closeHealthDetail();
    return;
  }
  selectedHealthPillar = name;
  const health = apiData.portfolio_scorecard_health || {};
  const detail = document.getElementById('health-detail');
  if (!detail) return;
  layout?.classList.add('has-detail');
  detail.setAttribute('aria-hidden', 'false');
  const scoreText = value => value == null ? '—' : `${Number(value).toFixed(1)}%`;
  const group = health.groups?.[name];

  if (name === 'Overall health') {
    const groups = HEALTH_PILLARS.map(pillar => {
      const pillarGroup = health.groups?.[pillar];
      const value = pillarGroup?.percentage;
      return `<div class="health-detail-pillar"><span>${pillar}</span><strong>${scoreText(value)}</strong></div>`;
    }).join('');
    const coverage = health.status === 'loading'
      ? 'Loading 15-point scorecards...'
      : health.score_pct == null
        ? 'No matching scorecards are available.'
        : `${health.scored_holdings} scored holdings · ${health.scored_weight_pct}% of portfolio value covered`;
    detail.innerHTML = `
      <div class="health-detail-head">
        <div><span class="health-detail-kicker">PORTFOLIO HEALTH</span><strong>Overall</strong></div>
        <b class="health-detail-score">${scoreText(health.score_pct)}</b>
        <button type="button" class="health-detail-close" aria-label="Close health breakdown">&times;</button>
      </div>
      <div class="health-detail-note">${escHtml(coverage)}</div>
      <div class="health-detail-groups">${groups}</div>`;
  } else {
    const criteria = (group?.criteria || []).map(item => `
      <div class="health-criterion">
        <span>${escHtml(item.criterion)}</span>
        <div class="health-criterion-track"><i style="width:${Math.max(0, Math.min(100, Number(item.percentage) || 0))}%"></i></div>
        <strong>${scoreText(item.percentage)}</strong>
      </div>`).join('');
    const coverage = group
      ? `${group.coverage_pct}% of portfolio value · ${group.criteria.length} measures`
      : 'Waiting for the portfolio scorecards.';
    detail.innerHTML = `
      <div class="health-detail-head">
        <div><span class="health-detail-kicker">PILLAR SCORE</span><strong>${name}</strong></div>
        <b class="health-detail-score">${scoreText(group?.percentage)}</b>
        <button type="button" class="health-detail-close" aria-label="Close health breakdown">&times;</button>
      </div>
      <div class="health-detail-note">${escHtml(coverage)}</div>
      <div class="health-detail-criteria">${criteria || '<span class="health-detail-empty">No measures available for this pillar.</span>'}</div>`;
  }

  document.querySelectorAll('[data-health-select]').forEach(control => {
    const selected = control.dataset.healthSelect === name;
    control.classList.toggle('selected', selected);
    if (control.matches('button,[role="button"]')) control.setAttribute('aria-pressed', selected ? 'true' : 'false');
  });
}

function closeHealthDetail() {
  selectedHealthPillar = null;
  document.getElementById('health-scene-layout')?.classList.remove('has-detail');
  const detail = document.getElementById('health-detail');
  if (detail) {
    detail.setAttribute('aria-hidden', 'true');
    detail.replaceChildren();
  }
  document.querySelectorAll('[data-health-select]').forEach(control => {
    control.classList.remove('selected');
    if (control.matches('button,[role="button"]')) control.setAttribute('aria-pressed', 'false');
  });
}

// ════════ TAB RENDERERS ════════
function renderOverview() {
    const o = apiData.overview;
    const portfolioHoldings = apiData.portfolio?.holdings || [];

    const portfolioHealth = apiData.portfolio_scorecard_health;
    const healthBreakdown = portfolioHealth?.groups || {};
    const healthScore = portfolioHealth?.score_pct ?? null;

    const overviewStats = `
      <div class="overview-hero">
        <div class="overview-hero-main">
          <div class="overview-kicker">Portfolio footprint</div>
          <div class="overview-value">${formatCr(o.total_value)}</div>
          <div class="overview-delta ${o.return_pct >= 0 ? 'pos' : 'neg'}">
            ${o.return_pct != null ? `${o.return_pct >= 0 ? '▲' : '▼'} ${Math.abs(o.return_pct).toFixed(2)}% vs cost` : 'Return data pending'}
          </div>
        </div>
        <div class="overview-metrics">
          <div class="mini-stat"><span>Health</span><strong>${healthScore != null ? `${healthScore}%` : '—'}</strong></div>
          <div class="mini-stat"><span>Invested</span><strong>${o.invested_value != null ? formatCr(o.invested_value) : '—'}</strong></div>
          <div class="mini-stat"><span>Holdings</span><strong>${o.holding_count || 0}</strong></div>
        </div>
      </div>`;

    const watchlistHtml = portfolioHoldings.map((holding, index) => `
      <button class="stock-selector" data-stock-name="${attrEsc(holding.security_name)}"
        onclick="openPortfolioStock(${index}); return false;">
        <div class="stock-index">${String(index + 1).padStart(2, '0')}</div>
        <div class="stock-copy">
          <div class="stock-name">${escHtml(holding.security_name || 'Unnamed holding')}</div>
          <div class="stock-meta">
            <span>${formatCr(holding.effective_value)}</span>
            <span class="gain-pill ${holding.gain_pct >= 0 ? 'positive' : 'negative'}">${holding.gain_pct != null ? `${holding.gain_pct >= 0 ? '+' : ''}${holding.gain_pct.toFixed(2)}%` : '—'}</span>
          </div>
        </div>
      </button>`).join('') || '<div class="empty-chip">No holdings available</div>';

    const healthMarkers = HEALTH_PILLARS.map(name => {
      const value = healthBreakdown[name]?.percentage;
      return `
        <button class="health-three-marker health-pillar-marker" type="button" data-health-marker="${name}" data-health-select="${name}"
          style="--marker-color:${HEALTH_COLORS[name]}"
          aria-label="${name} score ${value == null ? 'not available' : `${Number(value).toFixed(1)} percent`}">
          <span>${name}</span><strong>${value == null ? '—' : `${Number(value).toFixed(1)}%`}</strong>
        </button>`;
    }).join('');

    const healthTriangle = `
      <div class="health-triangle-wrap">
        <div class="health-scene-layout" id="health-scene-layout">
          <div class="health-scene-main">
            <div class="health-three-stage" id="health-three-stage" aria-label="Rotating three-dimensional portfolio health triangle">
              <div class="health-three-canvas" id="health-three-canvas" aria-hidden="true"></div>
              <div class="health-three-markers">
                ${healthMarkers}
                <button class="health-three-marker health-three-core" type="button" data-health-marker="Overall health" data-health-select="Overall health"
                  style="--marker-color:${HEALTH_COLORS['Overall health']}"
                  aria-label="Overall portfolio health ${healthScore == null ? 'not available' : `${Number(healthScore).toFixed(1)} percent`} ">
                  <span>Overall</span><strong>${healthScore == null ? '—' : `${Number(healthScore).toFixed(1)}%`}</strong>
                </button>
              </div>
            </div>
          </div>
          <aside class="health-detail" id="health-detail" aria-live="polite" aria-hidden="true"></aside>
        </div>
      </div>`;

    // KPI Cards
    const kpis = `
    <div class="kpis">
      <div class="kpi">
        <div class="kpi-top"><span class="kpi-lb">Current Market Value</span></div>
        <div class="kpi-v">${formatCr(o.total_value)}</div>
        <div class="kpi-d ${o.return_pct >= 0 ? 'pos' : 'neu'}">${o.return_pct != null ? (o.return_pct >= 0 ? '▲ ' : '▼ ') + Math.abs(o.return_pct).toFixed(2) + '%' : '-'} vs Cost</div>
        ${(() => {
          // State the basis: a headline that is partly statement-priced must say so.
          const v = o.valuation || {};
          if (!v.available || !v.priced_holdings) return '';
          return v.fully_priced
            ? `<div style="font-size:10px;color:#16a34a;margin-top:4px">All ${v.priced_holdings} holdings at live CMP</div>`
            : `<div style="font-size:10px;color:#f59e0b;margin-top:4px" title="Holdings with no live CMP are valued at their statement figure">${v.priced_holdings} at live CMP, ${v.statement_priced_holdings} at statement</div>`;
        })()}
      </div>
      <div class="kpi">
        <div class="kpi-top"><span class="kpi-lb">Invested Value</span></div>
        <div class="kpi-v">${o.invested_value != null ? formatCr(o.invested_value) : '<span style="font-size:14px;color:#94a3b8">No cost data</span>'}</div>
        <div class="kpi-d neu">${o.has_partial_cost_data ? '⚠️ Excludes accounts w/o cost' : ''}</div>
      </div>
      <div class="kpi">
        <div class="kpi-top"><span class="kpi-lb">Total Gain</span></div>
        <div class="kpi-v">${o.total_gain != null ? formatCr(o.total_gain) : '-'}</div>
        <div class="kpi-d ${(o.total_gain||0) >= 0 ? 'pos' : 'tn'}">${(o.total_gain||0) >= 0 ? '▲' : '▼'}</div>
      </div>
      <div class="kpi">
        <div class="kpi-top"><span class="kpi-lb">Portfolio Health</span></div>
        <div class="kpi-v">${healthScore != null ? `${healthScore}%` : '&mdash;'}</div>
        <div class="hbar"><div class="hfill" style="width:${healthScore || 0}%"></div></div>
        ${(() => {
          const hl = portfolioHealth || {};
          if (healthScore == null) return `<div class="kpi-d neu">${hl.status === 'loading' ? 'loading 15-point scores' : 'no score data'}</div>`;
          const parts = HEALTH_PILLARS
            .filter(key => hl.groups?.[key]?.percentage != null)
            .map(key => `${key[0]} ${hl.groups[key].percentage}%`).join(' · ');
          const cov = hl.scored_weight_pct != null && hl.scored_weight_pct < 99.5
            ? `<div class="kpi-d neu" title="Holdings without a scorecard are excluded from the average">on ${hl.scored_weight_pct}% of value</div>`
            : '';
          return `<div class="kpi-d neu" style="font-size:9.5px">${parts}</div>${cov}`;
        })()}
      </div>
    </div>`;
    
    // Top Holdings (from overview router)
    const topHoldingsRows = (o.top_holdings || []).slice(0,5).map(h => `
        <tr><td><div class="hn"><span class="hl">${h.security.substring(0,25)}</span></div></td>
        <td class="num tv">${formatCr(h.value)}</td><td class="num tw">${h.weight_pct}%</td><td class="num tg ${h.gain_pct >= 0 ? 'tp' : 'tn'}">${h.gain_pct != null ? (h.gain_pct>0?'+':'')+h.gain_pct.toFixed(2)+'%' : '-'}</td></tr>
    `).join('');

    const sectorColors = ['#08796a', '#3573c6', '#c8862a', '#17916b', '#d85d42', '#9ca3af'];
    const allSectors = o.sector_allocation || [];
    const sectorList = allSectors.slice(0, 5);
    if (allSectors.length > 5) {
      const remainder = allSectors.slice(5);
      sectorList.push({
        sector: 'Others',
        value: remainder.reduce((total, sector) => total + (Number(sector.value) || 0), 0),
        pct: remainder.reduce((total, sector) => total + (Number(sector.pct) || 0), 0),
        isOther: true
      });
    }
    const sectorDonutHtml = sectorList.length ? (() => {
      const radius = 53;
      const circumference = 2 * Math.PI * radius;
      const sliceGap = 2.8;
      let offset = 0;
      const slices = sectorList.map((sector, index) => {
        const amount = circumference * Math.max(0, Number(sector.pct) || 0) / 100;
        const color = sector.isOther ? '#9ca3af' : sectorColors[index];
        const slice = `<circle class="sector-slice" cx="72" cy="72" r="${radius}" fill="none" stroke="${color}" stroke-width="14" stroke-dasharray="${Math.max(0, amount - sliceGap)} ${circumference}" stroke-dashoffset="${-offset}"/>`;
        offset += amount;
        return slice;
      }).join('');
      const legend = sectorList.map((sector, index) => `
        <div class="sector-legend-row${sector.isOther ? ' sector-other' : ''}" title="${attrEsc(sector.sector)}: ${Number(sector.pct).toFixed(2)}% · ${formatCr(sector.value)}">
          <span class="sector-swatch" style="background:${sector.isOther ? '#9ca3af' : sectorColors[index]}"></span>
          <span class="sector-legend-name">${escHtml(sector.sector)}</span>
          <strong>${Number(sector.pct).toFixed(1)}%</strong>
        </div>`).join('');
      return `
        <div class="sector-allocation">
          <div class="sector-donut-wrap">
            <svg class="sector-donut-svg" viewBox="0 0 144 144" role="img" aria-label="Sector allocation donut">
              <circle class="sector-donut-track" cx="72" cy="72" r="${radius}"></circle>
              <g transform="rotate(-90 72 72)">${slices}</g>
            </svg>
            <div class="sector-donut-center">
              <strong>${formatCr(o.total_value)}</strong><span>Portfolio</span>
            </div>
          </div>
          <div class="sector-legend">${legend}</div>
        </div>`;
    })() : '<div class="empty-chip">Sector allocation is not available yet.</div>';

    // Insights (from insights router)
    let insList = apiData.insights.insights || [];
    if (!Array.isArray(insList)) insList = [];
    const insHtml = insList.slice(0,3).map(i => `
        <div style="display:flex; gap:12px; padding:12px 10px; border-bottom:1px solid #f1f5f9; background:${i.type==='danger'?'#fef2f2':(i.type==='success'?'#f0fdf4':'#fffbeb')}; border-radius:8px; margin-bottom:8px;">
            <div style="color:${i.type==='danger'?'#dc2626':(i.type==='success'?'#16a34a':'#d97706')}; padding-top:2px"><svg viewBox="0 0 24 24" style="width:18px;height:18px;stroke:currentColor;fill:none;stroke-width:2.5"><circle cx="12" cy="12" r="10"/></svg></div>
            <div>
                <div style="font-size:13px; font-weight:600; color:#0f172a; margin-bottom:4px">${i.title}</div>
                <div style="font-size:12px; color:#475569; line-height:1.4">${i.description}</div>
            </div>
        </div>
    `).join('');

    const portfolioSplit = `
    <div class="overview-grid health-sector-grid">
      <aside class="overview-panel sector-panel">
        <div class="panel-header">
          <h3>Sector distribution</h3>
          <span class="soft-pill">${(o.sector_allocation || []).length} sectors</span>
        </div>
        ${sectorDonutHtml}
      </aside>
      <div class="overview-panel center-panel">
        <div class="panel-header">
          <h3>Portfolio health</h3>
          <span class="soft-pill">${o.holding_count || 0} holdings</span>
        </div>
        ${healthTriangle}
      </div>
    </div>

    <div class="overview-grid watch-growth-grid">
      <aside class="overview-panel left-panel">
        <div class="panel-header">
          <h3>Portfolio watchlist</h3>
          <button class="text-link" onclick="switchTab('portfolio'); return false;">View all</button>
        </div>
        <div class="stock-list">${watchlistHtml}</div>
      </aside>
      <div class="overview-panel wide-panel">
        <div class="panel-header">
          <h3>Portfolio growth</h3>
          <button class="text-link" onclick="switchTab('performance'); return false;">Details</button>
        </div>
        <div class="chwrap" style="height: 250px; position: relative;"><canvas id="pc"></canvas></div>
      </div>
    </div>

    <div class="overview-grid insight-grid">
      <div class="overview-panel">
        <div class="panel-header">
          <h3>Top holdings</h3>
          <button class="text-link" onclick="switchTab('portfolio'); return false;">Open portfolio</button>
        </div>
        <table class="ht">
          <thead><tr><th>Security</th><th class="num">Value</th><th class="num">Gain %</th></tr></thead>
          <tbody>
          ${(o.top_holdings || []).slice(0,5).map(h => `
            <tr><td><div class="hn"><span class="hl">${h.security.substring(0,18)}</span></div></td>
            <td class="num tv">${formatCr(h.value)}</td><td class="num tg ${h.gain_pct >= 0 ? 'tp' : 'tn'}">${h.gain_pct != null ? (h.gain_pct>0?'+':'')+h.gain_pct.toFixed(2)+'%' : '-'}</td></tr>
          `).join('') || '<tr><td colspan="3">No holdings.</td></tr>'}
          </tbody>
        </table>
      </div>

      <div class="overview-panel">
        <div class="panel-header">
          <h3>Portfolio insights</h3>
          <button class="text-link" onclick="switchTab('insights'); return false;">All insights</button>
        </div>
        <div>${insHtml || '<p style="color:#94a3b8; font-size:13px">No insights generated yet.</p>'}</div>
      </div>
    </div>`;

    return `
    ${o.has_partial_cost_data ? `<div class="ban" style="margin-bottom:18px;background:#fffbe0;border:1px solid #fde047;color:#854d0e">⚠️ <strong>Notice:</strong> Some accounts in this family view (like Demat) do not have historical cost data. Invested Value and Total Gain only reflect accounts with known costs.</div>` : ''}
    ${overviewStats}
    ${portfolioSplit}
    ${kpis}
    `;
}

function selectOverviewStock(index) {
  const holding = (apiData.portfolio?.holdings || [])[Number(index)];
  if (!holding) return;
  selectedOverviewStock = holding.security_name;
    renderDashboard();
}

// ── Per-stock fundamental checklist (expandable Portfolio rows) ─────────────
let checklistRows = [];          // holdings of the currently rendered table
let overviewScorecardRequest = 0;

// One hue per sector, so two holdings in the same business read as related at a
// glance and the column scans as a group rather than a row of loose labels. Text
// is the darkest step of each hue against its lightest tint, which keeps every
// pairing above the 4.5:1 contrast floor at this size. Keys are Yahoo's sector
// names; `_` is the fallback for a holding whose sector never resolved.
const SECTOR_STYLE = {
  'Financial Services':     {color: '#3730a3', bg: '#eef2ff', bd: '#c7d2fe'},
  'Technology':             {color: '#1e40af', bg: '#eff6ff', bd: '#bfdbfe'},
  'Communication Services': {color: '#a21caf', bg: '#fdf4ff', bd: '#f5d0fe'},
  'Healthcare':             {color: '#0f766e', bg: '#f0fdfa', bd: '#99f6e4'},
  'Consumer Goods':         {color: '#15803d', bg: '#f0fdf4', bd: '#bbf7d0'},
  'Consumer Cyclical':      {color: '#b45309', bg: '#fffbeb', bd: '#fde68a'},
  'Industrials':            {color: '#334155', bg: '#f8fafc', bd: '#cbd5e1'},
  'Basic Materials':        {color: '#7c2d12', bg: '#fff7ed', bd: '#fed7aa'},
  'Energy':                 {color: '#9f1239', bg: '#fff1f2', bd: '#fecdd3'},
  'Utilities':              {color: '#155e75', bg: '#ecfeff', bd: '#a5f3fc'},
  'Real Estate':            {color: '#6b21a8', bg: '#faf5ff', bd: '#e9d5ff'},
  _:                        {color: '#475569', bg: '#f1f5f9', bd: '#e2e8f0'}
};

// The tag carries the broad sector, so the column resolves into a few familiar
// buckets a client can be talked through. The specific industry Yahoo assigns
// ("Banks - Regional", "Beverages - Wineries & Distilleries") is the finer
// detail and goes in the tooltip rather than the label.
function industryTag(h) {
  const sector = (h.sector_group || '').trim();
  if (!sector) return '';
  const st = SECTOR_STYLE[sector] || SECTOR_STYLE._;
  const industry = (h.industry || '').trim();
  const tip = industry && industry !== sector ? `${sector} · ${industry}` : sector;
  return `<span class="ind-tag" style="color:${st.color};background:${st.bg};border-color:${st.bd}"
    title="${attrEsc(tip)}">${escHtml(sector)}</span>`;
}

function escHtml(text) {
  return String(text == null ? '' : text)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function attrEsc(text) {
  return escHtml(text).replace(/"/g, '&quot;');
}

function checklistKey(holding) {
  return `${holding.isin || ''}|${holding.security_name || ''}`;
}

async function loadOverviewScorecard(holding) {
  const host = document.getElementById('overview-scorecard-content');
  if (!host) return;
  const requestId = ++overviewScorecardRequest;
  if (!holding) {
    host.innerHTML = '<div class="empty-chip">No stock is available to score.</div>';
    return;
  }

  host.innerHTML = `<div class="scorecard-loading">Loading fundamentals for ${escHtml(holding.security_name)}...</div>`;
  try {
    const data = await requestScorecard(holding);
    if (requestId !== overviewScorecardRequest || currentTab !== 'overview') return;
    currentOverviewScorecardData = data;
    host.innerHTML = renderScorecard15(data, holding, 'overview');
  } catch (error) {
    if (requestId !== overviewScorecardRequest || currentTab !== 'overview') return;
    console.error('Overview scorecard load failed:', error);
    host.innerHTML = `<div class="scorecard-error">Could not load the scorecard. Please try selecting this stock again.</div>`;
  }
}

function renderScorecard15(data, holding, instanceKey) {
  const domId = String(instanceKey).replace(/[^a-zA-Z0-9_-]/g, '-');
  if (!data?.scorecard) {
    return `<div class="empty-chip">A 15-point scorecard is not available for ${escHtml(holding?.security_name || 'this holding')}.</div>`;
  }
  scorecardInstances.set(domId, {data, holding});
  const groups = (data.scorecard.groups || []).filter(group => HEALTH_PILLARS.includes(group.name));
  const selected = HEALTH_PILLARS.includes(scorecardGroupSelections[domId])
    ? scorecardGroupSelections[domId] : (groups[0]?.name || 'Growth');
  scorecardGroupSelections[domId] = selected;
  const activeGroup = groups.find(group => group.name === selected) || groups[0];
  const tabs = groups.map(group => `
    <button type="button" class="score15-tab${group.name === selected ? ' active' : ''}"
      aria-pressed="${group.name === selected ? 'true' : 'false'}"
      onclick="selectScorecardGroup('${domId}','${group.name}')">
      <span>${escHtml(group.name)}</span><strong>${Number(group.percentage).toFixed(1)}%</strong>
    </button>`).join('');
  const criteria = (activeGroup?.criteria || []).map(item => `
    <div class="score15-criterion">
      <div class="score15-criterion-name">
        <strong>${escHtml(item.criterion)}</strong>
        <span>${escHtml(item.actual)} <i>· ${escHtml(item.full_marks_at)}</i></span>
      </div>
      <b>${item.score == null ? '—' : item.score}<span>/${item.pts}</span></b>
    </div>`).join('');
  const company = data.company || {};

  return `
    <div class="score15-wrap" id="score15-${domId}">
      <div class="score15-heading">
        <div><strong>${escHtml(company.name || data.query || holding?.security_name)}</strong><span>${escHtml(company.symbol || company.isin || '')} · 15 criteria</span></div>
        <b>${Number(data.scorecard.score_pct).toFixed(1)}<small>%</small></b>
      </div>
      <div class="score15-tabs">${tabs}</div>
      <div class="score15-criteria">${criteria || '<div class="empty-chip">No criteria in this pillar.</div>'}</div>
    </div>`;
}

function selectScorecardGroup(instanceKey, name) {
  const record = scorecardInstances.get(instanceKey);
  const root = document.getElementById(`score15-${instanceKey}`);
  if (!record || !root || !HEALTH_PILLARS.includes(name)) return;
  scorecardGroupSelections[instanceKey] = name;
  root.outerHTML = renderScorecard15(record.data, record.holding, instanceKey);
}

function toggleChecklist(idx) {
  const detail = document.getElementById(`detail-${idx}`);
  const chev = document.getElementById(`chev-${idx}`);
  if (!detail) return;

  const isOpen = detail.style.display !== 'none';
  detail.style.display = isOpen ? 'none' : 'table-row';
  if (chev) chev.innerHTML = isOpen ? '&#9656;' : '&#9662;';   // ▸ / ▾
  if (isOpen) return;

  const h = checklistRows[idx];
  if (!h) return;
  const body = document.getElementById(`detail-body-${idx}`);
  body.innerHTML = `<div style="padding:18px;font-size:12px;color:#64748b">Loading the 15-point scorecard for ${escHtml(h.security_name)}...</div>`;

  requestScorecard(h)
    .then(data => {
      if (body.isConnected) body.innerHTML = renderScorecard15(data, h, `portfolio-${idx}`);
    })
    .catch(e => {
      console.error('Checklist load failed:', e);
      body.innerHTML = `<div style="padding:18px;font-size:12px;color:#b91c1c">Could not load the checklist: ${e}</div>`;
    });
}

// Meter tones. The fill carries severity and the track is a lighter step of the
// same hue, so the state of a pillar reads across the whole bar rather than only
// where it happens to stop. Every fill clears 3:1 on white and each is always
// accompanied by its own number, so colour never carries the meaning alone.
const TONE = {
  good:     {fill: '#16a34a', track: '#dcfce7', ink: '#15803d', word: 'strong'},
  mid:      {fill: '#d97706', track: '#fef3c7', ink: '#b45309', word: 'fair'},
  low:      {fill: '#ea580c', track: '#ffedd5', ink: '#c2410c', word: 'weak'},
  critical: {fill: '#dc2626', track: '#fee2e2', ink: '#b91c1c', word: 'poor'},
  none:     {fill: '#cbd5e1', track: '#f1f5f9', ink: '#64748b', word: 'not scored'}
};

function scoreTone(score, max) {
  if (score == null || !max) return TONE.none;
  const r = score / max;
  if (r >= 0.75) return TONE.good;
  if (r >= 0.5) return TONE.mid;
  if (r >= 0.25) return TONE.low;
  return TONE.critical;
}

// Which pillar panel is open, per holding row, so opening one closes the other
// three rather than unrolling all 34 parameters at once.
const vmOpen = {};

function toggleVmPillar(idx, name) {
  const key = String(idx);
  vmOpen[key] = vmOpen[key] === name ? null : name;
  document.querySelectorAll('[data-vm-panel="' + idx + '"]').forEach(el => {
    el.hidden = el.getAttribute('data-vm-name') !== vmOpen[key];
  });
  document.querySelectorAll('[data-vm-card="' + idx + '"]').forEach(el => {
    const on = el.getAttribute('data-vm-name') === vmOpen[key];
    el.classList.toggle('on', on);
    el.setAttribute('aria-expanded', on ? 'true' : 'false');
  });
}

function renderChecklist(data, holding, idx) {
  if (!data.found) {
    return `<div style="padding:18px;font-size:12px;color:#92400e;background:#fffbeb">
      <strong>${escHtml(holding.security_name)}</strong> &mdash; ${escHtml(data.reason)}
    </div>`;
  }

  // Static files are re-read from disk on every request while the Python process
  // is only reloaded on restart, so this page can be newer than the API serving
  // it. Detect that instead of throwing on a missing field.
  if (!data.total || !Array.isArray(data.classes)) {
    return `<div style="padding:18px 20px;font-size:12px;color:#92400e;background:#fffbeb;line-height:1.6">
      <strong>The dashboard is newer than the running server.</strong><br>
      This page expects the Growth / Quality / Ownership / Valuation scorecard, but the
      API still returns the older checklist format. Restart the API process to pick up
      the current code:
      <div style="margin-top:8px;font-family:ui-monospace,monospace;font-size:11px;background:#fff;border:1px solid #fde68a;border-radius:6px;padding:8px 10px">
        python -m uvicorn app.main:app --port 8000 --reload
      </div>
    </div>`;
  }

  const t = data.total;
  const tone = scoreTone(t.score, t.max);
  // Two different dates, kept apart on purpose: the period the accounts cover,
  // and when we last fetched them. Only the first says anything about the
  // financials; showing the fetch date alone read as "accounts dated 1 Oct".
  const fetched = data.as_of ? new Date(data.as_of).toLocaleDateString('en-IN',
    {day: 'numeric', month: 'short', year: 'numeric'}) : null;
  const covers = data.period_latest
    ? `FY to ${data.period_latest}${data.has_ttm ? ' + TTM' : ''}`
    : null;
  const span = data.period_years > 1
    ? `${data.period_years} years of history` : null;
  const totalParams = t.params + (t.excluded_params || 0);
  const idxSafe = Number(idx) || 0;

  // ── The four pillars, as meters in a 2x2 ─────────────────────────────────
  const cards = (data.classes || []).map(cl => {
    const pct = cl.pct == null ? 0 : cl.pct;
    const ct = scoreTone(cl.score, cl.max);
    const open = vmOpen[String(idxSafe)] === cl.name;
    // A pillar can end up with nothing measurable — Yahoo publishes no Indian
    // shareholding pattern, so Ownership is empty for a company Screener has
    // not covered. Showing "0/0 - 0% - poor" would read as a failed pillar
    // rather than an unmeasured one, so it says so instead.
    if (!cl.params || !cl.max) {
      return `
      <div class="vm-card vm-card-empty">
        <span class="vm-card-top">
          <span class="vm-card-name">${escHtml(cl.name)}</span>
          <span class="vm-card-score" style="color:#94a3b8">&mdash;</span>
        </span>
        <span class="vm-meter" style="background:#f1f5f9"></span>
        <span class="vm-card-foot">
          <span class="vm-card-word" style="color:#94a3b8">not measurable</span>
          <span class="vm-card-n">${cl.excluded_params || 0} param${
            (cl.excluded_params || 0) === 1 ? '' : 's'} need a filing</span>
        </span>
      </div>`;
    }
    return `
      <button type="button" class="vm-card${open ? ' on' : ''}"
              data-vm-card="${idxSafe}" data-vm-name="${attrEsc(cl.name)}"
              aria-expanded="${open ? 'true' : 'false'}"
              onclick="toggleVmPillar(${idxSafe}, &quot;${attrEsc(cl.name)}&quot;)">
        <span class="vm-card-top">
          <span class="vm-card-name">${escHtml(cl.name)}</span>
          <span class="vm-card-score" style="color:${ct.ink}">${cl.score}<span class="vm-card-max">/${cl.max}</span></span>
        </span>
        <span class="vm-meter" style="background:${ct.track}">
          <span class="vm-meter-fill" style="width:${pct}%;background:${ct.fill}"></span>
        </span>
        <span class="vm-card-foot">
          <span class="vm-card-word" style="color:${ct.ink}">${pct}% &middot; ${ct.word}</span>
          <span class="vm-card-n">${cl.params} param${cl.params === 1 ? '' : 's'}<span class="vm-caret">&#9662;</span></span>
        </span>
      </button>`;
  }).join('');

  // ── One detail panel per pillar, revealed on demand ──────────────────────
  const panels = (data.classes || []).map(cl => {
    const open = vmOpen[String(idxSafe)] === cl.name;
    const rows = (cl.rows || []).map(r => {
      const rt = scoreTone(r.score, r.max);
      const pct = (r.score == null || !r.max) ? 0 : (r.score / r.max) * 100;
      return `
        <li class="vm-row">
          <span class="vm-row-main">
            <span class="vm-row-name">${escHtml(r.parameter)}${
              r.note ? `<span class="vm-why" title="${attrEsc(r.note)}">?</span>` : ''}</span>
            <span class="vm-row-crit">target ${escHtml(r.criteria)}</span>
          </span>
          <span class="vm-row-val">${escHtml(r.value)}</span>
          <span class="vm-row-meter" style="background:${rt.track}">
            <span class="vm-meter-fill" style="width:${pct}%;background:${rt.fill}"></span>
          </span>
          <span class="vm-row-score" style="color:${rt.ink}">${
            r.score == null ? '&mdash;' : r.score}<span class="vm-row-max">/${r.max}</span></span>
        </li>`;
    }).join('');
    return `
      <div class="vm-panel" data-vm-panel="${idxSafe}" data-vm-name="${attrEsc(cl.name)}"${open ? '' : ' hidden'}>
        <ul class="vm-rows">${rows}</ul>
        ${cl.excluded_params ? `<p class="vm-excl">${cl.excluded_params} further
          ${cl.excluded_params === 1 ? 'parameter needs' : 'parameters need'} an annual report,
          a filing or a web search, so ${cl.excluded_params === 1 ? 'it is' : 'they are'} left out
          of both this score and the total: <em>${escHtml((cl.excluded_names || []).join(', '))}</em>.</p>` : ''}
      </div>`;
  }).join('');

  const basis = data.basis === 'consolidated' ? 'Consolidated'
              : (data.basis === 'standalone' ? 'Standalone' : 'Unknown-basis');

  return `
  <div class="vm-wrap">
    <div class="vm-hero">
      <div class="vm-hero-l">
        <div class="vm-hero-num" style="color:${tone.ink}">${t.pct}<span class="vm-hero-pc">%</span></div>
        <div class="vm-hero-sub">${t.score} of ${t.max} points</div>
      </div>
      <div class="vm-hero-r">
        <div class="vm-hero-name">${escHtml(data.security)}</div>
        <div class="vm-hero-meta">${data.code ? escHtml(data.code) + ' &middot; ' : ''}${
          data.industry ? escHtml(data.industry) + ' &middot; ' : ''}${basis}${
          covers ? ' &middot; ' + escHtml(covers) : ''}${
          span ? ' &middot; ' + escHtml(span) : ''}</div>
        <span class="vm-meter vm-hero-meter" style="background:${tone.track}">
          <span class="vm-meter-fill" style="width:${t.pct || 0}%;background:${tone.fill}"></span>
        </span>
        <div class="vm-hero-note">${t.params} of ${totalParams} parameters could be measured${
          t.excluded_params ? '; the other ' + t.excluded_params + ' need a filing and are left out of the total' : ''}.${
          fetched ? ` <span class="vm-fetched" title="When these statements were last downloaded. Company accounts are published quarterly, so this is a freshness note, not the period they cover.">Data read ${escHtml(fetched)}.</span>` : ''}</div>
      </div>
    </div>

    <div class="vm-grid">${cards}</div>
    ${panels}
  </div>`;
}

function renderPortfolio() {
    const hList = apiData.portfolio.holdings || [];
    const md = apiData.portfolio.market_data || {};
    // Only show the Screener columns once market data has actually been imported,
    // so the table doesn't fill with dashes for users who haven't set it up.
    const showLive = !!md.available;

    const dash = '<span style="color:#cbd5e1">—</span>';

    const rows = hList.map((h, idx) => {
        // A row valued from the statement is flagged, so a figure that is not at
        // CMP is never silently mixed in with the ones that are.
        const isLive = h.valued_at === 'live';
        return `
        <tr>
            <td>
              <div class="hn">
                <button type="button" class="stock-link" onclick="openPortfolioStock(${idx})">${escHtml(h.security_name)}</button>
                ${industryTag(h)}
              </div>
            </td>
            <td class="num" ${h.price_source ? `title="Price source: ${h.price_source}"` : ''}>${h.cmp != null ? '₹' + h.cmp.toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2}) : dash}</td>
            <td class="num">${h.quantity != null ? h.quantity.toLocaleString('en-IN', {maximumFractionDigits: 3}) : dash}</td>
            <td class="num tv">${h.effective_value != null ? formatCr(h.effective_value) : dash}${isLive ? '' : `<div style="font-size:10px;color:#f59e0b;font-weight:400" title="No live CMP matched - this is the statement value">at statement</div>`}</td>
            <td class="num tw">${h.weight_pct.toFixed(2)}%</td>
        </tr>
        `;
    }).join('');

    const colCount = 5;

    // A partial revaluation must never be presented as a portfolio total: it only
    // covers the holdings that matched, so it is labelled and scoped explicitly.
    const liveBanner = showLive ? `
      <div style="margin-bottom:14px;padding:10px 14px;background:${md.fully_priced ? '#f0fdf4' : '#fffbeb'};border:1px solid ${md.fully_priced ? '#bbf7d0' : '#fde68a'};border-radius:8px;font-size:12px;color:#334155">
        <strong>Screener.in market data:</strong>
        ${md.priced_holdings} of ${hList.length} holdings priced at CMP
        ${md.fully_priced
          ? ` — all values below are live.`
          : `<div style="margin-top:4px;color:#92400e">
               The remaining ${md.statement_priced_holdings} are shown at their statement value and marked
               <em>at statement</em>, so Market Value and Weight are a blend of both bases.
               Unmatched holdings need an ISIN or a Screener row — check
               <code>/api/market/coverage/${clientId}</code>.
             </div>`}
      </div>` : '';

    return `
    <div class="crd">
      <div class="crd-h"><div class="crd-t">Complete Holdings (${apiData.portfolio.holding_count || 0})</div></div>
      ${liveBanner}
      <div style="overflow-x:auto">
      <table class="ht" style="width:100%">
        <!-- Percentage widths: the name column no longer absorbs all the slack,
             so the four figure columns sit close to the security name. -->
        <colgroup><col style="width:34%"><col style="width:15%"><col style="width:15%"><col style="width:20%"><col style="width:16%"></colgroup>
        <thead><tr><th>Security Name</th><th class="num">CMP</th><th class="num">Qty</th><th class="num">Market Value</th><th class="num">Weight</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="${colCount}">No holdings found.</td></tr>`}</tbody>
      </table>
      </div>
    </div>
    `;
}

function renderPerformance() {
    const pf = apiData.performance;
    const gainers = pf.top_gainers || [];
    const losers = pf.top_losers || [];
    
    return `
    ${!pf.has_cost_data ? `<div class="ban" style="margin-bottom:18px;background:#fffbe0;border:1px solid #fde047;color:#854d0e">⚠️ <strong>Notice:</strong> The selected account(s) do not contain historical cost data. Performance metrics cannot be calculated.</div>` : ''}
    
    <div class="mid" style="grid-template-columns: 1fr 1fr; margin-bottom: 20px;">
      <div class="crd" style="background: #f8fafc">
        <div class="kpi-top" style="margin-bottom:10px"><span class="kpi-lb">Absolute Return</span></div>
        <b style="font-size:24px; color: ${(pf.kpis?.absolute_return_pct||0) >= 0 ? '#059669' : '#dc2626'}">${pf.kpis?.absolute_return_pct != null ? pf.kpis.absolute_return_pct + '%' : '-'}</b>
      </div>
      <div class="crd" style="background: #f8fafc">
        <div class="kpi-top" style="margin-bottom:10px"><span class="kpi-lb">Best Performing Asset</span></div>
        <b style="font-size:20px; color: #0f172a">${pf.kpis?.best_performing_asset || '-'}</b>
      </div>
    </div>

    <div class="mid" style="grid-template-columns: 1fr 1fr;">
      <div class="crd">
        <div class="crd-h"><div class="crd-t">Top Winners</div></div>
        <table class="ht">
          <thead><tr><th>Security</th><th class="num">Gain %</th><th class="num">Value (LLM Extracted)</th></tr></thead>
          <tbody>${gainers.map(h => `
              <tr><td><div class="hn"><span class="hl">${h.security.substring(0,30)}</span></div></td>
              <td class="num tg tp">+${(h.gain_pct||0).toFixed(2)}%</td>
              <td class="num tw">${h.value != null ? h.value : '-'}</td></tr>
          `).join('') || '<tr><td colspan="3">No gainers found.</td></tr>'}</tbody>
        </table>
      </div>
      <div class="crd">
        <div class="crd-h"><div class="crd-t">Worst Performers (Drags)</div></div>
        <table class="ht">
          <thead><tr><th>Security</th><th class="num">Loss %</th><th class="num">Value (LLM Extracted)</th></tr></thead>
          <tbody>${losers.map(h => `
              <tr><td><div class="hn"><span class="hl">${h.security.substring(0,30)}</span></div></td>
              <td class="num tg tn">${(h.gain_pct||0).toFixed(2)}%</td>
              <td class="num tw">${h.value != null ? h.value : '-'}</td></tr>
          `).join('') || '<tr><td colspan="3">No losers found.</td></tr>'}</tbody>
        </table>
      </div>
    </div>
    `;
}

function renderRisk() {
    const r = apiData.risk;
    const flags = r.risk_flags || [];
    const overlaps = r.cross_account_overlaps || [];
    
    return `
    <div class="mid" style="grid-template-columns: 1fr 1fr; margin-bottom: 20px">
      <div class="crd" style="background: ${r.overall_risk_level === 'High' ? '#fef2f2' : (r.overall_risk_level === 'Medium' ? '#fffbeb' : '#f0fdf4')}">
        <div class="kpi-top" style="margin-bottom:10px"><span class="kpi-lb">Overall Risk Level</span></div>
        <b style="font-size:24px; color: ${r.overall_risk_level === 'High' ? '#dc2626' : (r.overall_risk_level === 'Medium' ? '#d97706' : '#16a34a')}">${r.overall_risk_level || 'Unknown'}</b>
      </div>
      <div class="crd">
        <div class="kpi-top" style="margin-bottom:10px"><span class="kpi-lb">Concentration Risk (HHI)</span></div>
        <b style="font-size:24px;">${r.concentration_risk || '-'}</b>
      </div>
    </div>
    
    ${flags.length > 0 ? `
    <div class="crd" style="margin-bottom: 20px">
      <div class="crd-h"><div class="crd-t">Detected Risk Flags</div></div>
      <ul style="padding-left: 20px; line-height: 1.8; color: #dc2626">
        ${flags.map(f => `<li>${f}</li>`).join('')}
      </ul>
    </div>
    ` : ''}

    <div class="mid" style="grid-template-columns: 1fr 1fr;">
      <div class="crd">
        <div class="crd-h"><div class="crd-t">Sector Concentration</div></div>
        <div style="padding-top:4px">
            ${(r.sector_concentration || []).slice(0,8).map(s => `
                <div class="sr"><span class="sn">${s.sector}</span><div class="sb2"><div class="sf" style="width:${s.pct}%"></div></div><span class="sp">${s.pct.toFixed(1)}%</span></div>
            `).join('') || '<p style="color:#94a3b8">No sector data.</p>'}
        </div>
      </div>

      <div class="crd">
        <div class="crd-h"><div class="crd-t">Cross-Account Overlaps</div></div>
        <table class="ht">
          <thead><tr><th>Security</th><th class="num">Total Exposure</th><th class="ctr">Found In</th></tr></thead>
          <tbody>${overlaps.map(o => `
              <tr><td><span class="hl" style="color:#dc2626">${o.security.substring(0,30)}</span></td>
              <td class="num tv">${(o.combined_pct || 0).toFixed(2)}%</td>
              <td class="ctr tw">${(o.accounts || []).length} accounts</td></tr>
          `).join('')}
          ${overlaps.length === 0 ? '<tr><td colspan="3" style="text-align:center;color:#94a3b8;padding:20px">No overlaps found across accounts. Great diversification!</td></tr>' : ''}
          </tbody>
        </table>
      </div>
    </div>
    `;
}

function renderInsights() {
    let insList = apiData.insights.insights || [];
    if (!Array.isArray(insList)) insList = [];
    
    return `
    <div class="crd">
      <div class="crd-h"><div class="crd-t">AI & Algorithmic Insights</div></div>
      <div>
        ${insList.map(i => `
        <div class="ins" style="padding: 16px 0; border-bottom: 1px solid #f1f5f9;">
            <div class="iic ${i.type==='danger'?'ib':(i.type==='success'?'ig':(i.type==='warning'?'iw':''))}" style="width:36px;height:36px;"><svg viewBox="0 0 24 24" style="width:20px;height:20px"><circle cx="12" cy="12" r="10"/></svg></div>
            <div style="margin-left: 6px;">
                <div class="it" style="font-size:15px; margin-bottom:4px; font-weight:600">${i.title} <span style="font-size:12px; font-weight:normal; color:#94a3b8; margin-left:8px; background:#f1f5f9; padding:2px 6px; border-radius:12px">${i.source_account ? i.source_account : 'Family Level'}</span></div>
                <div class="id" style="font-size:14px; line-height: 1.6; color:#475569">${i.description}</div>
            </div>
        </div>
        `).join('')}
        ${insList.length === 0 ? '<p style="color:#94a3b8; padding: 20px;">No insights available yet.</p>' : ''}
      </div>
    </div>
    `;
}

// ════════ EVENT LISTENERS ════════
function switchTab(tabName) {
    currentTab = tabName;
    renderDashboard();
}

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
document.querySelector('.sb-lo').addEventListener('click', async function(){
  await fetch('/api/auth/logout', {method:'POST'});
  window.location.href = 'login.html';
});
initDashboard().catch(error => {
  console.error('Dashboard could not be loaded:', error);
  window.location.replace('login.html');
});
