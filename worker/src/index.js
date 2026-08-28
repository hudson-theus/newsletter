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

  // GET / reports what the Worker would do right now, so the whole chain can be
  // verified without waiting for a cron fire. It never dispatches.
  async fetch(request, env) {
    const now = chicagoNow();
    return Response.json({
      chicago: `${now.weekday} ${String(now.hour).padStart(2, "0")}:00`,
      edition_due: editionDue(now),
      token_configured: Boolean(env.GITHUB_TOKEN),
      repo: REPO,
    });
  },
};
