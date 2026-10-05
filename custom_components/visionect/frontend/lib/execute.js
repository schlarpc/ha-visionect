/**
 * Running a confirmed plan, one step at a time, and stopping at the first
 * thing that goes wrong.
 *
 * Stopping is the important half. A plan that keeps going after
 * `wifi_security_set` fails will reach `flash_save` and commit a half-written
 * configuration, which is the state that costs a trip with a cable. So the
 * first failure ends the run, and the result says exactly how far it got --
 * which is also the information needed to finish the job by hand.
 */

import { renderStep } from "./plan.js";
import { SECRET_PSK } from "./plan.js";

/**
 * Execute *plan* against *console_*.
 *
 * @param secrets an object holding any value a step marked `secret` needs,
 *   e.g. `{psk: "..."}`. The only place in the panel that holds the
 *   passphrase. Passed to `console.command` as a mask list as well, so the
 *   device's own echo of it never reaches the transcript or the screen.
 * @param onStep called before each step with `{index, total, step}`, and again
 *   after with `{index, total, step, result}` or `{..., error}`.
 * @returns `{ran, failedAt, error, results}`.
 */
export async function executePlan(console_, plan, { secrets = {}, onStep = null } = {}) {
  const mask = Object.values(secrets).filter((v) => typeof v === "string" && v !== "");
  const results = [];
  const total = plan.steps.length;

  for (let index = 0; index < total; index += 1) {
    const step = plan.steps[index];
    onStep?.({ phase: "start", index, total, step });
    let line;
    try {
      line = renderStep(step, secrets);
    } catch (error) {
      onStep?.({ phase: "error", index, total, step, error });
      return { ran: index, failedAt: step, error, results };
    }
    try {
      const result = await console_.command(line, {
        expectPrompt: step.expectPrompt,
        secrets: mask,
      });
      results.push(result);
      onStep?.({ phase: "done", index, total, step, result });
      if (!result.ok) {
        const error = new Error(
          `${step.display} answered rv: ${result.rv}, so it did not take effect`,
        );
        onStep?.({ phase: "error", index, total, step, error });
        return { ran: index + 1, failedAt: step, error, results };
      }
    } catch (error) {
      onStep?.({ phase: "error", index, total, step, error });
      return { ran: index + 1, failedAt: step, error, results };
    }
  }
  return { ran: total, failedAt: null, error: null, results };
}

/**
 * A human sentence about where a stopped run left the sign.
 *
 * Worth generating rather than leaving to the UI, because what matters is
 * whether `flash_save` ran: before it, a power-cycle undoes everything, and
 * after it the change is permanent.
 */
export function describeOutcome(plan, outcome) {
  if (outcome.error === null) {
    return "Every step ran. The sign's configuration is written to flash.";
  }
  const committed = plan.steps.slice(0, outcome.ran).some((s) => s.name === "flash_save");
  const stopped = `Stopped at step ${outcome.ran} of ${plan.steps.length}, ${outcome.failedAt.display}.`;
  if (committed) {
    return (
      `${stopped} flash_save had already run, so everything before it is permanent. ` +
      "Fix the setting and run the plan again, or finish the remaining commands by hand."
    );
  }
  return (
    `${stopped} flash_save had not run yet, so nothing is committed: power-cycle the ` +
    "sign and it is exactly as it was."
  );
}

export { SECRET_PSK };
