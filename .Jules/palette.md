## 2024-10-24 - Make scrollable regions keyboard accessible
**Learning:** When using `overflow: auto` for horizontal scrolling (like data tables or charts) in static reports, these regions are unreachable for keyboard-only users unless explicitly made focusable.
**Action:** Always add `tabindex="0"`, an appropriate `aria-label`, and `role="region"` to scroll containers, and provide a clear `:focus-visible` indicator to improve accessibility without disrupting mouse users.
