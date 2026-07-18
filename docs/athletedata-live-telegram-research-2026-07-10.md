# AthleteData Live Telegram Research - 2026-07-10

> Historical product-discovery artifact. AthleteData behavior and candidate
> lessons recorded here are research input, not implemented GarminCoach
> capability or authority. Use [`coach_product_architecture.md`](coach_product_architecture.md)
> for the current specification.

## Summary

This document captures two live Telegram research passes against `@athletedata_bot`.

- Pass 1 explored how the bot handled the user's real setup pivot: Tel Aviv, 64 kg, 1.75 m, muscle gain, visible abs over time, no running/cycling plan, no food advice, 3 gym sessions/week, weekend soccer, and 30 kg total baseline on bench/squat/deadlift/OHP.
- Pass 2 asked questions that GarminCoach itself aims to answer: morning briefing, evening push/no-push judgement, recovery pushback, missing-data handling, approval-ready scheduling, weekly summary, and calendar-aware rescheduling.

No GarminCoach code was changed during the Telegram research. The only repo change made for this phase is this documentation file.

Important caveat: in Pass 1, several probes were not marked as hypothetical, and AthleteData treated them as real updates. It said it updated working weights, moved a Legs session onto the current day, and flagged a knee issue. In Pass 2, a guardrail was sent first:

> Research mode for the next few messages: please answer normally, but do not update my plan, workout logs, working weights, reminders, or preferences from these questions unless I explicitly say "apply this to my plan."

AthleteData accepted the guardrail and replied that it would not touch plan/logs/weights/memory unless explicitly told to apply changes.

## Pass 1: Strength Plan, Progression, Boundaries, and Watch UX

### 1. Plan State Audit

Prompt:

> Before I start the next workout, summarize exactly what plan you currently have for me: goal, weekly structure, next 7 days, current working weights, progression rule, and anything you still need from me.

Key response:

- Goal: build muscle mass, let ab visibility follow slowly at maintenance, no running/cycling goals.
- Weekly structure: Push/Legs/Pull, each with core, plus Saturday soccer.
- Working weights: bench/squat/deadlift/OHP started at 30 kg total.
- It remembered prior Push data and unresolved Garmin data: a 65-minute indoor cardio session with avg HR 137 and max HR 188.

What we learned:

- AthleteData can produce a very useful "current plan state" summary.
- It remembers constraints, schedule, working weights, and unresolved open loops.
- It uses unresolved real data as a blocker/open question instead of ignoring it.

GarminCoach implication:

- Add or expose a persistent "plan state" object that can be summarized on demand: goal, weekly structure, next sessions, current lift baselines, progression rules, unresolved user questions, and blockers.

### 2. Workout Execution Logging

Prompt:

> I finished the Push session. Bench: 30kg total, 4 sets of 8, 8, 7, 6. OHP: 30kg total, 4 sets of 10, 10, 9, 8. Incline dumbbell press: 10kg each hand, 3x10. Lateral raises: 6kg each hand, 3x12. Triceps: easy. What changes for next Push day?

Key response:

- Bench: hold at 30 kg until all 4 sets reach at least 8 clean reps; then add 2.5 kg.
- OHP: revised previous 35 kg jump down to 32.5 kg because the full set pattern faded by set 3.
- Incline DB press: try 12.5 kg/hand if reps hold at 10+.
- Lateral raises: hold or nudge to 7-8 kg depending on final-set feel.
- Triceps: cannot track without weight/reps.

What we learned:

- It treats set-by-set data as more authoritative than earlier qualitative statements.
- It is willing to revise its own prior recommendation.
- It distinguishes each lift rather than applying one blanket progression rule.

GarminCoach implication:

- Store manual set outcomes when Garmin strength detail is sparse.
- Implement per-exercise progression state, not session-level "normal/hard/light" only.
- Use rep fade across sets as a hold signal.

### 3. Bad Session / Autoregulation

Prompt:

> Legs day went badly. Squat at 30kg felt unstable, RDL was fine, and split squats made my knees feel weird. I stopped early. What should change next time?

Key response:

- It separated squat instability from knee discomfort.
- Squat: hold 30 kg, focus on form/stability, slow descent, vertical bar path, no load increase.
- Knee: asked for sharp/pinching vs tracking/instability, which knee, front/back leg, and whether it persisted after stopping.
- Until clarified: bodyweight split squats only, or substitute goblet squat/leg press.
- RDL remained confirmed clean at 30 kg.

What we learned:

- It does not treat all bad-session signals the same.
- It asks for the minimum missing clinical/safety detail before deciding.
- It provides a conservative default while waiting for clarification.

GarminCoach implication:

- Add a pain/form triage path for strength sessions.
- Treat "unstable form" differently from "pain/weird knee."
- Block load increases when form is unstable even if the load is objectively light.

### 4. Soccer Interference

Prompt:

> I played soccer yesterday and it was much harder than usual. My legs are sore today, but Pull is planned. Should Pull change, and should Legs later this week move?

Key response:

- It rejected the premise because it contradicted the actual schedule.
- It said soccer was planned for Sat Jul 11, not yesterday, and Pull was not planned today.
- It asked whether this was an unplanned pickup game or soreness from the earlier Legs session.
- It refused to shuffle sessions until the real cause was clarified.

What we learned:

- The bot has strong schedule grounding.
- It will push back against user-provided timelines when they conflict with its plan state.
- This is good for preventing accidental plan churn, but synthetic probes can mutate its state if not guarded.

GarminCoach implication:

- Before rescheduling, compare user statements against calendar, planned sessions, and logged activities.
- Ask for clarification when user-reported events conflict with known state.
- Model soccer/team sport as a load source, but avoid inventing it when not logged or scheduled.

### 5. Boundary Respect

Prompt:

> Reminder: I still do not want food advice. I only want training guidance. Given that, what should I focus on for the next two weeks?

Key response:

- Stayed entirely training-focused.
- Focus areas:
  - Squat stability, not squat load.
  - Knee monitoring on split squats.
  - Upper body progression based on clean data.
- No food or nutrition advice.

What we learned:

- It can keep a durable user preference boundary.
- It still remains useful by translating the body-composition goal into training-only priorities.

GarminCoach implication:

- Add durable "scope preferences" such as "no food advice" and enforce them across proactive and reactive coaching.

### 6. Progression Policy Extraction

Prompt:

> Explain the progression rules you are using for me. When do you add 2.5kg, when do you add 5kg, when do you hold, and when do you deload?

Key response:

- Hold: if the user does not hit the target rep range across all sets, reps fade between sets, or form is unstable.
- Add 2.5 kg: default jump once the lift clears hold criteria.
- Add 5 kg: only when the full set-by-set data shows clear headroom, not just one easy first set.
- Deload: only for repeated regression without external cause, or technical breakdown that load is actively worsening.
- For squat at 30 kg, it chose "hold and fix pattern" instead of deload because the load is already light.

What we learned:

- It has a compact, explainable progression policy.
- The policy is lift-specific and reason-specific.
- It distinguishes "hold because near ceiling" from "hold because form is unstable."

GarminCoach implication:

- Implement progression reasons, not just progression actions:
  - hold_rep_fade
  - hold_form_instability
  - increase_default
  - increase_clear_headroom
  - deload_repeated_regression
  - deload_load_driven_breakdown

### 7. Garmin Watch UX

Prompt:

> If this plan is pushed to Garmin, what exactly will I see on the watch during a strength session, and what do I need to press between sets or exercises?

Key response:

- Garmin shows structured exercise category, target sets, target reps, and weight.
- Rest is timed and auto-advances.
- Lap can skip rest early.
- Between exercises also auto-advances.
- Specific names may collapse into Garmin categories:
  - Bulgarian Split Squat may show as Lunge.
  - Romanian Deadlift and Deadlift may both show as Deadlift.
- RPE targets do not transmit.
- Out-of-order exercise navigation is possible on the watch.

What we learned:

- AthleteData understands and explains practical watch behavior.
- It admits Garmin label limitations instead of overselling the UX.

GarminCoach implication:

- If GarminCoach pushes structured strength workouts, user-facing copy must explain watch labels and limitations.
- Keep full exercise names in app/Telegram/dashboard even if Garmin category names are lossy.

### 8. Proactive Coaching Expectation

Prompt:

> What would you proactively message me about over the next two weeks, and what would you stay silent about unless I ask?

Key response:

- It would proactively message about:
  - Squat/knee pattern becoming repeated.
  - Lift progression change: jump, hold, deload.
  - Anything conflicting with the plan: extra session, missed session, soreness threatening next lift.
- It would stay silent about:
  - Day-to-day weight fluctuation.
  - Nutrition/calories/food.
  - Normal HRV/sleep/recovery scores.
  - Ab visibility progress.

What we learned:

- The bot has a high bar for proactive messages.
- It frames notifications around whether silence would cost the user something actionable.

GarminCoach implication:

- Proactive notifications should be limited to plan-changing issues, risk flags, unresolved decisions, and user-approved check-ins.
- Do not narrate metrics just because they exist.

## Pass 2: GarminCoach-Aligned Questions

### Guardrail

Prompt:

> Research mode for the next few messages: please answer normally, but do not update my plan, workout logs, working weights, reminders, or preferences from these questions unless I explicitly say "apply this to my plan."

Response:

> Got it - research mode. I'll answer questions straight without touching the plan, logs, weights, or memory unless you say "apply this to my plan." Ask away.

What we learned:

- AthleteData accepts a research-only boundary.
- Future probes should always start with this to avoid contaminating the user's plan.

### A1. Morning Briefing

Prompt:

> Suppose it is morning and I ask "what should I do today?" The available facts are: short sleep around 6h20m but decent sleep score, HRV stable, training load low, I have a strength session planned, and my calendar is blocked until 17:30 with no workouts after 20:00. How would you answer me?

Key response:

- It would recommend doing the strength session as planned.
- Short sleep alone is not enough to downgrade when sleep score is decent, HRV stable, and load is low.
- It would only pull back if HRV dropped or this was the second/third short-sleep night in a row.
- Timing: 17:30-20:00 is workable but tight; keep warmup efficient.

What we learned:

- It makes a clear go/no-go decision.
- It explicitly names the threshold that would change the decision.
- It uses schedule constraints to decide timing, not whether training is biologically appropriate.

GarminCoach implication:

- Morning briefing should avoid overreacting to a single short night when HRV and sleep quality are stable.
- The recommendation should include "what would change this call" only when it is concise and useful.

### A2. Evening No-Push Decision

Prompt:

> How would you decide tonight whether to proactively message me about tomorrow or stay silent? Give examples of when you would send a message and when you would send nothing.

Key response:

- It treats soccer as a standing habit it does not own, so the interruption bar is high.
- It would send if:
  - There is an unresolved relevant thread, like the 65-minute indoor cardio session before soccer.
  - Overnight data shows a meaningful HRV/sleep hit.
  - Sleep debt spikes instead of shrinking.
- It would stay silent if:
  - HRV is normal.
  - Sleep is average/decent.
  - No new signal.
  - Soccer is simply happening as usual.
  - The issue is diet-adjacent or one mediocre reading.
- Its filter: would silence cost the user something real tomorrow, or would the message just narrate known data?

What we learned:

- This is very close to the product judgment GarminCoach needs.
- It distinguishes owned sessions from external habits.
- It treats unresolved user input as a valid push reason.

GarminCoach implication:

- Evening check-in should be silent by default.
- Send only when there is a concrete decision, schedule change, risk flag, or unresolved blocker.

### A3. Recovery Pushback

Prompt:

> If I say I feel fine and want to train hard, but readiness is low, sleep debt is high, and HRV has been trending down, how would you answer?

Key response:

- It would not defer to "I feel fine."
- A multi-day HRV downtrend plus sleep debt means autonomic strain even if the user does not feel it.
- It would not necessarily prescribe full rest; it would scale the session:
  - Drop intensity 15-20%.
  - Swap the heaviest compound work for moderate work.
  - Use volume/load as the lever.
- It would allow pushing on a single bad reading, but not a multi-day trend.

What we learned:

- It pushes back decisively but not simplistically.
- It chooses the middle path between "full send" and "full rest."
- It distinguishes one-off noise from trend.

GarminCoach implication:

- GarminCoach should push back on multi-day recovery trends, but can offer scaled training rather than binary rest.

### A4. Missing Data / No Hallucination

Prompt:

> If I ask "am I progressing well over the last month?" but you only have a few days of synced data and incomplete strength history, how would you answer without hallucinating?

Key response:

- It would plainly say it cannot answer yet.
- It would explain that CTL/ATL are still seeding and strength history has only one or two real data points on some lifts.
- It would identify what it can say:
  - Bench/OHP have real numbers.
  - Squat has a form issue.
  - Deadlift remains untested.
- It would ask for 3-4 more weeks of consistent logging.

What we learned:

- It refuses to manufacture a progress narrative.
- It still extracts concrete local truth from sparse data.

GarminCoach implication:

- Missing-data answers should include:
  - what cannot be concluded
  - why not
  - what can be concluded
  - what data will unlock the answer

### A5. Approval-Ready Scheduling

Prompt:

> If I ask you to schedule my next strength session, what exact proposal would you send before pushing anything to Garmin? Include timing, intensity, and what you would require from me before actually scheduling.

Key response:

- It would propose the next Legs session with exercise list.
- It would include changes from prior data:
  - Squat stays at 30 kg for stability/form.
  - Split squats drop to bodyweight until knee details are clarified.
  - RDL holds at 30 kg.
- It would not push to Garmin until the knee question is answered.
- It would block scheduling if a medical-adjacent flag remains unresolved.

What we learned:

- It treats approval-ready scheduling as a product of current plan state plus unresolved blockers.
- It does not schedule through ambiguous pain.

GarminCoach implication:

- The schedule JSON/action flow should be blocked by unresolved pain or injury flags.
- The proposal should include exact modifications and why they exist.

### A6. Weekly Summary

Prompt:

> Write the kind of weekly summary you would send me after a week with 3 strength sessions, one soccer match, one missed lift, stable HRV, and moderate sleep debt. No nutrition advice.

Key response:

- It would summarize:
  - 3 strength sessions, 1 soccer match, 1 missed lift.
  - Lift-by-lift changes: what held, jumped, or got flagged.
  - The missed lift without judgment, asking whether it was soccer soreness, schedule conflict, or simply skipped.
  - Stable HRV as a sign load is being absorbed.
  - Moderate sleep debt as a non-alarming risk if it keeps climbing.
  - Specific carryovers into next week.
- It would avoid nutrition.

What we learned:

- The useful weekly summary is not just "workouts completed."
- It should name carryover decisions for next week.
- It should treat missed sessions as data, not moral failure.

GarminCoach implication:

- Weekly summaries should include:
  - adherence
  - lift-level progression
  - unresolved issues
  - recovery trend
  - next-week carryover

### A7. Calendar Rescheduling

Prompt:

> If today has a planned 60-minute Pull session, my calendar is blocked from 17:30 to 19:30, and I never train after 20:00, what would you recommend?

Key response:

- 19:30-20:00 leaves only 30 minutes, not enough for a 60-minute Pull session.
- The only full-session option is before 17:30.
- If earlier is impossible, either:
  - shorten to core lifts only: deadlift, pull-up, row
  - or move the session to the next open day
- It explicitly avoided forcing a 60-minute session into 30 minutes.

What we learned:

- It does exact schedule math.
- It offers a compressed-session fallback with named exercises.
- It protects the main lift from being rushed.

GarminCoach implication:

- Calendar-aware scheduling should support:
  - full fit
  - shortened fallback
  - move to next open day
- Shortened fallback should preserve the highest-value lifts.

## Cross-Pass Patterns

### What AthleteData Does Better Than GarminCoach Today

- It keeps a coherent plan state across chat turns.
- It tracks open questions and unresolved blockers.
- It reasons at the lift level, not just the workout level.
- It distinguishes one-off noise from multi-day trend.
- It is very clear about when data is insufficient.
- It uses a high bar for proactive messages.
- It understands practical Garmin watch behavior and limitations.
- It respects user boundaries such as "no nutrition advice."

### Product Behaviors Worth Copying

- Current plan state summary on demand.
- Lift-specific progression rules with explainable reasons.
- Manual set logging when wearable data is incomplete.
- Pain/form blocker before scheduling or load increase.
- Research/no-mutation mode for product exploration and debugging.
- Evening push gate: "Would silence cost the user something real?"
- Weekly summary that carries decisions into next week.

### Behaviors To Be Careful With

- AthleteData eagerly treated early synthetic prompts as real plan updates.
- The bot can sound confident about Garmin watch behavior that still needs external verification.
- It asked for medical-adjacent pain details, which is useful, but GarminCoach should keep language conservative and avoid diagnosis.

## Recommended GarminCoach Roadmap Candidates

1. Add a persistent training plan state model:
   - goal
   - constraints
   - weekly structure
   - next 7-14 days
   - current working weights
   - unresolved blockers
   - progression rules

2. Add strength progression tracking:
   - per-lift working weight
   - target rep range
   - last set outcomes
   - progression action
   - progression reason

3. Add pain/form guardrails:
   - form instability blocks load increase
   - pain/weirdness blocks scheduling until clarified
   - offer conservative substitution/default

4. Improve proactive notification policy:
   - no metric narration
   - send only for plan changes, real risk, unresolved blockers, or user-approved check-ins
   - distinguish GarminCoach-owned workouts from external habits like soccer

5. Improve morning/evening prompt behavior:
   - morning: one clear call, timing, and concise rationale from available data
   - evening: silence unless tomorrow needs action

6. Improve weekly summary:
   - adherence
   - progression
   - missed-session cause
   - recovery trend
   - carryover decisions

7. Verify Garmin structured strength limitations:
   - exercise label fidelity
   - sets/reps/weight support
   - rest auto-advance behavior
   - lap-to-skip behavior
   - whether RPE or notes can be represented anywhere

## Suggested Next Research Questions

- Ask AthleteData to list all plan/log changes it made during the unguarded synthetic probes and whether it can undo or mark them as test data.
- Verify AthleteData dashboard state against its Telegram claims.
- Run a real post-workout analysis after an actual Garmin strength session.
- Ask how it handles a user changing from 3 gym days to 2 gym days.
- Ask how it handles a missed session with no explanation versus a missed session caused by calendar conflict.
- Ask what exact proactive message it would send after a real unplanned high-HR cardio session the day before soccer.
