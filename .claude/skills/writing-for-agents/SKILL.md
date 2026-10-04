---
name: writing-for-agents
description: How to write a document an agent will consume. Use before editing agent instructions, project knowledge pages, task briefs, registers, or skills, and whenever an agent failed to reach a page that existed.
---

> Adapted from Matt Pocock's `mattpocock/skills/productivity/writing-for-agents`, MIT licensed. The accompanying `LICENSE` preserves the upstream notice. The general principles below are retained; repository-specific examples and addenda are omitted.

Reference for writing any document an agent consumes: a skill, an `AGENTS.md` / `CLAUDE.md`, or a document reached by a pointer. The formatting differs; the writing does not: the same levers make each one predictable, since the agent takes the same _process_ every run rather than producing the same output.

When the document you're writing is a skill, read [`SKILL-MECHANICS.md`](SKILL-MECHANICS.md) for frontmatter, invocation choice, and router skills.

## Context pointers

A **context pointer** is a reference held in the agent's context that names out-of-context material and encodes the condition for reaching it. A skill's description is one; a line in `AGENTS.md` naming a document is the same object. The pointer's _wording_, not its target, decides when the agent reaches the material, and how reliably. A must-have target behind a weakly worded pointer is a variance bug: sharpen the wording first, and inline material only if sharpening fails.

A pointer does two jobs: state what the material is, and list the **branches** that should trigger reaching it (a branch is a distinct case the document handles, so different runs take different paths through the material). Every word of an always-loaded pointer costs on every turn, so it earns even harder pruning than the body:

- **Front-load the leading word**: the pointer is where it does its triggering work.
- **One trigger per branch.** Synonyms that rename a single branch are one branch written twice; collapse them and keep only genuinely distinct branches.
- Cut identity the body already carries.

## The two loads

Every document and pointer spends one of two budgets:

- **Context load** is the cost of always-loaded material on the agent's window: an `AGENTS.md` line, a skill description, anything sitting in context every turn, spending tokens and attention whether or not it fires.
- **Cognitive load** is the cost on the human: which documents exist and when to reach for each. The human is the index. Not a cost to minimise: it is the price of human agency; spend it where human judgement matters, remove it where it does not.

## Information hierarchy

A document is built from two content types: **steps** (the ordered actions the agent performs) and **reference** (definitions, rules, facts consulted on demand). The two mix freely: all steps (a recipe), all reference (a review's rules, this skill), or both. The core decision is where each piece sits on the information hierarchy, a ladder ranked by how immediately the agent needs the material:

1. **In-file step** is the primary tier: what the agent does, in order.
2. **In-file reference** is consulted on demand. Often a legitimately flat peer-set (every rule of a review on one rung), which is a fine arrangement, not a smell.
3. **Disclosed reference** is pushed out into a separate file, reached by a context pointer, loaded only when the pointer fires. It can be a sibling file or fully external reference.

Push too little down and the top bloats; push too much and you hide material the agent needs. That tension is the whole decision.

**Progressive disclosure** moves material down the ladder so the top stays legible. Branching is the cleanest disclosure test: inline what every branch needs, and disclose what only some branches reach. When steps exist, reference that should be disclosed buries them and makes attention less reliable.

**Co-location** decides what sits together at each level: keep a concept's definition, rules, and caveats under one heading rather than scattering them. Grouped material reads like documentation written for the agent.

**Sprawl** is the failure mode: a document too long even when every line is live and unique. Attention thins across the excess. Disclose reference behind pointers and split by branch or sequence so each path carries only what it needs.

## Steps and completion criteria

Every step ends on a **completion criterion**, the condition that tells the agent the work is done. Strong criteria are both checkable and exhaustive.

- **Clarity:** a vague bound invites premature completion. Sharpen the bound first; only when it remains irreducibly fuzzy and rushing is observed should the sequence be split across a real context boundary.
- **Demand:** “every modified model accounted for” requires more legwork than “produce a change list.” Demand also binds a flat reference set: “every rule applied” is an exhaustiveness bar.

## When to split

Splitting spends context and cognitive load, so make a cut only when it earns them:

- **By sequence:** split steps when later work tempts an agent to rush the current step. Hiding later steps works only across a real handoff or subagent dispatch.
- **By invocation:** see [`SKILL-MECHANICS.md`](SKILL-MECHANICS.md).

## Leading words

A **leading word** is a compact concept already living in the model's pretraining that the agent thinks with while running a document (_lesson_, _fog of war_, _tracer bullets_). Repeated as a token, it accumulates a distributed definition and anchors behaviour in few tokens. Coining a new word works only when you define it clearly; an existing word brings useful priors at no extra cost.

It anchors twice: in the body, it guides execution; in a pointer, it guides invocation. Look for opportunities to collapse repeated explanations into a shared term, such as “fast, deterministic, low-overhead” into _tight_, or “a loop you believe in” into _red_.

**Negation** can make forbidden behaviour more salient. State the target behaviour positively; keep a prohibition only when it is a necessary hard guardrail, and pair it with the positive target.

## Pruning

- Keep each meaning in a **single source of truth**. Duplication costs maintenance and inflates its prominence.
- The **environment** is also a source of truth (`tool scripts` scripts, configuration, directory layout, help output). A document that restates it is a **cache**, which earns its load only when lookup is expensive. Cache unwritten conventions, reasons, and gotchas; leave simple lookups to the environment.
- Check each line for relevance. Stale layers settle as **sediment**; disclose reference and remove obsolete lines.
- Hunt **no-ops** sentence by sentence. Keep a sentence only if it changes behaviour versus the model's default; when it does not, remove it.
