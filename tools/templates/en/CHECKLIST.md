# Checklist

What needs checking by hand. Every item: **what to do** · **what we expect (PASS/FAIL)**. Tick it only when there is a number
or a screenshot. Finished items move down to "Where it went".

## Open

## Where it went

## Typical web-project checks (copy the ones you need into "Open")
- User data: changes never wipe what was already saved; tests use their own storage, not the live one.
- Destructive actions (delete, reset, import over existing): ask for confirmation.
- Import/export, if present: round-trips without loss.
- Keyboard and contrast: Tab reaches everything, focus is visible, text is readable.
- Layout: phone, tablet, wide screen — no horizontal scrolling.
- "Empty", "loading" and "error" states look sensible.
- What a machine cannot judge (taste, "feels nice", hardware) stays with you: "✅ machine-checked · look yourself: what to open".
- If something you already accepted is changed, a NEW re-check item is added to this list.
