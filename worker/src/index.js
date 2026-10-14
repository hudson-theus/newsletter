// COMPASS trigger.
//
// GitHub's own scheduled events are best-effort and have proved unreliable for
// this repo: measured over 2026-08-11..27 they dropped 12 of 112 fires (11%) and
// drifted every survivor 12-129 minutes, and on 08-27 all four morning fires were
// dropped outright and the edition never ran. A workflow_dispatch call is an
// ordinary API request and is not subject to that deprioritisation.
//
// This Worker is the primary trigger. GitHub's cron stays enabled as a backstop
// and the watchdog catches anything both of them miss; the workflow writes a
// dated shipped-marker, so whichever arrives first builds and the rest stand down.

const REPO = "hudson-theus/newsletter";
const WORKFLOW = "compass.yml";

/** Hour and weekday on the Chicago wall clock, which is what the editions key off. */
function chicagoNow() {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/Chicago",
    hour: "numeric", hour12: false, weekday: "short",
  }).formatToParts(new Date());
  const get = (t) => parts.find((p) => p.type === t)?.value;
  return { hour: Number(get("hour")) % 24, weekday: get("weekday") };
}

/**
 * Which edition, if any, is due right now.
 * Returning null is the normal case: half the cron fires exist only to cover the
 * other DST offset, and are expected to no-op.
 */
function editionDue({ hour, weekday }) {
  if (hour === 8) return "am";
  if (hour === 13 && !["Sat", "Sun"].includes(weekday)) return "pm";
  return null;
}

async function dispatch(edition, token) {
  const res = await fetch(
    `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`,
    {
      method: "POST",
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `Bearer ${token}`,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "compass-trigger",
        "Content-Type": "application/json",
      },
      // The dispatch API rejects raw booleans in `inputs`; these must be strings.
      body: JSON.stringify({
        ref: "main",
        inputs: { edition, send: "true", force: "false" },
      }),
    },
  );
  // 204 No Content is success for this endpoint.
  if (res.status !== 204) {
    throw new Error(`dispatch failed ${res.status}: ${(await res.text()).slice(0, 200)}`);
  }
  return `dispatched ${edition}`;
}

async function run(env) {
  const now = chicagoNow();
  const edition = editionDue(now);
  if (!edition) {
    return `no edition due (Chicago ${now.weekday} ${now.hour}:00)`;
  }
  if (!env.GITHUB_TOKEN) {
    throw new Error("GITHUB_TOKEN secret is not set");
  }
  // One retry. A single failed fetch must not cost an edition.
  try {
    return await dispatch(edition, env.GITHUB_TOKEN);
  } catch (e) {
    console.log(`first attempt failed (${e.message}) — retrying`);
    await new Promise((r) => setTimeout(r, 5000));
    return await dispatch(edition, env.GITHUB_TOKEN);
  }
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(
      run(env).then(
        (m) => console.log(m),
        (e) => console.error(`COMPASS trigger FAILED: ${e.message}`),
      ),
    );
  },

  // GET /fred?series=ID proxies FRED's official API for market.py. The key stays a
  // Worker secret (it is never in the public repo or a workflow log), and only the
  // series COMPASS charts are served, so this cannot be used as an open FRED proxy.
  // GET / reports what the Worker would do right now, so the whole chain can be
  // verified without waiting for a cron fire. It never dispatches.
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/fred") return fred(url, env, ctx);
    const now = chicagoNow();
    return Response.json({
      chicago: `${now.weekday} ${String(now.hour).padStart(2, "0")}:00`,
      edition_due: editionDue(now),
      token_configured: Boolean(env.GITHUB_TOKEN),
      fred_configured: Boolean(env.FRED_API_KEY),
      repo: REPO,
    });
  },
};

// The series market.py reads. Anything else is refused.
const FRED_SERIES = new Set([
  "DRCRELEXFACBS", "CPIAUCSL", "CPILFESL", "UNRATE", "BAMLH0A0HYM2",
]);

async function fred(url, env, ctx) {
  const id = url.searchParams.get("series") || "";
  if (!FRED_SERIES.has(id)) {
    return Response.json({ error: "series not served" }, { status: 404 });
  }
  if (!env.FRED_API_KEY) {
    return Response.json({ error: "FRED_API_KEY secret is not set" }, { status: 503 });
  }
  // FRED publishes these monthly or quarterly (daily for the HY spread); an hour
  // of edge cache costs nothing in freshness and keeps the key's quota untouched.
  const cache = caches.default;
  const key = new Request(`https://fred-cache.compass/${id}`);
  const hit = await cache.match(key);
  if (hit) return hit;
  const api = "https://api.stlouisfed.org/fred/series/observations"
    + `?series_id=${id}&api_key=${env.FRED_API_KEY}&file_type=json`
    + "&sort_order=desc&limit=24";
  const res = await fetch(api, { headers: { "User-Agent": "compass-trigger" } });
  if (!res.ok) {
    return Response.json({ error: `FRED answered ${res.status}` }, { status: 502 });
  }
  const body = await res.json();
  const obs = (body.observations || [])
    .filter((o) => o.value !== ".")
    .map((o) => ({ date: o.date, value: Number(o.value) }));
  const out = Response.json({ series: id, observations: obs },
    { headers: { "Cache-Control": "public, max-age=3600" } });
  ctx.waitUntil(cache.put(key, out.clone()));
  return out;
}
