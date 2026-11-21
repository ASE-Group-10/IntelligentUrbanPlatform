const $ = (id) => document.getElementById(id);
const common = { timezone: "Europe/Dublin", operating_hours: [], restrictions: [] };
const templates = {
  vehicle: { vehicle_category: "van", max_weight_kg: 800, max_volume_m3: 6, powertrain: "electric", emission_class: "zero_tailpipe", range_km: 150, autonomous: false, ...common },
  depot: { storage_capacity_m3: 250, loading_bays: 2, supported_vehicle_categories: ["van", "cargo_bike"], ...common },
  warehouse: { storage_capacity_m3: 1000, handling_capabilities: ["ambient", "pallets"], ...common },
  locker: { public_access: true, max_parcel_weight_kg: 20, compartments: [{ size: "small", count: 12, length_cm: 40, width_cm: 30, height_cm: 20 }], ...common },
};
let locations = [], selected = null, lastMutation = null, pending = null, nextCursor = null, busy = false;
let viewGeneration = 0;

function message(text, error = false) {
  $("message").hidden = !text;
  $("message").className = error ? "error" : "";
  $("message").textContent = text;
}
function showResponse(path, status, body, replayed = false) {
  $("request-label").textContent = `${path} · HTTP ${status}${replayed ? " · replayed" : ""}`;
  $("response-body").textContent = JSON.stringify(body, null, 2);
}
async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { Authorization: `Bearer ${$("identity").value}`,
      ...($("provider").value ? { "X-Acting-Provider": $("provider").value } : {}), ...options.headers },
  });
  const body = await response.json();
  showResponse(path, response.status, body, response.headers.get("Idempotency-Replayed") === "true");
  if (!response.ok) {
    const fields = body.error?.fields?.map((field) => `${field.path}: ${field.message}`).join("\n");
    throw new Error([body.error?.message || "Request failed.", fields].filter(Boolean).join("\n"));
  }
  return body;
}
async function mutate(path, body, repeat = false) {
  const headers = { "Content-Type": "application/json", "X-Mock-Location-Result": $("mapping-result").value, "X-Mock-Agent-Result": $("agent-result").value };
  const signature = JSON.stringify({ path, body, headers });
  if (!repeat && pending?.signature !== signature) pending = { signature, key: crypto.randomUUID() };
  const request = repeat ? lastMutation : { path, body, headers: { ...headers, "Idempotency-Key": pending.key } };
  lastMutation = structuredClone(request);
  $("replay").disabled = false;
  const result = await api(request.path, { method: "POST", headers: request.headers, body: JSON.stringify(request.body) });
  pending = null;
  return result;
}
function resetForm(clearMessage = true) {
  selected = null;
  $("form-title").textContent = "Register resource";
  $("editing").textContent = "New resources start with a validated profile.";
  $("save").textContent = "Register resource";
  $("activate").disabled = true;
  $("resource-type").disabled = false;
  $("profile-status").value = "active";
  $("availability").value = "available";
  fillTemplate();
  if (clearMessage) message("");
  document.querySelectorAll(".resource-card").forEach((card) => card.classList.remove("selected"));
}
function fillTemplate() {
  const type = $("resource-type").value;
  $("details").value = JSON.stringify(templates[type], null, 2);
  $("identifier").value = `${type.toUpperCase()}-${Date.now().toString().slice(-7)}`;
  $("name").value = `Test ${type}`;
}
function selectResource(resource) {
  selected = resource;
  $("form-title").textContent = "Edit resource";
  $("editing").textContent = `${resource.id} · version ${resource.version}`;
  $("save").textContent = "Save changes";
  $("activate").disabled = resource.agent_status === "active";
  $("resource-type").value = resource.resource_type;
  $("resource-type").disabled = true;
  $("identifier").value = resource.external_identifier;
  $("name").value = resource.name;
  $("profile-status").value = resource.profile_status;
  $("availability").value = resource.observed_availability;
  $("details").value = JSON.stringify(resource.details, null, 2);
  let index = locations.findIndex((location) => location.address === resource.address);
  if (index < 0) {
    index = locations.length;
    locations.push({ address: resource.address, latitude: resource.latitude, longitude: resource.longitude });
    const option = document.createElement("option"); option.value = index; option.textContent = resource.address;
    $("location").append(option);
  }
  $("location").value = index;
  document.querySelectorAll(".resource-card").forEach((card) => card.classList.toggle("selected", card.dataset.id === resource.id));
}
function renderCard(resource) {
  const button = document.createElement("button");
  button.type = "button"; button.className = "resource-card";
  button.dataset.id = resource.id;
  button.classList.toggle("selected", selected?.id === resource.id);
  const title = document.createElement("strong"); title.textContent = resource.name;
  const info = document.createElement("p"); info.textContent = `${resource.external_identifier} · ${resource.resource_type} · v${resource.version}`;
  const badges = document.createElement("div"); badges.className = "badges";
  for (const [text, style] of [[resource.planning_eligible ? "Planning eligible" : "Not eligible", resource.planning_eligible ? "good" : "warn"], [`Agent ${resource.agent_status}`, ""], [resource.profile_status, ""]]) {
    const badge = document.createElement("span"); badge.className = `badge ${style}`; badge.textContent = text; badges.append(badge);
  }
  button.append(title, info, badges);
  button.onclick = () => run(async () => {
    const current = await api(`/api/v1/resources/${resource.id}`); selectResource(current); message("");
  });
  return button;
}
async function refresh(append = false) {
  const generation = ++viewGeneration;
  const query = new URLSearchParams({ limit: "10" });
  if ($("type-filter").value) query.set("resource_type", $("type-filter").value);
  if (append && nextCursor) query.set("cursor", nextCursor);
  const data = await api(`/api/v1/resources?${query}`);
  if (generation !== viewGeneration) return;
  if (!append) $("resource-list").replaceChildren();
  data.items.forEach((resource) => $("resource-list").append(renderCard(resource)));
  if (!$("resource-list").children.length) {
    const empty = document.createElement("p"); empty.className = "empty"; empty.textContent = "No resources yet. Register one to begin."; $("resource-list").append(empty);
  }
  nextCursor = data.next_cursor;
  $("load-more").hidden = !nextCursor;
}
async function run(action) {
  if (busy) return;
  busy = true;
  document.querySelectorAll("button, select, input, textarea").forEach((control) => { control.disabled = true; });
  try { await action(); }
  catch (error) { message(error.message || "Connection interrupted. Retry without changing the form to reuse the same key.", true); }
  finally {
    busy = false;
    document.querySelectorAll("button, select, input, textarea").forEach((control) => { control.disabled = false; });
    $("resource-type").disabled = Boolean(selected);
    $("activate").disabled = !selected || selected.agent_status === "active";
    $("replay").disabled = !lastMutation;
  }
}
$("resource-form").onsubmit = (event) => {
  event.preventDefault();
  run(async () => {
    let details;
    try { details = JSON.parse($("details").value); } catch { throw new Error("Details must be valid JSON."); }
    const location = locations[Number($("location").value)];
    const body = { external_identifier: $("identifier").value, name: $("name").value, ...location,
      profile_status: $("profile-status").value, observed_availability: $("availability").value, details };
    const editing = Boolean(selected);
    if (editing) body.expected_version = selected.version;
    else body.resource_type = $("resource-type").value;
    const resource = await mutate(editing ? `/api/v1/resources/${selected.id}` : "/api/v1/resources", body);
    await refresh(); selectResource(resource);
    showResponse(editing ? `POST /resources/${resource.id}` : "POST /resources", editing ? 200 : 201, resource);
    message(`${editing ? "Resource updated" : "Resource registered"}. Agent ${resource.agent_status}. ${resource.planning_eligible ? "Eligible for planning." : "Not eligible for planning."}`);
  });
};
$("activate").onclick = () => run(async () => {
  if (!selected) return;
  const resource = await mutate(`/api/v1/resources/${selected.id}/agent-activation`, { expected_version: selected.version });
  await refresh(); selectResource(resource); showResponse("POST /agent-activation", resource.agent_status === "pending" ? 202 : 200, resource);
  message(`Agent ${resource.agent_status}.`);
});
$("replay").onclick = () => run(async () => {
  if (!lastMutation) return;
  const resource = await mutate(null, null, true);
  message(`Original response replayed for ${resource.external_identifier}; no duplicate write.`);
});
$("new-resource").onclick = () => resetForm();
$("resource-type").onchange = fillTemplate;
$("refresh").onclick = () => run(() => refresh());
$("load-more").onclick = () => run(() => refresh(true));
$("type-filter").onchange = () => run(() => refresh());
$("provider").onchange = $("identity").onchange = () => run(async () => {
  pending = null; lastMutation = null; $("replay").disabled = true;
  $("resource-list").replaceChildren(); showResponse("", "", {}); resetForm();
  if ($("identity").value) await refresh();
});
run(async () => {
  const [config, health] = await Promise.all([
    fetch("/api/v1/development/config").then((response) => response.json()),
    fetch("/health").then((response) => { if (!response.ok) throw new Error("Database connection unavailable."); return response.json(); }),
  ]);
  locations = config.locations;
  locations.forEach((location, index) => { const option = document.createElement("option"); option.value = index; option.textContent = location.address; $("location").append(option); });
  $("connection").textContent = health.status === "ok" ? "● Database connected" : "Database unavailable";
  resetForm(); if ($("identity").value) await refresh();
});
