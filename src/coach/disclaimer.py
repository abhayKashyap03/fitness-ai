"""Medical disclaimer: one canonical text, every surface that gives advice.

§8.6 and [ADR-0018](../../docs/adr/0018-multi-tenant-foundation.md) both name
this a **hard prerequisite** rather than a backlog item. Until now the app
carried a notice on exactly one page — the invite form — while the dashboard,
the plan page and the coach chat, which are the surfaces that actually tell a
person what to eat, carried nothing.

Two decisions are load-bearing here.

**One text, one module.** The notice appears in the web UI, in the CLI, and
inside the model's own system prompt. This repo has already been bitten twice by
the same logic living in two places (`plan set` drifted between the CLI and the
web form; the credential path drifted between users). A disclaimer that says one
thing to the user and another to the model is the same bug with worse
consequences, so every surface imports from here.

**Acknowledgement is recorded, not assumed.** A footer nobody reads is the
failure mode the auth work already rejected once — "a runtime warning nobody
reads is not good enough". The notice is shown once, blocking, and the
acknowledgement is stored per user against :data:`DISCLAIMER_VERSION`. Revising
the text means bumping the version, which re-prompts everyone: an acknowledgement
of a *previous* text is not consent to this one.

Nothing here is legal advice or a legal shield. It is an honest description of
what the tool does, what it does not know, and where it is likely to be wrong.
"""

from __future__ import annotations

# Bump when SHORT/FULL change materially. Everyone re-acknowledges, because an
# acknowledgement is of a specific text, not of the idea of a disclaimer.
DISCLAIMER_VERSION = 1

SHORT = (
    "Not a medical device. Targets are computed from your own logged data "
    "and can be wrong."
)

# Plain language on purpose. The generic version of this paragraph ("consult a
# physician before beginning any exercise program") is worth nothing to a reader
# because it is worth nothing to the writer — it names no actual failure. The
# limitations below are the ones this codebase has measured and documented:
# self-reported intake error (risk #7), recovery as signal not diagnosis (§8.6),
# and safety limits that are fixed constants rather than clinical judgement.
FULL = """\
This is a personal project, not a medical device.

WHAT IT DOES
It reads data you have logged — recovery, sleep, body weight, training and food —
computes targets from it arithmetically, and explains the result in plain
language. Every number it shows you comes from your own data.

WHAT IT IS NOT
It is not a doctor, a dietitian, or a diagnosis. It does not know your medical
history, your medications, your blood work, or anything you have not logged. It
cannot tell you whether a number is normal for you specifically, and it has no
way to notice that something is wrong beyond the few things it measures.

WHERE IT IS LIKELY TO BE WRONG
- Logged food is self-reported, and self-reported intake typically runs 20-40%
  below what was actually eaten. Every calorie target built on it inherits that
  error, and the tool cannot detect it.
- Low recovery means "consider training lighter". It never means you are ill.
- The safety limits are fixed numbers written into the code: a minimum daily
  calorie floor and a maximum rate of weight loss. They are general limits, not
  limits chosen for your body by anyone who has examined it.
- Missing data is shown as missing, never as zero — but a plan built on sparse
  logging is still a plan built on sparse logging.

TALK TO A CLINICIAN BEFORE USING THIS IF
you have any medical condition, are pregnant or breastfeeding, are under 18,
take medication that affects weight, appetite or blood sugar, or have any
history of disordered eating. That last one especially: a tool that puts a
daily calorie number in front of you every morning is not a neutral object.

You are responsible for what you do with the numbers.
"""

# Injected into the coach's system prompt, so the model is held to the same
# scope the user was shown. Kept adjacent to FULL for exactly that reason.
LLM_SCOPE = """\
SCOPE — you are not a medical professional:
- Do not diagnose, read labs as diagnosis, or advise on medication.
- Low recovery means "train lighter," never "you are ill."
- Do not tell the user a measurement is normal, healthy, or concerning for them
  personally. You do not have their history and cannot know that.
- If asked something medical, say plainly that it is outside what you can answer
  and belongs with a clinician. Do not answer it partially or hedge into it.
- Never present restriction as a goal to hit. When a safety limit fires, surface
  it as written and do not optimise around it.
"""
