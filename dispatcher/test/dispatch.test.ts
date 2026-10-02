import { describe, expect, it } from 'vitest';
import { runScheduled, type Deps, type Env } from '../src/dispatch.js';

const API = 'https://api.github.com/repos/deepregatta/forecast-tiles/actions/workflows';
const WEATHER_10_25 = Date.parse('2026-09-30T10:25:00Z');

interface Call {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: unknown;
}

/** A fetch answering from a queue of responses, recording every request. */
function github(...responses: Response[]) {
  const calls: Call[] = [];
  const lines: string[] = [];
  const errors: string[] = [];
  const deps: Deps = {
    fetch: async (url, init) => {
      calls.push({
        url,
        method: init?.method ?? 'GET',
        headers: (init?.headers ?? {}) as Record<string, string>,
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
      });
      const next = responses.shift();
      if (!next) throw new Error(`unexpected request ${url}`);
      return next;
    },
    now: () => WEATHER_10_25 + 2_000,
    log: (line) => lines.push(line),
    error: (line) => errors.push(line),
  };
  return { deps, calls, lines, errors };
}

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const dispatched = (id = 18_000_000_001) =>
  json(200, {
    workflow_run_id: id,
    run_url: `https://api.github.com/repos/deepregatta/forecast-tiles/actions/runs/${id}`,
    html_url: `https://github.com/deepregatta/forecast-tiles/actions/runs/${id}`,
  });
const live: Env = { DRY_RUN: 'false', GITHUB_TOKEN: 'test-token' };

describe('regional activation', () => {
  it('restricts enabled models to canary cycles until full cadence is explicit', async () => {
    const { deps, calls } = github();
    await runScheduled(Date.parse('2026-10-02T13:15:00Z'), { ...live, REGIONAL_MODELS: 'weather-arome' }, deps);
    expect(calls).toHaveLength(0);
    const full = github(dispatched());
    await runScheduled(Date.parse('2026-10-02T13:15:00Z'), {
      ...live, REGIONAL_MODELS: 'weather-arome', REGIONAL_FULL_CADENCE: 'true',
    }, full.deps);
    expect(full.calls).toHaveLength(1);
    expect(full.calls[0]!.body).toMatchObject({inputs: {cycle: '20261002T09', canary: 'false'}});
  });

  it('defaults to no regional dispatch even at a shared slot', async () => {
    const { deps, calls } = github(dispatched());
    await runScheduled(Date.parse('2026-10-02T05:45:00Z'), live, deps);
    expect(calls).toHaveLength(1);
    expect(calls[0]!.url).toContain('ingest-currents.yml/dispatches');
  });

  it('dispatches every enabled entry at a shared slot and includes the model input', async () => {
    const { deps, calls } = github(dispatched(), dispatched());
    await runScheduled(Date.parse('2026-10-02T05:45:00Z'), { ...live, REGIONAL_MODELS: 'weather-arome' }, deps);
    expect(calls).toHaveLength(2);
    expect(calls[1]!.url).toContain('ingest-openmeteo.yml/dispatches');
    expect(calls[1]!.body).toEqual({ref: 'main', inputs: {layer: 'weather-arome', cycle: '20261002T03',
      wait_minutes: '90', dry_run: 'false', canary: 'true'}, return_run_details: true});
  });
});

describe('dry run', () => {
  it('logs the dispatch it would make and contacts nothing', async () => {
    const { deps, calls, lines } = github();
    await runScheduled(WEATHER_10_25, { DRY_RUN: 'true' }, deps);
    expect(lines).toEqual([
      'would dispatch ingest-weather cycle=20260930T06 wait=90 scheduled=10:25:00Z fired=10:25:02Z',
    ]);
    expect(calls).toEqual([]);
  });

  it('logs the timetable minute as scheduled, so fired − scheduled is the start delay', async () => {
    // 2026-09-29: Cloudflare's scheduledTime for the 22:25 slot was 22:25:27
    const { deps, lines } = github();
    const fired = Date.parse('2026-09-29T22:25:27.400Z');
    await runScheduled(fired, { DRY_RUN: 'true' }, { ...deps, now: () => fired });
    expect(lines).toEqual([
      'would dispatch ingest-weather cycle=20260929T18 wait=90 scheduled=22:25:00Z fired=22:25:27Z',
    ]);
  });

  it('is the default: only the exact string "false" dispatches', async () => {
    for (const env of [{}, { DRY_RUN: 'FALSE' }, { DRY_RUN: '0' }, { GITHUB_TOKEN: 't' }]) {
      const { deps, calls, lines } = github();
      await runScheduled(WEATHER_10_25, env, deps);
      expect(lines[0]).toMatch(/^would dispatch /);
      expect(calls).toEqual([]);
    }
  });

  it('logs nothing on a */5 tick with no layer due', async () => {
    const { deps, calls, lines, errors } = github();
    await runScheduled(Date.parse('2026-09-30T10:30:00Z'), live, deps, '*/5 * * * *');
    expect([...calls, ...lines, ...errors]).toEqual([]);
  });
});

describe('a late fire', () => {
  it('still dispatches its slot, logged against the timetable minute', async () => {
    // 2026-10-01: the 05:00 slot started 48 s late
    const fired = Date.parse('2026-10-01T05:00:48.300Z');
    const { deps, calls, lines } = github(dispatched());
    await runScheduled(fired, live, { ...deps, now: () => fired }, '0 5,11,17,23 * * *');
    expect(calls[0]!.body).toEqual({ ref: 'main', inputs: { cycle: '20261001T00', wait_minutes: '90' }, return_run_details: true });
    expect(lines[0]).toMatch(/^dispatched ingest-waves cycle=20261001T00 wait=90 scheduled=05:00:00Z fired=05:00:48Z /);
  });

  it('fails loudly when it matches no slot, dispatching nothing', async () => {
    const fired = Date.parse('2026-09-30T10:30:05Z');
    const { deps, calls, lines, errors } = github();
    await expect(
      runScheduled(fired, live, { ...deps, now: () => fired }, '25 4,10,16,22 * * *'),
    ).rejects.toThrow(/MISSED SLOT/);
    expect([...calls, ...lines]).toEqual([]);
    expect(errors).toEqual([
      'MISSED SLOT: cron "25 4,10,16,22 * * *" started at 10:30:05Z (fired=10:30:05Z), more than 4 min ' +
        'after any timetable slot; nothing dispatched, the fallback crons pick the cycle up',
    ]);
  });
});

describe('dispatch', () => {
  it('posts the cycle and wait to the layer workflow on main and logs the run', async () => {
    const { deps, calls, lines, errors } = github(dispatched());
    await runScheduled(WEATHER_10_25, live, deps);
    expect(calls).toHaveLength(1);
    const [call] = calls;
    expect(call!.url).toBe(`${API}/ingest-weather.yml/dispatches`);
    expect(call!.method).toBe('POST');
    expect(call!.body).toEqual({ ref: 'main', inputs: { cycle: '20260930T06', wait_minutes: '90' }, return_run_details: true });
    expect(call!.headers).toMatchObject({
      Authorization: 'Bearer test-token',
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
      'Content-Type': 'application/json',
    });
    expect(call!.headers['User-Agent']).toBeTruthy();
    expect(lines).toEqual([
      'dispatched ingest-weather cycle=20260930T06 wait=90 scheduled=10:25:00Z fired=10:25:02Z ' +
        'run=https://github.com/deepregatta/forecast-tiles/actions/runs/18000000001',
    ]);
    expect(errors).toEqual([]);
  });

  it('dispatches the ensemble and ECMWF 18Z for the previous day at 00:15', async () => {
    const { deps, calls, lines } = github(dispatched(1), dispatched(2));
    await runScheduled(Date.parse('2026-10-01T00:15:00Z'), live, deps);
    expect(calls.map((c) => [c.url, c.body])).toEqual([
      [`${API}/ingest-ensemble.yml/dispatches`, { ref: 'main', inputs: { cycle: '20260930T18', wait_minutes: '90' }, return_run_details: true }],
      [`${API}/ingest-weather-ecmwf-short.yml/dispatches`, { ref: 'main', inputs: { cycle: '20260930T18', wait_minutes: '120' }, return_run_details: true }],
    ]);
    expect(lines).toHaveLength(2);
    expect(lines[1]).toMatch(/^dispatched ingest-weather-ecmwf-short cycle=20260930T18 wait=120 .* run=.*\/runs\/2$/);
  });

  it('still dispatches the second layer of a shared slot when the first fails, then fails the fire', async () => {
    const { deps, calls, lines, errors } = github(json(500, { message: 'Server Error' }), json(200, { state: 'active' }), dispatched(7));
    await expect(runScheduled(Date.parse('2026-09-30T12:15:00Z'), live, deps)).rejects.toThrow('dispatch failed for ensemble');
    expect(calls.at(-1)!.url).toBe(`${API}/ingest-weather-ecmwf-short.yml/dispatches`);
    expect(lines).toEqual([expect.stringMatching(/^dispatched ingest-weather-ecmwf-short cycle=20260930T06 wait=120 /)]);
    expect(errors).toEqual([expect.stringMatching(/^FAILED to dispatch ingest-ensemble cycle=20260930T06/)]);
  });

  it('accepts a 204 without run details', async () => {
    const { deps, lines } = github(new Response(null, { status: 204 }));
    await runScheduled(WEATHER_10_25, live, deps);
    expect(lines[0]).toMatch(/^dispatched ingest-weather .* run=.*ingest-weather\.yml \(no run id in HTTP 204\)$/);
  });
});

describe('a workflow disabled for inactivity', () => {
  it('is re-enabled and dispatched once more', async () => {
    const { deps, calls, lines } = github(
      json(422, { message: "Cannot trigger a 'workflow_dispatch' on a disabled workflow" }),
      json(200, { id: 1, name: 'ingest-weather', state: 'disabled_inactivity' }),
      new Response(null, { status: 204 }),
      dispatched(42),
    );
    await runScheduled(WEATHER_10_25, live, deps);
    expect(calls.map((c) => `${c.method} ${c.url}`)).toEqual([
      `POST ${API}/ingest-weather.yml/dispatches`,
      `GET ${API}/ingest-weather.yml`,
      `PUT ${API}/ingest-weather.yml/enable`,
      `POST ${API}/ingest-weather.yml/dispatches`,
    ]);
    expect(calls[3]!.body).toEqual(calls[0]!.body);
    expect(lines[0]).toMatch(/^re-enabled ingest-weather\.yml/);
    expect(lines[1]).toMatch(/^dispatched ingest-weather .* run=.*\/runs\/42$/);
  });

  it('is left alone when a person disabled it', async () => {
    const { deps, calls, errors } = github(
      json(422, { message: "Cannot trigger a 'workflow_dispatch' on a disabled workflow" }),
      json(200, { id: 1, name: 'ingest-weather', state: 'disabled_manually' }),
    );
    await expect(runScheduled(WEATHER_10_25, live, deps)).rejects.toThrow('dispatch failed for weather');
    expect(calls).toHaveLength(2);
    expect(errors[0]).toMatch(/^FAILED to dispatch ingest-weather cycle=20260930T06 .*HTTP 422.*workflow state disabled_manually/);
  });
});

describe('a rejected token', () => {
  it.each([401, 403])('is logged loudly on HTTP %i, with no retry', async (status) => {
    const { deps, calls, errors } = github(json(status, { message: 'Bad credentials' }));
    await expect(runScheduled(WEATHER_10_25, live, deps)).rejects.toThrow('dispatch failed for weather');
    expect(calls).toHaveLength(1);
    expect(errors).toHaveLength(1);
    expect(errors[0]).toContain(`GITHUB TOKEN REJECTED: HTTP ${status} (Bad credentials)`);
    expect(errors[0]).toContain('npx wrangler secret put GITHUB_TOKEN');
  });

  it('is reported when the secret is missing', async () => {
    const { deps, calls, errors } = github();
    await expect(runScheduled(WEATHER_10_25, { DRY_RUN: 'false' }, deps)).rejects.toThrow();
    expect(calls).toEqual([]);
    expect(errors[0]).toContain('GITHUB_TOKEN secret is not set');
  });
});
