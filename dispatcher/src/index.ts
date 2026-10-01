/**
 * forecast-tiles dispatcher: a Cron Triggers-only Worker (README.md →
 * Dispatcher). Each fire starts the ingest workflows the timetable has due,
 * for the cycle derived from the scheduled time.
 */

import { runScheduled, type Env } from './dispatch.js';

export default {
  async scheduled(controller, env) {
    await runScheduled(controller.scheduledTime, env, {
      fetch: (url, init) => fetch(url, init),
      now: () => Date.now(),
      log: (line) => console.log(line),
      error: (line) => console.error(line),
    }, controller.cron);
  },
} satisfies ExportedHandler<Env>;
