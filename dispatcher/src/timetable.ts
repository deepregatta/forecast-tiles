/**
 * When each forecast-tiles layer is dispatched, and which cycle it asks for.
 *
 * Providers publish at known times, so the dispatcher only keeps the clock:
 * it starts each ingest workflow a little before the provider usually
 * finishes a cycle, and the ingest waits (`wait_minutes`) until the cycle is
 * out. Provider times were measured on 2026-09-23 (NOAA, ECMWF) and
 * 2026-09-24..29 (Copernicus); see Passage docs/grib-export-plan.md, Phase 5.
 */

export type Layer = 'weather' | 'waves' | 'ensemble' | 'weather-ecmwf' | 'currents' | 'currents-ibi';

export interface TimetableEntry {
  layer: Layer;
  /** UTC hours at which the entry fires, each at `minute` */
  hours: number[];
  minute: number;
  /** The cycle dispatched is the fire time minus this. */
  lagMinutes: number;
  /** How long the ingest may wait for the provider, as the workflow's `wait_minutes`. */
  waitMinutes: number;
}

const h = (hours: number, minutes = 0) => hours * 60 + minutes;

export const TIMETABLE: readonly TimetableEntry[] = [
  // GFS f240 .idx at cycle + 4 h 37 to 4 h 41
  { layer: 'weather', hours: [4, 10, 16, 22], minute: 25, lagMinutes: h(4, 25), waitMinutes: 90 },
  // GFS-Wave f384 .idx at + 5 h 10 to 5 h 25
  { layer: 'waves', hours: [5, 11, 17, 23], minute: 0, lagMinutes: h(5), waitMinutes: 90 },
  // GEFS gep30 f384 .idx at + 6 h 29 to 6 h 31; 00:15 asks for the previous day's 18Z
  { layer: 'ensemble', hours: [0, 6, 12, 18], minute: 15, lagMinutes: h(6, 15), waitMinutes: 90 },
  // ECMWF's 240 h index at + 7 h 34; only 00Z and 12Z reach 240 h
  { layer: 'weather-ecmwf', hours: [7, 19], minute: 20, lagMinutes: h(7, 20), waitMinutes: 120 },
  // Copernicus GLO12 finished at 06:26, 06:10 and 09:05 (29 Sep-1 Oct; on
  // 1 Oct a second update ran from 07:01): that day's 00Z, waiting to 09:45
  { layer: 'currents', hours: [5], minute: 45, lagMinutes: h(5, 45), waitMinutes: 240 },
  // Copernicus IBI finished at 09:54 to 11:36 (24-29 Sep): that day's 00Z
  { layer: 'currents-ibi', hours: [9], minute: 45, lagMinutes: h(9, 45), waitMinutes: 180 },
];

export interface Dispatch {
  layer: Layer;
  /** forecast-tiles workflow file */
  workflow: string;
  /** `YYYYMMDDTHH`, the ingest's --cycle */
  cycle: string;
  waitMinutes: number;
}

const MINUTE_MS = 60_000;
const pad = (n: number) => String(n).padStart(2, '0');

/** `YYYYMMDDTHH` of a UTC instant. */
export function cycleId(ms: number): string {
  const t = new Date(ms);
  return `${t.getUTCFullYear()}${pad(t.getUTCMonth() + 1)}${pad(t.getUTCDate())}T${pad(t.getUTCHours())}`;
}

/**
 * How late a fire may start and still count for its slot. Cloudflare's
 * `scheduledTime` already carries its start delay (27-48 s over the
 * 2026-09-29..10-01 dry run), so flooring it to the minute alone would drop
 * a slot fired a minute late. Under 5 min, so a `*\/5` tick never
 * dispatches the slot before it again.
 */
export const MAX_LATE_MINUTES = 4;

const entriesAt = (minuteMs: number) => {
  const t = new Date(minuteMs);
  return TIMETABLE.filter(
    (entry) => entry.minute === t.getUTCMinutes() && entry.hours.includes(t.getUTCHours()),
  );
};

/**
 * The timetable minute a fire belongs to: the latest slot at most
 * MAX_LATE_MINUTES before `scheduledTime`, or null when there is none.
 */
export function slotAt(scheduledTime: number): number | null {
  const minuteMs = Math.floor(scheduledTime / MINUTE_MS) * MINUTE_MS;
  for (let late = 0; late <= MAX_LATE_MINUTES; late++) {
    const slot = minuteMs - late * MINUTE_MS;
    if (entriesAt(slot).length) return slot;
  }
  return null;
}

/**
 * The dispatches due at a scheduled fire time (`controller.scheduledTime`,
 * never the clock at run time, so a late start still asks for the right
 * cycle). Empty when no layer is due, as for most ticks of a `*\/5` cron.
 */
export function dueAt(scheduledTime: number): Dispatch[] {
  const slot = slotAt(scheduledTime);
  if (slot === null) return [];
  return entriesAt(slot).map((entry) => ({
    layer: entry.layer,
    workflow: `ingest-${entry.layer}.yml`,
    cycle: cycleId(slot - entry.lagMinutes * MINUTE_MS),
    waitMinutes: entry.waitMinutes,
  }));
}
