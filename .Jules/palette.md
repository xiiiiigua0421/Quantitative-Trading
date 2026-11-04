## 2024-11-04 - Scrollable Container Keyboard Accessibility
**Learning:** Found that scrollable containers (with `overflow: auto`) like tables and charts in generated HTML reports are not accessible via keyboard without a `tabindex`, meaning keyboard-only users cannot easily scroll through large datasets or charts.
**Action:** Add `tabindex="0"`, `role="region"`, a localized `aria-label`, and a clear `:focus-visible` outline for any newly created or existing `overflow: auto` data wrappers in the reports to ensure keyboard scrollability and focus feedback.
