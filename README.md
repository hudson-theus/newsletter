# COMPASS

A personal news brief, emailed at **9:00 AM CT every day** (morning edition) and
**2:00 PM CT on weekdays** (afternoon edition). Free sources only, curated by
Claude against a written editorial spec, rendered as an HTML email with an
animated cover drawn from the day's market numbers.

- What goes in the brief, and why: [`the-spread-editorial-spec.md`](the-spread-editorial-spec.md)
- The curator's instructions: [`prompts/curate.md`](prompts/curate.md)
- This file: how it runs, and what to do when it doesn't.

The morning edition is a full brief. The afternoon edition is "what changed since
this morning": it is handed the morning's items and leads with what moved since
9:00, so it is shorter and skips sections with nothing new.

---

## How a run is triggered

GitHub's scheduled workflows are not punctual enough to deliver on the minute. By
October 2026 they ran about half their fires and started them 2-5 hours late. So
the clock lives in a **Cloudflare Worker** (`worker/`), and GitHub's crons are a
last resort.

| Chicago time | Who fires | What happens |
|---|---|---|
| 08:15, 08:45 daily | Worker | dispatches `compass.yml` with `edition=am`, `source=worker` |
| 09:00 | the 08:15 run | the morning edition is sent |
| 09:20 daily | Worker | dispatches `watchdog.yml` with `edition=am` |
| 13:15, 13:45 Mon-Fri | Worker | dispatches `compass.yml` with `edition=pm`, `source=worker` |
| 14:00 | the 13:15 run | the afternoon edition is sent |
| 14:20 Mon-Fri | Worker | dispatches `watchdog.yml` with `edition=pm` |
| various | GitHub cron | backstops in both workflows, for a dead Worker; they usually arrive hours late |

- **Daylight saving.** Cloudflare crons are UTC-only, so every slot fires at both
  candidate UTC hours and the Worker checks the real Chicago clock; the twin that
  lands at the wrong hour does nothing.
- **Build early, send on the minute.** A run builds the issue as soon as it
  starts, then `publish.py` holds the send until exactly 09:00 or 14:00 CT. A run
  that starts after the send time sends immediately (late beats never).
- **Only one email per edition.** The first run to send uploads a dated
  *shipped-marker* artifact (below). Every later run of that edition, whoever
  started it, looks for the marker and stands down. The 08:45 and 13:45 slots
  exist only to retry a failed 08:15 or 13:15 run.
- **The watchdog** (`watchdog.yml`) checks that today's marker exists. If it is
  missing, it dispatches a recovery run of `compass.yml` with `force=false` and
  `source=watchdog` (so the recovery still stands down if the edition shipped in
  the meantime) and alerts with what happened. It also reports when the Worker did
  not dispatch an edition at all. Run by hand with `edition=due`, it checks every
  edition due so far today.

`compass.yml` can also be run by hand from the Actions tab (`workflow_dispatch`):

| Input | Values | Meaning |
|---|---|---|
| `edition` | `am` / `pm` | which edition to build |
| `send` | true / false | send the email; false is a dry run |
| `force` | true / false | build even if today's marker exists. **Can send a second copy.** |
| `source` | `manual` / `worker` / `watchdog` | who dispatched it; leave `manual` by hand |

---

## The pipeline

What a `compass.yml` run does once it decides to build, in order (a run that
finds the edition already shipped, or is paused, stops before any of this).
Everything except curation is deterministic
Python, standard library only. Pillow is optional: `art.py` draws the animated
images with it, and without it the edition ships with no images.

| Stage | Script | Reads | Writes |
|---|---|---|---|
| Fetch earlier editions | workflow step | shipped-marker artifacts | `prior/` (previous days; plus this morning's edition on a PM run) |
| Collect candidates | `scripts/collect.py` | the RSS feeds in its `FEEDS` list, `prior/` | `candidates.json` |
| Collect market numbers | `scripts/market.py` | Treasury.gov, Freddie Mac, Nasdaq, FRED (through the Worker) | `market.json` (scalars), `market_series.json` (chart data) |
| Flag dark sections | `scripts/sections.py --prep` | `state/sections.json` | `attention` in `candidates.json` |
| Curate (up to 3 attempts) | claude-code-action + `prompts/curate.md` | `candidates.json`, `market.json`, the spec | `issue.json` |
| Check each attempt | `scripts/check_issue.py`, `scripts/curator_summary.py` | `issue.json`, the action's execution log | `rejected/` on a bad attempt; a summary in the run's step summary |
| Fall back | `scripts/fallback.py` | `candidates.json` | a plain `issue.json` marked `"_fallback": true` |
| Verify, render, send | `scripts/publish.py` (+ `compass.py`, `scripts/art.py`) | everything above | `issue_<date>_<ed>.html`, GIFs, `sent.ok`, `issue_<date>_<ed>.eml` |
| Mark shipped | workflow step | `sent.ok` | the `compass-<ed>-<date>` artifact |
| Record coverage | `scripts/sections.py --record` | `issue.json` | a commit to `state/sections.json` (skipped for a fallback) |

Things worth knowing about the stages:

- **Candidates are the only source of links.** The curator can read files and
  write `issue.json`, nothing else: no web, no shell. `publish.py` then drops any
  link that is not in `candidates.json` (provenance), fetches the rest and drops
  any that are gone (resolution), and strips any markup in item text other than
  bold and italics. A failed link ships the item unlinked; the footer reports how
  many were checked and dropped.
- **Nothing ships twice across days.** `collect.py` removes anything whose link
  or headline shipped on an earlier day, and hands the curator the last two days'
  headlines (`recently_shipped`) to catch the same story from a new outlet.
- **Satire is flagged in `collect.py`** and labelled `SATIRE:` by `publish.py`,
  whatever the curator wrote.
- **The fallback** is what ships when no curation attempt produced a usable
  issue: real headlines and links in the standing sections, no commentary. Its
  front matter says it is unedited, and an alert goes out.
- **Never raise.** `market.py`, `art.py` and `fallback.py` are written so that
  their failure costs a number or an image, never the edition. A plain brief beats
  no brief.

### The shipped-marker

`compass-<am|pm>-<YYYY-MM-DD>`, uploaded right after the SMTP server accepts the
message (`publish.py` writes `sent.ok`; the upload is gated on it). Its existence
is the only "already shipped" signal every trigger checks. It holds the sent
message (`issue_*.eml`), the HTML and GIFs, `issue.json`, `candidates.json`,
`market.json` and `market_series.json`, and is kept 7 days, which is also how far
back `collect.py` can look for earlier editions.

A dry run (`send=false`) never uploads a marker, so it can never stand down the
real edition.

Other artifacts: `preview-<ed>-<run_id>` (a dry run's output, 3 days),
`rejected-<run_id>` (rejected curation attempts with their reasons and a summary
of each attempt, 14 days), `failed-<run_id>` (a failed run's output, 7 days).
The repository is public, so these are readable by any signed-in GitHub user. Raw
curator transcripts are never uploaded, only their summaries.

---

## Secrets and variables

**GitHub repository secrets** (Settings → Secrets and variables → Actions):

| Secret | Used by | Notes |
|---|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | curation | Generated with `claude setup-token`. When it expires or is revoked, every curation attempt fails within seconds and the fallback ships. |
| `GMAIL_ADDRESS` | `publish.py`, `alert.py` | The sending Gmail account and SMTP login. Its domain is also the `Message-ID` domain. |
| `GMAIL_APP_PASSWORD` | `publish.py`, `alert.py` | A 16-character Google app password, stored without spaces. Needs 2-step verification on; Google revokes app passwords when the account password changes. |
| `RECIPIENT` | `publish.py`, `alert.py` | Where editions and email alerts go. |

The workflows' own `GITHUB_TOKEN` is automatic: it reads the marker artifacts,
dispatches recovery runs, comments on the alerts issue and pushes the coverage
commit.

**GitHub repository variables** (same page, Variables tab):

| Variable | Effect |
|---|---|
| `COMPASS_PAUSE` | `true` stands down every automated run: the Worker's dispatches, the GitHub crons and the watchdog's recoveries. Manual dispatches still run. Unset (or anything else) means run. **This is the kill switch.** |
| `COMPASS_SEND` | Gates only runs started by GitHub's own cron: `true` lets them send, anything else makes them dry runs. Dispatched runs carry their own `send` input and ignore it. |

**Cloudflare Worker secrets** (`compass-trigger`; set with
`npx wrangler secret put NAME` from `worker/`, or in the dashboard):

| Secret | Notes |
|---|---|
| `GITHUB_TOKEN` | A GitHub personal access token that can dispatch workflows on this repository (fine-grained: Actions read and write). It has the expiry chosen when it was created; when it lapses, the Worker's dispatches fail with 401, every edition falls to the late GitHub crons, and the watchdog reports "the Worker did not dispatch". |
| `FRED_API_KEY` | A free key from fred.stlouisfed.org. The Worker serves `/fred?series=ID` to `market.py` for exactly five series, so the key never enters the repo or a workflow log. |

Rotation dates are not recorded anywhere automatically. When you create or rotate
a token, note its expiry somewhere you will see it.

---

## Alerts

`scripts/alert.py SUBJECT BODY` sends every alert to two places, so a Gmail
problem cannot silence both the edition and the alert about it:

1. **Email** to `RECIPIENT`, subject prefixed `[SPREAD]`.
2. **A comment on the open GitHub issue titled "COMPASS alerts"** (created if
   absent). Watch the repository or subscribe to that issue to be notified.

Alerts are raised for: a missing or late edition, a recovery that did not ship, a
marker that could not be checked, the Worker not dispatching an edition (all from
the watchdog); an edition that shipped as the unedited fallback; a run that
failed; curation touching files other than `issue.json`; a coverage commit that
could not be pushed.

GitHub's own notifications are a third channel: with "Actions → notify on failed
workflows" on in your GitHub notification settings, a failed run emails you,
including runs the Worker dispatched (they run as the token's owner). Worker
errors themselves are in Cloudflare → Workers → `compass-trigger` → Logs.

### The Gmail Apps Script

Not in this repo: an Apps Script in the recipient's Google account routes mail
whose subject starts with `[SPREAD]`. Editions (`[SPREAD] COMPASS AM / Oct 15`)
and email alerts both carry that prefix. If a marker exists but the email is not
where you expect it, the pipeline sent it (the SMTP server accepted it): look at
the script and Gmail's filters, not at the workflow. Every edition has a fixed
Message-ID, `<compass-<ed>-<YYYY-MM-DD>@<sender domain>>`, so Gmail's search
`rfc822msgid:compass-am-2026-10-15@gmail.com` finds it wherever it landed.

---

## Dry runs

Actions → **COMPASS** → Run workflow → pick the edition, leave **send** unticked.
The run builds everything (real feeds, real numbers, real curation), skips the
send and the hold, and uploads `preview-<ed>-<run_id>` with the HTML, GIFs,
`issue.json`, `candidates.json`, `market.json` and `market_series.json`. Download
it and open the HTML in a browser.

If today's edition has already shipped, a dry run stands down like any other run;
tick **force** to build anyway. With send unticked, force cannot send anything.

Locally (no curation; the fallback stands in for it):

```sh
python scripts/collect.py --edition am
python scripts/market.py
python scripts/sections.py --prep
python scripts/fallback.py            # or write issue.json yourself
python scripts/publish.py --edition am   # no --send: writes issue_<date>_am.html
```

---

## Deploying the Worker

The Worker's code and crons are in `worker/` (`src/index.js`, `wrangler.toml`).
A change there does nothing until it is deployed:

```sh
cd worker && npx wrangler deploy
```

If the Cloudflare dashboard's Workers Builds integration is connected to this
repository instead, it deploys on push, but only with **Root directory** set to
`worker` (Settings → Build); otherwise it cannot find `wrangler.toml`.

Verify after deploying:

- `GET https://compass-trigger.hudson-394.workers.dev/` reports the Chicago time,
  which edition is due, and whether `GITHUB_TOKEN` and `FRED_API_KEY` are set. It
  never dispatches anything.
- `GET https://compass-trigger.hudson-394.workers.dev/fred?series=UNRATE` returns
  recent observations.
- At the next slot, the Actions tab shows a run named `COMPASS am · worker` (or
  `pm`).

---

## The kill switch

Set the repository variable **`COMPASS_PAUSE` to `true`**. Every automated run
(Worker, GitHub cron, watchdog recovery) then stands down at its first step, and
the watchdog stops alerting. Manual dispatches still run, so you can still send
one by hand. Delete the variable, or set it to anything else, to resume.

The Worker keeps firing while paused; its runs just end in seconds. To stop even
that, disable the Worker's cron triggers in the Cloudflare dashboard.

---

## Runbook

**An edition did not arrive.**
1. Check the "COMPASS alerts" issue. The watchdog runs at 09:20 / 14:20 CT and
   says whether the edition shipped, was recovered, or is missing.
2. Look for today's `compass-<ed>-<date>` artifact. If it exists, the edition was
   sent: see *The Gmail Apps Script* above.
3. Actions tab: is there a `COMPASS <ed> · worker` run today? If not, the Worker
   did not dispatch: open the Worker's status page and its Cloudflare logs. A 401
   means its `GITHUB_TOKEN` expired: create a new token and
   `npx wrangler secret put GITHUB_TOKEN`.
4. If a run exists and failed, its log says where. To send the edition by hand:
   Run workflow with the edition, **send** ticked, **force** unticked. Force is
   only for re-sending an edition you know went out; it skips the marker check and
   sends a second copy.

**An edition shipped unedited (the fallback).** Its front matter says so, and an
alert names the reasons. The `rejected-<run_id>` artifact holds each rejected
attempt's `issue.json`, `reasons.txt` and a summary of the attempt (turns, cost,
refused tool calls, final message); the same summary is in the run's step
summary. Attempts failing within seconds usually mean `CLAUDE_CODE_OAUTH_TOKEN`
expired: regenerate it with `claude setup-token` and update the secret. A fallback
edition is not recorded in section coverage. Re-sending a curated version means a
second email (`force=true`); usually just let the next edition run.

**The numbers were wrong.** The edition's `market.json` is in its marker artifact,
so you can tell whether the input or the prose was wrong. The *Collect market
numbers* step logs each source's value and date, and a `::warning::` for any
source that was missing, stale or dropped (including FRED). Each source has its
own date key (`treasury_as_of`, `mortgage30_as_of`, `spy_session`, and `as_of`
inside each FRED value); the Treasury print is normally the previous business
day's close. If the input was right and the prose was wrong, tighten the
**Numbers** section of `prompts/curate.md`. If FRED series are missing, check
`fred_configured` on the Worker's status page and the `/fred` route.

**A feed died.** The *Collect candidates* step logs every feed with its item count,
or `FAILED (reason) URL`. Feeds live in `FEEDS` in `scripts/collect.py`, each as
(section hint, outlet name, URL). Test a replacement from a GitHub runner, not a
laptop: several publishers serve bot walls (403, 429, an empty 202) only to cloud
IP ranges. Remove a feed that fails every run rather than leaving it to normalise
`FAILED` lines in the log. If a section thins out, `sections.py` will start
flagging it in `attention`.

**The curator keeps getting something wrong.** Change `prompts/curate.md` (the
curator's working instructions, which win where they are more specific) or the
spec. Both are read fresh on every run; a dry run shows the effect.

---

## Housekeeping

- **`state/sections.json`** is committed by `compass-bot` after every sent
  edition. That commit also keeps the repository active: GitHub disables scheduled
  workflows after 60 days without a push, and the watchdog adds a `.keepalive`
  commit if a month passes without one.
- **`compass_am.png`, `compass_pm.png`, `banner_am.gif`, `banner_pm.gif`** are not
  used by the current code, which builds every image at run time and attaches it
  to the message. They stay because every edition emailed before 2026-08-28
  hotlinks them from this repository's `main` branch; deleting them would blank the
  images in those archived emails.
