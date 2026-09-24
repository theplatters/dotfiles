# Manifesto

Status (Sep 2026): **governing document.** These five principles decide
what this desktop environment is for and how it behaves.
`docs/roadmap.md` holds unscheduled ideas, `docs/streamlining.md` holds
the coherence findings loop, and `.pi/SYSTEM.md` holds the agent's
operating policy — all three derive from this file. When code and
manifesto disagree, either the code changes or this file is amended
deliberately; silent drift is a defect.

## 1. The desktop follows me

This environment serves a project-based workflow: I organize my thoughts
into projects and can capture, resume, and navigate them quickly.

In practice:

- Projects are first-class: a stable registry identity, one current
  project the whole shell agrees on, and per-project context that
  follows me into every surface.
- Capture is one gesture to the right destination; resume is
  preview-first; navigation never dead-ends.
- A surface earns its place only if it makes a project easier to
  capture into, resume, or navigate.

## 2. AI serves me, not the other way around

The agent is a personal assistant with real capabilities on my laptop.
Its job is to make my day easier — never to create work, ceremonies, or
obligations for me.

In practice:

- The agent acts on the machine (notes, files, agenda, shell) through
  explicit, previewable approvals; consent is the contract.
- Nothing waits on me without a safe way to defer it, and AI activity
  never gates my navigation.
- I do no bookkeeping for the AI: no mandatory labels, no state I have
  to feed so the model can function.

## 3. Route actions to the cheapest mechanism that can own the outcome

The old local-first rule is retired. It is replaced by this routing
rule:

- **Deterministic.** When the correct action follows from rules and
  state, code decides. Nothing goes to a model that a rule can own.
- **Classifier.** When the action is a choice, score, or yes/no over a
  predefined outcome space but needs judgment over messy input, a
  classifier (for example Jev) decides.
- **Agent.** When the outcome is not predefined — generation,
  open-ended reasoning, tool use — the Pi agent owns it.

The tiers are ordered: never send to a model what a rule or classifier
can own. A classifier only picks within its outcome space: it never
generates prose, never becomes a security boundary, and never writes on
its own.

Data-protection rules are orthogonal and stay in force: untrusted input
stays untrusted, secrets never enter prompts or egress text, egress text
passes `text_safety.py`, and every write keeps prepare → exact preview →
Confirm. What is retired is the blanket ban on model calls and hosted
inference, not the safety envelope around them.

## 4. One environment, not an assembly

Unified feel is the priority. This must read as a production-grade
desktop environment tailored to me, never as a collection of hacks.

In practice:

- One visual language (theme tokens), one interaction grammar, one home
  per piece of information, one rule per state concern.
- Shared components over per-surface copies; delete, do not deprecate.
- The coherence dimensions and the report → fix loop live in
  `docs/streamlining.md`; findings there are judged against this file.

## 5. Speed first

Everything must feel smooth, and the shell's performance footprint
stays minimal.

In practice:

- No busywork: poll only what is visible or needed, centralize shared
  state, and never fork per tick what one sampler can own.
- Work is bounded and debounced; cold surfaces show last-known content
  while refreshing in the background instead of blocking.
- A new surface states its cost and lives within the existing polling
  and rendering budget.

## How adherence is checked

- Findings from audits and live use are filed in the ledger
  (`docs/streamlining.md`) with the rule they violate; fixes are
  verified in the running shell, not only in tests.
- The agent's operating policy (`.pi/SYSTEM.md`) derives from this
  file; where it conflicts, this file wins.
