import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { cycleId, dueAt, slotAt, TIMETABLE } from '../src/timetable.js';

const at = (iso: string) => Date.parse(iso);

describe('dueAt', () => {
  it.each([
    ['2026-09-30T04:25:00Z', 'weather', '20260930T00', 90],
    ['2026-09-30T10:25:00Z', 'weather', '20260930T06', 90],
    ['2026-09-30T16:25:00Z', 'weather', '20260930T12', 90],
    ['2026-09-30T22:25:00Z', 'weather', '20260930T18', 90],
    ['2026-09-30T05:00:00Z', 'waves', '20260930T00', 90],
    ['2026-09-30T11:00:00Z', 'waves', '20260930T06', 90],
    ['2026-09-30T17:00:00Z', 'waves', '20260930T12', 90],
    ['2026-09-30T23:00:00Z', 'waves', '20260930T18', 90],
    // the day rollover: 00:15 asks for the previous day's 18Z
    ['2026-09-30T00:15:00Z', 'ensemble', '20260929T18', 90],
    ['2026-09-30T06:15:00Z', 'ensemble', '20260930T00', 90],
    ['2026-09-30T12:15:00Z', 'ensemble', '20260930T06', 90],
    ['2026-09-30T18:15:00Z', 'ensemble', '20260930T12', 90],
    ['2026-09-30T07:20:00Z', 'weather-ecmwf', '20260930T00', 120],
    ['2026-09-30T19:20:00Z', 'weather-ecmwf', '20260930T12', 120],
    // one expression, two layers: each at its own hour, both for that day's 00Z
    ['2026-09-30T05:45:00Z', 'currents', '20260930T00', 180],
    ['2026-09-30T09:45:00Z', 'currents-ibi', '20260930T00', 180],
  ] as const)('%s dispatches %s for %s, waiting %i min', (scheduled, layer, cycle, wait) => {
    expect(dueAt(at(scheduled))).toEqual([
      { layer, workflow: `ingest-${layer}.yml`, cycle, waitMinutes: wait },
    ]);
  });

  it('rolls over months and years', () => {
    expect(dueAt(at('2026-10-01T00:15:00Z'))[0]?.cycle).toBe('20260930T18');
    expect(dueAt(at('2027-01-01T00:15:00Z'))[0]?.cycle).toBe('20261231T18');
  });

  it('takes the cycle from the scheduled minute, whatever the seconds', () => {
    expect(dueAt(at('2026-09-30T10:25:59.999Z'))[0]?.cycle).toBe('20260930T06');
  });

  it('counts a fire up to 4 min 59 s late for its slot, since scheduledTime carries the delay', () => {
    expect(slotAt(at('2026-10-01T05:00:48Z'))).toBe(at('2026-10-01T05:00:00Z'));
    expect(dueAt(at('2026-10-01T05:00:48Z'))[0]).toMatchObject({ layer: 'waves', cycle: '20261001T00' });
    expect(dueAt(at('2026-09-30T10:29:59.999Z'))[0]).toMatchObject({ layer: 'weather', cycle: '20260930T06' });
    expect(slotAt(at('2026-09-30T10:30:00Z'))).toBeNull();
    expect(dueAt(at('2026-09-30T10:30:00Z'))).toEqual([]);
    // across midnight: the 00:15 slot started late still asks for the previous day's 18Z
    expect(dueAt(at('2026-10-01T00:18:30Z'))[0]?.cycle).toBe('20260930T18');
  });

  it('never matches the previous slot from a */5 tick', () => {
    const slots = new Set<number>();
    for (let tick = 0; tick < 24 * 12; tick++) {
      const t = at('2026-09-30T00:00:00Z') + tick * 5 * 60_000;
      const slot = slotAt(t);
      if (slot === null) continue;
      expect(slot).toBe(t);
      slots.add(slot);
    }
    expect(slots.size).toBe(16);
  });

  it('has nothing due between slots, as most */5 ticks', () => {
    for (const iso of ['2026-09-30T10:20:00Z', '2026-09-30T10:30:00Z', '2026-09-30T05:40:00Z', '2026-09-30T13:45:00Z']) {
      expect(dueAt(at(iso))).toEqual([]);
    }
  });

  it('fires 16 times a day, one layer each time, always on a cycle the layer has', () => {
    const cycleHours: Record<string, number[]> = {
      weather: [0, 6, 12, 18], waves: [0, 6, 12, 18], ensemble: [0, 6, 12, 18],
      'weather-ecmwf': [0, 12], currents: [0], 'currents-ibi': [0],
    };
    let fires = 0;
    for (let minute = 0; minute < 24 * 60; minute++) {
      const t = at('2026-09-30T00:00:00Z') + minute * 60_000;
      if (slotAt(t) !== t) continue; // a late start of an earlier slot
      const due = dueAt(t);
      expect(due.length).toBe(1);
      for (const dispatch of due) {
        fires += 1;
        expect(cycleHours[dispatch.layer]).toContain(Number(dispatch.cycle.slice(9)));
      }
    }
    expect(fires).toBe(16);
  });

  it('formats cycles as the ingest --cycle argument', () => {
    expect(cycleId(at('2026-09-05T06:00:00Z'))).toBe('20260905T06');
  });
});

describe('wrangler.toml', () => {
  const toml = readFileSync(new URL('../wrangler.toml', import.meta.url), 'utf8');
  const crons = [...toml.matchAll(/^\s*"([^"]+)",/gm)].map((m) => m[1]!);

  it('fires exactly at the timetable slots, with at most the free plan\'s 5 triggers', () => {
    expect(crons.length).toBeLessThanOrEqual(5);
    const fireTimes = crons.flatMap((cron) => {
      const [minute, hours, ...rest] = cron.split(' ');
      expect(rest).toEqual(['*', '*', '*']);
      return hours!.split(',').map((hour) => `${Number(hour)}:${Number(minute)}`);
    });
    const slots = TIMETABLE.flatMap((entry) => entry.hours.map((hour) => `${hour}:${entry.minute}`));
    expect(new Set(fireTimes)).toEqual(new Set(slots));
    expect(fireTimes).toHaveLength(slots.length);
  });

  it('dispatches (switched on in Phase 5B) and exposes no URL', () => {
    expect(toml).toMatch(/^DRY_RUN = "false"$/m);
    expect(toml).toMatch(/^workers_dev = false$/m);
    expect(toml).not.toMatch(/^routes?\s*=/m);
    expect(toml).toMatch(/\[observability\]\nenabled = true/);
    expect(toml).not.toMatch(/GITHUB_TOKEN\s*=/);
  });
});
