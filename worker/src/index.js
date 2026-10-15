// COMPASS trigger.
//
// GitHub's own scheduled events are best-effort and, for this repo, effectively
// useless on time: by October 2026 they ran only about half of the fires and
// started them 2-5 hours late. In 47 days of history no compass.yml schedule
// run started inside the morning window at all (earliest 10:59 CT), and the
// watchdog's 12:00 CT fire arrived between 14:00 and 18:30 CT. A
// workflow_dispatch call is an ordinary API request and starts within ~25 s.
//
// So this Worker is the only trigger that fires on time, for the editions AND
// for the watchdog that checks them:
//   - 08:15/08:45 and 13:15/13:45 CT it dispatches compass.yml (source=worker);
//   - 09:20 and 14:20 CT it dispatches watchdog.yml for the edition that was
//     just due, which verifies the shipped-marker, recovers a miss and alerts.
// GitHub's crons on both workflows remain as a last resort for a dead Worker.
// The workflow writes a dated shipped-marker, so whichever run ships first wins
// and the rest stand down.

const REPO = "hudson-theus/newsletter";
const WORKFLOW = "compass.yml";
const WATCHDOG = "watchdog.yml";

// event.cron is the expression exactly as written in wrangler.toml. These two
// dispatch the watchdog; every other cron dispatches the edition.
const WATCHDOG_CRONS = new Set(["20 14,15 * * *", "20 19,20 * * MON-FRI"]);

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

/**
 * Which edition the watchdog should verify right now: the one whose send time
 * (09:00 / 14:00 CT) passed twenty minutes ago. Null on the other DST twin.
 */
function watchdogDue({ hour, weekday }) {
  if (hour === 9) return "am";
  if (hour === 14 && !["Sat", "Sun"].includes(weekday)) return "pm";
  return null;
}

async function dispatch(workflow, inputs, token) {
  const res = await fetch(
    `https://api.github.com/repos/${REPO}/actions/workflows/${workflow}/dispatches`,
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
      body: JSON.stringify({ ref: "main", inputs }),
    },
  );
  // 204 No Content is success for this endpoint.
  if (res.status !== 204) {
    throw new Error(`dispatch failed ${res.status}: ${(await res.text()).slice(0, 200)}`);
  }
  return `dispatched ${workflow} ${JSON.stringify(inputs)}`;
}

async function run(env, cron) {
  const now = chicagoNow();
  const watchdog = WATCHDOG_CRONS.has(cron);
  const edition = watchdog ? watchdogDue(now) : editionDue(now);
  if (!edition) {
    return `no ${watchdog ? "watchdog" : "edition"} due (Chicago ${now.weekday} ${now.hour}:00)`;
  }
  if (!env.GITHUB_TOKEN) {
    throw new Error("GITHUB_TOKEN secret is not set");
  }
  // source=worker lets the workflow honour the COMPASS_PAUSE switch for this
  // trigger, and lets the watchdog tell a Worker dispatch from a manual one.
  const [workflow, inputs] = watchdog
    ? [WATCHDOG, { edition }]
    : [WORKFLOW, { edition, send: "true", force: "false", source: "worker" }];
  // One retry. A single failed fetch must not cost an edition.
  try {
    return await dispatch(workflow, inputs, env.GITHUB_TOKEN);
  } catch (e) {
    console.log(`first attempt failed (${e.message}) — retrying`);
    await new Promise((r) => setTimeout(r, 5000));
    return await dispatch(workflow, inputs, env.GITHUB_TOKEN);
  }
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(
      run(env, event.cron).then(
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
