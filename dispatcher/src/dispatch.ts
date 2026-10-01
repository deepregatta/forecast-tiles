/**
 * Starts the forecast-tiles ingest workflows that are due, through GitHub's
 * workflow_dispatch API. The Worker never contacts a provider, latest.json or
 * the runs list: the ingest itself waits for its cycle and skips one that is
 * already published.
 */

import { dueAt, MAX_LATE_MINUTES, slotAt, type Dispatch } from './timetable.js';

export const REPO = 'deepregatta/forecast-tiles';
export const REF = 'main';
const API = `https://api.github.com/repos/${REPO}/actions/workflows`;

export interface Env {
  /** Fine-grained token, `forecast-tiles` only, Actions: read and write. A secret. */
  GITHUB_TOKEN?: string;
  /** Anything but the exact string "false" logs instead of dispatching. */
  DRY_RUN?: string;
}

export interface Deps {
  fetch: (url: string, init?: RequestInit) => Promise<Response>;
  now: () => number;
  log: (line: string) => void;
  error: (line: string) => void;
}

export class DispatchError extends Error {}

const hms = (ms: number) => `${new Date(ms).toISOString().slice(11, 19)}Z`;

/**
 * Handle one cron fire. Throws after logging when any due dispatch failed,
 * or when a fire of one of the timetable's own crons matches no slot.
 *
 * `scheduled=` is the timetable minute. Cloudflare's own `scheduledTime`
 * already carries its start delay (22:25:27 for the 22:25 slot on
 * 2026-09-29), so `fired − scheduled` measures that delay only against the
 * minute.
 */
export async function runScheduled(
  scheduledTime: number,
  env: Env,
  deps: Deps,
  cron = '',
): Promise<void> {
  const fired = deps.now();
  const slot = slotAt(scheduledTime);
  if (slot === null) {
    // An ordinary tick of a `*/5` cron. Every other cron in wrangler.toml
    // fires on a slot, so this one started too late to tell which.
    if (cron.startsWith('*/')) return;
    const message = `MISSED SLOT: cron "${cron}" started at ${hms(scheduledTime)} (fired=${hms(fired)}), ` +
      `more than ${MAX_LATE_MINUTES} min after any timetable slot; nothing dispatched, ` +
      'the fallback crons pick the cycle up';
    deps.error(message);
    throw new DispatchError(message);
  }
  const due = dueAt(scheduledTime);
  const dryRun = env.DRY_RUN !== 'false';
  const failed: string[] = [];
  for (const dispatch of due) {
    const line = `ingest-${dispatch.layer} cycle=${dispatch.cycle} wait=${dispatch.waitMinutes} ` +
      `scheduled=${hms(slot)} fired=${hms(fired)}`;
    if (dryRun) {
      deps.log(`would dispatch ${line}`);
      continue;
    }
    try {
      const run = await dispatchWorkflow(dispatch, env, deps);
      deps.log(`dispatched ${line} run=${run}`);
    } catch (error) {
      deps.error(`FAILED to dispatch ${line}: ${error instanceof Error ? error.message : String(error)}`);
      failed.push(dispatch.layer);
    }
  }
  // A thrown error marks the invocation failed in the Worker's logs too.
  if (failed.length) throw new DispatchError(`dispatch failed for ${failed.join(', ')}`);
}

function headers(token: string): Record<string, string> {
  return {
    Accept: 'application/vnd.github+json',
    Authorization: `Bearer ${token}`,
    'X-GitHub-Api-Version': '2022-11-28',
    // GitHub refuses API requests without one, and Workers send none.
    'User-Agent': 'deepregatta-forecast-tiles-dispatcher',
  };
}

async function detail(res: Response): Promise<string> {
  const text = await res.text().catch(() => '');
  try {
    const message = (JSON.parse(text) as { message?: unknown }).message;
    if (typeof message === 'string') return message;
  } catch {
    // not JSON
  }
  return text.slice(0, 200);
}

function rejectedToken(res: Response, body: string): DispatchError {
  return new DispatchError(
    `GITHUB TOKEN REJECTED: HTTP ${res.status} (${body}). The GITHUB_TOKEN secret has expired, ` +
    `was revoked, or lacks Actions: read and write on ${REPO}. Nothing is being dispatched ` +
    'until it is replaced (npx wrangler secret put GITHUB_TOKEN); the fallback crons keep a slower cadence.',
  );
}

/**
 * POST the dispatch. If GitHub refuses it because the workflow was disabled
 * after 60 days without repository activity, re-enable it and dispatch once
 * more. Returns the run's URL.
 */
export async function dispatchWorkflow(dispatch: Dispatch, env: Env, deps: Deps): Promise<string> {
  const token = env.GITHUB_TOKEN;
  if (!token) throw new DispatchError('the GITHUB_TOKEN secret is not set');
  const workflowUrl = `${API}/${dispatch.workflow}`;
  const post = () => deps.fetch(`${workflowUrl}/dispatches`, {
    method: 'POST',
    headers: { ...headers(token), 'Content-Type': 'application/json' },
    body: JSON.stringify({
      ref: REF,
      inputs: { cycle: dispatch.cycle, wait_minutes: String(dispatch.waitMinutes) },
      // Without it GitHub answers 204 and no run id (the first live
      // dispatch, 2026-10-01 07:20); with it, 200 and the run.
      return_run_details: true,
    }),
  });

  let res = await post();
  if (res.status === 401 || res.status === 403) throw rejectedToken(res, await detail(res));
  if (!res.ok) {
    const refused = `HTTP ${res.status} (${await detail(res)})`;
    const info = await deps.fetch(workflowUrl, { headers: headers(token) });
    if (info.status === 401 || info.status === 403) throw rejectedToken(info, await detail(info));
    const state = info.ok ? ((await info.json()) as { state?: string }).state : `unknown (HTTP ${info.status})`;
    if (state !== 'disabled_inactivity') {
      throw new DispatchError(`GitHub refused the dispatch: ${refused}; workflow state ${state}`);
    }
    const enabled = await deps.fetch(`${workflowUrl}/enable`, { method: 'PUT', headers: headers(token) });
    if (!enabled.ok) {
      throw new DispatchError(`${dispatch.workflow} is disabled_inactivity and enabling it failed: HTTP ${enabled.status} (${await detail(enabled)})`);
    }
    deps.log(`re-enabled ${dispatch.workflow}: GitHub had disabled it after 60 days without repository activity`);
    res = await post();
    if (res.status === 401 || res.status === 403) throw rejectedToken(res, await detail(res));
    if (!res.ok) throw new DispatchError(`GitHub refused the dispatch after re-enabling: HTTP ${res.status} (${await detail(res)})`);
  }

  // 200 carries the run; a 204 with no body is still a dispatch.
  if (res.status === 200) {
    const run = (await res.json().catch(() => ({}))) as { workflow_run_id?: number; html_url?: string };
    if (run.html_url) return run.html_url;
    if (run.workflow_run_id) return `https://github.com/${REPO}/actions/runs/${run.workflow_run_id}`;
  }
  return `https://github.com/${REPO}/actions/workflows/${dispatch.workflow} (no run id in HTTP ${res.status})`;
}
