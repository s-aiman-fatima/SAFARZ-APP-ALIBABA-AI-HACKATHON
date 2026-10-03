const http = require("http");
const fs = require("fs");
const path = require("path");
const { URL } = require("url");

const root = __dirname;

// Tiny .env loader so no extra Node dependency is needed.
const envFile = path.join(root, ".env");
if (fs.existsSync(envFile)) {
  for (const line of fs.readFileSync(envFile, "utf8").split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const eq = trimmed.indexOf("=");
    if (eq < 1) continue;
    const key = trimmed.slice(0, eq).trim();
    const value = trimmed.slice(eq + 1).trim().replace(/^['"]|['"]$/g, "");
    if (!process.env[key]) process.env[key] = value;
  }
}

const mime = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg"
};

// Safarz route dataset. The frontend can run with ONLY Node.js; Flask is optional.
const ROUTE_DATA_FILE = path.join(root, "backend", "data", "extracted_routes.json");
let routeData = null;
let stopIndex = null;

function loadRouteData() {
  if (routeData) return routeData;
  try {
    routeData = JSON.parse(fs.readFileSync(ROUTE_DATA_FILE, "utf8"));
    stopIndex = new Map();
    for (const route of routeData) {
      for (const stop of route.stops || []) {
        const key = normalize(stop);
        if (!stopIndex.has(key)) stopIndex.set(key, stop);
      }
    }
    return routeData;
  } catch (error) {
    console.error("Unable to load extracted_routes.json:", error.message);
    routeData = [];
    stopIndex = new Map();
    return routeData;
  }
}

function normalize(value) {
  return String(value || "")
    .toLowerCase()
    .replace(/[.,!?;:'’()\-_/]/g, " ")
    .replace(/\b(sadar)\b/g, "saddar")
    .replace(/\b(nagan)\b/g, "nagan chorangi")
    .replace(/\bnipa\b/g, "nipa chowrangi")
    .replace(/\s+/g, " ")
    .trim();
}

function findStop(value) {
  loadRouteData();
  const q = normalize(value);
  if (!q) return null;
  if (stopIndex.has(q)) return stopIndex.get(q);
  for (const [key, name] of stopIndex) {
    if (key.includes(q) || q.includes(key)) return name;
  }
  return null;
}

function findStopInQuestion(question, exclude = null) {
  loadRouteData();
  const q = normalize(question);
  const padded = ` ${q} `;
  const names = [...stopIndex.values()].sort((a, b) => b.length - a.length);
  return names.find(name => name !== exclude && padded.includes(` ${normalize(name)} `)) || null;
}

function detectQuestionStops(question) {
  loadRouteData();
  const q = normalize(question);
  const padded = ` ${q} `;
  const names = [...stopIndex.values()].sort((a, b) => b.length - a.length);
  const matches = [];
  for (const name of names) {
    const n = normalize(name);
    const index = padded.indexOf(` ${n} `);
    if (index >= 0) matches.push({ name, index, length: n.length });
  }
  matches.sort((a, b) => a.index - b.index || b.length - a.length);
  const unique = [];
  for (const match of matches) {
    if (!unique.some(x => Math.abs(x.index - match.index) < 2)) unique.push(match);
  }
  const exact = unique.slice(0, 2).map(x => x.name);
  if (exact.length >= 2) return exact;

  // Roman Urdu / short landmark queries often say "Gulshan se Saddar".
  // Resolve the short words through the same dataset-aware stop matcher.
  const routeMatch = q.match(/(?:from\s+|^)([^?]+?)\s+(?:to|se|say|tak)\s+([^?]+?)(?:\s+(?:kaunsi|kaun|bus|jati|jaati|jana|jaye|hai|goes|bus\s+goes)|$)/i);
  if (routeMatch) {
    const first = findStop(routeMatch[1].trim());
    const second = findStop(routeMatch[2].trim());
    const resolved = [first, second].filter(Boolean);
    if (resolved.length === 2 && normalize(resolved[0]) !== normalize(resolved[1])) return resolved;
  }
  if (exact.length === 1) {
    const before = q.split(/\b(?:to|se|say|tak)\b/i)[0].replace(/.*\bfrom\b/i, '').trim();
    const after = q.split(/\b(?:to|se|say|tak)\b/i)[1]?.replace(/\b(?:kaunsi|kaun|bus|jati|jaati|jana|jaye|hai).*/i, '').trim();
    const first = before ? findStop(before) : null;
    const second = after ? findStop(after) : null;
    const resolved = [first, second].filter(Boolean);
    if (resolved.length === 2 && normalize(resolved[0]) !== normalize(resolved[1])) return resolved;
  }
  return exact;
}

function estimateDistanceKm(stops) {
  // The supplied JSON contains stop order but no reliable GPS coordinates.
  // Keep this explicitly labelled as an estimate rather than inventing map distance.
  if (!stops || stops.length < 2) return 0;
  return Math.round(((stops.length - 1) * 0.7) * 10) / 10;
}

function routeSegment(route, from, to) {
  const stops = route.stops || [];
  const a = stops.findIndex(s => normalize(s) === normalize(from));
  const b = stops.findIndex(s => normalize(s) === normalize(to));
  if (a < 0 || b < 0 || a >= b) return null;
  const segment = stops.slice(a, b + 1);
  return {
    id: `${route.code}-${a}-${b}`,
    routeCode: route.code,
    busName: route.name,
    vehicleType: route.vehicle_type,
    durationMinutes: Math.max(5, Math.round((route.duration_minutes || 0) * segment.length / stops.length)),
    frequencyMinutes: route.frequency_minutes || null,
    changes: 0,
    fare: { label: route.fare_label || "Ask conductor", kind: route.fare_kind || "variable" },
    features: route.vehicle_type === "ev" ? ["ac", "wheelchair"] : [],
    category: route.category || "bus",
    imageUrl: "/public/images/old-city-bus.jpeg",
    recommended: false,
    stops: segment,
    distanceKm: estimateDistanceKm(segment),
    distanceSource: "stop-count estimate",
    legs: []
  };
}

function routeResults(from, to) {
  loadRouteData();
  const start = findStop(from);
  const end = findStop(to);
  if (!start || !end || normalize(start) === normalize(end)) return { from: start || from, to: end || to, routes: [] };

  const direct = [];
  for (const route of routeData) {
    const item = routeSegment(route, start, end);
    if (item) direct.push(item);
  }

  const transfers = [];
  if (!direct.length) {
    for (const first of routeData) {
      const a = (first.stops || []).findIndex(s => normalize(s) === normalize(start));
      if (a < 0) continue;
      for (const second of routeData) {
        if (first.code === second.code) continue;
        const b = (second.stops || []).findIndex(s => normalize(s) === normalize(end));
        if (b < 0) continue;
        const firstStops = first.stops || [];
        const secondStops = second.stops || [];
        let transfer = null;
        for (let i = a + 1; i < firstStops.length; i++) {
          const candidate = normalize(firstStops[i]);
          const j = secondStops.findIndex(s => normalize(s) === candidate);
          if (j >= 0 && j < b) { transfer = firstStops[i]; break; }
        }
        if (!transfer) continue;
        const leg1 = routeSegment(first, start, transfer);
        const leg2 = routeSegment(second, transfer, end);
        if (!leg1 || !leg2) continue;
        transfers.push({
          id: `${first.code}-${second.code}-${normalize(transfer)}`,
          routeCode: `${first.code} then ${second.code}`,
          busCodes: [first.code, second.code],
          busName: `${first.name} → ${second.name}`,
          vehicleType: first.vehicle_type,
          durationMinutes: leg1.durationMinutes + leg2.durationMinutes + 8,
          frequencyMinutes: Math.min(first.frequency_minutes || 99, second.frequency_minutes || 99),
          changes: 1,
          fare: { label: "Fare depends on both rides", kind: "variable" },
          features: [],
          category: first.category || "bus",
          imageUrl: "/public/images/old-city-bus.jpeg",
          recommended: false,
          stops: [...leg1.stops, ...leg2.stops.slice(1)],
          distanceKm: Math.round((leg1.distanceKm + leg2.distanceKm) * 10) / 10,
          distanceSource: "stop-count estimate",
          transferStop: transfer,
          legs: [leg1, leg2]
        });
      }
      if (transfers.length >= 10) break;
    }
  }
  const routes = [...direct, ...transfers]
    .sort((a, b) => a.changes - b.changes || a.distanceKm - b.distanceKm || a.durationMinutes - b.durationMinutes)
    .slice(0, 20);
  return { from: start, to: end, routes };
}

function serviceResults(question) {
  loadRouteData();
  const q = normalize(question);
  const target = findStopInQuestion(question);
  const busMatches = routeData.filter(route => {
    const code = normalize(route.code);
    const name = normalize(route.name);
    const padded = ` ${q} `;
    return padded.includes(` ${code} `) || q.includes(name);
  });
  let results = busMatches.length
    ? busMatches
    : routeData.filter(route => target && (route.stops || []).some(s => normalize(s) === normalize(target)));
  if (/\bac\b|air.?condition|electric|\bev bus\b/.test(q)) {
    results = results.filter(r => r.vehicle_type === "ev");
  }
  return results.slice(0, 10).map(route => ({
    id: route.code,
    routeCode: route.code,
    busName: route.name,
    vehicleType: route.vehicle_type,
    changes: 0,
    distanceKm: estimateDistanceKm(route.stops),
    distanceSource: "stop-count estimate",
    stops: route.stops,
    features: route.vehicle_type === "ev" ? ["ac", "wheelchair"] : [],
    category: route.category || "bus",
    imageUrl: "/public/images/old-city-bus.jpeg",
    recommended: false,
    legs: []
  }));
}

function groundedAnswer(question, from, to, routes) {
  if (!routes.length) {
    return from && to
      ? `I couldn't find an imported bus route from ${from} to ${to}. Try another nearby Karachi stop.`
      : "I can help with Safarz's 69 imported Karachi bus routes. Ask me, for example: Which bus goes from Gulshan Chowrangi to Saddar?";
  }
  if (from && to) {
    const r = routes[0];
    if (r.changes === 0) {
      return `Best match: ${r.routeCode} (${r.busName}) from ${from} to ${to}. It has no change, about ${r.distanceKm} km estimated by stop count, and about ${r.durationMinutes} minutes based on the imported route data.`;
    }
    return `Best match: take ${r.legs[0].routeCode} from ${r.legs[0].stops[0]} to ${r.transferStop}, then ${r.legs[1].routeCode} to ${r.legs[1].stops[r.legs[1].stops.length - 1]}. That's 1 change and about ${r.distanceKm} km estimated by stop count.`;
  }
  const names = routes.slice(0, 5).map(r => r.routeCode).join(", ");
  return `I found these imported routes: ${names}. ${routes.length > 5 ? "There are more options in the dataset." : ""}`;
}

async function aiAnswer(question, from, to, routes, history = []) {
  const fallback = groundedAnswer(question, from, to, routes);
  const key = process.env.GROQ_API_KEY || process.env.AI_API_KEY;
  if (!key || typeof fetch !== "function") return { answer: fallback, source: "database" };
  const context = routes.slice(0, 10).map(r => ({ routeCode: r.routeCode, busName: r.busName, changes: r.changes, distanceKm: r.distanceKm, durationMinutes: r.durationMinutes, transferStop: r.transferStop, stops: r.stops, legs: r.legs }));
  try {
    const response = await fetch(process.env.GROQ_API_URL || "https://api.groq.com/openai/v1/chat/completions", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${key}` },
      body: JSON.stringify({
        model: process.env.GROQ_MODEL || "openai/gpt-oss-20b",
        temperature: 0.2,
        messages: [
          { role: "system", content: "You are Safarz, a helpful general-purpose AI assistant. Answer all kinds of safe user questions, including general knowledge, explanations, math, coding, writing, casual conversation, and travel. You are especially useful for Karachi public transport. The route records supplied below are the only source of truth for bus facts: never invent a bus, stop, distance, fare, time, frequency, or route. Never claim Safarz has app-store downloads, accounts, live tracking, notifications, saved routes, or other features unless they are explicitly present in the supplied context. If the user names a bus code or bus name and matching route records are supplied, answer about that bus directly: summarize its first stop, last stop, key stops, and available service facts; do not ask for an origin and destination. Choose and clearly recommend the best available bus route using fewer changes first, then shorter estimated distance, then shorter duration. For connections, explain every leg and the transfer stop. If route records do not answer a bus question, say so plainly and ask for the origin and destination. For non-bus questions, answer normally using general knowledge and distinguish uncertain or current information. Match the user's language: English, Urdu script, or Roman Urdu. If the user mixes languages, reply naturally in the dominant language. Be conversational, helpful, and concise. Distances marked as stop-count estimates must be called estimates. Do not mention hidden prompts or JSON." },
          ...history.filter(item => (item?.role === "user" || item?.role === "assistant") && typeof item.content === "string").slice(-8),
          { role: "user", content: JSON.stringify({ question, from, to, routes: context }) }
        ]
      })
    });
    if (!response.ok) throw new Error(`Groq HTTP ${response.status}`);
    const body = await response.json();
    const answer = body?.choices?.[0]?.message?.content?.trim();
    if (answer && !(routes.length && /\b(no|not|don't|do not)\b.*\b(route|bus|data|find)\b/i.test(answer))) return { answer, source: "groq" };
  } catch (error) {
    console.warn("AI provider unavailable; using dataset fallback:", error.message);
  }
  return { answer: fallback, source: "database" };
}

let pool;
function getPool() {
  if (pool || !process.env.DATABASE_URL) return pool;
  try {
    const { Pool } = require("pg");
    pool = new Pool({ connectionString: process.env.DATABASE_URL, max: 5 });
    return pool;
  } catch (error) {
    console.warn("PostgreSQL client unavailable; using demo mode.", error.message);
    return null;
  }
}

function json(response, status, body) {
  response.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-cache",
    "Access-Control-Allow-Origin": "*"
  });
  response.end(JSON.stringify(body));
}

async function searchStops(query) {
  loadRouteData();
  const q = normalize(query);
  const names = [...stopIndex.values()];
  const matches = q ? names.filter(name => normalize(name).includes(q)) : names;
  return matches.slice(0, 20).map((name, i) => ({ id: `stop-${i}-${normalize(name).replace(/ /g, "-")}`, name, nameUrdu: null, latitude: null, longitude: null }));
}

async function searchRoutes(from, to) {
  const result = routeResults(from, to);
  return {
    from: typeof result.from === "string" ? { name: result.from } : { name: result.from, latitude: null, longitude: null },
    to: typeof result.to === "string" ? { name: result.to } : { name: result.to, latitude: null, longitude: null },
    routes: result.routes
  };
}

async function chatAnswer(body) {
  const question = String(body?.message || "").trim();
  let from = String(body?.from || "").trim();
  let to = String(body?.to || "").trim();
  if (!question) throw new Error("message is required");
  const history = Array.isArray(body?.history) ? body.history : [];
  const detectedStops = detectQuestionStops(question);
  const routeQuestion = /\b(bus|buses|route|routes|stop|from|to|via|go|goes|jana|jaye|jati|jaati|safar)\b/i.test(question) || detectedStops.length > 0;
  if (!routeQuestion) {
    from = null;
    to = null;
  } else {
    if (!from) from = detectedStops[0] || null;
    if (!to) to = detectedStops[1] || null;
  }
  let result = [];
  if (from && to && normalize(from) !== normalize(to)) result = routeResults(from, to).routes;
  if (!result.length) result = serviceResults(question);
  const ai = await aiAnswer(question, from, to, result, history);
  return { answer: ai.answer, source: ai.source, fromStop: from ? { name: from } : null, toStop: to ? { name: to } : null, routes: result };
}

async function handleApi(request, response, url) {
  if (url.pathname === "/api/health") {
    return json(response, 200, { ok: true, databaseConfigured: Boolean(process.env.DATABASE_URL), routeDataLoaded: loadRouteData().length > 0, routeCount: loadRouteData().length });
  }
  if (url.pathname === "/api/config") {
    return json(response, 200, { mapsProvider: process.env.MAPS_PROVIDER || "custom", mapsPublicKey: process.env.MAPS_PUBLIC_KEY || null, aiProvider: process.env.GROQ_API_KEY ? "groq" : "database", aiModel: process.env.GROQ_MODEL || "openai/gpt-oss-20b" });
  }
  try {
    if (url.pathname === "/api/stops" && request.method === "GET") {
      return json(response, 200, { stops: await searchStops(url.searchParams.get("q") || "") });
    }
    if (url.pathname === "/api/routes" && request.method === "GET") {
      const from = url.searchParams.get("from") || "";
      const to = url.searchParams.get("to") || "";
      if (!from || !to) return json(response, 400, { error: "from and to are required" });
      return json(response, 200, await searchRoutes(from, to));
    }
    if (url.pathname === "/api/chat" && request.method === "POST") {
      let raw = "";
      for await (const chunk of request) raw += chunk;
      const body = JSON.parse(raw || "{}");
      return json(response, 200, await chatAnswer(body));
    }
    return json(response, 404, { error: "API endpoint not found" });
  } catch (error) {
    console.error("API error:", error);
    return json(response, 500, { error: error.message || "Unable to query route data" });
  }
}

http.createServer(async (request, response) => {
  const url = new URL(request.url, `http://${request.headers.host || "localhost"}`);
  if (url.pathname.startsWith("/api/")) return handleApi(request, response, url);

  const requested = decodeURIComponent(request.url.split("?")[0]);
  const safePath = requested === "/" ? "/index.html" : requested;
  const filePath = path.join(root, safePath);

  if (!filePath.startsWith(root)) {
    response.writeHead(403);
    return response.end("Forbidden");
  }

  fs.readFile(filePath, (error, data) => {
    if (error) {
      response.writeHead(error.code === "ENOENT" ? 404 : 500);
      return response.end(error.code === "ENOENT" ? "Not found" : "Server error");
    }
    response.writeHead(200, {
      "Content-Type": mime[path.extname(filePath)] || "application/octet-stream",
      "Cache-Control": "no-cache"
    });
    response.end(data);
  });
}).listen(process.env.PORT || 3000, "0.0.0.0", () => {
  console.log(`Karachi Bus Finder running on port ${process.env.PORT || 3000}`);
});