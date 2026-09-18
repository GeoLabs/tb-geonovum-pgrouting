const PDOK_ITEMS_URL = "https://api.pdok.nl/rws/nationaal-wegenbestand-wegen/ogc/v1/collections/wegvakken/items";
const ROUTING_PROCESS_URL = "/ogc-api/processes/routing/execution";
const STAGE_AHN_PROCESS_URL = "/ogc-api/processes/StageAHN/execution";
const PROFILE_PROCESS_URL = "/ogc-api/processes/GdalExtractProfile/execution";
const NETWORK_MIN_ZOOM = 16;
const NETWORK_PAGE_SIZE = 1000;
const NETWORK_MAX_FEATURES = 3000;

const map = L.map("map", { zoomControl: false }).setView([52.3702, 4.8952], 14);
L.control.zoom({ position: "bottomleft" }).addTo(map);

L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
}).addTo(map);

const networkLayer = L.geoJSON(null, {
  style: {
    color: "#147d64",
    weight: 2,
    opacity: 0.62,
  },
  interactive: false,
}).addTo(map);

const routeLayer = L.geoJSON(null, {
  style: {
    color: "#e2412f",
    weight: 6,
    opacity: 0.95,
  },
}).addTo(map);

const state = {
  placement: "start",
  start: null,
  end: null,
  startMarker: null,
  endMarker: null,
  networkController: null,
  networkTimer: null,
  elevationMarker: null,
};

const elements = {
  startMode: document.querySelector("#start-mode"),
  endMode: document.querySelector("#end-mode"),
  startCoordinate: document.querySelector("#start-coordinate"),
  endCoordinate: document.querySelector("#end-coordinate"),
  routeButton: document.querySelector("#route-button"),
  resetButton: document.querySelector("#reset-button"),
  routeResult: document.querySelector("#route-result"),
  routeDistance: document.querySelector("#route-distance"),
  routeSegments: document.querySelector("#route-segments"),
  routeMessage: document.querySelector("#route-message"),
  networkStatus: document.querySelector("#network-status"),
  elevationProfile: document.querySelector("#elevation-profile"),
  elevationRange: document.querySelector("#elevation-range"),
  elevationChart: document.querySelector("#elevation-chart"),
};

function markerIcon(kind) {
  const letter = kind === "start" ? "A" : "B";
  return L.divIcon({
    className: "",
    html: `<div class="route-marker ${kind}-marker"><span>${letter}</span></div>`,
    iconSize: [32, 32],
    iconAnchor: [8, 29],
  });
}

function coordinateText(point) {
  return point ? `${point.lng.toFixed(6)}, ${point.lat.toFixed(6)}` : "";
}

function setPlacement(kind) {
  state.placement = kind;
  elements.startMode.classList.toggle("active", kind === "start");
  elements.endMode.classList.toggle("active", kind === "end");
  elements.startMode.setAttribute("aria-pressed", String(kind === "start"));
  elements.endMode.setAttribute("aria-pressed", String(kind === "end"));
}

function updateRouteControls() {
  elements.startCoordinate.value = coordinateText(state.start);
  elements.endCoordinate.value = coordinateText(state.end);
  elements.routeButton.disabled = !(state.start && state.end);
  if (state.start && state.end) {
    elements.routeMessage.textContent = "Ready to calculate the shortest path.";
  } else if (state.start) {
    elements.routeMessage.textContent = "Select the finish point.";
  } else {
    elements.routeMessage.textContent = "Select a start point, then a finish point.";
  }
  elements.routeMessage.classList.remove("error");
}

function setPoint(kind, latlng) {
  const markerKey = `${kind}Marker`;
  if (state[markerKey]) {
    state[markerKey].setLatLng(latlng);
  } else {
    state[markerKey] = L.marker(latlng, {
      draggable: true,
      icon: markerIcon(kind),
      title: kind === "start" ? "Start point" : "Finish point",
    }).addTo(map);
    state[markerKey].on("dragend", (event) => {
      state[kind] = event.target.getLatLng();
      routeLayer.clearLayers();
      elements.routeResult.hidden = true;
      updateRouteControls();
    });
  }

  state[kind] = latlng;
  routeLayer.clearLayers();
  elements.routeResult.hidden = true;
  elements.elevationProfile.hidden = true;
  clearElevationMarker();
  setPlacement(kind === "start" ? "end" : "start");
  updateRouteControls();
}

async function executeProcess(url, request, accept = "application/json") {
  const response = await fetch(url, {
    method: "POST",
    headers: {
      Accept: accept,
      "Content-Type": "application/json",
      Prefer: "respond-sync",
    },
    body: JSON.stringify(request),
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.detail || `Process returned HTTP ${response.status}`);
  }
  return payload;
}

function routeCoordinates(route) {
  const result = [];
  for (const feature of route.features || []) {
    const geometry = feature.geometry || {};
    const lines = geometry.type === "MultiLineString" ? geometry.coordinates : [geometry.coordinates];
    for (const line of lines) {
      for (const coordinate of line || []) {
        const previous = result[result.length - 1];
        if (!previous || previous[0] !== coordinate[0] || previous[1] !== coordinate[1]) {
          result.push(coordinate);
        }
      }
    }
  }
  return result;
}

function interpolateRoutePosition(routePoints, ratio) {
  const segmentLengths = [];
  let totalLength = 0;
  for (let index = 1; index < routePoints.length; index += 1) {
    const length = map.distance(
      [routePoints[index - 1][1], routePoints[index - 1][0]],
      [routePoints[index][1], routePoints[index][0]],
    );
    segmentLengths.push(length);
    totalLength += length;
  }

  let targetDistance = totalLength * ratio;
  for (let index = 0; index < segmentLengths.length; index += 1) {
    if (targetDistance <= segmentLengths[index] || index === segmentLengths.length - 1) {
      const fraction = segmentLengths[index] ? targetDistance / segmentLengths[index] : 0;
      const start = routePoints[index];
      const end = routePoints[index + 1];
      return L.latLng(
        start[1] + (end[1] - start[1]) * fraction,
        start[0] + (end[0] - start[0]) * fraction,
      );
    }
    targetDistance -= segmentLengths[index];
  }
  const last = routePoints[routePoints.length - 1];
  return L.latLng(last[1], last[0]);
}

function clearElevationMarker() {
  if (state.elevationMarker) {
    state.elevationMarker.remove();
    state.elevationMarker = null;
  }
}

function showElevationLoading(stage, detail) {
  clearElevationMarker();
  elements.elevationProfile.hidden = false;
  elements.elevationProfile.classList.add("loading");
  elements.elevationRange.textContent = stage;
  elements.elevationChart.innerHTML = `
    <line class="profile-loading-line" x1="0" y1="60" x2="320" y2="60"></line>
    <text class="profile-loading-text" x="160" y="38" text-anchor="middle">${detail}</text>
  `;
}

function showElevationError(message) {
  elements.elevationProfile.classList.remove("loading");
  elements.elevationRange.textContent = "Unavailable";
  elements.elevationChart.innerHTML = `<text class="profile-error-text" x="160" y="48" text-anchor="middle">${message}</text>`;
}

function renderElevationProfile(coordinates, route) {
  const samples = coordinates.filter((point) => Number.isFinite(point[2]) && Math.abs(point[2]) < 1e20);
  const routePoints = routeCoordinates(route);
  if (samples.length < 2 || routePoints.length < 2) {
    throw new Error("AHN returned too few valid elevation samples.");
  }

  const distances = [0];
  for (let index = 1; index < samples.length; index += 1) {
    const deltaX = samples[index][0] - samples[index - 1][0];
    const deltaY = samples[index][1] - samples[index - 1][1];
    distances.push(distances[index - 1] + Math.hypot(deltaX, deltaY));
  }

  const elevations = samples.map((point) => point[2]);
  const minimum = Math.min(...elevations);
  const maximum = Math.max(...elevations);
  const elevationSpan = Math.max(maximum - minimum, 0.1);
  const totalDistance = distances[distances.length - 1] || 1;
  const width = 320;
  const height = 92;
  const top = 6;
  const bottom = 84;
  const points = samples.map((point, index) => {
    const xCoordinate = (distances[index] / totalDistance) * width;
    const yCoordinate = bottom - ((point[2] - minimum) / elevationSpan) * (bottom - top);
    return `${xCoordinate.toFixed(2)},${yCoordinate.toFixed(2)}`;
  });
  const areaPoints = [`0,${bottom}`, ...points, `${width},${bottom}`].join(" ");

  elements.elevationChart.innerHTML = `
    <line class="profile-baseline" x1="0" y1="${bottom}" x2="${width}" y2="${bottom}"></line>
    <polygon class="profile-area" points="${areaPoints}"></polygon>
    <polyline class="profile-line" points="${points.join(" ")}"></polyline>
    <line class="profile-cursor" x1="0" y1="${top}" x2="0" y2="${bottom}" hidden></line>
    <circle class="profile-point" cx="0" cy="0" r="4" hidden></circle>
    <g class="profile-tooltip" hidden>
      <rect x="0" y="0" width="92" height="20" rx="3"></rect>
      <text x="46" y="14" text-anchor="middle"></text>
    </g>
    <rect class="profile-hit-area" x="0" y="0" width="${width}" height="${height}"></rect>
  `;

  const cursor = elements.elevationChart.querySelector(".profile-cursor");
  const profilePoint = elements.elevationChart.querySelector(".profile-point");
  const tooltip = elements.elevationChart.querySelector(".profile-tooltip");
  const tooltipRect = tooltip.querySelector("rect");
  const tooltipText = tooltip.querySelector("text");
  const hitArea = elements.elevationChart.querySelector(".profile-hit-area");

  const showSample = (event) => {
    const bounds = elements.elevationChart.getBoundingClientRect();
    const pointerX = Math.max(0, Math.min(width, ((event.clientX - bounds.left) / bounds.width) * width));
    const targetDistance = (pointerX / width) * totalDistance;
    let low = 0;
    let high = distances.length - 1;
    while (low < high) {
      const middle = Math.floor((low + high) / 2);
      if (distances[middle] < targetDistance) low = middle + 1;
      else high = middle;
    }
    const sampleIndex = low > 0 && Math.abs(distances[low - 1] - targetDistance) < Math.abs(distances[low] - targetDistance) ? low - 1 : low;
    const xCoordinate = (distances[sampleIndex] / totalDistance) * width;
    const yCoordinate = bottom - ((samples[sampleIndex][2] - minimum) / elevationSpan) * (bottom - top);
    const tooltipX = Math.max(0, Math.min(width - 92, xCoordinate - 46));

    cursor.setAttribute("x1", xCoordinate);
    cursor.setAttribute("x2", xCoordinate);
    cursor.removeAttribute("hidden");
    profilePoint.setAttribute("cx", xCoordinate);
    profilePoint.setAttribute("cy", yCoordinate);
    profilePoint.removeAttribute("hidden");
    tooltip.setAttribute("transform", `translate(${tooltipX} 0)`);
    tooltipRect.setAttribute("x", 0);
    tooltipText.textContent = `${samples[sampleIndex][2].toFixed(2)} m · ${Math.round(distances[sampleIndex])} m`;
    tooltip.removeAttribute("hidden");

    const ratio = distances[sampleIndex] / totalDistance;
    const latlng = interpolateRoutePosition(routePoints, ratio);
    if (!state.elevationMarker) {
      state.elevationMarker = L.circleMarker(latlng, {
        radius: 7,
        color: "#ffffff",
        weight: 3,
        fillColor: "#147d64",
        fillOpacity: 1,
        interactive: false,
      }).addTo(map);
    } else {
      state.elevationMarker.setLatLng(latlng);
    }
  };

  const hideSample = () => {
    cursor.setAttribute("hidden", "");
    profilePoint.setAttribute("hidden", "");
    tooltip.setAttribute("hidden", "");
    clearElevationMarker();
  };

  hitArea.addEventListener("pointermove", showSample);
  hitArea.addEventListener("pointerdown", showSample);
  hitArea.addEventListener("pointerleave", hideSample);
  elements.elevationRange.textContent = `${minimum.toFixed(2)}–${maximum.toFixed(2)} m NAP`;
  elements.elevationProfile.classList.remove("loading");
  elements.elevationProfile.hidden = false;
}

async function loadElevationProfile(route) {
  showElevationLoading("1/2 · StageAHN", "Downloading and preparing AHN terrain");
  elements.routeMessage.textContent = "Route calculated. StageAHN is preparing the terrain raster…";
  const staged = await executeProcess(STAGE_AHN_PROCESS_URL, {
    inputs: {
      Geometry: { value: route, mediaType: "application/geo+json" },
      PaddingMeters: 10,
    },
    outputs: {
      RasterFile: { transmissionMode: "value" },
      Geometry28992: { transmissionMode: "value" },
    },
    response: "document",
  });

  showElevationLoading("2/2 · GdalExtractProfile", "Sampling elevation along the route");
  elements.routeMessage.textContent = "Terrain ready. GdalExtractProfile is sampling the route…";
  const profile = await executeProcess(PROFILE_PROCESS_URL, {
    inputs: {
      RasterFile: staged.RasterFile,
      Geometry: {
        value: staged.Geometry28992.value,
        mediaType: "application/geo+json",
      },
    },
    outputs: { Profile: { transmissionMode: "value" } },
    response: "raw",
  });
  renderElevationProfile(profile.coordinates || [], route);
}

function nextLink(payload) {
  return (payload.links || []).find((link) => link.rel === "next")?.href || null;
}

async function loadNetwork() {
  if (map.getZoom() < NETWORK_MIN_ZOOM) {
    networkLayer.clearLayers();
    elements.networkStatus.textContent = `Zoom to level ${NETWORK_MIN_ZOOM} to load PDOK NWB`;
    return;
  }

  if (state.networkController) {
    state.networkController.abort();
  }
  state.networkController = new AbortController();
  const signal = state.networkController.signal;
  const bounds = map.getBounds();
  const bbox = [bounds.getWest(), bounds.getSouth(), bounds.getEast(), bounds.getNorth()].join(",");
  let url = `${PDOK_ITEMS_URL}?f=json&limit=${NETWORK_PAGE_SIZE}&bbox=${encodeURIComponent(bbox)}`;
  let featureCount = 0;
  const features = [];

  elements.networkStatus.textContent = "Loading PDOK NWB…";
  try {
    while (url && featureCount < NETWORK_MAX_FEATURES) {
      const response = await fetch(url, { signal });
      if (!response.ok) {
        throw new Error(`PDOK returned HTTP ${response.status}`);
      }
      const payload = await response.json();
      const pageFeatures = payload.features || [];
      features.push(...pageFeatures);
      featureCount += pageFeatures.length;
      url = nextLink(payload);
    }

    networkLayer.clearLayers();
    networkLayer.addData({ type: "FeatureCollection", features });
    const capped = Boolean(url);
    elements.networkStatus.textContent = `${featureCount.toLocaleString()} PDOK road segments${capped ? " · zoom in for more detail" : ""}`;
  } catch (error) {
    if (error.name !== "AbortError") {
      elements.networkStatus.textContent = `Road network unavailable: ${error.message}`;
    }
  }
}

function scheduleNetworkLoad() {
  window.clearTimeout(state.networkTimer);
  state.networkTimer = window.setTimeout(loadNetwork, 280);
}

async function calculateRoute() {
  if (!state.start || !state.end) {
    return;
  }

  elements.routeButton.disabled = true;
  elements.routeButton.querySelector("span:last-child").textContent = "Calculating…";
  elements.routeMessage.textContent = "ZOO-Project is searching the NWB graph.";
  elements.routeMessage.classList.remove("error");

  const request = {
    inputs: {
      startPoint: `${state.start.lng},${state.start.lat}`,
      endPoint: `${state.end.lng},${state.end.lat}`,
      corridorMeters: 5000,
    },
    outputs: { Result: { transmissionMode: "value" } },
    response: "raw",
  };

  try {
    const payload = await executeProcess(ROUTING_PROCESS_URL, request, "application/geo+json");
    if (payload.type !== "FeatureCollection") {
      throw new Error("Routing did not return a FeatureCollection.");
    }
    if (!payload.features?.length) {
      throw new Error("No connected route was found inside the search corridor.");
    }

    routeLayer.clearLayers();
    routeLayer.addData(payload);
    map.fitBounds(routeLayer.getBounds(), { padding: [70, 70] });
    elements.routeDistance.textContent = `${(payload.distanceMeters / 1000).toFixed(2)} km`;
    elements.routeSegments.textContent = payload.numberReturned.toLocaleString();
    elements.routeResult.hidden = false;
    elements.routeMessage.textContent = "Route calculated. Loading AHN terrain profile…";
    try {
      await loadElevationProfile(payload);
      elements.routeMessage.textContent = "Route and AHN terrain profile calculated.";
    } catch (profileError) {
      elements.routeMessage.textContent = `Route calculated; elevation unavailable: ${profileError.message}`;
      elements.routeMessage.classList.add("error");
      showElevationError("Elevation profile unavailable");
    }
  } catch (error) {
    elements.routeMessage.textContent = error.message;
    elements.routeMessage.classList.add("error");
  } finally {
    elements.routeButton.disabled = false;
    elements.routeButton.querySelector("span:last-child").textContent = "Calculate route";
  }
}

function resetRoute() {
  for (const marker of [state.startMarker, state.endMarker]) {
    if (marker) {
      marker.remove();
    }
  }
  state.start = null;
  state.end = null;
  state.startMarker = null;
  state.endMarker = null;
  routeLayer.clearLayers();
  elements.routeResult.hidden = true;
  elements.elevationProfile.hidden = true;
  clearElevationMarker();
  setPlacement("start");
  updateRouteControls();
}

map.on("click", (event) => setPoint(state.placement, event.latlng));
map.on("moveend", scheduleNetworkLoad);
elements.startMode.addEventListener("click", () => setPlacement("start"));
elements.endMode.addEventListener("click", () => setPlacement("end"));
elements.routeButton.addEventListener("click", calculateRoute);
elements.resetButton.addEventListener("click", resetRoute);

lucide.createIcons();
updateRouteControls();
loadNetwork();
